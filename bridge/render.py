"""渲染桥：ParseResult → 模板 HTML → AstrBot t2i 截图。

模板与 safe_src 过滤器语义照搬上游 main 分支 render 模块（.sync-work 参考
件）；截图引擎用 AstrBot 内置 html_render。AstrBot 自定义模板只走网络
t2i 端点（远端浏览器渲染），读不到本地 file:// 路径，因此本地媒体必须
内联为 base64 data URI；任何渲染失败由调用方降级为 sender 纯文本路径。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import mimetypes
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path as SyncPath
from typing import Any, Literal, cast

import qrcode
from anyio import Path, to_thread
from markupsafe import Markup, escape

from ..vendor.nonebot_plugin_parser_lite.config import _nickname, pconfig
from ..vendor.nonebot_plugin_parser_lite.data import ParseResult
from ..vendor.nonebot_plugin_parser_lite.utils.cache import CacheManager
from ..vendor.nonebot_plugin_parser_lite.utils.ffmpeg import FFmpeg
from . import render_params, texts

PLACEHOLDER_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

INLINE_BUDGET_BYTES = 24 * 1024 * 1024
"""单次渲染允许内联进 HTML 的媒体总预算（base64 编码后字节）。

按 base64 后的实际字节记账（HTML 里 data URI 的真实体量，约原始文件的
4/3）：旧实现按原始字节计，24MB 预算实际会产出约 32MB HTML 整段 POST 给
远程 t2i（2026-09-17 评审 M14）。
"""
INLINE_MAX_FILE_BYTES = 8 * 1024 * 1024
"""单个媒体文件的内联上限（原始文件字节）"""
INLINE_DEGRADE_THRESHOLD = 2
"""允许降级为占位图的媒体数上限，超过即整页降级为文本。

