"""第一层注入：上游 introspection → 代码注入模板数据 vendor_analysis.json。

两层注入流水线的第一层——sync-upstream 把上游源码注入 vendor/ 之后，本脚本
自动分析其公开配置面，产出 JSON 模板数据（代码注入模板的注入源），随上游演进
由 CI/CD 自动再生。分析源（单一事实源，全部来自上游本体）：

- ``Config.model_fields``：字段名、注解（归一化为 kind/value_kind/options）、
  默认值；
- 字段 docstring（AST 提取，pydantic 不暴露）；
- 枚举成员、上游版本号与项目元数据（description/license/README 原文）；
- 上游 main 分支显示文本提取产物（scripts/extract_display_texts.py 在 sync
  工作流生成并入库）——桥内用户可见文案的注入源；
- 上游 main 分支渲染参数提取产物（scripts/extract_render_params.py 在 sync
  工作流生成并入库）——桥内渲染/裁剪关键参数的注入源；
- 上游 main 分支渲染模板提取产物（scripts/extract_render_templates.py 在
  sync 工作流生成并入库）——桥内 templates/ 卡面模板族的注入源。

第二层 ``scripts/generate_config.py`` 只消费该产物渲染仓库工件，不再 import
vendor——分析层需要 vendor 可导入，模板渲染层随处可跑。分析产物是管线
临时中间件：**不入库**（.gitignore），上游元信息数据不由本仓库维护，由
本脚本随时从 vendor（上游快照）现场再生。

本模块模块级零 vendor 依赖（离线承诺）：上游 Config 在 build_analysis
首次调用时才经 ``_config_cls`` 导入，故 --offline 消费入库分析产物的路径不会
拉起 vendor 包（实证：模块级导入会加载 31 个 nonebot_plugin_parser_lite.* 模块）。

响亮失败：未知类型形态、字段缺描述、动态默认（default_factory 产物）不可
序列化。幂等：重复运行零 diff（新鲜度由测试全管线断言）。
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import tomllib
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_PATH = REPO_ROOT / "vendor_analysis.json"
# JSON 无注释，来源契约写在键里；第二层据此拒收非分析产物的输入
PROVENANCE = "scripts/analyze_vendor.py 生成（第一层注入产物，勿手改）"

# 上游 Config 的模块级占位（零 vendor 依赖，见模块文档）：首次使用时才由
# _config_cls 现场导入并回填。该属性同时是注入防火墙契约测试的注入点
# （tests/test_codegen.py 以 monkeypatch 替换它注入敌意配置面）
Config: Any = None


def _config_cls() -> Any:
    """加载上游 Config（首次调用时才 import vendor；vendor.path 需先就位）。"""
    global Config
    if Config is None:
        if str(REPO_ROOT.parent) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT.parent))
        # vendor 导入前必须就位（vendor.path 导入时读取；introspection 不落盘，
        # 临时目录仅为本进程的一次性占位，进程退出即由系统回收）
        os.environ.setdefault("PARSER_LITE_BASE_DIR", tempfile.mkdtemp(prefix="plite-analyze-"))
        from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import (
            Config as _VendorConfig,
        )

        Config = _VendorConfig
    return Config


def _field_docstrings() -> dict[str, str]:
    """AST 提取字段 docstring：上游把描述写在字段注解下一行的字符串里。

    从 vendor 快照源文件读取（与其他提取器同一数据面），不依赖运行时
    introspection——inspect.getsource 对 .pyc-only 安装、源码缓存缺失等
    环境脆弱，且与「vendor 即真值」的快照纪律不一致。
    """
    source = (REPO_ROOT / "vendor" / "nonebot_plugin_parser_lite" / "config.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Config"), None)
    if cls is None:
        raise SystemExit(
            "上游 config.py 中未找到 Config 类（可能已改名），请复核 scripts/analyze_vendor.py",
        )
    docs: dict[str, str] = {}
    body = cls.body
    for i, node in enumerate(body):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        nxt = body[i + 1] if i + 1 < len(body) else None
        if (
            isinstance(nxt, ast.Expr)
            and isinstance(nxt.value, ast.Constant)
            and isinstance(nxt.value.value, str)
        ):
            docs[node.target.id] = nxt.value.value.strip()
    return docs


def _unwrap_optional(annotation: Any) -> Any:
    """``str | None`` → ``str``：可空性在 schema 侧统一表达为具体类型 + 空默认。"""
    if get_origin(annotation) in (UnionType, Union):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _shape(annotation: Any) -> dict[str, Any]:
    """vendor 注解 → 归一化形态 {kind, value_kind?, options?}。唯一的分析规则表。"""
    annotation = _unwrap_optional(annotation)
    if annotation is bool:  # 须先于 int：bool 是 int 的子类
        return {"kind": "bool"}
    if annotation is int:
        return {"kind": "int"}
    if annotation is float:
        return {"kind": "float"}
    if annotation is str:
        return {"kind": "string"}
    if get_origin(annotation) is list:
        item = get_args(annotation)[0]
        if isinstance(item, type) and issubclass(item, Enum):
            return {
                "kind": "enum_list",
                "value_kind": "string" if issubclass(item, str) else "int",
                "options": [e.value for e in item],
            }
        return {"kind": "list"}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return {
            "kind": "enum",
            "value_kind": "string" if issubclass(annotation, str) else "int",
            "options": [e.value for e in annotation],
        }
    raise SystemExit(
        f"配置字段出现未知类型形态，请在 scripts/analyze_vendor.py 扩充分析规则：{annotation!r}",
    )


def _json_safe(default: Any) -> Any:
    """默认值归一化为 JSON 可表达；动态默认（default_factory 产物等）响亮失败。"""
    if isinstance(default, Enum):
        return default.value
    if isinstance(default, (list, tuple)):
        return [_json_safe(item) for item in default]
    if default is None or isinstance(default, (bool, int, float, str)):
        return default
    raise SystemExit(
        f"字段默认值无法写入分析产物（可能为 default_factory 动态默认），"
        f"请扩充 scripts/analyze_vendor.py：{default!r}",
    )


def _vendor_version() -> str:
    pyproject = REPO_ROOT / "vendor" / "_upstream" / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    validate_version(version)
    return version


# 注入防火墙：字段名渲染进生成代码的字符串字面量，版本号写入
# metadata YAML 裸值与注释——两条不可信数据路径都必须是受控形态，越界字符在
# 第一层响亮拒绝，第二层 autoescape 仅作兜底防线
_FIELD_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")
_VERSION_RE = re.compile(r"\d(?:[\w.+-]*\w)?\Z")


def validate_field_name(name: str) -> None:
    """拒绝非小写 Python 标识符形态的字段名（进入模板渲染前的门禁）。"""
    if not _FIELD_NAME_RE.fullmatch(name):
        raise SystemExit(f"上游字段名非合法 Python 标识符，拒绝写入分析产物：{name!r}")


def validate_version(version: str) -> None:
    """拒绝可逃逸 YAML 裸值/注释语境的版本号（换行、引号、空白等）。"""
    if not _VERSION_RE.fullmatch(version):
        raise SystemExit(f"上游版本号格式异常，拒绝写入分析产物：{version!r}")


# 上游 description/license 渲染进 metadata YAML 裸值：拒绝换行、双引号、反斜杠
# 与 HTML/YAML 敏感字符（& < > '）——第二层 Jinja 环境开了
# autoescape=True（代码生成注入防线），它会把这四个字符改写为
# HTML 实体（&→&amp; 等），使「数据层完全上游驱动」的逐字节直通
# 承诺静默偏离；而第二层 --check 比的是「重新生成的结果」
# 而非「上游原文」，无法发现。在第一层
# 前置拒绝；第二层 build_metadata 另有汇点断言（纵深防御）。
_YAML_SAFE_RE = re.compile(r"[^\r\n\"\\&<>']*\Z")


def _vendor_meta() -> dict[str, str]:
    """提取上游项目元数据（pyproject [project] + README）：metadata/README 注入源。"""
    pyproject = REPO_ROOT / "vendor" / "_upstream" / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    description = project.get("description")
    license_value = project.get("license")
    if isinstance(license_value, dict):  # PEP 621 的 {text = "..."} 形态
        license_value = license_value.get("text")
    for label, value in (("description", description), ("license", license_value)):
        if not isinstance(value, str) or not _YAML_SAFE_RE.fullmatch(value):
            raise SystemExit(
                f"上游 {label} 缺失或含 YAML 不安全字符（换行/引号/反斜杠/& < > '），"
                f"拒绝写入分析产物：{value!r}",
            )
    return {
        "description": description,
        "license": license_value,
        "requires_python": _requires_python(project),
        "readme": _vendor_readme(),
    }


def _requires_python(project: dict[str, Any]) -> str:
    """上游 requires-python（README 徽章的 Python 兼容性事实，源=上游 pyproject）。"""
    value = project.get("requires-python")
    # 形态防火墙：非空、有界、无换行/引号/反斜杠（渲染侧经 quote 百分号编码，
    # >/</= 等 PEP 440 比较符属合法取值）
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 64
        or any(ch in value for ch in "\r\n\"'\\")
    ):
        raise SystemExit("上游 requires-python 缺失或形态异常（README 徽章输入），拒绝写入分析产物")
    return value


# 上游 README 直通进生成文档：摄取 main 平面提取件（extract_upstream_readme
# 产物），只做来源与非空校验，尺寸上限防御病态输入——入库快照可能被手改，
# 校验面与提取侧防火墙同规则。
_UPSTREAM_README_PATH = REPO_ROOT / "vendor" / "_upstream" / "upstream_readme.json"
_README_MAX_BYTES = 262_144


def _vendor_readme() -> str:
    """读取上游 main 分支 README 原文（平面快照，与渲染模板等同源同轨）。"""
    if not _UPSTREAM_README_PATH.is_file():
        raise SystemExit(
            f"上游 README 提取产物缺失：{_UPSTREAM_README_PATH.name}，"
            "请运行全量模式或 scripts/extract_upstream_readme.py 现场提取",
        )
    content = json.loads(_UPSTREAM_README_PATH.read_text(encoding="utf-8")).get("readme")
    if not isinstance(content, str) or not content.strip():
        raise SystemExit("上游 README 为空或提取产物形态异常，拒绝写入分析产物")
    if len(content.encode("utf-8")) > _README_MAX_BYTES:
        raise SystemExit(
            f"上游 README 超过 {_README_MAX_BYTES} 字节上限，疑似异常快照，拒绝写入分析产物",
        )
    return content


# 显示文本注入源：桥内用户可见文案由 sync 工作流从上游 main 分支
# 提取（scripts/extract_display_texts.py，产物入库），本层摄取进分析数据，
# 使第二层渲染 texts.py 与其他工件同一来源、离线可再生
_DISPLAY_TEXTS_PATH = REPO_ROOT / "vendor" / "_upstream" / "display_texts.json"
_TEXT_MAX_BYTES = 1024


def _upstream_texts() -> dict[str, dict[str, str]]:
    """摄取显示文本提取产物；键值形态防火墙（入库数据可能被手改）。"""
    if not _DISPLAY_TEXTS_PATH.is_file():
        raise SystemExit(
            f"显示文本提取产物缺失：{_DISPLAY_TEXTS_PATH.name}，"
            "请先在已 fetch 上游 main 的克隆上运行 scripts/extract_display_texts.py",
        )
    data = json.loads(_DISPLAY_TEXTS_PATH.read_text(encoding="utf-8"))
    texts = data.get("texts") if isinstance(data, dict) else None
    if (
        not isinstance(texts, dict)
        or not texts
        or not str(data.get("_provenance", "")).startswith("scripts/extract_display_texts.py")
    ):
        raise SystemExit(
            f"{_DISPLAY_TEXTS_PATH.name} 不是提取脚本产物（缺 _provenance/texts），"
            "请重跑 scripts/extract_display_texts.py",
        )
    validated: dict[str, dict[str, str]] = {}
    for key, entry in texts.items():
        validate_field_name(key)
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("value"), str)
            or not isinstance(entry.get("source"), str)
        ):
            raise SystemExit(
                f"显示文本 {key} 条目形态异常，请重跑 scripts/extract_display_texts.py",
            )
        value = entry["value"]
        if "\r" in value or len(value.encode("utf-8")) > _TEXT_MAX_BYTES:
            raise SystemExit(
                f"显示文本 {key} 含回车符或超过 {_TEXT_MAX_BYTES} 字节上限，"
                f"拒绝写入分析产物：{value!r}",
            )
        validated[key] = {"value": value, "source": entry["source"]}
    return validated


# 渲染参数注入源：桥内渲染/裁剪关键参数（缓存键版本、t2i 视口
# 基准、超大渲染图文件段阈值、二维码点阵参数）由 sync 工作流从上游 main
# 分支提取（scripts/extract_render_params.py，产物入库），本层摄取进分析
# 数据，使第二层渲染 render_params.py 与其他工件同一来源、离线可再生
_RENDER_PARAMS_PATH = REPO_ROOT / "vendor" / "_upstream" / "render_params.json"
_INT_PARAM_MAX = 2**31


def _upstream_render_params() -> dict[str, dict[str, Any]]:
    """摄取渲染参数提取产物；键值形态防火墙（入库数据可能被手改）。"""
    if not _RENDER_PARAMS_PATH.is_file():
        raise SystemExit(
            f"渲染参数提取产物缺失：{_RENDER_PARAMS_PATH.name}，"
            "请先在已 fetch 上游 main 的克隆上运行 scripts/extract_render_params.py",
        )
    data = json.loads(_RENDER_PARAMS_PATH.read_text(encoding="utf-8"))
    params = data.get("params") if isinstance(data, dict) else None
    if (
        not isinstance(params, dict)
        or not params
        or not str(data.get("_provenance", "")).startswith("scripts/extract_render_params.py")
    ):
        raise SystemExit(
            f"{_RENDER_PARAMS_PATH.name} 不是提取脚本产物（缺 _provenance/params），"
            "请重跑 scripts/extract_render_params.py",
        )
    validated: dict[str, dict[str, Any]] = {}
    for key, entry in params.items():
        validate_field_name(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise SystemExit(
                f"渲染参数 {key} 条目形态异常，请重跑 scripts/extract_render_params.py",
            )
        value = entry["value"]
        if isinstance(value, str):
            if "\r" in value or len(value.encode("utf-8")) > _TEXT_MAX_BYTES:
                raise SystemExit(
                    f"渲染参数 {key} 含回车符或超过 {_TEXT_MAX_BYTES} 字节上限，"
                    f"拒绝写入分析产物：{value!r}",
                )
        elif isinstance(value, int) and not isinstance(value, bool):
            if not 0 <= value < _INT_PARAM_MAX:
                raise SystemExit(
                    f"渲染参数 {key} 取值超出合理范围 [0, 2^31)，拒绝写入分析产物：{value!r}",
                )
        else:
            raise SystemExit(
                f"渲染参数 {key} 类型异常（应为 str/int），请重跑 scripts/extract_render_params.py",
            )
        validated[key] = {"value": value, "source": entry["source"]}
    return validated


# 渲染模板注入源（模板数据面）：桥内 templates/ 目录逐字节来自
# 上游 main 分支（scripts/extract_render_templates.py 在 sync 工作流提取并
# 入库），本层摄取进分析数据，使第二层再生模板文件与其他工件同一来源、
# 离线可再生
_RENDER_TEMPLATES_PATH = REPO_ROOT / "vendor" / "_upstream" / "render_templates.json"
_TEMPLATE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(jinja|css|json)\Z")
_TEMPLATE_FILE_MAX_BYTES = 512 * 1024


def _upstream_render_templates() -> dict[str, Any]:
    """摄取渲染模板提取产物；文件名/内容形态防火墙（入库数据可能被手改）。"""
    if not _RENDER_TEMPLATES_PATH.is_file():
        raise SystemExit(
            f"渲染模板提取产物缺失：{_RENDER_TEMPLATES_PATH.name}，"
            "请先在已 fetch 上游 main 的克隆上运行 scripts/extract_render_templates.py",
        )
    data = json.loads(_RENDER_TEMPLATES_PATH.read_text(encoding="utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(files, dict)
        or not files
        or not str(data.get("_provenance", "")).startswith("scripts/extract_render_templates.py")
        or not isinstance(data.get("source_revision"), str)
    ):
        raise SystemExit(
            f"{_RENDER_TEMPLATES_PATH.name} 不是提取脚本产物（缺 _provenance/files），"
            "请重跑 scripts/extract_render_templates.py",
        )
    for name, content in files.items():
        if not isinstance(name, str) or not _TEMPLATE_NAME_RE.fullmatch(name) or "/" in name:
            raise SystemExit(
                f"渲染模板文件名 {name!r} 形态异常，请重跑 scripts/extract_render_templates.py",
            )
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content.encode("utf-8")) > _TEMPLATE_FILE_MAX_BYTES
        ):
            raise SystemExit(
                f"渲染模板 {name} 内容形态异常（空或超上限），"
                "请重跑 scripts/extract_render_templates.py",
            )
    return {"files": files, "source_revision": data["source_revision"]}


def build_analysis() -> str:
    """生成 vendor_analysis.json 内容（纯函数，幂等）。"""
    docs = _field_docstrings()
    fields: dict[str, dict[str, Any]] = {}
    missing_doc: list[str] = []
    for name, field in _config_cls().model_fields.items():
        if not name.startswith("plite_"):
            continue
        validate_field_name(name)
        if name not in docs:
            # 描述是面板唯一解释文本，缺失时宁可响亮失败也不静默回退为字段名
            missing_doc.append(name)
            continue
        shape = _shape(field.annotation)
        entry: dict[str, Any] = {
            "kind": shape["kind"],
            "description": docs[name],
            "default": _json_safe(field.default),
        }
        for key in ("value_kind", "options"):
            if key in shape:
                entry[key] = shape[key]
        fields[name] = entry
    if missing_doc:
        raise SystemExit(
            f"上游字段缺描述（config.py 注解下无字符串说明）：{missing_doc}，请在上游补齐后再分析",
        )
    payload = {
        "_provenance": PROVENANCE,
        "upstream_version": _vendor_version(),
        "upstream_meta": _vendor_meta(),
        "upstream_texts": _upstream_texts(),
        "upstream_render_params": _upstream_render_params(),
        "upstream_render_templates": _upstream_render_templates(),
        "config_fields": fields,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    content = build_analysis()
    ANALYSIS_PATH.write_text(content, encoding="utf-8")
    print(f"已生成 {ANALYSIS_PATH.name}（管线临时产物，不入库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
