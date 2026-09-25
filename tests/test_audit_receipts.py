"""回执扫描器排除面契约：docstring 承诺被机械钉住。

默认扫描面枚举 git 跟踪文件（`.scratch/`、`docs/` 未跟踪，默认模式天然不
命中）；但 CLI 显式传路径时 `EXCLUDE_PARTS` 是唯一防线——本测试双向防
漂移：排除项必须兑现模块 docstring 的豁免声明（`.scratch`/`docs`/`vendor`），
且不得把 `doc/` 牵连进去（当前态文档正是扫描器要守的面）。
MC-15 是 advisory 通道，工具自身的边界失真不会被 CI 兜住，只能靠这里。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "audit_receipts", REPO_ROOT / "scripts" / "audit_receipts.py"
)
assert _spec is not None and _spec.loader is not None
audit: ModuleType = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

_RECEIPT_PROBE = "按 2026-01-01 的评审 M1 与上一版结论处理\n"


def test_exclude_parts_match_documented_promise() -> None:
    for part in ("vendor", ".sync-work", ".scratch", "docs"):
        assert part in audit.EXCLUDE_PARTS, f"豁免声明要求排除 {part}/，EXCLUDE_PARTS 缺项"
    assert "doc" not in audit.EXCLUDE_PARTS, "doc/ 是常规扫描面，不得被排除"
    assert "CHANGELOG.md" in audit.EXCLUDE_SUFFIXES


def test_explicit_scratch_path_not_scanned() -> None:
    scratch = REPO_ROOT / ".scratch"
    created = not scratch.exists()
    probe = scratch / "_audit_exclusion_probe.md"
    scratch.mkdir(parents=True, exist_ok=True)
    probe.write_text(_RECEIPT_PROBE, encoding="utf-8")
    try:
        assert audit.main([str(probe.relative_to(REPO_ROOT))]) == 0
    finally:
        probe.unlink()
        if created:
            scratch.rmdir()


def test_non_excluded_path_still_scanned() -> None:
    """对照组：未跟踪 ≠ 豁免——不在排除面的显式路径必须照常判红。"""
    probe = REPO_ROOT / "_audit_inclusion_probe.md"
    probe.write_text(_RECEIPT_PROBE, encoding="utf-8")
    try:
        assert audit.main([probe.name]) == 1
    finally:
        probe.unlink()
