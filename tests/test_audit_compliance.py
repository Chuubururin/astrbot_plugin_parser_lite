"""上架合规扫描器（scripts/audit_compliance.py）的门禁行为钉扎。

规则集与文件范围对齐 astr-plugin-reviewer 的可机器化子集（logging /
loguru / requests 禁令、git tree 全部 .py）；注释与字符串中的规则文本
（roll 替换锚点、verify/测试断言）不得误报——AST 层收集天然保证。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit_compliance.py"


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--paths", *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text(
        "from astrbot.api import logger\n\nlogger.info('x')\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "上架合规扫描通过" in result.stdout


def test_detects_forbidden_roots(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import logging\nfrom loguru import logger\nimport requests\n", encoding="utf-8"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    for rule in ("logging", "loguru", "requests"):
        assert rule in result.stdout


def test_relative_import_form_is_caught(tmp_path: Path) -> None:
    (tmp_path / "rel.py").write_text("from . import logging\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1 and "logging" in result.stdout


def test_rule_text_in_strings_and_comments_is_ignored(tmp_path: Path) -> None:
    """roll 替换锚点 / verify 断言等合规设施文本不得误报（AST 层保证）。"""
    (tmp_path / "facility.py").write_text(
        'ANCHOR = "import logging\\n"\n'
        'ASSERT = "import logging" not in src\n'
        "# 上架规范：禁止 import logging\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unparseable_file_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    result = _run(tmp_path)
    assert result.returncode == 1 and "无法解析" in result.stdout
