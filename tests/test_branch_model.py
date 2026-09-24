"""分支模型的机械钉扎（doc/BRANCHING.md）。

背景（2026-09-19 第二次重构）：用户要求**更简单**的 CICD，只保留两个分支，
并指定参考 SnowLuma/SnowLuma 的 actions 配置。模型因此从「PR 合并」改为
**「指针移动」**：

- **旧（本次被替换）**：public + 分支保护全开，`main` 开 PR 保护 +
  required checks（4 项，含 CodeQL）。dev → main 走 promote 工作流做**快进推送**。
- **新（本文件守护）**：只留两个分支。`main` 是**发布指针**，由
  `promote-dev-to-main.yml` 用 `git push --force-with-lease` **移动**过去，
  不产生 merge commit。CodeQL 与每日巡检移除；上游自动同步以「sync 走
  PR 轨道」的形态在役（roll 序列单实现 = scripts/roll_local.py）。

**本模型最关键、也最容易被人「好心改坏」的一条**：
`main` **不能**开启分支保护的 "Require a pull request before merging"
（API 字段 `required_pull_request_reviews`）。原因：该选项拒绝**所有**直接推送，
包括 promote 工作流自己的推送 —— 一旦开启 Promote 直接失效。
这与通用最佳实践相反，是本模型的有意取舍，所以必须有断言把它钉住。

本文件守护的契约：
- 只存在 4 个工作流，且它们的分工与名字稳定；
- `main` 的保护里**没有** PR 保护、有 `enforce_admins`、允许 force push；
- `dev` 的保护里有 required checks；
- 必需检查名在三处（workflow / 脚本 / 文档）逐字一致；
- promote 用 lease 推送、在 main 有独有提交时响亮失败；
- 被移除的工作流（codeql / protection-audit）**不得复活**；
- 保留前缀 `[merge]` / `chore(release):` 的使用纪律被写进文档。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

CI_PATH = WORKFLOWS / "ci.yml"
GUARD_PATH = WORKFLOWS / "main-pr-target-guard.yml"
PROMOTE_PATH = WORKFLOWS / "promote-dev-to-main.yml"
RELEASE_PATH = WORKFLOWS / "release.yml"

BRANCHING_DOC = REPO_ROOT / "doc" / "BRANCHING.md"
PROTECTION_SCRIPT = REPO_ROOT / "scripts" / "apply_branch_protection.sh"

# 在役工作流的**完整集合**。多一个少一个都要在这里显式决策。
EXPECTED_WORKFLOWS = {
    "ci.yml",
    "main-pr-target-guard.yml",
    "promote-dev-to-main.yml",
    "release.yml",
    "sync-upstream.yml",
}

# 分支保护上的必需检查 context —— 必须与各工作流的 jobs.<id>.name 逐字一致。
# 这是全套配置里**最易错**的一处：写错不报错，只让 PR 永久停在
# "Expected — Waiting for status to be reported"。
REQUIRED_CHECKS = (
    "lint (ruff / actionlint / zizmor)",
    "typecheck (mypy)",
    "test (pytest + vendor verify)",
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


# ============================================================ 工作流集合
#
# 本版的目标是「简单」。工作流数量本身就是复杂度，所以把集合当契约钉住：
# 任何新增/删除都必须改动这个集合，从而在 code review 里被看见。


def test_workflow_inventory_is_exactly_the_expected_set() -> None:
    """工作流集合就是 EXPECTED_WORKFLOWS，不多不少。"""
    actual = {p.name for p in WORKFLOWS.glob("*.yml")}
    assert actual == EXPECTED_WORKFLOWS, (
        f"工作流集合变了。期望 {sorted(EXPECTED_WORKFLOWS)}，实得 {sorted(actual)}。\n"
        "工作流数量本身就是复杂度：新增/删除都必须同时改动这个集合、"
        "doc/BRANCHING.md 与 CONTRIBUTING.md 的矩阵，从而在 code review 里被看见。"
    )


@pytest.mark.parametrize(
    "name",
    ["codeql.yml", "protection-audit.yml"],
)
def test_removed_workflows_stay_removed(name: str) -> None:
    """被移除的两个工作流不得复活。

    逐个给理由，因为「加回来」的动机各不相同：

    - ``codeql.yml``：会新增一个必需检查 ``analyze (python)``，且是**异步**的
      （首次扫描 5–10 分钟）。对单人维护 + 运维使用的场景，其噪声大于收益。
      注意：它**不属于** required checks 了，所以加回来不会被保护挡住，
      反而会静默地多跑一个没人看的分析 —— 更需要显式断言。
    - ``protection-audit.yml``：每日巡检。用户明确要求去掉。
    """
    assert not (WORKFLOWS / name).exists(), (
        f"{name} 又出现了。本版刻意移除了它（用户要求只保留核心流程）。"
        "若确有需要，请先更新 doc/BRANCHING.md、CONTRIBUTING.md 的矩阵，"
        "并在此测试里把它加入 EXPECTED_WORKFLOWS。"
    )


# ====================================================== 指针移动模型的核心
#
# 下面三条是**整套设计里最容易被好心改坏**的地方，逐条给出为什么。


def test_promote_moves_the_main_ref_with_lease() -> None:
    """promote 必须用 `--force-with-lease` 把 main **移动**到 dev。

    为什么不是裸 `git push origin dev:main`：lease 钉住「我校验过的那个 main」，
    若有人在校验与推送之间动了 main，推送被拒而不是把对方的提交抹掉。
    这是 race-free 的提权。

    为什么不是 merge：merge 会产生一个 dev 从未验证过的合并提交进入发布线，
    破坏「main 上的每个提交都逐字节等于某个已过 CI 的 dev 提交」这一保证。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "--force-with-lease" in text, "promote 必须用 --force-with-lease 移动 main"
    assert "origin/dev:refs/heads/main" in text, (
        "promote 应以 `origin/dev:refs/heads/main` 的 refspec 推送（指针移动）"
    )
    # 反向：不得对 main 做**裸** push（会抹掉他人提交）
    bare = re.findall(r"git push(?!\s+--force-with-lease)[^\n]*\bmain\b", text)
    assert not bare, f"不得对 main 做裸 push：{bare}"


