"""桥内事件终止契约：stop 后不得残留空链 result（第九轮回归）。

生产日志证据：每场解析会话固定多出一条空链「Prepare to send」并多触发一次
after_message_sent 钩子。根因：RespondStage 发送完毕已 clear_result，桥内
event.stop_event() 触发核心兜底 set_result（空链 MessageEventResult）；调度
器外层 for 循环不检查 is_stopped，耗尽 ProcessStage 后带着该空结果再次直达
RespondStage。

依赖真实 AstrBot 运行时（见 tests/requirements-test.txt）；``importorskip`` 仅
作本地未装时的优雅降级，静默退化由 ci.yml 的 skip 预算断言兜底。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytest.importorskip("astrbot")

from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot_plugin_parser_lite.main import _stop_event


def _make_event(*, group: bool = False) -> AstrMessageEvent:
    message_obj = AstrBotMessage()
    message_obj.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message_obj.self_id = "10000"
    message_obj.session_id = "10000"
    message_obj.message_id = "m1"
    message_obj.sender = MessageMember(user_id="u1", nickname="用户")
    message_obj.message = []
    message_obj.message_str = ""
    message_obj.raw_message = None
    if group:
        message_obj.group_id = "20000"
    platform_meta = PlatformMetadata(name="aiocqhttp", description="t", id="qq")
    return AstrMessageEvent(
        message_str="",
        message_obj=message_obj,
        platform_meta=platform_meta,
        session_id="10000" if not group else "20000",
    )


def test_core_stop_event_pins_empty_result_quirk() -> None:
    """钉住核心行为：result 为 None 时 stop_event 补设空链结果（桥补偿依据）。"""
    event = _make_event()

    event.stop_event()

    result = event.get_result()
    assert isinstance(result, MessageEventResult)
    assert result.chain == []


def test_bridge_stop_keeps_block_and_leaves_no_result() -> None:
    """桥内 _stop_event：阻断语义保留（is_stopped），不留任何 result。

    get_result() 为 None 时，调度器外层循环再达 RespondStage 会因空 result
    早退静默返回，不再产生空 Prepare；阻断由 _force_stopped 独立保证（核心
    文档：不依赖 _result，不会被 clear_result 重置）。
    """
    event = _make_event()

    _stop_event(event)

    assert event.is_stopped()
    assert event.get_result() is None


class _RecordingBot:
    """记录 set_msg_emoji_like 调用的最小 bot 桩。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call_action(self, action: str, **kwargs) -> None:
        self.calls.append({"action": action, **kwargs})


def _plugin_with_bot() -> tuple[Any, _RecordingBot]:
    """构造一个未走完整初始化、但具备 logger/_react 的插件实例。"""
    from astrbot_plugin_parser_lite.main import ParserLitePlugin

    plugin = ParserLitePlugin.__new__(ParserLitePlugin)
    plugin.logger = logging.getLogger("test.parser_lite")
    return plugin, _RecordingBot()


def _run_react(status: str) -> list[dict]:
    import asyncio

    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
        EMOJI_MAP,
    )

    plugin, bot = _plugin_with_bot()
    event = _make_event(group=True)  # 私聊不支持表情回应，会走早退分支
    event.bot = bot
    asyncio.run(plugin._react(event, status))
    expected = EMOJI_MAP["resolving" if status == "cancel" else status][0]
    assert len(bot.calls) == 1, f"_react({status}) 应恰好发起一次协议调用"
    assert bot.calls[0]["emoji_id"] == expected, f"_react({status}) 表情 ID 不符"
    return bot.calls


def test_react_cancel_clears_resolving_reaction() -> None:
    """用户拒绝懒下载问询时必须撤销 resolving 表情（2026-09-14 评审）：
    该分支此前直接 return，🔨 永久挂在消息上——既非成功也非失败，没有任何
    后续状态会覆盖它。撤销用同一个 resolving 表情 + set=False。
    """
    calls = _run_react("cancel")
    assert calls[0]["set"] is False, "cancel 未撤销表情（set 应为 False）"


