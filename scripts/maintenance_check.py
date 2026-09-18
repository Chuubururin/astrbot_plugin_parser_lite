"""维护清单检测器：一次输出全部维护条目的状态（agent 主入口）。

清单事实源 = ``maintenance/checklist.json``。本脚本只做三件事：读清单、
按 detector 求值、把 ``action`` / ``escalate_if`` 一并吐出。它**不复制任何判据**
——所有 detector 都指向仓库既有的关卡（pytest 节点 / 脚本命令），否则「清单绿」
与「仓库绿」会分裂成两个互不相干的事实。

求值策略：pytest 类条目共享**一次**全量 ``pytest --junit-xml`` 运行，再按 node id
映射结果；逐条起一次 pytest 会把 11 条条目变成 11 次全量运行。

detector 种类（与清单 JSON 同构）：

- ``pytest``：给 pytest 节点列表，任一失败即红；节点不存在视为清单过时（退出码 2）；
- ``cmd``：跑命令，退出码不符即红；
- ``gh_runs``：查 workflow 最近 N 次运行结论（需 gh 与鉴权，可带 fallback）；
- ``event``：工作树中无法判定（如 sync PR 正文的 advisory），只提示上下文。

``--summary-md PATH`` 额外把判定摘要（Markdown，只含红/过时条目）落盘，供 roll
工作流的 Step Summary 与 PR 正文消费 —— 该通道是 **advisory**：红条目是刻意设置的
tripwire，升级路径是「开 PR + 关 automerge 交人工评审」，故工作流不得当硬门禁。

退出码：0 = 全绿；1 = 有红条目；2 = 清单过时或环境不可用（响亮失败）。
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKLIST_PATH = REPO_ROOT / "maintenance" / "checklist.json"

RED = "red"
GREEN = "green"
SKIPPED = "skipped"
EVENT = "event"
UNKNOWN = "unknown"

CLASS_ORDER = {"auto": 0, "assisted": 1, "escalate": 2}
FAMILIES = ("plane", "platform", "contract", "ops")
CLASSES = ("auto", "assisted", "escalate")
NL = chr(10)


def load_checklist(path: Path = CHECKLIST_PATH) -> dict[str, Any]:
    """读清单并校验 schema 版本；版本不符即响亮失败。"""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"清单 schema_version 不受支持：{data.get('schema_version')!r}")
    return data


def _run(cmd: str, *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    """跑一条命令（shlex 切分，不走 shell）。

    清单里的 ``{py}`` 占位符替换为当前解释器 ``sys.executable``：CI 与开发机的
    解释器名不一致（``python`` vs ``python3``），硬编码任一个都会在另一侧响亮失败。
    """
    return subprocess.run(
        shlex.split(cmd.replace("{py}", sys.executable)),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _tail(text: str) -> str:
    """取最后一行非空输出，用作失败详情。"""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "(无输出)"


def _case_outcome(case: ET.Element) -> str:
    """junit testcase → pass / fail / skip。"""
    for child in case:
        if child.tag in ("failure", "error"):
            return "fail"
        if child.tag == "skipped":
            return "skip"
    return "pass"


def _node_id(case: ET.Element) -> str:
    """junit testcase → ``tests/test_x.py::test_y``（参数化归并到基名）。"""
    file = case.get("file") or ""
    if not file:
        file = (case.get("classname") or "").replace(".", "/") + ".py"
    name = (case.get("name") or "").split("[")[0]
    return f"{file}::{name}"


def junit_outcomes() -> dict[str, str]:
    """跑一次全量 pytest，返回 ``{node_id: pass|fail|skip}``。

    参数化用例归并到基名（``test_x[a-b]`` → ``test_x``），任一参数失败即该节点
    失败——条目关心的是「这条契约是否被守住」，不是单个参数。
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        result = _run(
            f"{sys.executable} -m pytest -c config/pyproject.toml --rootdir=. -q "
            f"-p no:cacheprovider --junit-xml={report}"
        )
        if not report.is_file():
            raise RuntimeError(
                "pytest 未产出 junit 报告，无法求值（测试环境不可用？）："
                + _tail(result.stdout + result.stderr)
            )
        root = ET.parse(report).getroot()
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        node = _node_id(case)
        outcome = _case_outcome(case)
        if outcome == "fail" or node not in outcomes:
            outcomes[node] = outcome
    return outcomes


