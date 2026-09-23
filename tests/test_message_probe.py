"""最终消息结构对比工装契约（scripts/message_structure_probe.py）。

四层守护：合成夹具双 profile 覆盖 send_content 分支（forward=生产默认主
路径含转发嵌套/投票/引用/链接/图片/图集，flat=平铺次路径）；归一化的等价
类行为（Reference≡Nodes、图集 UniMessage≡_AltMedia、相邻文本拼接）；对照
的纯函数行为；镜像自上游的切分算法与转发节点类方法以 AST 结构指纹对照
上游快照 revision 现场版本（docstring 剥离后逐节点等价，上游改写即红；无克隆时
跳过）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from astrbot_plugin_parser_lite.bridge import render_params

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "message_structure_probe.py"
SENDER_PATH = REPO_ROOT / "bridge" / "sender.py"
UPSTREAM_RENDER = "src/nonebot_plugin_parser_lite/render/__init__.py"
_SPLIT_FUNCTIONS = ("_find_text_split_end", "split_text_by_length_with_punct")
# ``_ForwardText.split`` **刻意不对齐上游**：protected 块硬超 max_len 时
# 桥内按 max_len 硬切（保护语义让位于不可发送的硬上限，见 sender.py 与
# tests/test_sender_chain.py::test_sender_forward_text_protected_block_not_split）。
# 故结构指纹只钉 ``text``；上游改写 split 由 test_sender_chain 行为用例覆盖。
_FORWARD_TEXT_METHODS = ("text",)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load("message_structure_probe", PROBE_PATH)


@pytest.fixture(scope="module")
def extractor() -> ModuleType:
    return _load(
        "extract_render_params_probe",
        REPO_ROOT / "scripts" / "extract_render_params.py",
    )


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _function_fingerprints(source: str, names: tuple[str, ...]) -> dict[str, str]:
    """顶层函数的 AST 结构指纹（docstring 剥离、位置信息不含）。

    指纹逐节点覆盖操作符/常量/调用结构——上游改写切分算法的任何实质
    变更都会改变指纹，而注释/文档串变更不触发误报。
    """
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.FunctionDef) and node.name in names):
            continue
        out[node.name] = ast.dump(
            ast.FunctionDef(
                name=node.name,
                args=node.args,
                body=_strip_docstring(node.body),
                decorator_list=node.decorator_list,
                returns=node.returns,
                type_comment=None,
                type_params=[],
            ),
            include_attributes=False,
        )
    missing = set(names) - set(out)
    assert not missing, f"源码中未找到函数：{sorted(missing)}"
    return out


def _method_fingerprints(source: str, cls_name: str, names: tuple[str, ...]) -> dict[str, str]:
    """类内方法的 AST 结构指纹（同 _function_fingerprints 口径）。"""
    out: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.ClassDef) and node.name == cls_name):
            continue
        for sub in node.body:
            if not (isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name in names):
                continue
            out[sub.name] = ast.dump(
                ast.FunctionDef(
                    name=sub.name,
                    args=sub.args,
                    body=_strip_docstring(sub.body),
                    decorator_list=sub.decorator_list,
                    returns=sub.returns,
                    type_comment=None,
                    type_params=[],
                ),
                include_attributes=False,
            )
    missing = set(names) - set(out)
    assert not missing, f"类 {cls_name} 中未找到方法：{sorted(missing)}"
    return out


# ---- 合成夹具：分支覆盖 ----


def test_fixture_profiles_cover_send_content_branches(probe: ModuleType) -> None:
    """forward profile 覆盖主路径全分支（含转发嵌套与图集）；flat 覆盖平铺。"""
    forward = probe._fixture_result("forward")
    kinds = [type(item).__name__ for item in forward.content]
    for expected in (
        "PollContent",
        "QuoteContent",
        "LinkContent",
        "ImageContent",
        "GraphicContent",
    ):
        assert expected in kinds, f"forward profile 缺 {expected} 分支"
    assert forward.repost is not None and forward.repost.content
    assert forward.title  # 转发应含标题（上游 af9e9bf 回归锚点）

    flat = probe._fixture_result("flat")
    assert flat.repost is None and flat.title


# ---- 归一化：等价类行为 ----


def test_norm_message_chain_equivalences(probe: ModuleType) -> None:
    """Reference ≡ Nodes ≡ forward；相邻文本段拼接（上游合并/桥逐段）。"""
    assert probe._norm_message_chain([type("Reference", (), {})()]) == [{"kind": "forward"}]
    nodes = type("Nodes", (), {})()
    assert probe._norm_message_chain([nodes]) == [{"kind": "forward"}]

    plain = type("Plain", (), {"text": "甲"})()
    text = type("Text", (), {"text": "乙"})()
    merged = probe._norm_message_chain([plain, text])
    assert merged == [{"kind": "text", "text": "甲乙"}]


def test_normalize_forward_segs_kinds(probe: ModuleType) -> None:
    """str/媒体对象/_ForwardText/_AltMedia/图集复合段归一。"""
    media = type("MediaFile", (), {"kind": "image"})()
    ftext = type("_ForwardText", (), {"text": "正文", "include_author": True})()
    segs = probe.normalize_forward_segs(["纯文本", media, ftext])
    assert segs == [
        {"kind": "text", "text": "纯文本"},
        {"kind": "image"},
        {"kind": "ftext", "text": "正文", "include_author": True},
    ]

    alt = type("_AltMedia", (), {"media": media, "alt": "说明"})()
    inner_image = type("Image", (), {})()
    inner_text = type("Text", (), {"text": "说明"})()
    composite = type(
        "UniMessage",
        (),
        {"__iter__": lambda self: iter([inner_image, inner_text])},
    )()
    segs = probe.normalize_forward_segs([alt, composite])
    assert segs == [
        {"kind": "alt", "media_kind": "image", "alt": "说明"},
        {"kind": "alt", "media_kind": "image", "alt": "说明"},
    ], "桥 _AltMedia 与上游「img+alt」复合段必须归一为同一形态"


# ---- 对照：纯函数行为 ----


def _capture(
    profile: str,
    segs: list[dict[str, object]],
    media: list[list[dict[str, object]]],
) -> dict[str, object]:
    return {"profile": profile, "forward_segs": segs, "media": media}


def test_compare_passes_on_identical_captures(probe: ModuleType) -> None:
    up = {
        "captures": [
            _capture(
                "forward", [{"kind": "text", "text": ">>>>>原帖<<<<<"}], [[{"kind": "video"}]]
            ),
        ],
    }
    assert probe.compare(up, up) == []


def test_compare_reports_profile_and_media_diffs(probe: ModuleType) -> None:
    up = {"captures": [_capture("live", [{"kind": "text", "text": "a"}], [[{"kind": "video"}]])]}
    br = {"captures": [_capture("live", [{"kind": "text", "text": "a"}], [[{"kind": "file"}]])]}
    diffs = probe.compare(up, br)
    assert len(diffs) == 1 and "[live] media[0]" in diffs[0]

    diffs = probe.compare(up, {"captures": []})
    assert any("只在单侧捕获" in d for d in diffs)

    br_segs = {
        "captures": [_capture("live", [{"kind": "text", "text": "改"}], [[{"kind": "video"}]])],
    }
    diffs = probe.compare(up, br_segs)
    assert any("forward_segs 不等" in d for d in diffs)


# ---- 结构指纹钉扎：桥镜像面 == 上游快照 revision 现场 AST ----


def test_bridge_split_surface_matches_upstream_ast(extractor: ModuleType) -> None:
    """镜像自上游的切分算法与转发节点类方法与入库快照 revision 逐节点等价。

    上游改写切分逻辑或 _ForwardText.split/text（如 af9e9bf「转发文本未包含
    标题」一类回归）而桥未跟时，sync 测试在此变红；注释/docstring 变更不
    触发误报。标点切分集本体由渲染参数注入层逐字承接（值级对照）。

    提取源锚定入库快照的 source_revision 而非本地 origin/main ref——后者可能
    因镜像缓存竞态滞后于真实上游（2026-09-14 实证），上游侧的真相以快照记录
    的 revision 为准。
    """
    clone = REPO_ROOT / ".sync-work" / "upstream"
    probe_run = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe_run.returncode != 0:
        pytest.skip("无上游克隆（CI 无克隆），跳过结构指纹钉扎")

    revision = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "render_params.json").read_text(encoding="utf-8"),
    )["source_revision"]
    upstream = subprocess.run(
        ["git", "-C", str(clone), "show", f"{revision}:{UPSTREAM_RENDER}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if upstream.returncode != 0:
        pytest.skip("快照 source_revision 对象不在本地克隆，跳过结构指纹钉扎")
    upstream_src = upstream.stdout
    upstream_punct = extractor.extract(upstream_src)["params"]["text_split_punctuation"]["value"]
    assert upstream_punct == render_params.TEXT_SPLIT_PUNCTUATION, (
        "标点切分集漂移：渲染参数注入层与上游不一致"
    )

    bridge_src = SENDER_PATH.read_text(encoding="utf-8")
    upstream_fns = _function_fingerprints(upstream_src, _SPLIT_FUNCTIONS)
    bridge_fns = _function_fingerprints(bridge_src, _SPLIT_FUNCTIONS)
    upstream_methods = _method_fingerprints(upstream_src, "_ForwardText", _FORWARD_TEXT_METHODS)
    bridge_methods = _method_fingerprints(bridge_src, "_ForwardText", _FORWARD_TEXT_METHODS)
    for name in (*_SPLIT_FUNCTIONS, *_FORWARD_TEXT_METHODS):
        up_fp = upstream_fns.get(name) or upstream_methods[name]
        br_fp = bridge_fns.get(name) or bridge_methods[name]
        assert up_fp == br_fp, f"{name} 与上游结构漂移（照搬面需跟随上游更新）"
