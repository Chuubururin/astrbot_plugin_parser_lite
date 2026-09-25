"""vendor 运行态缺陷的桥内注入补丁（vendor 零修改铁律下的唯一修法）。

上游 main 分支两处运行态缺陷未修复：

- parsers/buff/news.py：News.content 在 descendants 迭代中对视频块调
  decompose()，销毁子树断链导致迭代终止，视频块之后的正文静默丢失；
- parsers/hupu/util.py：_iter_media_and_text 同病（视频块含子节点时迭代
  断链截断，含文本子节点时 bs4 甚至抛 AttributeError），且 video 缺 src /
  poster 属性时 str(None) 产出字面 "None" URL。

kuwo ``music_id`` 参数尾随空格缺陷已由上游修复（#307 / 740e6c7），无对应
补丁；tests/test_vendor_patches.py 冒烟钉住「参数无空格」防上游回退。

修法遵循 bs4 官方惯例（Launchpad #2091118 的社区共识：先快照后修改）：
descendants 先 list 化，配 destroyed id 集合跳过已销毁子树成员，保留
上游「视频内部节点不再当作普通图处理」的语义而不截断后续内容。

另有桥内安全加固（超出上游姿态，与 ssrf.py 四层钉扎同策略）：HLS 视频
由 ffmpeg 子进程直连 URL 出站，不经过任何 Python HTTP 客户端，绕过
_PinnedTransport/_PinnedNetworkBackend——对 FFmpeg.download_hls_to_mp4
入口前置 ssrf.validate_url 校验（复用既有接口，拒绝经 vendor 调用点的
except Exception 包装为 DownloadException，走 sender 下载失败降级）。
局限：校验与 ffmpeg 实际解析存在 DNS TOCTOU 窗口（子进程无法钉扎 IP），
为子进程出站的行业尽力而为做法。

挂载契约：模块属性 setattr 永远成功，上游改名/改
形态时补丁会静默失效而源码字符串哨兵（字符串仍在）测不出——每次挂载前经
_mount 断言锚点存在且形态符合预期，否则响亮失败（RuntimeError）；实际
挂载点由 mounted_points() 记录，测试断言其承载函数 __module__ 指向本模块。
被复刻函数的「上游原件」在挂载前留存（VENDOR_BUFF_CONTENT /
VENDOR_HUPU_ITER / VENDOR_HLS_PARAMS），测试直接调用它们验证缺陷仍在
——上游修复后行为哨兵翻红，提醒按约定撤销补丁；上游改名则挂载即响亮失败。
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from anyio import to_thread

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

    from ..vendor.nonebot_plugin_parser_lite.data import ContentItem

_APPLIED: set[str] = set()
"""已成功挂载的补丁名集合（per-patch 幂等）。

