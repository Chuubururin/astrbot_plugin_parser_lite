"""维护清单的活性守护。

``maintenance/checklist.json`` 是 agent 自主维护的唯一入口，但它自身
会随代码演进**静默过时**。最危险的失效模式不是「报错」而是「报绿」：detector
指向的 pytest 节点被改名后，清单会若无其事地继续输出 green，而真正的契约已
无人守护。

本文件把清单当契约守护：结构字段、取值域、以及**每个 detector 指向的关卡必须
真实存在**。没有这层，清单只是一份会腐烂的文档。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKLIST_PATH = REPO_ROOT / "maintenance" / "checklist.json"
CHECKER_PATH = REPO_ROOT / "scripts" / "maintenance_check.py"


def test_filtered_run_reports_not_evaluated_instead_of_green() -> None:
    """过滤运行时 DoD 未求值必须非 0，而不是静默返回 0。

    此前 ``_evaluate_dod`` 在未跑 pytest 时返回 SKIPPED，而 ``main()`` 只把
    RED / UNKNOWN 当失败——退出码 0 会被 ``--json`` 的消费方读成「全绿」，
    与模块 docstring 的「0 = 全绿」契约冲突（2026-09-18 修复）。
    """
    result = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "--id", "MC-10"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "DoD 无法求值" in result.stdout


FAMILIES = {"plane", "platform", "contract", "ops"}
CLASSES = {"auto", "assisted", "escalate"}
SEVERITIES = {"blocking", "advisory"}
DETECTOR_KINDS = {"pytest", "cmd", "gh_runs", "event"}
ENTRY_REQUIRED = (
    "id",
    "title",
    "family",
    "class",
    "severity",
    "detector",
    "scope",
    "action",
    "escalate_if",
)


@pytest.fixture(scope="module")
def checklist() -> dict[str, Any]:
    return json.loads(CHECKLIST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("maintenance_check", CHECKER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collected_nodes() -> set[str]:
    """真实套件的全部 node id（参数化归并到基名）。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    nodes = {line.strip().split("[")[0] for line in result.stdout.splitlines() if "::" in line}
    assert nodes, "测试收集失败：" + (result.stdout + result.stderr)[-2000:]
    return nodes


def test_schema_version_is_supported(checklist: dict[str, Any]) -> None:
    assert checklist["schema_version"] == 1


def test_entry_ids_are_unique_and_well_formed(checklist: dict[str, Any]) -> None:
    ids = [entry["id"] for entry in checklist["entries"]]
    assert len(ids) == len(set(ids)), f"条目 id 重复：{ids}"
    assert all(item.startswith("MC-") for item in ids)


def test_entries_carry_required_fields(checklist: dict[str, Any]) -> None:
    for entry in checklist["entries"]:
        for field in ENTRY_REQUIRED:
            assert field in entry, f"{entry.get('id')} 缺字段 {field}"
        assert entry["title"].strip()
        assert entry["action"], f"{entry['id']} 的 action 不得为空"
        assert entry["escalate_if"].strip(), f"{entry['id']} 缺 escalate_if（agent 的停手边界）"
        for step in entry["action"]:
            assert isinstance(step, str), f"{entry['id']} 的 action 步骤必须是字符串：{step!r}"
        for item in entry.get("acceptance", []):
            assert isinstance(item.get("cmd"), str), f"{entry['id']} 的 acceptance 缺 cmd：{item!r}"
            ok = isinstance(item.get("expect_exit"), int)
            assert ok, f"{entry['id']} 的 expect_exit 必须是整数：{item!r}"


def test_enum_fields_are_in_domain(checklist: dict[str, Any]) -> None:
    for entry in checklist["entries"]:
        assert entry["family"] in FAMILIES, entry["id"]
        assert entry["class"] in CLASSES, entry["id"]
        assert entry["severity"] in SEVERITIES, entry["id"]
        assert isinstance(entry["scope"], list), entry["id"]


