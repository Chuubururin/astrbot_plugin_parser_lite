"""roll_local 变更检测与归档解包契约：提交主题解析、祖先校验门、提取门。

scripts/roll_local.py 是本地 roll 的编排层；本文件钉住 _detect 的两条
不可混淆的语义——「解析失败」必须响亮退出（静默 unknown 会让祖先校验门
误判为 PR 预览而跳过 roll，未合入代码滞留 vendor），「合法 PR 预览」
则是正常的跳过 + state 留痕路径——以及归档逐成员安全解包的规范化门
（拒绝绝对路径/上跳分量/链接成员，先于 realpath 校验）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# roll_local 的 sys.path 垫片会导入 scripts/ 下的平级模块；测试侧同样导入，
# 以便用身份断言（is）钉死「双轨共用同一实现」（M16）。
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import pipeline_common  # noqa: E402

_LOCAL_PLANE_REF = "origin/main"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def roll() -> ModuleType:
    return _load("roll_local", REPO_ROOT / "scripts" / "roll_local.py")


_SHA_A = "a" * 40
_SHA_B = "b" * 40


def test_detect_loud_failure_on_unparseable_subject(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """提交主题不含 main sha 时 _detect 响亮 SystemExit（不静默记 unknown）。"""
    monkeypatch.setattr(roll, "_fetch_standalone", lambda: None)
    monkeypatch.setattr(
        roll,
        "_git_or_die",
        lambda args: _SHA_A if args[:2] == ["rev-parse", "FETCH_HEAD"] else "weird publish subject",
    )
    state = tmp_path / "sync-state.json"
    state.write_text(json.dumps({"standalone_sha": _SHA_B}), encoding="utf-8")
    monkeypatch.setattr(roll, "STATE_PATH", state)
    with pytest.raises(SystemExit, match="无法解析 main sha"):
        roll._detect()


def test_detect_parses_auto_publish_subject(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上游自动发布主题正常解析出 main sha，且解析经共享底座（M16）。

    额外断言「真的调用了 parse_build_source_sha」：仅靠身份断言（导入的是同一
    函数）无法防住「就地另写一份、把导入变成死代码」。
    """
    forwarded: list[str] = []

    def fake_parse(subject: str) -> str:
        forwarded.append(subject)
        return _SHA_B

    monkeypatch.setattr(roll, "parse_build_source_sha", fake_parse)
    monkeypatch.setattr(roll, "_fetch_standalone", lambda: None)
    monkeypatch.setattr(
        roll,
        "_git_or_die",
        lambda args: (
            _SHA_A
            if args[:2] == ["rev-parse", "FETCH_HEAD"]
            else f"auto: publish standalone for {_SHA_B}"
        ),
    )
    state = tmp_path / "sync-state.json"
    state.write_text(json.dumps({"standalone_sha": "c" * 40}), encoding="utf-8")
    monkeypatch.setattr(roll, "STATE_PATH", state)
    new_standalone, new_main, old_standalone = roll._detect()
    assert (new_standalone, new_main, old_standalone) == (_SHA_A, _SHA_B, "c" * 40)
    assert forwarded == [f"auto: publish standalone for {_SHA_B}"]


