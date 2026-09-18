"""发送链集成测试：文本拆分 / 受保护块 / MediaFile 组件翻译。

sender 模块顶层依赖 astrbot 运行时（Comp 组件、session_waiter）。CI 与本地测试
环境通过 ``python -m pip install --no-deps -r tests/requirements-test.txt``
安装**真实** AstrBot（刻意不用手写替身：替身会接受真实库拒绝的参数，见
tests/test_ssrf.py 的历史教训——那条假 session 掩盖了一个高危签名缺陷）。

``importorskip`` 仅作本地未装 astrbot 时的优雅降级；其静默退化由 ci.yml 的
「skip 预算断言」（预算 = 1）兜底——整模块被跳过会直接让 CI 变红。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("astrbot.api")

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from astrbot_plugin_parser_lite.bridge import sender, texts
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import configure
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
    PlatformEnum,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    Author,
    ParseResult,
    Platform,
)


def test_sender_text_split_prefers_punctuation() -> None:
    text = "第一段。" * 20 + "无标点结尾"
    chunks = sender.split_text_by_length_with_punct(text, 12)
    assert "".join(chunks) == text
    assert all(len(chunk) <= 12 for chunk in chunks)
    # 除最后一段外都应断在标点之后
    assert all(chunk.endswith("。") for chunk in chunks[:-1])


def test_sender_forward_text_protected_block_not_split() -> None:
    ft = sender._ForwardText(
        author_name="作者",
        parts=[
            sender._ForwardTextPart("普通" * 40),
            sender._ForwardTextPart("受保护块整体不内切", protected=True),
        ],
    )
    chunks = ft.split(20)
    assert "".join(chunks) == ft.text
    assert any("受保护块整体不内切" in chunk for chunk in chunks)


async def test_mediafile_translation_chain(tmp_path: Path) -> None:
    configure(plite_use_base64=False)

    image_path = tmp_path / "a.jpg"
    image_path.write_bytes(b"img")
    image = sender.MediaFile("image", path=image_path, name="a.jpg")
    assert isinstance(await sender.mediafile_to_comp(image), Comp.Image)

    video_path = tmp_path / "v.mp4"
    video_path.write_bytes(b"video-bytes")
    # 缩略图必须真实存在且非空才会设封面（上游 helper.video_seg 的第三道闸，
    # 2026-09-18 修复）。旧断言用的是从未创建的路径，钉的是缺陷行为。
    thumb_path = tmp_path / "t.jpg"
    thumb_path.write_bytes(b"thumb")
    video = sender.MediaFile("video", path=video_path, thumbnail=thumb_path)
    video_comp = await sender.mediafile_to_comp(video)
    assert isinstance(video_comp, Comp.Video)
    assert video_comp.cover == thumb_path.as_uri()

    audio_path = tmp_path / "a.mp3"
    audio_path.write_bytes(b"audio")
    audio = sender.MediaFile("audio", path=audio_path)
    assert isinstance(await sender.mediafile_to_comp(audio), Comp.Record)

    file_path = tmp_path / "f.zip"
    file_path.write_bytes(b"zip")
    file = sender.MediaFile("file", path=file_path, name="f.zip")
    assert isinstance(await sender.mediafile_to_comp(file), Comp.File)


async def test_video_cover_skipped_when_thumbnail_unreadable(tmp_path: Path) -> None:
    """零字节 / 不可读缩略图不设封面（上游 helper.video_seg 的第三道闸）。

    桥此前无条件 ``as_uri()``，会把 cover 指向空文件或根本不存在的路径；
    不可读时按「无封面」处理而非抛错——封面是装饰，不该让整条视频发送失败。
    """
    configure(plite_use_base64=False)
    video_path = tmp_path / "v.mp4"
    video_path.write_bytes(b"video-bytes")

    missing = tmp_path / "missing.jpg"
    comp = await sender.mediafile_to_comp(
        sender.MediaFile("video", path=video_path, thumbnail=missing)
    )
    assert isinstance(comp, Comp.Video)
    assert comp.cover == ""

    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    comp2 = await sender.mediafile_to_comp(
        sender.MediaFile("video", path=video_path, thumbnail=empty)
    )
    assert isinstance(comp2, Comp.Video)
    assert comp2.cover == ""


async def test_lazy_download_tip_gated_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """提示发送受 plite_lazy_download_tip 门控（上游 matchers/__init__.py:162-168）。

    此前无条件发送，用户在 WebUI 关掉开关后行为完全不变——配置静默失效。
    """
    sent: list[str] = []

    class _FakeEvent:
        unified_msg_origin = "test:GroupMessage:1"

        def get_sender_id(self) -> str:
            return "1"

        def plain_result(self, text: str) -> str:
            return text

        async def send(self, msg: str) -> None:
            sent.append(msg)

    def _fake_session_waiter(**kwargs: object) -> object:
        def _deco(func: object) -> object:
            async def _call(event: object, **kw: object) -> None:
                raise TimeoutError

            return _call

        return _deco

    monkeypatch.setattr(sender, "session_waiter", _fake_session_waiter)

    configure(plite_lazy_download_tip=False)
    assert await sender._ask_lazy_download(cast(AstrMessageEvent, _FakeEvent())) is False
    assert sent == []

    configure(plite_lazy_download_tip=True)
    assert await sender._ask_lazy_download(cast(AstrMessageEvent, _FakeEvent())) is False
    assert len(sent) == 1


async def test_video_zero_size_falls_back_to_text(tmp_path: Path) -> None:
    """上游 helper.video_seg：零字节视频回退提示文本（不发送空视频）。"""
    configure(plite_use_base64=False)
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    comp = await sender.mediafile_to_comp(sender.MediaFile("video", path=empty))

    assert isinstance(comp, Comp.Plain)
    assert comp.text == texts.VIDEO_ZERO_SIZE


async def test_video_over_threshold_becomes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上游 helper.video_seg：超阈值视频转文件消息（收口含转发内视频）。"""
    configure(plite_use_base64=False)
    monkeypatch.setattr(sender, "_video_file_threshold_mb", 1)
    big = tmp_path / "big.mp4"
    big.write_bytes(b"0" * (1024 * 1024 + 1))

    comp = await sender.mediafile_to_comp(sender.MediaFile("video", path=big))

    assert isinstance(comp, Comp.File)
    assert comp.file == str(big)


