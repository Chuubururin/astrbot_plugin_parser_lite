"""两段式渲染第一段离线冒烟。

断言本地 Jinja 段产出自包含 HTML：无 ``file://`` 引用（远程 t2i 读不到
本地文件系统）、_resolve_src 内联与预算按既定约束生效。转义单点在数据层
（build_theme_data/_escape_html），模板族只有 default。
第二段（AstrBot html_render 远程出图）属网络路径，归隔离区而非 required。
"""

from __future__ import annotations

import base64
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_parser_lite.bridge import render
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import PlatformEnum
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    Author,
    ImageContent,
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


class _RecordingLogger:
    """替 render.logger 记账：断言「诊断到底说了什么」，其余级别吞掉。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args: Any) -> None:
        self.warnings.append(msg % args if args else msg)

    def __getattr__(self, name: str) -> Any:
        return lambda *_a: None


def test_stage_one_rejects_platform_name_path_traversal() -> None:
    """平台名不得穿出模板目录。

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
    """样式表与媒体同理必须内联：远程 t2i 端点的浏览器解析不了相对模板
    路径，``<link rel="stylesheet">`` 必然 404，卡片全部渲染为无样式裸
    HTML。"""
    pconfig.plite_append_qrcode = False
    html = await render.build_html(_result(), "light")

    assert "<link" not in html, "存在外链样式表，远程 t2i 渲染为无样式裸 HTML"
    assert "<style>" in html
    assert "--default-mono-font-family" in html, "tailwind.css 内容未内联"
    assert "--icon-color-view" in html, "icon.css 内容未内联"
    assert "width:620px" in html, "620px 卡片画布特征缺失（模板资产未对齐上游）"


async def test_stage_one_centers_card_for_fixed_t2i_viewport() -> None:
    """桥侧视口适配：公共 t2i 端点忽略 viewport/dsf 参数且视口宽因端点而异
    （实测 800/1280 两档），620px 卡片直接渲染会随端点漂移出宽窄不一的留
    白条；body zoom 放大卡面至内容宽 1280px——full_page 截图取内容宽，任意
    端点都满幅无留白，文字按布局级缩放保持锐利。"""
    html = await render.build_html(_result(), "light")
    assert "margin:0 auto" in html or "margin: 0 auto" in html, "620px 卡片未居中"
    assert f"zoom:{render._T2I_ZOOM:.6f}" in html, "卡面未按内容宽目标缩放"


async def test_stage_one_escapes_attacker_controlled_html() -> None:
    """用户可控字段必须 HTML 转义：标题/作者名/评论作者等字段源自被解析
    页面，且第一段 HTML 会被送给**远程** t2i 端点渲染，裸插值等于把解析侧
    内容当 HTML 执行（可注入伪造卡片内容、外链、追踪像素）。转义单点在
    **数据层**（build_theme_data 末尾 _escape_html 递归），模板裸插值 +
    Environment(autoescape=False)，全树恰好一次转义——卡片结构不被转义由
    下一条用例守护。
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
    """评论作者名/位置与 AI 摘要同样是解析侧内容：模板里
    ``{{ comment.author.name }}``、``{{ result.ai_summary }}`` 是裸插值，
    转义完全依赖数据层单点。"""
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
    """转义必须只作用于模板副本：ParseResult 是 sender 与
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
    """标题恰好一次转义。

    转义单点在数据层，模板全裸插值 + autoescape=False；桥若做字段级预转义
    （或误开 autoescape），default 卡会落入 ``&amp;lt;`` 双转义字面量——
    本用例的 ``&amp;lt;`` 断言对此立即变红。
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
    """桥面画布底色：上游模板无 body 底色规则，远程 t2i
    默认白底会让暗色卡的两侧留白条穿帮——底色规则按主题选择器留在桥侧
    视口补丁内，两种主题都可达。"""
    html = await render.build_html(_result(), "light")
    assert "html,body{background:#f8fafc}" in html, "亮色画布底色缺失"
    assert '[data-theme="dark"],[data-theme="dark"] body{background:#020617}' in html, (
        "暗色画布底色缺失"
    )


async def test_stage_one_renders_paired_stats_extra() -> None:
    """stats.extra 二元组形状直供渲染。

    vendor 与 main 同源的现行形状：stats.extra 值就是
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

    sender 与 render 共享同一 ParseResult 实例；副本纪律由
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

    只写缓存不读会让同 URL 在缓存有效期内被完整重渲并覆写，上游「命中即
    复用、跨重启复用」语义失效——本用例钉住读路径。
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