def test_ancestor_gate_skips_pr_preview_and_records_state(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """构建源不在远端 main 祖先内 → 跳过 vendor roll，state 留痕 last_skipped_build。

    这是与「解析失败」互斥的正常路径：main sha 解析成功但祖先校验不过
    （PR 预览构建），平面照常跟 main、vendor 基线回退保留。
    """
    state_path = tmp_path / "sync-state.json"
    state_path.write_text(
        json.dumps({"standalone_sha": _SHA_B, "consecutive_failures": 0}), encoding="utf-8"
    )
    monkeypatch.setattr(roll, "STATE_PATH", state_path)
    monkeypatch.setattr(roll, "_fetch_standalone", lambda: None)
    monkeypatch.setattr(roll, "_fetch_main", lambda: None)
    monkeypatch.setattr(roll, "_tree_version", lambda ref: "1.3.7-pre-release.9")
    monkeypatch.setattr(roll, "_is_ancestor", lambda candidate, tip: False)
    # 平面提取源与祖先门基准均委托共享底座（M16），故在共享函数层打桩：
    # 远端 tip 与本地 ref 一致 → 不触发 PLITE_SYNC_REF。
    monkeypatch.setattr(roll, "remote_branch_sha", lambda repo: _SHA_B)
    monkeypatch.setattr(roll, "resolve_plane_ref", lambda repo: _LOCAL_PLANE_REF)
    # 基线检出 + 注入/校验/状态写入后的 git status：全部干净通过
    monkeypatch.setattr(roll, "_git", lambda args: (0, ""))

    def _fake_run(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(roll.subprocess, "run", _fake_run)
    roll._roll(_SHA_A, _SHA_B, _SHA_B)
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    assert recorded["last_skipped_build"]["sha"] == _SHA_A
    assert recorded["last_skipped_build"]["main_sha"] == _SHA_B
    assert recorded["last_skipped_build"]["version"] == "1.3.7-pre-release.9"


# ---- 归档逐成员安全解包：规范化门先于 realpath 校验 ----


def _tar_blob(members: list[tuple]) -> bytes:
    """内存构造归档：(成员名, 类型, 内容[, mode])。类型 dir/sym/lnk/fifo/file。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        for entry in members:
            name, kind, data, *rest = entry
            info = tarfile.TarInfo(name)
            info.mode = rest[0] if rest else 0o644
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/evil"
                tar.addfile(info)
            elif kind == "lnk":
                info.type = tarfile.LNKTYPE
                info.linkname = "src/pkg/config.py"
                tar.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tar.addfile(info)
            else:
                assert data is not None
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_archive_keeps_normal_file(roll: ModuleType, tmp_path: Path) -> None:
    """常规文件成员正常落盘（目录成员递归创建）。"""
    blob = _tar_blob([("src/pkg/config.py", "file", b"x = 1\n"), ("src/pkg", "dir", None)])
    dest = tmp_path / "out"
    roll._extract_archive_blob(blob, dest)
    assert (dest / "src" / "pkg" / "config.py").read_bytes() == b"x = 1\n"


def test_extract_archive_rejects_parent_jump(roll: ModuleType, tmp_path: Path) -> None:
    """上跳分量（../）成员在规范化门响亮拒绝，不落盘。"""
    blob = _tar_blob([("../escape.py", "file", b"evil\n")])
    with pytest.raises(SystemExit, match="上跳分量"):
        roll._extract_archive_blob(blob, tmp_path / "out")
    assert not (tmp_path / "escape.py").exists()


def test_extract_archive_rejects_absolute_member(roll: ModuleType, tmp_path: Path) -> None:
    """绝对路径成员在规范化门响亮拒绝。"""
    blob = _tar_blob([("/etc/evil.py", "file", b"evil\n")])
    with pytest.raises(SystemExit, match="绝对路径"):
        roll._extract_archive_blob(blob, tmp_path / "out")


def test_extract_archive_rejects_symlink_member(roll: ModuleType, tmp_path: Path) -> None:
    """符号链接成员响亮拒绝（快照内不应存在，静默跳过会让 vendor 缺文件）。"""
    blob = _tar_blob([("src/link", "sym", None)])
    with pytest.raises(SystemExit, match="符号/硬链接"):
        roll._extract_archive_blob(blob, tmp_path / "out")


def test_extract_archive_rejects_hardlink_member(roll: ModuleType, tmp_path: Path) -> None:
    """硬链接成员与符号链接同门拒绝（链接语义一律不进 vendor 树）。"""
    blob = _tar_blob([("src/pkg/config.py", "file", b"x = 1\n"), ("src/hard", "lnk", None)])
    with pytest.raises(SystemExit, match="符号/硬链接"):
        roll._extract_archive_blob(blob, tmp_path / "out")


def test_extract_archive_rejects_nonregular_member(roll: ModuleType, tmp_path: Path) -> None:
    """非常规成员（fifo 等）必须响亮拒绝——静默跳过会造成 vendor 树缺文件。"""
    blob = _tar_blob([("src/fifo", "fifo", None)])
    with pytest.raises(SystemExit, match="非常规类型"):
        roll._extract_archive_blob(blob, tmp_path / "out")


# ---- vendor 重建：显式 sha 源（FETCH_HEAD 竞态回归钉扎）----


def test_rebuild_vendor_uses_explicit_sha_not_fetch_head(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_rebuild_vendor 的检出/归档源必须是显式 standalone sha。

    这里钉死的是 FETCH_HEAD 竞态：_roll 在 _rebuild_vendor 前会 _fetch_main
    覆写 FETCH_HEAD；且 main 根结构与 standalone 不同（根上无
    pyproject.toml），沿用 FETCH_HEAD 误档 main 树会直接 FileNotFoundError。
    本测试以「归档参数含目标 sha、全程不出现 FETCH_HEAD」钉死该不变量。
    """
    vendor_pkg = tmp_path / "vpkg"
    vendor_meta = tmp_path / "vmeta"
    monkeypatch.setattr(roll, "VENDOR_PKG", vendor_pkg)
    monkeypatch.setattr(roll, "VENDOR_META", vendor_meta)
    blob = _tar_blob(
        [
            ("src/nonebot_plugin_parser_lite/config.py", "file", b"class Config: ...\n"),
            ("pyproject.toml", "file", b'[project]\nversion = "9.9.9"\n'),
            ("requirements.txt", "file", b"anyio\n"),
            ("README.md", "file", b"# up\n"),
            ("LICENSE", "file", b"MIT\n"),
        ]
    )
    git_calls: list[list[str]] = []
    bytes_calls: list[list[str]] = []

    def fake_git(args: list[str]) -> tuple[int, str]:
        git_calls.append(args)
        return 0, ""

    def fake_git_bytes(args: list[str]) -> tuple[int, bytes]:
        bytes_calls.append(args)
        return 0, blob

    monkeypatch.setattr(roll, "_git", fake_git)
    monkeypatch.setattr(roll, "_git_bytes", fake_git_bytes)
    roll._rebuild_vendor(_SHA_A)
    assert ["checkout", _SHA_A] in git_calls
    assert ["archive", _SHA_A] in bytes_calls
    assert not any("FETCH_HEAD" in a for call in git_calls + bytes_calls for a in call)
    assert (vendor_meta / "pyproject.toml").is_file()
    assert (vendor_pkg / "config.py").is_file()


# ---- H4：state 推进严格晚于「契约测试绿 + 提交成功」----

_SHA_C = "c" * 40
_SHA_D = "d" * 40


class _Result:
    """subprocess.run 的最小替身（_roll 只读 returncode/stdout/stderr）。"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _roll_harness(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_fake
) -> Path:
    """装配 _roll 全路径的假环境：state 与 vendor 元数据落 tmp，外部命令全假。

    返回 state 文件路径；初值为「已同步到 _SHA_C / v1.3.7 / 0 次失败」。
    """
    state_path = tmp_path / "sync-state.json"
    state_path.write_text(
        json.dumps(
            {
                "standalone_sha": _SHA_C,
                "main_sha": _SHA_D,
                "upstream_version": "1.3.7",
                "consecutive_failures": 0,
            }
        ),
        encoding="utf-8",
    )
    vmeta = tmp_path / "vmeta"
    vmeta.mkdir()
    (vmeta / "pyproject.toml").write_text('[project]\nversion = "1.3.7"\n', encoding="utf-8")
    monkeypatch.setattr(roll, "STATE_PATH", state_path)
    monkeypatch.setattr(roll, "VENDOR_META", vmeta)
    monkeypatch.setattr(roll.os, "chdir", lambda path: None)
    monkeypatch.setattr(roll, "_ensure_clone", lambda: None)
    monkeypatch.setattr(roll, "_detect", lambda: (_SHA_A, _SHA_B, _SHA_C))
    monkeypatch.setattr(roll, "_fetch_standalone", lambda: None)
    monkeypatch.setattr(roll, "_fetch_main", lambda: None)
    monkeypatch.setattr(roll, "_tree_version", lambda ref: "1.3.7")
    monkeypatch.setattr(roll, "_is_ancestor", lambda candidate, tip: True)
    monkeypatch.setattr(roll, "_rebuild_vendor", lambda sha: None)
    monkeypatch.setattr(roll, "remote_branch_sha", lambda repo: _SHA_B)
    monkeypatch.setattr(roll, "resolve_plane_ref", lambda repo: _LOCAL_PLANE_REF)
    monkeypatch.setattr(roll, "_git_bytes", lambda args: (0, b""))
    monkeypatch.setattr(roll.subprocess, "run", run_fake)
    return state_path


def test_roll_pytest_failure_does_not_advance_state_and_counts_failures(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H4：契约测试失败时不得推进 standalone_sha，且失败计数逐次递增（熔断有效）。

    推进时序是本断言的全部：若先推进 state 再跑 pytest，失败 roll 会被永久
    记为已同步（下次 _detect 见 sha 相同直接幂等退出，vendor 滞留旧版且再无
    重试触发点）；失败计数若被清零后仅由 main() +1，每次失败恒为 1、
    永达不到熔断阈值。
    """

    def _fake_run(cmd, **kw):
        if "pytest" in cmd:
            return _Result(1, "1 failed", "boom")
        return _Result(0)

    state_path = _roll_harness(roll, monkeypatch, tmp_path, _fake_run)
    for _ in range(2):
        with pytest.raises(SystemExit, match="契约测试失败"):
            roll.main([])
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    assert recorded["standalone_sha"] == _SHA_C
    assert recorded["main_sha"] == _SHA_D
    assert recorded["consecutive_failures"] == 2


def test_roll_commit_failure_does_not_advance_state(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H4：git commit 失败同样不得把 state 留在「已同步」——推进与提交同真同假。"""

    def _fake_run(cmd, **kw):
        if cmd[:2] == ["git", "commit"]:
            return _Result(1, "", "hook rejected")
        return _Result(0)

    state_path = _roll_harness(roll, monkeypatch, tmp_path, _fake_run)
    with pytest.raises(SystemExit, match="roll 提交失败"):
        roll.main([])
    recorded = json.loads(state_path.read_text(encoding="utf-8"))
    assert recorded["standalone_sha"] == _SHA_C
    assert recorded["consecutive_failures"] == 1


# ---- L1：版本读取与检出/归档同源（显式 sha，不依赖 FETCH_HEAD）----


def test_tree_version_reads_explicit_sha_not_fetch_head(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_tree_version 必须以显式 standalone sha 取 pyproject，不得沿用 FETCH_HEAD。

    读取经共享底座的 ``git_show_text``（不做通用换行翻译，L5）；参数断言同时
    钉死「显式 sha + 正确路径 + 共享读取器」三件事。
    """
    calls: list[tuple[Path, str, str]] = []

    def fake_show(repo: Path, ref: str, path: str) -> str:
        calls.append((repo, ref, path))
        return '[project]\nversion = "1.3.7"\n'

    monkeypatch.setattr(roll, "git_show_text", fake_show)
    assert roll._tree_version(_SHA_A) == "1.3.7"
    assert calls == [(roll.UPSTREAM_DIR, _SHA_A, "pyproject.toml")]
    assert not any("FETCH_HEAD" in ref for _, ref, _ in calls)


# ---- L2：--check 退出码语义（有变更即非零）----


def test_check_exit_code_nonzero_when_upstream_changed(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--check 检测到更新时退出码 1——退出码要能作「有更新」判据，恒 0 即失职。"""
    monkeypatch.setattr(roll.os, "chdir", lambda path: None)
    monkeypatch.setattr(roll, "_ensure_clone", lambda: None)
    monkeypatch.setattr(roll, "_detect", lambda: (_SHA_A, _SHA_B, _SHA_C))
    assert roll.main(["--check"]) == 1


def test_check_exit_code_zero_when_upstream_unchanged(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--check 无更新时退出码 0（幂等）。"""
    monkeypatch.setattr(roll.os, "chdir", lambda path: None)
    monkeypatch.setattr(roll, "_ensure_clone", lambda: None)
    monkeypatch.setattr(roll, "_detect", lambda: (_SHA_A, _SHA_B, _SHA_A))
    assert roll.main(["--check"]) == 0


# ---- L3：祖先校验门区分「非祖先」与「校验失败」----


def test_is_ancestor_distinguishes_git_error_from_not_ancestor(
    roll: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退出码 1 = 非祖先；128（对象缺失等）= 校验失败，必须响亮而非静默当 False。

    把 128 当 False 会把「校验失败」误判为 PR 预览构建并跳过 vendor roll，
    未合入代码反而滞留 vendor。
    """

    def _cp(returncode: int, stderr: bytes = b"") -> object:
        return type("CP", (), {"returncode": returncode, "stderr": stderr, "stdout": b""})()

    # 祖先语义已委托共享底座（M16），故在 pipeline_common._git 层打桩
    monkeypatch.setattr(pipeline_common, "_git", lambda repo, args: _cp(0))
    assert roll._is_ancestor(_SHA_A, _SHA_B) is True
    monkeypatch.setattr(pipeline_common, "_git", lambda repo, args: _cp(1))
    assert roll._is_ancestor(_SHA_A, _SHA_B) is False
    monkeypatch.setattr(
        pipeline_common, "_git", lambda repo, args: _cp(128, b"fatal: Not a valid object name")
    )
    with pytest.raises(SystemExit, match="merge-base"):
        roll._is_ancestor(_SHA_A, _SHA_B)


# ---- M17：归档解包恢复 mode 位；安全门先于任何落盘 ----


def test_extract_archive_preserves_file_mode(roll: ModuleType, tmp_path: Path) -> None:
    """成员 mode 位落盘后恢复（CI 侧 cp -a 保留）：只写内容会让双轨树 mode 分叉。

    两个成员分别给 0o755/0o644，钉住「按成员恢复」而非一刀切。
    """
    blob = _tar_blob(
        [
            ("src/pkg/tool.sh", "file", b"#!/bin/sh\necho hi\n", 0o755),
            ("src/pkg/config.py", "file", b"x = 1\n", 0o644),
        ]
    )
    dest = tmp_path / "out"
    roll._extract_archive_blob(blob, dest)
    assert (dest / "src" / "pkg" / "tool.sh").stat().st_mode & 0o777 == 0o755
    assert (dest / "src" / "pkg" / "config.py").stat().st_mode & 0o777 == 0o644


def test_extract_archive_validates_all_members_before_writing(
    roll: ModuleType, tmp_path: Path
) -> None:
    """安全门先于任何落盘：靠后成员违规时，靠前合法成员也不得落盘。"""
    blob = _tar_blob(
        [("src/ok.py", "file", b"ok\n"), ("../escape.py", "file", b"evil\n")],
    )
    with pytest.raises(SystemExit, match="上跳分量"):
        roll._extract_archive_blob(blob, tmp_path / "out")
    assert not (tmp_path / "out" / "src" / "ok.py").exists()


def test_plane_truth_disciplines_delegate_to_shared_base(roll: ModuleType) -> None:
    """M16：roll_local 的编排层真值纪律必须是共享底座的同一函数对象。

    双轨各写一份时细节必然漂移（远端真值兜底、祖先门退出码语义、主题解析
    的 unknown 兜底）。以身份断言（is）钉死委托关系：任何一侧被就地重写、
    或日后又复制一份，都会让本测试变红。
    """
    assert roll.is_ancestor is pipeline_common.is_ancestor
    assert roll.parse_build_source_sha is pipeline_common.parse_build_source_sha
    assert roll.remote_branch_sha is pipeline_common.remote_branch_sha
    assert roll.resolve_plane_ref is pipeline_common.resolve_plane_ref
    assert roll.git_show_text is pipeline_common.git_show_text


def test_roll_pytest_invocation_matches_gate_1() -> None:
    """roll 序列的 pytest 调用必须与门禁 1 完全同参（-c config + --rootdir）。

    裸 `-q` 拿不到仓库 pytest 配置（根目录无默认发现路径上的 ini），asyncio
    用例整批假红——管线红必须只可归因契约本身，不可归因调用形态漂移。
    """
    source = (REPO_ROOT / "scripts" / "roll_local.py").read_text(encoding="utf-8")
    invocation = '"python3", "-m", "pytest", "-c", "config/pyproject.toml", "--rootdir=.", "-q"'
    assert invocation in source
