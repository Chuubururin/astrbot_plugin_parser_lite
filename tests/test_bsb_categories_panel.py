"""空降分类名的面板输入面守护（桥侧归一 + vendor 出网形状哨兵）。

背景：上游 issue #379 已修掉出网编码（`json.dumps`），但**配置面是桥给的自由文本**，
上游不会替它收口。服务端对 `categories` 是 all-or-nothing——一个带空格或大写的名字
让整批裁切失效（实测 400 `categories parameter does not match format requirements.`），
而不是只有那个名字失效；`vendor` 侧 `plite_bsb_categories` 又没有 validator
（`config.py:131`），错值要等到解析时才炸。所以归一只能在透传前做。

本文件两类断言：
1. 归一函数的行为矩阵，以及「`__init__` 真的调了它、且在 `configure()` 之前」；
2. 出网形状哨兵——打在**被 httpx 编码之后**的 URL 上，不是打在 params 字典上。
   缺陷恰好发生在 dict→query 的编码环节，断字典看不出重复参数；这条同时是
   上游若在下一次 roll 里改回裸 list 时的回归警报。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("astrbot")

from astrbot_plugin_parser_lite import main as main_mod


def _vendor_sponsorblock():
    """延迟导入：vendor 包必须在桥的运行时上下文里取，避免导入期副作用顺序问题。"""
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.bilibili import (
        sponsorblock as module,
    )

    return module


# --------------------------------------------------------------------------
# 归一行为矩阵


def test_bsb_categories_are_stripped_casefolded_and_deduped() -> None:
    """服务端只认小写字母/数字/下划线/连字符；归一后重复的项要并掉而不是当成被丢弃。"""
    sanitize = main_mod._sanitize_bsb_categories

    assert sanitize(["  Sponsor ", "INTRO", "music_Offtopic"]) == (
        ["sponsor", "intro", "music_offtopic"],
        [],
    )
    # 归一后重复 → 静默去重，dropped 必须为空（否则日志会谎报"丢弃"）
    assert sanitize(["sponsor", "Sponsor", " sponsor "]) == (["sponsor"], [])
    assert sanitize([]) == ([], [])


def test_malformed_bsb_category_names_are_dropped_and_named() -> None:
    """非法项要**原样**回显给日志，否则运维无从知道用户写错了什么。"""
    sanitize = main_mod._sanitize_bsb_categories

    kept, dropped = sanitize(["sponsor", "self promo", "", "  ", "中文", "intro"])
    assert kept == ["sponsor", "intro"]
    assert dropped == ["self promo", "", "  ", "中文"]


def test_non_list_bsb_categories_is_no_categories_not_per_character() -> None:
    """形态异常按「没有名单」处理。

    把裸字符串当列表用会逐字符匹配（`"sponsor"` 被拆成 s/p/o...），既裁不出东西
    也不报错——与黑名单同款失效形态。
    """
    sanitize = main_mod._sanitize_bsb_categories

    assert sanitize("sponsor") == ([], ["sponsor"])
    assert sanitize(None) == ([], [])
    assert sanitize(3) == ([], [3])
    assert sanitize({"sponsor": True}) == ([], [{"sponsor": True}])


def test_normalized_categories_reach_vendor_runtime() -> None:
    """归一结果必须真的落到 `pconfig`，且能与服务端返回的分类名比中。

    `sponsorblock.py` 把服务端的 `category` 做了 `casefold()` 再与配置列表比较，
    所以只要发出去/存下来的那份没归一，即便请求成功也会「无异常、只是没裁」。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import configure
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig

    # pconfig 是进程级单例：本用例写了就得自己擦干净，否则 api_base 会漏给后续用例
    before = (pconfig.bsb_api_base, list(pconfig.bsb_categories))
    try:
        kept, _ = main_mod._sanitize_bsb_categories([" Intro ", "SPONSOR"])
        configure(**{"plite_bsb_api_base": "https://bsbsb.top", "plite_bsb_categories": kept})

        assert pconfig.bsb_categories == ["intro", "sponsor"]
        # 服务端返回的段（已 casefold）能在本地过滤里命中
        server_side_category = "Intro".casefold()
        assert server_side_category in pconfig.bsb_categories
    finally:
        configure(**{"plite_bsb_api_base": before[0], "plite_bsb_categories": before[1]})
        assert pconfig.bsb_api_base == before[0]


