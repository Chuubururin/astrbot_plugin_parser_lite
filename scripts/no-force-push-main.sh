#!/usr/bin/env bash
# 拒绝向受保护的长期分支（main / dev）的非快进推送
# （Tree-hygiene：先 revert 恢复绿，而不是 force push）。
#
# pre-commit pre-push 钩子（.pre-commit-config.yaml no-force-push-main）调用；
# stdin 行格式：<local ref> <local sha> <remote ref> <remote sha>，
# 删除远端分支（全零 sha）放行。
#
# 为什么把 dev 也纳入：dev 是工作分支，但它是 promote-dev-to-main 的**输入**——
# 提权做的是 `dev` 快进到 `main`。若 dev 自身历史可被改写，则
#   ① promote 的 merge-base 校验会在改写后拒绝提权（main 不再是祖先），
#   ② 已提权到 main 的提交在 dev 上消失，两条线失去可比性。
# dev 上要重整历史（rebase/squash）时走「新分支 + PR」：改动经评审进入 dev，
# 而不是就地覆写它。
#
# 注：lkg / sync/standalone-* / feature 分支刻意**不**受此约束——前者是回滚锚点，
# 每次 roll 覆写是语义正确的；后两者是短生命周期分支，允许 force 推送。
set -u

PROTECTED_REFS="refs/heads/main refs/heads/dev"
ZERO_SHA="0000000000000000000000000000000000000000"

while read -r _ local_sha remote_ref remote_sha; do
    # 只检查受保护的长期分支
    case " ${PROTECTED_REFS} " in
        *" ${remote_ref} "*) ;;
        *) continue ;;
    esac
    # 创建分支（远端尚不存在）放行
    [ "$remote_sha" != "$ZERO_SHA" ] || continue
    if ! git merge-base --is-ancestor "$remote_sha" "$local_sha"; then
        echo "BLOCKED: non-fast-forward push to ${remote_ref#refs/heads/}." >&2
        echo "这两个分支（main/dev）历史不可改写：main 是发布指针，dev 是提权输入。" >&2
        echo "改走「新分支 + PR」；回滚用 revert 而非 force（Tree-hygiene: green first）。" >&2
        exit 1
    fi
done
exit 0
