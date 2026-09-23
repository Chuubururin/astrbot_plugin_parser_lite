"""vendor_patches 桥内补丁：挂载契约 + 行为哨兵 + 行为回归。

四类测试：
1. 挂载契约（M5）：每个挂载点承载的函数 __module__ 必须指向 vendor_patches
   （而不是上游模块）；锚点缺失/形态不符必须响亮失败（RuntimeError）——
   模块属性 setattr 永远成功，上游改名时补丁会静默失效，本类用例先红；
2. 行为哨兵：直接调用挂载前留存的上游原件，用构造的 HTML 验证缺陷仍在
   （视频块之后的正文仍丢失）——上游修复后本类用例翻红，提醒按哨兵约定
   撤销补丁；旧版「源码里还有某字符串」的哨兵已删除（改名后字符串仍在，
   测不出补丁失效）；
3. 行为回归：调用补丁后的函数，验证视频块之后的正文未丢失（buff/hupu
   各一条）、video 缺属性不再产出字面 "None" URL、music_id 无空格；
4. 幂等与绑定：apply_vendor_patches 可重复调用；hupu bbs/comment 的
   from .util import 绑定自动走到修复版。

kuwo：上游 #307（740e6c7）已修复 ``music_id`` 尾随空格（端点路径同版
修正），对应补丁已按哨兵约定移除；``music_id`` 无空格与请求行为保留为
vendor 冒烟，防上游回退。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from anyio import Path as AnyioPath
from astrbot_plugin_parser_lite.bridge import ssrf, vendor_patches
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    GraphicContent,
    VideoContent,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.buff import (
    news as buff_news,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.buff.share import (
    ShareData,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.hupu import (
    util as hupu_util,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.kuwo import (
    KuWoParser,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils import (
    ffmpeg as ffmpeg_mod,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.ffmpeg import (
    FFmpeg,
)
from bs4 import BeautifulSoup

VENDOR_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "nonebot_plugin_parser_lite"

# 补丁在收集期生效（main.py 实例化时同样调用）；幂等，重复调用无害
vendor_patches.apply_vendor_patches()


def _vendor_src(rel: str) -> str:
    return (VENDOR_ROOT / rel).read_text(encoding="utf-8")


def _strs(items: list[Any]) -> list[str]:
    return [i for i in items if isinstance(i, str)]


# ---------------------------------------------------------------- 挂载契约（M5）


def test_contract_kuwo_upstream_fix_stays() -> None:
    """反向哨兵：上游 #307 已修复 music_id 尾随空格，此处防回退。

    （原为补丁契约断言缺陷仍在；上游修复→补丁按约定移除→断言翻转。）
    """
    assert '"music_id "' not in _vendor_src("parsers/kuwo.py")


def test_mount_points_are_bridge_owned() -> None:
    """每个挂载点承载的函数必须来自 vendor_patches（不是上游模块）。

    模块属性 setattr 永远成功，「补丁没挂上去」在运行期毫无声息（2026-09-17
    评审 M5）——本用例把「补丁归属」变成可机械核验的事实：上游改名/形态
    变化时挂载即响亮失败，本用例连同收集期报错一起变红。
    """
    points = vendor_patches.mounted_points()
    assert {qualname for _, qualname in points} == {
        "News.content",
        "_iter_media_and_text",
        "FFmpeg.download_hls_to_mp4",
    }
    for module_name, qualname in points:
        current = vendor_patches.resolve_mount_point(module_name, qualname)
        func = vendor_patches.bridge_function(current)
        assert func is not None, f"挂载点 {module_name}.{qualname} 不可解析"
        assert func.__module__ == vendor_patches.__name__, (
            f"挂载点 {module_name}.{qualname} 未指向桥实现（实际 {func.__module__}）"
        )
        assert func.__module__ != module_name, "挂载点仍指向上游模块，补丁未生效"
        assert "<locals>" in func.__qualname__, (
            f"挂载点 {module_name}.{qualname} 的 __qualname__ 不是桥内局部函数：{func.__qualname__}"
        )


def test_mount_raises_loudly_when_anchor_renamed() -> None:
    """锚点不存在即 RuntimeError（M5 核心）：复现「上游把函数改名」。"""
    with pytest.raises(RuntimeError, match="不存在"):
        vendor_patches.resolve_mount_point(hupu_util.__name__, "_iter_media_and_text_renamed")


def test_mount_raises_loudly_when_shape_mismatches() -> None:
    """锚点存在但形态不符（如 property 变普通方法）同样响亮失败，且不改写现场。"""
    before = hupu_util._iter_media_and_text
    with pytest.raises(RuntimeError, match="形态不符"):
        vendor_patches._mount(
            hupu_util, "_iter_media_and_text", lambda *a, **k: None, expect="property"
        )
    assert hupu_util._iter_media_and_text is before, "形态断言失败后仍改写了挂载点"


# ---------------------------------------------------------------- 行为哨兵

_BUFF_VIDEO_BODY = (
    "<p>前文</p>"
    '<div class="video-content" data-src="https://example.com/v.mp4">'
    '<img src="https://example.com/c.jpg"/></div>'
    "<p>后文</p>"
    '<img data-original="https://example.com/i.jpg"/>'
    "<p>尾文</p>"
)

# 视频块带子节点（source + 内部 img）才是上游缺陷的触发形状：空 <video>
# 下 decompose 不破坏迭代，旧用例因此是假绿（实测确认）
_HUPU_VIDEO_HTML = (
    "<p>前文</p>"
    '<video src="https://example.com/v.mp4?token=x" poster="https://example.com/p.jpg">'
    '<source src="https://example.com/v.mp4"/>'
    '<img src="https://example.com/inner.jpg"/></video>'
    "<p>后文</p>"
    '<img data-src="https://example.com/i.jpg"/>'
    "<p>尾文</p>"
)


def test_sentinel_buff_upstream_still_loses_text_after_video() -> None:
    """行为哨兵（buff）：上游原件仍在视频块处截断迭代。

    断言的是上游缺陷本身——上游修复后本用例翻红，提示按约定撤销补丁。
    """
    upstream = vendor_patches.VENDOR_BUFF_CONTENT
    assert isinstance(upstream, property) and upstream.fget is not None
    items = upstream.fget(_make_news(_BUFF_VIDEO_BODY))
    assert _strs(items) == ["前文"], "上游 buff 截断行为已变：请复检补丁必要性"
    assert not [i for i in items if isinstance(i, GraphicContent)]


def test_sentinel_hupu_upstream_still_truncates_after_video() -> None:
    """行为哨兵（hupu）：上游原件在视频块含子节点时截断后续内容。"""
    upstream = vendor_patches.VENDOR_HUPU_ITER
    assert upstream is not None
    raw = list(upstream(BeautifulSoup(_HUPU_VIDEO_HTML, "html.parser")))
    texts = _strs(raw)
    assert "后文" not in texts and "尾文" not in texts, "上游 hupu 截断行为已变：请复检补丁必要性"
    assert not [i for i in raw if isinstance(i, GraphicContent)]


# ---------------------------------------------------------------- kuwo
# （补丁已随上游 #307 修复移除，以下保留为 vendor 冒烟防回退）


_KUWO_API_RESPONSE = {
    "code": 200,
    "data": {
        "download_url": "https://example.com/song.mp3",
        "duration_seconds": 12,
        "cover": "",
        "quality": {"name": "320kbps"},
        "album": "专辑",
        "lyric": "歌词",
        "title": "歌名",
        "artist": "歌手",
        "artist_pic": "https://example.com/pic.jpg",
    },
}


class _FakeKuwoResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return _KUWO_API_RESPONSE


async def test_kuwo_request_uses_clean_music_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_get(url: str, **kwargs: Any) -> _FakeKuwoResponse:
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return _FakeKuwoResponse()

    parser = KuWoParser()
    monkeypatch.setattr(parser.httpx, "get", fake_get)
    try:
        keyword, mwp = KuWoParser.search_url("https://www.kuwo.cn/play_detail/51685512")
        result = await parser.parse(keyword, mwp)
    finally:
        await parser.aclose()

    assert captured["params"] == {"music_id": "51685512", "quality": "320k"}
    assert result.title == "歌名"


# ---------------------------------------------------------------- buff


def _make_news(body: str) -> buff_news.News:
    return buff_news.News(
        author="作者",
        user_id="1",
        avatar="",
        body=body,
        ip_location="",
        publish_time=0,
        replies=0,
        title="标题",
        ups_num=0,
        views=0,
        share_data=ShareData(title="分享", url="https://buff.163.com/x"),
    )


def test_buff_news_content_keeps_content_after_video() -> None:
    news = _make_news(_BUFF_VIDEO_BODY)

    items = news.content
    videos = [i for i in items if isinstance(i, VideoContent)]
    graphics = [i for i in items if isinstance(i, GraphicContent)]

    # 视频块之后的「后文 / 尾文」不再静默丢失
    assert _strs(items) == ["前文", "后文", "尾文"]
    assert len(videos) == 1
    assert videos[0].path_task.url == "https://example.com/v.mp4"
    assert videos[0].cover is not None
    assert videos[0].cover.url == "https://example.com/c.jpg"
    assert len(graphics) == 1
    assert graphics[0].path_task.url == "https://example.com/i.jpg"


def test_buff_news_content_without_video_unchanged() -> None:
    news = _make_news("<p>纯文本</p><img data-original='https://example.com/i.jpg'/>")
    items = news.content
    assert _strs(items) == ["纯文本"]
    graphics = [i for i in items if isinstance(i, GraphicContent)]
    assert len(graphics) == 1


# ---------------------------------------------------------------- hupu


def test_hupu_rich_content_keeps_content_after_video() -> None:
    items = hupu_util.parse_rich_content(_HUPU_VIDEO_HTML)
    videos = [i for i in items if isinstance(i, VideoContent)]
    graphics = [i for i in items if isinstance(i, GraphicContent)]

    assert _strs(items) == ["前文", "后文", "尾文"]
    assert len(videos) == 1
    assert videos[0].path_task.url == "https://example.com/v.mp4?token=x"
    assert videos[0].cover is not None
    assert videos[0].cover.url == "https://example.com/p.jpg"
    # 视频内部 img 不再被当作普通图（保留上游语义），只剩视频块之后的图
    assert len(graphics) == 1
    assert graphics[0].path_task.url == "https://example.com/i.jpg"


def test_hupu_video_with_text_child_does_not_crash() -> None:
    """上游在「video 含文本子节点」形状下会抛 AttributeError（bs4 迭代断链），
    补丁须不崩且保留后文。"""
    items = hupu_util.parse_rich_content(
        '<video src="https://example.com/v.mp4">caption</video><p>后文</p>'
    )
    assert "后文" in _strs(items)


def test_hupu_video_without_poster_has_no_cover() -> None:
    items = hupu_util.parse_rich_content('<video src="https://example.com/v.mp4"></video>')
    videos = [i for i in items if isinstance(i, VideoContent)]
    assert len(videos) == 1
    assert videos[0].cover is None


def test_hupu_video_without_src_is_skipped() -> None:
    items = hupu_util.parse_rich_content(
        '<video poster="https://example.com/p.jpg"></video><p>正文</p>'
    )
    assert not [i for i in items if isinstance(i, VideoContent)]
    assert "正文" in _strs(items)


def test_hupu_bbs_and_comment_bindings_reach_patched_iter() -> None:
    """bbs/comment 以 from .util import 直接绑定 parse_rich_content；
    补丁替换 util 模块属性后既有绑定应自动走到修复版。"""
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.hupu import (
        bbs as hupu_bbs,
    )
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.hupu import (
        comment as hupu_comment,
    )

    assert hupu_bbs.parse_rich_content is hupu_util.parse_rich_content
    assert hupu_comment.parse_rich_content is hupu_util.parse_rich_content


# ---------------------------------------------------------------- 幂等


def test_apply_vendor_patches_is_idempotent() -> None:
    vendor_patches.apply_vendor_patches()
    after_first = vendor_patches.mounted_points()
    content = buff_news.News.content
    it = hupu_util._iter_media_and_text
    vendor_patches.apply_vendor_patches()
    # 挂载点清单不得因二次调用重复追加；承载对象引用不变（不得被二次挂载覆盖）
    assert vendor_patches.mounted_points() == after_first
    assert buff_news.News.content is content
    assert hupu_util._iter_media_and_text is it


# ---------------------------------------------------------------- ffmpeg HLS SSRF


def test_ffmpeg_hls_blocks_loopback_url(tmp_path: Path) -> None:
    """ffmpeg 子进程出站通道纳入 SSRF 校验：环回地址在入口即被拒。"""
    from astrbot_plugin_parser_lite.bridge.ssrf import UrlBlockedError

    vendor_patches.apply_vendor_patches()
    with pytest.raises(UrlBlockedError):
        asyncio.run(FFmpeg.download_hls_to_mp4("http://127.0.0.1/x.m3u8", tmp_path / "a.mp4"))


def test_ffmpeg_hls_guard_is_applied_once() -> None:
    vendor_patches.apply_vendor_patches()
    guarded = FFmpeg.download_hls_to_mp4.__func__
    assert getattr(guarded, "_ssrf_guarded", False)
    vendor_patches.apply_vendor_patches()
    assert FFmpeg.download_hls_to_mp4.__func__ is guarded


def test_ffmpeg_vendor_signature_contract() -> None:
    """vendor 签名契约（M6）：上游改 download_hls_to_mp4 形参即红。

    守卫用 *args/**kwargs 透传，因此上游签名是唯一真相源；改签名需同步复核
    本契约与守卫的 url 位置参数假设。
    """
    assert vendor_patches.VENDOR_HLS_PARAMS == (
        "url",
        "output_path",
        "headers",
        "max_size_mb",
    )


def test_ffmpeg_hls_guard_forwards_args_to_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SSRF 守卫放行后必须把整套参数正确转交原始实现（成功路径回归）。

    历史缺陷：包装器以 ``__func__`` 取出未绑定 classmethod（签名含 cls）后
    按 (url, output_path, ...) 调用，参数整体错位一位、必然 TypeError——
    被 vendor 调用点的 except Exception 吞成 DownloadException，表现为
    所有 HLS 视频静默下载失败。此用例桩掉真实 ffmpeg 子进程，只钉参数
    转发，是原先缺失的 happy path 覆盖。
    """
    vendor_patches.apply_vendor_patches()
    # 校验层放行（其余用例证明它确实会拦），聚焦纯转发
    monkeypatch.setattr(ssrf, "validate_url", lambda url: url)

    captured: dict[str, Any] = {}

    async def fake_monitored(
        cls: type[FFmpeg], cmd: list[str], *, output_path: AnyioPath, max_size_mb: int
    ) -> AnyioPath:
        captured["cls"] = cls
        captured["cmd"] = cmd
        captured["max_size_mb"] = max_size_mb
        # 复刻真实子进程副作用：产出输出文件供 replace 使用
        await output_path.write_bytes(b"stub-mp4")
        return output_path

    monkeypatch.setattr(ffmpeg_mod.FFmpeg, "exec_ffmpeg_monitored", classmethod(fake_monitored))

    out = AnyioPath(str(tmp_path / "out.mp4"))
    result = asyncio.run(
        FFmpeg.download_hls_to_mp4(
            "https://cdn.example.com/x.m3u8",
            out,
            headers={"Referer": "https://example.com"},
            max_size_mb=42,
        )
    )

    assert result == out, "HLS 下载成功路径未返回输出路径（参数错位）"
    assert captured["cls"] is ffmpeg_mod.FFmpeg, "cls 未正确绑定到 FFmpeg"
    assert captured["max_size_mb"] == 42, "max_size_mb 上限未转发"
    cmd = captured["cmd"]
    assert "https://cdn.example.com/x.m3u8" in cmd, "URL 未传入 ffmpeg 命令"
    assert "-headers" in cmd, "自定义头未转发"
    header_idx = cmd.index("-headers")
    assert "Referer: https://example.com" in cmd[header_idx + 1], "自定义头内容未转发"


async def test_ffmpeg_hls_validate_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate_url 内含阻塞的 socket.getaddrinfo，必须在工作线程执行（M6）。

    同步调用会冻结整个 AstrBot 事件循环（DNS 慢/黑洞时最明显），
    ssrf.py 自己注明「放线程池执行避免卡死事件循环」。
    """
    vendor_patches.apply_vendor_patches()
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def fake_validate(url: str) -> str:
        seen["thread"] = threading.get_ident()
        return url

    async def fake_monitored(
        cls: type[FFmpeg], cmd: list[str], *, output_path: AnyioPath, max_size_mb: int
    ) -> AnyioPath:
        await output_path.write_bytes(b"stub-mp4")
        return output_path

    monkeypatch.setattr(ssrf, "validate_url", fake_validate)
    monkeypatch.setattr(ffmpeg_mod.FFmpeg, "exec_ffmpeg_monitored", classmethod(fake_monitored))

    out = AnyioPath(str(tmp_path / "off-loop.mp4"))
    await FFmpeg.download_hls_to_mp4("https://cdn.example.com/x.m3u8", out)

    assert seen, "守卫未调用 ssrf.validate_url"
    assert seen["thread"] != loop_thread, "validate_url 在事件循环线程内执行，DNS 会卡死循环"


async def test_hls_guard_transparently_forwards_new_upstream_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游给 download_hls_to_mp4 新增带默认值的形参时不得被守卫静默丢弃（M6）。

    固定签名转发下该形参会消失，且必填才 TypeError、还会被 vendor 调用点的
    except Exception 吞成 DownloadException——本用例用「升级版」原实现复现
    上游新增形参，直接断言它到达了原实现。
    """
    monkeypatch.setattr(ssrf, "validate_url", lambda url: url)
    seen: dict[str, Any] = {}

    async def upgraded(
        url: str,
        output_path: AnyioPath,
        headers: dict[str, str] | None = None,
        max_size_mb: int = 90,
        *,
        new_flag: bool = True,
    ) -> AnyioPath:
        seen["new_flag"] = new_flag
        seen["url"] = url
        seen["headers"] = headers
        return output_path

    guard = vendor_patches._build_hls_guard(upgraded, ssrf)
    out = AnyioPath(str(tmp_path / "new-param.mp4"))
    # 直接调用未绑定守卫：首位是 cls（挂载为 classmethod 时由描述符绑定）
    await guard(None, "https://cdn.example.com/x.m3u8", out, new_flag=False)

    assert seen == {
        "new_flag": False,
        "url": "https://cdn.example.com/x.m3u8",
        "headers": None,
    }, "上游新增形参被守卫静默丢弃"
