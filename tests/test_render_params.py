"""渲染参数注入契约：上游 main 渲染参数 → render_params.py → 桥消费
（渲染参数注入层，与文本注入层同构）。

第一层 scripts/extract_render_params.py 从上游 main 分支 render 模块提取
渲染/裁剪关键参数（渲染缓存键版本、t2i 视口基准、超大渲染图文件段阈值、
二维码点阵参数），产物 JSON 入库（vendor/_upstream/render_params.json）；
scripts/analyze_vendor.py 摄取进分析数据，第二层 scripts/generate_config.py
生成 render_params.py；桥 render.py/main.py 消费生成物、不再硬编码。任一
环节与管线再生结果不一致（上游快照滚动后未重跑流水线）时此处变红。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR_PATH = REPO_ROOT / "scripts" / "extract_render_params.py"
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_vendor.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_config.py"
PARAMS_JSON_PATH = REPO_ROOT / "vendor" / "_upstream" / "render_params.json"

_PARAM_NAMES = {
    "render_template_version",
    "viewport_width",
    "viewport_height",
    "oversized_image_bytes",
    "qrcode_version",
    "qrcode_error_correction",
    "qrcode_box_size",
    "qrcode_border",
    "max_forward_text_len",
    "max_forward_nodes",
    "text_split_punctuation",
}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def extractor() -> ModuleType:
    return _load_module("extract_render_params", EXTRACTOR_PATH)


@pytest.fixture(scope="module")
def analyzer() -> ModuleType:
    return _load_module("analyze_vendor_render_params", ANALYZER_PATH)


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load_module("generate_config_render_params", GENERATOR_PATH)


# 代表性上游 main render/__init__.py 结构（锚点测试夹具，与真实模块同形）
UPSTREAM_RENDER_SRC = textwrap.dedent(
    """\
    RENDER_TEMPLATE_VERSION = "20260913"

    class Renderer:
        async def render_image(self, result, theme):
            async with get_new_page(
                2,
                **{
                    "viewport": {"width": 620, "height": 1000},
                    "base_url": self.templates_dir.as_uri(),
                },
            ) as page:
                await page.set_content(html, wait_until="networkidle")

        async def cache_or_render_image(self, result):
            if (await image_path.stat()).st_size >= 5 * 1024 * 1024:
                return await UniHelper.file_seg(image_path)
            return await UniHelper.img_seg(image_path)

        async def resolve_parse_result(self, result):
            if pconfig.append_qrcode:
                qr = qrcode.QRCode(version=1, error_correction=1, box_size=10, border=1)

    MAX_FORWARD_TEXT_LEN = 30000
    MAX_FORWARD_NODES = 90

    TEXT_SPLIT_PUNCTUATION = frozenset("。！？!?；;，,、…")
    """
)


# ---- 第一层前端：上游 main 渲染参数提取 ----


def test_extractor_follows_anchors(extractor: ModuleType) -> None:
    """锚点规则在代表性上游结构上提取全部渲染参数（乘法阈值归一为字节值）。"""
    payload = extractor.extract(UPSTREAM_RENDER_SRC)
    assert {k: v["value"] for k, v in payload["params"].items()} == {
        "render_template_version": "20260913",
        "viewport_width": 620,
        "viewport_height": 1000,
        "oversized_image_bytes": 5 * 1024 * 1024,
        "qrcode_version": 1,
        "qrcode_error_correction": 1,
        "qrcode_box_size": 10,
        "qrcode_border": 1,
        "max_forward_text_len": 30000,
        "max_forward_nodes": 90,
        "text_split_punctuation": "。！？!?；;，,、…",
    }
    assert all(entry["source"] for entry in payload["params"].values()), "缺来源注记"


def test_extractor_zero_anchor_hit_fails_loudly(extractor: ModuleType) -> None:
    """上游改写参数位置导致锚点零命中时响亮失败（sync PR 变红交人工复核）。"""
    with pytest.raises(SystemExit, match="零命中"):
        extractor.extract("x = 1\n")


# 上游 Theme API v1 起 QRCode 调用点从 render/__init__.py 迁到 render/context.py，
# 提取源随之扩为「主源 + 上下文源」两棵树。
CONTEXT_QRCODE_SRC = (
    "def _build_qrcode(url):\n"
    "    qr = qrcode.QRCode(version=2, error_correction=1, box_size=6, border=1)\n"
)


def test_qrcode_anchor_follows_context_refactor(extractor: ModuleType) -> None:
    """主源无 QRCode 调用时锚点在 context 源命中，来源注记指向实际文件。"""
    main_wo_qr = UPSTREAM_RENDER_SRC.replace(
        "            qr = qrcode.QRCode(version=1, error_correction=1, box_size=10, border=1)\n",
        "            pass\n",
    )
    payload = extractor.extract(main_wo_qr, CONTEXT_QRCODE_SRC)
    assert payload["params"]["qrcode_version"]["value"] == 2
    assert payload["params"]["qrcode_version"]["source"].startswith("render/context.py")


def test_qrcode_anchor_single_when_moved(extractor: ModuleType) -> None:
    """迁移是搬家不是复制：两棵树各留一处调用会失去唯一性，响亮失败。"""
    with pytest.raises(SystemExit, match="区分度"):
        extractor.extract(UPSTREAM_RENDER_SRC, CONTEXT_QRCODE_SRC)


def test_extractor_ambiguous_anchor_fails_loudly(extractor: ModuleType) -> None:
    """锚点命中多个候选（失去区分度）时响亮失败，绝不静默取首个。"""
    ambiguous = UPSTREAM_RENDER_SRC.replace(
        "    async def resolve_parse_result(self, result):",
        '    RENDER_TEMPLATE_VERSION = "19990101"\n\n'
        "    async def resolve_parse_result(self, result):",
    )
    with pytest.raises(SystemExit, match="区分度"):
        extractor.extract(ambiguous)


def test_extractor_rejects_hostile_value(extractor: ModuleType) -> None:
    """提取值形态异常（回车符/超长/负数/超界整数）时在最前端响亮拒绝。"""
    with pytest.raises(SystemExit, match="回车符"):
        extractor._validate_value("k", "a\rb")
    with pytest.raises(SystemExit, match="上限"):
        extractor._validate_value("k", "长" * 2000)
    with pytest.raises(SystemExit, match="合理范围"):
        extractor._validate_value("k", -1)
    with pytest.raises(SystemExit, match="合理范围"):
        extractor._validate_value("k", 2**31)


def test_render_params_json_is_committed_and_well_formed() -> None:
    """提取产物入库且形态合法：provenance/revision/八锚点/值类型。"""
    data = json.loads(PARAMS_JSON_PATH.read_text(encoding="utf-8"))
    assert data["_provenance"].startswith("scripts/extract_render_params.py")
    assert isinstance(data["source_revision"], str) and len(data["source_revision"]) >= 7
    assert set(data["params"]) == _PARAM_NAMES
    for entry in data["params"].values():
        assert isinstance(entry["source"], str) and entry["source"]
        assert isinstance(entry["value"], str | int)


# ---- 第一层摄取：分析数据嵌入入库提取产物 ----


def test_analysis_includes_upstream_render_params(analyzer: ModuleType) -> None:
    """分析数据摄取渲染参数提取产物：值与入库 JSON 同源、来源注记随行。"""
    analysis = json.loads(analyzer.build_analysis())
    committed = json.loads(PARAMS_JSON_PATH.read_text(encoding="utf-8"))
    assert analysis["upstream_render_params"] == committed["params"]


def test_render_params_rejects_hostile_ingest(
    analyzer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提取产物被手改（伪 provenance/坏键/回车值/超界整数）时第一层摄取响亮拒绝。"""
    fake = tmp_path / "render_params.json"
    monkeypatch.setattr(analyzer, "_RENDER_PARAMS_PATH", fake)
    header = "scripts/extract_render_params.py 生成（上游 main 渲染参数提取，勿手改）"
    good = {"value": "20260913", "source": "render/__init__.py:RENDER_TEMPLATE_VERSION"}

    fake.write_text(json.dumps({"_provenance": "x", "params": {"a": good}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="不是提取脚本产物"):
        analyzer._upstream_render_params()

    fake.write_text(
        json.dumps({"_provenance": header, "params": {'bad key"; import os': good}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="合法 Python 标识符"):
        analyzer._upstream_render_params()

    fake.write_text(
        json.dumps({"_provenance": header, "params": {"k": {**good, "value": "a\rb"}}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="回车符"):
        analyzer._upstream_render_params()

    fake.write_text(
        json.dumps({"_provenance": header, "params": {"k": {**good, "value": -1}}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="合理范围"):
        analyzer._upstream_render_params()


# ---- 第二层：分析产物 + 注入模板 → render_params.py ----


def test_generator_rejects_handmodified_render_params_at_sink(
    gen: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """手改分析产物伪造渲染参数（回车符逃逸）时第二层汇点处同样响亮拒绝。"""
    forged = tmp_path / "vendor_analysis.json"
    forged.write_text(
        json.dumps(
            {
                "_provenance": "scripts/analyze_vendor.py 生成（第一层注入产物，勿手改）",
                "upstream_version": "1.3.6",
                "config_fields": {},
                "upstream_meta": {"description": "d", "license": "MIT", "readme": "r"},
                "upstream_texts": {
                    "render_failed": {
                        "value": "图片渲染失败",
                        "source": "render/__init__.py@main",
                    }
                },
                "upstream_render_params": {"k": {"value": "a\rb", "source": "x"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "ANALYSIS_PATH", forged)
    with pytest.raises(SystemExit, match="upstream_render_params"):
        gen._load_analysis()


def test_render_params_module_is_pure_data() -> None:
    """render_params.py 只含 __future__ 导入 + 常量赋值（生成模块零依赖）。"""
    tree = ast.parse((REPO_ROOT / "bridge" / "render_params.py").read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert all(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" for node in imports
    ), "生成模块必须保持零依赖（模板产出不得引入 import）"

    from astrbot_plugin_parser_lite.bridge.render_params import (
        MAX_FORWARD_NODES,
        MAX_FORWARD_TEXT_LEN,
        OVERSIZED_IMAGE_BYTES,
        QRCODE_BORDER,
        QRCODE_BOX_SIZE,
        QRCODE_ERROR_CORRECTION,
        QRCODE_VERSION,
        RENDER_TEMPLATE_VERSION,
        TEXT_SPLIT_PUNCTUATION,
        VIEWPORT_HEIGHT,
        VIEWPORT_WIDTH,
    )

    committed = json.loads(PARAMS_JSON_PATH.read_text(encoding="utf-8"))["params"]
    assert committed["render_template_version"]["value"] == RENDER_TEMPLATE_VERSION
    assert committed["viewport_width"]["value"] == VIEWPORT_WIDTH
    assert committed["viewport_height"]["value"] == VIEWPORT_HEIGHT
    assert committed["oversized_image_bytes"]["value"] == OVERSIZED_IMAGE_BYTES
    assert committed["qrcode_version"]["value"] == QRCODE_VERSION
    assert committed["qrcode_error_correction"]["value"] == QRCODE_ERROR_CORRECTION
    assert committed["qrcode_box_size"]["value"] == QRCODE_BOX_SIZE
    assert committed["qrcode_border"]["value"] == QRCODE_BORDER
    assert committed["max_forward_text_len"]["value"] == MAX_FORWARD_TEXT_LEN
    assert committed["max_forward_nodes"]["value"] == MAX_FORWARD_NODES
    assert committed["text_split_punctuation"]["value"] == TEXT_SPLIT_PUNCTUATION


# ---- 桥消费：生成物替代桥内硬编码 ----


def test_bridge_consumes_injected_params() -> None:
    """render.py 经生成模块消费注入参数，且值与入库 JSON 同源。"""
    import astrbot_plugin_parser_lite.bridge.render as bridge_render
    import astrbot_plugin_parser_lite.bridge.render_params as injected

    committed = json.loads(PARAMS_JSON_PATH.read_text(encoding="utf-8"))["params"]
    assert bridge_render.render_params is injected
    assert committed["render_template_version"]["value"] == injected.RENDER_TEMPLATE_VERSION
    assert committed["viewport_width"]["value"] == injected.VIEWPORT_WIDTH


def test_bridge_has_no_hardcoded_injected_values() -> None:
    """桥内不得再硬编码注入层参数（防双源漂移：上游 roll 后桥静默过期）。"""
    render_src = (REPO_ROOT / "bridge" / "render.py").read_text(encoding="utf-8")
    assert 'RENDER_TEMPLATE_VERSION = "' not in render_src, (
        "render.py 仍硬编码渲染缓存版本，应消费 render_params 生成物"
    )
    main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "render_params.OVERSIZED_IMAGE_BYTES" in main_src, "main.py 未消费注入的渲染图文件段阈值"
    sender_src = (REPO_ROOT / "bridge" / "sender.py").read_text(encoding="utf-8")
    assert "MAX_FORWARD_TEXT_LEN = 30000" not in sender_src, (
        "sender.py 仍硬编码转发文本上限，应消费 render_params 生成物"
    )
    assert 'frozenset("' not in sender_src, (
        "sender.py 仍硬编码切分标点集，应消费 render_params 生成物"
    )
    assert "render_params.MAX_FORWARD_TEXT_LEN" in sender_src, "sender.py 未消费注入的转发文本上限"
    assert "render_params.TEXT_SPLIT_PUNCTUATION" in sender_src, "sender.py 未消费注入的标点集"
    # 卡面画布宽必须来自注入的 viewport 宽：硬编码副本会在上游调整视口宽时
    # 静默产出错误缩放比（2026-09-14 评审发现的真实破口，此前无机械守护）
    assert "_T2I_CARD_WIDTH = 620" not in render_src, (
        "render.py 仍硬编码卡面画布宽，应消费 render_params 生成物"
    )
    assert "render_params.VIEWPORT_WIDTH" in render_src, "render.py 未消费注入的 viewport 宽"


# ---- 防漂移：本地有上游 main 引用时现场复提比对 ----


def test_render_params_match_upstream_when_clone_present(extractor: ModuleType) -> None:
    """按入库快照记录的 source_revision 现场复提比对（手改或漂移即红）。

    锚定快照自身的 source_revision 而非本地 origin/main ref——后者可能因
    镜像缓存竞态滞后于真实上游（2026-09-14 实证），入库件的源以快照记录
    为准。
    """
    clone = REPO_ROOT / ".sync-work" / "upstream"
    probe = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("无上游克隆（CI 无克隆），跳过提取复检")

    committed = json.loads(PARAMS_JSON_PATH.read_text(encoding="utf-8"))
    revision = committed["source_revision"]
    fresh = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "show",
            f"{revision}:src/nonebot_plugin_parser_lite/render/__init__.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fresh.returncode != 0:
        pytest.skip("快照 source_revision 对象不在本地克隆，跳过提取复检")
    assert extractor.extract(fresh.stdout)["params"] == committed["params"]