def test_promote_fails_loudly_when_main_has_unique_commits() -> None:
    """main 有 dev 之外的历史时必须响亮失败，不得静默 merge。

    「main 有独有提交」意味着有人绕过工作流直接写进了 main。
    此时静默 merge 会把未经 dev 验证的内容送进发布线 —— 正是本模型要杜绝的。
    必须先由人把这些提交收回 dev（或确认后重置 main）。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "git cherry" in text, "缺「main 是否含 dev 之外提交」的判定"
    assert "::error::" in text, "main 不可快进时应给 ::error:: 而不是静默处理"
    assert "exit 1" in text, "判定失败必须非零退出"


def test_promote_uses_prefix_or_tag_or_manual() -> None:
    """promote 只能由三种**显式意图**触发，不得对每次 dev 推进都自动改 main。

    三种方式（与参考实现 SnowLuma 一致）：
      ① dev 上以 ``[merge]`` 或 ``chore(release):`` **开头**的提交
      ② 推 ``chore.*`` tag
      ③ 手动 ``workflow_dispatch``

    若缺了这层判定，dev 上每次 feature 合入都会改 main —— 发布节奏失控，
    且 main 的移动就失去了「人决定何时发」的语义。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "[merge]" in text, "缺 [merge] 前缀判定"
    assert "chore\\(release\\)" in text or "chore(release)" in text, "缺 chore(release): 前缀判定"
    assert "workflow_dispatch" in text, "缺手动触发分支"

    triggers = _workflow_triggers(_load(PROMOTE_PATH))
    assert "chore.*" in triggers["push"]["tags"], (
        f"缺 chore.* tag 触发：{triggers['push'].get('tags')}"
    )
    # 前缀必须是**锚定行首**的匹配，'fix: xxx [merge]' 不应触发
    assert re.search(r"\^\\\[merge\\\]", text) or "^\\[merge\\]" in text, (
        "前缀匹配必须锚定行首（^），否则 'fix: xxx [merge]' 也会触发"
    )


def test_promote_does_not_trigger_on_main_push() -> None:
    """promote 不得由 push:main 触发（会形成自触发循环）。"""
    triggers = _workflow_triggers(_load(PROMOTE_PATH))
    assert triggers["push"]["branches"] == ["dev"], (
        f"promote 的 push 只应跟 dev：{triggers['push'].get('branches')}"
    )


def test_promote_has_non_cancelling_concurrency() -> None:
    """提权不得被新一轮触发取消——半个 main 比没 main 糟。"""
    doc = _load(PROMOTE_PATH)
    conc = doc["concurrency"]
    assert conc["group"] == "promote-dev-to-main"
    assert conc["cancel-in-progress"] is False, (
        "提权不取消在途：中断可能留下 main 已推但 tag 未打的半成品状态"
    )