def _eval_pytest(spec: dict[str, Any], outcomes: dict[str, str]) -> tuple[str, str]:
    nodes: list[str] = list(spec.get("nodes", []))
    missing = [node for node in nodes if node not in outcomes]
    if missing:
        return UNKNOWN, f"清单过时：pytest 节点未在套件中出现 {missing}"
    failed = [node for node in nodes if outcomes[node] == "fail"]
    if failed:
        return RED, "失败节点：" + "、".join(failed)
    if all(outcomes[node] == "skip" for node in nodes):
        return SKIPPED, "全部节点被跳过（缺上游克隆或属 network 隔离区）"
    return GREEN, "全部节点通过"


def _eval_cmd(spec: dict[str, Any]) -> tuple[str, str]:
    expect = int(spec.get("expect_exit", 0))
    result = _run(str(spec["cmd"]))
    if result.returncode != expect:
        detail = _tail(result.stdout + result.stderr)
        return RED, f"退出码 {result.returncode}（期望 {expect}）：{detail}"
    return GREEN, f"退出码 {result.returncode}"


def _eval_gh_runs(spec: dict[str, Any]) -> tuple[str, str]:
    limit = int(spec["limit"])
    threshold = int(spec["red_when_failures_at_least"])
    if shutil.which("gh"):
        cmd = (
            f"gh run list --workflow={spec['workflow']} --status completed"
            f" --limit {limit} --json conclusion"
        )
        result = _run(cmd, timeout=120)
        if result.returncode == 0:
            runs = json.loads(result.stdout or "[]")
            if isinstance(runs, list):
                fails = sum(
                    1
                    for run in runs
                    if isinstance(run, dict) and run.get("conclusion") == "failure"
                )
                detail = f"最近 {limit} 次已完成运行中失败 {fails} 次（阈值 {threshold}）"
                return (RED if fails >= threshold else GREEN), detail
    fallback = spec.get("fallback")
    if isinstance(fallback, dict):
        state, detail = _eval_cmd(fallback)
        return state, f"gh 不可用或查询失败，回退本地状态：{detail}"
    return SKIPPED, "gh 不可用且清单未提供 fallback"


def _eval_event(spec: dict[str, Any]) -> tuple[str, str]:
    source = spec.get("source", "外部上下文")
    return EVENT, f"事件型：需在「{source}」中判定，工作树中不可求值"


CMD_EVALUATORS: dict[str, Callable[[dict[str, Any]], tuple[str, str]]] = {
    "cmd": _eval_cmd,
    "gh_runs": _eval_gh_runs,
    "event": _eval_event,
}


def _detect(entry: dict[str, Any], outcomes: dict[str, str]) -> tuple[str, str]:
    spec: dict[str, Any] = entry["detector"]
    kind = str(spec.get("kind"))
    if kind == "pytest":
        return _eval_pytest(spec, outcomes)
    evaluator = CMD_EVALUATORS.get(kind)
    if evaluator is None:
        return UNKNOWN, f"未知 detector.kind：{kind!r}"
    return evaluator(spec)


def _evaluate_dod(
    checklist: dict[str, Any],
    outcomes: dict[str, str],
) -> list[dict[str, Any]]:
    """求值完成定义。``pytest_all`` 类复用已跑的 junit 结果，不重复起进程。"""
    out: list[dict[str, Any]] = []
    for item in checklist.get("definition_of_done", []):
        if item.get("kind") == "pytest_all":
            if not outcomes:
                # UNKNOWN 而非 SKIPPED：过滤运行（--id X）下 DoD 无法求值，
                # 若按 SKIPPED 静默通过，退出码 0 会被读成「全绿」—— 与模块
                # docstring 的「0 = 全绿」契约冲突（2026-09-18 复核）
                state, detail = (
                    UNKNOWN,
                    "本次未运行 pytest，DoD 无法求值（过滤运行请加 --no-dod 显式声明）",
                )
            else:
                failed = [node for node, outcome in outcomes.items() if outcome == "fail"]
                state = RED if failed else GREEN
                detail = f"{len(outcomes)} 个节点，失败 {len(failed)}"
                if failed:
                    detail += "：" + "、".join(failed[:5])
        else:
            state, detail = _eval_cmd(item)
        out.append(
            {
                "id": item.get("id", ""),
                "cmd": _resolve(item["cmd"]),
                "note": item.get("note", ""),
                "state": state,
                "detail": detail,
            }
        )
    return out


