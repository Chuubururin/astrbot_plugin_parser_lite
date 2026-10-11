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

from pathlib import Path
from typing import Any

import httpx
import pytest
from astrbot.api import logger as _test_logger

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


class _RecordingLogger:
    """记录 warning/exception 的日志桩：把「落哪个日志级别」变成可断言的事实。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.exceptions: list[str] = []

    def warning(self, msg: str, *args: Any) -> None:
        self.warnings.append(msg % args if args else msg)

    def exception(self, msg: str, *args: Any) -> None:
        self.exceptions.append(msg % args if args else msg)

    def __getattr__(self, name: str) -> Any:
        return lambda *_a: None


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
    plugin.logger = _test_logger
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


def _upstream_status_error(
    status_code: int = 500, path: str = "/api/getSongInfo"
) -> httpx.HTTPStatusError:
    """构造平台 API 的 raise_for_status 产物（vendor 各 parser 直接抛裸 httpx 异常）。"""
    request = httpx.Request("POST", f"https://nextmusic.toubiec.cn{path}")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )


async def test_upstream_http_error_is_classified_as_warning() -> None:
    """上游接口的 4xx/5xx 是上游状态，不是桥侧未知异常：WARN 一行，不落 ERROR 全栈。"""
    plugin = _make_plugin(_FakeParser(parse_exc=_upstream_status_error()))
    log = _RecordingLogger()
    plugin.logger = log
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert log.exceptions == [], "上游 HTTP 错误被当成未知异常处理（ERROR + 完整堆栈）"
    assert len(log.warnings) == 1
    assert "nextmusic.toubiec.cn" in log.warnings[0]
    assert "500" in log.warnings[0]
    assert yielded == [], "verbose_error 关闭时上游 HTTP 错误不得向用户发消息"
    assert event.is_stopped()
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot)[-1] == (EMOJI_MAP["fail"][0], True)


async def test_upstream_detail_truncates_long_path() -> None:
    """端点摘要的路径部分来自群里贴的链接，长度不可控——记日志前必须截断。"""
    long_path = "/api/getSongInfo/" + "z" * 400 + "TAILEND"
    plugin = _make_plugin(_FakeParser(parse_exc=_upstream_status_error(path=long_path)))
    log = _RecordingLogger()
    plugin.logger = log

    await _drive(plugin, _make_event(URL))

    assert len(log.warnings) == 1
    line = log.warnings[0]
    assert "TAILEND" not in line, "超长路径未截断，整段写进日志"
    assert len(line) < 200, f"日志行随输入无限增长：len={len(line)}"


async def test_upstream_error_user_text_has_no_path_echo() -> None:
    """会话侧只回状态码：路径可能整段来自成员贴的链接，不得当作机器人发言回显。"""
    plugin = _make_plugin(
        _FakeParser(parse_exc=_upstream_status_error(502, path="/api/echo-MARKER-never-echo")),
        config={"plite_verbose_error": True},
    )
    plugin.logger = _RecordingLogger()

    yielded = await _drive(plugin, _make_event(URL))

    assert len(yielded) == 1, "上游接口失败对用户完全静默"
    result = yielded[0]
    assert isinstance(result, MessageEventResult)
    texts = [c.text for c in result.chain if isinstance(c, Comp.Plain)]
    assert any("502" in t for t in texts)
    assert all("echo-MARKER-never-echo" not in t for t in texts), (
        f"把成员可控的 URL 路径回显进会话：{texts}"
    )


# ---- 即时媒体链的直发接线 ----


async def _two_chain_send_result(*_a: Any) -> Any:
    """替身 send_result：先产出一条单组件视频链，再产出一条文本链。"""
    yield [Comp.Video(file="file:///tmp/v.mp4")]
    yield [Comp.Plain(text="正文")]


async def test_media_chain_is_direct_sent_and_never_yielded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """媒体链接管后不得再 yield——否则宿主二次发送，同一条视频发两遍。"""
    sent: list[list[Any]] = []

    async def spy(event: Any, comps: list[Any], **_kw: Any) -> bool:
        sent.append(comps)
        return True

    monkeypatch.setattr(main_mod, "send_result", _two_chain_send_result)
    monkeypatch.setattr(main_mod, "send_media_chain_with_retry", spy)

    plugin = _make_plugin(_FakeParser())
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert len(sent) == 1 and isinstance(sent[0][0], Comp.Video), "视频链未走直发"
    assert len(yielded) == 1, "被直发的媒体链又 yield 了一次（会重复发送）"
    result = yielded[0]
    assert isinstance(result, MessageEventResult)
    assert any(c.text == "正文" for c in result.chain if isinstance(c, Comp.Plain))
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot)[-1] == (EMOJI_MAP["done"][0], True)


async def test_media_send_exhaustion_ends_with_fail_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试预算用尽：表情终态是 fail 而非 done，事件同样被阻断。"""

    async def spy(event: Any, comps: list[Any], **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(main_mod, "send_result", _two_chain_send_result)
    monkeypatch.setattr(main_mod, "send_media_chain_with_retry", spy)

    plugin = _make_plugin(_FakeParser())
    event = _make_event(URL)

    yielded = await _drive(plugin, event)

    assert len(yielded) == 1, "文本链仍应送达"
    assert event.is_stopped()
    bot = event.bot
    assert isinstance(bot, _RecordingBot)
    assert _emoji_ids(bot)[-1] == (EMOJI_MAP["fail"][0], True), (
        "媒体没发出去却记 done——表情终态与事实不符"
    )


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
