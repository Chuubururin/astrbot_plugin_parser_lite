"""两层注入管线的脚本间共享底座（无锚点/规则知识）。

scripts/ 非包：直接运行与 importlib 按文件路径加载两种形态都靠各脚本内的
sys.path 垫片导入本模块（mypy 对该顶层名按幻影依赖 Any 化，见 pyproject
overrides）。仅收敛四件逐字节敏感的公共实现——上游克隆只读管道
（git show / ls-tree / rev-parse）与产物字节公式（json.dumps indent=2 +
尾换行）；锚点规则、防火墙与产物形态归属各提取脚本模块（单一事实源）。

另收敛编排层的两条上游真值纪律（sync-upstream 工作流与 scripts/roll_local.py
共用同一实现，杜绝两处语义漂移）：

- 祖先校验门：standalone 分支除随 main push 自动发布外还会发布未合入 PR 的
  预览构建（实证 f9e8c67 = PR #306 中间态 adc5403 的预览）——构建源必须 ∈
  远端 main 祖先才允许 vendor roll，PR 预览跳过（平面照常跟、state 留痕）；
- 远端真值提取源：git fetch 的 pack 路径可能被本地网络缓存钉在旧值
  （origin/main 长期停在 201ebe8，而 ls-remote 已到 adc5403），故平面提取
  源以 ls-remote 的远端 tip 为准。

CLI（工作流侧只经此处调用，不把上述语义抄进 YAML）：

    python scripts/pipeline_common.py parse-subject --subject "<上游提交主题>"
    python scripts/pipeline_common.py remote-main  [--repo DIR]
    python scripts/pipeline_common.py plane-ref    [--repo DIR]
    python scripts/pipeline_common.py is-ancestor  [--repo DIR] --candidate X --tip Y
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# 上游构建 sha / 分支 tip 的受控形态（短 sha 亦合法）
_SHA_RE = re.compile(r"[0-9a-f]{7,40}\Z")
# 上游自动发布提交形如 "auto: publish standalone for <main_sha>"（$ 锚定保证形态合法）
_BUILD_SOURCE_RE = re.compile(r"[0-9a-f]{7,40}$")


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """git 子命令（在上游克隆内执行）；一律二进制捕获，换行翻译交给调用点决定。"""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)


def _detail(result: subprocess.CompletedProcess[bytes]) -> str:
    """失败详情（stderr 优先，退回 stdout）。"""
    return (result.stderr or result.stdout).decode("utf-8", "replace").strip()


def git_show_bytes(repo: Path, ref: str, path: str) -> bytes:
    """读上游克隆内文件的逐字节真值；失败响亮退出（缺克隆/未 fetch 都在此点名）。"""
    result = _git(repo, ["show", f"{ref}:{path}"])
    if result.returncode != 0:
        raise SystemExit(f"无法读取上游 {ref}:{path}（请先 fetch 目标分支）：{_detail(result)}")
    return result.stdout


def git_show_text(repo: Path, ref: str, path: str) -> str:
    """git_show_bytes 的 UTF-8 解码（不做通用换行翻译：CRLF 原样保留）。

    等价 subprocess 的 ``text=True, newline=""``；此前用 ``text=True``
    （newline=None）会把 CRLF 折成 LF，破坏上游模板的「逐字节快照」。
    """
    return git_show_bytes(repo, ref, path).decode("utf-8")


def git_show(repo: Path, ref: str, path: str) -> str:
    """兼容名：等价 git_show_text（既有调用点取 str；逐字节消费用 git_show_bytes）。"""
    return git_show_text(repo, ref, path)


def git_ls_tree(repo: Path, ref: str, dirpath: str) -> list[str]:
    """列上游克隆内目录下的文件路径（相对仓库根）；失败响亮退出。"""
    result = _git(repo, ["ls-tree", "-r", "--name-only", ref, "--", dirpath])
    if result.returncode != 0:
        raise SystemExit(
            f"无法列取上游 {ref}:{dirpath}（请先 fetch 目标分支）：{_detail(result)}",
        )
    return [line for line in result.stdout.decode("utf-8").splitlines() if line]


def rev_parse(repo: Path, ref: str) -> str:
    """读上游克隆引用的完整 sha（快照注记 source_revision）。"""
    result = _git(repo, ["rev-parse", ref])
    if result.returncode != 0:
        raise SystemExit(f"无法读取上游引用 {ref}（请先 fetch）：{_detail(result)}")
    return result.stdout.decode("utf-8").strip()


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    """产物落盘的唯一字节公式（提取脚本 CLI 与编排层共用，保证逐字节幂等）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---- 编排层共用的上游真值纪律（sync-upstream 工作流 ←→ roll_local）----


