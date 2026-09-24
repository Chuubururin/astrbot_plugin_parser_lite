"""upstream_sync.classify 供应链判据契约：异常即红的三条机械闸门。

全自动 sync 管道里本脚本是唯一「绿≠恶意」防线（规格 D1）：测试钉三类判据
各自的命中/放行/边界（阈值恰等不红、+1 红）、fail-closed 语义（清单文件
出现/消失、TOML 不可解析都算命中），以及孤儿树（standalone 无父子关系）下
tree-to-tree diff 成立。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load("upstream_sync", REPO_ROOT / "scripts" / "upstream_sync.py")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, text=True
    )
    return result.stdout


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "up"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    return repo


def _tree_commit(repo: Path, files: dict[str, str], branch: str, message: str) -> str:
    """孤儿分支上提交一棵完整树（standalone 形态：每次构建无父子链）。"""
    _git(repo, "checkout", "-q", "--orphan", branch)
    _git(repo, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
    for stale in sorted(repo.iterdir()):
        if stale.name != ".git":
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


_BASE = {
    "requirements.txt": "httpx==0.27.0\nstarlette>=0.37\n",
    "pyproject.toml": (
        '[project]\nname = "p"\nversion = "1.0.0"\ndependencies = ["httpx>=0.27"]\n'
    ),
    "LICENSE": "MIT License\n",
    "src/pkg/mod.py": "x = 1\n",
}


def _kinds(report: dict) -> set[str]:
    return {hit["kind"] for hit in report["hits"]}


def test_clean_roll_passes_with_minor_content_change(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(upstream, {**_BASE, "src/pkg/mod.py": "x = 2\n"}, "b2", "行为改动，非判据面")
    report = sync.classify(upstream, old, new)
    assert report["verdict"] == "ok"
    assert report["hits"] == []
    assert report["stats"]["files_changed"] == 1


def test_dependency_manifest_change_blocks(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(
        upstream,
        {**_BASE, "requirements.txt": "httpx==0.27.0\nstarlette>=0.37\nevil-package==6.6.6\n"},
        "b2",
        "投毒主通道",
    )
    report = sync.classify(upstream, old, new)
    assert report["verdict"] == "blocked"
    assert "dep-manifest" in _kinds(report)
    evidence = "\n".join(line for h in report["hits"] for line in h["evidence"])
    assert "evil-package" in evidence


def test_whitespace_and_comment_churn_in_manifest_is_not_a_hit(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(
        upstream,
        {**_BASE, "requirements.txt": "HttpX  == 0.27.0  # pinned\n\nstarlette>=0.37\n"},
        "b2",
        "书写差异不是依赖变化",
    )
    assert "dep-manifest" not in _kinds(sync.classify(upstream, old, new))


def test_pyproject_optional_dependency_change_blocks(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(
        upstream,
        {
            **_BASE,
            "pyproject.toml": (
                '[project]\nname = "p"\nversion = "1.0.1"\ndependencies = ["httpx>=0.27"]\n'
                '[project.optional-dependencies]\nextra = ["evil>=1"]\n'
            ),
        },
        "b2",
        "optional 依赖也是面",
    )
    assert "dep-manifest" in _kinds(sync.classify(upstream, old, new))


def test_manifest_file_appearance_fails_closed(upstream: Path) -> None:
    old = _tree_commit(
        upstream, {k: v for k, v in _BASE.items() if k != "requirements.txt"}, "b1", "old"
    )
    new = _tree_commit(upstream, dict(_BASE), "b2", "清单出现")
    report = sync.classify(upstream, old, new)
    assert "dep-manifest" in _kinds(report)
    assert any("出现/消失" in line for h in report["hits"] for line in h["evidence"])


def test_unparseable_pyproject_change_fails_closed(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(upstream, {**_BASE, "pyproject.toml": "[[ unterminated\n"}, "b2", "坏 TOML")
    assert "dep-manifest" in _kinds(sync.classify(upstream, old, new))


def test_structural_file_count_boundary(upstream: Path) -> None:
    churn = {f"src/pkg/f{i}.py": "o = 0\n" for i in range(50)}
    base = _tree_commit(upstream, dict(_BASE), "b1", "基线")
    at_limit = _tree_commit(upstream, {**_BASE, **churn}, "b2", "恰 50 增")
    assert sync.classify(upstream, base, at_limit)["verdict"] == "ok"
    over = _tree_commit(upstream, {**_BASE, **churn, "src/pkg/f50.py": "o = 0\n"}, "b3", "第 51 增")
    report = sync.classify(upstream, base, over)
    assert "structural" in _kinds(report)
    assert report["stats"]["files_added"] == 51


def test_diff_lines_threshold_boundary(upstream: Path) -> None:
    base = _tree_commit(upstream, dict(_BASE), "b1", "基线")
    at_limit = _tree_commit(
        upstream,
        {**_BASE, "src/pkg/big.py": "a = 1\n" * sync.MAX_DIFF_LINES},
        "b2",
        "恰等行预算",
    )
    assert sync.classify(upstream, base, at_limit)["stats"]["diff_lines"] == sync.MAX_DIFF_LINES
    assert sync.classify(upstream, base, at_limit)["verdict"] == "ok"
    over = _tree_commit(
        upstream,
        {**_BASE, "src/pkg/big.py": "a = 1\n" * (sync.MAX_DIFF_LINES + 1)},
        "b3",
        "超行预算",
    )
    report = sync.classify(upstream, base, over)
    assert report["stats"]["diff_lines"] == sync.MAX_DIFF_LINES + 1
    assert "structural" in _kinds(report)


def test_license_change_blocks(upstream: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(upstream, {**_BASE, "LICENSE": "GPL v3\n"}, "b2", "换证")
    assert "license" in _kinds(sync.classify(upstream, old, new))


def test_identical_trees_across_orphan_commits_pass(upstream: Path) -> None:
    a = _tree_commit(upstream, dict(_BASE), "b1", "一")
    b = _tree_commit(upstream, dict(_BASE), "b2", "二")
    assert a != b  # 孤儿 commit sha 不同
    report = sync.classify(upstream, a, b)  # 但树逐字节相同 → diff 为空
    assert report["verdict"] == "ok"
    assert report["stats"] == {
        "files_added": 0,
        "files_removed": 0,
        "files_changed": 0,
        "diff_lines": 0,
    }


def test_cli_exit_codes_and_json_report(upstream: Path, tmp_path: Path) -> None:
    old = _tree_commit(upstream, dict(_BASE), "b1", "old")
    new = _tree_commit(upstream, {**_BASE, "LICENSE": "closed\n"}, "b2", "换证")
    out = tmp_path / "report.json"
    rc = sync.main(
        ["classify", "--repo", str(upstream), "--old", old, "--new", new, "--json", str(out)]
    )
    assert rc == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["report_version"] == 1
    assert report["verdict"] == "blocked"
    assert {hit["kind"] for hit in report["hits"]} == {"license"}
    assert sync.main(["classify", "--repo", str(upstream), "--old", new, "--new", new]) == 0
    assert sync.main(["classify", "--repo", str(upstream), "--old", "zz", "--new", new]) == 2
