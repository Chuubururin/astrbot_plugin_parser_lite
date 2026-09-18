#!/usr/bin/env bash
# 拒绝向 main 的非快进推送（Tree-hygiene：先 revert 恢复绿，而不是 force push）。
# pre-commit pre-push 钩子（.pre-commit-config.yaml no-force-push-main）调用；
# stdin 行格式：<local ref> <local sha> <remote ref> <remote sha>，删除远端分支（全零 sha）放行。
set -u

while read -r _ local_sha remote_ref remote_sha; do
    [ "$remote_ref" = "refs/heads/main" ] || continue
    [ "$remote_sha" != "0000000000000000000000000000000000000000" ] || continue
    if ! git merge-base --is-ancestor "$remote_sha" "$local_sha"; then
        echo "BLOCKED: non-fast-forward push to main. Revert instead (Tree-hygiene: green first)." >&2
        exit 1
    fi
done
exit 0
