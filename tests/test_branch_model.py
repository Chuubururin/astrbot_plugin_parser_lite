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


# ---------------------------------------------------------------- 告警生命周期
#
# 背景：sync-upstream 会开两类告警 issue。原实现里
#   - `upstream lagging`：关闭步骤只在 `steps.roll_pr.outcome == 'success'`
#     时运行，失败路径永远到不了 → issue 永久滞留；且创建无去重 → 重复堆积
#   - `sync-upstream roll failed`：**完全没有关闭路径** → 永久 OPEN
# 实测仓库里 3 条 issue 全部滞留（两条 lagging 互为重复）。
# 下列断言把「自动关闭」钉死，并防止退回「挂某个步骤 outcome」的写法。


def _sync_steps() -> list[dict]:
    return _load(SYNC_PATH)["jobs"]["roll"]["steps"]


def _find_step(keyword: str) -> dict:
    for s in _sync_steps():
        if keyword in str(s.get("name", "")):
            return s
    raise AssertionError(f"sync-upstream 里找不到步骤：{keyword}")


def _install_lines(step: dict) -> str:
    """取步骤 run 块里**可执行**的行，剥掉注释。

    为什么必须剥注释：本仓库的步骤注释里大量提到包名（「漏装 pytest 会让…」
    「types-qrcode：qrcode 无 py.typed」）。若直接对整段 run 做
    `"pytest" in body`，**注释本身就能满足断言** —— 把安装行删掉测试照绿。
    反向验证抓到过这个漏洞（去掉 mypy / types-qrcode 两个用例均 MISSED）。

    注：自 2026-09-19 起，安装清单的唯一实现搬到了复合 action，对它的断言在
    tests/test_supply_chain.py（那里的 `_code_lines` 是同一逻辑）。本函数保留
    给仍直接读 workflow 内联脚本的用例使用。
    """
    body = str(step.get("run", ""))
    return "\n".join(
        ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")
    )


# 依赖安装的唯一实现（复合 action）。所有 job 都必须委派给它，而不是各抄一份
# 安装清单 —— 2026-09-19 的「提权通道被假红锁死」事故就源于清单漂移。
#
# 为什么是 `./` 而不是 zizmor 推荐的 `$/`（self-repository 语法）：actionlint
# 最新版 v1.7.12（2026-03-30）尚不认识 `$/`，会判 "ref is missing"；而 actionlint
# 是本地与 CI 双端硬门禁。故工作流里保留 `./` 并**显式行内豁免**该建议，理由见
# ci.yml 的说明；复评触发条件是 actionlint 支持 `$/`。
SETUP_ENV_USES = "./.github/actions/setup-env"


def _setup_env_call(steps: list[dict]) -> dict | None:
    """在工作流步骤里找对复合 action 的调用（`uses: ./.github/actions/setup-env`）。"""
    for s in steps:
        if str(s.get("uses", "")) == SETUP_ENV_USES:
            return s
    return None


def test_alert_reconcile_step_exists_and_runs_always() -> None:
    """必须有一个「对账」步骤，且它与**成功/失败无关**地运行。

    这是本组断言的核心：关闭动作若挂在某个步骤的 outcome 上，失败路径就到不了，
    issue 必然滞留。判定依据必须是「整条流水线的健康结论」，而非某一个步骤。
    """
    step = _find_step("告警对账")
    cond = str(step.get("if", ""))
    assert "always()" in cond, (
        f"对账步骤必须用 always() 运行（否则又回到「只在某分支关闭」）：{cond}"
    )


def test_alert_reconcile_closes_both_alert_types() -> None:
    """对账必须同时覆盖两类告警标题。"""
    body = str(_find_step("告警对账").get("run", ""))
    assert "sync-upstream roll failed" in body, "对账未覆盖失败告警"
    assert "upstream lagging" in body, "对账未覆盖滞后告警"
    assert "gh issue close" in body, "对账里没有关闭动作"


def test_alert_reconcile_treats_cancelled_as_unhealthy() -> None:
    """job 被取消时**不得**判为健康。

    取消 = 没有验证过任何东西。若此判健康，就会误关告警、把真实问题掩盖掉。
    该分支由真值表验证时发现（11 例里唯一失败的一例）。

    断言必须**锚定 JOB_STATUS 那一行**：早先只写 `'"cancelled"' in body` 是无效的
    ——`cancelled` 在更上面的步骤 outcome 循环里也出现，于是把 JOB_STATUS 的
    cancelled 判定删掉后断言照绿（反向验证实测）。必须精确到行。
    """
    body = str(_find_step("告警对账").get("run", ""))
    # 找到 job.status 兜底那一行的 if 判断
    lines = [
        ln.strip()
        for ln in body.splitlines()
        if "JOB_STATUS" in ln and ln.strip().startswith("if ")
    ]
    assert lines, "找不到 job.status 兜底判定行"
    job_if = lines[0]
    assert '"failure"' in job_if, f"job.status 兜底未认 failure：{job_if}"
    assert '"cancelled"' in job_if, (
        f"job.status 兜底未认 cancelled——取消时会被误判为健康并关闭告警：{job_if}"
    )


