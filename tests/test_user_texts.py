"""用户可见文案注入契约（文本注入层）。

桥内面向用户的运行时文案必须来自 texts.py（上游 main 分支提取）；
卡面模板的中文文案必须是上游模板提取的子集。两处契约各配一张极小
白名单——白名单即规则层全量清单，新增文案必须走注入层而非白名单。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXTS_JSON_PATH = REPO_ROOT / "vendor" / "_upstream" / "display_texts.json"


def _load_script(name: str) -> ModuleType:
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_extractor() -> ModuleType:
    return _load_script("extract_display_texts")


def _han(value: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in value)


_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


def _han_strings(extractor: ModuleType, source: Path) -> list[str]:
    """桥运行时模块里的 CJK 文案：字符串常量 + f-string 常量部件（剪注记语句）。

    f-string 模板剔除 {n} 占位后逐部件检查——模板同样是用户文案载体，
    只看 kind=="const" 会漏掉整族 f-string 文案（扫描盲区）。
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    # 赋值后的孤立字符串注记（如 MAX_FORWARD_TEXT_LEN 下方的说明）不是文案
    bare_lines = {
        node.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    values: list[str] = []
    for kind, value, lineno in extractor._candidates(tree):
        if lineno in bare_lines:
            continue
        if kind == "const":
            if _han(value):
                values.append(value)
        else:
            values.extend(
                fragment.strip()
                for fragment in _PLACEHOLDER_RE.split(value)
                if _han(fragment.strip())
            )
    return values


def test_user_facing_strings_flow_through_texts() -> None:
    """桥运行时模块不得新写面向用户的 CJK 文案（白名单 = 规则层清单）。"""
    extractor = _load_extractor()
    # 上游无对应文案的桥 glue（各项均为规则层显式声明）：
    # 「解析失败：」前缀——ParseException 文案本身上游驱动；
    # 『』——命令包裹符（占位数据的桥侧呈现格式）。
    # 已归源出白名单的条目：
    # 「项」——候选扫描发现上游 render send_content 尾段整句
    #   「{0} 项媒体下载失败」，经 download_failed_count 锚点提取、sender 消费；
    # 标点切分集——渲染参数注入层 text_split_punctuation，sender 消费；
    # 「弹幕」「硬币」——显示文本注入层（parsers/bilibili，equals_repeated
    #   锚点），render.py 从 texts.py 消费
    allowlist = {"解析失败：", "『", "』"}
    offenders: dict[str, list[str]] = {}
    for name in ("main.py", "bridge/sender.py", "bridge/render.py"):
        hits = [v for v in _han_strings(extractor, REPO_ROOT / name) if v not in allowlist]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        f"出现未经文本注入层的用户文案（请改用 texts.py 或扩充上游锚点）：{offenders}"
    )


def test_text_firewall_limits_agree() -> None:
    """显示文本防火墙字节上限三层同值（extract/analyze/generate），漂移即红。"""
    limits = {
        name: _load_script(name)._TEXT_MAX_BYTES
        for name in ("extract_display_texts", "analyze_vendor", "generate_config")
    }
    assert set(limits.values()) == {1024}, f"三层防火墙常量漂移：{limits}"


def test_template_texts_follow_upstream() -> None:
    """桥内卡面模板的 CJK 文案必须是上游模板提取的子集（上游漂移 → 红）。"""
    committed = json.loads(TEXTS_JSON_PATH.read_text(encoding="utf-8"))
    upstream_texts = set(committed["template_texts"])
    bridge = (REPO_ROOT / "templates" / "macros.jinja").read_text(encoding="utf-8")
    bridge_texts = {
        fragment.strip()
        for fragment in re.findall(r">([^<>{}\n]*[\u4e00-\u9fff][^<>{}\n]*)<", bridge)
        if fragment.strip()
    }
    assert bridge_texts <= upstream_texts, (
        f"模板文案脱离上游提取（上游可能已改名，请复核模板适配）："
        f"{sorted(bridge_texts - upstream_texts)}"
    )


def test_texts_json_is_committed_snapshot() -> None:
    """显示文本提取产物入库（两层离线再生的数据源），来源契约在键里。"""
    data = json.loads(TEXTS_JSON_PATH.read_text(encoding="utf-8"))
    assert data["_provenance"].startswith("scripts/extract_display_texts.py")
    assert data["source_revision"], "必须记录上游 main 提取 revision"
    assert len(data["texts"]) >= 19, "锚点表意外缩水，请复核提取脚本"
