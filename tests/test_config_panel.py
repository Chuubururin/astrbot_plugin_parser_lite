"""面板字段必须有人读：WebUI 开关 ↔ vendor/桥读取点 的双向对账。

存在的理由：上游 Config 里有若干字段的实现面在 NoneBot 层（delivery / render /
matchers 的消息组装），standalone 分支发布时已剥离——桥里没人读它们。把它们放进
面板等于给用户一个改了值既无效果、也无报错的开关；反过来，面板省略的字段若日后
在 vendor 里出现读取点，省略台账就腐化成静默吞开关的黑洞。本模块把两个方向都钉住。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from functools import lru_cache
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = REPO_ROOT / "vendor" / "nonebot_plugin_parser_lite" / "config.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_config.py"
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"

# 生成清单与 vendor 类体本身不算读取点：前者只是字段名列表，后者是定义处。
NON_READER_FILE_NAMES = frozenset({"config.py", "gen_config.py"})

_SCAN_ROOTS = (
    REPO_ROOT / "vendor" / "nonebot_plugin_parser_lite",
    REPO_ROOT / "bridge",
    REPO_ROOT / "main.py",
)


@lru_cache(maxsize=1)
def _property_reads() -> dict[str, set[str]]:
    """Config 的 @property → 其函数体里 `self.<名称>` 读到的成员名集合。

    每个面板字段都要走一遍可达性传递，vendor config.py 的 AST 只解析一次。
    """
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Config"
    )
    reads: dict[str, set[str]] = {}
    for stmt in cls.body:
        if not isinstance(stmt, ast.FunctionDef):
            continue
        is_property = any(
            isinstance(dec, ast.Name) and dec.id == "property" for dec in stmt.decorator_list
        )
        if not is_property:
            continue
        touched = {
            node.attr
            for node in ast.walk(stmt)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        if touched:
            reads[stmt.name] = touched
    return reads


def _reachable_names(field: str) -> set[str]:
    """字段本身 + 读它的 @property（含属性套属性），迭代到不动点。

    `plite_need_upload` 只在 config.py 的两个属性里被读，而那两个属性在 sender.py
    有读取点——按「属性被读」传递，否则兼容别名会被误判成死开关。
    """
    reads = _property_reads()
    names = {field}
    while True:
        expanded = {prop for prop, touched in reads.items() if touched & names}
        if expanded <= names:
            return names
        names |= expanded


@lru_cache(maxsize=1)
def _scan_blobs() -> tuple[tuple[str, str], ...]:
    """待扫描文件的（相对路径, 文本）快照。

    每个面板字段都要全树扫一遍读取点：225 个 .py（约 1.8MB）× 字段数是本模块
    最贵的一项（单条用例实测 6.0s，其中读盘占满）。快照进程内只取一次。
    """
    blobs: list[tuple[str, str]] = []
    for root in _SCAN_ROOTS:
        files = [root] if root.is_file() else list(root.rglob("*.py"))
        for path in files:
            if path.name in NON_READER_FILE_NAMES:
                continue
            blobs.append(
                (
                    path.relative_to(REPO_ROOT).as_posix(),
                    path.read_text(encoding="utf-8", errors="replace"),
                )
            )
    return tuple(blobs)


def _reader_files(names: set[str]) -> list[str]:
    patterns = [re.compile(rf"\b{re.escape(name)}\b") for name in sorted(names)]
    return sorted(
        relative
        for relative, blob in _scan_blobs()
        if any(pattern.search(blob) for pattern in patterns)
    )


def _readers(field: str) -> list[str]:
    return _reader_files(_reachable_names(field))


def _upstream_field_names() -> set[str]:
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.config import Config

    return {name for name in Config.model_fields if name.startswith("plite_")}


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_config", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_panel_field_set_is_upstream_minus_ledger(gen: ModuleType) -> None:
    """面板 = 上游全部字段 − 省略台账 ∪ 桥自有字段；透传面仍是全量。"""
    from astrbot_plugin_parser_lite.bridge.gen_config import BRIDGE_FIELDS, VENDOR_FIELDS

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected = (_upstream_field_names() - set(gen.PANEL_OMITTED_FIELDS)) | set(BRIDGE_FIELDS)
    assert set(schema) == expected
    # 透传面保持全量：存量配置键不误报「已不被上游识别」，上游若把实现面搬进
    # standalone，用户已写的值即刻生效。
    assert set(VENDOR_FIELDS) == _upstream_field_names()
    assert set(gen.PANEL_OMITTED_FIELDS) <= _upstream_field_names(), (
        "省略台账指向已不存在的上游字段，请拆净该条目"
    )


def test_omitted_fields_state_a_reason_and_panel_has_no_dead_switch(gen: ModuleType) -> None:
    """省略必须有理由；面板上的每个开关必须有读取点——不分上游还是桥自有。

    读取点判定对桥自有字段同样成立：开关换主人（例如上游删掉 `plite_blacklist_users`
    后桥接手该字段）不改变「面板里不许有假开关」这条判据。
    """
    for field, reason in gen.PANEL_OMITTED_FIELDS.items():
        assert len(reason) >= 20, f"{field} 的省略理由过于简略，无法支撑裁决"

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    dead = sorted(name for name in schema if not _readers(name))
    assert not dead, f"面板上的开关在 vendor/桥里无人读取（假开关）：{dead}"


def test_omitted_fields_truly_have_no_reader(gen: ModuleType) -> None:
    """省略台账反向守护：字段一旦在 vendor/桥里出现读取点，必须回到面板。"""
    revived = sorted(name for name in gen.PANEL_OMITTED_FIELDS if _readers(name))
    assert not revived, f"这些字段已被 vendor/桥读取，须从省略台账拆出：{revived}"


def test_null_default_without_ledger_entry_stays_loud(gen: ModuleType) -> None:
    """台账只解释「为什么不进面板」，不得成为空默认值的放行通道。"""
    with pytest.raises(SystemExit, match="默认值为 null"):
        gen._default_value("plite_x", {"kind": "float", "default": None}, "float")
