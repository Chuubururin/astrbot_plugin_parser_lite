"""长任务工作链集成测试（连续操作 / 失败路径 / 降级链 / 缓存命中）。

覆盖链路：
- 配置链：configure → sync_import_time_config → vendor 导入期快照点生效
  （MAX_RETRIES / linuxdo_ck / zhihu_ck，含防上游同步新增快照点的 AST 扫描）
- 解析链：连续调用缓存命中、不可解析文本失败路径
- 下载链：DownloadTaskWrapper 多消费者单次执行（sender 双 await 语义）
- SSRF 链：BaseParser.httpx 纳入钉扎（parser API 面，含 kuaishou 重定向直连）
发送链（文本拆分 / 受保护块 / MediaFile 组件翻译）在 tests/test_sender_chain.py，
其依赖 astrbot 运行时，宿主跳过、容器实跑。
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_parser_lite.bridge import ssrf
from astrbot_plugin_parser_lite.bridge.config_sync import sync_import_time_config
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import (
    configure,
    pipeline,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import pconfig
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
    COMMON_TIMEOUT,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
    StreamDownloader,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download.task import (
    DownloadTaskWrapper,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.exception import (
    ParseException,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.bilibili import (
    BilibiliParser,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.linuxdo import (
    LinuxDoParser,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.zhihu import (
    ZhiHuParser,
)

VENDOR_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "nonebot_plugin_parser_lite"

# vendor 全树允许存在的导入期 pconfig 快照点（类体/模块级直接赋值）；
# sync_import_time_config 必须逐一回写
ALLOWED_SNAPSHOT_ATTRS = frozenset({"max_retries", "linuxdo_ck", "zhihu_ck"})


@pytest.fixture(autouse=True)
def _restore_pconfig() -> Iterator[None]:
    saved = pconfig.model_dump()
    yield
    configure(**saved)
    sync_import_time_config()
    pipeline.clear_result_cache()


# ---------------------------------------------------------------- 配置链


def test_sync_writes_max_retries_into_downloader() -> None:
    configure(plite_max_retries=7)
    sync_import_time_config()
    assert StreamDownloader.MAX_RETRIES == 7


def test_sync_writes_platform_cookies_into_parsers() -> None:
    configure(plite_linuxdo_ck="a=1; b=2", plite_zhihu_ck="d_c0=xyz")
    sync_import_time_config()
    assert LinuxDoParser.linuxdo_ck == {"a": "1", "b": "2"}
    assert ZhiHuParser.zhihu_ck == {"d_c0": "xyz"}

    # 未配置时回写为空 dict（与 vendor 类体默认一致）
    configure(plite_linuxdo_ck=None, plite_zhihu_ck=None)
    sync_import_time_config()
    assert LinuxDoParser.linuxdo_ck == {}
    assert ZhiHuParser.zhihu_ck == {}


def _pconfig_bindings(tree: ast.Module) -> set[str]:
    """模块内绑定到 pconfig 对象的全部名字（含 ``as`` 别名与 base 再导出）。"""
    names = {"pconfig"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "pconfig":
                    names.add(alias.asname or alias.name)
    return names


def _config_module_bindings(tree: ast.Module) -> set[str]:
    """模块内绑定到 config **模块** 的名字（``from .. import config`` 形态）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "config":
                    names.add(alias.asname or alias.name)
    return names


def _pconfig_attrs_in(
    expr: ast.expr | None, pconfig_names: set[str], config_mods: set[str]
) -> set[str]:
    """表达式中对 pconfig 的属性访问名集合。

    三种形态（2026-09-17 评审 M13 后加固）：``pconfig.attr``（含 ``as`` 别名）、
    ``config.pconfig.attr``（先绑模块再取属性）、``getattr(pconfig, "attr")``。
    """
    if expr is None:
        return set()
    found: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute):
            value = node.value
            direct = isinstance(value, ast.Name) and value.id in pconfig_names
            via_module = (
                isinstance(value, ast.Attribute)
                and value.attr == "pconfig"
                and isinstance(value.value, ast.Name)
                and value.value.id in config_mods
            )
            if direct or via_module:
                found.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in pconfig_names
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
    return found


def _import_time_statements(tree: ast.Module) -> Iterator[ast.Assign | ast.AnnAssign]:
    """导入期会执行的赋值语句（模块级 / 类体，含控制流包裹）。

    递归进入 ``if`` / ``with`` / ``try`` / ``for`` / ``while`` 的 body，但
    **不进入函数体**——实例 ``__init__`` 与方法内的求值晚于 ``configure()``，
    不属于导入期快照。
    """

    def walk_body(body: Sequence[ast.stmt]) -> Iterator[ast.Assign | ast.AnnAssign]:
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(stmt, ast.Assign | ast.AnnAssign):
                yield stmt
                continue
            if isinstance(stmt, ast.ClassDef):
                yield from walk_body(stmt.body)
                continue
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, field, None)
                if nested:
                    yield from walk_body(nested)
            for handler in getattr(stmt, "handlers", None) or ():
                yield from walk_body(handler.body)

    yield from walk_body(tree.body)