def parse_build_source_sha(subject: str) -> str:
    """解析上游自动发布提交主题尾部的构建源 main sha。

    解析失败响亮退出：兜底值（如 unknown）会被祖先校验门误判为 PR 预览构建，
    未合入代码反而滞留 vendor（与 scripts/roll_local.py 的 _detect 同一语义）。
    """
    match = _BUILD_SOURCE_RE.search(subject.strip())
    if not match:
        raise SystemExit(
            f"standalone 提交主题无法解析 main sha（上游发布格式可能已变），拒绝继续：{subject!r}",
        )
    return match.group(0)


def remote_branch_sha(repo: Path, remote: str = "origin", branch: str = "main") -> str:
    """远端分支 tip 真值（ls-remote）：fetch 的 pack 路径可能被本地网络缓存钉在旧值。"""
    result = _git(repo, ["ls-remote", remote, f"refs/heads/{branch}"])
    if result.returncode != 0:
        raise SystemExit(f"ls-remote {remote} {branch} 失败：{_detail(result)}")
    fields = result.stdout.split()
    sha = fields[0].decode("ascii", "replace") if fields else ""
    if not _SHA_RE.fullmatch(sha):
        raise SystemExit(f"远端 {remote}/{branch} sha 形态异常：{sha!r}")
    return sha


def has_commit(repo: Path, sha: str) -> bool:
    """sha 的 commit 对象是否已在本地（cat-file -e 探针，失败即视为缺失）。"""
    return _git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0


def resolve_plane_ref(repo: Path, remote: str = "origin", branch: str = "main") -> str:
    """平面提取源引用：远端 tip 已在本地且本地 ref 滞后时直取 sha。

    直取 sha 绕开镜像缓存竞态（本地 origin/main 可能被钉在旧值）；本地 ref 与
    远端一致或缺对象时退回 ``remote/branch``（对象缺失时按 ref 提取，语义不变）。
    """
    remote_sha = remote_branch_sha(repo, remote, branch)
    local = _git(repo, ["rev-parse", "--verify", "--quiet", f"{remote}/{branch}^{{commit}}"])
    local_sha = local.stdout.decode("utf-8").strip() if local.returncode == 0 else ""
    if remote_sha != local_sha and has_commit(repo, remote_sha):
        return remote_sha
    return f"{remote}/{branch}"


def is_ancestor(repo: Path, candidate: str, tip: str) -> bool:
    """candidate 是否为 tip 的祖先（PR 预览构建的判定依据）。

    退出码 1 = 明确「非祖先」；其余非零（128：对象缺失、short sha 歧义等）=
    校验本身失败——后者若也当 False，会把校验失败误判为 PR 预览构建并静默跳过
    vendor roll，未合入代码反而滞留 vendor，故响亮退出（对齐 roll_local）。
    """
    result = _git(repo, ["merge-base", "--is-ancestor", candidate, tip])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise SystemExit(
        f"merge-base --is-ancestor 校验失败（退出码 {result.returncode}）：{_detail(result)}",
    )


def main(argv: list[str] | None = None) -> int:
    """编排层 CLI：把上述真值纪律暴露给工作流 YAML（不复制语义进 bash）。"""
    parser = argparse.ArgumentParser(description="两层注入管线共享底座的编排层真值查询")
    parser.add_argument("--repo", default=".sync-work/upstream", help="上游克隆目录")
    parser.add_argument("--remote", default="origin", help="远端名")
    parser.add_argument("--branch", default="main", help="上游主干分支名")
    sub = parser.add_subparsers(dest="command", required=True)
    p_subject = sub.add_parser("parse-subject", help="解析自动发布提交主题尾部的 main sha")
    p_subject.add_argument(
        "--subject",
        required=True,
        help="上游提交主题（git log -1 --format=%s）",
    )
    sub.add_parser("remote-main", help="打印远端 main tip sha（ls-remote 真值）")
    sub.add_parser("plane-ref", help="打印平面提取源引用（远端真值优先）")
    p_ancestor = sub.add_parser("is-ancestor", help="祖先校验：0=是 / 1=否 / 2=校验失败")
    p_ancestor.add_argument("--candidate", required=True, help="待判定构建源 sha")
    p_ancestor.add_argument("--tip", required=True, help="基准 tip sha")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    if args.command == "parse-subject":
        print(parse_build_source_sha(args.subject))
        return 0
    if args.command == "remote-main":
        print(remote_branch_sha(repo, args.remote, args.branch))
        return 0
    if args.command == "plane-ref":
        print(resolve_plane_ref(repo, args.remote, args.branch))
        return 0
    try:
        ancestor = is_ancestor(repo, args.candidate, args.tip)
    except SystemExit as exc:  # 校验本身失败：与「非祖先」区分（退出码 2）
        print(exc, file=sys.stderr)
        return 2
    return 0 if ancestor else 1


if __name__ == "__main__":
    raise SystemExit(main())
