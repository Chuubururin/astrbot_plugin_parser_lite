"""分支模型的机械钉扎（CONTRIBUTING.md 第 8 节）。

背景：本仓库的强制力**不在 GitHub 侧**——仓库 private 且未开通 GitHub Pro，
`/branches/main/protection` 与 `/rulesets` 均返回 403。所以「PR 只能打 dev」
「main 只由 promote 推进」这些约定，若不写成断言，就只是文档里的一句话。

本文件把分支模型当契约守护：
- 两个新工作流存在且触发条件正确；
- ci.yml 的 push 触发跟 dev 而非 main；
- sync-upstream 的 roll PR base、keepalive 目标、staleness 基线都是 dev；
- release.yml 的白名单/结构断言不被本次改动破坏（防误伤）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

GUARD_PATH = WORKFLOWS / "main-pr-target-guard.yml"
PROMOTE_PATH = WORKFLOWS / "promote-dev-to-main.yml"
CI_PATH = WORKFLOWS / "ci.yml"
SYNC_PATH = WORKFLOWS / "sync-upstream.yml"
RELEASE_PATH = WORKFLOWS / "release.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflow_triggers(doc: dict) -> dict:
    """取 on: 段。

    YAML 1.1 把裸 ``on`` 解析成布尔 True，PyYAML 亦然——两种键名都要认，
    否则测试会在「找不到 on」上失败，而真正的问题是解析细节。
    """
    for key in ("on", True, "true"):
        if key in doc:
            return doc[key]
    raise AssertionError("workflow 缺 on: 段")


# ---------------------------------------------------------------- 守卫工作流


def test_main_pr_target_guard_exists_and_targets_main() -> None:
    """守卫必须只对 base=main 的 PR 触发。

    若它改成对所有 PR 触发，合法打到 dev 的 PR 会被一起判红——门禁从
    「拦错方向」变成「拦一切」，分支模型直接不可用。
    """
    assert GUARD_PATH.is_file(), "缺 main-pr-target-guard.yml"
    triggers = _workflow_triggers(_load(GUARD_PATH))
    assert list(triggers) == ["pull_request"], f"守卫只应由 pull_request 触发：{list(triggers)}"
    assert triggers["pull_request"]["branches"] == ["main"], (
        "守卫必须只匹配 base=main；否则会误伤打到 dev 的合法 PR"
    )


def test_main_pr_target_guard_actually_fails() -> None:
    """守卫的 job 必须真的失败（exit 1），而不是只打印警告。

    `echo ::error::` 加 exit 0 会让 check 变绿——那是个不拦任何东西的装饰。
    """
    text = GUARD_PATH.read_text(encoding="utf-8")
    assert re.search(r"^\s+exit 1\s*$", text, re.MULTILINE), (
        "守卫的 run 块必须以 exit 1 收场，否则 required check 会是绿的"
    )
    assert "::error::" in text, "守卫应给出 ::error:: 注记（PR 页面上可见）"


def test_main_pr_target_guard_has_no_write_permissions() -> None:
    """守卫是纯判定，不得有写权限（Scorecard 最小权限）。"""
    doc = _load(GUARD_PATH)
    assert doc["permissions"] == {"contents": "read"}, doc.get("permissions")
    for job in doc["jobs"].values():
        assert "permissions" not in job, "守卫的 job 不应额外声明权限"


# ---------------------------------------------------------------- 提权工作流


def test_promote_workflow_triggers_are_explicit() -> None:
    """提权只能由「手动 dispatch」或「dev 上带前缀的提交」触发。

    关键反面：**不得**对 push:main 触发（自触发循环），也不得对任意 push 触发
    （那样每个 feature 合入都会试图改 main，发布节奏就失控了）。
    """
    triggers = _workflow_triggers(_load(PROMOTE_PATH))
    assert "workflow_dispatch" in triggers, "提权必须支持手动触发"
    assert "push" in triggers, "提权应支持 dev push + 前缀触发"
    assert triggers["push"]["branches"] == ["dev"], (
        f"提权只应跟 dev：{triggers['push'].get('branches')}"
    )
    assert "main" not in triggers["push"].get("branches", []), (
        "提权不得由 push:main 触发——会形成自触发循环"
    )


def test_promote_requires_prefix_or_manual() -> None:
    """push 路径必须校验 [promote] 前缀；缺了它 dev 的每次推进都会改 main。"""
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "[promote]" in text, "提权缺 [promote] 前缀判定"
    assert "workflow_dispatch" in text, "提权缺手动放行分支"


def test_promote_runs_full_gates_before_pushing_main() -> None:
    """**本文件最重要的一条**：提权必须在推 main 之前自带全套门禁。

    为什么不能省：本仓库 remote 无分支保护（private 且无 GitHub Pro），而
    GITHUB_TOKEN 推的提交**不触发**后续工作流——main 上不会出现 CI 运行。
    因此 promote job 内的门禁是 main 的**唯一**防线。若有人删掉这些步骤，
    main 就会变成「未经测试的代码也能进」的通道，而表面上一切照常。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    required = [
        "python -m pytest -c config/pyproject.toml --rootdir=. -q",
        "python scripts/typecheck.py",
        "python scripts/verify_vendor.py",
        "python scripts/check_test_deps.py",
        "ruff check --config config/pyproject.toml .",
        "ruff format --config config/pyproject.toml --check .",
    ]
    missing = [cmd for cmd in required if cmd not in text]
    assert not missing, f"提权 job 缺门禁命令（main 将失去唯一防线）：{missing}"

    # 门禁必须排在**真正的**推送指令之前。注意不能用 ``text.rindex("git push")``：
    # 步骤末尾的 Step Summary 里有一句 ``printf '发布 tag：git tag ... && git push ...'``
    # ——那是提示文案不是命令，会把「最后一个 push」钉在全文末尾，于是任何计
    # 排序的判据都恒红。故这里只认**行首**的 push 命令（缩进后紧跟 git push）。
    push_lines = [m.start() for m in re.finditer(r"(?m)^\s*git push\s", text)]
    assert push_lines, "提权工作流里找不到真正的 push 命令"
    first_push = min(push_lines)
    late = [cmd for cmd in required if text.rindex(cmd) > first_push]
    assert not late, f"门禁排在推送之后——形同虚设：{late}"


