"""上架合规扫描器：astr-plugin-reviewer 可机器化规则的仓库全 .py 门禁。

审核器（AstrBotDevs/astr-plugin-reviewer）扫 GitHub 仓库 git tree 的全部
.py 文件，prompt 硬规则包括：插件日志必须且只能来自 astrbot.api logger
（严禁内置 logging 模块与 loguru）、网络请求禁用 requests。本脚本以 AST
收集真实 import 节点——注释与字符串中的规则文本（roll 的替换锚点、
verify/test 的断言）天然不命中，无需豁免清单。在 roll、CI、release 三处
作门禁：上游新增违规在承接时即红，而非等市场审核反馈。

规则表（可扩展；「同步阻塞网络 I/O」「StarTools 持久化」等 AI 判断维度
不在此列，由审核器自行评定）：
- logging：import logging / from logging import ...
- loguru：import loguru / from loguru import ...
- requests：import requests / from requests import ...

用法::

     python scripts/audit_compliance.py            # 扫 git ls-files 的全部 .py
     python scripts/audit_compliance.py --paths DIR [DIR ...]   # 指定目录
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_ROOTS = {
    "logging": "上架规范：日志必须来自 astrbot.api logger，禁止内置 logging 模块",
    "loguru": "上架规范：禁止第三方日志库 loguru，日志来自 astrbot.api logger",
    "requests": "上架规范：网络请求禁用 requests，使用 httpx/aiohttp 等异步库",
}


def collect_py_files(paths: list[str] | None) -> list[Path]:
    """文件清单与审核器同源：默认 git tree 的全部 .py（git ls-files），
    天然排除 .sync-work/ 等忽略路径；--paths 指定时在其下收集 .py。"""
    if paths:
        files: list[Path] = []
        for raw in paths:
            root = Path(raw)
            files.extend(sorted(root.rglob("*.py")))
        return files
    out = subprocess.run(["git", "ls-files", "*.py"], capture_output=True, text=True, check=True)
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """单文件 AST 扫描，返回 (行号, 顶层模块, 规则说明) 违规清单。"""
    violations: list[tuple[int, str, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # 绝对导入看 module 顶层；相对导入（level>0）看 alias（包内
            # 顶层名不可能与禁用根重名，仅防御 from . import logging 形态）
            base_top = (node.module or "").split(".")[0] if node.module else ""
            imports = [(base_top, node.lineno)] if base_top else []
            imports += [(a.name.split(".")[0], node.lineno) for a in node.names if node.level > 0]
        else:
            continue
        for top, lineno in imports:
            rule = FORBIDDEN_ROOTS.get(top)
            if rule:
                violations.append((lineno, top, rule))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上架合规扫描（logging/loguru/requests 禁令）")
    parser.add_argument("--paths", nargs="*", help="限定扫描目录（默认 git ls-files 全部 .py）")
    args = parser.parse_args(argv)

    files = collect_py_files(args.paths)
    violations: list[str] = []
    scanned = 0
    for path in files:
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        try:
            found = scan_file(path)
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            violations.append(f"{rel}: 文件无法解析（{exc}）")
            continue
        scanned += 1
        for lineno, top, rule in found:
            violations.append(f"{rel}:{lineno}: import {top} —— {rule}")

    if violations:
        print("上架合规扫描 FAIL：")
        for item in violations:
            print(f"  - {item}")
        return 1
    print(f"上架合规扫描通过（{scanned} 个 .py，规则：{', '.join(sorted(FORBIDDEN_ROOTS))}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
