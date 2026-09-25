"""滚动同步的 roll 序列唯一实现：上游 standalone → vendor 整树重建 → 两层注入。

`sync-upstream.yml`（CI 编排）与本地手动驱动**共用本脚本**，roll 序列因此
只有一份实现——工作流侧不抄 fetch/注入/校验，避免双轨在细节上漂移。上游
standalone 分支由其 CI 随 main 每次 push 自动发布，本脚本把 vendor 轨的
滚动收敛为单命令：

1. fetch 上游 standalone（孤儿单提交构建）+ main（平面提取源），网络抖动
   自动重试；
2. 变更检测：standalone_sha 与 .github/sync-state.json 记录比对，未变化
   幂等退出（--check 只检测不执行，检测到更新时退出码 1）；
3. 整树重建 vendor（唯一升级方式，禁止 merge/patch；standalone 归档逐
   成员安全解包——realpath 校验拒绝路径穿越）→ 派生 requirements →
   两层注入单命令（提取四数据面 → 分析 → 生成）→ vendor 三层校验 →
   全量契约测试；
4. 更新 sync-state（version/main_sha/standalone_sha/synced_at/failures）
   并提交。

安全口径：进程启动于仓库根（os.chdir）；git 子命令统一经 _git（字面量
参数列表、无 shell），引用固定为 standalone/main/FETCH_HEAD；固定步骤
的 python 调用逐条内联字面量列表；归档解包逐成员 realpath 校验落盘
路径（pathlib 拼接）。

两条编排层上游真值纪律（提交主题解析 / 远端 main 真值提取源）与祖先校验门
本章程由 **scripts/pipeline_common.py 单点承载**，双轨共用同一语义——
逐轨各写一份会在细节上漂移。

用法::

     python3 scripts/roll_local.py --check    # 只检测；有更新退出码 1，无更新 0
     python3 scripts/roll_local.py            # 检测到变化即执行完整 roll
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".github" / "sync-state.json"
UPSTREAM_DIR = REPO_ROOT / ".sync-work" / "upstream"
UPSTREAM_REPO = "https://github.com/sokoko-org/nonebot-plugin-parser-lite.git"
VENDOR_PKG = REPO_ROOT / "vendor" / "nonebot_plugin_parser_lite"
VENDOR_META = REPO_ROOT / "vendor" / "_upstream"
VENDOR_PKG_REL = Path("src") / "nonebot_plugin_parser_lite"
# 本地平面 ref 名（与远端真值相等时直接用它；resolve_plane_ref 的退回值）
_LOCAL_PLANE_REF = "origin/main"
PLITES = ("pyproject.toml", "requirements.txt", "README.md", "LICENSE")
ROLL_ADD_PATHS = (
    "vendor/",
    "requirements.txt",
    "requirements/host-provided.txt",
    "metadata.yaml",
    "_conf_schema.json",
    "bridge/gen_config.py",
    "bridge/texts.py",
    "bridge/render_params.py",
    "README.md",
    "templates",
    ".github/sync-state.json",
)

# 平级共享底座（git 只读管道 + 编排层真值纪律）：scripts/ 非包，直接运行与
# importlib 按路径加载两种形态都靠 sys.path 垫片导入
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pipeline_common import (  # noqa: E402
    git_show_text,
    is_ancestor,
    parse_build_source_sha,
    remote_branch_sha,
    resolve_plane_ref,
)

_SHA_RE = re.compile(r"[0-9a-f]{7,40}\Z")


def _git(args: list[str]) -> tuple[int, str]:
    """git 子命令（在 .sync-work/upstream 内执行）；返回 (退出码, stderr)。"""
    result = subprocess.run(
        ["git", "-C", ".sync-work/upstream", *args], capture_output=True, text=True
    )
    return result.returncode, result.stderr.strip() or result.stdout


def _git_bytes(args: list[str]) -> tuple[int, bytes]:
    """git 子命令二进制变体（archive 等输出非 UTF-8 文本的场景）。"""
    result = subprocess.run(["git", "-C", ".sync-work/upstream", *args], capture_output=True)
    return result.returncode, result.stdout


def _git_or_die(args: list[str]) -> str:
    code, out = _git(args)
    if code != 0:
        raise SystemExit(f"git {args[0]} 失败：{out}")
    return out


def _fetch_standalone(attempts: int = 3) -> None:
    """fetch 上游 standalone（网络抖动指数退避重试）。"""
    last = ""
    for _ in range(attempts):
        code, err = _git(["fetch", "origin", "standalone"])
        if code == 0:
            return
        last = err
        time.sleep(1)
    raise SystemExit(f"fetch origin standalone 连续 {attempts} 次失败：{last}")


def _fetch_main(attempts: int = 3) -> None:
    """fetch 上游 main（平面提取源；网络抖动指数退避重试）。"""
    last = ""
    for _ in range(attempts):
        code, err = _git(["fetch", "origin", "main"])
        if code == 0:
            return
        last = err
        time.sleep(1)
    raise SystemExit(f"fetch origin main 连续 {attempts} 次失败：{last}")


def _ensure_clone() -> None:
    if (UPSTREAM_DIR / ".git").is_dir():
        return
    UPSTREAM_DIR.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--branch", "standalone", UPSTREAM_REPO, str(UPSTREAM_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"上游克隆失败：{result.stderr.strip()}")


def _detect() -> tuple[str, str, str]:
    """返回 (new_standalone, new_main, old_standalone)。"""
    _fetch_standalone()
    new_standalone = _git_or_die(["rev-parse", "FETCH_HEAD"]).strip()
    if not _SHA_RE.fullmatch(new_standalone):
        raise SystemExit(f"standalone sha 形态异常：{new_standalone!r}")
    subject = _git_or_die(["log", "-1", "--format=%s", "FETCH_HEAD"]).strip()
    # 上游自动发布提交形如 "auto: publish standalone for <main_sha>"。解析规则
    # 与 sync-upstream 工作流共用 scripts/pipeline_common.py 的同一实现：双轨
    # 各写一份正则曾在细节上漂移（M16）；解析失败响亮退出——兜底值会被祖先
    # 校验门误判为 PR 预览构建，未合入代码反而滞留 vendor。
    new_main = parse_build_source_sha(subject)
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return new_standalone, new_main, state["standalone_sha"]


def _extract_archive_blob(blob: bytes, src: Path) -> None:
    """归档逐成员安全解包：成员名先规范化（拒绝绝对路径/上跳分量/链接成员），
    再 realpath 校验落盘路径必须留在目标目录内。

    符号/硬链接响亮拒绝而非跳过（响亮失败）：快照内不应存在，静默跳过会让
    vendor 树缺文件——deferred 到 verify_vendor 才暴露且信息含糊。

    校验与落盘分两趟：全部成员过门后才写盘，违规成员不会留下「已落盘的前缀」
    （门先于任何落盘与 chmod）。落盘后按成员 mode 位恢复权限——CI 侧用 cp -a
    保留可执行位，只写内容会让双轨 vendor 树 mode 分叉，而 verify_vendor 只比
    内容故不报。
    """
    src_real = src.resolve()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as tar:
        plan: list[tuple[tarfile.TarInfo, Path]] = []
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute():
                raise SystemExit(f"归档成员名为绝对路径，拒绝解包：{member.name!r}")
            if ".." in name.parts:
                raise SystemExit(f"归档成员名含上跳分量，拒绝解包：{member.name!r}")
            if member.issym() or member.islnk():
                raise SystemExit(f"归档成员为符号/硬链接，拒绝解包：{member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise SystemExit(f"归档成员为非常规类型，拒绝解包：{member.name!r}")
            dest = (src / member.name).resolve()
            if not dest.is_relative_to(src_real):
                raise SystemExit(f"归档成员路径越界，拒绝解包：{member.name!r}")
            plan.append((member, dest))
        for member, dest in plan:
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_file = tar.extractfile(member)
            if src_file is None:
                raise SystemExit(
                    f"归档文件成员无法读取内容（非常规归档），拒绝解包：{member.name!r}",
                )
            dest.write_bytes(src_file.read())
            os.chmod(dest, member.mode & 0o777)


def _rebuild_vendor(standalone_sha: str) -> None:
    """整树重建 vendor（唯一升级方式，禁止 merge/patch）。

    归档/检出源必须是显式 standalone sha：FETCH_HEAD 会被 _fetch_main 覆写
    （沿用 FETCH_HEAD 时可能误档 main 树，standalone 专有文件直接
    FileNotFoundError），孤儿
    分支的构建 sha 才是跨函数稳定的唯一引用。
    """
    for stale in (VENDOR_PKG, VENDOR_META):
        if stale.exists():
            shutil.rmtree(stale)
    VENDOR_PKG.mkdir(parents=True)
    VENDOR_META.mkdir(parents=True)
    # 克隆工作区同步切到新 standalone（verify_vendor 与工作区比对，旧检出
    # 必然误报不一致）；checkout 到显式 sha 为分离头，不动任何本地分支
    code, err = _git(["checkout", standalone_sha])
    if code != 0:
        raise SystemExit(f"standalone 检出失败：{err}")
    with tempfile.TemporaryDirectory(prefix="roll-standalone-") as tmp:
        src = Path(tmp) / "sa"
        src.mkdir()
        code, blob = _git_bytes(["archive", standalone_sha])
        if code != 0:
            raise SystemExit("standalone archive 失败")
        _extract_archive_blob(blob, src)
        shutil.copytree(src / VENDOR_PKG_REL, VENDOR_PKG, dirs_exist_ok=True)
        for name in PLITES:
            shutil.copy2(src / name, VENDOR_META / name)


def _state_write(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _tree_version(ref: str) -> str:
    """读 standalone 构建树内 pyproject 的版本号（经 git show，不依赖工作区）。

    经共享底座的 ``git_show_text``：它不做通用换行翻译（CRLF 原样保留），
    而此前的 ``subprocess(text=True)`` 会把 CRLF 折成 LF（L5）。
    """
    out = git_show_text(UPSTREAM_DIR, ref, "pyproject.toml")
    return tomllib.loads(out)["project"]["version"]


def _is_ancestor(candidate: str, tip: str) -> bool:
    """candidate 是否为 tip 的祖先（PR 预览构建的判定依据）。

    语义（含退出码 1 = 明确「非祖先」、其余非零 = 校验本身失败则响亮退出）
    与 sync-upstream 工作流共用 scripts/pipeline_common.py 的同一实现：
    双轨各写一份曾在细节上漂移（M16）。
    """
    return is_ancestor(UPSTREAM_DIR, candidate, tip)


def _release_advisory(old_version: str, new_version: str) -> None:
    """跨越区 release notes 机器解析（💥/🚀/📦 节）——更新规律匹配自动化的一环。

    辅助通道不阻断 roll：抓取失败（网络等）响亮提示人工查阅、roll 照常
    推进；注入面的真正闸门是 roll 后全量契约测试。版本序与节解析见
    scripts/release_advisory.py。
    """
    if old_version == new_version:
        return
    result = subprocess.run(
        [
            "python3",
            "scripts/release_advisory.py",
            "--old",
            old_version,
            "--new",
            new_version,
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        print(
            "⚠️ release advisory 抓取失败（非阻断）——请人工查阅上游 releases 页面"
            f"（关注 breaking 项）：{result.stderr.strip()}"
        )


def _roll(new_standalone: str, new_main: str, old_standalone: str) -> None:
    for value in (new_standalone, new_main):
        if not _SHA_RE.fullmatch(value):
            raise SystemExit(f"引用 sha 形态异常：{value!r}")
    old_short, new_short = old_standalone[:12], new_standalone[:12]

    _fetch_standalone()
    new_version = _tree_version(new_standalone)
    _fetch_main()

    # 平面提取源取「远端 main 真值」（ls-remote）：git fetch 的 pack 路径可能被
    # 本地网络缓存钉在旧值（实证：origin/main 长期停在 201ebe8，而 ls-remote
    # 与 standalone 通道早已到 adc5403）。提取源选择与祖先门基准共用
    # scripts/pipeline_common.py 的同一实现（M16：双轨曾各写一份而细节漂移）。
    remote_main = remote_branch_sha(UPSTREAM_DIR)
    plane_ref = resolve_plane_ref(UPSTREAM_DIR)
    if plane_ref != _LOCAL_PLANE_REF:
        os.environ["PLITE_SYNC_REF"] = plane_ref
        print(f"平面提取源使用远端真值 {plane_ref[:12]}（本地 ref 滞后时绕开镜像缓存竞态）")
    # 祖先校验门（以远端 main 真值为基准）：standalone 分支除了随 main push
    # 自动发布外还会发布未合入 PR 的预览构建（实证：f9e8c67 即 PR #306
    # 中间态 adc5403 的预览，而 #306 head 已前移且未合入）。构建源不在
    # 远端 main 祖先内 = PR 预览，跳过 vendor roll（否则未合入代码进入
    # 生产），平面照常跟 main。
    if not _is_ancestor(new_main, remote_main):
        print(
            f"跳过 vendor roll：standalone 构建源 {new_main[:12]} 不在远端 main "
            f"({remote_main[:12]}) 祖先内"
            f"（PR 预览构建 v{new_version}，等待上游合入后随 main 构建到来）",
        )
        # 工作区回退到当前 vendor 基线，保证 verify_vendor 的比对语义成立
        code, err = _git(["checkout", old_standalone])
        if code != 0:
            raise SystemExit(f"基线检出失败（本地缺 {old_short} 对象）：{err}")
        result = subprocess.run(
            ["python3", "scripts/run_injection.py"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise SystemExit(f"两层注入失败：{result.stderr.strip()}")
        print(result.stdout.strip())
        result = subprocess.run(
            ["python3", "scripts/verify_vendor.py"], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise SystemExit(f"vendor 三层校验失败：{result.stderr.strip()}")
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state["last_skipped_build"] = {
            "sha": new_standalone,
            "version": new_version,
            "main_sha": new_main,
            "at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        }
        _state_write(state)
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if result.stdout.strip():
            print("平面/state 变更保留在工作树（未提交）；vendor 基线未变")
        return

    old_version = json.loads(STATE_PATH.read_text(encoding="utf-8"))["upstream_version"]
    if old_version.split("-")[0] != new_version.split("-")[0]:
        print(f"版本段变更：{old_version} → {new_version}——上游进入新版本周期")
    _release_advisory(old_version, new_version)

    old_short, new_short = old_standalone[:12], new_standalone[:12]

    _rebuild_vendor(new_standalone)

    result = subprocess.run(
        ["python3", "scripts/derive_requirements.py"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"派生 requirements 失败：{result.stderr.strip()}")
    print(result.stdout.strip())

    result = subprocess.run(["python3", "scripts/run_injection.py"], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"两层注入失败：{result.stderr.strip()}")
    print(result.stdout.strip())

    result = subprocess.run(["python3", "scripts/verify_vendor.py"], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"vendor 三层校验失败：{result.stderr.strip()}")
    print(result.stdout.strip())

    version = tomllib.loads(
        (VENDOR_META / "pyproject.toml").read_text(encoding="utf-8"),
    )["project"]["version"]

    # pytest 必须与门禁 1 完全同参（-c config/pyproject.toml --rootdir=.）：
    # 裸 `-q` 落到 rootdir 自动发现、拿不到仓库 pytest 配置，asyncio 用例
    # 整批假红，管线红必须只反映
    # 契约本身。
    result = subprocess.run(
        ["python3", "-m", "pytest", "-c", "config/pyproject.toml", "--rootdir=.", "-q"],
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "")
    if result.returncode != 0:
        raise SystemExit(f"roll 后契约测试失败：\n{result.stdout[-2000:]}{result.stderr[-500:]}")

    # state 是「已成功同步」的凭据而非「已尝试」的日志，故推进严格晚于契约测试绿：
    # 先推进会让失败 roll 被永久记为已同步（下次 _detect 见 standalone_sha 相同
    # 直接幂等退出，vendor 滞留旧版且再无重试触发点），且 consecutive_failures
    # 被清零后由 main() +1 恒为 1、永达不到熔断阈值。失败路径只由 main() 递增
    # failures、不动 sha。写入须先于 git add（state 是提交清单成员），故 add/
    # commit 失败时回滚原文，保证「state 已推进」与「roll 已提交」同真同假。
    previous_state = STATE_PATH.read_text(encoding="utf-8")
    state = json.loads(previous_state)
    state.update(
        upstream_version=version,
        main_sha=new_main,
        standalone_sha=new_standalone,
        synced_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        consecutive_failures=0,
    )
    _state_write(state)

    result = subprocess.run(["git", "add", *ROLL_ADD_PATHS], capture_output=True, text=True)
    if result.returncode != 0:
        STATE_PATH.write_text(previous_state, encoding="utf-8")
        raise SystemExit(f"git add 失败：{result.stderr.strip()}")
    result = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"sync: roll parser-lite standalone {old_short}..{new_short} (本地 roll)",
            "-m",
            f"上游版本 {version}；standalone 构建 main_sha {new_main}。"
            "由 scripts/roll_local.py 执行（与 sync-upstream 工作流同序列：整树重建"
            " vendor + 派生 requirements + 两层注入 + 三层校验 + 全量契约测试）。",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        STATE_PATH.write_text(previous_state, encoding="utf-8")
        raise SystemExit(f"roll 提交失败：{result.stderr.strip()}")
    print(f"roll 完成：{old_short}..{new_short}（v{version}），已提交")


def main(argv: list[str] | None = None) -> int:
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description="本地一键滚动同步（工作流同序列）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检测上游变化，不执行 roll（检测到更新时退出码 1，无更新 0）",
    )
    args = parser.parse_args(argv)

    _ensure_clone()
    new_standalone, new_main, old_standalone = _detect()
    if new_standalone == old_standalone:
        print(f"上游无变化：standalone 停在 {old_standalone[:12]}")
        return 0
    print(f"检测到上游滚动：{old_standalone[:12]} → {new_standalone[:12]}（main {new_main[:12]}）")
    if args.check:
        return 1
    try:
        _roll(new_standalone, new_main, old_standalone)
    except (SystemExit, Exception):
        # 覆盖 SystemExit 之外的类型：_roll 里 copytree / tomllib 在上游树结构
        # 变化时会抛 OSError / TOMLDecodeError，若任其冒泡则失败计数不增长
        # → 本地熔断低估失败次数。刻意不含 KeyboardInterrupt
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        _state_write(state)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