def _make_result() -> ParseResult:
    return ParseResult(
        platform=Platform(name=PlatformEnum.HUPU, display_name="虎扑"),
        author=Author(name="作者"),
        url="https://bbs.hupu.com/12345678.html",
        content=["正文"],
        title="标题",
    )


async def test_forward_title_node_trailing_newline_matches_upstream() -> None:
    """标题节点尾随换行与上游 __build_forward_segs 逐字节一致。"""
    segs = await sender._build_forward_segs(_make_result())

    assert isinstance(segs[0], sender._ForwardText)
    assert segs[0].text == "标题\n"
    assert isinstance(segs[1], sender._ForwardText)
    assert segs[1].text == "作者：正文"


async def test_card_chain_url_join_and_oversize_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上游 render_messages/cache_or_render_image 对齐：URL 合并单条文本、
    ≥5MB 渲染图按文件消息发送。"""
    from astrbot_plugin_parser_lite import main as main_mod

    configure(plite_append_url=True, plite_embed_url=False)
    plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)

    small = tmp_path / "card.png"
    small.write_bytes(b"png")

    async def fake_render(result: object, renderer: object) -> Path:
        return small

    monkeypatch.setattr(main_mod, "cache_or_render_image", fake_render)
    chain = await plugin._build_card_chain(_make_result())

    assert isinstance(chain[0], Comp.Image)
    assert chain[1].text == "链接: https://bbs.hupu.com/12345678.html"

    oversize = tmp_path / "big.png"
    oversize.write_bytes(b"0" * (5 * 1024 * 1024))

    async def fake_render_big(result: object, renderer: object) -> Path:
        return oversize

    monkeypatch.setattr(main_mod, "cache_or_render_image", fake_render_big)
    chain_big = await plugin._build_card_chain(_make_result())

    assert isinstance(chain_big[0], Comp.File)
    assert chain_big[0].file == str(oversize)


# ---- M8：懒下载问询的会话并发隔离（2026-09-17 评审） ----


class _FakeEvent:
    """最小事件替身：问询路径只需 unified_msg_origin / get_sender_id。"""

    def __init__(self, umo: str, sender_id: str) -> None:
        self.unified_msg_origin = umo
        self._sender_id = sender_id

    def get_sender_id(self) -> str:
        return self._sender_id


def test_sender_filter_key_is_purely_event_derived() -> None:
    """问询会话键只能由事件推导（掺入链接指纹会让回复永远匹配不上）。"""
    assert sender._SenderFilter().filter(_FakeEvent("umo", "42")) == "umo:42"


def test_session_lock_is_per_session() -> None:
    """同一会话同一把锁；不同会话不同锁（避免跨会话过度串行）。"""
    first = sender._session_lock("umo:42")
    assert sender._session_lock("umo:42") is first
    assert sender._session_lock("umo:43") is not first


async def test_lazy_download_same_session_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """M8 回归：同一会话并发问询必须串行。

    AstrBot 的 ``USER_SESSIONS`` 是单槽覆盖（``register_wait`` 直接赋值、
    ``_cleanup`` 无条件 ``pop``）：两个等待者并存时后者顶掉前者，先结束者的
    清理又会 pop 掉后者——两条链接的媒体都可能静默不发送。
    """
    state = {"calls": 0, "active": 0, "max_active": 0}

    async def fake_ask(event: object) -> bool:
        state["calls"] += 1
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return True

    monkeypatch.setattr(sender, "_ask_lazy_download", fake_ask)
    event = _FakeEvent("umo:group:1", "10001")

    results = await asyncio.gather(
        sender.ask_lazy_download(event),
        sender.ask_lazy_download(event),
    )

    assert results == [True, True]
    assert state["calls"] == 2
    assert state["max_active"] == 1, "同一会话的问询未被串行化（USER_SESSIONS 会被覆盖）"


async def test_lazy_download_different_sessions_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同会话不得被串行化（锁粒度是会话，不是全局）。"""
    state = {"active": 0, "max_active": 0}

    async def fake_ask(event: object) -> bool:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.05)
        state["active"] -= 1
        return True

    monkeypatch.setattr(sender, "_ask_lazy_download", fake_ask)

    await asyncio.gather(
        sender.ask_lazy_download(_FakeEvent("umo:group:1", "10001")),
        sender.ask_lazy_download(_FakeEvent("umo:group:2", "10002")),
    )

    assert state["max_active"] == 2, "不同会话被不必要地串行化"


