"""渲染桥：ParseResult → Theme API v1 数据 → 模板 HTML → AstrBot t2i 截图。

数据面镜像上游 main 分支 render/context.py（Theme API v1：模板只消费
JSON-like 的 ``data`` 根变量，不再接触模型对象与过滤器）；模板为上游
render/templates 的逐字节快照（模板数据面注入层）；截图引擎用 AstrBot
内置 html_render。AstrBot 自定义模板只走网络 t2i 端点（远端浏览器渲染），
读不到本地 file:// 路径，因此本地媒体必须内联为 base64 data URI；任何
渲染失败由调用方降级为 sender 纯文本路径。

已知边界：上游 Theme API 的用户主题目录（plite_theme_dirs/plite_render_theme）
在桥不生效——远端 t2i 无 base_url 相对资源解析，自定义主题目录的
file:// 与外链样式在桥管线里必然穿帮；桥固定渲染内置 default 主题
（templates/theme.json 快照），扩展用户主题是显式的未来缺口。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path as SyncPath
from typing import Any, Literal, cast

import qrcode
from anyio import Path, to_thread
from PIL import Image

from ..vendor.nonebot_plugin_parser_lite.config import _nickname, pconfig
from ..vendor.nonebot_plugin_parser_lite.data import (
    AudioContent,
    Comment,
    GraphicContent,
    ImageContent,
    LinkContent,
    LivePhotoContent,
    MediaContent,
    ParseResult,
    PollContent,
    QuoteContent,
    Stats,
    StickerContent,
    VideoContent,
)
from ..vendor.nonebot_plugin_parser_lite.utils.cache import CacheManager
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

        按路径去重而非按调用次数：同一张媒体可在主卡面/转发卡面被多次取源
        （get_path / get_base / get_cover_path 等），按次数计会让阈值随
        取源次数漂移。
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

_MUSIC_PLATFORMS = frozenset({"kugou", "netease", "kuwo", "qsmusic"})
"""镜像上游 render/theme.py ``MUSIC_PLATFORMS``（Theme API v1 模板选择规则）。"""

_THEME_SCHEMA_VERSION = 1
"""Theme 数据面契约版本（镜像上游 render/theme.py ``THEME_SCHEMA_VERSION``）。"""


def _theme_manifest() -> dict[str, Any]:
    """内置主题清单（templates/theme.json，模板数据面逐字节快照产物）。

    ``data.theme_id`` 从这里取值而非硬编码：上游改主题清单 id 时随 roll
    自动跟随（与渲染参数注入层同一「按源取值」纪律）。
    """
    manifest_text = (TEMPLATES_DIR / "theme.json").read_text(encoding="utf-8")
    return cast(dict[str, Any], json.loads(manifest_text))


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
    相对路径，外链 CSS 必然 404 → 全卡片渲染为无样式裸 HTML，因此 CSS 与
    本地媒体一样必须自包含。样式表缺失/越界直接抛错，交由调用方降级为纯
    文本路径。
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

    同步实现：由 _resolve_src 经 to_thread 调用，读文件+编码（单文件上限 8MB）
    不占事件循环。记账口径为 base64 后的字节，且「读成功后才记账」——若先
    记账后读文件，读失败会漏账虚耗预算。
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


async def _resolve_src(
    obj: Any,
    method: str = "get_path",
    *,
    budget: InlineBudget,
    return_none_on_fail: bool = False,
) -> str | None:
    """safe_src 的内联镜像（上游 render/context.py@main 数据层，Theme API v1）。

    上游把 safe_src 从模板过滤器收进数据层：模板拿到的已是 URI，不再接触
    模型对象。桥镜像的唯一实质差异是 URI 形态——上游产 ``file://``（本地
    浏览器 base_url 可解），远端 t2i 读不到本地路径，必须换成本地内联的
    base64 data URI；失败回退形态与上游同构（PLACEHOLDER_IMAGE 或 None）。

    桥侧记账纪律（桥自有约定，独立于上游语义）：
    - fail 为 PLACEHOLDER 时是**可见灰块**，必须计入整页降级判据——否则
      「方法缺失/异常/None src」的灰块卡会被当成功缓存（灰块永久卡）；
    - 去重键按 reason:type:method 折叠不同对象，是刻意取舍：占位图为 1×1
      透明 GIF（非可见灰块），评论头像/回复头像不带 return_none_on_fail——
      逐对象计数会让多条无头像评论的正常卡片误触整页降级。内容图的预算类
      降级按文件路径逐张计数（_inline_image），不受此折叠影响；结构性失败
      （vendor 改名等）同时命中多个不同 (type, method) 组合，折叠后仍能触发
      阈值；
    - 异常就地 logger.warning（与上游同处一致）：「卡片上几张图变灰块」这类
      事故需要在日志里有定位线索；结构性失败则靠计数超阈触发整页降级告警。
    """
    fail = None if return_none_on_fail else PLACEHOLDER_IMAGE

    def _note_placeholder(reason: str) -> None:
        if fail is not None:
            budget.note_degraded(f"{reason}:{type(obj).__name__}:{method}")

    try:
        if obj is None or not hasattr(obj, method):
            _note_placeholder("obj-none" if obj is None else "no-method")
            return fail
        attr = getattr(obj, method)
        if not callable(attr):
            _note_placeholder("not-callable")
            return fail
        src = attr()
        if hasattr(src, "__await__"):
            src = await src
        if src is None:
            _note_placeholder("src-none")
            return fail
        inlined = await _inline_image(str(src), budget)
        if inlined:
            return inlined
        _note_placeholder("inline-miss")
        return fail
    except Exception as error:
        logger.warning("safe_src(%s) 处理 %s 时失败: %r", method, type(obj).__name__, error)
        _note_placeholder("error")
        return fail


def _json_value(value: Any) -> Any:
    """把扩展字段限制为模板可安全消费的 JSON-like 值（镜像上游）。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_value(item) for item in value]
    return str(value)


