#!/usr/bin/env bash
# 应用 main / dev 的分支保护 —— 幂等，可重复执行。
#
# 用法：
#   bash scripts/apply_branch_protection.sh --dry-run   # 只打印将要发送的内容
#   bash scripts/apply_branch_protection.sh             # 应用并回读校验
#
# 为什么用脚本而不是在 UI 上点：
#   UI 点击无法 review、无法复现、无法在换台机器时重放。脚本进版本库，
#   配置本身就成了可审计的代码。
#
# ── 本模型最重要的一条：main 不能开 PR 保护 ──────────────────────────────
# 分支保护的 "Require a pull request before merging"
# （API 字段 required_pull_request_reviews）会拒绝**所有**直接推送到 main 的
# ref 更新 —— 包括 promote-dev-to-main 工作流自己的推送。
# 一旦开启，Promote 直接失效。
# 因此 main 的配置里该字段必须是 null（见下方 MAIN_PAYLOAD），
# 这是**有意为之**，不是遗漏。
#
# 与之配套：
#   - main 用 required_status_checks 保证质量
#   - main 用 enforce_admins=true 保证「只有 CI 能推」
#   - 用 main-pr-target-guard.yml 拦住以 main 为 base 的 PR
#
# ── 必须与这三个地方保持一致（改动时同步改）────────────────────────────
#   1. .github/workflows/*.yml 里各 job 的 name（事实来源）
#   2. 本文件的 REQUIRED_CHECKS
#   3. CONTRIBUTING.md 的必需检查表格
# tests/test_branch_model.py 有断言把这三处钉死，改名会立刻变红。

set -uo pipefail

DRY_RUN=false
case "${1:-}" in
  --dry-run) DRY_RUN=true ;;
  '') ;;
  *) echo "未知参数：$1（只支持 --dry-run）" >&2; exit 2 ;;
esac

REPO="${REPO:-Chuubururin/astrbot_plugin_parser_lite}"

# 必需检查名 —— 必须与 workflow 里 jobs.<id>.name **逐字一致**（含括号/空格/斜杠）。
# 对不上时 GitHub 不报错，PR 只会永远卡在 "Expected — Waiting for status to be reported"。
REQUIRED_CHECKS='[
  "lint (ruff / actionlint / zizmor)",
  "typecheck (mypy)",
  "test (pytest + vendor verify)"
]'

# ── main：发布指针 ────────────────────────────────────────────────────────
MAIN_PAYLOAD=$(cat <<JSON
{
  "required_status_checks": {
    "strict": false,
    "contexts": ${REQUIRED_CHECKS}
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": true,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
JSON
)

# ── dev：工作分支 ────────────────────────────────────────────────────────
DEV_PAYLOAD=$(cat <<JSON
{
  "required_status_checks": {
    "strict": false,
    "contexts": ${REQUIRED_CHECKS}
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
JSON
)

apply_one() {
  local branch="$1" payload="$2"

  echo "────────────────────────────────────────"
  echo "分支：${branch}"
  echo "────────────────────────────────────────"
  echo "${payload}" | python3 -m json.tool
  echo

  if [ "${DRY_RUN}" = true ]; then
    echo "[dry-run] 跳过实际 PUT"
    echo
    return 0
  fi

  if ! printf '%s' "${payload}" \
      | gh api -X PUT "repos/${REPO}/branches/${branch}/protection" --input - >/dev/null; then
    echo "::error::PUT ${branch} 分支保护失败" >&2
    return 1
  fi
  echo "已应用 ${branch} 的保护配置。"
  echo

  # ── 回读校验 ────────────────────────────────────────────────────────────
  # PUT 成功 ≠ 配置生效：组织的 Actions policy、套餐限制、
  # 或服务端字段语义差异都可能让实际值与请求值不同。逐字段比对。
  local got expect rc=0
  check() { # $1=jq 表达式  $2=期望值  $3=字段名
    got="$(gh api "repos/${REPO}/branches/${branch}/protection" --jq "$1" 2>/dev/null)"
    if [ "${got}" != "$2" ]; then
      echo "::error::${branch} 的 $3 期望 '$2'，实得 '${got}'" >&2
      rc=1
    else
      echo "  ✓ $3 = ${got}"
    fi
  }

  check '.required_status_checks.strict'               'false' 'strict'
  check '.required_status_checks.contexts | length'    '3'     '必需检查数量'
  check '.allow_deletions.enabled'                     'false' 'allow_deletions'
  check '.required_linear_history.enabled'             'false' 'required_linear_history'

  if [ "${branch}" = 'main' ]; then
    check '.enforce_admins.enabled'      'true'  'enforce_admins'
    check '.allow_force_pushes.enabled'  'true'  'allow_force_pushes'
    # main **必须没有** PR 保护，否则 promote 会失效。这条是本模型的关键断言。
    if [ "$(gh api "repos/${REPO}/branches/main/protection" --jq 'has("required_pull_request_reviews")' 2>/dev/null)" = 'true' ]; then
      echo "::error::main 开了 PR 保护 —— promote-dev-to-main 会失效，必须移除" >&2
      rc=1
    else
      echo "  ✓ required_pull_request_reviews 未启用（promote 可用）"
    fi
  else
    check '.enforce_admins.enabled'     'false' 'enforce_admins'
    check '.allow_force_pushes.enabled' 'false' 'allow_force_pushes'
  fi

  # 逐个核对必需检查名确实被服务端接受（写错名字在 PUT 时不报错）
  local ctx
  for ctx in "lint (ruff / actionlint / zizmor)" "typecheck (mypy)" "test (pytest + vendor verify)"; do
    if gh api "repos/${REPO}/branches/${branch}/protection" \
         --jq '.required_status_checks.contexts[]' 2>/dev/null | grep -Fxq "${ctx}"; then
      echo "  ✓ 必需检查：${ctx}"
    else
      echo "::error::${branch} 缺少必需检查 '${ctx}'" >&2
      rc=1
    fi
  done

  echo
  return "${rc}"
}

echo "仓库：${REPO}"
[ "${DRY_RUN}" = true ] && echo "模式：dry-run（不写入）" || echo "模式：应用"
echo

rc=0
apply_one main "${MAIN_PAYLOAD}" || rc=1
apply_one dev  "${DEV_PAYLOAD}"  || rc=1

echo "────────────────────────────────────────"
if [ "${DRY_RUN}" = true ]; then
  echo "dry-run 结束。去掉 --dry-run 以实际应用。"
elif [ "${rc}" = 0 ]; then
  echo "全部应用并校验通过。"
else
  echo "存在校验失败项，请查看上方 ::error:: 输出。" >&2
fi
exit "${rc}"