def test_promote_declares_write_permission_at_job_level() -> None:
    """写权限必须在 job 级显式声明（zizmor undocumented-permissions）。"""
    doc = _load(PROMOTE_PATH)
    assert doc["permissions"] == {"contents": "read"}, "顶层应保持只读"
    assert doc["jobs"]["promote"]["permissions"]["contents"] == "write", (
        "promote job 需要 contents: write（唯一向 main 写的通道）"
    )


def test_promote_keeps_credentials_out_of_checkout() -> None:
    """checkout 必须 persist-credentials: false，凭据在校验步骤内显式注入。

    这是 zizmor artipacked 的要求，也与仓库既有的 ci.yml / release.yml 约定一致：
    凭据不落在 .git/config 里，减少被后续步骤误用的面。
    """
    doc = _load(PROMOTE_PATH)
    steps = doc["jobs"]["promote"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkouts, "缺 checkout 步骤"
    assert checkouts[0]["with"].get("persist-credentials") is False, (
        "checkout 应设 persist-credentials: false"
    )
    # 凭据注入必须经 env 而非直接展开进 run:（zizmor template-injection）
    body = "\n".join(str(s.get("run", "")) for s in steps)
    assert "x-access-token:${GH_TOKEN}" in body, "缺凭据注入"
    assert "${{ github.token }}" not in body, (
        "凭据表达式不得直接写进 run: 块——会被判为模板注入，应经 env 传递"
    )


# ==================================================== 守卫工作流（本版恢复）
#
# 注意与上一版的**语义反转**：上一版把 guard 删了（因为服务端会拒绝 base=main
# 的 PR）；本版把它**加回来**。为什么？
#
# 服务端的分支保护在 main 上**不能开 PR 保护**（否则 promote 失效）。
# 也就是说：服务端不再拒绝「以 main 为 base 的 PR」了 —— 那正是上一版删它的理由，
# 而这个理由在本版不成立了。
#
# 若不恢复 guard，有人开一个 base=main 的 PR 并合并，就会在 main 上造出
# dev 没有的提交，随后 promote 失败（"main 存在 dev 之外的提交"）。
# guard 让这个错误**在打开 PR 时就暴露**，并给出可操作的提示。


def test_guard_rejects_prs_targeting_main() -> None:
    """以 main 为 base 的 PR 必须被拒绝，且给出可操作的错误信息。"""
    doc = _load(GUARD_PATH)
    triggers = _workflow_triggers(doc)
    assert triggers["pull_request"]["branches"] == ["main"], (
        f"guard 应只在 base=main 的 PR 上触发：{triggers['pull_request']}"
    )
    jobs = doc["jobs"]
    assert len(jobs) == 1, f"guard 应只有一个 job：{list(jobs)}"
    steps = next(iter(jobs.values()))["steps"]
    body = "\n".join(str(s.get("run", "")) for s in steps)
    assert "exit 1" in body, (
        "guard 必须真的 exit 1。`echo ::error::` + `exit 0` 是不拦任何东西的装饰。"
    )
    assert "::error::" in body, "guard 应给出可操作的错误信息"
    assert "dev" in body, "错误信息应指明「请把 PR 开向 dev」"


def test_guard_is_read_only() -> None:
    """守卫是纯判定，不得有写权限。"""
    doc = _load(GUARD_PATH)
    assert doc["permissions"] == {"contents": "read"}, "守卫不得有写权限"


# ================================================================ ci.yml


def test_ci_push_trigger_follows_dev_not_main() -> None:
    """CI 的 push 触发跟 dev。

    main 由 promote 推进，而 GITHUB_TOKEN 推的提交不触发工作流——把 main 留在
    触发列表里只会让人误以为「main 上有 CI」，实际永远不会跑。
    """
    triggers = _workflow_triggers(_load(CI_PATH))
    assert triggers["push"]["branches"] == ["dev"], (
        f"ci.yml 的 push 触发应为 dev：{triggers['push'].get('branches')}"
    )
    # pull_request 不得过滤 base 分支：guard 需要靠 ci 也出信号，
    # 且任何 base 的 PR 都应得到测试反馈
    pr = triggers["pull_request"]
    assert pr is None or "branches" not in pr, (
        "CI 的 pull_request 不应按 base 过滤——误打 main 的 PR 也要出信号"
    )


def test_ci_keeps_three_required_check_job_names() -> None:
    """三个 required check 的 job name 是外部契约（CONTRIBUTING.md §8.3）。

    改名会让 PR 永久卡在 "Expected — Waiting for status to be reported"，
    且**不会有任何报错**。
    """
    doc = _load(CI_PATH)
    names = {job["name"] for job in doc["jobs"].values()}
    assert names == {
        "lint (ruff / actionlint / zizmor)",
        "typecheck (mypy)",
        "test (pytest + vendor verify)",
    }, f"required check 名字变了会影响外部约定：{sorted(names)}"


# ============================================ 必需检查名：三处同源
#
# 期望值散落在三处引用里，任何一处漏改后果不同：
#   ① doc/BRANCHING.md                —— 人读的说明（漏改 = 文档骗人）
#   ② scripts/apply_branch_protection.sh —— 应用（漏改 = 配错，PR 卡死）
#   ③ CONTRIBUTING.md §8.3            —— 开发人员读的（漏改 = 沟通失败）
# 所以三处都要断言。


def test_required_checks_match_workflow_job_names() -> None:
    """必需检查 context 必须与真实 job name 逐字一致。"""
    actual: set[str] = set()
    for path in (CI_PATH,):
        doc = _load(path)
        for job in doc["jobs"].values():
            if isinstance(job, dict) and job.get("name"):
                actual.add(job["name"])

    missing = [c for c in REQUIRED_CHECKS if c not in actual]
    assert not missing, (
        f"分支保护期望的必需检查在真实工作流里找不到对应 job name：{missing}；"
        f"现有 job name：{sorted(actual)}。"
        "改名时必须同步 doc/BRANCHING.md、scripts/apply_branch_protection.sh、"
        "CONTRIBUTING.md §8.3。"
    )


def test_required_checks_count_is_three() -> None:
    """本版必需检查**恰好三项**。

    CodeQL 移除后若忘了从 REQUIRED_CHECKS 里删掉 ``analyze (python)``，
    PR 会永远卡在等待该检查 —— 一个不存在的 job 永远不会上报。
    """
    assert len(REQUIRED_CHECKS) == 3, f"本版应为三项必需检查：{REQUIRED_CHECKS}"
    assert not any("analyze" in c for c in REQUIRED_CHECKS), (
        "CodeQL 已移除，必需检查里不应还有 analyze"
    )


def test_branching_doc_lists_every_required_check() -> None:
    """BRANCHING.md 必须列出每一条必需检查（人读的那一份也要同步）。"""
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in doc, f"doc/BRANCHING.md 未列出必需检查：{check}"


def test_protection_script_expects_every_required_check() -> None:
    """apply_branch_protection.sh 的 REQUIRED_CHECKS 必须齐全。"""
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in text, f"apply_branch_protection.sh 缺必需检查：{check}"


def test_contributing_lists_every_required_check() -> None:
    """CONTRIBUTING.md 必须列出每一条必需检查（开发人员读的那一份）。"""
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for check in REQUIRED_CHECKS:
        assert check in text, f"CONTRIBUTING.md 未列出必需检查：{check}"


# =================================================== 分支保护配置的语义
#
# 这一段是**本模型区别于常规最佳实践**的地方，每条都给出理由。


def test_main_protection_has_no_pull_request_requirement() -> None:
    """**本文件最重要的一条**：main 的保护里必须**没有** PR 保护。

    分支保护的 "Require a pull request before merging"
    （API 字段 ``required_pull_request_reviews``）会拒绝**所有**直接推送
    —— 包括 promote 工作流自己的推送。一旦开启，Promote 直接失效，
    而 main 将永远停在旧位置，且**不会有任何报错**说明原因。

    这与通用最佳实践相反（常规做法是「main 必须走 PR」），是本模型的有意取舍：
    指针移动模型的「main 不可直接修改」是靠 `enforce_admins` 实现的，
    不是靠 PR 保护。
    """
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    # main 的 payload 里该字段必须是 null
    main_payload = text.split("MAIN_PAYLOAD=$(cat <<JSON", 1)[1].split("JSON\n)", 1)[0]
    assert '"required_pull_request_reviews": null' in main_payload, (
        "main 的保护里 required_pull_request_reviews 必须为 null，否则 promote-dev-to-main 会失效。"
    )
    assert '"enforce_admins": true' in main_payload, (
        "main 必须 enforce_admins=true —— 这是本模型「只有 CI 能改 main」的实现方式"
    )
    assert '"allow_force_pushes": true' in main_payload, (
        "main 必须允许 force push，否则 --force-with-lease 在非快进场景会被拒"
    )
    # main 上不跑 CI（ci.yml 的 push 只跟 dev），所以不能挂 required checks。
    # 漏掉这条断言就会重演 2026-09-19 的真实失误：脚本里写着三条 contexts，
    # 而文档写着 null，两边不一致却**没有任何测试发现**（只断言了 PR 保护那三项）。
    assert '"required_status_checks": null' in main_payload, (
        "main 的 required_status_checks 必须为 null —— main 上不跑 CI，"
        "挂上必需检查会让 main 在缺失 check run 时永久卡在 "
        "'Expected — Waiting for status to be reported'，且无任何报错。"
        "「main 是绿的」由 dev 上那次 CI 承载（两者同 sha）。"
    )


def test_dev_protection_requires_checks_but_not_admins() -> None:
    """dev 是工作分支：要必需检查，但**不**锁死管理员。

    理由：dev 上 CI 因基础设施故障卡住时，维护者需要能直接救急。
    对开发人员而言 dev 的保护仍然生效（他们不是管理员）。
    """
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    dev_payload = text.split("DEV_PAYLOAD=$(cat <<JSON", 1)[1].split("JSON\n)", 1)[0]
    assert '"enforce_admins": false' in dev_payload, "dev 不应锁死管理员——CI 故障时需能救急"
    assert '"allow_force_pushes": false' in dev_payload, (
        "dev 是工作分支，应禁止强推（保护他人的提交）"
    )
    assert "required_approving_review_count" in dev_payload, (
        "dev 应显式声明批准数（说明为什么是 0）"
    )
    # dev 是唯一跑必需检查的分支 —— 三条必须真的在它的 payload 里。
    #
    # ⚠️ 不能直接断言三个检查名出现在 DEV_PAYLOAD 里：payload 用的是
    # `${REQUIRED_CHECKS}` 变量插值（shell heredoc），字面名字只在变量定义处出现。
    # 第一版这么写就红了（2026-09-19）。正确判据分两步：
    #   ① dev 的 payload 必须引用 REQUIRED_CHECKS 变量；
    #   ② REQUIRED_CHECKS 变量里必须有全部三条（含精确的括号/斜杠/空格）。
    assert "required_status_checks" in dev_payload, "dev 必须有必需检查"
    assert "${REQUIRED_CHECKS}" in dev_payload, (
        "dev 的 contexts 应引用 ${REQUIRED_CHECKS} 变量，而不是各抄一份字面清单 —— "
        "抄字面清单就是「清单漂移」的入口"
    )
    for check in REQUIRED_CHECKS:
        assert check in text, (
            f"REQUIRED_CHECKS 变量里缺必需检查：{check}。"
            "它是三条检查名的唯一来源，写错不报错，只让 PR 永久卡在 "
            "'Expected — Waiting for status to be reported'。"
        )
    # 批准数必须是 0 —— 单人维护者无法批准自己的 PR
    assert re.search(r'"required_approving_review_count":\s*0', dev_payload), (
        "批准数必须为 0：单人维护者无法批准自己的 PR，设 1 会自我死锁"
    )


def test_protection_script_reads_back_every_field() -> None:
    """配置脚本必须回读校验，不能只 PUT。

    「写完 ≠ 生效」：组织的 Actions policy、套餐限制、或服务端字段语义差异
    都可能让实际值与请求值不同。且本模型对一个字段的正确性极度敏感
    （main 的 required_pull_request_reviews 若被误设，promote 静默失效），
    所以回读校验里必须**专门**检查它。
    """
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    assert "回读校验" in text, "缺回读校验"
    assert "required_pull_request_reviews" in text.split("回读校验", 1)[1], (
        "回读校验必须专门检查 main 的 required_pull_request_reviews —— 这是本模型静默失效的头号风险"
    )
    assert "--dry-run" in text, "应支持 --dry-run 先预览"


def test_protection_script_is_idempotent_put() -> None:
    """必须用 PUT 全量替换（幂等），而不是 PATCH 增量。"""
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    assert "-X PUT" in text, "应当用 PUT 全量替换以获得幂等性"
    assert (
        "branches/${branch}/protection" in text
        or "branches/%s/protection" in text
        or ("branches/${branch}/protection" in text)
    ), "缺分支保护端点"


def test_protection_script_covers_both_branches() -> None:
    """脚本必须同时配 main 与 dev（本版只有两个分支）。"""
    text = PROTECTION_SCRIPT.read_text(encoding="utf-8")
    assert 'apply_one main "${MAIN_PAYLOAD}"' in text, "缺 main 的应用调用"
    assert 'apply_one dev  "${DEV_PAYLOAD}"' in text or 'apply_one dev "${DEV_PAYLOAD}"' in text, (
        "缺 dev 的应用调用"
    )


# ============================================================ release.yml


def test_promote_autotags_release_at_main_tip() -> None:
    """发布 tag 是 promote 尾部的机械推论，不是人敲的动作。

    版本号唯一来自上游：「main 尖端版本 ≠ 已发布 tag」等价于
    「有已验证未发布的版本」。人工打 tag 等于把同一判断做第二遍，
    忘记就打 = main 绿着但发布面停滞的静默漂移。
    """
    text = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "自动打发布 tag" in text, "promote 尾部缺自动 tag 步骤"
    assert "refs/tags/" in text, "自动 tag 必须先查远端同名 tag（tag 不可变，不顺指不重发）"
    # 续链不赌 tag push：GITHUB_TOKEN 的 push 事件不触发工作流是平台语义
    assert "gh workflow run release.yml" in text, (
        "打完 tag 必须显式 dispatch release（tag push 不可信赖）"
    )


def test_release_accepts_dispatch_and_verifies_main_tip() -> None:
    """release 双入口（v* push + dispatch tag_name），且机器校验 tag 指 main 尖端。

    main 尖端校验把 BRANCHING「发布只在 main 上打 tag」从人工纪律升格为
    门禁：非尖端 tag（dev 中途手打）不再可能产出 Release。
    """
    doc = _load(RELEASE_PATH)
    triggers = _workflow_triggers(doc)
    assert "workflow_dispatch" in triggers, "promote 的续链入口（dispatch tag_name）缺失"
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "tag_name" in inputs, "dispatch 必须收 tag_name 输入"
    text = RELEASE_PATH.read_text(encoding="utf-8")
    assert "refs/heads/main" in text, "缺 tag→main 尖端的机器校验"
    assert "gh release view" in text, "缺双通道重复触发的存在性消化"


def test_release_triggers_on_version_tags_only() -> None:
    """发布只在 ``v*`` tag 上触发。"""
    triggers = _workflow_triggers(_load(RELEASE_PATH))
    tags = triggers["push"]["tags"]
    assert "v*" in tags, f"release 应只在 v* tag 上触发：{tags}"
    # chore.* tag 属于 promote，不应触发发布
    assert "chore.*" not in tags, (
        "chore.* tag 应只触发 promote，不应触发 release（会把 promote 开关当成发版）"
    )


def test_release_whitelist_still_present() -> None:
    """回归护栏：release.yml 的产物白名单不得被误伤。"""
    text = RELEASE_PATH.read_text(encoding="utf-8")
    assert "cp -a metadata.yaml requirements.txt main.py bridge" in text, (
        "release.yml 的产物白名单被改动了"
    )


def test_release_is_fully_autonomous_without_approval_pause() -> None:
    """release job 不得有等待人的闸门：契约里没有人工放行环节。

    环境门在仓库历史上要么是想象（private+Free 不供给），要么是单人
    自动发布下的停摆点（超时含审批等待、self-review 死锁）。安全闸已
    前移为机器判定：sync 供应链判据 + 「tag 必指 main 尖端」+ PR 轨道
    required checks。想恢复人拦环节，先改 BRANCHING 的维护契约，
    不要顺手加 environment。
    """
    doc = _load(RELEASE_PATH)
    job = doc["jobs"]["release"]
    assert "environment" not in job, (
        "release job 出现了 environment 门——自动发布链会停在那里等人批准，"
        "与「人不批 roll 内容」的契约冲突"
    )
    assert 0 < job.get("timeout-minutes", 0) <= 30, (
        f"timeout={job.get('timeout-minutes')} 过大——大值是「超时含人工审批等待」"
        "时代的遗留，现在只该覆盖构建与证明耗时"
    )


# ================================================== 文档与使用纪律


def test_docs_state_reserved_commit_prefixes() -> None:
    """文档必须写明 `[merge]` / `chore(release):` 是**保留前缀**。

    开发人员若在提交信息里用了这两个前缀，会意外触发 promote（把 dev 当成
    发布点）。这是本模型下最容易误用的一处，且后果是「静默发布」。

    参考实现 SnowLuma 的 CONTRIBUTING.md 同样有这条警告。
    """
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "[merge]" in text, "CONTRIBUTING.md 必须写明 [merge] 保留前缀"
    assert "保留前缀" in text or "不要" in text, "只说存在前缀不够，必须写明「开发人员不得使用」"


def test_branching_doc_explains_pointer_model() -> None:
    """doc/BRANCHING.md 必须解释「指针移动」而非「PR 合并」，并给出理由。

    这是本模型最反直觉的地方。若文档只写「main 不可直接修改」而不解释
    「为什么不能开 PR 保护」，下一个人会「顺手打开 PR 保护」并静默搞坏发布。
    """
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    assert "指针" in doc, "BRANCHING.md 必须解释 main 是指针"
    assert "Require a pull request" in doc or "required_pull_request_reviews" in doc, (
        "必须写明「不能给 main 开 PR 保护」及原因"
    )
    assert "promote" in doc, "必须说明 promote 工作流是唯一的 main 写入通道"


def test_default_branch_choice_is_documented() -> None:
    """默认分支选 dev 的决策必须写进文档。

    默认分支设成 dev 之后，「改了工作流但 cron / dispatch 仍跑旧版本」
    这个坑才消失。

    本断言只要求两份文档**合起来**说清这件事：事件 × ref 映射表在
    CONTRIBUTING.md §12.2（运维视角），BRANCHING.md §8 引用默认分支选择本身。
    不去两处都要求同一段文字——那是复制而不是覆盖。
    """
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    assert "默认分支" in doc, "BRANCHING.md 必须说明默认分支的选择"
    assert "dev" in doc, "必须写明默认分支是 dev"
    # 事件 × ref 的映射表在 CONTRIBUTING.md（运维视角的归属地）
    contrib = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "schedule" in contrib, "CONTRIBUTING.md 必须说明 cron 读默认分支的定义"
    assert "12.2" in contrib, "CONTRIBUTING.md 缺事件 × ref 的映射表小节"


def test_docs_contain_per_branch_workflow_matrix() -> None:
    """分支×工作流矩阵是运维参考，别被删掉，且必须与实际文件一致。

    **注意判据**（这条断言第一版就踩过坑）：不能简单断言「已删除的文件名不得
    出现」——文档需要解释「我们移除了 codeql.yml，理由是…」，这既要提及文件名
    又是**正确内容**。字面禁令会与「如实说明为什么不做了」直接冲突。

    第一版修正只把范围缩到「工作流清单那一节」，仍然失败。原因很值得记下来：
    它用 ``split("\n\n\n")`` 切段，而 CONTRIBUTING.md 那里只有**一个**空行，
    于是「切出来的段」实际吞掉了整篇文档的剩余部分 —— 范围根本没缩住。

    所以本版改用**结构性判据**：
      ① 「在役清单」= 清单小节里的**表格行**（形如 ``| `` + 反引号文件名 +
         反引号 + `` |``）。
         表格行才是「这个文件在役」的声明；散文不是。
      ② 反向要求：清单小节里必须有一句**显式**说明哪些被移除了
         （否则「静默删掉不提」也能通过 —— 那同样是在骗运维）。
    """

    def listing_section(name: str) -> str:
        """取「工作流清单」小节的正文（到下一个二级标题或分隔线为止）。"""
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        marker = "**工作流清单**（`.github/workflows/`）："
        assert marker in text, f"{name} 缺工作流清单小节"
        rest = text.split(marker, 1)[1]
        # 到下一个 "===" 分隔线、"## " 标题、或 "---" 前后为止
        stop = re.search(r"\n(?:---|## |\*\*[^*]+\*\*（)", rest)
        return rest[: stop.start()] if stop else rest

    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for name in EXPECTED_WORKFLOWS:
        assert name in text, f"CONTRIBUTING.md 的工作流矩阵缺 {name}"

    section = listing_section("CONTRIBUTING.md")

    # ① 表格行里不得出现已删除的工作流 —— 表格行 = 在役声明
    rows = [ln for ln in section.splitlines() if ln.lstrip().startswith("|") and "`" in ln]
    assert rows, (
        "CONTRIBUTING.md 的工作流清单小节里没有表格行；清单必须用表格给出（散文清单无法被机械校验）"
    )
    for gone in ("codeql.yml", "protection-audit.yml"):
        assert not any(f"`{gone}`" in row for row in rows), (
            f"CONTRIBUTING.md 的在役工作流表格里仍列着已删除的 {gone} —— "
            "表格行是「在役」声明，必须删掉该行；"
            "若要说「为什么不做了」，请写在表格之外的散文里（正文原样放行）"
        )

    # ② 表格里必须恰好是那四个
    listed = {m.group(1) for row in rows for m in re.finditer(r"`([a-z0-9-]+\.yml)`", row)}
    assert listed == EXPECTED_WORKFLOWS, (
        f"CONTRIBUTING.md 的在役工作流表格与实际文件不一致："
        f"表格 {sorted(listed)} vs 实际 {sorted(EXPECTED_WORKFLOWS)}"
    )

    # ③ 必须显式说明哪些被移除了（「静默不提」同样是让运维误判）
    assert "已移除" in section or "已删除" in section, (
        "工作流清单小节必须显式说明有工作流被移除了；"
        "只说「现在有这四个」会让运维不知道上一版的另三个去哪了"
    )


def test_branching_doc_does_not_reference_removed_workflows() -> None:
    """doc/BRANCHING.md 不得把已删除的工作流当作**在役**描述。

    BRANCHING.md **允许**而且**需要**点名这些文件：
    - §9「已知缺口」必须说「上一版用 protection-audit.yml 每日巡检来堵这个缺口，
      本版撤掉了，所以缺口重新打开」——这是本设计最诚实的部分；
    - §10「明确不做的项」要逐条说明为什么不恢复。

    所以判据不能是「出现即违规」（第一版踩了这个坑）。本版改判**时态/语气**：
    已删除的工作流若出现，其所在行必须带**过去式或否定式**限定词
    （上一版/原/曾经/已移除/不再/不做/撤掉…）；若它出现在
    **现在时**的架构描述句里，才判违规 —— 那才是「让运维以为它还在跑」。
    """
    doc = BRANCHING_DOC.read_text(encoding="utf-8")
    gone_names = ("codeql.yml", "protection-audit.yml")
    # 「已不在役」的限定词：出现这些词就说明这句话在讲历史或不做，而非在役
    retired_markers = (
        "上一版",
        "原",
        "曾经",
        "已移除",
        "已删除",
        "不再",
        "不做",
        "撤掉",
        "移除",
        "删除",
        "去掉",
        "没有",
        "缺",
        "缺口",
    )

    for gone in gone_names:
        for lineno, line in enumerate(doc.splitlines(), 1):
            if gone not in line:
                continue
            assert any(m in line for m in retired_markers), (
                f"doc/BRANCHING.md:{lineno} 用现在时提到了已删除的 {gone}：{line.strip()!r}\n"
                "要么删掉这句，要么加限定词说明它已不在役"
                "（如「上一版用 …」「不再使用 …」）。"
                "让运维以为一个已删除的工作流还在跑，比不提它更糟。"
            )

    # 反向：§9 必须显式承认巡检缺口（这是唯一诚实的地方，不能被「顺手删掉」）
    assert "已知缺口" in doc or "缺口" in doc, (
        "BRANCHING.md 必须显式承认「服务端保护配置无自动巡检」这个缺口；"
        "删掉缺口声明会让读者以为已经全覆盖"
    )


def test_docs_describe_the_two_branch_model_only() -> None:
    """文档必须只描述两个长期分支。

    与上面两条同理：「不得出现 `lkg` 字样」是个过严的字面禁令 ——
    说明「为什么不再需要 lkg」正是需要写出它的场合（本仓库的回滚策略一节
    确实解释了「锚点是 tag 不是分支」）。
    正确判据是：**分支表格里不得有第三行**。
    """
    for path in (BRANCHING_DOC, REPO_ROOT / "CONTRIBUTING.md"):
        text = path.read_text(encoding="utf-8")
        for branch in ("`lkg`", "`staging`", "`release` 分支"):
            assert f"| {branch} |" not in text, (
                f"{path.name} 的分支表格里仍有 {branch} 行 —— 本版只有 dev 与 main"
            )
    # 反向：两份文档都必须明确写出「只有两个长期分支」
    for path in (BRANCHING_DOC, REPO_ROOT / "CONTRIBUTING.md"):
        text = path.read_text(encoding="utf-8")
        assert "两条" in text or "两个长期分支" in text, (
            f"{path.name} 未明确声明本版只有两条长期分支"
        )