def _snapshot_offenders(tree: ast.Module) -> set[str]:
    """该模块中的导入期 pconfig 快照点属性名集合。"""
    pconfig_names = _pconfig_bindings(tree)
    config_mods = _config_module_bindings(tree)
    offenders: set[str] = set()
    for stmt in _import_time_statements(tree):
        offenders |= _pconfig_attrs_in(stmt.value, pconfig_names, config_mods)
    return offenders


def test_no_new_import_time_pconfig_snapshots() -> None:
    """上游同步若新增类体/模块级 pconfig 快照点，本测试即红：
    提醒把新属性加进 config_sync.sync_import_time_config。"""
    offenders: list[str] = []
    for py in sorted(VENDOR_ROOT.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        if unknown := _snapshot_offenders(tree) - ALLOWED_SNAPSHOT_ATTRS:
            offenders.append(f"{py.relative_to(VENDOR_ROOT)}: {sorted(unknown)}")
    assert offenders == []


def _src(*lines: str) -> str:
    """把多行源码片段拼成字符串（避免在测试源码里写转义换行）。"""
    return chr(10).join(lines) + chr(10)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # 类体直写（原实现已覆盖）
        (_src("class A:", "    X = pconfig.max_retries"), {"max_retries"}),
        # 盲区①：控制流包裹
        (_src("class A:", "    if True:", "        X = pconfig.brand_new"), {"brand_new"}),
        (
            _src(
                "class A:",
                "    try:",
                "        X = pconfig.brand_new",
                "    except Exception:",
                "        X = None",
            ),
            {"brand_new"},
        ),
        # 盲区②：别名导入
        (
            _src("from .config import pconfig as pc", "", "class A:", "    X = pc.brand_new"),
            {"brand_new"},
        ),
        # 盲区③：先绑模块再取属性
        (_src("from . import config", "", "X = config.pconfig.brand_new"), {"brand_new"}),
        # 盲区④：getattr
        (_src('X = getattr(pconfig, "brand_new")'), {"brand_new"}),
        # 嵌套类
        (_src("class A:", "    class B:", "        X = pconfig.brand_new"), {"brand_new"}),
        # 函数体内求值晚于 configure，不算导入期快照
        (_src("class A:", "    def __init__(self):", "        self.x = pconfig.brand_new"), set()),
        # 无关属性访问不计
        (_src("X = other.thing"), set()),
    ],
)
def test_snapshot_scanner_covers_nested_and_alias_forms(source: str, expected: set[str]) -> None:
    """守护的守护（M13）：扫描器必须识别嵌套/别名等已知形态。

    没有这条反向验证时，「上游新增导入期快照即红」只是一个声称——
    2026-09-17 评审实测原实现对 `if` 包裹与 `as` 别名两种写法全绿。
    """
    assert _snapshot_offenders(ast.parse(source)) == expected


def test_allowed_snapshot_attrs_are_actually_synced() -> None:
    """白名单里的属性必须真实存在且被 sync 回写（防 vendor 改名后白名单空转）。"""
    assert hasattr(StreamDownloader, "MAX_RETRIES")
    assert hasattr(LinuxDoParser, "linuxdo_ck")
    assert hasattr(ZhiHuParser, "zhihu_ck")
    configure(
        plite_max_retries=5,
        plite_linuxdo_ck="k=v",
        plite_zhihu_ck="d_c0=abc",
    )
    sync_import_time_config()
    assert StreamDownloader.MAX_RETRIES == 5
    assert LinuxDoParser.linuxdo_ck == {"k": "v"}
    assert ZhiHuParser.zhihu_ck == {"d_c0": "abc"}


# ---------------------------------------------------------------- 解析链


async def test_repeated_parse_hits_result_cache() -> None:
    parser = pipeline.Parser()
    text = "https://www.bilibili.com/video/BV1x3411c7eK"
    matched = parser.match(text)
    sentinel: Any = object()
    pipeline._RESULT_CACHE[matched.cache_key] = sentinel
    assert await parser.parse(text) is sentinel


async def test_unparseable_text_raises_parse_exception() -> None:
    parser = pipeline.Parser()
    with pytest.raises(ParseException):
        await parser.parse("看看这个 https://example.com/not-a-supported-site")


# ---------------------------------------------------------------- 下载链


async def test_download_task_wrapper_single_flight() -> None:
    calls = 0

    async def download() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "ok"

    wrapper: DownloadTaskWrapper[str] = DownloadTaskWrapper(
        func=download, args=(), kwargs={}, url="https://example.com/file"
    )
    # sender 链里 get_display_size 与 get_path/转发节点会对同一任务多次 await
    results = await asyncio.gather(wrapper, wrapper, wrapper)
    assert calls == 1
    assert results == ["ok", "ok", "ok"]
    assert await wrapper == "ok"  # 结果缓存：后续 await 不再执行


# ---------------------------------------------------------------- SSRF 链


async def test_parser_httpx_is_pinned_after_guard() -> None:
    ssrf.install_ssrf_guard()  # 幂等；可能已被其它测试装过
    parser = BilibiliParser()
    try:
        assert isinstance(parser.httpx._transport, ssrf._PinnedTransport)
        # 上游姿态保持：headers/timeout 透传；TLS 校验由 _PinnedTransport
        # 内部连接池按 httpx 默认开启
        assert "User-Agent" in parser.httpx.headers
        assert parser.httpx.timeout == COMMON_TIMEOUT
    finally:
        await parser.aclose()
