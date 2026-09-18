"""上游 main 分支渲染参数提取 → vendor/_upstream/render_params.json。

渲染/裁剪关键参数注入的最前端：桥内渲染缓存键版本、远程 t2i 视口适配基准、
超大渲染图文件段阈值、二维码点阵参数、发送编排参数（合并转发文本/节点
上限、长文本标点切分集）等数值逐字来自上游 main 分支 render
模块；standalone 快照剥离了渲染层，因此提取源是 main 分支本体（vendor 单一
权威上游的另一条数据面）。本脚本只在 sync-upstream 工作流中运行（那里已
fetch 上游 main）；产物 JSON **入库**（与 vendor/_upstream/display_texts.json
同类的上游元数据快照），使 analyze_vendor → generate_config 两层注入在本地
与 CI 均可离线再生。

提取规则（唯一的锚点规则表，属桥规则层——值是上游数据，锚点指向哪个值是
桥的语义知识）：

- AST 遍历上游 render/__init__.py，按锚点定位赋值/调用实参并求值字面量
  （``5 * 1024 * 1024`` 之类纯整数乘法表达式归一为整数字节值）；
- 每个锚点必须恰好命中一个候选：零命中说明上游改写了该参数位置，多命中
  说明锚点失去区分度——都响亮失败，让 sync PR 变红交由人工复核，绝不
  静默携带过期参数。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_ROOT / "vendor" / "_upstream" / "render_params.json"
PROVENANCE = "scripts/extract_render_params.py 生成（上游 main 渲染参数提取，勿手改）"

# 平级共享底座（git 只读管道 + 产物字节公式）：scripts/ 非包，直接运行与
# importlib 按路径加载两种形态都靠 sys.path 垫片导入
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pipeline_common import dump_json, git_show, rev_parse  # noqa: E402

_UPSTREAM_RENDER = "src/nonebot_plugin_parser_lite/render/__init__.py"

# 注入防火墙：str 值经分析数据进入生成代码字符串字面量（回车符破坏行结构、
# 超长值疑似异常快照）；int 值渲染进生成代码整数字面量（负数/超 2^31 不是
# 上游渲染参数的合理形态）——都在最前端响亮拒绝
_TEXT_MAX_BYTES = 1024
_INT_MAX = 2**31


def _literal_int(node: ast.expr) -> int | None:
    """整数字面量或纯整数乘法表达式（如 5 * 1024 * 1024）→ 字节值。"""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _literal_int(node.left)
        right = _literal_int(node.right)
        if left is not None and right is not None:
            return left * right
    return None


def _callee_name(func: ast.expr) -> str | None:
    """调用目标名：Name 或 Attribute 尾名（get_new_page / qrcode.QRCode）。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _version_candidates(tree: ast.Module) -> list[tuple[str, str]]:
    """模块级 ``RENDER_TEMPLATE_VERSION`` 字符串常量（渲染缓存键版本）。"""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "RENDER_TEMPLATE_VERSION" for t in targets):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.append((value.value, "render/__init__.py:RENDER_TEMPLATE_VERSION"))
    return found


def _named_int_candidates(tree: ast.Module, name: str) -> list[tuple[int, str]]:
    """模块级指定名整型常量（发送编排参数：转发文本/节点上限等）。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        literal = _literal_int(value)
        if literal is not None:
            found.append((literal, f"render/__init__.py:{name}"))
    return found


def _named_frozenset_str_candidates(tree: ast.Module, name: str) -> list[tuple[str, str]]:
    """模块级 ``<NAME> = frozenset("…")`` 字符串实参（标点切分集等）。"""
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            found.append((value.args[0].value, f"render/__init__.py:{name}"))
    return found


def _viewport_candidates(tree: ast.Module) -> list[tuple[dict[str, int], str]]:
    """``get_new_page(2, **{"viewport": {...}})`` 实参（t2i 视口基准）。

    上游 2026-09-17 起把整页 ``template_to_pic`` 换成 ``get_new_page`` +
    分段滚动截图拼接（绕开 full_page 大位图限制），视口改经 ``**{...}``
    解包传入——AST 上是 ``keyword.arg is None`` 的字面量字典。锚点只认
    当前形状：上游再次改写时零命中响亮失败交人工复核，不做新旧兼容兜底。
    """
    found: list[tuple[dict[str, int], str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) != "get_new_page":
            continue
        for kw in node.keywords:
            # **{...} 解包：keyword.arg 为 None，value 是字面量字典
            if kw.arg is not None or not isinstance(kw.value, ast.Dict):
                continue
            for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "viewport"):
                    continue
                if not isinstance(value, ast.Dict):
                    continue
                viewport: dict[str, int] = {}
                for k2, v2 in zip(value.keys, value.values, strict=True):
                    if isinstance(k2, ast.Constant) and k2.value in ("width", "height"):
                        literal = _literal_int(v2)
                        if literal is not None:
                            viewport[str(k2.value)] = literal
                if {"width", "height"} <= viewport.keys():
                    found.append((viewport, "render/__init__.py:get_new_page viewport"))
    return found


def _threshold_candidates(tree: ast.Module) -> list[tuple[int, str]]:
    """``st_size >= <字面量>`` 比较（超大渲染图 img_seg/file_seg 分流阈值）。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.GtE)
        ):
            continue
        if not (isinstance(node.left, ast.Attribute) and node.left.attr == "st_size"):
            continue
        literal = _literal_int(node.comparators[0])
        if literal is not None:
            found.append((literal, "render/__init__.py:cache_or_render_image st_size 阈值"))
    return found


