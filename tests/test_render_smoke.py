"""两段式渲染第一段离线冒烟（入库取代 /tmp 脚本）。

断言本地 Jinja 段产出自包含 HTML：无 ``file://`` 引用（远程 t2i 读不到
本地文件系统）、_resolve_src 内联与预算按既定约束生效。Theme API v1 起
转义单点在数据层（build_theme_data/_escape_html），模板族只剩 default。
第二段（AstrBot html_render 远程出图）属网络路径，归隔离区而非 required。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_parser_lite.bridge import render
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import PlatformEnum
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    Author,
    ParseResult,
    Platform,
    Stats,
)

# 1×1 透明 PNG（最小合法图片，保持用例离线密闭）
_MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _result(url: str = "https://www.bilibili.com/video/av170001") -> ParseResult:
    return ParseResult(
        platform=Platform(name=PlatformEnum.BILIBILI, display_name="哔哩哔哩"),
        author=Author(name="冒烟作者"),
        url=url,
        content=[],
        title="冒烟标题",
    )


class _HasPath:
    """safe_src 的最小消费对象：get_path 返回本地路径（同步，非协程）。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_path(self) -> Path:
        return self._path


def test_stage_one_rejects_platform_name_path_traversal() -> None:
    """平台名不得穿出模板目录（2026-09-14 评审 #10）。

    模板名进 ``FileSystemLoader`` 前先过 `[a-z0-9_-]+` 白名单。当前
    ``PlatformEnum`` 值域封闭走不到这里，但 ``ParseResult.platform`` 由桥
    组装，按「输入不可信」设防；白名单外的值回退 default 而不抛错。

    注意断言的是**白名单生效**而非最终结果：仅靠末尾 ``is_file()`` 兜底在
    ``../templates/default`` 这类 payload 上会失效——它穿越后确实命中
    ``default.html.jinja``（同类文件的真实存在），因此这里用「结果 + 拼接
    内容」双重断言，路径型平台名必须连候选名都拼不出来。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import Platform

    defaults = "default.html.jinja"
    # ../templates/default 会穿越回模板目录并命中真实文件，是能骗过 is_file()
    # 兜底的样本；其余为分隔符/空白/父目录变体
    for hostile in (
        "../templates/default",
        "../../../../etc/passwd",
        "..",
        "sub/dir",
        "a b",
        "bilibili/../..",
    ):
        result = _result()
        result.platform = Platform(name=hostile, display_name=hostile)
        assert render._select_template(result) == defaults, (
            f"平台名 {hostile!r} 未回退 default，存在路径穿越风险"
        )


async def test_stage_one_html_is_self_contained() -> None:
    """第一段渲染：无 file:// 引用（内联/占位），模板选择按回退规则。"""
    # append_qrcode 是 vendor Config 上的只读 property（底层字段代理），开关落在字段上
    pconfig.plite_append_qrcode = False
    result = _result()
    assert render._select_template(result) == "default.html.jinja"  # 无平台模板回退

    html = await render.build_html(result, "light")
    assert "file://" not in html, "第一段 HTML 含 file:// 引用，远程 t2i 无法渲染"
    assert "冒烟标题" in html
    assert "冒烟作者" in html


async def test_stage_one_inlines_stylesheets() -> None:
    """样式表与媒体同理必须内联（P0 回归 2026-09-12）：远程 t2i 端点的
    浏览器解析不了相对模板路径，``<link rel="stylesheet">`` 必然 404，
    导致全部卡片渲染为无样式裸 HTML（6 张生产渲染图视觉取证）。"""
    pconfig.plite_append_qrcode = False
    html = await render.build_html(_result(), "light")

    assert "<link" not in html, "存在外链样式表，远程 t2i 渲染为无样式裸 HTML"
    assert "<style>" in html
    assert "--default-mono-font-family" in html, "tailwind.css 内容未内联"
    assert "--icon-color-view" in html, "icon.css 内容未内联"
    assert "width:620px" in html, "620px 卡片画布特征缺失（模板资产未对齐上游）"


