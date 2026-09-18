"""插件入口：AstrBot v4 事件桥（工单 03/06/07）。

职责边界（ACL R1）：本模块只做「事件抽取 → vendor 权威匹配/解析 → 翻译发
送」，不实现任何平台解析规则。vendor 是唯一解析权威，桥内 Fan-in=1（R2，
由契约测试 test_import_contract.py 机器化守护）。

发送顺序与上游 main 分支一致：卡片渲染 → 即时媒体/合并转发；
表情回应（resolving/done/fail）按上游 message_reaction 语义移植
（私聊跳过、QQ 族平台用数字表情 ID），失败仅告警。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path as SyncPath
from typing import Any

import astrbot.api.message_components as Comp
from anyio import Path as AnyioPath
from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# vendor 的 path.py 在「导入时」读取 PARSER_LITE_BASE_DIR，因此必须先落好
# 环境变量再导入任何 vendor 依赖模块（下方 E402 豁免即为此序）。
os.environ.setdefault(
    "PARSER_LITE_BASE_DIR",
    str(SyncPath(get_astrbot_plugin_data_path()) / "astrbot_plugin_parser_lite"),
)

from .bridge import render_params, texts
from .bridge.config_sync import sync_import_time_config
from .bridge.gen_config import BRIDGE_DEFAULTS, BRIDGE_FIELDS, VENDOR_FIELDS
from .bridge.render import cache_or_render_image
from .bridge.sender import ask_lazy_download, send_result, set_video_file_threshold_mb
from .bridge.ssrf import install_ssrf_guard
from .bridge.vendor_patches import apply_vendor_patches
from .vendor.nonebot_plugin_parser_lite import Parser, configure, shutdown_runtime
from .vendor.nonebot_plugin_parser_lite.config import pconfig
from .vendor.nonebot_plugin_parser_lite.constants import EMOJI_MAP
from .vendor.nonebot_plugin_parser_lite.exception import ParseException, TipException

URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；！？】》（【]+", re.IGNORECASE)

# 分享文案常把链接与标点直接相连（"…（分享了视频）"、"链接。"、"(见)"），
# 而排除集只列了独占性强的 CJK 标点，抽取结果可能带尾随字符。带脏字符的
# URL 交给上游 match 会不匹配或把脏字符带进解析目标，用户侧表现为「发了
# 链接没反应」（2026-09-14 双轴评审实测：尾随句点的链接完全解析失败）。
_URL_TRAILING_STRIP = ".,;:!?、，。；：！？\"'】》>"
# 成对括号（含全角）：仅当 URL 内该符号未配对时才视为尾随标点——维基类
# 路径 "/wiki/Foo_(bar)" 的右括号属路径本体，裁剪会破坏合法 URL；反过来
# "…（分享了视频）" 的右括号无对应左括号，必须裁掉。左括号字符同时计入
# 配对计数，否则上一步剥离标点时会把左括号删掉导致计数失真。
_URL_BRACKET_PAIRS = (("(", ")"), ("（", "）"), ("[", "]"), ("{", "}"))


def _strip_url_trailing(url: str) -> str:
    """裁掉分享文案粘在链接尾部的标点（括号按配对判定，幂等）。

    已知取舍（2026-09-17 评审 L9，**刻意保留**）：URL 本体无左括号却以右
    括号结尾时（如 ``http://x.com/a)``）会被裁成 ``http://x.com/a``。这与
    ``(见 https://b23.tv/AbCdEf)`` 这类「正文括号包住链接」的常见形态在
    URL 字符串层面**不可区分**（两者都是「无左括号 + 尾部右括号」），而后
    者远多于前者，故按后者处理。配对括号（``/wiki/Foo_(bar)``）照旧保留。
    """
    while url:
        stripped = url
        for opener, closer in _URL_BRACKET_PAIRS:
            while stripped.endswith(closer) and stripped.count(opener) < stripped.count(closer):
                stripped = stripped[:-1]
        stripped = stripped.rstrip(_URL_TRAILING_STRIP)
        if stripped == url:
            break
        url = stripped
    return url


# vendor 的 runtime 单例（DOWNLOADER 等）跨插件实例共享：AstrBot 保存配置会
# 重载插件实例，旧实例已关闭的 runtime 不应被后续 terminate 再次关闭
# （curl_cffi 的 aclose 非幂等，二次关闭即崩——容器实测复现）
_runtime_shutdown_done = False

# 表情回应的 QQ 族平台（对应上游 message_reaction 的 onebot11/qq/milky
# 适配器集合，milky 无 AstrBot 对应）：用 EMOJI_MAP 第一槽数字表情 ID，
# 其余平台用第二槽 Unicode emoji
_REACTION_QQ_PLATFORMS = frozenset({"aiocqhttp", "qq_official", "qq"})

# 上游 cache_or_render_image：渲染长图超过该体积按文件消息发送（QQ 图片受限）；
# 阈值为上游 main 注入值（渲染参数注入层，bridge/render_params.py 生成物）
_RENDER_FILE_THRESHOLD_BYTES = render_params.OVERSIZED_IMAGE_BYTES

# JSON 卡片里优先取 URL 的字段；其余字符串叶子走兜底正则
_JSON_URL_KEYS = frozenset(
    {
        "jump_url",
        "qqdocurl",
        "url",
        "native_url",
        "middleware_url",
        "previewurl",
        "clickurl",
        "appid_url",
        "config_forward_url",
    },
)


def _urls_from_json(data: Any) -> list[str]:
    """抽取 JSON 卡片里的 URL：优先已知协议字段，其余字符串正则兜底。

    显式栈遍历（先序，与递归实现逐项同序），**无递归深度上限**：卡片嵌套
    深度由远端消息决定，不可控。此前 `depth > 8` 的递归截断会静默丢弃第 9
    层及更深的链接（QQ 卡片实测第 9 层返回 []），而直接放开上限又会把病态
    深层输入变成 RecursionError（2026-09-17 评审 L8）。

    注意：与递归实现一致，字符串**仅在 dict 值位置**被抽取——list 里的裸
    字符串不是 URL 候选（上游卡片把链接放在具名协议字段里）。
    """
    urls: list[str] = []
    stack: list[tuple[bool, Any]] = [(False, data)]
    while stack:
        is_emit, payload = stack.pop()
        if is_emit:
            urls.extend(payload)
            continue
        node = payload
        if isinstance(node, dict):
            # 逆序入栈，保证与递归先序访问逐项同序（URL 优先级顺序影响解析目标）
            for key, value in reversed(list(node.items())):
                if isinstance(value, str):
                    if key in _JSON_URL_KEYS and value.startswith(("http://", "https://")):
                        stack.append((True, [value]))
                    else:
                        stack.append((True, URL_PATTERN.findall(value)))
                else:
                    stack.append((False, value))
        elif isinstance(node, list):
            stack.extend((False, item) for item in reversed(node))
    return urls


def _stop_event(event: AstrMessageEvent) -> None:
    """阻断事件传播并清掉 stop_event 的兜底空结果。

    RespondStage 发送完毕已 clear_result，此时 stop_event 会补设一个空链
    MessageEventResult；调度器外层 for 循环不检查 is_stopped，会带着该空
    结果再次直达 RespondStage——生产表现为每场解析多一条空 Prepare 日志
    并多触发一次 after_message_sent 钩子。阻断由 _force_stopped 独立保证
    （核心文档：不依赖 _result，不会被 clear_result 重置）。
    """
    event.stop_event()
    event.clear_result()


class ParserLitePlugin(star.Star):
    """nonebot-plugin-parser-lite（vendored snapshot）的 AstrBot 桥。"""

    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        # 只透传上游识别的字段：不依赖 vendor pydantic extra 策略（上游改
        # extra="forbid" 时存量陈旧键也不会炸加载）；桥自有键不在 VENDOR_FIELDS
        configure(**{k: v for k, v in config.items() if k in VENDOR_FIELDS})
        # vendor 类体在导入期快照的配置点（MAX_RETRIES / 两平台 cookies）
        # 在上游环境中导入时配置已就绪；桥的 configure 晚于导入，须回写
        sync_import_time_config()
        # 上游 roll 改名/移除字段后，存量 WebUI 配置键会静默失效——启动时即提醒
        stale = sorted(
            k
            for k in config
            if k.startswith("plite_") and k not in BRIDGE_FIELDS and k not in VENDOR_FIELDS
        )
        if stale:
            self.logger.warning("以下配置键已不被上游识别（可能已随上游同步改名或移除）：%s", stale)
        threshold_mb = int(self._cfg("plite_video_file_threshold_mb"))
        set_video_file_threshold_mb(threshold_mb)
        self._parser = Parser()
        install_ssrf_guard()
        # vendor 运行态缺陷的桥内注入补丁（kuwo 参数名 / buff、hupu 视频块
        # decompose 截断迭代），幂等；详见 bridge/vendor_patches.py 模块文档
        apply_vendor_patches()
        self.logger.info(
            "parser_lite 桥就绪：渲染=%s，懒下载=%s，阈值=%sMB",
            self._cfg("plite_render"),
            pconfig.lazy_download,
            threshold_mb,
        )

    def _cfg(self, key: str) -> Any:
        """桥自有配置读取：缺省回落 BRIDGE_DEFAULTS（键字面量只写一次）。"""
        return self.config.get(key, BRIDGE_DEFAULTS[key])

    async def terminate(self) -> None:
        global _runtime_shutdown_done
        # 顺序与置位时机都关键：shutdown_runtime 非幂等（curl_cffi
        # curl_multi_cleanup 二次调用即崩），因此**先**置位再调用——若在调用
        # 之后置位，一次 TypeError 中断会让标志留在 False，后续实例的
        # terminate 会对已拆解的 runtime 再关一次。aclose 与 shutdown_runtime
        # 分属两个清理阶段，aclose 失败不应吞掉 runtime 关闭（各自的异常各自
        # 降级告警，均不向宿主传播）。
        try:
            await self._parser.aclose()
        except Exception as e:
            # 清理路径容错：curl_cffi aclose 非幂等（curl_multi_cleanup(None)），
            # 任何残留的二次关闭在此降级告警。只捕 TypeError 与上方 docstring
            # 承诺的「均不向宿主传播」不符 —— 非 TypeError 会穿透并跳过
            # shutdown_runtime（scheduler / DOWNLOADER 泄漏），2026-09-18 复核
            self.logger.warning("parser_lite 运行时清理忽略关闭异常：%s", e)
        if not _runtime_shutdown_done:
            _runtime_shutdown_done = True
            try:
                await shutdown_runtime()
            except Exception as e:
                self.logger.warning("parser_lite shutdown_runtime 忽略关闭异常：%s", e)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        sender_id = str(event.get_sender_id())
        if sender_id == str(event.get_self_id()) or sender_id in set(pconfig.blacklist_users):
            return

        url = self._extract_url(event)
        if url is None:
            return

        await self._react(event, "resolving")
        try:
            result = await self._parser.parse(url)
        except TipException as exc:
            # 上游 matcher：提示性异常（如 up 主黑名单）的 message 必达用户，
            # 不受 verbose_error 门控
            self.logger.warning("解析失败: %s", exc.message)
            yield event.plain_result(exc.message)
            await self._fail(event)
            return
        except ParseException as exc:
            self.logger.warning("解析失败: %s", exc.message)
            await self._fail(event)
            if self._cfg("plite_verbose_error"):
                yield event.plain_result(f"解析失败：{exc.message}")
            return
        except Exception:
            self.logger.exception("解析异常")
            await self._fail(event)
            return

        try:
            if self._cfg("plite_render"):
                yield event.chain_result(await self._build_card_chain(result))
            if pconfig.lazy_download and not await ask_lazy_download(event):
                # 用户拒绝：撤销 resolving 表情（否则 🔨 永久残留），已发的
                # 卡片保留——用户主动拒绝的是媒体下载而非卡片
                await self._react(event, "cancel")
                _stop_event(event)
                return
            async for chain in send_result(result, event):
                yield event.chain_result(chain)
        except Exception:
            self.logger.exception("发送解析结果异常")
            await self._fail(event)
            return

        await self._react(event, "done")
        _stop_event(event)

    async def _fail(self, event: AstrMessageEvent) -> None:
        """统一失败收尾：表情回应 + 阻断后续插件。"""
        await self._react(event, "fail")
        _stop_event(event)

    def _extract_url(self, event: AstrMessageEvent) -> str | None:
        """按优先级抽取候选 URL 并交给 vendor 匹配：纯文本 → JSON 卡片。"""
        candidates: list[str] = []
        if event.message_str and event.message_str.strip():
            candidates.append(event.message_str)
        for comp in event.get_messages():
            if isinstance(comp, Comp.Json):
                candidates.extend(_urls_from_json(comp.data))
        if not candidates:
            return None

        for text in candidates:
            for raw in URL_PATTERN.findall(text):
                url = _strip_url_trailing(raw)
                if not url:
                    continue
                try:
                    # 同步调用是有意为之：vendor match 是纯正则匹配（遍历
                    # 各 parser 的关键字 pattern，无 I/O），实测 ~39µs/次
                    # （454 字符输入，2026-09-14），仅为一次网络请求的
                    # 0.04%——投线程池的调度开销大于工作量本身。
                    self._parser.match(url)
                except ParseException:
                    continue
                except Exception:
                    self.logger.exception("匹配候选 URL 失败: %s", url)
                    continue
                return url
        return None

    async def _build_card_chain(self, result: Any) -> list[Comp.BaseMessageComponent]:
        """渲染卡片长图；失败降级为占位文本（与上游 render_messages 等价）。"""
        try:
            path = await cache_or_render_image(result, renderer=self)
            # 上游 cache_or_render_image 尾段：超过 5MB 的渲染图按文件消息
            # 发送（组件构造差异留在桥内）
            render_path = AnyioPath(str(path))
            if (await render_path.stat()).st_size >= _RENDER_FILE_THRESHOLD_BYTES:
                chain: list[Comp.BaseMessageComponent] = [
                    Comp.File(name=render_path.name, file=str(render_path))
                ]
            else:
                chain = [Comp.Image.fromFileSystem(str(path))]
        except Exception as exc:
            self.logger.warning("渲染卡片失败，降级文本: %r", exc)
            chain = [Comp.Plain(texts.RENDER_FAILED)]
        if pconfig.append_url:
            # 上游 render_messages：两段 URL 以换行合并为单条文本
            urls = (result.display_url, result.repost_display_url)
            chain.append(Comp.Plain("\n".join(url for url in urls if url)))
        if pconfig.embed_url and result.embed_url:
            chain.append(Comp.Plain(texts.ONLINE_PLAY + result.embed_url))
        return chain

    async def _react(self, event: AstrMessageEvent, status: str) -> None:
        """表情回应（上游 UniHelper.message_reaction 等价移植）；失败只告警。

        ``status="cancel"`` 表示撤销此前的 resolving 表情（用户拒绝了懒下载
        问询——既非成功也非失败，若不作终态处理 🔨 会永久挂在消息上）。
        """
        if event.is_private_chat():
            # 上游语义：私聊不支持消息回应，告警后跳过（不发起协议调用）
            self.logger.warning("私聊消息不支持表情回应，跳过")
            return
        bot = getattr(event, "bot", None)
        message_id = getattr(event.message_obj, "message_id", None)
        if bot is None or message_id is None:
            return
        if status == "cancel":
            emoji_onebot, emoji_generic = EMOJI_MAP["resolving"]
            is_set = False
        else:
            emoji_onebot, emoji_generic = EMOJI_MAP[status]
            is_set = True
        emoji = (
            emoji_onebot if event.get_platform_name() in _REACTION_QQ_PLATFORMS else emoji_generic
        )
        try:
            await asyncio.wait_for(
                bot.call_action(
                    "set_msg_emoji_like",
                    message_id=message_id,
                    emoji_id=emoji,
                    emoji_type="1",
                    set=is_set,
                ),
                timeout=5,
            )
        except Exception as exc:
            self.logger.warning("设置表情回应失败（忽略）: %r", exc)