按名记录使失败项可单独重试；单一布尔标志无法表达「部分成功」的中间态
——重跑已成功的补丁会经 _mount 二次追加 _MOUNTED，并把 VENDOR_* 留存
的上游原件覆盖为桥实现，行为哨兵随之失真。
"""

VENDOR_BUFF_CONTENT: property | None = None
"""挂载前留存的上游 ``News.content``；行为哨兵直接调用其 fget 验证缺陷仍在。"""

VENDOR_HUPU_ITER: Callable[[BeautifulSoup], Iterator[Any]] | None = None
"""挂载前留存的上游 ``_iter_media_and_text``；行为哨兵直接调用它。"""

VENDOR_HLS_PARAMS: tuple[str, ...] = ()
"""挂载前留存的上游 ``FFmpeg.download_hls_to_mp4`` 形参名（签名契约）。"""

_MOUNTED: list[tuple[str, str]] = []


def mounted_points() -> tuple[tuple[str, str], ...]:
    """已实际挂载的（模块名, 限定名）清单。

    测试据此断言每个挂载点承载的函数 __module__ 指向本模块（而非上游模块），
    从而把「补丁到底有没有挂上去」变成可机械核验的事实。
    """
    return tuple(_MOUNTED)


def resolve_mount_point(module_name: str, qualname: str) -> Any:
    """按（模块名, 限定名）解析挂载点当前对象；锚点缺失即响亮失败。"""
    module = importlib.import_module(module_name)
    target: Any = module
    owner_name = module_name
    for part in qualname.split("."):
        if not hasattr(target, part):
            raise RuntimeError(
                f"补丁锚点 {owner_name}.{part} 不存在——上游已改名/重构，请复核补丁必要性后再挂载"
            )
        target = getattr(target, part)
        owner_name = f"{owner_name}.{part}"
    return target


def bridge_function(obj: Any) -> Any:
    """取出挂载点上承载的「桥实现函数」（property→fget，classmethod→__func__）。"""
    if isinstance(obj, property):
        return obj.fget
    return getattr(obj, "__func__", None) or obj


def _mount(
    module: ModuleType,
    qualname: str,
    replacement: Any,
    *,
    expect: Literal["property", "callable", "async_callable"],
) -> Any:
    """挂载前断言锚点存在且形态符合预期，否则响亮失败；返回挂载前的原件。

    模块属性 setattr 永远成功，不会因上游改名而报错——若不显式断言，补丁会
    静默失效而源码字符串哨兵测不出。
    """
    current = resolve_mount_point(module.__name__, qualname)
    shape_ok = {
        "property": isinstance(current, property),
        "callable": callable(current),
        "async_callable": inspect.iscoroutinefunction(current),
    }[expect]
    if not shape_ok:
        raise RuntimeError(
            f"补丁锚点 {module.__name__}.{qualname} 形态不符：期望 {expect}，"
            f"实为 {type(current).__name__}——上游已改签名/形态，请复核补丁必要性"
        )
    owner_name, _, attr = qualname.rpartition(".")
    owner = module if not owner_name else resolve_mount_point(module.__name__, owner_name)
    setattr(owner, attr, replacement)
    _MOUNTED.append((module.__name__, qualname))
    return current


def apply_vendor_patches() -> None:
    """应用全部 vendor 运行态补丁；按名幂等，可重复调用。

    任一挂载点缺失或形态不符即抛 RuntimeError（响亮失败），不静默降级；
    已成功的补丁下次调用跳过，失败项可重试。
    """
    for name, patcher in (
        ("buff_news_content", _patch_buff_news_content),
        ("hupu_iter_media_and_text", _patch_hupu_iter_media_and_text),
        ("ffmpeg_hls", _patch_ffmpeg_hls_ssrf),
    ):
        if name in _APPLIED:
            continue
        patcher()
        _APPLIED.add(name)


# ---------------------------------------------------------------- buff


def _patch_buff_news_content() -> None:
    from bs4 import BeautifulSoup
    from bs4.element import NavigableString, Tag

    from ..vendor.nonebot_plugin_parser_lite.creator import Creator
    from ..vendor.nonebot_plugin_parser_lite.parsers.buff import news as buff_news
    from ..vendor.nonebot_plugin_parser_lite.utils.format import (
        HTML_NEWLINE_TAGS,
        append_html_text,
        clean_blank,
        replace_anchor_hrefs,
    )

    def _content(self: buff_news.News) -> list[ContentItem]:
        """复刻 News.content（上游 buff/news.py:30-83）。

        差异：descendants 先快照再迭代 + destroyed 集合跳过已销毁子树；
        上游在迭代中 decompose() 会截断迭代，视频块后正文静默丢失。
        """
        data: list[ContentItem] = []
        soup = BeautifulSoup(self.body, "html.parser")
        replace_anchor_hrefs(soup, "https://buff.163.com/")

        text_buffer: list[str] = []

        def flush_text() -> None:
            append_html_text(data, text_buffer)
            text_buffer.clear()

        destroyed: set[int] = set()
        for element in list(soup.descendants):
            if id(element) in destroyed:
                continue
            # 标签节点
            if isinstance(element, Tag):
                if element.name in HTML_NEWLINE_TAGS:
                    text_buffer.append("\n")
                    continue
                if element.name == "div" and "video-content" in (element.get("class") or []):
                    # data-src 一定存在
                    video = str(element["data-src"])
                    # div 下面必含一个 img 封面（第一个 img 即封面）
                    imgs = element.find_all("img")
                    if not imgs:
                        continue
                    flush_text()
                    cover_img = imgs[0]
                    thumb = str(cover_img["src"])

                    data.append(
                        Creator.video(
                            url_or_task=video,
                            cover_url=thumb,
                        )
                    )
                    # 与上游语义一致：视频内部节点不再当作普通图/文本处理；
                    # 差异：decompose 前先快照子树成员，迭代得以继续
                    destroyed.update(id(node) for node in element.descendants)
                    element.decompose()
                    continue

                # 普通图片（保持与上游逐行同源）
                if element.name == "img":  # noqa: SIM102
                    if src_attr := element.get("data-original"):
                        flush_text()
                        data.append(Creator.graphic(url=str(src_attr)))

            elif isinstance(element, NavigableString):
                if text := clean_blank(str(element)):
                    text_buffer.append(text)

        flush_text()

        return data

    global VENDOR_BUFF_CONTENT
    VENDOR_BUFF_CONTENT = _mount(buff_news, "News.content", property(_content), expect="property")


# ---------------------------------------------------------------- hupu


def _patch_hupu_iter_media_and_text() -> None:
    from bs4.element import NavigableString, Tag

    from ..vendor.nonebot_plugin_parser_lite.creator import Creator
    from ..vendor.nonebot_plugin_parser_lite.parsers.hupu import util as hupu_util
    from ..vendor.nonebot_plugin_parser_lite.utils.format import (
        HTML_NEWLINE_TAGS,
        anchor_text,
        clean_blank,
    )

    def _iter_media_and_text(
        soup: BeautifulSoup,
    ) -> Iterator[ContentItem | str]:
        """复刻 _iter_media_and_text（上游 hupu/util.py:37-77）。

        差异：①descendants 快照迭代 + destroyed 集合，视频后内容不再截断；
        ②video 缺 src 时跳过（上游产出字面 "None" URL）；③poster 缺失传
        None（上游 str(None) 产出 "None" 封面 URL）。
        """
        seen_anchors: set[int] = set()
        destroyed: set[int] = set()
        for element in list(soup.descendants):
            if id(element) in destroyed:
                continue
            if isinstance(element, Tag):
                if element.name in HTML_NEWLINE_TAGS:
                    yield "\n"
                    continue

                if element.name == "video":
                    video_url = element.get("src")
                    if not isinstance(video_url, str) or not video_url:
                        continue
                    stable_url = urlparse(video_url)._replace(query="", fragment="").geturl()
                    yield Creator.video(
                        url_or_task=video_url,
                        cover_url=element.get("poster"),
                        cache_key=f"hupu:{stable_url}",
                    )
                    # decompose 前先快照子树成员，迭代得以继续
                    destroyed.update(id(node) for node in element.descendants)
                    element.decompose()
                    continue

                # 保持与上游逐行同源
                if element.name == "img":  # noqa: SIM102
                    if src := (
                        element.get("data-gif") or element.get("data-src") or element.get("src")
                    ):
                        yield Creator.graphic(url=str(src))

            elif isinstance(element, NavigableString):
                anchor = element.find_parent("a")
                if anchor is not None and id(anchor) not in seen_anchors:
                    seen_anchors.add(id(anchor))
                    if text := anchor_text(anchor, "https://bbs.hupu.com/"):
                        yield text
                    continue
                if anchor is not None:
                    continue
                if text := clean_blank(str(element)):
                    yield text

    # parse_rich_content 按模块全局查找调用本函数，bbs/comment 的
    # from .util import parse_rich_content 绑定因此自动走到修复版
    global VENDOR_HUPU_ITER
    VENDOR_HUPU_ITER = _mount(
        hupu_util, "_iter_media_and_text", _iter_media_and_text, expect="callable"
    )


# ---------------------------------------------------------------- ffmpeg


def _build_hls_guard(original: Any, ssrf_module: Any) -> Any:
    """构造 HLS 下载的 SSRF 守卫（包住 vendor 的 ``download_hls_to_mp4``）。

    ① ``*args/**kwargs`` 透传：上游新增带默认值的形参不再被静默丢弃（固定
    签名下必填才 TypeError，且会被 vendor 调用点的 except Exception 吞成
    DownloadException）；② ``validate_url`` 内含阻塞的 socket.getaddrinfo
    （ssrf.py 自注「阻塞调用，放线程池执行避免卡死事件循环」），DNS 慢/黑洞
    时同步调用会冻结整个 AstrBot 事件循环——此处投线程池。

    ``ssrf_module.validate_url`` 在调用时按模块属性查找（而非构造时捕获），
    使测试与 ssrf 内部替身能生效。
    """

    async def guarded(cls: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        # 子进程出站无法钉扎 IP，入口处按桥内 SSRF 白名单校验（尽力而为，
        # 见模块文档局限说明）。UrlBlockedError 是 BaseException：vendor
        # 调用点的 except Exception 包不住它，安全拒绝直达插件入口
        # （main 的 except UrlBlockedError），不与普通下载失败混淆。
        await to_thread.run_sync(ssrf_module.validate_url, url)
        return await original(url, *args, **kwargs)

    guarded._ssrf_guarded = True  # type: ignore[attr-defined]
    return guarded


def _patch_ffmpeg_hls_ssrf() -> None:
    from ..vendor.nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
    from . import ssrf

    if getattr(FFmpeg.download_hls_to_mp4, "_ssrf_guarded", False):
        return
    # 取绑定后的 classmethod（cls 自动携带）而非 __func__：后者签名首位
    # 是 cls，按 (url, output_path, ...) 调用会整体错位一位而抛 TypeError，
    # 且被调用点的 except Exception 吞成 DownloadException，
    # 表现为全部 HLS 视频静默下载失败）
    original = FFmpeg.download_hls_to_mp4
    global VENDOR_HLS_PARAMS
    VENDOR_HLS_PARAMS = tuple(inspect.signature(original).parameters)
    _mount(
        sys.modules[FFmpeg.__module__],
        "FFmpeg.download_hls_to_mp4",
        classmethod(_build_hls_guard(original, ssrf)),
        expect="async_callable",
    )