async def test_cache_or_render_image_rejects_non_image_t2i_artifact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """t2i 产物不是图片字节即响亮失败，而不是交给 PIL 抛出无信息的 UnidentifiedImageError。

    宿主 download_image_by_url 不校验响应状态码与内容类型，错误页也落盘成
    「渲染成功」返回路径，失败点于是漂到转换环节。重试上收后本用例还钉住
    接线：非图片产物按退避序列重试满次数才交给降级路径。
    """
    bad = tmp_path / "io_temp_img_1700000000_abcd1234.jpg"
    bad.write_bytes(b"<html><head><title>502 Bad Gateway</title></head></html>")
    calls: list[int] = []

    log = _RecordingLogger()
    monkeypatch.setattr(render, "logger", log)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(render.RenderArtifactError):
        await render.cache_or_render_image(
            _result(url="https://www.bilibili.com/video/av-t2i-non-image"),
            renderer=_ShotRenderer(bad, calls),
            sleep=_fake_sleep,
        )

    assert len(calls) == render.T2I_RENDER_ATTEMPTS, (
        f"html_render 调用数 {len(calls)} 与尝试上界脱钩，形态校验或重试接线坏了"
    )
    assert slept == list(render.T2I_RENDER_BACKOFF_SECONDS), f"退避序列不符：{slept}"
    # 诊断必须自带证据：拒绝把失败点漂到转换环节后，仅抛异常仍无法定位
    # 远端到底返回了什么
    assert any("502 Bad Gateway" in line for line in log.warnings), (
        f"拒绝产物时未把产物字节头记入日志，故障仍不可定位：{log.warnings}"
    )


async def test_cache_or_render_image_accepts_jpeg_t2i_artifact(tmp_path: Path) -> None:
    """形态校验按字节判定，不误杀 JPEG 产物：宿主 save_temp_img 把任何响应字节
    都写成 ``.jpg`` 文件名，扩展名不承载格式信息，校验也不得按请求的
    ``type=png`` 收窄成只认 PNG。

    转换器不打桩：桩的返回值就是断言的期望值时，真转换器坏了这条用例照样绿。
    """
    from PIL import Image

    payload = _small_jpeg()
    shot = tmp_path / "io_temp_img_1700000001_ef012345.jpg"
    shot.write_bytes(payload)
    calls: list[int] = []

    dest = await render.cache_or_render_image(
        _result(url="https://www.bilibili.com/video/av-t2i-jpeg"),
        renderer=_ShotRenderer(shot, calls),
    )

    cached = await dest.read_bytes()
    assert render._is_jpeg(cached), "真 JPEG 产物被误杀，或落缓存的不是 JPEG"
    with Image.open(BytesIO(cached)) as im:
        assert im.format == "JPEG" and im.size == (64, 64), (
            f"缓存产物不可解码：{im.format} {im.size}"
        )


# ---------------------------------------------------------------- 内联预算