def _resolve(text: str) -> str:
    """展开 {py} 占位符，使输出里的命令可直接复制执行。"""
    return text.replace("{py}", sys.executable)


def _resolved(entry: dict[str, Any]) -> dict[str, Any]:
    """拷一份条目并把 action / acceptance 里的 {py} 展开。"""
    out = dict(entry)
    out["action"] = [_resolve(str(step)) for step in entry.get("action", [])]
    out["acceptance"] = [
        {**item, "cmd": _resolve(str(item.get("cmd", "")))} for item in entry.get("acceptance", [])
    ]
    return out


def _selected(entries: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    out = entries
    if args.id:
        wanted = {item.upper() for item in args.id}
        out = [entry for entry in out if entry["id"] in wanted]
    if args.family:
        out = [entry for entry in out if entry["family"] == args.family]
    if args.klass:
        out = [entry for entry in out if entry["class"] == args.klass]
    return out


def _print_human(
    results: list[dict[str, Any]],
    dod: list[dict[str, Any]],
    checklist: dict[str, Any],
) -> None:
    tag = {GREEN: "GREEN", RED: "RED  ", SKIPPED: "SKIP ", EVENT: "EVENT", UNKNOWN: "STALE"}
    print(f"维护清单总览（schema v{checklist['schema_version']}，{len(results)} 条目）")
    print()
    for item in sorted(results, key=lambda x: (CLASS_ORDER.get(x["class"], 9), x["id"])):
        head = f"[{tag.get(item['state'], '?')}] {item['id']}  {item['family']}/{item['class']}"
        print(f"{head}  {item['title']}")
        print(f"         判据：{item['detail']}")
        if item["state"] in (RED, UNKNOWN):
            print(f"         操作：{'；'.join(item.get('action', []))}")
            if item.get("escalate_if"):
                print(f"         停手：{item['escalate_if']}")
            if item.get("doc_ref"):
                print(f"         参考：{item['doc_ref']}")
    states = (GREEN, RED, SKIPPED, EVENT, UNKNOWN)
    counts = {state: sum(1 for r in results if r["state"] == state) for state in states}
    print()
    print("汇总：" + "  ".join(f"{k} {v}" for k, v in counts.items() if v))
    if dod:
        print()
        print("完成定义（DoD）：")
        for item in dod:
            print(f"[{tag.get(item['state'], '?')}] {item['id']}  {item['cmd']}")
            print(f"         判据：{item['detail']}")


def _md_cell(text: str) -> str:
    """Markdown 表格单元格转义：竖线撕裂表格，换行截断行。"""
    return " ".join(text.replace("|", r"\|").split())


def _render_markdown(payload: dict[str, Any]) -> str:
    """把判定渲染成 Markdown 摘要（只列非绿条目）。

    消费方两处：roll 工作流的 Step Summary，以及 roll PR 正文。只保留红/过时
    条目 —— 全绿时一句话带过，避免 PR 正文被十余条 green 淹没。

    维护清单在此是 **advisory**：红条目是刻意设置的 tripwire（新可匹配平台、
    正则漂移、vendor 公共 API 变更），升级路径是「开 PR + 关 automerge 交人工
    评审」。若在工作流里硬失败，tripwire 连送达评审的 PR 都不存在，反而失效。
    """
    # 计数含 DoD：payload["summary"] 只统计 entries，若条目全绿而 DoD 变红，
    # 摘要会印出「green 12」却同时列出红行，自相矛盾（2026-09-18 实跑所见）。
    counts: dict[str, int] = dict.fromkeys((GREEN, RED, SKIPPED, EVENT, UNKNOWN), 0)
    for item in [*payload.get("entries", []), *payload.get("definition_of_done", [])]:
        state = item.get("state")
        counts[state] = counts.get(state, 0) + 1
    counts_text = "  ".join(f"{key} {value}" for key, value in counts.items() if value)
    rows: list[tuple[str, str, str]] = [
        (
            _md_cell(entry.get("id", "")),
            _md_cell(entry.get("title", "")),
            _md_cell(entry.get("detail", "")),
        )
        for entry in payload.get("entries", [])
        if entry.get("state") in (RED, UNKNOWN)
    ]
    rows += [
        (
            _md_cell(dod.get("id", "")),
            _md_cell("DoD：`" + str(dod.get("cmd", "")) + "`"),
            _md_cell(dod.get("detail", "")),
        )
        for dod in payload.get("definition_of_done", [])
        if dod.get("state") in (RED, UNKNOWN)
    ]
    lines = ["### 维护清单判定（advisory）", ""]
    if not rows:
        lines.append(f"全绿（{counts_text}）—— 无需人工介入。")
        return NL.join(lines) + NL
    lines.append(f"存在非绿条目（{counts_text}）：**请人工评审本 PR，不要直接 automerge。**")
    lines.append("")
    lines += ["| 条目 | 说明 | 判定 |", "| --- | --- | --- |"]
    lines += [f"| `{ident}` | {title} | {detail} |" for ident, title, detail in rows]
    return NL.join(lines) + NL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="维护清单检测器：输出全部维护条目的状态（agent 主入口）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--summary-md",
        metavar="PATH",
        help="把判定摘要（Markdown，只含非绿条目）写入 PATH（roll 工作流消费）",
    )
    parser.add_argument("--list", action="store_true", help="只列条目不执行 detector")
    parser.add_argument("--id", action="append", metavar="MC-NN", help="只求值指定条目（可重复）")
    parser.add_argument("--family", choices=FAMILIES, help="按族过滤")
    parser.add_argument(
        "--class",
        dest="klass",
        choices=CLASSES,
        help="按自主度过滤（auto / assisted / escalate）",
    )
    parser.add_argument("--checklist", default=str(CHECKLIST_PATH), help="清单路径")
    parser.add_argument("--no-dod", action="store_true", help="跳过完成定义求值")
    args = parser.parse_args(argv)

    try:
        checklist = load_checklist(Path(args.checklist))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"清单不可用：{exc}", file=sys.stderr)
        return 2

    entries = _selected(checklist["entries"], args)
    if args.list:
        for entry in entries:
            print(f"{entry['id']}  {entry['family']}/{entry['class']}  {entry['title']}")
        return 0

    if not entries:
        print("过滤后无条目", file=sys.stderr)
        return 2

    needs_pytest = any(entry["detector"].get("kind") == "pytest" for entry in entries)
    try:
        outcomes = junit_outcomes() if needs_pytest else {}
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"pytest 求值失败：{exc}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    for entry in entries:
        state, detail = _detect(entry, outcomes)
        results.append({**_resolved(entry), "state": state, "detail": detail})

    dod = [] if args.no_dod else _evaluate_dod(checklist, outcomes)

    payload = {
        "checklist_schema_version": checklist["schema_version"],
        "summary": {
            state: sum(1 for r in results if r["state"] == state)
            for state in (GREEN, RED, SKIPPED, EVENT, UNKNOWN)
        },
        "definition_of_done": dod,
        "prohibited_actions": checklist.get("prohibited_actions", []),
        "loop_guard": checklist.get("loop_guard", {}),
        "entries": results,
    }

    if args.summary_md:
        # 先落摘要再吐 JSON：消费方（roll 工作流）以「JSON 非空」为成功信号，
        # 顺序反过来时摘要文件可能在读取时尚未落盘。
        Path(args.summary_md).write_text(_render_markdown(payload), encoding="utf-8")

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write(NL)
    else:
        _print_human(results, dod, checklist)

    if any(r["state"] == UNKNOWN for r in results) or any(d["state"] == UNKNOWN for d in dod):
        return 2
    if any(r["state"] == RED for r in results) or any(d["state"] == RED for d in dod):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