def test_detector_kinds_are_known(checklist: dict[str, Any]) -> None:
    for entry in checklist["entries"]:
        kind = entry["detector"].get("kind")
        assert kind in DETECTOR_KINDS, f"{entry['id']} 的 detector.kind 未知：{kind!r}"
        if kind == "pytest":
            assert entry["detector"].get("nodes"), f"{entry['id']} 缺 detector.nodes"
        if kind == "cmd":
            assert entry["detector"].get("cmd"), f"{entry['id']} 缺 detector.cmd"


def test_scope_paths_exist(checklist: dict[str, Any]) -> None:
    """scope 是 agent 的改动白名单；路径不存在说明清单或代码已漂移。"""
    for entry in checklist["entries"]:
        for path in entry["scope"]:
            assert (REPO_ROOT / path).exists(), f"{entry['id']} 的 scope 路径不存在：{path}"


def test_pytest_detectors_point_at_real_nodes(
    checklist: dict[str, Any],
    collected_nodes: set[str],
) -> None:
    """清单活性核心：detector 指向的 pytest 节点必须真实存在。

    节点被改名或删除后，maintenance_check 会报 STALE（退出码 2）而不是绿，但那种
    失效要靠「恰好跑一次」才发现。这里把它变成 CI 必红。
    """
    for entry in checklist["entries"]:
        detector = entry["detector"]
        if detector.get("kind") != "pytest":
            continue
        for node in detector["nodes"]:
            assert node in collected_nodes, f"{entry['id']} 指向不存在的 pytest 节点：{node}"


def _all_commands(checklist: dict[str, Any]) -> list[str]:
    cmds = [item["cmd"] for item in checklist["definition_of_done"]]
    for entry in checklist["entries"]:
        detector = entry["detector"]
        if "cmd" in detector:
            cmds.append(detector["cmd"])
        fallback = detector.get("fallback")
        if isinstance(fallback, dict) and "cmd" in fallback:
            cmds.append(fallback["cmd"])
        cmds.extend(item["cmd"] for item in entry.get("acceptance", []))
    return cmds


def test_commands_use_py_placeholder(checklist: dict[str, Any]) -> None:
    """命令必须用 {py} 占位符，不得硬编码解释器名。

    CI（setup-python）提供 ``python``，开发机往往只有 ``python3``；硬编码任一个
    都会在另一侧以 FileNotFoundError 响亮失败。
    """
    for cmd in _all_commands(checklist):
        assert "{py}" in cmd, f"命令未使用 {{py}} 占位符：{cmd}"
        assert not [tok for tok in cmd.split() if tok in ("python", "python3")], (
            f"命令硬编码了解释器名：{cmd}"
        )
    # action 步骤同样会被 agent 直接执行，不得残留硬编码解释器名
    for entry in checklist["entries"]:
        for step in entry["action"]:
            assert not [tok for tok in step.split() if tok in ("python", "python3")], (
                f"{entry['id']} 的 action 步骤硬编码了解释器名：{step}"
            )


def test_prohibited_actions_are_actionable(checklist: dict[str, Any]) -> None:
    for item in checklist["prohibited_actions"]:
        assert item["id"].startswith("P-"), item
        assert item["rule"].strip()
        assert item["why"].strip(), f"{item['id']} 缺 why（无理由的禁令无法被判断边界）"


def test_loop_guard_is_declared(checklist: dict[str, Any]) -> None:
    guard = checklist["loop_guard"]
    assert guard["max_attempts_per_entry"] >= 1
    assert guard["on_exceed"] == "escalate"


def test_checker_loads_the_committed_checklist(
    checker: ModuleType,
    checklist: dict[str, Any],
) -> None:
    """checker 与清单同源：加载结果与直接读取一致。"""
    loaded = checker.load_checklist(CHECKLIST_PATH)
    assert loaded["schema_version"] == checklist["schema_version"]
    assert [e["id"] for e in loaded["entries"]] == [e["id"] for e in checklist["entries"]]


def test_checker_expands_py_placeholder(checker: ModuleType) -> None:
    """输出里的命令必须可直接复制执行，不能残留 {py}。"""
    resolved = checker._resolve("{py} -m pytest -q")
    assert "{py}" not in resolved
    assert sys.executable in resolved