def test_react_statuses_set_reaction() -> None:
    """resolving/done/fail 三个既有状态必须仍为 set=True（回归护栏）。"""
    for status in ("resolving", "done", "fail"):
        calls = _run_react(status)
        assert calls[0]["set"] is True, f"{status} 应设置表情而非撤销"
        assert calls[0]["action"] == "set_msg_emoji_like"


class _StubParser:
    """最小 parser 替身：仅需 aclose 可被调用/可控抛错。"""

    def __init__(self, *, raise_type_error: bool = False) -> None:
        self.aclose_calls = 0
        self._raise = raise_type_error

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self._raise:
            raise TypeError("curl_multi_cleanup(NULL) returned 1")


def _run_terminate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    aclose_raises: bool = False,
    shutdown_raises: bool = False,
    done: bool = False,
) -> tuple[int, bool]:
    """跑一次 terminate，返回 (shutdown_runtime 调用次数, 结束时标志值)。

    ``main`` 模块级状态在每个用例前后显式重置——``_runtime_shutdown_done``
    是全局单例语义，测试之间必须隔离。
    """
    import asyncio

    import astrbot_plugin_parser_lite.main as main_mod

    monkeypatch.setattr(main_mod, "_runtime_shutdown_done", done)
    calls = {"n": 0}

    async def fake_shutdown() -> None:
        calls["n"] += 1
        if shutdown_raises:
            raise TypeError("already closed")

    monkeypatch.setattr(main_mod, "shutdown_runtime", fake_shutdown)

    plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)
    plugin.logger = logging.getLogger("test.parser_lite")
    plugin._parser = _StubParser(raise_type_error=aclose_raises)
    asyncio.run(plugin.terminate())
    return calls["n"], main_mod._runtime_shutdown_done


def test_terminate_is_not_idempotent_unsafe_across_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次 terminate 失败后，第二次不得再关 runtime（2026-09-14 评审 #7）。

    这是标志存在的**真实**语义：shutdown_runtime 非幂等，二次 curl_multi_cleanup
    即崩。注意断言的是「跨调用的累计次数」而非单次返回值——单看一次调用时，
    置位在调用前还是调用后观察不到差别（调用后置位时标志照样已为 True），
    只有连续两次 terminate 才能暴露旧实现在 aclose 失败路径上的重复关闭。
    """
    import asyncio

    import astrbot_plugin_parser_lite.main as main_mod

    monkeypatch.setattr(main_mod, "_runtime_shutdown_done", False)
    calls = {"n": 0}

    async def fake_shutdown() -> None:
        calls["n"] += 1

    monkeypatch.setattr(main_mod, "shutdown_runtime", fake_shutdown)

    def _fresh_plugin() -> Any:
        plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)
        plugin.logger = logging.getLogger("test.parser_lite")
        # 两个实例都 aclose 失败，模拟 curl_cffi 的重复关闭
        plugin._parser = _StubParser(raise_type_error=True)
        return plugin

    asyncio.run(_fresh_plugin().terminate())
    asyncio.run(_fresh_plugin().terminate())

    assert calls["n"] == 1, (
        f"shutdown_runtime 被调用 {calls['n']} 次；aclose 失败路径下旧实现"
        "会重复关闭非幂等的 runtime"
    )


def test_terminate_skips_shutdown_when_already_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """标志已置位时不得再调 shutdown_runtime（重载场景的核心语义）。"""
    n, flag = _run_terminate(monkeypatch, done=True)
    assert n == 0, "已关闭的 runtime 不应被二次关闭（curl_cffi 非幂等）"
    assert flag is True


def test_terminate_aclose_failure_does_not_skip_runtime_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aclose 抛 TypeError 不应吞掉 shutdown_runtime（两个清理阶段互不阻塞）。

    原实现的单个 try 块让 aclose 的异常直接跳过 shutdown_runtime，令 runtime
    泄漏（进程退出前 DOWNLOADER 线程池与 curl 句柄不释放）。
    """
    n, flag = _run_terminate(monkeypatch, aclose_raises=True)
    assert n == 1, "aclose 异常后 shutdown_runtime 仍须执行"
    assert flag is True