def test_init_normalizes_categories_before_handing_them_to_vendor(monkeypatch) -> None:
    """真装配点断言：`__init__` 必须把**归一后**的列表交给 `configure()`。

    手法与 test_plugin_init 同源（`__new__` + 显式 `__init__` + 协作者全部替身）。
    刻意不写成「在 main.py 里搜子串再比先后」：那种断言对「调用被 `if False and …`
    短路」「整块被注释」这类失效完全无感——首版就是假守卫，注入变异后仍然 7 绿。
    """
    seen: dict[str, Any] = {}

    def _capture_configure(**kwargs: Any) -> None:
        seen.update(kwargs)

    plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)
    monkeypatch.setattr(main_mod, "rearm_runtime", lambda: None)
    monkeypatch.setattr(main_mod, "configure", _capture_configure)
    monkeypatch.setattr(main_mod, "sync_import_time_config", lambda: None)
    monkeypatch.setattr(main_mod, "Parser", lambda *a, **k: object())
    monkeypatch.setattr(main_mod, "set_video_file_threshold_mb", lambda _mb: None)
    monkeypatch.setattr(main_mod, "apply_vendor_patches", lambda: None)
    monkeypatch.setattr(main_mod, "install_ssrf_guard", lambda: None)

    main_mod.ParserLitePlugin.__init__(
        plugin,
        None,
        {
            "plite_render": False,
            "plite_bsb_api_base": "https://bsbsb.top",
            "plite_bsb_categories": ["  Sponsor ", "INTRO", "self promo", "中文", 7],
        },
    )

    assert seen["plite_bsb_categories"] == ["sponsor", "intro"], (
        f"交给 vendor 的分类名不是归一后的集合：{seen.get('plite_bsb_categories')!r}"
    )
    # 其余配置项照常透传，归一不得把别的键一起吞掉
    assert seen["plite_bsb_api_base"] == "https://bsbsb.top"
    assert "plite_render" not in seen, "桥自有键不该进 vendor configure"


def test_bsb_category_shape_constant_matches_server_rule() -> None:
    """归一用的字符集必须与服务端 validateString 的认识范围对齐，防止各自漂移。

    服务端放行「不含 `[^a-z|_-]` 的串」，本桥要求整串由小写字母/数字/下划线/连字符
    组成——比服务端更严（不接受 `|`），但不放宽任何合法分类名。
    """
    pattern = main_mod._BSB_CATEGORY_SHAPE_RE.pattern
    assert pattern == r"[a-z0-9_-]+\Z"
    assert re.match(pattern, "music_offtopic")
    assert re.match(pattern, "poi_highlight")
    assert not re.match(pattern, "Intro")
    assert not re.match(pattern, "self promo")


# --------------------------------------------------------------------------
# 出网形状哨兵（打 vendor，随上游回归而红）


class _LogCapture:
    """替掉 vendor 的 logger 出口，只收集消息文本。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args: Any) -> None:
        self.warnings.append(msg % args if args else msg)

    def debug(self, msg: str, *args: Any) -> None:
        return None


def _real_channel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    content: bytes,
) -> dict[str, Any]:
    """装一条**真实**的出站通道：vendor 的 `UniHttpClient` + 真 `httpx.AsyncClient`，
    只在最底下的 transport 收线并回放预置响应。

    桩位刻意打在 `AsyncHTTPTransport.handle_async_request`，而不是把
    `DOWNLOADER.client` 换成裸替身：#379 的成因是 dict→query 的编码环节，只有
    transport 层拿到的 `httpx.Request.url` 才是真出网的字节。桥的 `_PinnedTransport`
    不在路径内——它要先有校验过的 IP（真 DNS），那部分由 test_ssrf 单独覆盖；而 URL
    编码发生在 `httpx.Request` 构造期、早于任何 transport，所以本断言与钉扎层无关。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
        UniHttpClient,
    )

    seen: dict[str, Any] = {}

    async def _fake_upstream(_self: Any, request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(status, content=content)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_upstream)

    channel = UniHttpClient(timeout=httpx.Timeout(10.0, connect=5.0))
    get_calls: list[dict[str, Any]] = []
    original_get = channel.get

    async def _spy_get(url: str, **kwargs: Any) -> Any:
        get_calls.append(kwargs)
        return await original_get(url, **kwargs)

    monkeypatch.setattr(channel, "get", _spy_get)
    monkeypatch.setattr(
        _vendor_sponsorblock(), "DOWNLOADER", SimpleNamespace(client=channel, headers={})
    )
    seen["get_calls"] = get_calls
    return seen