def test_alert_reconcile_has_no_set_e_landmine() -> None:
    """不得使用 `cond && assign` 形态的条件赋值。

    在 `set -e` 下，条件为假时复合命令返回 1 → 脚本被静默中止，
    `healthy` 永远写不进 GITHUB_OUTPUT → 后续步骤恒判「不健康」
    → 自动关闭被永久锁死。必须用显式 if 块。
    """
    body = str(_find_step("告警对账").get("run", ""))
    # 排除注释行后，检查是否存在 `[ ... ] && 变量=值` 这种形态
    code_lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    offenders = [
        ln.strip()
        for ln in code_lines
        if re.search(r"^\s*\[.*\]\s*&&\s*\w+=", ln) or re.search(r";\s*\[.*\]\s*&&\s*\w+=", ln)
    ]
    assert not offenders, f"存在 set -e 陷阱式的条件赋值（会锁死自动关闭）：{offenders}"


def test_alert_reconcile_declares_healthy_output_in_all_paths() -> None:
    """两条分支都必须写出 healthy，否则下游 `!= 'true'` 判断会退化。"""
    body = str(_find_step("告警对账").get("run", ""))
    assert "healthy=false" in body and "healthy=true" in body, (
        "对账步骤必须在健康与不健康两条分支上都写 healthy 输出"
    )
    # 且这两次写入都要落到 GITHUB_OUTPUT
    assert body.count('>> "$GITHUB_OUTPUT"') >= 2, "healthy 的两条分支都应写入 $GITHUB_OUTPUT"


def test_lagging_alert_has_dedup_guard() -> None:
    """滞后告警的创建必须有去重（原实现缺此步 → 实测产生重复 issue）。"""
    body = str(_find_step("滞后告警").get("run", ""))
    assert "in:title state:open" in body, "滞后告警缺去重查询"
    assert "跳过重复创建" in body, "滞后告警缺去重短路"


def test_alerts_are_gated_on_reconcile_health_not_step_outcome() -> None:
    """创建告警的条件必须依赖对账结论，而不是某一步的 outcome。

    这样「健康就不开、不健康才开」与「健康就关」共用同一个判据，
    不会出现两个判据漂移（一边认为健康关了告警，另一边又开一条）。
    """
    for keyword in ("滞后告警", "失败告警"):
        cond = str(_find_step(keyword).get("if", ""))
        assert "steps.reconcile.outputs.healthy" in cond, f"{keyword} 的条件未依赖对账结论：{cond}"


def test_alert_creation_is_ordered_after_reconcile() -> None:
    """对账步骤必须排在两个创建步骤**之前**。

    先关后开：若创建在前，同一轮里刚创建的告警会被紧随其后的对账关掉
    （当本轮健康时），反而丢失信号。
    """
    names = [str(s.get("name", "")) for s in _sync_steps()]
    idx_reconcile = next(i for i, n in enumerate(names) if "告警对账" in n)
    for keyword in ("滞后告警", "失败告警"):
        idx = next(i for i, n in enumerate(names) if keyword in n)
        assert idx_reconcile < idx, (
            f"「{keyword}」排在对账之前（第 {idx} 步 vs 第 {idx_reconcile} 步）——先开后会自关"
        )