def test_checker_rejects_unknown_schema_version(checker: ModuleType, tmp_path: Path) -> None:
    """清单版本不受支持时响亮失败，而不是静默当成 v1 跑。"""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ValueError):
        checker.load_checklist(bad)


def test_render_markdown_lists_only_non_green(checker: ModuleType) -> None:
    """摘要只列非绿条目：PR 正文不该被十余条 green 淹没。"""
    payload = {
        "summary": {"green": 2, "red": 1, "skipped": 0, "event": 0, "unknown": 0},
        "definition_of_done": [],
        "entries": [
            {"id": "MC-01", "title": "平台", "detail": "退出码 0", "state": "green"},
            {"id": "MC-02", "title": "契约", "detail": "节点缺失", "state": "red"},
            {"id": "MC-03", "title": "运维", "detail": "退出码 0", "state": "green"},
        ],
    }
    text = checker._render_markdown(payload)
    assert "MC-02" in text
    assert "MC-01" not in text
    assert "MC-03" not in text
    assert "不要直接 automerge" in text


def test_render_markdown_reports_green_when_nothing_is_wrong(checker: ModuleType) -> None:
    payload = {
        "summary": {"green": 3, "red": 0, "skipped": 0, "event": 0, "unknown": 0},
        "definition_of_done": [],
        "entries": [
            {"id": "MC-01", "title": "平台", "detail": "退出码 0", "state": "green"},
        ],
    }
    text = checker._render_markdown(payload)
    assert "全绿" in text
    assert "|" not in text, "全绿时不应渲染表格"


def test_render_markdown_counts_definition_of_done_too(checker: ModuleType) -> None:
    """计数必须含 DoD。

    ``payload["summary"]`` 只统计 entries；若条目全绿而 DoD 变红，摘要会印出
    「green 2」却同时列出红行，自相矛盾（2026-09-18 实跑所见，已修）。
    """
    payload = {
        "summary": {"green": 2, "red": 0, "skipped": 0, "event": 0, "unknown": 0},
        "definition_of_done": [
            {"id": "DOD-01", "cmd": "pytest -q", "detail": "失败 1", "state": "red"},
        ],
        "entries": [
            {"id": "MC-01", "title": "平台", "detail": "退出码 0", "state": "green"},
            {"id": "MC-02", "title": "契约", "detail": "退出码 0", "state": "green"},
        ],
    }
    text = checker._render_markdown(payload)
    assert "red 1" in text, f"计数漏了 DoD 的红条目：{text!r}"
    assert "DOD-01" in text
    assert "DoD：" in text


def test_render_markdown_escapes_table_cells(checker: ModuleType) -> None:
    """detail / title 里的竖线会撕裂表格，换行会截断行。"""
    payload = {
        "summary": {"green": 0, "red": 1, "skipped": 0, "event": 0, "unknown": 0},
        "definition_of_done": [],
        "entries": [
            {"id": "MC-09", "title": "a|b", "detail": "第一行\n第二行|尾巴", "state": "red"},
        ],
    }
    text = checker._render_markdown(payload)
    row = next(line for line in text.splitlines() if line.startswith("| `MC-09`"))
    assert row.count(r"\|") == 2, f"竖线未转义：{row!r}"
    assert row.count("|") - row.count(r"\|") == 4, f"分隔符数不对：{row!r}"
    assert "第二行" in row, "换行应折叠进同一行"


def test_render_markdown_treats_unknown_as_stale(checker: ModuleType) -> None:
    """unknown = 清单过时（节点被改名等），必须与 red 一样进摘要。"""
    payload = {
        "summary": {"green": 0, "red": 0, "skipped": 0, "event": 0, "unknown": 1},
        "definition_of_done": [],
        "entries": [
            {"id": "MC-11", "title": "平台", "detail": "节点不存在", "state": "unknown"},
        ],
    }
    text = checker._render_markdown(payload)
    assert "MC-11" in text
