"""第三轮工作链：补丁×下游消费 / 插件生命周期 / 并发解析隔离 / 渲染链。

全部离线可跑（宿主 + 容器）：
- 渲染链用 VideoContent(cover=None)（hupu 补丁新引入的取值分布）：vendor
  模板对视频只调 safe_src("get_cover_path")，None 走 PLACEHOLDER 降级，
  不触发媒体下载；
- 并发解析隔离走 result cache 预置 sentinel（与 test_workchains 同模式），
  缓存命中短路网络。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from anyio import Path as AnyioPath
from astrbot_plugin_parser_lite.bridge import ssrf, vendor_patches
from astrbot_plugin_parser_lite.bridge.render import PLACEHOLDER_IMAGE, build_html
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import (
    configure,
    pipeline,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    Author,
    Comment,
    ParseResult,
    Platform,
    PlatformEnum,
    VideoContent,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.helper import UniHelper
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.hupu import (
    util as hupu_util,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.kuwo import (
    KuWoParser,
)

PLATFORM = Platform(name=PlatformEnum.HUPU, display_name="虎扑")


@pytest.fixture(autouse=True)
def _patches_and_state() -> Iterator[None]:
    vendor_patches.apply_vendor_patches()
    ssrf.install_ssrf_guard()
    saved: dict[str, Any] = pconfig.model_dump()
    yield
    configure(**saved)
    pipeline.clear_result_cache()


def _hupu_video_without_cover() -> VideoContent:
    items = hupu_util.parse_rich_content('<video src="https://example.com/v.mp4"></video>')
    return next(i for i in items if isinstance(i, VideoContent))


# ---------------------------------------------------------------- 补丁×SSRF


async def test_patched_kuwo_handler_keeps_ssrf_pinning() -> None:
    """补丁替换 handler 后，新建 parser 的 API 客户端仍走 SSRF 钉扎。"""
    parser = KuWoParser()
    try:
        assert isinstance(parser.httpx._transport, ssrf._PinnedTransport)
        assert getattr(type(parser), "_ssrf_guarded", False)
    finally:
        await parser.aclose()


# ---------------------------------------------------------------- cover=None 消费链


async def test_hupu_cover_none_flows_through_media_chain() -> None:
    """hupu 无封面视频的 cover=None 贯穿 vendor 媒体链（sender video_seg
    与 mediafile_to_comp 的 thumbnail 分支均以 None 安全处理）。"""
    video = _hupu_video_without_cover()
    assert video.cover is None
    assert await video.get_cover_path() is None

    seg = await UniHelper.video_seg(
        AnyioPath("/tmp/parser-lite-fake.mp4"),
        thumbnail=await video.get_cover_path(),
    )
    assert seg.kind == "video"
    assert seg.thumbnail is None


# ---------------------------------------------------------------- 渲染链


async def test_render_chain_with_patched_video_repost_and_qrcode() -> None:
    """补丁产出 + 转发递归 + 二维码 + max_comments 运行期求值，一次贯通。"""
    configure(plite_append_qrcode=True, plite_max_comments=1)
    video = _hupu_video_without_cover()
    result = ParseResult(
        platform=PLATFORM,
        author=Author(name="作者"),
        url="https://bbs.hupu.com/123",
        content=["前文", video, "尾文"],
        title="标题",
        comments=[
            Comment(author=Author(name="评论者甲"), content=["好帖"], timestamp=0),
            Comment(author=Author(name="评论者乙"), content=["顶"], timestamp=0),
        ],
        repost=ParseResult(
            platform=PLATFORM,
            author=Author(name="原主"),
            url="https://bbs.hupu.com/456",
            content=["转发文本"],
            title="转发帖",
        ),
    )

    html = await build_html(result, "light")

    assert "标题" in html and "前文" in html and "尾文" in html
    assert "转发帖" in html and "转发文本" in html
    # append_qrcode 运行期生效：二维码 data URI 已内联
    assert "data:image/png;base64," in html
    # cover=None 走占位降级而非崩溃（补丁引入的取值分布）
    assert PLACEHOLDER_IMAGE in html
    # max_comments 运行期求值：只渲染 1 条评论
    assert "评论者甲" in html
    assert "评论者乙" not in html


# ---------------------------------------------------------------- 生命周期


async def test_parser_aclose_then_new_instance_still_works() -> None:
    """AstrBot 保存配置会重载插件实例：旧实例 aclose 后新实例完整可用。"""
    old = pipeline.Parser()
    await old.aclose()

    fresh = pipeline.Parser()
    try:
        _keyword, mwp = KuWoParser.search_url("https://www.kuwo.cn/play_detail/51685512")
        assert mwp.url
    finally:
        await fresh.aclose()


async def test_concurrent_parse_cache_isolation() -> None:
    """同一 Parser 并发解析多平台 URL：result cache 按平台 cache_key 隔离，
    各请求取回各自的缓存对象，无串扰（缓存命中短路网络）。"""
    parser = pipeline.Parser()
    urls = [
        "https://www.bilibili.com/video/BV1x3411c7eK",
        "https://www.bilibili.com/video/BV1GJ411x7h7",
        "https://www.kuwo.cn/play_detail/51685512",
    ]
    sentinels = [object(), object(), object()]
    for url, sentinel in zip(urls, sentinels, strict=True):
        matched = parser.match(url)
        pipeline._RESULT_CACHE[matched.cache_key] = sentinel

    got = await asyncio.gather(*(parser.parse(url) for url in urls))
    assert list(got) == sentinels
