"""发送桥：ParseResult → AstrBot 消息链（上游 main send_content 等价移植）。

ACL R1：本模块只做 vendor 模型 → AstrBot 组件的翻译，不引入解析业务规则；
vendor MediaFile 是唯一中转模型，不越桥（R2）。发送顺序与上游 main 一致：
卡片（main.py 渲染）→ 即时媒体（视频/音频）→ 图文合并转发。

懒下载（与上游 main 的映射差异）：上游用命令领取制
（LazyManager + download_command）；桥内用 session_waiter 问询——
lazy_download 开启时，卡片发出后问询一次，回复 download_command 之一确认
后发送媒体，其他回复或超时静默跳过。
"""

from __future__ import annotations

import asyncio
import base64
import weakref
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path as SyncPath
from typing import TYPE_CHECKING, Any

import anyio
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from ..vendor.nonebot_plugin_parser_lite.config import _nickname, pconfig
from ..vendor.nonebot_plugin_parser_lite.data import (
    AudioContent,
    GraphicContent,
    ImageContent,
    LinkContent,
    LivePhotoContent,
    MediaContent,
    ParseResult,
    PollContent,
    QuoteContent,
    StickerContent,
    VideoContent,
)
from ..vendor.nonebot_plugin_parser_lite.exception import (
    DownloadException,
    SizeLimitException,
)
from ..vendor.nonebot_plugin_parser_lite.helper import MediaFile, UniHelper
from ..vendor.nonebot_plugin_parser_lite.utils.log import logger
from . import render_params, texts

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

MAX_FORWARD_TEXT_LEN = render_params.MAX_FORWARD_TEXT_LEN
"""单个合并转发的文本总长上限（上游 main 注入值，渲染参数注入层）"""
MAX_FORWARD_NODES = render_params.MAX_FORWARD_NODES
"""单个合并转发的节点数上限（上游 main 注入值）"""
TEXT_SPLIT_PUNCTUATION = frozenset(render_params.TEXT_SPLIT_PUNCTUATION)
"""长文本切分标点集（上游 main 注入值；frozenset 形态由桥组包）"""

FORWARD_TEXT_THRESHOLD_MIN = 1
"""转发文本阈值下界：0/负数会让「切分」与「是否转发」两个判据自相矛盾。"""
FORWARD_TEXT_THRESHOLD_MAX = 4500
"""转发文本阈值上界（与 _conf_schema.json 描述「最大4500」对齐）。

AstrBot 4.28 的插件配置面板对 int 字段不做范围校验（`minimum`/`maximum`
不是宿主消费的键，`slider` 也只是额外渲染一个滑块，旁边的数字输入框仍可
自由输入），因此上下界必须由运行期钳制兜住。
"""

VIDEO_FILE_THRESHOLD_DEFAULT_MB = 100
"""视频转文件发送阈值的默认值（MB）：模块初值、非法配置回落、main.py
入口解析兜底共用（单点定义）。"""

_video_file_threshold_mb: int = VIDEO_FILE_THRESHOLD_DEFAULT_MB
"""当前生效阈值；main.py 在配置桥时注入，测试可直接覆写"""

LAZY_TIMEOUT_MIN = 5
"""懒下载问询超时下界（与 session_waiter 最小可用超时对齐）"""
LAZY_TIMEOUT_MAX = 300
"""懒下载问询超时上界：WebUI 不校验 int，无上界会让一次拒绝挂 3 天。"""


def _clamp_lazy_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return LAZY_TIMEOUT_MIN
    return max(LAZY_TIMEOUT_MIN, min(LAZY_TIMEOUT_MAX, value))


def lazy_download_timeout() -> int:
    """本次问询实际生效的懒下载超时（已钳制到 [5, 300]）。"""
    return _clamp_lazy_timeout(pconfig.lazy_download_timeout)


