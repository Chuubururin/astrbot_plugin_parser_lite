#!/usr/bin/env bash
# 把分支保护幂等地打到 main 上。
#
# 为什么需要这个脚本：仓库从 private 转为 **public** 后，分支保护从「不可用
# （403 Upgrade to GitHub Pro）」变成「完整可用」。这是本次重构最大的收益 ——
# 强制力第一次真的落在服务端，而不是靠 CI 判红去「模拟」拦截。
#
# 但服务端配置有个致命特性：**它可以被人随手在 UI 上点掉，且不会留下任何
# 代码痕迹**。代码有 git 历史与 review，配置没有。所以：
#   ① 本脚本把期望配置**写成代码**（可评审、可 diff、可回滚）；
#   ② .github/workflows/protection-audit.yml 每日巡检实际配置是否仍等于期望；
#   ③ tests/test_branch_model.py 断言本脚本的期望值与 docs/BRANCHING.md 一致。
# 三者合起来才构成「配置不会悄悄漂移」的保证。
#
# 用法：
#   GH_TOKEN 需有 admin:repo 或 repo 权限（workflow 的 GITHUB_TOKEN 权限不够）
#   bash scripts/apply_branch_protection.sh [--dry-run]
#
# 幂等：重复执行结果相同（PUT 语义即为全量替换）。
set -euo pipefail

REPO="${REPO:-${GITHUB_REPOSITORY:-Chuubururin/astrbot_plugin_parser_lite}}"
BRANCH="${BRANCH:-main}"
DRY_RUN="false"
[ "${1:-}" = "--dry-run" ] && DRY_RUN="true"

# ---- 期望值（单一事实来源：这里改，巡检与文档都跟着改）--------------------
#
# 必需状态检查的 context 必须与各工作流的 **job name** 逐字一致
# （不是文件名、不是 workflow name）。写错的后果是 PR 永久卡在
# "Expected — Waiting for status to be reported"，且不会报错，只会一直等。
REQUIRED_CHECKS='[
  "lint (ruff / actionlint / zizmor)",
  "typecheck (mypy)",
  "test (pytest + vendor verify)",
  "analyze (python)"
]'

# enforce_admins=true：管理员也不能绕过。这是本设计的**核心承诺** ——
# 若管理员可绕过，那「main 只由提权通道推进」就只是对普通贡献者的约束。
#
# required_approving_review_count=0 是刻意的（见 docs/BRANCHING.md §决策记录）：
#   · 本模型的强制力核心是「必需检查必须过」，不是「必须有人 approve」；
#   · 单人维护下 count>=1 会**自我死锁**（无法批准自己的 PR），
#     除非同时开 bypass —— 而 bypass 与 enforce_admins=true 语义冲突。
#   · 协作方变多后可提到 1；那时需要重新评估 enforce_admins 与 bypass 的取舍。
PAYLOAD="$(cat <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": ${REQUIRED_CHECKS}
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": true,
  "required_conversation_resolution": false
}
JSON
)"

# 注：required_pull_request_reviews 传 null 而非缺省 —— 缺省在某些 API 版本
# 下会被解释为「保持不变」而不是「清空」，于是重复执行不清除历史配置。
# null 是明确的「不要 review 要求」。
#
# required_linear_history=true 与 fast-forward 提权模型一致：
# promote 做的是 `main ← dev` 快进，从不产生合并提交，所以线性历史是本模型的
# 自然属性而非额外约束。开着它能防止有人用 merge PR 往 main 里塞合并提交。

echo "== 应用分支保护 =="
echo "仓库：${REPO}"
echo "分支：${BRANCH}"
echo "期望配置："
echo "${PAYLOAD}"
echo

if [ "${DRY_RUN}" = "true" ]; then
    echo "::notice::--dry-run：仅打印，不调用 API"
    exit 0
fi

if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "::error::需要 GH_TOKEN 或 GITHUB_TOKEN（且具备仓库 admin 权限）" >&2
    exit 1
fi

# ---- 应用 ----------------------------------------------------------------
# PUT 全量替换：这就是幂等性的来源，无需先 GET 再 diff。
gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "repos/${REPO}/branches/${BRANCH}/protection" \
    --input - <<< "${PAYLOAD}" \
    > /dev/null

echo "✅ 分支保护已应用"
echo

# ---- 回读校验：写成功 ≠ 生效 ---------------------------------------------
# 这一步不是多余的：API 返回 200 但字段被服务端规范化（例如把 contexts
# 拆成 checks 数组、或忽略未知字段）是真实发生过的。回读比对才是「生效」的证据。
echo "== 回读校验 =="
ACTUAL="$(gh api "repos/${REPO}/branches/${BRANCH}/protection")"

fail=0
check() {
    local label="$1" actual="$2" expected="$3"
    if [ "${actual}" = "${expected}" ]; then
        echo "  ✓ ${label} = ${actual}"
    else
        echo "  ✗ ${label}：期望 ${expected}，实际 ${actual}" >&2
        fail=1
    fi
}

check "enforce_admins" \
    "$(echo "${ACTUAL}" | jq -r '.enforce_admins.enabled')" "true"
check "strict" \
    "$(echo "${ACTUAL}" | jq -r '.required_status_checks.strict')" "true"
check "allow_force_pushes" \
    "$(echo "${ACTUAL}" | jq -r '.allow_force_pushes.enabled')" "false"
check "allow_deletions" \
    "$(echo "${ACTUAL}" | jq -r '.allow_deletions.enabled')" "false"
check "required_linear_history" \
    "$(echo "${ACTUAL}" | jq -r '.required_linear_history.enabled')" "true"

# contexts 顺序不保证一致，故排序后比对
actual_checks="$(echo "${ACTUAL}" | jq -r '.required_status_checks.contexts | sort | join(" | ")')"
expected_checks="$(echo "${REQUIRED_CHECKS}" | jq -r 'sort | join(" | ")')"
check "required_status_checks.contexts" "${actual_checks}" "${expected_checks}"

if [ "${fail}" -ne 0 ]; then
    echo >&2
    echo "::error::分支保护未按期望生效 —— 上面的 ✗ 项需人工排查（详见 docs/BRANCHING.md）" >&2
    exit 1
fi

echo
echo "✅ 回读校验全部通过：main 的强制力已在服务端生效"
