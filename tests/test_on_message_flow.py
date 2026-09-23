"""on_message 事件流端到端冒烟：主流程接线契约。

单点契约（_stop_event / _react / sender 链 / render 链 / URL 抽取）各有
专属用例；本模块串起「事件进 → URL 抽取 → 匹配 → 解析 → 发送 → 表情
回应 → 阻断」完整主流程，钉住组件之间的接线：配置读取、生成器传播、
失败分支收尾、懒下载取消路径。

手法与 test_event_stop 同源：真实 AstrMessageEvent + ``__new__`` 构造的
插件实例 + 最小 parser 替身；``send_result`` 用真实实现（ParseResult 只含
文本内容，零网络面）。``filter.event_message_type`` 装饰器原样返回函数，
on_message 可直接以 async generator 驱动。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("astrbot")

import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_parser_lite import main as main_mod
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import configure
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
    EMOJI_MAP,
    PlatformEnum,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    Author,
    ParseResult,
    Platform,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.exception import (
    ParseException,
    TipException,
)

URL = "https://bbs.hupu.com/12345678.html"


class _RecordingBot:
    """记录 set_msg_emoji_like 调用的最小 bot 桩（与 test_event_stop 同构）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_action(self, action: str, **kwargs) -> None:
        self.calls.append({"action": action, **kwargs})


def _make_event(
    text: str = "",
    *,
    sender_id: str = "u1",
    self_id: str = "10000",
    group: bool = True,
) -> AstrMessageEvent:
    message_obj = AstrBotMessage()
    message_obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message_obj.self_id = self_id
    message_obj.session_id = "10000"
    message_obj.message_id = "m1"
    message_obj.sender = MessageMember(user_id=sender_id, nickname="用户")
    message_obj.message = []
    message_obj.message_str = text
    message_obj.raw_message = None
    if group:
        message_obj.group_id = "20000"
    platform_meta = PlatformMetadata(name="aiocqhttp", description="t", id="qq")
    event = AstrMessageEvent(
        message_str=text,
        message_obj=message_obj,
        platform_meta=platform_meta,
        session_id="20000" if group else "10000",
    )
    event.bot = _RecordingBot()
    return event


def _make_result() -> ParseResult:
    return ParseResult(
        platform=Platform(name=PlatformEnum.HUPU, display_name="虎扑"),
        author=Author(name="作者"),
        url=URL,
        content=["正文"],
        title="标题",
    )


class _FakeParser:
    """最小 parser 替身：match/parse 行为按构造参数控制。"""

    def __init__(
        self,
        result: ParseResult | None = None,
        *,
        parse_exc: Exception | None = None,
    ) -> None:
        self._result = result if result is not None else _make_result()
        self._parse_exc = parse_exc
        self.matched_urls: list[str] = []
        self.parsed_urls: list[str] = []

    def match(self, url: str) -> object:
        self.matched_urls.append(url)
        return object()

    async def parse(self, url: str) -> ParseResult:
        self.parsed_urls.append(url)
        if self._parse_exc is not None:
            raise self._parse_exc
        return self._result

    async def aclose(self) -> None:
        pass


def _make_plugin(
    parser: _FakeParser, config: dict[str, Any] | None = None
) -> main_mod.ParserLitePlugin:
    plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)
    plugin.logger = logging.getLogger("test.parser_lite")
    plugin.config = {"plite_render": False, "plite_verbose_error": False, **(config or {})}
    plugin._parser = parser
    return plugin


@pytest.fixture(autouse=True)
def _vendor_config_baseline():
    """每个用例复位共享 vendor 配置（pconfig 是进程级单例，用例间必须隔离）。"""
    configure(
        plite_lazy_download=False,
        plite_blacklist_users=[],
        plite_append_url=False,
        plite_embed_url=False,
        plite_need_forward_contents=False,
    )
    yield


async def _drive(plugin: main_mod.ParserLitePlugin, event: AstrMessageEvent) -> list[Any]:
    return [chunk async for chunk in plugin.on_message(event)]


def _emoji_ids(bot: _RecordingBot) -> list[tuple[str, bool]]:
    return [(call["emoji_id"], call["set"]) for call in bot.calls]


# ---- 主流程 ----


async def test_happy_path_yields_media_chain_and_done_reaction() -> None:
    parser = _FakeParser()
    plugin = _make_plugin(parser)
    event = _make_event(f"看看这个 {URL}")

    yielded = await _drive(plugin, event)

    assert parser.matched_urls == [URL], "抽取的 URL 未原样交给 vendor match"
    assert parser.parsed_urls == [URL]
    # sender 真实实现：标题 + 正文走平铺链（need_forward_contents=False 且短文本）
    plains = [c.text for result in yielded for c in result.chain if isinstance(c, Comp.Plain)]
    assert any("标题" in t for t in plains) and any("正文" in t for t in plains)
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot) == [
        (EMOJI_MAP["resolving"][0], True),
        (EMOJI_MAP["done"][0], True),
    ], "表情回应序列应为 resolving → done（QQ 族平台用数字表情 ID）"
    assert event.is_stopped(), "成功路径必须阻断后续插件"
    assert event.get_result() is None, "_stop_event 不得残留空链 result"