def _escape_html(value: Any) -> Any:
    """递归转义进模板的字符串值（镜像上游 render/context.py ``_escape_html``）。

    Theme API v1 的转义责任**全量在数据层**：模板全部裸插值、Environment
    autoescape=False。旧「逐字段判定模板里有没有 ``| e``」的双模板转义矩阵
    随 music 模板与模板内过滤器一起退役——单点转义，无二次转义分叉。
    桥内联产物（data URI）的 base64 字母表不含 ``& < > \"``，``+ /=`` 与
    MIME ``;`` 均非 html.escape 的转义对象，属性上下文按实体正确还原。
    """
    if isinstance(value, str):
        return escape(value, quote=True)
    if isinstance(value, Mapping):
        return {
            escape(key, quote=True) if isinstance(key, str) else key: _escape_html(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [_escape_html(item) for item in value]
    return value


def _task_url(item: MediaContent) -> str | None:
    task = getattr(item, "path_task", None)
    url = getattr(task, "url", None)
    return url if isinstance(url, str) else None


async def _display_size(item: MediaContent) -> str:
    try:
        return await item.get_display_size()
    except Exception:
        return texts.UNKNOWN_SIZE


async def _serialize_author(author: Any, *, budget: InlineBudget) -> dict[str, Any]:
    return {
        "name": author.name,
        "id": author.id,
        "description": author.description,
        "location": author.location,
        "avatar": await _resolve_src(author, "get_avatar_path", budget=budget),
    }


async def _serialize_comment(comment: Comment, *, budget: InlineBudget) -> dict[str, Any]:
    return {
        "author": await _serialize_author(comment.author, budget=budget),
        "content": [await _serialize_content(item, budget=budget) for item in comment.content],
        "timestamp": comment.timestamp,
        "formatted_datetime": comment.formatted_datetime,
        "stats": _serialize_stats(comment.stats),
        "replies": [await _serialize_comment(reply, budget=budget) for reply in comment.replies],
        "parent_author": (
            await _serialize_author(comment.parent_author, budget=budget)
            if comment.parent_author
            else None
        ),
    }


def _serialize_stats(stats: Stats) -> dict[str, Any]:
    extra: list[dict[str, Any]] = []
    for key, value in stats.extra.items():
        label, amount = value[0], value[1]
        extra.append({"key": str(key), "label": _json_value(label), "value": _json_value(amount)})
    return {
        "view_count": stats.view_count,
        "like_count": stats.like_count,
        "collect_count": stats.collect_count,
        "share_count": stats.share_count,
        "comment_count": stats.comment_count,
        "extra": extra,
    }


async def _serialize_content(
    item: Any, *, budget: InlineBudget, is_cover: bool = False
) -> dict[str, Any]:
    if isinstance(item, str):
        return {"type": "text", "text": item}
    if isinstance(item, ImageContent):
        content: dict[str, Any] = {
            "type": "cover" if is_cover else "image",
            "src": await _resolve_src(item, budget=budget),
            "layout": item.layout,
            "is_live": False,
            "source_url": _task_url(item),
        }
        if is_cover:
            content["alt"] = texts.COVER_ALT
        return content
    if isinstance(item, LivePhotoContent):
        return {
            "type": "live_photo",
            "src": await _resolve_src(item, "get_base", budget=budget),
            "layout": "grid",
            "is_live": True,
            "source_url": _task_url(item),
        }
    if isinstance(item, GraphicContent):
        content = {
            "type": "cover" if is_cover else "graphic",
            "src": await _resolve_src(item, budget=budget),
            "alt": item.alt,
            "source_url": _task_url(item),
        }
        if is_cover:
            content["layout"] = "grid"
            content["is_live"] = False
        return content
    if isinstance(item, StickerContent):
        return {
            "type": "sticker",
            "src": await _resolve_src(item, budget=budget, return_none_on_fail=True),
            "size": item.size,
            "description": item.desc,
            "source_url": _task_url(item),
        }
    if isinstance(item, VideoContent):
        return {
            "type": "video",
            "src": await _resolve_src(item, "get_cover_path", budget=budget),
            "duration": item.display_duration,
            "size": await _display_size(item),
            "source_url": _task_url(item),
        }
    if isinstance(item, AudioContent):
        return {
            "type": "audio",
            "duration": item.display_duration,
            "size": await _display_size(item),
            "source_url": _task_url(item),
        }
    if isinstance(item, LinkContent):
        return {
            "type": "link",
            "url": item.url,
            "title": item.title,
            "site_name": item.site_name,
            "description": item.description,
            "icon": await _resolve_src(
                item, "get_icon_path", budget=budget, return_none_on_fail=True
            ),
            "preview": await _resolve_src(
                item, "get_preview_path", budget=budget, return_none_on_fail=True
            ),
        }
    if isinstance(item, QuoteContent):
        return {
            "type": "quote",
            "text": item.text,
            "title": item.title,
            "url": item.url,
            "icon": await _resolve_src(
                item, "get_icon_path", budget=budget, return_none_on_fail=True
            ),
        }
    if isinstance(item, PollContent):
        total = item.option_vote_total
        return {
            "type": "poll",
            "title": item.title,
            "options": [
                {
                    "text": option.text,
                    "votes": option.votes,
                    "percentage": item.option_percentage(option, total),
                }
                for option in item.options
            ],
            "option_vote_total": total,
            "total_votes": item.total_votes,
            "total_voters": item.total_voters,
            "multiple": item.multiple,
            "closed": item.closed,
            "close_at": item.close_at,
        }
    return {"type": "unknown", "text": str(item)}


async def _serialize_result(
    result: ParseResult, *, budget: InlineBudget, max_comments: int
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    cover_found = False
    for item in result.content:
        is_cover = (
            str(result.platform.name) in _MUSIC_PLATFORMS
            and not cover_found
            and (isinstance(item, ImageContent | GraphicContent))
        )
        content.append(await _serialize_content(item, budget=budget, is_cover=is_cover))
        cover_found = cover_found or is_cover

    return {
        "title": result.title,
        "url": result.url,
        "formatted_datetime": result.formatted_datetime,
        "timestamp": result.timestamp,
        "extra": _json_value(result.extra),
        "platform": {
            "id": str(result.platform.name),
            "name": result.platform.display_name,
            "logo": await _resolve_src(result.platform, "get_logo_path", budget=budget),
        },
        "author": await _serialize_author(result.author, budget=budget),
        "content": content,
        "stats": _serialize_stats(result.stats),
        "comments": [
            await _serialize_comment(comment, budget=budget)
            for comment in result.comments[:max_comments]
        ],
        "qrcode": None,
        "ai_summary": result.ai_summary,
        "embed_url": result.embed_url,
        "repost": (
            await _serialize_result(result.repost, budget=budget, max_comments=max_comments)
            if result.repost
            else None
        ),
    }


def _build_qrcode(url: str) -> str:
    """二维码 data URI（镜像上游 render/context.py ``_build_qrcode``）。

    差异：点阵参数不取上游字面量，走渲染参数注入层（四项锚点与上游
    现场 AST 同值，注入层守护）——上游调参时随 roll 自动跟随。
    """
    qr = qrcode.QRCode(
        version=render_params.QRCODE_VERSION,
        error_correction=render_params.QRCODE_ERROR_CORRECTION,
        box_size=render_params.QRCODE_BOX_SIZE,
        border=render_params.QRCODE_BORDER,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    cast(Any, image).save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


async def build_theme_data(
    result: ParseResult, *, color_scheme: Theme, budget: InlineBudget
) -> dict[str, Any]:
    """构造 Theme API v1 模板数据（镜像上游 render/context.py ``build_theme_data``）。

    返回值只含 JSON-like 数据，且逐字镜像上游的构建顺序：**先递归转义整棵
    数据树、再塞二维码**——上游如此，qr 的 base64 因此不被二次转义成
    ``&amp;``。桥打乱该顺序会在模板裸插值下截断二维码。

    桥侧取值差异（其余与上游一致）：``color_scheme`` 由 get_theme() 的日夜
    判定传入（上游同构）；``theme_id`` 恒为内置主题清单 id（桥不解析用户
    主题目录，见模块文档）；``meta.width`` 消费渲染参数注入层；
    ``max_comments`` 用钳制后的桥配置（WebUI 负数防御）、``bot_name`` 取
    vendor 配置全局 ``_nickname``；二维码参数走注入层（_build_qrcode）。
    """
    post = await _serialize_result(result, budget=budget, max_comments=max_comments_count())
    data: dict[str, Any] = _escape_html(
        {
            "schema_version": _THEME_SCHEMA_VERSION,
            "theme": color_scheme,
            "theme_id": _theme_manifest()["id"],
            "post": post,
            "meta": {
                "bot_name": _nickname,
                "rendering_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "width": render_params.VIEWPORT_WIDTH,
            },
        },
    )
    if pconfig.append_qrcode:
        data["post"]["qrcode"] = _build_qrcode(result.url)
    return data


_SAFE_PLATFORM_NAME = re.compile(r"[a-z0-9_-]+")


def _select_template(result: ParseResult) -> str:
    """镜像上游 ThemeDefinition.resolve_template 候选序（Theme API v1）。

    上游候选依次：``{platform}.html.jinja`` →（音乐平台）``music.html.jinja``
    → ``default.html.jinja``，取第一个存在文件——平台专属模板**优先于**音乐
    模板（旧桥序相反，本版随上游镜像）。内置模板族当前只有 default，全部
    平台实际落 default；上游新增平台/音乐模板时模板数据面随 roll 进入
    ``TEMPLATES_DIR``，本选择逻辑自动跟随。

    平台名在拼接前做白名单校验：模板名会进 ``FileSystemLoader``，含 ``/``
    或 ``..`` 的值能穿出 ``TEMPLATES_DIR``（``../../etc/passwd.html.jinja``
    实测可解析）。``ParseResult.platform`` 是桥对外
    组装的字段，此处按「输入不可信」设防，白名单不匹配即回退 default
    而非抛错（选择模板失败不该让整次渲染失败）。
    """
    default = "default.html.jinja"
    if not result.platform:
        return default
    platform_name = str(result.platform.name).lower()
    if not _SAFE_PLATFORM_NAME.fullmatch(platform_name):
        return default
    candidates = [f"{platform_name}.html.jinja"]
    if platform_name in _MUSIC_PLATFORMS:
        candidates.append("music.html.jinja")
    for candidate in candidates:
        if (TEMPLATES_DIR / candidate).is_file():
            return candidate
    return default


async def build_html(result: ParseResult, theme: Theme) -> str:
    """两段式渲染的第一段：本地 Jinja 产出自包含 HTML（冒烟接缝）。

    调用面镜像上游 render_image：数据面为 build_theme_data 的纯 JSON-like
    树，模板唯一根变量 ``data``。autoescape=False 与上游一致——转义责任
    全量在数据层（_escape_html），模板裸插值，开启 autoescape 会二次转义
    卡片正文。本地媒体由 _resolve_src 内联为 base64 data URI（远程 t2i
    读不到本地 file://），每次渲染独立内联预算。样式表同样内联
    （_inline_css_sync），并以此覆盖上游的 _inject_fallback_icon_css：
    内置模板自带 icon.css/tailwind.css link，替换后已在 ``<style>`` 内，
    无需再注入重复副本。
    """
    from jinja2 import Environment, FileSystemLoader

    budget = InlineBudget()
    data = await build_theme_data(result, color_scheme=theme, budget=budget)
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), enable_async=True, autoescape=False)
    template = env.get_template(_select_template(result))
    rendered = await template.render_async(data=data)
    if budget.degraded > INLINE_DEGRADE_THRESHOLD:
        # 缺图不再静默：超阈值的降级意味着卡片大面积缺图，整页降级为文本
        # 比发一张「一半是灰块」的卡更诚实
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
    html_render 截图——远端 t2i 只接收最终 HTML 字符串（``{{ html }}``
    变量注入），渲染编排全在桥侧。renderer 为插件实例（Star 基类提供
    html_render 方法）。
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


RENDER_CACHE_REV = "4"
"""桥本地渲染行为版本：桥侧渲染管线变更（如 2026-09-14 zoom 缩放补丁）
时递增，使旧键缓存整体失效——上游模板变更走注入的 RENDER_TEMPLATE_VERSION，
桥自身变更走这里，两把钥匙互不覆盖。

v4：Theme API v1 数据面重建（post/meta 嵌套、数据层单点转义、stats.extra
形状翻译退役、QR 键位迁移 post.qrcode、JPEG 转换改桥内 PIL）——旧缓存
产物与新数据面的转义/形状语义不同，必须整体失效重建。"""


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
    已在键内（日夜不串图），不重复。缓存键若缺这些，WebUI 改配置后最长
    vendor 清理周期（2h）内仍会发旧图。

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


_JPEG_MAGIC = b"\xff\xd8\xff"
"""JPEG SOI 魔数（前 3 字节）。PNG→JPEG 转换失败可能返回非 JPEG 字节，
仅判非空会把坏产物写进缓存并当命中（灰块/花屏卡永久复用）。"""


async def _png_to_jpeg(png_data: bytes, quality: int = 85) -> bytes:
    """PNG→JPEG（体积约为原图 1/8，与旧上游 FFmpeg.png_to_jpeg 同语义）。

    上游 1.3.8rc6 移除 ``FFmpeg.png_to_jpeg``（渲染改 PNG 分段拼接），
    桥的「落图片段而非 5MB 文件段」发送策略不随之改变，转换收编进桥内
    PIL（qrcode 已依赖 Pillow，无新增依赖）；to_thread 执行不卡事件循环。
    """

    def convert() -> bytes:
        with Image.open(BytesIO(png_data)) as im:
            buf = BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=quality)
            return buf.getvalue()

    return await to_thread.run_sync(convert)


def _is_jpeg(data: bytes) -> bool:
    return len(data) >= 3 and data.startswith(_JPEG_MAGIC)


async def _cache_artifact_usable(path: Path) -> bool:
    """缓存产物可用 = 存在且为合法 JPEG（SOI 魔数 + 非空）。

    只判存在会把 0 字节产物（PNG→JPEG 失败、上次写入被中断）当成命中缓存
    到下次清理；vendor 每 2h 的清理任务也可能刚好删掉文件。魔数校验补上
    「非空但不是 JPEG」的第三类坏产物。
    """
    try:
        async with await path.open("rb") as fh:
            head = await fh.read(3)
        if len(head) < 3 or not head.startswith(_JPEG_MAGIC):
            return False
        return (await path.stat()).st_size > 0
    except OSError:  # 含 FileNotFoundError：文件缺失/不可读一律按缓存未命中
        return False


async def cache_or_render_image(result: ParseResult, renderer: Any) -> Path:
    """上游 cache_or_render_image 等价移植：命中渲染缓存直接复用。

    以模板族版本+桥渲染行为版本+主题+渲染相关配置态摘要+结果 URL 为稳定键
    生成缓存文件名：产物存在且为合法 JPEG 即复用不重渲（跨重启可复用）；
    否则渲染，并把 PNG 转 JPEG（体积约为原图 1/8，多数场景落回图片段而非
    5MB 文件段，与上游发送语义一致）后原子落盘，并复验产物可用。
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
        jpeg = await _png_to_jpeg(await render_image(result, renderer, theme=theme))
        if not _is_jpeg(jpeg):
            logger.warning(
                "渲染产物非 JPEG 或为空（len=%d），拒绝写入缓存：%s", len(jpeg or b""), dest.name
            )
            raise RenderArtifactError("render artifact is not JPEG (png conversion failed?)")
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
            logger.warning("缓存产物写入后不可用：%s", dest.name)
            raise RenderArtifactError(f"cache artifact unusable after write: {dest.name}")
    result.render_image = dest
    return dest
