"""最终消息结构对比工装：上游 UniMessage 组装 vs 桥 sender 组件链。

「最终发送的消息」对比固化为可重跑 harness（运维锚点）。
三个子命令，两侧各在对应容器内经 stdin 运行（源码不入容器）：

1. 捕获（同一来源跑两遍）::

     nonebot 容器:  <venv python> message_structure_probe.py fixture \\
         --side upstream --out /tmp/up.json
     astrbot 容器:  python message_structure_probe.py fixture \\
         --side bridge --out /tmp/br.json

   ``fixture`` 模式按 profile 构造合成 ParseResult（零网络确定性）：

   - ``forward``（生产默认 need_forward_contents=True 的主路径）：标题/
     文本/投票/引用/链接/图片/图集（alt）+ 转发嵌套，含 REPOST_MARKER；
   - ``flat``（临时置 need_forward_contents=False 的次路径）：标题+文本+
     图片的平铺发送。

   媒体用桩 DownloadTaskWrapper（异步桩返回临时 PNG，零网络）。``url
   <URL>`` 模式活体解析真实分享链接（含真实媒体下载）。
2. 对照（宿主即可）::

     python message_structure_probe.py compare /tmp/up.json /tmp/br.json

   按 profile 配对逐组判定：forward_segs 严格相等，media 消息按等价类
   （上游 alconna Reference ≡ 桥 Comp.Nodes ≡ forward；上游「图+alt」
   复合段 ≡ 桥 _AltMedia），文本逐字比对，媒体文件名仅报告（跨容器缓存
   名不同属预期）。bot 名/uin 为配置面，不参判。有差异退出码 1。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# 1x1 透明 PNG：桩媒体文件内容（仅要求存在与非空，不要求可解码）
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)
_MEDIA_DIR = Path(tempfile.gettempdir()) / "plite-probe-media"
_FIXTURE_MEDIA_NAMES = ("fixture-cover.png", "fixture-graphic.png")

_VENDOR_DATA: Any = None


def _vendor_data() -> Any:
    """vendor 数据类按运行环境三态解析（字节同源，类签名一致）：

    astrbot 容器 → 桥 vendor；zhenxun/上游 venv → 上游包本体；宿主开发
    环境 → 仓库包（父目录入 sys.path）。三处 data.py 逐字节相同，合成
    夹具与归一化因此跨环境等价。
    """
    global _VENDOR_DATA
    if _VENDOR_DATA is None:
        import importlib

        attempts: list[tuple[str, str | None]] = [
            (
                "astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data",
                "/AstrBot/data/plugins",
            ),
            ("nonebot_plugin_parser_lite.data", None),
        ]
        for module_name, path in attempts:
            try:
                if path and path not in sys.path:
                    sys.path.insert(0, path)
                _VENDOR_DATA = importlib.import_module(module_name)
                return _VENDOR_DATA
            except ModuleNotFoundError:
                continue
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        _VENDOR_DATA = importlib.import_module(
            "astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data",
        )
    return _VENDOR_DATA


def _download_wrapper(url: str) -> Any:
    """桩下载包装器：await 后返回预置临时 PNG 的 anyio.Path（惰性，零网络）。

    必须是 anyio.Path——上游 main 默认 use_base64=True，img_seg 走
    ``await file.read_bytes()``，pathlib.Path 会 TypeError。
    """
    from anyio import Path as AnyioPath

    data = _vendor_data()
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    stub = _MEDIA_DIR / name
    if not stub.exists():
        stub.write_bytes(_TINY_PNG)
    target = AnyioPath(stub)

    async def _get() -> Any:
        return target

    return data.DownloadTaskWrapper(_get, (), {}, url)


# ---- 合成夹具（两侧共用同一 vendor 数据类，零网络确定性） ----


def _fixture_result(profile: str) -> Any:
    """按 profile 构造覆盖 send_content 纯数据分支的合成 ParseResult。"""
    data = _vendor_data()
    platform = data.Platform(name=data.PlatformEnum.BILIBILI, display_name="哔哩哔哩")

    if profile == "flat":
        return data.ParseResult(
            platform=platform,
            author=data.Author(name="样例UP主", id="42"),
            url="https://www.bilibili.com/video/BV1FIXTUREFLAT",
            content=[
                "主帖正文文本",
                data.ImageContent(_download_wrapper("https://fixture.invalid/fixture-cover.png")),
            ],
            title="短标题",
        )

    poll = data.PollContent(
        options=[
            data.PollOption(text="选项甲", votes=12),
            data.PollOption(text="选项乙", votes=30),
        ],
        title="周末去哪",
        total_voters=40,
        multiple=False,
        closed=False,
    )
    quote = data.QuoteContent(text="引用正文样例", title="来源帖", url="https://example.com/q")
    link = data.LinkContent(url="https://example.com/a", title="链接卡标题", site_name="示例站")
    image = data.ImageContent(_download_wrapper("https://fixture.invalid/fixture-cover.png"))
    graphic = data.GraphicContent(
        _download_wrapper("https://fixture.invalid/fixture-graphic.png"),
        alt="图集说明文字",
    )
    repost = data.ParseResult(
        platform=platform,
        author=data.Author(name="被转发UP"),
        url="https://www.bilibili.com/video/BV1FIXTURE2",
        content=["转发体正文第一段", "转发体正文第二段"],
        title="被转发的标题",
    )
    return data.ParseResult(
        platform=platform,
        author=data.Author(name="样例UP主", id="42"),
        url="https://www.bilibili.com/video/BV1FIXTURE1",
        content=["主帖正文段落", poll, quote, link, image, graphic],
        title="主帖标题（转发应含）",
        repost=repost,
    )


FIXTURE_PROFILES = ("forward", "flat")


# ---- 归一化（纯函数，宿主可测） ----

# send_content 最终消息的等价类：上游 alconna Reference ≡ 桥 Comp.Nodes
_FORWARD_EQUIV = frozenset({"reference", "nodes"})
# 文本段的等价类：上游 alconna Text ≡ 桥 Comp.Plain（相邻段拼接后对照，
# 上游 UniMessage(list) 会合并相邻字符串而桥保持逐段）
_TEXT_EQUIV = frozenset({"text", "plain"})


def _norm_fwd_seg(seg: Any) -> dict[str, Any]:
    name = type(seg).__name__
    if name.endswith("_ForwardText"):
        return {
            "kind": "ftext",
            "text": getattr(seg, "text", ""),
            "include_author": bool(getattr(seg, "include_author", False)),
        }
    if name == "_AltMedia":
        media = getattr(seg, "media", None)
        return {
            "kind": "alt",
            "media_kind": getattr(media, "kind", ""),
            "alt": getattr(seg, "alt", ""),
        }
    if name == "UniMessage":
        # 上游图集表示：img_seg(path) + alt 相加产出 UniMessage 复合段
        # （[Image, Text(alt)]）≡ 桥 _AltMedia（media + alt 成对）
        parts = list(seg)
        media_kind = type(parts[0]).__name__.lower() if parts else ""
        alt = getattr(parts[1], "text", "") if len(parts) > 1 else ""
        return {"kind": "alt", "media_kind": media_kind, "alt": alt}
    if isinstance(seg, str):
        return {"kind": "text", "text": seg}
    kind = getattr(seg, "kind", None) or name.lower()
    return {"kind": str(kind)}


def normalize_forward_segs(segs: list[Any]) -> list[dict[str, Any]]:
    return [_norm_fwd_seg(s) for s in segs]


def _norm_message_chain(chain: list[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for seg in chain:
        name = type(seg).__name__.lower()
        if name in _FORWARD_EQUIV:
            merged.append({"kind": "forward"})
            continue
        text = getattr(seg, "text", None)
        entry: dict[str, Any] = {"kind": name}
        if isinstance(text, str):
            entry["text"] = text
        # 相邻文本段拼接（上游 UniMessage(list) 合并相邻字符串，桥逐段 Plain）
        if merged and entry["kind"] in _TEXT_EQUIV and merged[-1]["kind"] in _TEXT_EQUIV:
            merged[-1]["text"] = merged[-1].get("text", "") + entry.get("text", "")
            continue
        merged.append(entry)
    for entry in merged:
        if entry["kind"] in _TEXT_EQUIV:
            entry["kind"] = "text"
    return merged


def normalize_messages(messages: list[list[Any]]) -> list[list[dict[str, Any]]]:
    return [_norm_message_chain(m) for m in messages]


# ---- 对照（纯函数，宿主可测） ----


def _compare_capture(up: dict[str, Any], br: dict[str, Any], label: str) -> list[str]:
    diffs: list[str] = []
    up_segs, br_segs = up["forward_segs"], br["forward_segs"]
    if up_segs != br_segs:
        diffs.append(
            f"{label} forward_segs 不等：\n  上游: "
            + json.dumps(up_segs, ensure_ascii=False)
            + "\n  桥:   "
            + json.dumps(br_segs, ensure_ascii=False)
        )
    up_msgs, br_msgs = up["media"], br["media"]
    if len(up_msgs) != len(br_msgs):
        diffs.append(f"{label} media 消息数不等：上游 {len(up_msgs)} vs 桥 {len(br_msgs)}")
    for i, (u, b) in enumerate(zip(up_msgs, br_msgs, strict=False)):
        if u != b:
            diffs.append(
                f"{label} media[{i}] 不等：\n  上游: "
                + json.dumps(u, ensure_ascii=False)
                + "\n  桥:   "
                + json.dumps(b, ensure_ascii=False)
            )
    return diffs


def compare(capture_up: dict[str, Any], capture_br: dict[str, Any]) -> list[str]:
    """按 profile 配对返回差异清单；空清单 = 结构等价。"""
    ups = {c["profile"]: c for c in capture_up["captures"]}
    brs = {c["profile"]: c for c in capture_br["captures"]}
    diffs: list[str] = []
    for name in sorted(set(ups) | set(brs)):
        if name not in ups or name not in brs:
            diffs.append(
                f"profile {name!r} 只在单侧捕获："
                f"上游={'有' if name in ups else '无'} 桥={'有' if name in brs else '无'}"
            )
            continue
        diffs.extend(_compare_capture(ups[name], brs[name], f"[{name}]"))
    return diffs


# ---- 捕获端（容器内执行；依赖各自运行栈，宿主不可测） ----


def _force_need_forward(value: bool) -> tuple[bool, str]:
    """临时改 need_forward_contents（进程内，不落盘）；失败时如实报告。

    公开名是无 setter 的 property，可写面为底层 plite 字段。
    """
    data = _vendor_data()
    try:
        import importlib

        module = importlib.import_module(data.__name__.rsplit(".", 1)[0] + ".config")
        pconfig = module.pconfig
        original = bool(pconfig.need_forward_contents)
        for name in ("plite_need_forward_contents", "need_forward_contents"):
            try:
                setattr(pconfig, name, value)
                return original, ""
            except Exception:
                continue
        return original, "need_forward_contents 不可改写，flat profile 退化为 forward"
    except Exception as exc:
        return value, f"need_forward_contents 不可改写（{exc!r}）"


async def _capture_upstream(
    side_args: argparse.Namespace,
    profiles: list[str],
) -> list[dict[str, Any]]:
    import nonebot

    nonebot.init(driver="~fastapi")
    nonebot.load_plugin("nonebot_plugin_parser_lite")

    class _FakeAdapter:
        def get_name(self) -> str:
            return "cqhttp"

    class _FakeBot:
        self_id = "10000"
        adapter = _FakeAdapter()

    from nonebot.matcher import current_bot

    current_bot.set(_FakeBot())

    from nonebot_plugin_parser_lite.config import pconfig
    from nonebot_plugin_parser_lite.render import RENDERER

    # 上游包须在 nonebot 引导完成后再导入/构夹具（包 __init__ 会拉起
    # htmlrender 插件，先于 init 导入必然失败）
    result_by_profile: dict[str, Any] = {}
    for profile in profiles:
        result_by_profile[profile] = (
            await _parse_upstream(side_args.url)
            if side_args.cmd == "url"
            else _fixture_result(profile)
        )

    captures: list[dict[str, Any]] = []
    for profile in profiles:
        result = result_by_profile[profile]
        original, note = _force_need_forward(False) if profile == "flat" else (None, "")
        try:
            fwd = normalize_forward_segs(await RENDERER._Renderer__build_forward_segs(result))
            media = [list(msg) async for msg in RENDERER.send_content(result)]
        finally:
            if original is not None:
                pconfig.plite_need_forward_contents = original
        captures.append(
            {
                "profile": profile,
                "note": note,
                "forward_segs": fwd,
                "media": normalize_messages(media),
            }
        )
    return captures


def _capture_bridge(side_args: argparse.Namespace, profiles: list[str]) -> list[dict[str, Any]]:
    if "/AstrBot/data/plugins" not in sys.path:
        sys.path.insert(0, "/AstrBot/data/plugins")
    os.environ.setdefault("PARSER_LITE_BASE_DIR", "/tmp/plite-probe-base")
    Path("/tmp/plite-probe-base").mkdir(parents=True, exist_ok=True)

    from astrbot_plugin_parser_lite.bridge import sender
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig

    class _FakeEvent:
        def get_self_id(self) -> str:
            return "10000"

    async def _run() -> list[dict[str, Any]]:
        result_by_profile: dict[str, Any] = {}
        for profile in profiles:
            result_by_profile[profile] = (
                await _parse_bridge(side_args.url)
                if side_args.cmd == "url"
                else _fixture_result(profile)
            )
        captures: list[dict[str, Any]] = []
        for profile in profiles:
            result = result_by_profile[profile]
            original, note = _force_need_forward(False) if profile == "flat" else (None, "")
            try:
                fwd = normalize_forward_segs(await sender._build_forward_segs(result))
                chains = [list(chain) async for chain in sender.send_result(result, _FakeEvent())]
            finally:
                if original is not None:
                    pconfig.plite_need_forward_contents = original
            captures.append(
                {
                    "profile": profile,
                    "note": note,
                    "forward_segs": fwd,
                    "media": normalize_messages(chains),
                }
            )
        return captures

    return asyncio.run(_run())


async def _parse_upstream(url: str) -> Any:
    from nonebot_plugin_parser_lite.constants import MatchWithParams
    from nonebot_plugin_parser_lite.parsers import load_enabled_parsers

    result = None
    for cls in load_enabled_parsers():
        for keyword, pattern, _rules in cls._key_patterns:
            m = pattern.search(url)
            if not m:
                continue
            result = await cls().parse(keyword, MatchWithParams(m))
            break
        if result is not None:
            break
    if result is None:
        raise SystemExit(f"无解析器匹配 {url}")
    return result


async def _parse_bridge(url: str) -> Any:
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Parser

    return await Parser().parse(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="最终消息结构对比工装（上游 vs 桥）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cap = sub.add_parser("fixture", help="合成结果捕获（零网络，双 profile）")
    p_cap.add_argument("--side", choices=("upstream", "bridge"), required=True)
    p_cap.add_argument("--out", default=None, help="产物 JSON 路径（--print-fixture 时可省）")
    p_cap.add_argument("--print-fixture", action="store_true", help="打印合成结果摘要后退出")

    p_url = sub.add_parser("url", help="活体 URL 捕获")
    p_url.add_argument("url")
    p_url.add_argument("--side", choices=("upstream", "bridge"), required=True)
    p_url.add_argument("--out", required=True)

    p_cmp = sub.add_parser("compare", help="对照两份捕获产物")
    p_cmp.add_argument("upstream_json")
    p_cmp.add_argument("bridge_json")

    args = parser.parse_args(argv)

    if args.cmd == "compare":
        up = json.loads(Path(args.upstream_json).read_text(encoding="utf-8"))
        br = json.loads(Path(args.bridge_json).read_text(encoding="utf-8"))
        diffs = compare(up, br)
        if diffs:
            print("消息结构对照失败：", *diffs, sep="\n")
            return 1
        counts = "、".join(
            f"{c['profile']} {len(c['forward_segs'])} 段/{len(c['media'])} 条"
            for c in up["captures"]
        )
        print(f"消息结构对照通过（{counts}）")
        return 0

    if args.cmd == "fixture" and args.print_fixture:
        for profile in FIXTURE_PROFILES:
            result = _fixture_result(profile)
            print(
                f"fixture[{profile}]:",
                result.title,
                "|",
                [type(c).__name__ for c in result.content],
                "| repost:",
                result.repost is not None,
            )
        return 0

    if args.out is None:
        parser.error("--out 必填（--print-fixture 时可省）")

    profiles = ["live"] if args.cmd == "url" else list(FIXTURE_PROFILES)
    if args.side == "upstream":
        captures = asyncio.run(_capture_upstream(args, profiles))
    else:
        captures = _capture_bridge(args, profiles)

    Path(args.out).write_text(
        json.dumps({"captures": captures}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(
        "CAPTURED",
        "side=" + args.side,
        "profiles=" + ",".join(c["profile"] for c in captures),
        "path=" + args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