预算按 base64 后字节计，预算用尽等于剩余媒体全部缺图；缺 1~2 张图卡片仍
可读，缺 3 张以上应整体降级，而不是发一张「一半是灰块」且毫无提示的卡。
"""

logger = logging.getLogger(__name__)


class InlineBudgetExceeded(RuntimeError):
    """内联降级媒体数超阈值：整页降级为文本（由调用方降级路径接管）。"""


class RenderArtifactError(RuntimeError):
    """渲染产物不可用（0 字节）：拒绝把它写进缓存或当成缓存命中。"""


def base64_size(raw_size: int) -> int:
    """原始字节数 → base64 编码后字节数（不含 data URI 前缀，量级可忽略）。"""
    return 4 * ((raw_size + 2) // 3)


class InlineBudget:
    """单次渲染的 base64 内联字节预算（每次渲染独立实例，防跨请求串账）。"""

    __slots__ = ("_degraded_paths", "used")

    def __init__(self) -> None:
        self.used = 0
        self._degraded_paths: set[str] = set()

    @property
    def degraded(self) -> int:
        """因超预算/超单文件上限而降级为占位图的媒体数（按文件去重）。

        按路径去重而非按调用次数：模板对同一张图会多次调 safe_src（get_path /
        get_base / get_cover_path 等），按次数计会让阈值随模板调用次数漂移。
        """
        return len(self._degraded_paths)

    def try_charge(self, size: int) -> bool:
        """预算内则记账并返回 True；超预算不记账返回 False。"""
        if self.used + size > INLINE_BUDGET_BYTES:
            return False
        self.used += size
        return True

    def note_degraded(self, path: str) -> None:
        """记一个「因预算降级为占位图」的媒体路径（见 build_html 的整页降级判据）。"""
        self._degraded_paths.add(path)


Theme = Literal["light", "dark"]

TEMPLATES_DIR = SyncPath(__file__).resolve().parent.parent / "templates"

_CSS_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_CSS_REL_RE = re.compile(r'\brel="stylesheet"', re.IGNORECASE)
_CSS_HREF_RE = re.compile(r'\bhref="([^"]+)"', re.IGNORECASE)

_T2I_CARD_WIDTH = render_params.VIEWPORT_WIDTH
"""卡面画布宽 = 上游 get_new_page viewport.width（渲染
参数注入层）。消费注入值而非硬编码，上游调整视口宽时缩放比自动跟随——
硬编码副本会在上游变更时静默产出错误缩放（2026-09-14 双轴评审发现的
双源漂移：注入面有值、桥内另存一份 620）。"""
_T2I_CONTENT_WIDTH = 1280
"""桥侧内容宽目标：实测公共端点视口宽不一致（soulter=800 / rcfortress=
1280，viewport/dsf 参数均被忽略，2026-09-14 逐端点实测），full_page 截图
取「内容宽与视口宽的较大值」——把卡面放大到 1280px 内容宽后，任意端点
出图都无留白条，且与上游 dsf=2 的 1240px 输出同量级。"""
_T2I_ZOOM = _T2I_CONTENT_WIDTH / _T2I_CARD_WIDTH

_T2I_VIEWPORT_PATCH = (
    "<style>"
    f"body{{margin:0 auto;zoom:{_T2I_ZOOM:.6f}}}"
    "html,body{background:#f8fafc}"
    '[data-theme="dark"],[data-theme="dark"] body{background:#020617}'
    "</style>"
)
"""桥侧视口、底色与缩放补丁：远程 t2i 端点忽略 viewport/dsf 参数且视口
宽度因端点而异（2026-09-14 实测 800/1280 两档），620px 卡片直接渲染会
随端点漂移出宽窄不一的留白条（群 1124969653 实发卡 1280×2558，两侧约
330px 空白）。body zoom 放大卡面至内容宽 1280px（zoom 是布局级缩放，
文字按 2.06x 重排保持锐利，等效上游 dsf=2 的输出）——full_page 截图宽
取内容宽，任意端点都满幅无留白。画布底色：模板数据面回归上游逐字节后，
上游模板不含 body 底色规则（2026-09-13 活体对照发现，旧副本冻结了上游
已删除的两条规则），远程端点默认白底会让暗色卡两侧穿帮——底色规则按
主题选择器留在本补丁。"""


def _inline_css_sync(html: str) -> str:
    """把 ``<link rel="stylesheet" href="*.css">`` 替换为内联 ``<style>``。

    远程 t2i 端点（AstrBot html_render 唯一路径）的浏览器解析不了模板目录
    相对路径，外链 CSS 必然 404 → 全卡片渲染为无样式裸 HTML（2026-09-12
    六张生产渲染图视觉取证），因此 CSS 与本地媒体一样必须自包含。样式表
    缺失/越界直接抛错，交由调用方降级为纯文本路径。
    """

    def _sub(m: re.Match[str]) -> str:
        tag = m.group(0)
        if not _CSS_REL_RE.search(tag):
            return tag
        href_m = _CSS_HREF_RE.search(tag)
        if href_m is None:
            raise ValueError(f"stylesheet link missing href: {tag}")
        css_path = (TEMPLATES_DIR / href_m.group(1)).resolve()
        if not css_path.is_relative_to(TEMPLATES_DIR):
            raise ValueError(f"template stylesheet escapes templates dir: {href_m.group(1)}")
        if not css_path.is_file():
            raise ValueError(f"template stylesheet missing: {href_m.group(1)}")
        return "<style>\n" + css_path.read_text(encoding="utf-8") + "\n</style>"

    html = _CSS_LINK_RE.sub(_sub, html)
    if "</head>" in html:
        return html.replace("</head>", _T2I_VIEWPORT_PATCH + "</head>", 1)
    return html + _T2I_VIEWPORT_PATCH


def get_theme() -> Theme:
    """根据配置的白天时间范围返回当前主题（与上游逻辑一致）。"""
    start, end = pconfig.day_range_minutes
    now = datetime.now()
    current = now.hour * 60 + now.minute
    if start == end:
        in_day = False
    elif start < end:
        in_day = start <= current < end
    else:
        in_day = current >= start or current < end
    return "light" if in_day else "dark"


def _inline_image_sync(path_str: str, budget: InlineBudget) -> str | None:
    """本地文件 → base64 data URI；非图片/超预算返回 None 交给调用方降级。

    同步实现：由 safe_src 经 to_thread 调用，读文件+编码（单文件上限 8MB）
    不占事件循环。记账口径为 base64 后的字节，且「读成功后才记账」——旧
    实现先记账后读文件，读失败会漏账虚耗预算（2026-09-17 评审 M14）。
    """
    path = SyncPath(path_str)
    mime = mimetypes.guess_type(path.name)[0]
    if mime is None or not mime.startswith("image/") or not path.is_file():
        return None
    try:
        size = path.stat().st_size
        if size > INLINE_MAX_FILE_BYTES:
            budget.note_degraded(path_str)
            return None
        raw = path.read_bytes()
    except OSError:
        # 读失败（竞态删除/权限）不计账：预算不该被未产出的 data URI 占用
        return None
    if not budget.try_charge(base64_size(len(raw))):
        budget.note_degraded(path_str)
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def _inline_image(path_str: str, budget: InlineBudget) -> str | None:
    return await to_thread.run_sync(_inline_image_sync, path_str, budget)


def make_safe_src(budget: InlineBudget) -> Callable[..., Awaitable[Markup | None]]:
    """构造带内联预算的 safe_src 过滤器（每次渲染独立预算，避免跨请求串账）。

    用法与上游一致：{{ cont | safe_src }}、{{ cont | safe_src("get_cover_path") }}、
    {{ author | safe_src("get_avatar_path", return_none_on_fail=True) }}

    返回 Markup 而非 str：autoescape 开启后普通字符串会被 HTML 转义，而
    data URI 里的 ``&``（SVG/多参数 MIME）转义成 ``&amp;`` 会让图片加载
    失败——safe_src 的产物是桥内自造的受控 URL，标记为安全是正确语义。
    """

    async def safe_src(
        obj: Any,
        method: str = "get_path",
        *,
        return_none_on_fail: bool = False,
    ) -> Markup | None:
        fail = None if return_none_on_fail else Markup(PLACEHOLDER_IMAGE)
        try:
            if obj is None:
                return fail
            attr = getattr(obj, method, None)
            if attr is None:
                logger.warning("对象 %s 不存在方法 '%s'", type(obj).__name__, method)
                return fail
            if not callable(attr):
                logger.warning("%s 的属性 '%s' 不是可调用对象", type(obj).__name__, method)
                return fail
            src = attr()
            if hasattr(src, "__await__"):
                src = await src
            if src is None:
                return fail
            # vendor 返回 anyio.Path / pathlib.Path，统一取文件系统路径
            inlined = await _inline_image(str(src), budget)
            return Markup(inlined) if inlined else fail
        except Exception as e:
            # 上游同处有 3 处 logger.warning；桥此前一律静默 return fail ——
            # vendor 改名这类事故会表现为「卡片上几张图变灰块」而日志里
            # 一个字都没有（2026-09-18 复核）
            logger.warning("safe_src(%s) 处理 %s 时失败: %r", method, type(obj).__name__, e)
            return fail

    return safe_src


_EXTRA_LABELS = {"danmaku": texts.EXTRA_LABEL_DANMAKU, "coin": texts.EXTRA_LABEL_COIN}
"""vendor standalone 的 stats.extra 值为标量（B 站 danmaku/coin），上游
main 模板契约要求值为（标签, 数值）二元组、键为图标类名（macros.jinja
``{% set label, amount = value %}``，标量直供必然 ValueError 降级文本，
2026-09-13 生产流量复现）。标签文案经显示文本注入层逐字取上游 main
parsers/bilibili；vendor roll 到 main 新形状（值已是二元组）
后自动透传、本表退化 no-op。"""


def _translate_stats_extra(stats: Any) -> Any:
    """ACL 翻译：stats.extra 标量值 → main 模板二元组契约（幂等，不改原对象）。

    ``ParseResult`` 是 sender 与 render 共享的同一实例——返回副本而非原地改写，
    与 ``_escaped_author``/``_escaped_comment`` 的副本纪律一致。
    """
    extra = getattr(stats, "extra", None)
    if not isinstance(extra, dict):
        return stats
    return replace(
        stats,
        extra={
            k: (v if isinstance(v, (tuple, list)) else (_EXTRA_LABELS.get(k, k), v))
            for k, v in extra.items()
        },
    )


def _esc(value: str | None) -> str | None:
    """HTML 转义模板插值用的字符串（None 原样透传，供模板做存在性判断）。"""
    return None if value is None else str(escape(value))


def _escaped_author(author: Any) -> Any:
    """作者信息副本：仅转义进模板的展示字段，不动 vendor 对象本身。

    ``ParseResult`` 是 sender 与 render 共享的同一实例，原地改写会污染发送
    到聊天平台的文本（本该显示原样昵称却显示 ``&amp;``）。模板字典必须是
    独立副本。
    """
    if author is None:
        return None
    return replace(
        author,
        name=_esc(author.name),
        location=_esc(author.location),
        description=_esc(author.description),
    )


def _escaped_comment(comment: Any) -> Any:
    """评论文本副本：作者与正文（含图片之外的文本项）按模板插值口径转义。"""
    content = [_esc(item) if isinstance(item, str) else item for item in (comment.content or [])]
    return replace(
        comment,
        author=_escaped_author(comment.author),
        content=content,
        replies=[_escaped_comment(reply) for reply in (comment.replies or [])],
    )


def _escaped_stats(stats: Any) -> Any:
    """统计条目副本：各计数与 ``extra`` 的标签/数值都是解析侧字符串。

    ``extra`` 先经 ``_translate_stats_extra`` 归一为模板要求的二元组契约，
    再按二元组逐项转义——顺序不能反：标量形状下按 ``value[0]`` 取值会把
    数值字符串切碎（"321" → ("3","2")），形状翻译随之失效。
    """
    translated = _translate_stats_extra(stats)
    extra = {
        key: (
            (_esc(value[0]), _esc(value[1]))
            if isinstance(value, (tuple, list)) and len(value) == 2
            else value
        )
        for key, value in (getattr(translated, "extra", None) or {}).items()
    }
    return replace(
        translated,
        view_count=_esc(stats.view_count),
        like_count=_esc(stats.like_count),
        collect_count=_esc(stats.collect_count),
        share_count=_esc(stats.share_count),
        comment_count=_esc(stats.comment_count),
        extra=extra,
    )


def _escaped_extra(extra: Any) -> Any:
    """平台扩展字段副本（音乐专辑/歌词/简介等均为解析侧文本，dict[str, Any]）。"""
    if extra is None:
        return None
    if isinstance(extra, dict):
        return {k: (_esc(v) if isinstance(v, str) else v) for k, v in extra.items()}
    return extra


async def resolve_parse_result(result: ParseResult) -> dict[str, Any]:
    """把 ParseResult 解析为模板字典（与上游 resolve_parse_result 等价）。

    文本字段在此转义，但**只转模板未过滤的那些**——模板是上游
    render/templates 的逐字节镜像（模板数据面，``test_render_templates``
    守护），桥不能为转义去改它，而两个模板对同一字段的处理不一致，转义
    责任因此必须逐字段判定：

    - ``result.title``：**桥转**。default 卡在 macros.jinja:420 套了 ``| e``，
      music 模板（music.html.jinja:6/52）却是**裸插值**——桥若透传，音乐卡
      会被注入原始 HTML。两害相权取安全：桥先转，default 卡上被 ``| e``
      二次转义成可见的 ``&lt;`` 字面量（仅对含特殊字符的标题，观感损失
      可接受；正常标题无特殊字符不受影响），music 卡得到正确转义。
    - ``result.content`` 各项：**模板转**（render_content_items 内逐字段
      ``| e``），桥原样透传，否则二次转义。
    - ``author.name/location/description``、``ai_summary``、``comments``
      的作者与正文、``stats`` 各计数与 extra 标签/数值、``extra``（音乐
      专辑/歌词/简介）、``formatted_datetime``、``bot_name``：**桥转**
      （模板均为裸插值）。
    - ``platform`` / ``rendering_time`` / ``qrcode_path``：桥自造或受控值。

    判据是「模板里这个字段有没有 ``| e``」，且**两个模板都要看**（同名字段
    在 default 与 music 里的过滤状态可以不同，title 即是反例）。改模板插值
    或增删字段时两边必须同看；``tests/test_render_smoke.py`` 的转义用例
    覆盖两种模板。不开启 Jinja autoescape 的理由是模板用 ``~`` 拼装 HTML：
    autoescape 会把 ``(v|e)`` 的 Markup 产物在字符串拼接时重新转义，整张
    卡片退化成标签源码（2026-09-14 实测）。
    """
    data: dict[str, Any] = {
        # title 必须此处转义：default 模板有 `| e`，music 模板没有（见 docstring）
        "title": _esc(result.title),
        "formatted_datetime": _esc(result.formatted_datetime),
        "extra": _escaped_extra(result.extra),
        "platform": result.platform,
        "content": result.content,  # render_content_items 内逐字段 `| e`
        "stats": _escaped_stats(result.stats),  # 内含形状翻译，勿另包一层
        # 切片前必须钳制：WebUI 面板不校验 int 范围，负数会让 [:-1] 变成
        # 「去掉末尾一条」而非「取零条」，评论数随之**非单调**（2026-09-20
        # 审计缺陷 1）。钳制值与缓存键共用 max_comments_count()，两者不会分叉。
        "comments": [_escaped_comment(c) for c in result.comments[: max_comments_count()]],
        "author": _escaped_author(result.author),
        "ai_summary": _esc(result.ai_summary),
        "rendering_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot_name": _esc(_nickname),
    }
    if result.repost:
        data["repost"] = await resolve_parse_result(result.repost)

    if pconfig.append_qrcode:
        # 二维码点阵参数为上游 main 注入值（渲染参数注入层）
        qr = qrcode.QRCode(
            version=render_params.QRCODE_VERSION,
            error_correction=render_params.QRCODE_ERROR_CORRECTION,
            box_size=render_params.QRCODE_BOX_SIZE,
            border=render_params.QRCODE_BORDER,
        )
        qr.add_data(result.url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        cast(Any, img).save(buffer, format="PNG")
        data["qrcode_path"] = (
            f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"
        )
    return data


_SAFE_PLATFORM_NAME = re.compile(r"[a-z0-9_-]+")


def _select_template(result: ParseResult) -> str:
    """上游模板选择规则；不存在的平台/音乐模板回退 default。

    平台名在拼接前做白名单校验：模板名会进 ``FileSystemLoader``，含 ``/``
    或 ``..`` 的值能穿出 ``TEMPLATES_DIR``（``../../etc/passwd.html.jinja``
    实测可解析，2026-09-14 评审 #10）。当前 ``PlatformEnum`` 是值域封闭的
    ``[a-z0-9]`` StrEnum，走不到该路径——但 ``ParseResult.platform`` 是桥
    对外组装的字段，此处按「输入不可信」设防，白名单不匹配即回退 default
    而非抛错（选择模板失败不该让整次渲染失败）。
    """
    default = "default.html.jinja"
    if not result.platform:
        return default
    platform_name = str(result.platform.name).lower()
    if platform_name in {"kugou", "netease", "kuwo", "qsmusic"}:
        candidate = "music.html.jinja"
    elif _SAFE_PLATFORM_NAME.fullmatch(platform_name):
        candidate = f"{platform_name}.html.jinja"
    else:
        return default
    return candidate if (TEMPLATES_DIR / candidate).is_file() else default


async def build_html(result: ParseResult, theme: Theme) -> str:
    """两段式渲染的第一段：本地 Jinja 产出自包含 HTML（冒烟接缝）。

    safe_src 过滤器逐张内联本地媒体为 base64 data URI（远程 t2i 读不到
    本地 file://）；每次渲染独立内联预算，避免跨请求串账。样式表同样
    内联（_inline_css_sync），产出完全自包含 HTML。
    """
    from jinja2 import Environment, FileSystemLoader

    template_data = await resolve_parse_result(result)
    # autoescape 保持关闭：模板会用 `~` 把字面标签与值拼成 HTML 再整体
    # `| safe` 输出，开启 autoescape 后 `| e` 的产物在 `~` 拼接时被二次
    # 转义（Markup.__add__ 重新转义），卡片会渲染成可见的标签源码。
    # 用户可控字段因此在模板内逐点显式 `| e`（见 macros.jinja/music）。
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), enable_async=True)
    budget = InlineBudget()
    env.filters["safe_src"] = make_safe_src(budget)
    template = env.get_template(_select_template(result))
    rendered = await template.render_async(result=template_data, theme=theme)
    if budget.degraded > INLINE_DEGRADE_THRESHOLD:
        # 缺图不再静默：超阈值的降级意味着卡片大面积缺图，整页降级为文本
        # 比发一张「一半是灰块」的卡更诚实（2026-09-17 评审 M14）
        logger.warning(
            "内联降级媒体 %d 张（阈值 %d，预算 %d 字节，已用 %d）：整页降级为文本",
            budget.degraded,
            INLINE_DEGRADE_THRESHOLD,
            INLINE_BUDGET_BYTES,
            budget.used,
        )
        # 异常消息保持 ASCII：CJK 诊断只走上一条日志——桥运行时模块的用户
        # 可见 CJK 文案必须经 texts.py 注入层（tests/test_user_texts.py 的
        # 文案防火墙把 const 与 f-string 部件都算作候选，仅剪日志子树）。
        raise InlineBudgetExceeded(
            f"inline degraded media {budget.degraded} > threshold {INLINE_DEGRADE_THRESHOLD}"
        )
    return await to_thread.run_sync(_inline_css_sync, rendered)


async def render_image(result: ParseResult, renderer: Any, *, theme: Theme) -> bytes:
    """渲染结果卡片长图，返回 PNG 原始字节（上游 render_image 等价）。

    两阶段渲染：桥内 jinja2（build_html）产出完整 HTML，再交给 AstrBot
    html_render 截图——html_render 不支持注册过滤器，因此不能把上游模板
    直接当它的模板传入。renderer 为插件实例（Star 基类提供 html_render
    方法）。
    """
    html = await build_html(result, theme)

    image_path = await renderer.html_render(
        "{{ html }}",
        {"html": html},
        return_url=False,
        options={"type": "png", "full_page": True},
    )

    # 读图为 anyio.Path 异步（内部 to_thread），不阻塞事件循环
    return await Path(str(image_path)).read_bytes()


RENDER_CACHE_REV = "2"
"""桥本地渲染行为版本：桥侧渲染管线变更（如 2026-09-14 zoom 缩放补丁）
时递增，使旧键缓存整体失效——上游模板变更走注入的 RENDER_TEMPLATE_VERSION，
桥自身变更走这里，两把钥匙互不覆盖。"""


MAX_COMMENTS_LIMIT = 100
"""评论条数上界（写入面板 slider 的量程，同时是运行期钳制上界）。

