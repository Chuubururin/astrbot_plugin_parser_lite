"""静态轨契约①：import 面 AST 白名单（ACL R1/R2，工单 09）。

R2 Fan-in=1：vendor 是最末端叶子，唯一消费面 = 桥接六模块（消费清单即下方
``VENDOR_CONSUMERS`` 常量）；任何其他模块新增 vendor 导入都会使本测试变红。
白名单即《被依赖行为清单》的 import 维度，扩白名单需过契约评审。

检测面（2026-09-17 评审 H3 后加固）：相对导入、绝对路径导入（含包名前缀
``astrbot_plugin_parser_lite.vendor.*``）、以及 ``ast.Import`` 形态三种写法
均被识别。原实现只遍历 ``ast.ImportFrom`` 且仅认 ``level >= 1`` 或
``module.startswith("vendor")``，故绝对路径与 ``import x.y`` 两种写法可
静默绕开本白名单；检测器自身由文件末的 ``test_vendor_import_detector_covers_known_forms``
反向验证（守护的守护）。

残余边界（如实声明）：``importlib.import_module`` / ``__import__`` 等动态
导入无法静态分析，不在本测试覆盖面内。

扫描面（2026-09-18 迁移后）：仓库根的 ``main.py`` + ``bridge/`` 下六个 ACL
模块；清单与实际扫描结果的一致性由 ``test_bridge_source_scan_is_complete``
自身钉扎。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
BRIDGE_DIR = PLUGIN_DIR / "bridge"

# 桥接层扫描面：仓库根的 main.py + bridge/ 下八个模块（六 ACL 模块 + 三生成工件）
BRIDGE_MODULES = frozenset(
    {
        "main",
        "sender",
        "render",
        "ssrf",
        "config_sync",
        "vendor_patches",
        # 生成工件（注入流水线产出，同属桥接层，永不手改）
        "gen_config",
        "texts",
        "render_params",
    },
)

VENDOR_CONSUMERS = frozenset({"main", "sender", "render", "ssrf", "config_sync", "vendor_patches"})

VENDOR_IMPORT_WHITELIST: dict[str, frozenset[str]] = {
    "main": frozenset(
        {
            "nonebot_plugin_parser_lite",
            "nonebot_plugin_parser_lite.config",
            "nonebot_plugin_parser_lite.constants",
            "nonebot_plugin_parser_lite.exception",
        },
    ),
    "sender": frozenset(
        {
            "nonebot_plugin_parser_lite.config",
            "nonebot_plugin_parser_lite.data",
            "nonebot_plugin_parser_lite.exception",
            "nonebot_plugin_parser_lite.helper",
            "nonebot_plugin_parser_lite.utils.log",
        },
    ),
    "render": frozenset(
        {
            "nonebot_plugin_parser_lite.config",
            "nonebot_plugin_parser_lite.data",
            "nonebot_plugin_parser_lite.utils.cache",
            # cache_or_render_image：png 转 jpeg（上游 cache_or_render_image 同款）
            "nonebot_plugin_parser_lite.utils.ffmpeg",
        },
    ),
    "ssrf": frozenset(
        {
            "nonebot_plugin_parser_lite.download",
            # _wrap_parser_clients 包装 BaseParser.__init__（parser API 面钉扎）
            "nonebot_plugin_parser_lite.parsers.base",
            # _wrap_aux_clients：weibo SESSION / bilibili HTTP_CLIENT+GRPC_CLIENT 就地钉扎
            "nonebot_plugin_parser_lite.parsers.weibo.auth",
            "nonebot_plugin_parser_lite.utils.bilibili.client",
        },
    ),
    # 导入期配置快照回写（MAX_RETRIES / 两平台 cookies），只写标量/dict；
    # rearm_runtime 重建 DOWNLOADER.client 时取 DOWNLOAD_TIMEOUT
    "config_sync": frozenset(
        {
            "nonebot_plugin_parser_lite.config",
            "nonebot_plugin_parser_lite.constants",
            "nonebot_plugin_parser_lite.download",
            "nonebot_plugin_parser_lite.parsers.linuxdo",
            "nonebot_plugin_parser_lite.parsers.zhihu",
            "nonebot_plugin_parser_lite.utils.cookie",
        },
    ),
    # vendor 运行态缺陷的桥内注入补丁（vendor 零修改铁律下的唯一修法）；
    # 被复刻函数见 vendor_patches.py，上游修复后应移除对应补丁
    "vendor_patches": frozenset(
        {
            "nonebot_plugin_parser_lite.creator",
            "nonebot_plugin_parser_lite.data",
            "nonebot_plugin_parser_lite.parsers",
            "nonebot_plugin_parser_lite.parsers.base",
            "nonebot_plugin_parser_lite.parsers.buff",
            "nonebot_plugin_parser_lite.parsers.hupu",
            "nonebot_plugin_parser_lite.parsers.kuwo",
            "nonebot_plugin_parser_lite.utils.ffmpeg",
            "nonebot_plugin_parser_lite.utils.format",
        },
    ),
}

# 桥接侧禁止出现的 nonebot 生态顶层包（R1：vendor 类型/生态不越桥）
FORBIDDEN_TOP_LEVEL = frozenset({"nonebot", "nonebot_plugin_alconna", "nonebot_plugin_uninfo"})


def _bridge_source_paths() -> dict[str, Path]:
    """桥接层源文件：仓库根的 ``main.py`` + ``bridge/`` 下的六个 ACL 模块。

    同名模块出现两次即视为冲突——后者会静默遮蔽前者，使白名单守护失效，因此
    显式报错而不是让 dict 覆盖。
    """
    paths: dict[str, Path] = {}
    for path in [*sorted(PLUGIN_DIR.glob("*.py")), *sorted(BRIDGE_DIR.glob("*.py"))]:
        if path.stem in paths:
            raise AssertionError(f"桥接模块名冲突：{path} 与 {paths[path.stem]} 同名")
        paths[path.stem] = path
    return paths


def _bridge_sources() -> dict[str, str]:
    return {name: path.read_text(encoding="utf-8") for name, path in _bridge_source_paths().items()}


# 插件目录自身即一个包；绝对导入可带该前缀
_PLUGIN_PACKAGE = PLUGIN_DIR.name
_VENDOR_INNER = "nonebot_plugin_parser_lite"
_VENDOR_SEG = "vendor"


def _strip_plugin_prefix(name: str, level: int) -> str:
    """绝对导入剥掉插件包前缀 ``astrbot_plugin_parser_lite.``；相对导入原样返回。"""
    if level == 0 and name.startswith(f"{_PLUGIN_PACKAGE}."):
        return name[len(_PLUGIN_PACKAGE) + 1 :]
    return name


def _is_vendor_root(name: str, level: int) -> bool:
    """``from .vendor import X``：包与子模块名分开写，需展开别名才能定位。"""
    return _strip_plugin_prefix(name, level) == _VENDOR_SEG


def _normalize_vendor_module(name: str, level: int) -> str | None:
    """导入目标 → vendor 内模块名；不落在 vendor 面则返回 None。

    ``level >= 1`` 为相对导入（基准即插件包本身），``level == 0`` 为绝对导入
    （接受 ``astrbot_plugin_parser_lite.vendor.`` 与裸 ``vendor.`` 两种前缀，
    以及裸 ``nonebot_plugin_parser_lite.*``）。裸 ``vendor`` 包本身不是
    「vendor 内模块」，返回 None。
    """
    if not name:
        return None
    name = _strip_plugin_prefix(name, level)
    if name.startswith(f"{_VENDOR_SEG}."):
        inner = name[len(_VENDOR_SEG) + 1 :]
    elif name == _VENDOR_INNER or name.startswith(f"{_VENDOR_INNER}."):
        inner = name
    else:
        return None
    return inner or None


def _vendor_imports(tree: ast.AST) -> set[str]:
    """收集模块内（含函数级）对 vendor 子包的导入，规范化为 vendor 内模块名。

    覆盖三种写法（H3 加固）：``from .vendor.X import ...``、
    ``from astrbot_plugin_parser_lite.vendor.X import ...``、``import X.Y``。
    ``from .vendor import X`` 形态单独还原（此时 ``X`` 才是 vendor 子模块）。

    残余边界（如实声明）：``importlib.import_module`` / ``__import__`` 等动态导入
    无法静态分析；``from . import vendor`` 后经属性访问的间接用法亦不可见。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            level = node.level
            if _is_vendor_root(base, level):
                for alias in node.names:
                    normalized = _normalize_vendor_module(f"{_VENDOR_SEG}.{alias.name}", 0)
                    if normalized is not None:
                        found.add(normalized)
                continue
            normalized = _normalize_vendor_module(base, level)
            if normalized is not None:
                found.add(normalized)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                normalized = _normalize_vendor_module(alias.name, 0)
                if normalized is not None:
                    found.add(normalized)
    return found