def _result_with_images(tmp_path: Path, count: int) -> ParseResult:
    """构造带 count 张本地图片的 ParseResult（每张都走 safe_src 内联路径）。

    path_task 必须产 anyio.Path：``MediaContent.get_path`` 还要
    ``await path.stat()``，给 pathlib.Path 会让取源直接抛 TypeError，图片根本
    进不到内联与记账那一步——预算类用例就会退化成「结构性失败折叠成 1 个键」，
    看着像过了其实什么都没测。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
        ImageContent,
    )

    result = _result(url=f"https://www.bilibili.com/video/av-budget-{count}")
    items: list[Any] = []
    for index in range(count):
        img = tmp_path / f"pic-{index}.png"
        img.write_bytes(_MINIMAL_PNG)
        items.append(ImageContent(path_task=_MediaPath(img)))
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


async def test_inline_budget_degrades_are_deduped_by_media(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """同一张媒体的多次内联失败只计一次（模板对同图多次取源）。

    降级计数由 _resolve_src 单点负责（_inline_image 只记字节），且按文件路径
    记——一张放不下的图在整页降级判据里只占一个名额。
    """
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)
    monkeypatch.setattr(render, "INLINE_BUDGET_BYTES", 1)
    budget = render.InlineBudget()
    holder = _HasPath(img)
    for _ in range(3):
        assert await render._resolve_src(holder, budget=budget) == render.PLACEHOLDER_IMAGE
    assert budget.used == 0, "超预算仍记账"
    assert budget.degraded == 1, f"同一张图占了 {budget.degraded} 个降级名额（应 1 个）"
    assert budget.decoration_misses == 0


async def test_decoration_slots_never_degrade_the_whole_page(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """整页降级判据只数内容缺口：装饰位（头像/logo）回退透明占位不顶掉整张卡。

    两张放不下的内容图恰好占满阈值（`> 2` 才整页降级），作者头像缺席只进
    ``decoration_misses``——肉眼看不见的洞不该把一张只有两个真洞的卡顶成纯文本。
    """
    monkeypatch.setattr(render, "INLINE_BUDGET_BYTES", 1)
    monkeypatch.setattr(render, "_slim_decorative_images_sync", lambda h: h)
    pconfig.plite_append_qrcode = False
    images = []
    for index in range(2):
        img = tmp_path / f"photo-{index}.jpg"
        img.write_bytes(_noise_jpeg())
        images.append(ImageContent(path_task=_MediaPath(img)))
    result = _result(url="https://t.bilibili.com/probe-decoration-not-content")
    # Author 不带 avatar → 装饰位失败；两张内容图超预算 → 真实缺口两个
    result.content = images

    html = await render.build_html(result, "light")

    assert render.PLACEHOLDER_IMAGE in html, "内容图未回退占位（超预算应留洞）"
    assert "冒烟标题" in html, "整页降级判据被装饰位顶掉：卡片没渲染成 HTML"


async def test_placeholder_degraded_folds_across_objects() -> None:
    """占位降级按 reason:type:method 折叠不同对象（刻意取舍）。

    占位图是 1×1 透明 GIF（非可见灰块）；评论头像/回复头像不带
    return_none_on_fail——若逐对象计数，≥3 条无头像评论的正常卡片
    会误触整页降级（INLINE_DEGRADE_THRESHOLD=2），整卡退化为纯文本。
    内容图的内联失败按文件路径逐张计数（test_inline_budget_degrades_
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
    """读文件失败不得记账——先记账后读会在读失败时虚耗预算，故先读后记账。"""
    img = tmp_path / "cover.png"
    img.write_bytes(_MINIMAL_PNG)

    def _boom(self: Path) -> bytes:
        raise OSError("读取失败")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    budget = render.InlineBudget()
    assert render._inline_image_sync(str(img), budget) is None
    assert budget.used == 0, "读失败仍记账，预算被虚耗"
    assert budget.degraded == 0, "读失败不应算作预算降级"


# ---------------------------------------------------------------- 缓存键与产物


def test_render_cache_key_tracks_render_config(monkeypatch: Any) -> None:
    """缓存键纳入影响渲染输出的配置态。

    二维码开关 / 评论条数 / 昵称都进模板输出：不进键则改配置后最长
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
    """0 字节缓存产物不算命中：必须重渲。"""
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
    """PNG→JPEG 返回 0 字节或非 JPEG 时拒绝写缓存（魔数校验）。"""
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


class _MediaPath:
    """``MediaContent.path_task`` 替身：await 得 anyio.Path。

    必须是 anyio.Path 而非 pathlib.Path——``MediaContent.get_path`` 还要
    ``await path.stat()``，本地 Path 的 stat() 是同步的，会在那里炸。
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def __await__(self) -> Any:
        import anyio

        async def _resolve() -> Any:
            return anyio.Path(self._path)

        return _resolve().__await__()