def test_promote_fails_loudly_when_main_has_unique_commits() -> None:
    """main 有 dev 之外的历史时必须响亮失败，不得静默 merge。

    静默 merge 会把一个 dev 从未验证过的合并提交送进发布线——正是本分支模型
    要杜绝的事。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "merge-base --is-ancestor" in text, "缺「main 是 dev 祖先」的快进校验"
    assert "::error::" in text, "main 不可快进时应给 ::error::"
    # 反向：不得对 main 做**裸** push（绕过 lease 会抹掉他人提交）。
    # 允许的唯一形态是 --force-with-lease 钉住校验过的 sha。
    bare = re.findall(r"git push(?!\s+--force-with-lease)[^\n]*\bmain\b", text)
    assert not bare, f"不得对 main 做裸 push：{bare}"


def test_promote_declares_write_permission_at_job_level() -> None:
    """写权限必须在 job 级显式声明并附注释（zizmor undocumented-permissions）。"""
    doc = _load(PROMOTE_PATH)
    assert doc["permissions"] == {"contents": "read"}, "顶层应保持只读"
    jobs = doc["jobs"]
    assert jobs["promote"]["permissions"]["contents"] == "write"


def test_promote_has_non_cancelling_concurrency() -> None:
    """提权不得被新一轮触发取消——半个 main 比没 main 糟。"""
    doc = _load(PROMOTE_PATH)
    conc = doc["concurrency"]
    assert conc["group"] == "promote-dev-to-main"
    assert conc["cancel-in-progress"] is False, (
        "提权不取消在途：中断可能留下 main 已推但 tag 未打的半成品状态"
    )


# ---------------------------------------------------------------- ci.yml


def test_ci_push_trigger_follows_dev_not_main() -> None:
    """CI 的 push 触发跟 dev。

    main 由 promote 推进，而 GITHUB_TOKEN 推的提交不触发工作流——把 main 留在
    触发列表里只会让人误以为「main 上有 CI」，实际永远不会跑。
    """
    triggers = _workflow_triggers(_load(CI_PATH))
    assert triggers["push"]["branches"] == ["dev"], (
        f"ci.yml 的 push 触发应为 dev：{triggers['push'].get('branches')}"
    )
    # pull_request 不得过滤 base 分支：任何 base 都要跑
    pr = triggers["pull_request"]
    assert pr is None or "branches" not in pr, (
        "CI 的 pull_request 不应按 base 过滤——误打 main 的 PR 也要出信号"
    )


def test_ci_keeps_three_required_check_job_names() -> None:
    """三个 required check 的 job name 是外部契约（CONTRIBUTING.md 第 8.3 节）。"""
    doc = _load(CI_PATH)
    names = {job["name"] for job in doc["jobs"].values()}
    assert names == {
        "lint (ruff / actionlint / zizmor)",
        "typecheck (mypy)",
        "test (pytest + vendor verify)",
    }, f"required check 名字变了会影响外部约定：{sorted(names)}"


# ---------------------------------------------------------------- sync-upstream


def test_sync_roll_pr_targets_dev() -> None:
    """roll PR 的 base 必须是 dev——roll 改工作分支，不改发布指针。"""
    text = SYNC_PATH.read_text(encoding="utf-8")
    assert "gh pr create --base dev" in text, "roll PR 的 base 应为 dev"
    assert "gh pr create --base main" not in text, (
        "roll PR 不得以 main 为 base：那会绕过 promote 直接改发布线"
    )
    assert "gh pr merge --auto" in text and "--base dev" in text


def test_sync_checks_out_dev() -> None:
    """roll 必须检出 dev：叠在 main 上会与 dev 的在途改动打架。"""
    doc = _load(SYNC_PATH)
    steps = doc["jobs"]["roll"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "缺 checkout 步骤"
    assert checkouts[0]["with"].get("ref") == "dev", "roll 的 checkout 应显式 ref: dev"


def test_sync_never_pushes_main() -> None:
    """sync-upstream 内不得出现任何以 main 为目标的 push。

    keepalive 曾直推 main；分支模型下它必须推 dev。LKG 推的是 lkg 分支（允许）。
    """
    text = SYNC_PATH.read_text(encoding="utf-8")
    assert "HEAD:main" not in text, "sync 内仍有直推 main 的路径（keepalive？）"
    assert "HEAD:dev" in text, "keepalive 应推 dev"
    assert "refs/heads/lkg" in text, "LKG 留档应推 lkg 分支"


def test_sync_staleness_baseline_reads_dev() -> None:
    """staleness 基线必须读 origin/dev。

    state 由 roll 提交进 dev，main 只在 promote 时才拿到它。读 main 会把
    「已同步但尚未 promote」误报成滞后——一个永不消失的假告警。
    """
    text = SYNC_PATH.read_text(encoding="utf-8")
    assert 'git show "origin/dev:${STATE_FILE}"' in text, "staleness 基线应读 origin/dev"
    assert 'git show "origin/main:${STATE_FILE}"' not in text, (
        "staleness 仍读 origin/main——会产生永不消失的假告警"
    )


# ---------------------------------------------------------------- 反向保护


@pytest.mark.parametrize(
    "path",
    [GUARD_PATH, PROMOTE_PATH, CI_PATH, SYNC_PATH, RELEASE_PATH],
    ids=["guard", "promote", "ci", "sync", "release"],
)
def test_workflows_are_valid_yaml_with_jobs(path: Path) -> None:
    """五个工作流都必须是合法 YAML 且含 jobs（防手改引入语法错）。"""
    doc = _load(path)
    assert isinstance(doc.get("jobs"), dict) and doc["jobs"], f"{path.name} 缺 jobs"
    assert "permissions" in doc, f"{path.name} 缺顶层 permissions"


def test_release_whitelist_still_present() -> None:
    """本次改动不得误伤 release.yml 的白名单（回归护栏）。"""
    text = RELEASE_PATH.read_text(encoding="utf-8")
    assert "cp -a metadata.yaml requirements.txt main.py bridge" in text