def test_bridge_source_scan_is_complete():
    """扫描面自身必须完整：漏扫一个桥接模块会让后续各条契约静默失效。"""
    scanned = set(_bridge_source_paths())
    assert scanned == BRIDGE_MODULES, (
        f"桥接模块清单与扫描结果不一致：{sorted(scanned ^ BRIDGE_MODULES)}；"
        "新增/移动模块须同步更新 BRIDGE_MODULES"
    )


def test_vendor_fan_in_is_one():
    consumers = {
        name for name, source in _bridge_sources().items() if _vendor_imports(ast.parse(source))
    }
    assert consumers == set(VENDOR_CONSUMERS), (
        f"vendor 消费面变化：{sorted(consumers)}；扩面需过 ACL 契约评审"
    )


@pytest.mark.parametrize("name", sorted(VENDOR_CONSUMERS))
def test_import_surface_whitelisted(name: str):
    tree = ast.parse(_bridge_sources()[name])
    surface = _vendor_imports(tree)
    assert surface <= VENDOR_IMPORT_WHITELIST[name], (
        f"{name}.py 触达未声明 vendor 面：{sorted(surface - VENDOR_IMPORT_WHITELIST[name])}"
    )


def test_no_nonebot_ecosystem_import_in_bridge():
    for name, source in _bridge_sources().items():
        for node in ast.walk(ast.parse(source)):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for module in modules:
                top = module.split(".")[0]
                assert top not in FORBIDDEN_TOP_LEVEL, f"{name}.py 泄漏 nonebot 生态导入：{module}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # 相对导入（原实现已覆盖）
        (
            "from .vendor.nonebot_plugin_parser_lite import Parser",
            {"nonebot_plugin_parser_lite"},
        ),
        (
            "from .vendor.nonebot_plugin_parser_lite.config import pconfig",
            {"nonebot_plugin_parser_lite.config"},
        ),
        # 包 + 子模块名分开写的相对形态
        ("from .vendor import nonebot_plugin_parser_lite", {"nonebot_plugin_parser_lite"}),
        # 绝对路径导入（原实现漏判）
        (
            "from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Parser",
            {"nonebot_plugin_parser_lite"},
        ),
        # ast.Import 形态（原实现完全忽略）
        ("import nonebot_plugin_parser_lite.creator", {"nonebot_plugin_parser_lite.creator"}),
        (
            "import astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.creator",
            {"nonebot_plugin_parser_lite.creator"},
        ),
        # 函数级导入同样入账（单行 suite，规避多行字符串转义歧义）
        (
            "def f(): import nonebot_plugin_parser_lite.data",
            {"nonebot_plugin_parser_lite.data"},
        ),
        # 非 vendor 面一律不计
        ("import json", set()),
        ("from . import render_params", set()),
        ("from astrbot_plugin_parser_lite import sender", set()),
        ("from .vendorish import x", set()),
    ],
)
def test_vendor_import_detector_covers_known_forms(source: str, expected: set[str]) -> None:
    """守护的守护：检测器必须识别全部已知越桥写法。

    没有这条反向验证时，「白名单机械守护」只是一个声称——2026-09-17 评审
    实测原实现对绝对路径与 ``ast.Import`` 两种写法全绿。
    """
    assert _vendor_imports(ast.parse(source)) == expected


def test_vendor_seam_exists():
    """缝合点契约：桥内触达的 vendor 内部结构必须存在。

    上游改名时此处先红，给出可读信号（而非运行期 AttributeError 或
    getattr 静默退化为 None）。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import config
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
        VideoContent,
    )
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
        DOWNLOADER,
    )

    client = DOWNLOADER.client
    assert hasattr(client, "_httpx"), "DOWNLOADER.client._httpx 缝合点消失"
    assert hasattr(client, "_curl"), "DOWNLOADER.client._curl 缝合点消失"

    # sender.py 视频分流读取 VideoContent._size_bytes（get_display_size 预热）
    assert hasattr(VideoContent, "get_display_size"), "VideoContent.get_display_size 消失"
    assert "_size_bytes" in getattr(VideoContent, "__dataclass_fields__", {}) or hasattr(
        VideoContent,
        "_size_bytes",
    ), "VideoContent._size_bytes 缝合点消失"

    # sender/render 导入 vendor.config 的 _nickname 私有名
    assert hasattr(config, "_nickname"), "config._nickname 缝合点消失"
    assert hasattr(config, "pconfig"), "config.pconfig 缝合点消失"