AstrBot 4.28 的插件配置面板对 int 字段**不做范围校验**：`_conf_schema.json`
里的 `minimum`/`maximum` 不是宿主消费的键，`slider` 也只是**额外**渲染一个
滑块，旁边的 `type="number"` 数字输入框仍然可自由输入任意整数（含负数）。
因此非法值必须由运行期钳制兜住——这正是本常量的存在理由（2026-09-20 审计
缺陷 1/2 的修复）。"""


def _clamp_config_int(value: Any, *, minimum: int, maximum: int) -> int:
    """把 WebUI 可自由输入的 int 配置钳制到合法区间。

    面板不校验 ⇒ 负数会畅通无阻地进入消费点：``comments[:-1]`` 是「去掉末尾
    一条」而非「取零条」，会让评论数**非单调**（设 -1 反而比设 0 显示得多）。
    对非 int（None/str 等异常形态）一律回落到下界，宁可少显示也不静默反向。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return minimum
    return max(minimum, min(maximum, value))


def max_comments_count() -> int:
    """本次渲染实际生效的评论条数（已钳制，缓存键与切片共用同一取值）。"""
    return _clamp_config_int(pconfig.max_comments, minimum=0, maximum=MAX_COMMENTS_LIMIT)


def _render_config_rev() -> str:
    """影响渲染输出的配置态短摘要（纳入缓存键）。

    只纳入真正进模板输出的配置：二维码开关、评论条数、机器人昵称。theme
    已在键内（日夜不串图），不重复。缓存键缺这些会让 WebUI 改配置后最长
    vendor 清理周期（2h）内仍发旧图（2026-09-17 评审 M7）。

    评论条数取**钳制后**的值：若用裸配置，非法值（如 -1 与 -2）会算出不同的
    键却产出同一张图，白白多渲染一次；反之钳制后 -1 与 0 同键同图，语义正确。
    """
    payload = repr((pconfig.append_qrcode, max_comments_count(), _nickname))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def render_cache_key(result: ParseResult, theme: Theme) -> str:
    """渲染缓存键：模板族版本 + 桥渲染行为版本 + 主题 + 配置态摘要 + URL。"""
    return (
        f"{render_params.RENDER_TEMPLATE_VERSION}:{RENDER_CACHE_REV}:{theme}"
        f":{_render_config_rev()}:{result.url}"
    )