def _qrcode_candidates(tree: ast.Module) -> list[tuple[dict[str, int], str]]:
    """``qrcode.QRCode(version=..., error_correction=..., box_size=..., border=...)``
    实参（二维码点阵参数，四项齐备才视为命中）。"""
    wanted = ("version", "error_correction", "box_size", "border")
    found: list[tuple[dict[str, int], str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node.func) != "QRCode":
            continue
        entry: dict[str, int] = {}
        for kw in node.keywords:
            if kw.arg in wanted:
                literal = _literal_int(kw.value)
                if literal is not None:
                    entry[kw.arg] = literal
        if all(name in entry for name in wanted):
            found.append((entry, "render/__init__.py:qrcode.QRCode"))
    return found


def _validate_value(key: str, value: Any) -> None:
    """提取值形态防火墙：str 非空、无回车且限长；int 非负且 < 2^31。"""
    if isinstance(value, str):
        if "\r" in value:
            raise SystemExit(f"渲染参数 {key} 含回车符，疑似异常快照，拒绝写入：{value!r}")
        if not value:
            raise SystemExit(f"渲染参数 {key} 为空串，疑似异常快照，拒绝写入")
        if len(value.encode("utf-8")) > _TEXT_MAX_BYTES:
            raise SystemExit(f"渲染参数 {key} 超过 {_TEXT_MAX_BYTES} 字节上限，疑似异常快照")
    elif isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value < _INT_MAX:
            raise SystemExit(f"渲染参数 {key} 取值超出合理范围 [0, 2^31)：{value!r}")
    else:
        raise SystemExit(f"渲染参数 {key} 类型异常（应为 str/int）：{type(value).__name__}")


def extract(source: str) -> dict[str, Any]:
    """从上游 render/__init__.py 源码提取渲染参数（纯函数，锚点响亮失败）。"""
    tree = ast.parse(source)

    def single(
        finder: Any,
        label: str,
    ) -> tuple[Any, str]:
        found = finder(tree)
        if not found:
            raise SystemExit(f"渲染参数锚点零命中（上游可能已改写 {label} 位置），请人工复核锚点表")
        if len(found) > 1:
            raise SystemExit(
                f"渲染参数锚点命中 {len(found)} 处（失去区分度）：{label}，请人工复核锚点表"
            )
        return found[0]

    version, _ = single(_version_candidates, "RENDER_TEMPLATE_VERSION")
    viewport, viewport_src = single(_viewport_candidates, "get_new_page viewport")
    threshold, _ = single(_threshold_candidates, "st_size 分流阈值")
    qrcode, qrcode_src = single(_qrcode_candidates, "qrcode.QRCode")
    fwd_len, _ = single(
        lambda t: _named_int_candidates(t, "MAX_FORWARD_TEXT_LEN"), "MAX_FORWARD_TEXT_LEN"
    )
    fwd_nodes, _ = single(
        lambda t: _named_int_candidates(t, "MAX_FORWARD_NODES"), "MAX_FORWARD_NODES"
    )
    split_punct, _ = single(
        lambda t: _named_frozenset_str_candidates(t, "TEXT_SPLIT_PUNCTUATION"),
        "TEXT_SPLIT_PUNCTUATION",
    )

    params: dict[str, dict[str, Any]] = {
        "render_template_version": {
            "value": version,
            "source": "render/__init__.py:RENDER_TEMPLATE_VERSION",
        },
        "viewport_width": {"value": viewport["width"], "source": viewport_src + ".width"},
        "viewport_height": {"value": viewport["height"], "source": viewport_src + ".height"},
        "oversized_image_bytes": {
            "value": threshold,
            "source": "render/__init__.py:cache_or_render_image st_size 阈值",
        },
        "qrcode_version": {"value": qrcode["version"], "source": qrcode_src + ".version"},
        "qrcode_error_correction": {
            "value": qrcode["error_correction"],
            "source": qrcode_src + ".error_correction",
        },
        "qrcode_box_size": {"value": qrcode["box_size"], "source": qrcode_src + ".box_size"},
        "qrcode_border": {"value": qrcode["border"], "source": qrcode_src + ".border"},
        "max_forward_text_len": {
            "value": fwd_len,
            "source": "render/__init__.py:MAX_FORWARD_TEXT_LEN",
        },
        "max_forward_nodes": {
            "value": fwd_nodes,
            "source": "render/__init__.py:MAX_FORWARD_NODES",
        },
        "text_split_punctuation": {
            "value": split_punct,
            "source": "render/__init__.py:TEXT_SPLIT_PUNCTUATION",
        },
    }
    for key, entry in params.items():
        _validate_value(key, entry["value"])
    return {"_provenance": PROVENANCE, "params": params}


def build_payload(source: str, revision: str) -> dict[str, Any]:
    """提取产物 + 快照注记（入库 JSON 完整形态；CLI 与编排层共用同一公式）。"""
    payload = extract(source)
    payload["source_revision"] = revision
    payload["source_digest"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return payload


def read_source(repo: Path, ref: str) -> str:
    """读上游 render 模块源码（本数据面的唯一提取源，公开给编排层）。"""
    return git_show(repo, ref, _UPSTREAM_RENDER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取上游 main 分支渲染参数（渲染参数注入最前端）")
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT / ".sync-work" / "upstream"),
        help="上游克隆目录（需已 fetch 目标 ref）",
    )
    parser.add_argument("--ref", default="origin/main", help="渲染参数提取源分支引用")
    args = parser.parse_args(argv)

    source = read_source(Path(args.repo), args.ref)
    revision = rev_parse(Path(args.repo), args.ref)
    payload = build_payload(source, revision)
    dump_json(OUTPUT_PATH, payload)
    print(f"已提取 {len(payload['params'])} 项渲染参数 → {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