async def test_stage_one_centers_card_for_fixed_t2i_viewport() -> None:
    """桥侧视口适配（活体复验 2026-09-14）：公共 t2i 端点忽略 viewport/dsf
    参数且视口宽因端点而异（实测 800/1280 两档），620px 卡片直接渲染会
    随端点漂移出宽窄不一的留白条（群 1124969653 实发卡 1280×2558，两侧
    约 330px 空白）；body zoom 放大卡面至内容宽 1280px——full_page 截图
    取内容宽，任意端点都满幅无留白，文字按布局级缩放保持锐利。"""
    html = await render.build_html(_result(), "light")
    assert "margin:0 auto" in html or "margin: 0 auto" in html, "620px 卡片未居中"
    assert f"zoom:{render._T2I_ZOOM:.6f}" in html, "卡面未按内容宽目标缩放"


async def test_stage_one_escapes_attacker_controlled_html() -> None:
    """用户可控字段必须 HTML 转义（2026-09-14 双轴评审）：标题/作者名/评论
    作者等字段源自被解析页面，且第一段 HTML 会被送给**远程** t2i 端点渲染，
    裸插值等于把解析侧内容当 HTML 执行（可注入伪造卡片内容、外链、追踪
    像素）。Theme API v1 起转义单点在**数据层**（build_theme_data 末尾
    _escape_html 递归），模板裸插值 + Environment(autoescape=False)，
    全树恰好一次转义——旧的「两模板逐字段判定有没有 ``| e``」转义矩阵
    随 music 模板退役（卡片结构不被转义由下一条用例守护）。
    """
    pconfig.plite_append_qrcode = False
    payload = '<img src=x onerror="alert(1)">'
    result = _result()
    result.title = payload
    result.author = Author(name=payload)

    html = await render.build_html(result, "light")

    assert payload not in html, "用户可控字段未转义，原始 HTML 泄入待渲染页面"
    assert "&lt;img src=x" in html, "转义产物缺失"


async def test_stage_one_escapes_comment_and_summary_fields() -> None:
    """评论作者名/位置与 AI 摘要同样是解析侧内容（评审实证漏点：
    ``{{ comment.author.name }}``、``{{ result.ai_summary }}`` 无 ``| e``）。"""
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
        Comment,
    )

    pconfig.plite_append_qrcode = False
    payload = "<script>alert(1)</script>"
    result = _result()
    result.ai_summary = payload
    result.comments = [
        Comment(
            author=Author(name=payload, location=payload),
            content=["评论正文"],
            timestamp=None,
        )
    ]

    html = await render.build_html(result, "light")

    assert payload not in html, "评论/摘要字段未转义"
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3, (
        "评论作者名/位置/摘要三处转义不完整"
    )


async def test_stage_one_does_not_mutate_parse_result() -> None:
    """转义必须只作用于模板副本（2026-09-14 评审）：ParseResult 是 sender 与
    render 共享的同一实例，原地改写会让发送到聊天平台的文本出现 ``&amp;``
    之类的转义残留。"""
    pconfig.plite_append_qrcode = False
    payload = "昵称 & <b>粗</b>"
    result = _result()
    result.title = payload
    result.author = Author(name=payload, location=payload)
    result.ai_summary = payload

    await render.build_html(result, "light")

    assert result.title == payload, "render.py 就地改写了 result.title"
    assert result.author.name == payload, "render.py 就地改写了作者名"
    assert result.author.location == payload, "render.py 就地改写了作者位置"
    assert result.ai_summary == payload, "render.py 就地改写了 ai_summary"


async def test_stage_one_escapes_music_platform_fields() -> None:
    """音乐平台卡面转义（Theme API v1）：music.html.jinja 已随上游退役，
    音乐平台落 default 模板族——专辑/歌词/简介（post.extra）不再进模板
    输出面，转义由数据层统一兜住；标题/作者等实际渲染字段必须转义。"""
    pconfig.plite_append_qrcode = False
    payload = "<img src=x onerror=alert(1)>"
    result = ParseResult(
        platform=Platform(name=PlatformEnum.KUGOU, display_name="酷狗"),
        author=Author(name=payload),
        url="https://x.test/",
        content=[],
        title=payload,
    )
    result.extra = {"album": payload, "lyric": payload, "info": payload}

    html = await render.build_html(result, "light")

    assert payload not in html, "音乐平台卡面存在未转义字段"
    assert html.count("&lt;img src=x") >= 2, "标题/作者转义产物缺失"