async def _cache_artifact_usable(path: Path) -> bool:
    """缓存产物可用 = 存在且非空。

    只判存在会把 0 字节产物（PNG→JPEG 失败、上次写入被中断）当成命中缓存
    到下次清理；vendor 每 2h 的清理任务也可能刚好删掉文件（2026-09-17
    评审 L10）。
    """
    try:
        return (await path.stat()).st_size > 0
    except FileNotFoundError:
        return False


async def cache_or_render_image(result: ParseResult, renderer: Any) -> Path:
    """上游 cache_or_render_image 等价移植：命中渲染缓存直接复用。

    以模板族版本+桥渲染行为版本+主题+渲染相关配置态摘要+结果 URL 为稳定键
    生成缓存文件名：产物存在且非空即复用不重渲（跨重启可复用）；否则渲染，
    并把 PNG 转 JPEG（体积约为原图 1/8，多数场景落回图片段而非 5MB 文件段，
    与上游发送语义一致）后原子落盘，并复验产物非空。
    """
    theme = get_theme()
    cache_dir = await CacheManager.ensure_dir(CacheManager.RENDER)
    # 缓存键版本为上游 main 注入值（渲染参数注入层）：上游随模板
    # 变更递增时，sync 流水线自动再生产物、渲染缓存随之整体失效重建；
    # RENDER_CACHE_REV 使桥侧渲染行为变更同样整体失效；配置态摘要使 WebUI
    # 改二维码/评论条数/昵称后立即生效（见 _render_config_rev）。键构成变更
    # 本身即让旧键整体失效，故 RENDER_CACHE_REV 无需为此递增
    key = render_cache_key(result, theme)
    dest = cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, key)}.jpeg"
    if not await _cache_artifact_usable(dest):
        jpeg = await FFmpeg.png_to_jpeg(await render_image(result, renderer, theme=theme))
        if not jpeg:
            logger.warning("渲染产物为空（png_to_jpeg 返回 0 字节），拒绝写入缓存：%s", dest.name)
            raise RenderArtifactError("empty render artifact (png_to_jpeg returned 0 bytes)")
        temp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        try:
            await temp.write_bytes(jpeg)
            await temp.replace(dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                await temp.unlink()
        if not await _cache_artifact_usable(dest):
            with contextlib.suppress(FileNotFoundError):
                await dest.unlink()
            logger.warning("缓存产物写入后为空：%s", dest.name)
            raise RenderArtifactError(f"cache artifact empty after write: {dest.name}")
    result.render_image = dest
    return dest
