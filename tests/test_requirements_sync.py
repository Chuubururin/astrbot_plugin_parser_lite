"""静态轨契约③：requirements 派生一致性（工单 09）。

requirements.txt / host-provided.txt 是 vendor/_upstream/pyproject.toml 的
派生产物，勿手改。本测试在临时目录运行真实派生脚本，再与仓库文件逐字节
比对：上游快照滚动后若忘记重跑 scripts/derive_requirements.py，此处变红。
"""

from __future__ import annotations

import importlib.util
import shutil
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "derive_requirements.py"
UPSTREAM_PYPROJECT = REPO_ROOT / "vendor" / "_upstream" / "pyproject.toml"


@pytest.fixture
def derived(tmp_path: Path) -> Path:
    """在临时根目录重放派生脚本，返回该根目录。"""
    spec = importlib.util.spec_from_file_location("derive_requirements", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    upstream_dir = tmp_path / "vendor" / "_upstream"
    upstream_dir.mkdir(parents=True)
    shutil.copy(UPSTREAM_PYPROJECT, upstream_dir)

    script.REPO_ROOT = tmp_path  # type: ignore[attr-defined] # 脚本所有读写以此为根
    script.main()
    return tmp_path


def _manifest_names(path: Path) -> set[str]:
    return {
        line.split(";")[0].split("[")[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_requirements_files_match_derivation(derived: Path):
    assert (derived / "requirements.txt").read_bytes() == (
        REPO_ROOT / "requirements.txt"
    ).read_bytes()
    assert (derived / "requirements" / "host-provided.txt").read_bytes() == (
        REPO_ROOT / "requirements" / "host-provided.txt"
    ).read_bytes()


def test_host_provided_subset_of_upstream():
    deps = tomllib.loads(UPSTREAM_PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    upstream = {dep.split(";")[0].split("[")[0].strip() for dep in deps}

    host = _manifest_names(REPO_ROOT / "requirements" / "host-provided.txt")
    assert host <= upstream, f"host-provided 声明了上游不存在的包：{sorted(host - upstream)}"


def test_two_manifests_partition_upstream_deps(derived: Path):
    req = _manifest_names(derived / "requirements.txt")
    host = _manifest_names(derived / "requirements" / "host-provided.txt")
    assert not req & host, f"两清单重叠：{sorted(req & host)}"