def test_alert_reconcile_outcome_refs_are_real_step_ids() -> None:
    """对账里引用的 `steps.<id>.outcome` 必须是**真实存在**的步骤 id。

    踩过的坑：早先写了 `O_ROLL: ${{ steps.roll.outcome }}`，而工作流里根本没有
    `id: roll`（真正干活的步骤 id 是 `roll_pr`）。PyYAML 读得进来、pytest 全绿，
    只有 actionlint 的表达式检查会红——**本地测试没覆盖的盲区靠 CI 兜**。
    所以这里主动做一遍静态校验。
    """
    steps = _sync_steps()
    defined = {str(s["id"]) for s in steps if "id" in s}
    assert defined, "sync-upstream 的步骤没有任何 id，解析可能出错"

    body = str(_find_step("告警对账").get("run", ""))
    env = _find_step("告警对账").get("env", {}) or {}

    # env 值里的 ${{ steps.X.outcome }} / ${{ steps.X.outputs.Y }}
    for key, val in env.items():
        for ref in re.findall(r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.", str(val)):
            assert ref in defined, (
                f"对账 env {key} 引用了不存在的步骤 id `{ref}`"
                f"（已定义：{sorted(defined)}）——actionlint 会判红"
            )

    # run 脚本里若出现 ${{ steps.X.outcome }} 同样校验
    for ref in re.findall(r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.", body):
        assert ref in defined, f"对账 run 引用了不存在的步骤 id `{ref}`"


def test_alert_reconcile_env_lists_every_gating_step() -> None:
    """`detect` 与 `roll_pr` 是本工作流仅有的两个「失败即 roll 失败」的步骤。

    对账必须把它们的 outcome 都抓进 env，否则该步骤失败时对账看不到，
    会把不健康的 run 判成健康 → 误关告警。
    """
    env = _find_step("告警对账").get("env", {}) or {}
    vals = " ".join(str(v) for v in env.values())
    assert "steps.detect.outcome" in vals, "对账未采集 detect 的 outcome"
    assert "steps.roll_pr.outcome" in vals, "对账未采集 roll_pr 的 outcome"


def test_docs_warn_that_dispatch_and_schedule_use_default_branch() -> None:
    """把「dispatch/schedule 取默认分支定义」这个坑写进 CONTRIBUTING.md。

    实测：`main` 是默认分支时，`schedule` 与不带 ref 的 `workflow_dispatch`
    都拿 `main` 上的 workflow 定义（run 的 headSha 等于 main 的 sha），
    因此对 `dev` 上刚改完的 `sync-upstream` 做验证时，**必须** `--ref dev`，
    否则你以为在验证新逻辑，实际跑的是旧版本。

    这条断言防的是「文档被删掉 → 下次有人又踩」。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "--ref dev" in text, "CONTRIBUTING.md 缺「手动触发须带 --ref dev」的说明"
    assert "默认分支" in text, "缺「dispatch/schedule 取默认分支定义」的解释"
    # 速查块里的命令也必须是带 ref 的形态，不能留着裸调用
    assert "gh workflow run sync-upstream --ref dev" in text, (
        "速查块里的 sync-upstream 手动触发命令未带 --ref dev"
    )


def test_sync_upstream_delegates_dependency_setup_to_shared_action() -> None:
    """`sync-upstream` 跑全量 pytest，依赖必须齐备 —— 但安装定义只能有一处。

    实测事故：安装块只装了 requirements + host-provided，没有 pytest，于是
    「dry-run 全量契约测试」以 `No module named pytest` 直接红，连续 3 次触发
    MC-11 熔断，把整个同步流水线停摆。

    这类**假红**比真红更糟：它把「环境缺失」伪装成「契约破了」，掩盖真问题。
    而事故的根因不是「漏了一个包」，是**同一份安装清单在 4 个工作流里各抄了
    一遍**。所以这里的断言从「逐包检查」改成「必须委派给唯一实现」：包清单本身
    的正确性由 tests/test_supply_chain.py 对复合 action 本体负责。
    """
    steps = _sync_steps()
    call = _setup_env_call(steps)
    assert call is not None, "sync-upstream 未使用复合 action .github/actions/setup-env"
    # 默认档 = astrbot 与 test-reqs 都装（dry-run 要跑全量 pytest）
    for key, why in (
        ("astrbot", "否则桥接层行为契约会整模块静默 skip"),
        ("test-reqs", "否则 dry-run 契约测试以 No module named pytest 假红"),
    ):
        value = str(call.get("with", {}).get(key, "true"))
        assert value != "false", f"sync-upstream 的 {key} 不能关：{why}"
    # 不得再出现手抄的安装清单（否则漂移的入口又回来了）
    for s in steps:
        assert "pip install -r requirements.txt" not in str(s.get("run", "")), (
            "sync-upstream 又手抄了安装清单——应统一走复合 action"
        )


def test_sync_upstream_still_runs_full_pytest_dry_run() -> None:
    """dry-run 必须是**全量** pytest，不能为了躲依赖问题退化成子集。"""
    steps = _sync_steps()
    dry = next((s for s in steps if "dry-run" in str(s.get("name", ""))), None)
    assert dry is not None, "缺 dry-run 契约测试步骤"
    body = str(dry.get("run", ""))
    assert "pytest" in body, "dry-run 未跑 pytest"
    assert "-c config/pyproject.toml" in body, "dry-run 未用项目 pytest 配置"
    # 不得出现 -k / -m 之类的**筛选**（那会让 dry-run 悄悄只跑一部分）。
    # 注意不能直接 substring 查 " -m "：`python -m pytest` 里的 `-m` 是
    # 「跑模块」而非「标记筛选」，裸匹配会误伤（自己踩过）。
    # 做法：剥掉 `python -m ` 前缀后再看 pytest 的参数串。
    args = re.sub(r"^\s*python\s+-m\s+", "", body.strip())
    args = re.sub(r"^pytest\s+", "", args)
    for flag in ("-k", "-m", "--deselect", "--ignore"):
        assert not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", args), (
            f"dry-run 被窄化（出现 {flag}）：{body}"
        )


def test_promote_declares_gate_extras() -> None:
    """promote 直接调 `python -m ruff` / mypy，就必须声明 extras=gate。

    实测事故：安装块装了 pytest 却没装 ruff，于是提权门禁的第一步
    `lint（ruff check）` 以 `No module named ruff` 假红 —— 门禁过不了，
    **整条提权通道被锁死**（main 永远推不动）。dry-run run 35383666398 复现。

    注意 promote 与 ci.yml 的 lint job 取 ruff 的方式不同：ci.yml 走 pre-commit
    （ruff 在 pre-commit 的隔离环境里），promote 是裸 `python -m ruff`，所以
    必须显式声明 gate 档。版本对齐改由复合 action 从 pre-commit 的 rev 推导，
    不再手抄 —— 见 tests/test_supply_chain.py。
    """
    doc = _load(PROMOTE_PATH)
    steps = doc["jobs"]["promote"]["steps"]
    call = _setup_env_call(steps)
    assert call is not None, "promote 未使用复合 action .github/actions/setup-env"
    assert call.get("with", {}).get("extras") == "gate", (
        "promote 必须声明 extras=gate，否则 ruff/mypy 不装，提权门禁假红并锁死通道"
    )


def test_promote_has_no_hand_rolled_install_list() -> None:
    """promote 不得再手抄安装清单 —— 那正是「门禁假红」的入口。

    这条与上一条是一对：上一条要求「委派给复合 action」，这条禁止「同时保留
    旧的手抄清单」。两者都满足时，安装定义才真的只有一处。
    """
    doc = _load(PROMOTE_PATH)
    steps = doc["jobs"]["promote"]["steps"]
    for s in steps:
        body = str(s.get("run", ""))
        assert "pip install -r requirements.txt" not in body, (
            "promote 手抄了安装清单——应统一走复合 action，否则清单会再次漂移"
        )


def test_docs_document_ref_dev_for_both_manual_workflows() -> None:
    """速查块里两个手动触发命令都必须带 `--ref dev`。

    `promote-dev-to-main` 不带 ref 会直接 HTTP 422（main 上没有该文件），
    实测确认；`sync-upstream` 不带 ref 会静默用 main 上的旧定义。
    两种失败形态不同，但都源于「默认分支是 main」。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "gh workflow run sync-upstream --ref dev" in text, (
        "速查块的 sync-upstream 手动触发未带 --ref dev"
    )
    assert "gh workflow run promote-dev-to-main --ref dev" in text, (
        "速查块的 promote 手动触发未带 --ref dev——会 422"
    )
    # 速查块（§12 的第一个 bash 代码块）里不得留下裸调用形态。
    # 注意不能全文搜：§12.2 的「所以：」块里**故意**展示了会 422 的错误写法
    # 作为反例，全文搜会把那个反例当成违规（自己踩过）。
    section = text.split("## 12. 运维速查", 1)[1]
    block = section.split("```bash", 1)[1].split("```", 1)[0]
    assert not re.search(r"gh workflow run promote-dev-to-main\s+-f", block), (
        "速查块里仍有无 --ref 的 promote 调用（会 422）"
    )


def test_docs_contain_per_branch_workflow_matrix() -> None:
    """§12.3 的分支×工作流矩阵是运维参考，别被删掉。

    矩阵记录了「main 上缺 main-pr-target-guard / promote-dev-to-main」这一
    非直观事实——它是「尚未首次 promote」的必然结果，也是守卫可被绕过的原因。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "12.3 各分支上有哪些工作流" in text, "缺 §12.3 分支×工作流矩阵"
    for name in ("main-pr-target-guard.yml", "promote-dev-to-main.yml"):
        assert name in text, f"矩阵未提及 {name}"
    assert "首次 promote" in text, "未说明「main 缺文件是尚未首次 promote 的必然结果」"
