"""第二层注入：分析数据 + 注入模板 + AstrBot 插件模板 → 仓库工件。

两层注入流水线的第二层——只消费第一层分析数据 ``vendor_analysis.json``（上游
配置面，管线临时产物**不入库**，由 ``scripts/analyze_vendor.py`` 从 vendor
现场再生），实现随上游 roll 的迭代修改：``gen_config.py`` 与
``metadata.yaml``、``README.md`` 经 Jinja 注入模板渲染；``_conf_schema.json``
按 AstrBot 插件模板约定（宿主 schema 接口）纯函数构造：

- ``_conf_schema.json``：AstrBot WebUI 配置面板 schema（宿主生态原生接口）；
- ``gen_config.py``：main.py 消费的字段清单代码（区分上游/桥自有字段、
  启动时告警陈旧配置键）；
- ``metadata.yaml``：AstrBot 插件元数据，desc/许可证/版本由上游元数据注入、
  随 roll 再生（release tag 与版本锁步，release.yml 校验）；仅 name/author/
  usage 为桥身份手写。
- ``README.md``：正文为上游 README 直通，手写面仅模板内桥接说明一节。
- ``texts.py``：桥显示文本模块，值逐字来自上游 main 分支提取，
  main.py/sender.py 的用户可见文案从这里消费；
- ``render_params.py``：桥渲染参数模块，值逐字来自上游 main 分支渲染模块
  提取（渲染参数注入层），render.py/main.py 的渲染/裁剪关键数值
  从这里消费；
- ``templates/*``：卡面模板族（default/music/macros 与 CSS），**逐字节**
  来自上游 main 分支模板快照（模板数据面）——上游模板演进随
  roll 自动再生，不经过 Jinja 二次渲染、不做 EOF 归一化（模板字节的唯一
  权威是上游本体）。

本层零 vendor 依赖（分析在第一层 scripts/analyze_vendor.py），检出本仓库即可
离线运行。桥自有语义（3 字段默认值、cdn_region 选项覆盖）在本层声明——唯一
的例外白名单，属于「一次维护长期有效」的规则层；数据层完全上游驱动。幂等：
重复运行零 diff；``--check`` 供 CI/测试比对。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import jinja2

REPO_ROOT = Path(__file__).resolve().parent.parent

# 桥自有配置键（有序，面板置顶；不透传 vendor configure()）。桥接层自有语义，
# 仅桥自身演进时才需要改这里。
BRIDGE_FIELDS: dict[str, dict[str, Any]] = {
    "plite_render": {
        "description": "启用渲染长图卡片（关闭后直接发送文本/媒体）",
        "type": "bool",
        "default": True,
    },
    "plite_video_file_threshold_mb": {
        "description": "视频改用文件消息发送的大小阈值（MB）",
        "type": "int",
        "default": 100,
    },
    "plite_verbose_error": {
        "description": "解析失败时向会话发送简短错误说明（默认仅回应表情并记日志）",
        "type": "bool",
        "default": False,
    },
}

# 上游仅以描述文字声明取值而未类型化的字段（上游改为 StrEnum 后，移除覆盖
# 即可随分析产物自动生成选项；Literal 形态需在第一层扩充分析规则）
STRING_OPTIONS: dict[str, list[str]] = {
    "plite_bili_cdn_region": ["zh", "en", "ja", "proxy"],
}

# 需要在**描述文字**上补写合法区间的上游 int 字段。
#
# 存在的理由：AstrBot 4.28 的插件配置面板对 int 字段不做范围校验——
# `minimum`/`maximum` 不是宿主消费的键（写进 schema 是**假保护**），`slider`
# 也只是额外渲染一个滑块，旁边仍有可自由输入的数字框（含负数）。既然面板
# 无法拦住非法值，至少要让**描述**如实说明代码在运行期会钳制到什么区间，
# 否则用户按描述以为自己受保护（2026-09-20 审计缺陷 1/2/7）。
#
# 注意：这里只改描述，**真正的强制力在运行期钳制**：
#   - bridge/render.py   max_comments_count()            → [0, MAX_COMMENTS_LIMIT]
#   - bridge/sender.py   forward_text_threshold()         → [1, 4500]
#   - bridge/sender.py   _clamp_lazy_timeout()            → [5, 300]
# 二者与本表的区间由 tests/test_config_bounds.py 机械对账，防止描述漂移。
RANGE_NOTES: dict[str, str] = {
    "plite_max_comments": "有效范围 0-100，超出范围按边界值处理",
    "plite_forward_text_threshold": "有效范围 1-4500，超出范围按边界值处理",
    "plite_lazy_download_timeout": "有效范围 5-300，超出范围按边界值处理",
}

# 上游描述里已经写明的上界（形如「(最大4500)」）。命中时不再追加区间注记，
# 只补下界说明——否则会出现「…强制转发(最大4500)（有效范围 1-4500…）」
# 这种同义重复，读起来像是两个不同来源的约束在打架。
_DESC_UPPER_BOUND_RE = re.compile(r"[（(]\s*最大\s*(\d+)\s*[）)]")

ANALYSIS_PATH = REPO_ROOT / "vendor_analysis.json"
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"
GEN_CONFIG_PATH = REPO_ROOT / "bridge" / "gen_config.py"
METADATA_PATH = REPO_ROOT / "metadata.yaml"
README_PATH = REPO_ROOT / "README.md"
TEXTS_PATH = REPO_ROOT / "bridge" / "texts.py"
RENDER_PARAMS_PATH = REPO_ROOT / "bridge" / "render_params.py"
RENDER_TEMPLATES_DIR = REPO_ROOT / "templates"
TEMPLATES = REPO_ROOT / "scripts" / "templates"

# 桥自有渲染模板白名单：上游快照之外的 templates/ 文件（桥自加模板时在此
# 登记，否则再生器的陈旧清理会将其删除）。当前为空——桥模板与上游全等。
BRIDGE_TEMPLATE_EXTRAS: frozenset[str] = frozenset()

# AstrBot 插件模板的固定面（metadata.yaml 除 version 外的宿主约定内容）；
# 仅桥自身演进时才需要改这里。
# 手写面（桥身份、桥展示文案与桥用法，上游无对应数据）。字段口径依 AstrBot
# StarMetadata：必填 name/desc/version/author，可选 display_name/short_desc/
# repo/astrbot_version/support_platforms。其中 version 由第一层上游元数据注入
# （upstream_meta），展示文案为桥身份手写（面向 AstrBot 用户的中文，与
# usage/README 桥接说明同源）；上游 description/license 仅进溯源注释。
PLUGIN_META: dict[str, Any] = {
    "name": "astrbot_plugin_parser_lite",
    "author": "Chuubururin",
    "display_name": "链接解析 Lite",
    "desc": (
        "自动解析 B 站/抖音/小红书等平台分享链接与 JSON 卡片并转发为内容卡片；"
        "nonebot-plugin-parser-lite 的 AstrBot 桥接版"
    ),
    "short_desc": "自动解析社交平台分享链接并转发内容",
    "repo": "https://github.com/Chuubururin/astrbot_plugin_parser_lite",
    # 与 ci.yml / CONTRIBUTING.md 的 pip install --no-deps "astrbot>=4.28,<5" 同口径
    "astrbot_version": ">=4.28,<5",
    # 只背书真实验证过的平台：aiocqhttp（OneBot v11）——本插件的主战场。
    # 代码本身与平台无关，但不做没验证过的背书，以免把兼容性写成承诺。
    "support_platforms": ["aiocqhttp"],
    # 插件市场分类与搜索用（宿主 4.28 的 StarMetadata 不解析此字段，
    # 由市场侧消费；见官方「发布插件到插件市场」文档）
    "tags": ["工具"],
    "usage": [
        "群内发送 B 站/抖音/小红书等分享链接或 JSON 卡片，自动解析并转发内容",
        "配置项请在 AstrBot WebUI 插件页中调整",
    ],
}

_ASTRBOT_TYPES = frozenset({"string", "text", "int", "float", "bool", "list", "object"})

# 与第一层 analyze_vendor.py 同规则的汇点复检（防御纵深：手改产物或伪造
# provenance 均在渲染前被拒）
_VERSION_RE = re.compile(r"\d(?:[\w.+-]*\w)?\Z")

# 显示文本防火墙字节上限：与 scripts/extract_display_texts.py、
# scripts/analyze_vendor.py 的 _TEXT_MAX_BYTES 同值（三层防火墙常量），
# 一致性由 tests/test_user_texts.py 机械比对守护
_TEXT_MAX_BYTES = 1024

# 两类模板共用。autoescape 是代码生成注入防线：即使第一层字段名/版本号校验被
# 绕过，变量中的引号也会被转义为 HTML 实体，无法从生成代码的字符串字面量或
# YAML 裸值中逃逸；当前全部输入（字段标识符、bool/int repr、手写元数据、受控
# 版本号）转义后字节不变，由第二层 --check 与契约测试守护。keep_trailing_newline
# 保证模板末行换行即工件末行换行（PEP 8 / ruff W292）
ENVIRONMENT = jinja2.Environment(autoescape=True, keep_trailing_newline=True)


def _load_analysis() -> dict[str, Any]:
    """读取第一层产物；缺失或非分析产物时响亮失败（第二层不做内联分析）。"""
    if not ANALYSIS_PATH.exists():
        raise SystemExit(
            f"缺少第一层分析产物 {ANALYSIS_PATH.name}，请先运行 scripts/analyze_vendor.py",
        )
    data = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8"))
    provenance = data.get("_provenance") if isinstance(data, dict) else None
    if (
        not isinstance(provenance, str)
        or not provenance.startswith("scripts/analyze_vendor.py")
        or not isinstance(data, dict)
        or "config_fields" not in data
    ):
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 不是第一层分析产物（缺 _provenance/config_fields），"
            "请重跑 scripts/analyze_vendor.py",
        )
    version = data.get("upstream_version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 的 upstream_version 格式异常（可能被手改），"
            "请重跑 scripts/analyze_vendor.py",
        )
    upstream_meta = data.get("upstream_meta")
    if (
        not isinstance(upstream_meta, dict)
        or not isinstance(upstream_meta.get("description"), str)
        or not isinstance(upstream_meta.get("license"), str)
        or not isinstance(upstream_meta.get("readme"), str)
    ):
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 的 upstream_meta 缺失或形态异常（可能被手改），"
            "请重跑 scripts/analyze_vendor.py",
        )
    # 显示文本汇点复检（与第一层 _upstream_texts 同规则，防御手改分析产物）
    upstream_texts = data.get("upstream_texts")
    if not isinstance(upstream_texts, dict) or not upstream_texts:
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 的 upstream_texts 缺失（可能被手改），"
            "请重跑 scripts/analyze_vendor.py",
        )
    for key, entry in upstream_texts.items():
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or not isinstance(entry, dict)
            or not isinstance(entry.get("value"), str)
            or "\r" in entry["value"]
            or len(entry["value"].encode("utf-8")) > _TEXT_MAX_BYTES
        ):
            raise SystemExit(
                f"{ANALYSIS_PATH.name} 的 upstream_texts[{key!r}] 形态异常（可能被手改），"
                "请重跑 scripts/analyze_vendor.py",
            )
    # 渲染参数汇点复检（与第一层 _upstream_render_params 同规则，防御手改）
    upstream_render_params = data.get("upstream_render_params")
    if not isinstance(upstream_render_params, dict) or not upstream_render_params:
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 的 upstream_render_params 缺失（可能被手改），"
            "请重跑 scripts/analyze_vendor.py",
        )
    for key, entry in upstream_render_params.items():
        value = entry.get("value") if isinstance(entry, dict) else None
        bad_str = isinstance(value, str) and (
            "\r" in value or len(value.encode("utf-8")) > _TEXT_MAX_BYTES
        )
        bad_int = isinstance(value, int) and not isinstance(value, bool) and not 0 <= value < 2**31
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or not isinstance(entry, dict)
            or not isinstance(value, str | int)
            or isinstance(value, bool)
            or bad_str
            or bad_int
        ):
            raise SystemExit(
                f"{ANALYSIS_PATH.name} 的 upstream_render_params[{key!r}] 形态异常（可能被手改），"
                "请重跑 scripts/analyze_vendor.py",
            )
    # 渲染模板汇点复检（与第一层 _upstream_render_templates 同规则，防御手改）
    upstream_render_templates = data.get("upstream_render_templates")
    if (
        not isinstance(upstream_render_templates, dict)
        or not isinstance(upstream_render_templates.get("files"), dict)
        or not upstream_render_templates["files"]
        or not isinstance(upstream_render_templates.get("source_revision"), str)
    ):
        raise SystemExit(
            f"{ANALYSIS_PATH.name} 的 upstream_render_templates 缺失（可能被手改），"
            "请重跑 scripts/analyze_vendor.py",
        )
    for name, content in upstream_render_templates["files"].items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(jinja|css|json)", name)
            or not isinstance(content, str)
            or not content
        ):
            raise SystemExit(
                f"{ANALYSIS_PATH.name} 的 upstream_render_templates[{name!r}] 形态异常"
                "（可能被手改），请重跑 scripts/analyze_vendor.py",
            )
    return data


def _astrbot_type(field: dict[str, Any]) -> tuple[str, list[Any] | None]:
    """分析产物 kind → AstrBot schema (type, options)。唯一的类型映射规则表。"""
    kind = field["kind"]
    if kind in ("bool", "int", "float", "string"):
        return kind, None
    if kind == "list":
        return "list", None
    if kind == "enum":
        # 标量枚举：StrEnum → string，数值枚举 → int
        return field["value_kind"], field["options"]
    if kind == "enum_list":
        return "list", field["options"]
    raise SystemExit(
        f"分析产物出现未知类型形态 {kind!r}，请复核 scripts/analyze_vendor.py 的分析规则",
    )


def _default_value(name: str, field: dict[str, Any], astr_type: str) -> Any:
    default = field["default"]
    if default is None:
        if astr_type == "string":
            return ""  # AstrBot 面板需要具体默认值；可空字符串统一表达为空串
        raise SystemExit(
            f"上游字段 {name} 类型为 {astr_type} 但默认值为 null——面板需要具体默认值，"
            "请在上游补默认值或扩充空值归一规则",
        )
    return default


def build_conf_schema(analysis: dict[str, Any] | None = None) -> str:
    """生成 _conf_schema.json 内容（纯函数，幂等）。analysis 缺省时现场加载。"""
    analysis = analysis if analysis is not None else _load_analysis()
    vendor_fields: dict[str, dict[str, Any]] = analysis["config_fields"]
    clash = set(vendor_fields) & set(BRIDGE_FIELDS)
    if clash:
        # 上游与桥自有同名时，静默合并会覆盖桥条目且 main.py 透传会误挡真上游字段
        raise SystemExit(f"上游新增了与桥自有配置同名的字段 {sorted(clash)}，需人工裁决归属后再生")
    stale_options = set(STRING_OPTIONS) - set(vendor_fields)
    if stale_options:
        # 覆盖指向已不存在的字段时静默失效会让面板退化为自由文本，必须响亮
        raise SystemExit(
            f"STRING_OPTIONS 覆盖指向已不存在的上游字段 {sorted(stale_options)}，请清理或改名",
        )
    stale_range_notes = set(RANGE_NOTES) - set(vendor_fields)
    if stale_range_notes:
        # 同 STRING_OPTIONS：区间注记指向已不存在的字段时，描述会静默失去
        # 「代码会钳制」的说明，用户重新回到「以为自己受保护」的状态
        raise SystemExit(
            f"RANGE_NOTES 覆盖指向已不存在的上游字段 {sorted(stale_range_notes)}，请清理或改名",
        )
    schema: dict[str, dict[str, Any]] = {}
    for name, bridge_entry in BRIDGE_FIELDS.items():
        schema[name] = dict(bridge_entry)
    for name, field in vendor_fields.items():
        astr_type, options = _astrbot_type(field)
        if astr_type not in _ASTRBOT_TYPES:
            raise SystemExit(f"映射产生了非 AstrBot schema 类型 {astr_type!r}（字段 {name}）")
        description = field["description"]
        note = RANGE_NOTES.get(name)
        if note and astr_type == "int" and note not in description:
            # 追加式改写：上游描述原文逐字保留在前，桥的区间说明补在后——
            # 避免覆盖上游文案（数据层上游驱动），同时让区间可被用户读到
            stated = _DESC_UPPER_BOUND_RE.search(description)
            lower = re.search(r"(\d+)-", note)
            if stated and lower and stated.group(1) in note:
                # 上游已写明上界且与代码一致 → 只补下界，避免同义重复
                note_lower = f"最小 {lower.group(1)}，超出范围按边界值处理"
                description = f"{description}（{note_lower}）"
            else:
                description = f"{description}（{note}）"
        entry: dict[str, Any] = {
            "description": description,
            "type": astr_type,
            "default": _default_value(name, field, astr_type),
        }
        if astr_type == "string" and name in STRING_OPTIONS:
            entry["options"] = STRING_OPTIONS[name]
        elif options is not None:
            entry["options"] = options
        schema[name] = entry
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def build_gen_config(analysis: dict[str, Any] | None = None) -> str:
    """渲染 gen_config.py（代码工件经 Jinja 模板注入生成）。"""
    analysis = analysis if analysis is not None else _load_analysis()
    template = ENVIRONMENT.from_string(
        (TEMPLATES / "gen_config.py.jinja").read_text(encoding="utf-8"),
    )
    return template.render(
        vendor_version=analysis["upstream_version"],
        vendor_fields=sorted(analysis["config_fields"]),
        bridge_fields=sorted(BRIDGE_FIELDS),
        # repr 保证布尔/整数/字符串默认值都渲染为合法 Python 字面量
        bridge_defaults={name: repr(spec["default"]) for name, spec in BRIDGE_FIELDS.items()},
    )


def _assert_upstream_bytes_passthrough(rendered: str, upstream_meta: dict[str, str]) -> None:
    """汇点断言：上游 description/license 必须逐字节出现在 metadata 中。

    模板环境开了 ``autoescape=True``（代码生成注入防线），而 Jinja 的
    autoescape 会把 ``&``/``<``/``>``/``'`` 改写为 HTML 实体（``&amp;`` 等）
    ——上游元数据若含这些字符，生成物会与「数据层完全上游驱动」的
    承诺静默偏离，且 ``--check`` 自比对（比的是「重新生成的结果」而非
    「上游原文」）不会发现（2026-09-17 评审 M15）。第一层
    ``analyze_vendor._YAML_SAFE_RE`` 已前置拒绝，此处是纵深防御：
    绕过第一层也在渲染汇点响亮失败。
    """
    for label in ("description", "license"):
        value = upstream_meta.get(label, "")
        if value and value not in rendered:
            raise SystemExit(
                f"metadata 未逐字节透传上游 {label}"
                f"（疑似 Jinja autoescape 改写为 HTML 实体）：{value!r}",
            )


def build_metadata(analysis: dict[str, Any] | None = None) -> str:
    """渲染 metadata.yaml（desc/许可证/版本由上游元数据注入，桥身份手写）。"""
    analysis = analysis if analysis is not None else _load_analysis()
    template = ENVIRONMENT.from_string(
        (TEMPLATES / "metadata.yaml.jinja").read_text(encoding="utf-8"),
    )
    meta = dict(PLUGIN_META)
    # 预拼列表行，避免模板内 for 循环的空白控制噪音
    meta["usage_lines"] = [f"    - {line}" for line in meta["usage"]]
    meta["support_platform_lines"] = [f"  - {name}" for name in meta["support_platforms"]]
    meta["tag_lines"] = [f"  - {tag}" for tag in meta["tags"]]
    rendered = template.render(
        meta=meta,
        upstream_version=analysis["upstream_version"],
        upstream_meta=analysis["upstream_meta"],
    )
    _assert_upstream_bytes_passthrough(rendered, analysis["upstream_meta"])
    return rendered


def build_readme(analysis: dict[str, Any] | None = None) -> str:
    """渲染 README.md（正文为上游 README 直通，手写面仅模板内桥接说明）。"""
    analysis = analysis if analysis is not None else _load_analysis()
    template = ENVIRONMENT.from_string(
        (TEMPLATES / "README.md.jinja").read_text(encoding="utf-8"),
    )
    return template.render(
        meta=PLUGIN_META,
        upstream_version=analysis["upstream_version"],
        upstream_meta=analysis["upstream_meta"],
        vendor_count=len(analysis["config_fields"]),
        bridge_count=len(BRIDGE_FIELDS),
    )


def build_texts(analysis: dict[str, Any] | None = None) -> str:
    """渲染 texts.py（桥显示文本：值逐字来自上游 main 分支提取）。"""
    analysis = analysis if analysis is not None else _load_analysis()
    template = ENVIRONMENT.from_string(
        (TEMPLATES / "texts.py.jinja").read_text(encoding="utf-8"),
    )
    # json.dumps（ensure_ascii=False）预渲染为完整 Python 双引号字符串字面量
    # （引号/换行/反斜杠全部转义，无法逃逸，且与 ruff format 双引号偏好一致），
    # 模板侧 |safe 防 autoescape 破坏其定界引号
    entries = [
        {
            "name": key.upper(),
            "literal": json.dumps(entry["value"], ensure_ascii=False),
            "source": entry["source"],
            "placeholders": entry.get("placeholders", ""),
        }
        for key, entry in analysis["upstream_texts"].items()
    ]
    # 迭代体自带行尾换行，行尾统一为单换行（模板 EOF 换行不再叠加）
    return template.render(entries=entries).rstrip("\n") + "\n"


def build_render_params(analysis: dict[str, Any] | None = None) -> str:
    """渲染 render_params.py（桥渲染参数：值逐字来自上游 main 分支提取）。"""
    analysis = analysis if analysis is not None else _load_analysis()
    template = ENVIRONMENT.from_string(
        (TEMPLATES / "render_params.py.jinja").read_text(encoding="utf-8"),
    )
    # str 经 json.dumps 预渲染为完整双引号字符串字面量、int 经 repr 渲染为
    # 整数字面量（模板侧 |safe 防 autoescape 破坏定界引号，同 build_texts）
    entries = [
        {
            "name": key.upper(),
            "literal": (
                json.dumps(entry["value"], ensure_ascii=False)
                if isinstance(entry["value"], str)
                else repr(entry["value"])
            ),
            "source": entry["source"],
        }
        for key, entry in analysis["upstream_render_params"].items()
    ]
    return template.render(entries=entries).rstrip("\n") + "\n"


def build_template_files(analysis: dict[str, Any]) -> dict[Path, str]:
    """组装渲染模板工件（逐字节直通上游快照，模板数据面）。

    刻意不走 EOF 归一化与 Jinja 二次渲染：模板字节的唯一权威是上游本体，
    任何归一化都会破坏与快照的逐字节等价（活体对照与 --check 的判据）。
    """
    files: dict[str, str] = analysis["upstream_render_templates"]["files"]
    return {RENDER_TEMPLATES_DIR / name: content for name, content in sorted(files.items())}


def expected_template_names(analysis: dict[str, Any]) -> frozenset[str]:
    """templates/ 目录的期望文件名全集（上游快照 + 桥自有白名单）。

    再生器据此清理上游已删除/改名的陈旧模板文件；check 模式据此点名漂移。
    """
    return frozenset(analysis["upstream_render_templates"]["files"]) | BRIDGE_TEMPLATE_EXTRAS


def build_artifacts(analysis: dict[str, Any]) -> dict[Path, str]:
    """组装全部派生工件——check、write 与测试共用的单一装配点。

    EOF 规范化：代码/文档工件一律以恰好一个换行收尾，与 pre-commit
    end-of-file-fixer 约定对齐，避免卫生钩子与再生器互相打架。渲染模板
    例外——逐字节直通（build_template_files）。
    """
    raw = {
        SCHEMA_PATH: build_conf_schema(analysis),
        GEN_CONFIG_PATH: build_gen_config(analysis),
        METADATA_PATH: build_metadata(analysis),
        README_PATH: build_readme(analysis),
        TEXTS_PATH: build_texts(analysis),
        RENDER_PARAMS_PATH: build_render_params(analysis),
    }
    artifacts = {path: content.rstrip("\n") + "\n" for path, content in raw.items()}
    artifacts.update(build_template_files(analysis))
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从第一层分析产物渲染仓库工件（第二层注入）")
    parser.add_argument("--check", action="store_true", help="只比对不写入，有过期工件时退出码 1")
    args = parser.parse_args(argv)

    # 全部工件共用同一次分析加载（此前每个 build_* 各读一遍分析文件）
    analysis = _load_analysis()
    artifacts = build_artifacts(analysis)
    if args.check:
        stale = [
            str(path)
            for path, content in artifacts.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        extras = sorted(
            p.name
            for p in RENDER_TEMPLATES_DIR.iterdir()
            if p.name not in expected_template_names(analysis)
        )
        if stale or extras:
            print(
                f"生成工件过期（请重跑 scripts/generate_config.py）：{stale or '无'}；"
                f"templates/ 陈旧文件（上游已删除/改名，或登记 BRIDGE_TEMPLATE_EXTRAS）：{extras}",
                file=sys.stderr,
            )
            return 1
        print("生成工件与重新生成一致")
        return 0

    for path, content in artifacts.items():
        path.write_text(content, encoding="utf-8")
    print(f"已生成 {len(artifacts)} 个工件：{', '.join(path.name for path in artifacts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
