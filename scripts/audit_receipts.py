r"""回执污染扫描器（非阻断）：doc/注释只陈述当前态，不记录开发过程。

**为什么存在**：模型易把「纠正」当「新增要求」累积进产物——注释与文档里
残留的日期戳、评审编号、票号、「上一版曾如何、本版改成如何」属于开发
过程的回执（Receipt Pollution），不是系统当前事实。理想模型要求：最终
产物只写现在是什么样，被否决/撤销/试错的历史视为从未存在；过程记录只归
`doc/CHANGELOG.md`、ADR 与 retired 结构化条目。

**判据边界**（与「如实声明非目标」严格区分）：
- 命中：日期戳、`评审 M\d`、票号、`上一版/原实现/曾是/第一版踩过坑/作废`
  等**过程叙事**标记——它们描述「发生过什么」，不描述「是什么」。
- 放行：合法的缺口/非目标当前态声明（`不做 SAST`、`服务端配置无漂移巡检`、
  `移除了 codeql.yml，因为…`）不含上述过程标记，天然不命中。

**扫描面**：git 跟踪的 `.py`（仅 `#` 注释 + docstring，含常量属性 docstring；
避开字符串字面量与测试断言）、`.md`、`.jinja`（全文）、`.yml`/`.yaml`
（仅注释，值不扫）。豁免：`CHANGELOG.md`（历史落点）、`.scratch/`
（本地票据，历史区）、`vendor/`（零修改快照）、`docs/`（未跟踪的本地产物区）、
本脚本自身（内含标记正则字面量）。

退出码：有命中→1，干净→0。由 `maintenance/checklist.json` 的 MC-15 以
advisory 方式消费（命中显红但不阻断 CI——维护清单通道整体 exit 0）。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 回执标记 = 「发生过什么」的过程叙事，不是「是什么」的当前事实。
# 只取高置信模式（日期戳 / 评审号 / 票号 / 版本前后 / 曾如何 / 初版踩坑）；
# 泛化词（试错、作废、撤销）会误伤法则文本自身与合法的「非目标如实声明」，
# 故不入常驻护栏——它们对应的真实回执几乎必伴随下列高置信标记之一。
RECEIPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("date-stamp", re.compile(r"\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b")),
    ("review-id", re.compile(r"评审\s*[#M]?\d+")),
    ("ticket-ref", re.compile(r"票\s*0\d")),
    ("prior-version", re.compile(r"上一版|前一版|原实现")),
    ("was-once", re.compile(r"曾是|曾把|曾按|曾设|曾以|旧实现")),
    ("first-draft-pitfall", re.compile(r"第[一二]版.{0,8}(?:踩|失败|就)")),
)

# 行内豁免标记（确需保留的过程事实，如复评触发条件里的真实日期）。
IGNORE_MARKER = "audit-receipts:ignore"

# 排除项对整树扫描与显式传参两面同时生效（见 _is_scannable）：`.scratch` 与
# `docs` 均未被 git 跟踪、默认枚举不会命中，但显式传入时仍须挡住，兑现上方
# 豁免声明。`doc/` 是常规扫描面（当前态文档正是本工具要守的地方），与
# 未跟踪的 `docs/` 无关，不列入排除。
EXCLUDE_PARTS = ("vendor", ".sync-work", ".venv", ".git", "__pycache__", "docs", ".scratch")
EXCLUDE_SUFFIXES = ("CHANGELOG.md",)
SELF = Path(__file__).resolve()


SCANNABLE_SUFFIXES = (".py", ".md", ".jinja", ".yml", ".yaml")


def _is_scannable(path: Path) -> bool:
    """排除谓词单点：整树枚举与显式传参两面共用，防两处漂移。"""
    rel = path.relative_to(REPO_ROOT)
    return (
        path.is_file()
        and path.suffix in SCANNABLE_SUFFIXES
        and not any(part in EXCLUDE_PARTS for part in rel.parts)
        and rel.name not in EXCLUDE_SUFFIXES
        and path.resolve() != SELF
    )


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    files: list[Path] = []
    for raw in out.split(b"\x00"):
        if not raw:
            continue
        path = (REPO_ROOT / raw.decode("utf-8")).resolve()
        if _is_scannable(path):
            files.append(path)
    return files


def _docstring_ranges(tree: ast.Module) -> set[int]:
    """docstring 覆盖的行号（含多行区间）。

    两类都算：module/class/function 首语句 docstring，以及紧跟赋值的
    **常量属性 docstring**（module/class 级 ``Expr(Constant[str])``——
    工具链文档字符串惯例，本仓库大量用于常量语义说明）。
    """
    lines: set[int] = set[int]()

    def _add(value: ast.Constant) -> None:
        if isinstance(value.value, str):
            start = value.lineno
            end = getattr(value, "end_lineno", start) or start
            lines.update(range(start, end + 1))

    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                _add(body[0].value)
        if isinstance(node, ast.Module | ast.ClassDef):
            prev = None
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(prev, ast.Assign | ast.AnnAssign)
                ):
                    _add(stmt.value)
                prev = stmt
    return lines


def _scannable_python_lines(text: str) -> list[tuple[int, str]]:
    """仅返回注释行与 docstring 行（(行号, 内容)，行号 1-based）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return list(enumerate(text.splitlines(), start=1))
    doc_lines = _docstring_ranges(tree)
    comment_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
    except tokenize.TokenError:
        pass
    keep = comment_lines | doc_lines
    return [(n, line) for n, line in enumerate(text.splitlines(), start=1) if n in keep]


def _scannable_yaml_lines(text: str) -> list[tuple[int, str]]:
    """YAML 注释面：整行注释与行尾注释（引号内 '#' 不算），值不扫。"""
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        quote: str | None = None
        for idx, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
                continue
            if ch in "'\"":
                quote = ch
            elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
                out.append((lineno, line[idx:]))
                break
    return out


def _match(line: str) -> str | None:
    if IGNORE_MARKER in line:
        return None
    for label, pattern in RECEIPT_PATTERNS:
        if pattern.search(line):
            return label
    return None


def scan(paths: list[Path] | None = None) -> list[tuple[str, int, str, str]]:
    hits: list[tuple[str, int, str, str]] = []
    for path in paths if paths is not None else _tracked_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.suffix == ".py":
            candidates = _scannable_python_lines(text)
        elif path.suffix in (".yml", ".yaml"):
            candidates = _scannable_yaml_lines(text)
        else:
            candidates = list(enumerate(text.splitlines(), start=1))
        for lineno, line in candidates:
            label = _match(line)
            if label is not None:
                hits.append((rel, lineno, label, line.strip()[:120]))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描注释与文档中的回执污染（非阻断）")
    parser.add_argument("paths", nargs="*", help="限定扫描的文件/目录（默认全跟踪树）")
    args = parser.parse_args(argv)

    if args.paths:
        files: list[Path] = []
        for arg in args.paths:
            target = (REPO_ROOT / arg).resolve()
            if target.is_file():
                if _is_scannable(target):
                    files.append(target)
            elif target.is_dir():
                files.extend(p for p in target.rglob("*") if _is_scannable(p))
        files = sorted(dict.fromkeys(files))
    else:
        files = _tracked_files()

    hits = scan(files)
    if not hits:
        print(f"回执扫描干净：{len(files)} 个文件，0 命中")
        return 0
    print(f"回执污染命中 {len(hits)} 处（应为当前态，删除过程叙事）：\n")
    for rel, lineno, label, snippet in hits:
        print(f"  {rel}:{lineno}  [{label}]  {snippet}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
