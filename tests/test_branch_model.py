"""分支模型的机械钉扎（docs/BRANCHING.md）。

背景（2026-09-19 重构）：仓库从 private 转为 **public** 后，强制力的落点变了。

- **旧**：private + Free，`/branches/main/protection` 与 `/rulesets` 均 403。
  强制力只能**模拟**——PR 守卫工作流判红 + 提权 job 自带门禁 + 本地 pre-push
  钩子。三者都不阻止有写权限的人直接 `git push main`，文档必须承认这一点。
- **新**：public + Free，分支保护完整可用（`enforce_admins: true`）。
  强制力**真的在服务端**。于是：
    ① PR 守卫工作流（`main-pr-target-guard.yml`）被**删除**——服务端会直接
       拒绝 base=main 的 PR，留着它只是重复的 CI 信号，且会让人误以为
       强制力来自那个文件；
    ② 提权 job 的门禁从「唯一防线」降为「第二道」（不可逆动作前独立复算）；
    ③ 新增一类必须守护的东西：**服务端配置**。它可以被人在 UI 上点掉而不留
       任何代码痕迹，所以需要 `protection-audit.yml` 每日巡检 + 本文件断言
       期望值三处同源。

本文件把分支模型当契约守护：
- 强制力期望值在 docs/BRANCHING.md、apply_branch_protection.sh、
  protection-audit.yml 三处**逐字一致**（改一处漏另两处 → PR 永久卡死）；
- ci.yml 的 push 触发跟 dev 而非 main；
- sync-upstream 的 roll PR base、keepalive 目标、staleness 基线都是 dev；
- release.yml 的白名单/结构断言不被本次改动破坏（防误伤）；
- 守卫工作流**不得复活**（本条防止有人「顺手加回来」）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

PROMOTE_PATH = WORKFLOWS / "promote-dev-to-main.yml"
CI_PATH = WORKFLOWS / "ci.yml"
SYNC_PATH = WORKFLOWS / "sync-upstream.yml"
RELEASE_PATH = WORKFLOWS / "release.yml"
CODEQL_PATH = WORKFLOWS / "codeql.yml"
AUDIT_PATH = WORKFLOWS / "protection-audit.yml"
BRANCHING_DOC = REPO_ROOT / "doc" / "BRANCHING.md"
PROTECTION_SCRIPT = REPO_ROOT / "scripts" / "apply_branch_protection.sh"

# 分支保护上的必需检查 context —— 必须与各工作流的 jobs.<id>.name 逐字一致。
# 这是全套配置里**最易错**的一处：写错不报错，只让 PR 永久停在
# "Expected — Waiting for status to be reported"。
REQUIRED_CHECKS = (
    "lint (ruff / actionlint / zizmor)",
    "typecheck (mypy)",
    "test (pytest + vendor verify)",
    "analyze (python)",
)


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


# ------------------------------------------------- 守卫的退役（public 后）
#
# 旧方案里 `main-pr-target-guard.yml` 是「PR 只能打 dev」的**唯一**强制力。
# public 后分支保护在服务端拒绝 base=main 的 PR，守卫变成重复信号，已删除。
# 下面两条断言防止它被「顺手加回来」——加回来不只是冗余，还会让人误判
# 强制力的来源（以为靠这个工作流拦，实际上靠服务端）。


def test_pr_target_guard_workflow_is_gone() -> None:
    """守卫工作流必须不存在（public 后由服务端分支保护接管）。"""
    guard = WORKFLOWS / "main-pr-target-guard.yml"
    assert not guard.exists(), (
        "main-pr-target-guard.yml 又出现了。public 仓库下 base=main 的 PR 由"
        "服务端分支保护直接拒绝，本工作流是重复信号，且会掩盖真正的强制力来源。"
        "若确实要恢复它（例如仓库重新转回 private），必须同时恢复文档里"
        "「直推 main 不会被拦」的边界声明。"
    )


def test_docs_no_longer_claim_the_repo_is_private() -> None:
    """文档不得再把「无分支保护」当作**现状**陈述。

    这类陈述在 public 后是**反向误导**：它会让维护者以为直推 main 拦不住，
    从而放弃使用（实际可用的）服务端强制力。

    注意：文档里**允许**出现「private 下 403」这类对比性说明（解释为什么
    以前做不到）。所以断言的是「现状陈述」而非「字面出现」，判据是：
    不得出现把仓库说成 private 的句子，且必须明确写出 public 的强制力。
    """
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    assert "public" in doc, "doc/BRANCHING.md 必须写明仓库可见性是 public"

    # 旧方案的「边界声明」——它断言的是当时的现实，public 后必须消失
    for stale in (
        "不阻止有写权限的人",
        "远端没有分支保护",
        "强制力**不在 GitHub 侧**",
        "本仓库是 private",
    ):
        assert stale not in doc, f"doc/BRANCHING.md 仍含旧方案的过时现状陈述：{stale}"

    # 现状必须写清：强制力真的在服务端
    assert "enforce_admins" in doc, "必须写明 enforce_admins=true（管理员也不能绕过）"


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
    [PROMOTE_PATH, CI_PATH, SYNC_PATH, RELEASE_PATH, CODEQL_PATH, AUDIT_PATH],
    ids=["promote", "ci", "sync", "release", "codeql", "audit"],
)
def test_workflows_are_valid_yaml_with_jobs(path: Path) -> None:
    """六个工作流都必须是合法 YAML 且含 jobs（防手改引入语法错）。"""
    doc = _load(path)
    assert isinstance(doc.get("jobs"), dict) and doc["jobs"], f"{path.name} 缺 jobs"
    assert "permissions" in doc, f"{path.name} 缺顶层 permissions"


def test_release_whitelist_still_present() -> None:
    """本次改动不得误伤 release.yml 的白名单（回归护栏）。"""
    text = RELEASE_PATH.read_text(encoding="utf-8")
    assert "cp -a metadata.yaml requirements.txt main.py bridge" in text


# ------------------------------------------------- 强制力期望值三处同源
#
# public 方案的强制力在**服务端配置**里，而配置可以被人点掉且不留代码痕迹。
# 唯一可行的防御是：期望值写成代码（可评审、可 diff），且三处引用必须一致：
#   ① docs/BRANCHING.md            —— 人读的说明
#   ② scripts/apply_branch_protection.sh —— 应用
#   ③ .github/workflows/protection-audit.yml —— 巡检
# 任何一处漏改，后果分别是：文档骗人 / 配错 / 巡检误报。


def test_required_checks_match_every_workflow_job_name() -> None:
    """必需检查 context 必须与真实 job name 逐字一致。

    这是全套配置里最易错、且**报错最不友好**的一处：context 写错不会报错，
    只会让 PR 永久停在 "Expected — Waiting for status to be reported"。
    """
    actual: set[str] = set()
    for path in (CI_PATH, CODEQL_PATH):
        doc = _load(path)
        for job in doc["jobs"].values():
            if isinstance(job, dict) and job.get("name"):
                actual.add(job["name"])

    missing = [c for c in REQUIRED_CHECKS if c not in actual]
    assert not missing, (
        f"分支保护期望的必需检查在真实工作流里找不到对应 job name：{missing}；"
        f"现有 job name：{sorted(actual)}。"
        "改名时必须同步 docs/BRANCHING.md、scripts/apply_branch_protection.sh、"
        "protection-audit.yml。"
    )


def test_branching_doc_lists_every_required_check() -> None:
    """BRANCHING.md 必须列出每一条必需检查（人读的那一份也要同步）。"""
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in doc, f"BRANCHING.md 未列出必需检查：{check}"


def test_protection_script_expects_every_required_check() -> None:
    """apply_branch_protection.sh 的 REQUIRED_CHECKS 必须齐全。"""
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in text, f"apply_branch_protection.sh 缺必需检查：{check}"


def test_protection_audit_expects_every_required_check() -> None:
    """protection-audit.yml 的 EXPECTED_CHECKS 必须齐全。

    巡检漏一项 = 那一项被关掉了也不会有人知道。
    """
    text = AUDIT_PATH.read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in text, f"protection-audit.yml 缺必需检查：{check}"


def test_protection_audit_covers_every_enforcement_surface() -> None:
    """巡检必须覆盖每一种「可以被点掉」的强制力。"""
    text = AUDIT_PATH.read_text(encoding="utf-8")
    surfaces = {
        "分支保护可读": "/protection",
        "enforce_admins": "enforce_admins",
        "严格模式": "required_status_checks.strict",
        "禁止强推": "allow_force_pushes",
        "禁止删除": "allow_deletions",
        "线性历史": "required_linear_history",
        "secret scanning": "secret_scanning",
        "Dependabot alerts": "vulnerability-alerts",
        "release 环境 reviewer": "required_reviewers",
        "默认分支工作流齐全": "contents/.github/workflows/",
    }
    missing = [label for label, needle in surfaces.items() if needle not in text]
    assert not missing, f"protection-audit 未覆盖以下强制力面：{missing}"


def test_protection_script_enforces_admins_and_reads_back() -> None:
    """配置脚本必须①禁止管理员绕过 ②回读校验。

    ① 是本设计的核心承诺——若管理员可绕过，「main 只由提权推进」就只是
       对普通贡献者的约束。
    ② 因为「写完 ≠ 生效」：API 返回 200 但字段被服务端规范化是真实发生过的。
    """
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    assert '"enforce_admins": true' in text, "apply_branch_protection.sh 必须禁止管理员绕过"
    assert "--method PUT" in text, "应当用 PUT 全量替换以获得幂等性"
    assert "回读校验" in text, "写完必须回读比对，否则「写成功」被当成「生效」"


def test_protection_audit_reconcile_precedes_detection() -> None:
    """告警对账必须排在检测之前，否则本轮新开的告警会被同轮关掉。

    （与 sync-upstream 的告警生命周期同一纪律。）
    """
    doc = _load(AUDIT_PATH)
    steps = doc["jobs"]["audit"]["steps"]
    names = [s.get("name", "") for s in steps]
    reconcile = next(i for i, n in enumerate(names) if "对账" in n)
    detect = next(i for i, n in enumerate(names) if "检测" in n)
    assert reconcile < detect, f"对账步骤必须排在检测之前：{names}"


def test_codeql_excludes_vendor_snapshot() -> None:
    """CodeQL 必须排除 vendor/ 与 templates/。

    它们是上游快照（零修改铁律：只能整树重建，不许就地改）。对这些目录报出的
    问题我们**改不了**，留在结果里只会淹没真问题。
    """
    text = CODEQL_PATH.read_text(encoding="utf-8")
    assert "paths-ignore" in text, "codeql.yml 缺 paths-ignore"
    for excluded in ("vendor", "templates"):
        assert excluded in text, f"codeql.yml 未排除 {excluded}/"
    # CodeQL 的三项权限，缺 actions: read 会以与代码无关的报错收场
    doc = _load(CODEQL_PATH)
    perms = doc["jobs"]["analyze"]["permissions"]
    assert perms.get("security-events") == "write", "CodeQL 需 security-events: write"
    assert perms.get("actions") == "read", "CodeQL 需 actions: read（最易漏的一项）"


def test_release_job_timeout_covers_human_approval() -> None:
    """release job 的超时必须覆盖人工审批等待。

    `timeout-minutes` **包含**停在 `environment` 等批准的时长。旧值 15 意味着
    reviewer 必须在一刻钟内点批准，否则 job 直接失败——而那时发布物已构建、
    已签名，失败后需重跑整个 trust chain。
    """
    doc = _load(RELEASE_PATH)
    job = doc["jobs"]["release"]
    assert job.get("environment") == "release", "release job 必须声明受保护环境"
    timeout = job.get("timeout-minutes", 0)
    assert timeout >= 30, (
        f"release job 的 timeout-minutes={timeout} 太短——它包含人工审批等待时间，"
        "至少应留 30 分钟给审阅者"
    )


def test_default_branch_choice_is_documented() -> None:
    """默认分支选 dev 的决策必须写进文档。

    这张表（哪个事件读哪个 ref 的定义）是最容易踩坑、也最容易忘的地方：
    默认分支设成 dev 之后，「改了定时工作流但 cron 仍跑旧版本」这个坑才消失。
    """
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    assert "默认分支" in doc, "BRANCHING.md 必须说明默认分支的选择"
    assert "schedule" in doc, "必须说明 cron 读默认分支的定义"
    assert "dev" in doc


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
    """分支×工作流矩阵是运维参考，别被删掉。

    矩阵记录了「默认分支上缺哪些工作流」这一非直观事实。public 重构后
    `main-pr-target-guard.yml` 已删除，矩阵与工作流清单必须同步更新——
    否则文档会描述一个不存在的文件。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "12.3 各分支上有哪些工作流" in text, "缺 §12.3 分支×工作流矩阵"
    assert "promote-dev-to-main.yml" in text, "矩阵未提及 promote-dev-to-main.yml"
    # 守卫已退役：文档里不应再把它列为在役工作流
    assert "main-pr-target-guard.yml" not in text, (
        "CONTRIBUTING.md 仍提及已删除的 main-pr-target-guard.yml —— 文档描述了不存在的文件"
    )