async def test_skip_segments_query_encodes_categories_as_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`categories` 必须是**单个**、值为 URL 编码 JSON 数组的 query 参数。

    上游 issue #379 的回归哨兵：那次合入把 `categories` 留成 `list`，httpx 编成
    `categories=sponsor&categories=intro`，服务端对复数键无条件 `JSON.parse` → 恒 400，
    面板上看不出任何异常。下一次 roll 若再退回来，本用例必须红。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig

    module = _vendor_sponsorblock()
    seen = _real_channel(monkeypatch, status=200, content=b"[]")
    restore = (pconfig.plite_bsb_api_base, pconfig.plite_bsb_categories)
    pconfig.plite_bsb_api_base = "https://bsbsb.top"
    pconfig.plite_bsb_categories = ["sponsor", "intro"]
    try:
        await module.SponsorBlock.fetch_segments("BV11Kc2zZEDK", 36656057057)
    finally:
        pconfig.plite_bsb_api_base, pconfig.plite_bsb_categories = restore

    encoded: httpx.URL = seen["request"].url
    assert str(encoded).startswith("https://bsbsb.top/api/skipSegments?")
    assert str(encoded).count("categories=") == 1, f"重复参数形态：{encoded}"
    assert encoded.params["categories"] == '["sponsor","intro"]'
    # 通道无关性：交给出站层的值必须是标量串。list 在 httpx 与 curl_cffi 两条通道上
    # 都可能被展开成重复参数，标量串则两边一致——这条替 curl 通道也兜住。
    (call_kwargs,) = seen["get_calls"]
    assert isinstance(call_kwargs["params"]["categories"], str)
    # actionTypes 同款约束：它也是复数键，服务端一样无条件 JSON.parse
    assert str(encoded).count("actionTypes=") == 1, f"重复参数形态：{encoded}"
    assert isinstance(call_kwargs["params"]["actionTypes"], str)
    assert call_kwargs.get("use_curl_cffi", False) is False  # 空降走 httpx 通道


async def test_non_200_reason_reaches_log_without_faking_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败诊断：服务端给的原因要进日志，但正文里的控制字符不得伪造日志行。

    上游 #380 用 `response.content` + `decode(errors="replace")` + `isprintable()`
    三件事同时挡住「诊断代码自己把链路炸穿」（`.text` 在编码不符时抛 UnicodeError）
    与「远端正文注入假日志行」。本用例把这三条钉住在位（本仓 #21 的核实结论）。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig

    module = _vendor_sponsorblock()
    logger = _LogCapture()
    monkeypatch.setattr(module, "logger", logger)
    body = b'["Unknown category provided."]\n\x1b[31m\xe4\xb8\xad\xe6\x96\x87\r\nFAKE-LINE'
    _real_channel(monkeypatch, status=400, content=body)
    restore = (pconfig.plite_bsb_api_base, pconfig.plite_bsb_categories)
    pconfig.plite_bsb_api_base = "https://bsbsb.top"
    pconfig.plite_bsb_categories = ["sponsor"]
    try:
        segments = await module.SponsorBlock.fetch_segments("BV1x", 1)
    finally:
        pconfig.plite_bsb_api_base, pconfig.plite_bsb_categories = restore

    assert segments == []
    assert len(logger.warnings) == 1, f"应当只有一条告警：{logger.warnings!r}"
    line = logger.warnings[0]
    assert "HTTP 400" in line
    assert "Unknown category provided." in line, f"服务端原因没有进日志：{line!r}"
    assert "\n" not in line and "\r" not in line and "\x1b" not in line, (
        f"日志被脏字符污染：{line!r}"
    )
    assert "中文" in line  # 可打印的非 ASCII 正文仍保留：那是诊断信息，不是噪声