async def test_render_path_prepends_card_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    card = tmp_path / "card.jpeg"
    card.write_bytes(b"jpeg-bytes")

    async def fake_render(result: object, renderer: object) -> Path:
        return card

    monkeypatch.setattr(main_mod, "cache_or_render_image", fake_render)
    plugin = _make_plugin(_FakeParser(), config={"plite_render": True})
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    first_chain = yielded[0]
    assert isinstance(first_chain, MessageEventResult)
    assert any(isinstance(c, Comp.Image) for c in first_chain.chain), "渲染开启时首链应为卡片图"


# ---- 失败分支 ----


async def test_parse_exception_silent_by_default() -> None:
    parser = _FakeParser(parse_exc=ParseException("不支持的链接"))
    plugin = _make_plugin(parser)
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert yielded == [], "verbose_error 关闭时 ParseException 不得向用户发消息"
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot) == [
        (EMOJI_MAP["resolving"][0], True),
        (EMOJI_MAP["fail"][0], True),
    ]
    assert event.is_stopped()


async def test_parse_exception_verbose_reaches_user() -> None:
    parser = _FakeParser(parse_exc=ParseException("不支持的链接"))
    plugin = _make_plugin(parser, config={"plite_verbose_error": True})
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert len(yielded) == 1
    result = yielded[0]
    assert isinstance(result, MessageEventResult)
    assert any("不支持的链接" in c.text for c in result.chain if isinstance(c, Comp.Plain))


async def test_tip_exception_bypasses_verbose_gate() -> None:
    """TipException 的 message 必达用户（上游 matcher 语义），不受 verbose 门控。"""
    parser = _FakeParser(parse_exc=TipException("up 主在黑名单中"))
    plugin = _make_plugin(parser, config={"plite_verbose_error": False})
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert len(yielded) == 1, "TipException 文案必须送达用户"
    result = yielded[0]
    assert isinstance(result, MessageEventResult)
    assert any("黑名单" in c.text for c in result.chain if isinstance(c, Comp.Plain))
    assert event.is_stopped()


async def test_unexpected_exception_fails_closed() -> None:
    parser = _FakeParser(parse_exc=RuntimeError("boom"))
    plugin = _make_plugin(parser)
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert yielded == [], "未知异常不得向用户泄露堆栈"
    assert event.is_stopped(), "未知异常同样必须阻断事件"
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot)[-1] == (EMOJI_MAP["fail"][0], True)


# ---- 入口过滤 ----


async def test_message_without_url_has_no_side_effects() -> None:
    parser = _FakeParser()
    plugin = _make_plugin(parser)
    event = _make_event("今天天气不错")

    yielded = await _drive(plugin, event)

    assert yielded == []
    assert parser.matched_urls == []
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert bot.calls == [], "无 URL 消息不应触发任何表情回应"
    assert not event.is_stopped(), "无 URL 消息不得阻断其他插件"


async def test_blacklisted_user_is_ignored() -> None:
    configure(plite_blacklist_users=["u1"])
    parser = _FakeParser()
    plugin = _make_plugin(parser)
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert yielded == []
    assert parser.matched_urls == []
    assert not event.is_stopped()


async def test_self_message_is_ignored() -> None:
    parser = _FakeParser()
    plugin = _make_plugin(parser)
    event = _make_event(URL, sender_id="10000", self_id="10000")

    yielded = await _drive(plugin, event)

    assert yielded == []
    assert parser.matched_urls == []


async def test_match_failure_skips_to_next_candidate() -> None:
    """match 抛 ParseException 的候选被跳过；全部不匹配则无副作用。"""

    class _NoMatchParser(_FakeParser):
        def match(self, url: str) -> object:
            self.matched_urls.append(url)
            raise ParseException("文本中没有可解析的内容")

    parser = _NoMatchParser()
    plugin = _make_plugin(parser)
    event = _make_event("https://unknown.example.com/x 和 https://b23.tv/AbCdEf")

    yielded = await _drive(plugin, event)

    assert yielded == []
    assert len(parser.matched_urls) == 2, "首个候选不匹配后应继续尝试下一个"
    assert parser.parsed_urls == []
    assert not event.is_stopped()


# ---- 懒下载取消路径 ----


async def test_lazy_download_rejection_cancels_resolving_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(plite_lazy_download=True)

    async def fake_ask(event: AstrMessageEvent) -> bool:
        return False

    monkeypatch.setattr(main_mod, "ask_lazy_download", fake_ask)
    plugin = _make_plugin(_FakeParser(), config={"plite_render": False})
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert yielded == [], "用户拒绝懒下载后不得再发送媒体"
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot) == [
        (EMOJI_MAP["resolving"][0], True),
        (EMOJI_MAP["resolving"][0], False),
    ], "拒绝路径必须撤销 resolving 表情（cancel 语义），否则 🔨 永久残留"
    assert event.is_stopped()