async def test_stage_one_escapes_title_exactly_once() -> None:
    """标题恰好一次转义（旧「双模板过滤状态不一致」契约的换代留痕）。

    旧契约：macros.jinja 的 ``| e`` 与 music 模板裸插值不一致，桥必须预转
    title，代价是 default 卡 ``&amp;lt;`` 双转义字面量。Theme API v1 把转义
    收进数据层单点，模板全裸插值 + autoescape=False——桥若恢复字段级预转义
    （或误开 autoescape），本用例的 ``&amp;lt;`` 断言立即变红。
    """
    pconfig.plite_append_qrcode = False
    payload = "<img src=x onerror=alert(1)>"
    result = _result()
    result.title = payload

    html = await render.build_html(result, "light")
    assert payload not in html, "标题未转义泄入待渲染页面"
    assert "&lt;img src=x" in html, "标题单次转义产物缺失"
    assert "&amp;lt;" not in html, "标题被二次转义（数据层之外多了一道转义）"


async def test_stage_one_keeps_structural_markup_intact(
    tmp_path: Path,
) -> None:
    """模板自身拼装的 HTML 结构必须仍是真标签：macros 用 ``~`` 拼接字符串
    产出结构，转义只在数据层对**值**发生；若数据层/桥误伤结构（或误开
    autoescape），卡片会退化成可见标签源码——本用例守护结构不被回归破坏。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
        ImageContent,
    )
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download.task import (
        DownloadTaskWrapper,
    )

    img = tmp_path / "pic.png"
    img.write_bytes(_MINIMAL_PNG)

    async def _provide() -> Path:
        return img

    pconfig.plite_append_qrcode = False
    result = _result()
    result.content = [
        ImageContent(path_task=DownloadTaskWrapper(func=_provide, args=(), kwargs={}, url="x")),
        "纯文本段落 <b>不该成标签</b>",
    ]

    html = await render.build_html(result, "light")

    assert "<img" in html, "结构标签被转义成文本"
    assert "&lt;img" not in html, "结构标签被转义成文本"
    assert "纯文本段落" in html
    assert "single-image" in html, "图片容器结构缺失"
    # 正文里的标签必须被转义（文本项的值经数据层 _escape_html），不得与结构标签混淆
    assert "<b>不该成标签</b>" not in html, "内容项文本未转义"
    assert "&lt;b&gt;不该成标签&lt;/b&gt;" in html, "内容项文本转义产物缺失"


async def test_stage_one_carries_theme_canvas_background() -> None:
    """桥面画布底色（2026-09-13 模板数据面活体对照）：上游模板无 body
    底色规则（旧副本冻结的上游已删规则已随逐字节再生移除），远程 t2i
    默认白底会让暗色卡的两侧留白条穿帮——底色规则按主题选择器留在桥侧
    视口补丁内，两种主题都可达。"""
    html = await render.build_html(_result(), "light")
    assert "html,body{background:#f8fafc}" in html, "亮色画布底色缺失"
    assert '[data-theme="dark"],[data-theme="dark"] body{background:#020617}' in html, (
        "暗色画布底色缺失"
    )


async def test_stage_one_renders_paired_stats_extra() -> None:
    """stats.extra 二元组形状直供渲染（2026-09-13 P1 回归的换代形态）。

    旧 vendor standalone 的标量形状与桥内 ACL 翻译表（_EXTRA_LABELS）已随
    Theme API v1 roll 退役：vendor 与 main 同源后 stats.extra 值就是
    （标签, 数值）二元组，_serialize_stats 逐项拆进 ``extra[]`` 列表、
    模板按 item.label/item.value 裸插值。标量直供在上游/桥镜像里会把
    字符串按字符切碎（"321" → "3","2"）——那是上游自身语义，桥不另设防线。
    """
    paired = _result()
    paired.stats = Stats(
        view_count="1.2万",
        like_count="500",
        extra={"danmaku": ("弹幕", "321"), "coin": ("硬币", "10")},  # vendor 现行形状
    )
    html = await render.build_html(paired, "light")
    assert "弹幕" in html and "321" in html, "danmaku 二元组未渲染"
    assert "硬币" in html and "10" in html, "coin 二元组未渲染"


async def test_serialize_stats_returns_new_dict_not_inplace() -> None:
    """数据层镜像不改写共享 ParseResult：stats.extra 保持 vendor 原对象。

    sender 与 render 共享同一 ParseResult 实例；旧 ACL 的副本纪律由
    _serialize_stats 的「构建新 dict」天然承接——输出是形状翻译后的
    JSON-like 列表，原 Stats.extra 字典必须原封不动。"""
    stats = Stats(view_count="1.2万", like_count="500", extra={"danmaku": ("弹幕", "321")})
    serialized = render._serialize_stats(stats)
    assert stats.extra == {"danmaku": ("弹幕", "321")}, "原对象被就地改写"
    assert serialized["extra"] == [{"key": "danmaku", "label": "弹幕", "value": "321"}]
    assert serialized["view_count"] == "1.2万"


async def test_resolve_src_inlines_and_accounts_budget(tmp_path: Path) -> None:
    """_resolve_src：本地图片内联为 data URI 并入账预算。"""
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)
    budget = render.InlineBudget()
    src = await render._resolve_src(_HasPath(img), budget=budget)

    assert src is not None and src.startswith("data:image/png;base64,")
    # 预算口径为 base64 后的字节（POST 给远程 t2i 的真实体量），非原始文件字节
    assert budget.used == render.base64_size(len(_MINIMAL_PNG))
    assert budget.used > len(_MINIMAL_PNG)
    assert budget.degraded == 0


async def test_resolve_src_enforces_budget_and_placeholder(tmp_path: Path) -> None:
    """_resolve_src：超预算/缺失/非图片一律占位图降级，绝不产生本地路径引用。"""
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)

    exhausted_budget = render.InlineBudget()
    exhausted_budget.used = render.INLINE_BUDGET_BYTES
    assert await render._resolve_src(_HasPath(img), budget=exhausted_budget) == (
        render.PLACEHOLDER_IMAGE
    )

    fresh_budget = render.InlineBudget()
    assert await render._resolve_src(_HasPath(tmp_path / "nope.png"), budget=fresh_budget) == (
        render.PLACEHOLDER_IMAGE
    )
    assert (
        await render._resolve_src(
            _HasPath(tmp_path / "not-image.txt"), budget=render.InlineBudget()
        )
        == render.PLACEHOLDER_IMAGE
    )


class _ShotRenderer:
    """html_render 边界替身：返回真实临时 PNG 路径，并记录调用次数。"""

    def __init__(self, shot_path: Path, calls: list[int]) -> None:
        self._shot_path = shot_path
        self._calls = calls

    async def html_render(self, *_a: Any, **_k: Any) -> str:
        self._calls.append(1)
        return str(self._shot_path)


async def test_cache_or_render_image_reuses_cache(tmp_path: Path, monkeypatch: Any) -> None:
    """上游 cache_or_render_image 等价移植：命中渲染缓存不重渲、png 转 jpeg。

    回归护栏（2026-09-12 生产日志）：桥内旧实现只写缓存不读，同 URL 十几
    分钟内被完整重渲并覆写（11.4MB PNG 重复截图），上游「命中即复用、跨
    重启复用」语义从未生效。
    """
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(_MINIMAL_PNG)
    jpeg_marker = b"\xff\xd8\xff\xe0-fake-jpeg"

    async def _fake_png_to_jpeg(png_data: bytes, quality: int = 85) -> bytes:
        return jpeg_marker

    monkeypatch.setattr(render, "_png_to_jpeg", _fake_png_to_jpeg)

    calls: list[int] = []
    renderer = _ShotRenderer(shot, calls)
    result = _result()

    dest1 = await render.cache_or_render_image(result, renderer=renderer)
    assert dest1.suffix == ".jpeg"
    assert await dest1.read_bytes() == jpeg_marker
    assert result.render_image == dest1
    residue = [p async for p in dest1.parent.glob(".*.tmp")]
    assert not residue, "临时文件残留"

    dest2 = await render.cache_or_render_image(result, renderer=renderer)
    assert dest2 == dest1
    assert len(calls) == 1, "缓存命中仍触发 html_render——复用语义被破坏"


# ---------------------------------------------------------------- 内联预算（M14）


def _result_with_images(tmp_path: Path, count: int) -> ParseResult:
    """构造带 count 张本地图片的 ParseResult（每张都走 safe_src 内联路径）。"""
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
        ImageContent,
    )
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download.task import (
        DownloadTaskWrapper,
    )

    result = _result(url=f"https://www.bilibili.com/video/av-budget-{count}")
    items: list[Any] = []
    for index in range(count):
        img = tmp_path / f"pic-{index}.png"
        img.write_bytes(_MINIMAL_PNG)

        async def _provide(p: Path = img) -> Path:
            return p

        items.append(
            ImageContent(path_task=DownloadTaskWrapper(func=_provide, args=(), kwargs={}, url="x"))
        )
    result.content = items
    return result


async def test_build_html_degrades_whole_page_when_media_over_budget(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """超预算媒体数超阈值 → 整页降级为文本，而非静默发「一半是灰块」的卡。"""
    monkeypatch.setattr(render, "INLINE_BUDGET_BYTES", 1)
    result = _result_with_images(tmp_path, render.INLINE_DEGRADE_THRESHOLD + 1)
    with pytest.raises(render.InlineBudgetExceeded, match="threshold"):
        await render.build_html(result, "light")


async def test_build_html_keeps_placeholders_below_degrade_threshold(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """降级媒体数未超阈值时保持单图占位降级（不整页降级）。

    占位计数含装饰面（logo/头像/环境光等安全占位），1 张内容图在预算=1
    时实测可累计 >2 张降级；本用例只锁「未超阈值 → 不整页降级」，故抬高
    阈值排除装饰面噪声（整页降级的「超阈值」分支由上一用例覆盖）。
    """
    monkeypatch.setattr(render, "INLINE_BUDGET_BYTES", 1)
    monkeypatch.setattr(render, "INLINE_DEGRADE_THRESHOLD", 50)
    result = _result_with_images(tmp_path, 1)
    html = await render.build_html(result, "light")
    assert render.PLACEHOLDER_IMAGE in html


def test_inline_budget_degrades_are_deduped_by_media(tmp_path: Path, monkeypatch: Any) -> None:
    """同一张媒体的多次内联失败只计一次（模板对同图多次调 safe_src）。"""
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)
    monkeypatch.setattr(render, "INLINE_BUDGET_BYTES", 1)
    budget = render.InlineBudget()
    for _ in range(3):
        assert render._inline_image_sync(str(img), budget) is None
    assert budget.used == 0, "超预算仍记账"
    assert budget.degraded == 1, "同一张图的内联失败被重复计数"


async def test_placeholder_degraded_folds_across_objects() -> None:
    """占位降级按 reason:type:method 折叠不同对象（刻意取舍）。

    占位图是 1×1 透明 GIF（非可见灰块）；评论头像/回复头像不带
    return_none_on_fail——若逐对象计数，≥3 条无头像评论的正常卡片
    会误触整页降级（INLINE_DEGRADE_THRESHOLD=2），整卡退化为纯文本。
    内容图的预算类降级按文件路径逐张计数（test_inline_budget_degrades_
    are_deduped_by_media），不受此折叠影响。
    """
    budget = render.InlineBudget()

    class _NoMedia:
        pass

    objs = [_NoMedia() for _ in range(3)]
    for obj in objs:
        placeholder = await render._resolve_src(obj, budget=budget)
        assert placeholder is not None
    assert budget.degraded == 1, "同类结构失败被逐对象计数——会误触整页降级"

    # 不同方法（结构性失败的不同侧面）仍分别计数
    await render._resolve_src(objs[0], "get_cover_path", budget=budget)
    assert budget.degraded == 2

    # 装饰路径（return_none_on_fail=True）返回 None 且完全不计数
    before = budget.degraded
    assert (
        await render._resolve_src(None, "get_avatar_path", budget=budget, return_none_on_fail=True)
        is None
    )
    assert budget.degraded == before


def test_inline_read_failure_does_not_consume_budget(tmp_path: Path, monkeypatch: Any) -> None:
    """读文件失败不得记账（旧实现先记账后读，读失败会虚耗预算）。"""
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)

    def _boom(self: Path) -> bytes:
        raise OSError("读取失败")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    budget = render.InlineBudget()
    assert render._inline_image_sync(str(img), budget) is None
    assert budget.used == 0, "读失败仍记账，预算被虚耗"
    assert budget.degraded == 0, "读失败不应算作预算降级"


# ---------------------------------------------------------------- 缓存键与产物（M7/L10）


def test_render_cache_key_tracks_render_config(monkeypatch: Any) -> None:
    """缓存键纳入影响渲染输出的配置态（M7）。

    二维码开关 / 评论条数 / 昵称都进模板输出却曾不在键内，改配置后最长
    vendor 清理周期（2h）内仍发旧图；theme 已在键内（日夜不串图）不重复。
    """
    result = _result()
    base = render.render_cache_key(result, "light")
    assert base == render.render_cache_key(result, "light"), "同输入键不稳定"
    assert render.render_cache_key(result, "dark") != base, "主题未进键"
    assert base.endswith(result.url)
    assert len(render._render_config_rev()) == 12, "配置态摘要长度不符（应取 sha256 前 12 位）"

    monkeypatch.setattr(pconfig, "plite_append_qrcode", True)
    with_qrcode = render.render_cache_key(result, "light")
    assert with_qrcode != base, "二维码开关未进缓存键"

    monkeypatch.setattr(pconfig, "plite_max_comments", 9)
    with_comments = render.render_cache_key(result, "light")
    assert with_comments not in {base, with_qrcode}, "评论条数未进缓存键"

    monkeypatch.setattr(render, "_nickname", "另一个昵称")
    with_nickname = render.render_cache_key(result, "light")
    assert with_nickname not in {base, with_qrcode, with_comments}, "昵称未进缓存键"

    other = _result(url="https://www.bilibili.com/video/av999")
    assert render.render_cache_key(other, "light") not in {base, with_nickname}, "URL 未进键"


async def test_cache_hit_rejects_empty_artifact(tmp_path: Path, monkeypatch: Any) -> None:
    """0 字节缓存产物不算命中：必须重渲（L10）。"""
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(_MINIMAL_PNG)
    jpeg_marker = b"\xff\xd8\xff\xe0-fake-jpeg"

    async def _fake_png_to_jpeg(png_data: bytes, quality: int = 85) -> bytes:
        return jpeg_marker

    monkeypatch.setattr(render, "_png_to_jpeg", _fake_png_to_jpeg)
    calls: list[int] = []
    renderer = _ShotRenderer(shot, calls)
    result = _result(url="https://www.bilibili.com/video/av-empty-cache")

    dest = await render.cache_or_render_image(result, renderer=renderer)
    assert await dest.read_bytes() == jpeg_marker

    # 模拟 PNG→JPEG 产出 0 字节被缓存（或上次写入被中断）
    await dest.write_bytes(b"")
    calls.clear()
    dest2 = await render.cache_or_render_image(result, renderer=renderer)
    assert dest2 == dest
    assert len(calls) == 1, "0 字节产物被当成缓存命中，未重渲"
    assert await dest2.read_bytes() == jpeg_marker


async def test_empty_render_artifact_is_not_cached(tmp_path: Path, monkeypatch: Any) -> None:
    """PNG→JPEG 返回 0 字节或非 JPEG 时拒绝写缓存（L10 + 魔数校验）。"""
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(_MINIMAL_PNG)

    async def _ok(png_data: bytes, quality: int = 85) -> bytes:
        return b"\xff\xd8\xffok-jpeg"

    monkeypatch.setattr(render, "_png_to_jpeg", _ok)
    calls: list[int] = []
    renderer = _ShotRenderer(shot, calls)
    result = _result(url="https://www.bilibili.com/video/av-empty-artifact")
    dest = await render.cache_or_render_image(result, renderer=renderer)
    assert await dest.read_bytes() == b"\xff\xd8\xffok-jpeg"

    async def _empty(png_data: bytes, quality: int = 85) -> bytes:
        return b""

    await dest.unlink()
    monkeypatch.setattr(render, "_png_to_jpeg", _empty)
    with pytest.raises(render.RenderArtifactError):
        await render.cache_or_render_image(result, renderer=renderer)
    assert not await dest.exists(), "0 字节产物被写进了缓存"

    async def _not_jpeg(png_data: bytes, quality: int = 85) -> bytes:
        return b"not-a-jpeg"

    monkeypatch.setattr(render, "_png_to_jpeg", _not_jpeg)
    with pytest.raises(render.RenderArtifactError):
        await render.cache_or_render_image(result, renderer=renderer)
    assert not await dest.exists(), "非 JPEG 产物被写进了缓存"


async def test_png_to_jpeg_converts_locally() -> None:
    """桥内真实转换闭环（上游 1.3.8rc6 移除 FFmpeg.png_to_jpeg 后无 fake）：
    合法 PNG 必须转出带 SOI 魔数的 JPEG。"""
    jpeg = await render._png_to_jpeg(_MINIMAL_PNG)
    assert render._is_jpeg(jpeg), "桥内 PNG→JPEG 转换产物非法"