# ---- M9：媒体文件不可读时的单媒体降级（2026-09-17 评审） ----


async def test_missing_audio_file_degrades_to_text(tmp_path: Path) -> None:
    """M9 回归：媒体文件缺失时降级为文本占位，不抛 OSError 中断整条发送链。

    真实触发路径：``use_base64=true`` 下 ``_to_base64`` 的 ``read_bytes()``
    抛 ``FileNotFoundError``（视频走 ``stat().st_size``）；此前该异常直穿到
    main.py 的宽 ``except``，后续合并转发与失败计数全部丢失。
    """
    configure(plite_use_base64=True)
    missing = tmp_path / "gone.mp3"  # 刻意不创建

    comp = await sender.mediafile_to_comp(sender.MediaFile("audio", path=missing))

    assert isinstance(comp, Comp.Plain)
    assert comp.text == texts.MEDIA_FAILED.format("audio")


async def test_missing_video_file_degrades_to_text(tmp_path: Path) -> None:
    """M9：视频路径的 ``stat().st_size`` 缺失同样按单媒体降级。"""
    configure(plite_use_base64=False)
    missing = tmp_path / "gone.mp4"

    comp = await sender.mediafile_to_comp(sender.MediaFile("video", path=missing))

    assert isinstance(comp, Comp.Plain)
    assert comp.text == texts.MEDIA_FAILED.format("video")


async def test_audio_conversion_failure_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M9/工单 04：语音消息**构造**失败 → 文件消息兜底（此前是不可达分支）。

    文件存在但无法转为语音（如缺 ffmpeg）时文件消息仍可送达；这与「文件不可
    读」（降级文本）是两种不同失败，必须分开处置。
    """
    configure(plite_use_base64=False)
    audio_path = tmp_path / "a.mp3"
    audio_path.write_bytes(b"audio")

    def boom(path: object, **_: object) -> object:
        raise ValueError("no ffmpeg")

    monkeypatch.setattr(Comp.Record, "fromFileSystem", staticmethod(boom))

    comp = await sender.mediafile_to_comp(sender.MediaFile("audio", path=audio_path))

    assert isinstance(comp, Comp.File)
    assert comp.file == str(audio_path)


async def test_missing_image_file_degrades_to_text(tmp_path: Path) -> None:
    """M9 补全：非 base64 的 image 分支同样必须降级。

    ``Comp.Image.fromFileSystem`` 用 ``Path.resolve(strict=False)``，对缺失文件
    **不抛**，会产出指向不存在路径的 Image——到协议适配层发送时才失败，届时
    已无法按单媒体降级。显式 stat（``_ensure_readable``）让缺失文件在此就走
    OSError → 文本占位。
    """
    configure(plite_use_base64=False)
    missing = tmp_path / "gone.jpg"

    comp = await sender.mediafile_to_comp(sender.MediaFile("image", path=missing))

    assert isinstance(comp, Comp.Plain)
    assert comp.text == texts.MEDIA_FAILED.format("image")


async def test_missing_audio_file_degrades_to_text_without_base64(tmp_path: Path) -> None:
    """M9 补全：非 base64 的 audio 分支（Record.fromFileSystem 同样不校验存在性）。"""
    configure(plite_use_base64=False)
    missing = tmp_path / "gone.mp3"

    comp = await sender.mediafile_to_comp(sender.MediaFile("audio", path=missing))

    assert isinstance(comp, Comp.Plain)
    assert comp.text == texts.MEDIA_FAILED.format("audio")


async def test_present_media_file_is_not_disturbed(tmp_path: Path) -> None:
    """存在性校验不得误伤正常文件（反向钉住，防把 stat 加错位置）。"""
    configure(plite_use_base64=False)
    image = tmp_path / "ok.jpg"
    image.write_bytes(b"\xff\xd8\xff")

    comp = await sender.mediafile_to_comp(sender.MediaFile("image", path=image))

    assert isinstance(comp, Comp.Image)