def _clamp_forward_text_threshold(value: Any) -> int:
    """把 WebUI 可自由输入的转发阈值钳制到 ``[1, 4500]``。

    为什么不能容忍 <=0：``need_forward`` 用 ``total_plain_len > split_threshold``
    判定，阈值 0/负数对任何非空文本恒为 True ⇒ **一定走转发**；而
    ``split_text_by_length_with_punct`` 与 ``_ForwardText.split`` 在
    ``max_len <= 0`` 时走快路径**整段不切**。两个判据在同一次调用里互相矛盾，
    结果是超长文本（可达 30000 上限之上）被整体塞进单个转发节点。钳到下界 1 后：
    要么按 1 字切分（退化但不越界），要么用户
    走 `need_forward_contents=False` 这个语义正确的开关。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return FORWARD_TEXT_THRESHOLD_MIN
    return max(FORWARD_TEXT_THRESHOLD_MIN, min(FORWARD_TEXT_THRESHOLD_MAX, value))


def forward_text_threshold() -> int:
    """本次发送实际生效的转发阈值（已钳制）。"""
    return _clamp_forward_text_threshold(pconfig.forward_text_threshold)


def _sync_path(path: Any) -> SyncPath:
    """vendor 返回的 anyio.Path → 同步 pathlib（.name/.as_uri/read_bytes 均同步语义）。"""
    return SyncPath(str(path))


def set_video_file_threshold_mb(mb: int) -> None:
    global _video_file_threshold_mb
    try:
        _video_file_threshold_mb = max(1, int(mb))
    except (TypeError, ValueError):
        # WebUI/陈旧配置可能给到 str/None；回落默认而非炸加载
        _video_file_threshold_mb = VIDEO_FILE_THRESHOLD_DEFAULT_MB


class _SenderFilter(SessionFilter):
    """问询会话粒度 = 会话 + 发送者，避免群内他人消息误触发确认。

    键必须**仅由事件推导**（不能掺入本次解析的链接指纹）：AstrBot
    在注册侧与回复侧都会调用 ``filter(event)`` 来定位 ``USER_SESSIONS``，掺入
    只有注册侧才知道的信息会让回复永远匹配不上。并发隔离由下方的
    会话锁承担。
    """

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


# 每个问询会话一把锁（WeakValueDictionary 自清理：无人持有时条目自动消失）。
# AstrBot 的 USER_SESSIONS 是**单槽覆盖**（register_wait 直接赋值、_cleanup 无
# 条件 pop），同一会话并发两次问询时后者会顶掉前者：用户回复只触发后者，
# 前者静默等到超时——两条链接的媒体都可能不发送。
# 串行化后同一会话同一时刻只有一个等待者，「一次回复确认一次问询」的语义得以保持。
_lazy_session_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _session_lock(session_id: str) -> asyncio.Lock:
    """取该会话的问询锁（首次访问时创建）。

    get→set 之间无 await，asyncio 单线程下即为原子的 check-and-set。

    ``asyncio.Lock`` 在**首次真正竞争**时才绑定事件循环（无竞争
    时 acquire 走快路径不绑定），之后换循环 acquire 会抛
    ``RuntimeError: is bound to a different event loop``。AstrBot 是长驻单循环故不会触发；
    但测试（pytest-asyncio 每用例新建循环）若让同一把锁跨用例
    存活就会踩到，故此处显式核对循环身份，不匹配即重建。
    """
    lock = _lazy_session_locks.get(session_id)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中循环（如同步调用本帮助函数）时无从核对循环身份，直接复用
        running = None
    bound = getattr(lock, "_loop", None)
    if lock is None or (running is not None and bound is not None and bound is not running):
        lock = asyncio.Lock()
        _lazy_session_locks[session_id] = lock
    return lock


async def ask_lazy_download(event: AstrMessageEvent) -> bool:
    """问询是否发送媒体资源；确认（回复下载命令之一）返回 True。

    其他回复或超时（TimeoutError）均静默返回 False，与工单 07 验收一致。
    同一会话内的并发问询按到达顺序串行（见 ``_lazy_session_locks``）。
    """
    session_id = f"{event.unified_msg_origin}:{event.get_sender_id()}"
    async with _session_lock(session_id):
        return await _ask_lazy_download(event)


async def _ask_lazy_download(event: AstrMessageEvent) -> bool:
    """实际问询（调用方已持有该会话的锁，保证 USER_SESSIONS 单槽不被覆盖）。"""
    timeout = lazy_download_timeout()
    if pconfig.lazy_download_tip:
        # 与上游 matchers/__init__.py:162-168 同构：提示受 plite_lazy_download_tip
        # 门控（schema 默认 false）——门控缺失会让用户在 WebUI 关掉开关后
        # 行为完全不变（配置静默失效）
        commands = "、".join(f"『{cmd}』" for cmd in pconfig.download_command)
        await event.send(event.plain_result(texts.LAZY_DOWNLOAD_PROMPT.format(timeout, commands)))
    confirmed = False

    @session_waiter(timeout=timeout, record_history_chains=False)
    async def waiter(controller: SessionController, ev: AstrMessageEvent) -> None:
        nonlocal confirmed
        confirmed = ev.message_str.strip() in set(pconfig.download_command)
        controller.stop()

    try:
        await waiter(event, session_filter=_SenderFilter())
    except TimeoutError:
        return False
    return confirmed


async def _to_base64(path: SyncPath, raw: bytes | None = None) -> str:
    """线程内读文件并 base64 编码：阻塞 IO 不占事件循环（大视频可达数十 MB）。"""
    data = await anyio.to_thread.run_sync(lambda: raw if raw is not None else path.read_bytes())
    return base64.b64encode(data).decode()


def _ensure_readable(path: SyncPath) -> SyncPath:
    """缺失/不可读的媒体文件在此抛 ``OSError``（由 mediafile_to_comp 降级为文本）。

    真实 AstrBot 的 ``fromFileSystem`` 一律是 ``Path.resolve(strict=False)``，对
    缺失文件不抛，会产出指向不存在路径的组件（到协议适配层
    发送时才失败，届时已无法按单媒体降级）。显式 stat 使「文件缺失」
    在所有 kind/模式下一律走 OSError → 文本占位：M9 初版修法只覆盖了
    video（本就有 stat）与 base64 分支（read_bytes 会抛），非 base64 的
    image/audio 漏了（审阅时实测确认）。
    """
    path.stat()
    return path


async def mediafile_to_comp(mf: MediaFile) -> Comp.BaseMessageComponent:
    """vendor MediaFile → AstrBot 组件；use_base64 语义与上游一致。

    媒体文件在转换瞬间可能已被外部清理（vendor 缓存目录由每 2h 的清理任务
    接管）：``stat()`` / ``read_bytes()`` 的 ``OSError`` 一律按**单媒体降级**处理
    （文本占位），不向上传播——否则会中断整条发送链，后续合并转发与失败
    计数全部丢失。
    """
    try:
        return await _mediafile_to_comp_strict(mf)
    except OSError:
        logger.warning("媒体文件不可读，降级为文本：%s", mf.path, exc_info=True)
        return Comp.Plain(texts.MEDIA_FAILED.format(mf.kind))


async def _mediafile_to_comp_strict(mf: MediaFile) -> Comp.BaseMessageComponent:
    """严格转换。

    文件不可读（``stat()`` / ``read_bytes()`` 抛 ``OSError``）时**向上抛**，由
    ``mediafile_to_comp`` 统一降级为文本占位。音频分支额外区分两种失败：
    文件不可读（``OSError``，继续向上抛）与组件构造失败（如缺 ffmpeg，
    回退文件消息）——后者文件存在但无法转为语音，文件消息仍可送达（M9）。
    """
    if mf.kind == "image":
        if pconfig.use_base64:
            if mf.raw is not None:
                encoded = base64.b64encode(mf.raw).decode()
            else:
                encoded = await _to_base64(_sync_path(mf.path))
            return Comp.Image.fromBase64(encoded)
        return Comp.Image.fromFileSystem(str(_ensure_readable(_sync_path(mf.path))))
    if mf.kind == "video":
        # 上游 helper.video_seg 的两道闸：零字节视频回退为提示文本（防缓存
        # 文件被外部截断后发送空视频），超阈值转文件消息（NapCat 视频消息
        # 体积受限）——即时媒体与转发内视频（如 LivePhoto）共用本收口
        video_path = _sync_path(mf.path)
        file_size = video_path.stat().st_size
        if file_size == 0:
            return Comp.Plain(texts.VIDEO_ZERO_SIZE)
        # 边界含阈值本身（>=）：「等于阈值也改文件」与配置描述一致
        if file_size >= _video_file_threshold_mb * 1024 * 1024:
            return Comp.File(name=video_path.name, file=str(video_path))
        if pconfig.use_base64:
            comp: Comp.BaseMessageComponent = Comp.Video.fromBase64(
                await _to_base64(video_path),
            )
        else:
            comp = Comp.Video.fromFileSystem(str(video_path))
        if mf.thumbnail is not None:
            # Video 组件有 cover 字段（适配器透传给 NapCat 作封面）。
            # 零字节或不可读缩略图不设封面——对齐上游 helper.video_seg 的第三道闸
            # （``if thumb_stat.st_size > 0``）。
            # 不可读按「无封面」处理而非抛错：封面是装饰，不该让整条视频发送失败
            thumb = _sync_path(mf.thumbnail)
            try:
                has_cover = thumb.stat().st_size > 0
            except OSError:
                has_cover = False
            if has_cover:
                comp.cover = thumb.as_uri()
        return comp
    if mf.kind == "audio":
        try:
            if pconfig.use_base64:
                return Comp.Record.fromBase64(await _to_base64(_sync_path(mf.path)))
            return Comp.Record.fromFileSystem(str(_ensure_readable(_sync_path(mf.path))))
        except OSError:
            # 文件本身不可读：文件消息兜底同样无意义，交由上层文本占位
            raise
        except Exception:
            # 语音消息构造失败（如缺 ffmpeg / 格式不支持）→ 文件消息兜底（工单 04）
            logger.warning("语音消息构造失败，回退文件消息: %s", mf.path, exc_info=True)
            return Comp.File(name=_sync_path(mf.path).name, file=str(mf.path))
    return Comp.File(name=mf.name or _sync_path(mf.path).name, file=str(mf.path))


async def _handle_immediate_media(
    cont: MediaContent,
    event: AstrMessageEvent,
) -> AsyncGenerator[list[Comp.BaseMessageComponent]]:
    """即时媒体（视频/音频）→ 逐条 yield 消息链。

    视频按 _video_file_threshold_mb 分流：超限或 need_upload_video → File，
    否则 Video（带封面）。与上游 __handle_immediate_media 等价，仅分流阈值
    是桥内新增。
    """
    if not isinstance(cont, VideoContent | AudioContent):
        return
    if isinstance(cont, VideoContent):
        await cont.get_display_size()  # HEAD 预热；失败时 _size_bytes 为 None
        size = getattr(cont, "_size_bytes", None)
        threshold_bytes = _video_file_threshold_mb * 1024 * 1024
        path = await cont.get_path()
        # vendor get_path 返回 anyio.Path：其 stat() 是协程，同步取 st_size 会
        # 抛 AttributeError（except OSError 接不住，整个发送段崩溃）——必须先
        # 转同步 pathlib。零字节闸覆盖 File 分支：HEAD 失败（size=None）时以
        # 本地 stat 兜底，空文件不进聊天。
        sync_path = _sync_path(path)
        try:
            path_size = sync_path.stat().st_size
        except OSError:
            path_size = -1  # 缺失交给下游 OSError 降级
        if path_size == 0:
            yield [Comp.Plain(texts.VIDEO_ZERO_SIZE)]
            return
        if pconfig.need_upload_video or (
            (size if size is not None else path_size) >= threshold_bytes
        ):
            yield [Comp.File(name=sync_path.name, file=str(sync_path))]
        else:
            seg = await UniHelper.video_seg(path, thumbnail=await cont.get_cover_path())
            yield [await mediafile_to_comp(seg)]
        return

    path = await cont.get_path()
    # 语音构造失败的文件兜底已下沉到 _mediafile_to_comp_strict：UniHelper.record_seg
    # 永不抛（helper.py 仅构造 MediaFile），在此处写 try/except 是死分支。
    seg = (
        await UniHelper.file_seg(path)
        if pconfig.need_upload_audio
        else await UniHelper.record_seg(path)
    )
    yield [await mediafile_to_comp(seg)]


def _find_text_split_end(text: str, start: int, max_len: int) -> int:
    """返回下一段的结束索引，优先落在标点之后（照搬上游）。"""
    end = min(start + max_len, len(text))
    if end == len(text):
        return end
    return next(
        (
            index + 1
            for index in range(end - 1, start - 1, -1)
            if text[index] in TEXT_SPLIT_PUNCTUATION
        ),
        end,
    )


def split_text_by_length_with_punct(text: str, max_len: int) -> list[str]:
    """按长度切分文本，优先在标点符号处断句（照搬上游）。"""
    if max_len <= 0 or len(text) <= max_len:
        return [text]

    result: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = _find_text_split_end(text, start, max_len)
        result.append(text[start:end])
        start = end
    return result


@dataclass(slots=True)
class _ForwardTextPart:
    text: str
    protected: bool = False


@dataclass(slots=True)
class _ForwardText:
    """保留块边界的待拆分转发文本（照搬上游）。"""

    author_name: str
    parts: list[_ForwardTextPart]
    include_author: bool = True
    text_length: int = field(init=False)

    def __post_init__(self) -> None:
        self.text_length = len(self.prefix) + sum(len(part.text) for part in self.parts)

    @property
    def prefix(self) -> str:
        return f"{self.author_name}：" if self.include_author else ""

    @property
    def text(self) -> str:
        return f"{self.prefix}{''.join(part.text for part in self.parts)}"

    def split(self, max_len: int) -> list[str]:
        if max_len <= 0 or self.text_length <= max_len:
            return [self.text]

        prefix = self.prefix
        chunks: list[str] = []
        current = prefix

        def flush() -> None:
            nonlocal current
            if current:
                chunks.append(current)
                current = ""

        for part in self.parts:
            if part.protected:
                # 受保护块可以超过软拆分阈值，但默认不内切；若块本身硬超
                # max_len（远超单节点容量），继续整塞会产出超长节点——
                # 此时按 max_len 硬切（保护语义让位于不可发送的硬上限）。
                if current and current != prefix and len(current) + len(part.text) > max_len:
                    flush()
                remaining = part.text
                if len(remaining) <= max_len and len(current) + len(remaining) <= max_len:
                    current += remaining
                    continue
                while remaining:
                    room = max_len - len(current)
                    if room <= 0:
                        # current==prefix 且前缀本身已超 max_len：前缀是原子
                        # 单元不单独成块，继续塞入（宁可超限也不切碎作者名）
                        if current != prefix:
                            flush()
                        room = max_len
                    take = remaining[:room]
                    current += take
                    remaining = remaining[room:]
                    if remaining:
                        flush()
                continue

            start = 0
            part_length = len(part.text)
            while start < part_length:
                room = max_len - len(current)
                if room <= 0:
                    flush()
                    room = max_len
                if part_length - start <= room:
                    current += part.text[start:]
                    break
                end = _find_text_split_end(part.text, start, room)
                current += part.text[start:end]
                start = end
                flush()

        flush()
        return chunks


@dataclass(slots=True)
class _AltMedia:
    """图文中图片 + 说明文字合入同一转发节点（上游 seg + alt 的等价物）。"""

    media: MediaFile
    alt: str


async def _seg_to_comps(seg: str | MediaFile | _AltMedia) -> list[Comp.BaseMessageComponent]:
    if isinstance(seg, str):
        return [Comp.Plain(seg)]
    if isinstance(seg, _AltMedia):
        return [await mediafile_to_comp(seg.media), Comp.Plain(seg.alt)]
    return [await mediafile_to_comp(seg)]


async def _seg_to_node(seg: str | MediaFile | _AltMedia, uin: str) -> Comp.Node:
    return Comp.Node(content=await _seg_to_comps(seg), uin=uin, name=_nickname)


async def _build_forward_segs(
    result: ParseResult,
) -> list[str | MediaFile | _AltMedia | _ForwardText]:
    """构造有序转发段（文本 + 媒体，保持顺序）；照搬上游 __build_forward_segs。"""

    async def build_nodes(pr: ParseResult) -> list[str | MediaFile | _AltMedia | _ForwardText]:
        author_name = pr.author.name
        nodes: list[str | MediaFile | _AltMedia | _ForwardText] = []
        text_buffer: list[_ForwardTextPart] = []
        author_prefix_pending = True
        if title := pr.title:
            # 标题节点尾随换行与上游一致（_ForwardTextPart(f"{title}\n")）
            nodes.append(
                _ForwardText(author_name, [_ForwardTextPart(f"{title}\n")], include_author=False)
            )

        async def flush_text() -> None:
            nonlocal author_prefix_pending, text_buffer
            if text_buffer:
                nodes.append(
                    _ForwardText(
                        author_name,
                        text_buffer,
                        include_author=author_prefix_pending,
                    ),
                )
                author_prefix_pending = False
                text_buffer = []

        def append_text(text: str) -> None:
            text_buffer.append(_ForwardTextPart(text))

        def append_text_block(text: str) -> None:
            """块级文本与相邻内容换行分隔，且不在块内部切开。"""
            if not text:
                return
            if text_buffer and not text_buffer[-1].text.endswith("\n"):
                append_text("\n")
            text_buffer.append(_ForwardTextPart(f"{text}\n", protected=True))

        async def append_media(cont: MediaContent) -> None:
            try:
                if isinstance(cont, VideoContent):
                    # 视频在转发里用封面图占位
                    path = await cont.get_cover_path()
                    if path:
                        nodes.append(await UniHelper.img_seg(path))
                    return

                if isinstance(cont, ImageContent):
                    nodes.append(await UniHelper.img_seg(await cont.get_path()))
                    return

                if isinstance(cont, GraphicContent):
                    seg = await UniHelper.img_seg(await cont.get_path())
                    nodes.append(_AltMedia(seg, cont.alt) if cont.alt else seg)
                    return

                if isinstance(cont, LivePhotoContent):
                    if pconfig.live_photo:
                        live_path = await cont.get_live()
                        nodes.append(
                            await UniHelper.video_seg(live_path, thumbnail=await cont.get_base()),
                        )
                    else:
                        base_path = await cont.get_base()
                        live_path = await cont.get_path()
                        nodes.append(await UniHelper.img_seg(base_path))
                        nodes.append(await UniHelper.video_seg(live_path, thumbnail=base_path))
                    return
            except Exception:
                logger.warning("媒体加载失败: %s", type(cont).__name__, exc_info=True)
                nodes.append(texts.MEDIA_FAILED.format(type(cont).__name__))

        for item in pr.content:
            if isinstance(item, str):
                append_text(item)
            elif isinstance(item, StickerContent):
                append_text(item.desc or texts.STICKER_PLACEHOLDER)
            elif isinstance(item, MediaContent) and item.need_send:
                await flush_text()
                await append_media(item)
            elif isinstance(item, LinkContent):
                await flush_text()
                if preview := await item.get_preview_path():
                    nodes.append(await UniHelper.img_seg(preview))
                append_text(item.url)
            elif isinstance(item, QuoteContent):
                quote_parts = [part for part in (item.title, item.text) if part]
                if item.url:
                    quote_parts.append(item.url)
                append_text_block("\n".join(quote_parts))
            elif isinstance(item, PollContent):
                option_vote_total = item.option_vote_total
                poll_parts = [texts.POLL_HEADER.format(item.title or texts.POLL_TITLE_FALLBACK)]
                poll_parts.extend(
                    texts.POLL_OPTION_LINE.format(
                        option.text,
                        option.votes,
                        item.option_percentage(option, option_vote_total),
                    )
                    for option in item.options
                )
                status = [texts.POLL_STATUS_CLOSED if item.closed else texts.POLL_STATUS_OPEN]
                if item.multiple:
                    status.append(texts.POLL_STATUS_MULTIPLE)
                if item.total_voters is not None:
                    status.append(texts.POLL_VOTERS.format(item.total_voters))
                poll_parts.append(texts.STATUS_SEPARATOR.join(status))
                append_text_block("\n".join(poll_parts))

        await flush_text()
        return nodes

    ordered: list[str | MediaFile | _AltMedia | _ForwardText] = list(await build_nodes(result))
    repost = result.repost
    if not repost:
        return ordered
    ordered.append(texts.REPOST_MARKER)
    ordered.extend(await build_nodes(repost))
    return ordered


async def send_result(
    result: ParseResult,
    event: AstrMessageEvent,
) -> AsyncGenerator[list[Comp.BaseMessageComponent]]:
    """发送媒体内容：即时媒体逐条 yield，图文/图片合并转发或平铺。

    与上游 send_content 等价；forward_text_threshold 在调用时读取（桥的
    配置注入晚于模块导入，不能缓存为模块常量）。
    """
    failed_count = 0
    repost_medias = result.repost.content if result.repost else []
    media_contents = (
        cont
        for cont in chain(result.content, repost_medias)
        if isinstance(cont, MediaContent) and cont.need_send
    )
    for cont in media_contents:
        try:
            async for msg_chain in _handle_immediate_media(cont, event):
                yield msg_chain
        except SizeLimitException:
            yield [Comp.Plain(texts.OVERSIZED_HINT.format(result.platform.display_name))]
            continue
        except DownloadException as exc:
            failed_count += 1
            logger.warning("%s 下载失败: %r", cont.__class__.__name__, exc)
            continue

    ordered_segs = await _build_forward_segs(result)
    if ordered_segs:
        # 钳制后再判/再切：裸配置为 0/负数时 need_forward 恒 True 而切分函数
        # 整段返回，超长文本会不经切分塞进单个转发节点
        split_threshold = forward_text_threshold()
        processed_segs: list[str | MediaFile | _AltMedia] = []
        total_plain_len = 0
        node_count = 0

        for seg in ordered_segs:
            node_count += 1
            if isinstance(seg, _ForwardText):
                total_plain_len += seg.text_length
                processed_segs.extend(seg.split(split_threshold))
            elif isinstance(seg, str):
                total_plain_len += len(seg)
                if len(seg) > split_threshold:
                    processed_segs.extend(split_text_by_length_with_punct(seg, split_threshold))
                else:
                    processed_segs.append(seg)
            else:
                processed_segs.append(seg)

        need_forward = (
            pconfig.need_forward_contents or total_plain_len > split_threshold or node_count > 4
        )

        if not need_forward:
            flat_chain: list[Comp.BaseMessageComponent] = []
            for seg in processed_segs:
                flat_chain.extend(await _seg_to_comps(seg))
            yield flat_chain
        else:
            uin = event.get_self_id()
            current_chunk: list[str | MediaFile | _AltMedia] = []
            current_text_len = 0

            async def flush_chunk() -> Comp.Nodes | None:
                nonlocal current_text_len
                if not current_chunk:
                    return None
                nodes = [await _seg_to_node(seg, uin) for seg in current_chunk]
                current_chunk.clear()
                current_text_len = 0
                return Comp.Nodes(nodes)

            for seg in processed_segs:
                seg_text_len = len(seg) if isinstance(seg, str) else 0
                if current_chunk and (
                    current_text_len + seg_text_len > MAX_FORWARD_TEXT_LEN
                    or len(current_chunk) >= MAX_FORWARD_NODES
                ):
                    msg = await flush_chunk()
                    if msg is not None:
                        yield [msg]
                current_chunk.append(seg)
                current_text_len += seg_text_len

            last = await flush_chunk()
            if last is not None:
                yield [last]

    if failed_count > 0:
        # 下载失败计数整句逐字来自上游 render send_content 尾段（经
        # bridge/texts.py 锚点收编，随 roll 同步）
        yield [Comp.Plain(texts.DOWNLOAD_FAILED_COUNT.format(failed_count))]