def _encode_jpeg(side: int) -> bytes:
    """噪声 JPEG（合法图）。不做截断也不补零凑大小：坏图会让装饰层走 fail-safe
    保留整图，量不出瘦身路径。"""
    from PIL import Image

    raw = bytes(
        v
        for y in range(side)
        for x in range(side)
        for v in ((x * 7 + y * 13) % 256, (x * 11 + y * 5) % 256, (x + y * 3) % 256)
    )
    img = Image.frombytes("RGB", (side, side), raw)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _noise_jpeg(side: int = 420) -> bytes:
    """过装饰层瘦身门槛的噪声 JPEG（~100KB）。"""
    data = _encode_jpeg(side)
    assert len(data) > render._DECORATIVE_SLIM_MIN_BYTES, "夹具太小，测不到瘦身门槛"
    return data


def _small_jpeg() -> bytes:
    """门槛**以下**的合法 JPEG（~1.5KB）：能缩小、但按规则不该被解码替换。

    不能拿 1×1 透明 PNG 当小图夹具——它重编码后不会变小，任何门槛都会被
    「没变小就保留」这条兜底掩盖掉，测不出门槛本身。
    """
    data = _encode_jpeg(64)
    assert len(data) < render._DECORATIVE_SLIM_MIN_BYTES, "夹具太大，测不到门槛"
    return data


