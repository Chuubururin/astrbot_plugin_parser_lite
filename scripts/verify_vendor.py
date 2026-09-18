"""vendor 快照校验。

三层：
1. 无 nonebot 残留：vendor 全部 .py 的 import 不含 nonebot*；
2. 依赖覆盖：vendor 第三方顶层 import ⊆ requirements.txt ∪ host-provided.txt ∪ stdlib
   ∪ vendor 自身模块；
3. 一致性：存在 .sync-work/upstream 时，与上游产物逐字节比对
   （vendor 包 + vendor/_upstream 四个原样拷贝文件）；无克隆时显式打印
   [SKIPPED] 而非「通过」。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_PKG = REPO_ROOT / "vendor" / "nonebot_plugin_parser_lite"

IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)


def declared_dists() -> set[str]:
    """requirements.txt + host-provided.txt 声明的发行名（归一化为 import 名）。"""
    names: set[str] = set()
    for fname in ("requirements.txt", "requirements/host-provided.txt"):
        for line in (REPO_ROOT / fname).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            base = re.split(r"[\[<>=!~;\s]", line)[0].strip()
            if base:
                names.add(base.replace("-", "_").lower())
    # 常见 import 名 ↔ 发行名差异
    aliases = {
        "beautifulsoup4": "bs4",
        "protobuf": "google",  # google.protobuf 命名空间
    }
    for dist, mod in aliases.items():
        if dist in names:
            names.add(mod)
    return names


def vendor_modules() -> set[str]:
    mods = {"nonebot_plugin_parser_lite"}
    for p in VENDOR_PKG.rglob("__init__.py"):
        rel = p.parent.relative_to(VENDOR_PKG.parent)
        mods.add(".".join(rel.parts))
    # vendor 自带的 sys.modules 别名（utils/bilibili/__init__.py 把内嵌 proto 包
    # 注册为顶层 "bilibili"，pb2 生成代码据此绝对导入）
    mods.add("bilibili")
    return mods


def check_no_nonebot(violations: list[str]) -> None:
    for py in VENDOR_PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            violations.extend(
                f"{py.relative_to(REPO_ROOT)}: import {m}"
                for m in mods
                if m == "nonebot" or m.startswith("nonebot")
            )


def check_dependency_coverage(violations: list[str]) -> None:
    declared = declared_dists()
    local = vendor_modules() | {"vendor"}
    stdlib = set(sys.stdlib_module_names)
    for py in VENDOR_PKG.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            tops: list[str] = []
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops = [node.module.split(".")[0]]
            for t in tops:
                norm = t.replace("-", "_").lower()
                if norm not in declared and norm not in local and norm not in stdlib:
                    violations.append(
                        f"{py.relative_to(REPO_ROOT)}: 未声明依赖 '{t}' "
                        "(在 verify_vendor.declared_dists() 的 aliases 映射里补"
                        "发行名→import 名，或把该依赖加进上游 pyproject / host-provided)",
                    )


# 上游快照中由同步流水线原样拷贝的四个文件（与 roll_local 的 PLITES、
# sync-upstream.yml 的 cp -a 清单同源）。vendor/_upstream/*.json 是本仓提取产
# 物，由 tests/test_codegen.py 与 test_display_texts_match_upstream_when_clone_present
# 另行校验，不在此列。
_UPSTREAM_COPIED_FILES = ("pyproject.toml", "requirements.txt", "README.md", "LICENSE")


def check_upstream_consistency(violations: list[str]) -> bool:
    """第 3 层：与上游克隆逐字节比对；返回是否真正执行。

    vendor/_upstream 是版本/依赖/元数据的**唯一真值源**（metadata.yaml 版本、
    requirements.txt 派生、release tag 校验都依赖它），而此前第 3 层只遍历
    VENDOR_PKG（原实现里的 `"_upstream" in py.parts` 是死代码：_upstream 本就
    不在 rglob 范围内），该目录完全无校验——手改可静默传导到发布物
    （2026-09-17 评审 M4）。无克隆时显式声明 SKIPPED，不打印「通过」。
    """
    upstream_root = REPO_ROOT / ".sync-work" / "upstream"
    upstream_src = upstream_root / "src" / "nonebot_plugin_parser_lite"
    if not upstream_src.is_dir():
        print("  [SKIPPED] 第 3 层：无 .sync-work/upstream 克隆，上游产物漂移未校验")
        return False
    import filecmp

    for py in VENDOR_PKG.rglob("*"):
        # __pycache__/ 是 import vendor 产生的字节码缓存（本地与 CI runner 都会
        # 写入），不属于快照内容，排除出逐字节比对
        if not py.is_file() or "__pycache__" in py.parts:
            continue
        rel = py.relative_to(VENDOR_PKG)
        ref = upstream_src / rel
        if not ref.is_file() or not filecmp.cmp(py, ref, shallow=False):
            violations.append(f"vendor 与上游不一致: {rel}")

    for name in _UPSTREAM_COPIED_FILES:
        local = REPO_ROOT / "vendor" / "_upstream" / name
        ref = upstream_root / name
        if not local.is_file():
            violations.append(f"vendor/_upstream 缺上游文件: {name}")
        elif not ref.is_file():
            print(f"  [SKIPPED] 上游克隆缺 {name}，该文件未校验")
        elif not filecmp.cmp(local, ref, shallow=False):
            violations.append(f"vendor/_upstream 与上游不一致: {name}")
    return True


def main() -> int:
    violations: list[str] = []
    check_no_nonebot(violations)
    check_dependency_coverage(violations)
    layer3_ran = check_upstream_consistency(violations)
    if violations:
        print(f"FAIL: {len(violations)} 项违规")
        for v in violations[:30]:
            print(" -", v)
        return 1
    scope = "第 1/2/3 层" if layer3_ran else "第 1/2 层（第 3 层 SKIPPED）"
    print(f"verify_vendor: OK（{scope}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