def _noise_png_rgba(side: int = 300) -> bytes:
    """带 alpha 的噪声 PNG（合法图，过瘦身门槛）：用于钉住「非 JPEG 保格式」。"""
    from PIL import Image

    raw = bytes(
        v
        for y in range(side)
        for x in range(side)
        for v in ((x * 3) % 256, (y * 5) % 256, (x * y) % 256, (x + y) % 256)
    )
    img = Image.frombytes("RGBA", (side, side), raw)
    buf = BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    assert len(data) > render._DECORATIVE_SLIM_MIN_BYTES, "夹具太小，测不到瘦身门槛"
    return data


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def test_decorative_blur_layers_no_longer_carry_the_full_image(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """一张照片在卡面 HTML 里只留一份整图：装饰层（模糊光晕）改用缩略图。

    瘦身前同一张 4.8MB 照片产出 19.9MB HTML（cover 环境光 + 光晕 + 正文前景
    各一份整图），POST 给公共 t2i 端点的就是这份 HTML，而远端会以
    ``request body read timed out`` 拒收。模板本体逐字节来自上游、桥改不了，
    所以收口在 HTML 面的收尾接缝。

    本用例同时是**适配失效哨兵**：若上游把 ``blur-[...]`` 改名，装饰层不再被
    识别，整图副本数回到 3，这里即红——载荷悄悄长回去比载荷大更难发现。
    """
    pconfig.plite_append_qrcode = False
    img = tmp_path / "photo.jpg"
    img.write_bytes(_noise_jpeg())
    result = _result(url="https://t.bilibili.com/probe-decorative-slim")
    result.content = [ImageContent(path_task=_MediaPath(img))]

    html = await render.build_html(result, "light")

    full = _data_uri("image/jpeg", img.read_bytes())
    assert html.count(full) == 1, (
        f"整图 data URI 在 HTML 里出现 {html.count(full)} 次，装饰层未瘦身（POST 载荷按份数翻倍）"
    )
    # 判据在测试里独立表达（不复用桥的正则，否则尺子就是被检对象）：
    # 带 blur-[ 的 img 一律算装饰层
    blur_srcs = []
    for tag in re.findall(r"<img\b[^>]*>", html):
        if "blur-[" in tag:
            found = re.search(r'src="([^"]+)"', tag)
            assert found is not None, f"装饰层 img 没有 src：{tag[:120]}"
            blur_srcs.append(found.group(1))
    assert blur_srcs, "卡面里找不到装饰层 img——判据已与模板脱钩，瘦身实际未执行"
    for src in blur_srcs:
        assert src != full, "装饰层仍携带整图"
        assert len(src) < len(full) / 4, f"装饰层未换成缩略图（{len(src)} vs {len(full)}）"


def test_slim_targets_blur_layers_and_skips_small_media() -> None:
    """瘦身判据是 class 命中 blur-[...]，且只动过门槛的 data URI。

    前景正文图与图标级小图必须原样保留：前者是卡面唯一的真实图像，后者缩完
    省不下几百字节却要白解码一次。
    """
    big = _data_uri("image/jpeg", _noise_jpeg())
    small = _data_uri("image/jpeg", _small_jpeg())
    html = (
        f'<img src="{big}" class="w-full h-auto object-cover" />'
        f'<img src="{big}" class="absolute blur-[28px]" />'
        f'<img src="{small}" class="blur-[80px]" />'
        '<img src="https://cdn.example.com/x.png" class="blur-[96px]" />'
    )

    out = render._slim_decorative_images_sync(html)

    assert out.count(big) == 1, "前景整图副本被误改，或装饰层未换"
    assert f'src="{small}"' in out, "未过门槛的小图被改写"
    assert 'src="https://cdn.example.com/x.png"' in out, "非 data URI 的 src 被动过"


def test_slim_keeps_png_format_for_alpha_media() -> None:
    """非 JPEG 装饰图保格式重编码：转 RGB 会把透明区填成黑块，光晕层上就是暗斑。"""
    png = _data_uri("image/png", _noise_png_rgba())
    html = f'<img src="{png}" class="absolute blur-[80px]" />'

    out = render._slim_decorative_images_sync(html)

    assert f'src="{png}"' not in out, "带 alpha 的 PNG 装饰层未被瘦身"
    thumb = re.search(r'src="(data:image/[a-z]+;base64,[^"]+)"', out)
    assert thumb is not None
    assert thumb.group(1).startswith("data:image/png"), "带 alpha 的 PNG 被转成了 JPEG"


def test_slim_fail_safe_keeps_full_image_on_undecodable_uri(monkeypatch: Any) -> None:
    """缩略图生成失败按整图保留并告警：载荷优化不得反过来弄坏卡面。"""

    log = _RecordingLogger()
    monkeypatch.setattr(render, "logger", log)
    bogus = _data_uri("image/jpeg", b"n" * 40000)
    html = f'<img src="{bogus}" class="absolute blur-[28px]" />'

    assert render._slim_decorative_images_sync(html) == html, "坏图应原样保留，不做替换"
    assert any("缩略图生成失败" in line for line in log.warnings), f"失败被静默吞掉：{log.warnings}"


async def test_render_html_reports_artifact_problem_and_payload_size(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """产物形态与载荷字节数必须进日志：宿主不校验状态码/Content-Type，桥是唯一
    检测点，诊断若不带证据就退化成「渲染失败」四个字。"""

    log = _RecordingLogger()
    monkeypatch.setattr(render, "logger", log)
    calls: list[int] = []
    err = tmp_path / "io_temp_img_err.jpg"
    err.write_bytes(b'{"code":1,"message":"request body read timed out","data":{}}')

    with pytest.raises(render.RenderArtifactError) as raised:
        await render.render_html("<html><body>卡片</body></html>", _ShotRenderer(err, calls))

    line = next((w for w in log.warnings if "t2i 产物不可用" in w), "")
    assert "request body read timed out" in line, "远端错误文本未进日志，仍无法定责"
    assert re.search(r"html=\d+ 字节", line), "日志缺载荷字节数（上游拒收的第一解释变量）"
    assert re.search(r"耗时=\d+\.\d+s", line), "日志缺单次耗时"
    # 异常 repr 会被 plite_verbose_error 端给用户：远端自由文本只能留在日志
    assert "request body read timed out" not in str(raised.value), (
        f"远端文本泄进异常消息（可能直达会话）：{raised.value!r}"
    )


async def test_render_html_rejects_oversized_artifact(tmp_path: Path, monkeypatch: Any) -> None:
    """产物超上限即拒读：它来自公共渲染端点（不可信输入），无上限的 read_bytes
    加 PIL 解码能把一次恶意响应变成内存事故。"""
    log = _RecordingLogger()
    monkeypatch.setattr(render, "logger", log)
    monkeypatch.setattr(render, "T2I_ARTIFACT_MAX_BYTES", 8)
    big = tmp_path / "io_temp_img_big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    calls: list[int] = []

    with pytest.raises(render.RenderArtifactError, match="too large"):
        await render.render_html("<html></html>", _ShotRenderer(big, calls))


async def test_retry_reuses_the_built_html(tmp_path: Path, monkeypatch: Any) -> None:
    """重试只重发 POST：卡面构建（读媒体、base64、缩略图、模板编译）是确定性的，
    每次重试重建一遍纯属白烧 CPU 与内存，而重试路径恰恰是最常跑的那条。"""
    builds: list[int] = []
    original_build = render.build_html

    async def _counting_build(result: Any, theme: render.Theme) -> str:
        builds.append(1)
        return await original_build(result, theme)

    monkeypatch.setattr(render, "build_html", _counting_build)
    monkeypatch.setattr(render, "logger", _QuietLogger())
    pconfig.plite_append_qrcode = False
    bad = tmp_path / "shot.html"
    bad.write_text("<html>502</html>", encoding="utf-8")
    good = tmp_path / "shot.png"
    good.write_bytes(_MINIMAL_PNG)
    shots = [str(bad), str(bad), str(good)]
    calls: list[int] = []

    class _FlakyRenderer:
        async def html_render(self, *_a: Any, **_k: Any) -> str:
            calls.append(1)
            return shots[len(calls) - 1]

    async def _fake_sleep(seconds: float) -> None:
        return None

    data = await render._render_image_with_retry(
        _result(url="https://t.bilibili.com/probe-html-reuse"),
        renderer=_FlakyRenderer(),
        theme="light",
        sleep=_fake_sleep,
    )

    assert builds == [1], f"重试重建了 HTML（构建 {len(builds)} 次，POST {len(calls)} 次）"
    assert len(calls) == render.T2I_RENDER_ATTEMPTS
    assert data == _MINIMAL_PNG


async def test_render_retries_transient_artifact_failure_and_succeeds(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """端点抖动是秒级可自愈的：非图片产物重试一次即换端点/换时刻成功。

    宿主每次调用重新洗牌端点池（实测仅 2 个 active），桥侧重试等于再掷一次
    硬币；不做重试就等于把这次重掷交给群成员「再发一遍」。
    """
    pconfig.plite_append_qrcode = False
    calls: list[int] = []
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"<html>502</html>")
    good = tmp_path / "good.png"
    good.write_bytes(_MINIMAL_PNG)

    class _FlakyRenderer:
        async def html_render(self, *_a: Any, **_k: Any) -> str:
            calls.append(1)
            return str(bad if len(calls) == 1 else good)

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def _fake_png_to_jpeg(png_data: bytes, quality: int = 85) -> bytes:
        return b"\xff\xd8\xff" + png_data[:8]

    monkeypatch.setattr(render, "_png_to_jpeg", _fake_png_to_jpeg)
    log = _QuietLogger()
    monkeypatch.setattr(render, "logger", log)

    dest = await render.cache_or_render_image(
        _result(url="https://t.bilibili.com/probe-retry-ok"),
        renderer=_FlakyRenderer(),
        sleep=_fake_sleep,
    )

    assert len(calls) == 2, f"应只重试一次就成功（实际 {len(calls)} 次）"
    assert slept == [render.T2I_RENDER_BACKOFF_SECONDS[0]], f"退避序列不符：{slept}"
    assert await dest.read_bytes() == b"\xff\xd8\xff" + _MINIMAL_PNG[:8]


async def test_render_does_not_retry_deterministic_budget_failure(monkeypatch: Any) -> None:
    """卡面自身超预算（InlineBudgetExceeded）不重试：它在构建阶段抛出，落在重试
    循环之外——重发也不会变，白占消息处理器。"""
    builds: list[int] = []

    async def _budget_exceeded(*_a: Any, **_k: Any) -> str:
        builds.append(1)
        raise render.InlineBudgetExceeded("inline degraded media 3 > threshold 2")

    monkeypatch.setattr(render, "build_html", _budget_exceeded)
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(render.InlineBudgetExceeded):
        await render._render_image_with_retry(
            _result(), renderer=object(), theme="light", sleep=_fake_sleep
        )

    assert builds == [1], "确定性失败被重试（每次都注定失败，只是拖长表情悬挂）"
    assert slept == []


async def test_render_stops_retrying_past_deadline(tmp_path: Path, monkeypatch: Any) -> None:
    """累计耗时超上界即放弃剩余尝试：这段代码跑在消息处理器里，会话锁与 🔨
    表情一并挂住，最坏路径必须有界。"""
    calls: list[int] = []
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"<html>502</html>")
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(render, "T2I_RENDER_DEADLINE_SECONDS", -1.0)
    log = _QuietLogger()
    monkeypatch.setattr(render, "logger", log)

    with pytest.raises(render.RenderArtifactError):
        await render._render_image_with_retry(
            _result(),
            renderer=_ShotRenderer(bad, calls),
            theme="light",
            sleep=_fake_sleep,
        )

    assert calls == [1], f"超上界后仍在重试（{len(calls)} 次）"
    assert slept == []


async def test_render_gives_up_after_all_attempts(tmp_path: Path, monkeypatch: Any) -> None:
    """全部尝试耗尽才交给降级路径：尝试次数由退避序列推导，两常量不得各改一半。"""
    calls: list[int] = []
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"")

    async def _fake_sleep(seconds: float) -> None:
        return None

    log = _QuietLogger()
    monkeypatch.setattr(render, "logger", log)

    with pytest.raises(render.RenderArtifactError) as raised:
        await render._render_image_with_retry(
            _result(),
            renderer=_ShotRenderer(bad, calls),
            theme="light",
            sleep=_fake_sleep,
        )

    assert len(calls) == render.T2I_RENDER_ATTEMPTS
    assert "bytes=0" in str(raised.value), "异常消息未带产物字节数"


class _QuietLogger:
    """吞掉日志的 logger 替身（只关心控制流的用例用）。"""

    def __getattr__(self, name: str) -> Any:
        return lambda *_a: None


async def test_media_charged_once_per_resolve_not_per_html_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """预算按**取源**记账，不按 HTML 里的副本数记账。

    一张图在卡面 HTML 里出现 3 份（环境光、光晕、正文前景），但数据层只取源
    一次、只记一次账。这条区分决定了「装饰层重复」是载荷问题（由收尾面瘦身
    解决）而不是预算问题——把它当预算问题去收总预算，只会让多图卡整页降级为
    文本，载荷并不会因此变安全。
    """
    pconfig.plite_append_qrcode = False
    img = tmp_path / "photo.jpg"
    payload = _noise_jpeg()
    img.write_bytes(payload)
    result = _result(url="https://t.bilibili.com/probe-charge-once")
    result.content = [ImageContent(path_task=_MediaPath(img))]

    charges: list[int] = []
    original_try_charge = render.InlineBudget.try_charge

    def _spy(self: render.InlineBudget, size: int) -> bool:
        charges.append(size)
        return original_try_charge(self, size)

    monkeypatch.setattr(render.InlineBudget, "try_charge", _spy)
    # 关掉收尾面瘦身，让模板的多次发射显形
    monkeypatch.setattr(render, "_slim_decorative_images_sync", lambda h: h)

    html = await render.build_html(result, "light")
    full = _data_uri("image/jpeg", payload)

    content_charges = [c for c in charges if c == render.base64_size(len(payload))]
    assert len(content_charges) == 1, (
        f"内容图被记账 {len(content_charges)} 次（应按取源 1 次，副本数是载荷问题）"
    )
    assert html.count(full) == 3, (
        f"关掉瘦身后整图副本数 {html.count(full)} ≠ 3：模板发射次数变了，"
        "本用例与装饰层瘦身判据需一并复核"
    )


def test_single_file_cap_fits_inside_total_budget() -> None:
    """单文件上限的 base64 体量必须放得进总预算。

    若哪天把总预算收到低于单文件上限，一张「被允许内联」的大图会**必然**降级
    成占位图——症状是「图不算大却总缺图」，且没有任何异常与测试提示。
    """
    assert render.base64_size(render.INLINE_MAX_FILE_BYTES) <= render.INLINE_BUDGET_BYTES, (
        f"单文件 {render.INLINE_MAX_FILE_BYTES:,} → base64 "
        f"{render.base64_size(render.INLINE_MAX_FILE_BYTES):,} 已超总预算 "
        f"{render.INLINE_BUDGET_BYTES:,}"
    )


async def test_png_to_jpeg_converts_locally() -> None:
    """桥内真实转换闭环（上游 1.3.8rc6 移除 FFmpeg.png_to_jpeg 后无 fake）：
    合法 PNG 必须转出带 SOI 魔数的 JPEG。"""
    jpeg = await render._png_to_jpeg(_MINIMAL_PNG)
    assert render._is_jpeg(jpeg), "桥内 PNG→JPEG 转换产物非法"
