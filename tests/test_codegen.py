"""两层注入契约：全管线再生（vendor → 分析数据 → 工件）与语义完备
（工单 codegen-injection/04-06）。

第一层 scripts/analyze_vendor.py 从 vendor introspection 产出分析数据
vendor_analysis.json（管线临时产物，**不入库**，上游元信息不由本仓库
维护）；第二层 scripts/generate_config.py 只消费该数据渲染
_conf_schema.json / gen_config.py / metadata.yaml 三个入库工件。任一
工件与全管线再生结果不一致（上游快照滚动后未重跑流水线）时此处变红。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import textwrap
import tomllib
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Config
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
    PlatformEnum,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_PATH = REPO_ROOT / "scripts" / "analyze_vendor.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_config.py"
EXTRACTOR_PATH = REPO_ROOT / "scripts" / "extract_display_texts.py"
# 上游同步工作流（在役）：roll 序列由 scripts/roll_local.py 单实现承载，
# 本文件只守护「工作流没有把注入面抄成第二份」这一层。
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "sync-upstream.yml"

ANALYSIS_PATH = REPO_ROOT / "vendor_analysis.json"
ANALYSIS_KINDS = {"bool", "int", "float", "string", "list", "enum", "enum_list"}


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def analyzer() -> ModuleType:
    return _load_module("analyze_vendor", ANALYZER_PATH)


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load_module("generate_config", GENERATOR_PATH)


@pytest.fixture(scope="module")
def extractor() -> ModuleType:
    return _load_module("extract_display_texts", EXTRACTOR_PATH)


# ---- 第一层：上游分析 → vendor_analysis.json ----


def test_analysis_regenerated_from_vendor(analyzer: ModuleType) -> None:
    """第一层现场再生：vendor → 分析数据（管线临时产物，不入库），幂等确定。"""
    content = analyzer.build_analysis()
    ANALYSIS_PATH.write_text(content, encoding="utf-8")
    assert content == analyzer.build_analysis()


def test_analysis_data_is_not_committed() -> None:
    """上游元信息数据不由本仓库维护：.gitignore 必须排除第一层分析产物。"""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/vendor_analysis.json" in gitignore


def test_analysis_covers_all_vendor_fields(analyzer: ModuleType) -> None:
    """分析数据覆盖上游全部 plite_* 字段，来源契约与形态合法。"""
    analysis = json.loads(analyzer.build_analysis())
    vendor_names = {n for n in Config.model_fields if n.startswith("plite_")}
    assert set(analysis["config_fields"]) == vendor_names
    assert analysis["_provenance"].startswith("scripts/analyze_vendor.py")
    for name, field in analysis["config_fields"].items():
        assert field["description"], f"{name} 缺描述文本"
        assert field["kind"] in ANALYSIS_KINDS, f"{name} 类型形态非法：{field['kind']}"


def test_analysis_unknown_type_fails_loudly(analyzer: ModuleType) -> None:
    """未知类型形态显式报错，而非静默产出错误分析产物。"""
    with pytest.raises(SystemExit, match="未知类型形态"):
        analyzer._shape(dict[str, int])


def test_analysis_missing_doc_fails_loudly(
    analyzer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游字段缺描述时分析响亮失败（描述不得静默回退为字段名）。"""
    monkeypatch.setattr(analyzer, "_field_docstrings", lambda: {})
    with pytest.raises(SystemExit, match="缺描述"):
        analyzer.build_analysis()


def test_analysis_dynamic_default_fails_loudly(analyzer: ModuleType) -> None:
    """动态默认（default_factory 产物）无法写入分析产物时响亮失败。"""
    with pytest.raises(SystemExit, match="动态默认"):
        analyzer._json_safe(object())


def test_field_name_must_be_identifier(analyzer: ModuleType) -> None:
    """注入防火墙：字段名只接受小写 Python 标识符（渲染进生成代码的字符串字面量）。"""
    analyzer.validate_field_name("plite_video_file_threshold_mb")
    for hostile in ('plite_x"; import os', "plite-x", "Plite_x", "plite x", "plite_x\n"):
        with pytest.raises(SystemExit, match="合法 Python 标识符"):
            analyzer.validate_field_name(hostile)


def test_version_must_be_well_formed(analyzer: ModuleType) -> None:
    """注入防火墙：版本号只接受 PEP 440 风格形态（写入 metadata YAML 裸值与注释）。"""
    for ok in ("1.3.6", "1.3.6.post1", "1.3.6+git.abc"):
        analyzer.validate_version(ok)
    for hostile in ('1.3.6"\nimport os', "1.3.6 dev", "latest", ""):
        with pytest.raises(SystemExit, match="版本号格式异常"):
            analyzer.validate_version(hostile)


def test_analysis_rejects_hostile_field_name(
    analyzer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游出现非标识符字段名（如动态 create_model）时在第一层响亮拒绝。"""
    hostile_field = SimpleNamespace(annotation=bool, default=False)
    hostile_config = SimpleNamespace(model_fields={'plite_x"; import os': hostile_field})
    monkeypatch.setattr(analyzer, "Config", hostile_config)
    monkeypatch.setattr(analyzer, "_field_docstrings", lambda: {})
    with pytest.raises(SystemExit, match="合法 Python 标识符"):
        analyzer.build_analysis()


# ---- 第二层：分析产物 + 模板 → 仓库工件 ----


def test_repo_artifacts_are_fresh(gen: ModuleType, analyzer: ModuleType) -> None:
    """仓库工件 == 全管线再生（vendor → 分析数据 → 渲染，幂等）。"""
    ANALYSIS_PATH.write_text(analyzer.build_analysis(), encoding="utf-8")
    analysis = json.loads(analyzer.build_analysis())
    for path, content in gen.build_artifacts(analysis).items():
        assert path.read_text(encoding="utf-8") == content


def test_generator_consumes_analysis_only(gen: ModuleType) -> None:
    """第二层零 vendor 依赖：不 import 仓库包（分析只发生在第一层）。"""
    tree = ast.parse(GENERATOR_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.startswith("astrbot_plugin_parser_lite")]
    assert not offenders, f"第二层生成器不得依赖 vendor：{offenders}"


def test_generator_rejects_unknown_kind(gen: ModuleType) -> None:
    """分析产物出现未知形态时第二层响亮失败，而非静默产出错误 schema。"""
    with pytest.raises(SystemExit, match="未知类型形态"):
        gen._astrbot_type({"kind": "object"})


def test_generator_rejects_non_analysis_input(
    gen: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二层拒收非第一层产物的输入（防御手工伪造/旧格式）。"""
    fake = tmp_path / "vendor_analysis.json"
    fake.write_text(json.dumps({"upstream_version": "9.9.9"}), encoding="utf-8")
    monkeypatch.setattr(gen, "ANALYSIS_PATH", fake)
    with pytest.raises(SystemExit, match="不是第一层分析产物"):
        gen.build_conf_schema()
    fake.write_text(json.dumps({"_provenance": "x", "config_fields": {}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="不是第一层分析产物"):
        gen.build_conf_schema()


def test_stale_string_options_fail_loudly(gen: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """选项覆盖指向已不存在的上游字段时响亮失败（否则面板静默退化为自由文本）。"""
    monkeypatch.setattr(gen, "STRING_OPTIONS", {"plite_renamed_field": ["a"]})
    with pytest.raises(SystemExit, match="STRING_OPTIONS"):
        gen.build_conf_schema()


def test_non_string_null_default_fails_loudly(gen: ModuleType) -> None:
    """非 string 类型的 null 默认值响亮失败（面板需要具体默认值）。"""
    with pytest.raises(SystemExit, match="默认值为 null"):
        gen._default_value("plite_x", {"kind": "int", "default": None}, "int")


def test_metadata_version_follows_analysis(gen: ModuleType, analyzer: ModuleType) -> None:
    """metadata.yaml version 与第一层 upstream_version 锁步（版本跟随统一进流水线）。"""
    analysis = json.loads(analyzer.build_analysis())
    metadata = (REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert f"version: v{analysis['upstream_version']}" in metadata


def test_plugin_meta_handwritten_surface_is_minimal(gen: ModuleType) -> None:
    """手写面收缩契约：metadata 手写项仅限桥身份/桥展示/桥用法三类。

    版本与上游溯源仍由注入层提供（version 随上游 roll 再生）。集合任何变化
    都意味着手写面扩张，需重新评审。
    """
    assert set(gen.PLUGIN_META) == {
        "name",
        "author",
        "display_name",
        "desc",
        "short_desc",
        "repo",
        "astrbot_version",
        "support_platforms",
        "tags",
        "usage",
    }


def test_analysis_includes_upstream_meta(analyzer: ModuleType) -> None:
    """上游元数据（description/license）进分析数据，与上游 pyproject 同源。"""
    analysis = json.loads(analyzer.build_analysis())
    project = tomllib.loads(
        (analyzer.REPO_ROOT / "vendor" / "_upstream" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert analysis["upstream_meta"]["description"] == project["description"]
    assert analysis["upstream_meta"]["license"] == project["license"]
    assert analysis["upstream_meta"]["readme"] == (
        analyzer.REPO_ROOT / "vendor" / "_upstream" / "README.md"
    ).read_text(encoding="utf-8")


def test_metadata_upstream_provenance_is_verbatim(
    gen: ModuleType,
    analyzer: ModuleType,
) -> None:
    """上游 description/license 逐字节留作溯源注释（不再混入展示字段）。

    展示文案改为桥身份手写后，上游原文仍必须在产物中可见：这是「数据层
    上游驱动」承诺的可验证痕迹，也是 generate_config 汇点断言在产物侧的镜像。
    """
    ANALYSIS_PATH.write_text(analyzer.build_analysis(), encoding="utf-8")
    analysis = json.loads(analyzer.build_analysis())
    metadata = gen.build_metadata()
    assert f"# 上游 description：{analysis['upstream_meta']['description']}" in metadata
    assert f"# 上游许可证 {analysis['upstream_meta']['license']}" in metadata


def test_metadata_satisfies_astrbot_required_fields(gen: ModuleType) -> None:
    """AstrBot 必填四项齐备且为非空字符串。

    口径对齐 astrbot.core.star.updater 的 ``PLUGIN_METADATA_REQUIRED_FIELDS``
    与 ``validate_plugin_metadata`` 的「必须是非空字符串」校验——缺任一字段
    或为空串，插件都会加载失败。
    """
    metadata = yaml.safe_load(gen.build_metadata())
    for field in ("name", "desc", "version", "author"):
        assert isinstance(metadata.get(field), str) and metadata[field].strip(), field


def test_metadata_display_texts_are_chinese(gen: ModuleType) -> None:
    """展示文案（display_name/desc/short_desc）统一中文，与 usage 同源。"""
    for field in ("display_name", "desc", "short_desc"):
        assert re.search(r"[一-鿿]", gen.PLUGIN_META[field]), f"{field} 非中文文案"


def test_metadata_repo_points_at_plugin_repo(gen: ModuleType) -> None:
    """repo 供更新器与市场页定位仓库，须指向与插件同名的仓库。"""
    metadata = yaml.safe_load(gen.build_metadata())
    assert metadata["repo"] == f"https://github.com/Chuubururin/{metadata['name']}"


def test_metadata_astrbot_version_is_pep440_without_v_prefix(gen: ModuleType) -> None:
    """astrbot_version：PEP 440 版本规格，且不得带 v 前缀（文档硬要求）。"""
    value = yaml.safe_load(gen.build_metadata())["astrbot_version"]
    assert not value.startswith("v"), "文档要求 astrbot_version 不带 v 前缀"
    clauses = [clause.strip() for clause in value.split(",")]
    assert clauses and all(
        re.fullmatch(r"(~=|>=|<=|==|!=|>|<)?\d[\w.*+!-]*", clause) for clause in clauses
    ), value


def test_metadata_support_platforms_are_known_adapters(gen: ModuleType) -> None:
    """support_platforms 必须是 ADAPTER_NAME_2_TYPE 的键位。

    否则 WebUI 会展示一个 AstrBot 不认识的平台。依赖真实 AstrBot 运行时
    （见 tests/requirements-test.txt）；``importorskip`` 仅作本地未装时的
    优雅降级。
    """
    pytest.importorskip("astrbot")
    from astrbot.core.star.filter.platform_adapter_type import ADAPTER_NAME_2_TYPE

    platforms = yaml.safe_load(gen.build_metadata())["support_platforms"]
    assert platforms == ["aiocqhttp"], (
        "只背书真实验证过的平台（aiocqhttp = OneBot v11）；"
        f"要扩列需先跑通对应适配器的实测，当前={platforms}"
    )
    unknown = set(platforms) - set(ADAPTER_NAME_2_TYPE)
    assert not unknown, f"未知平台键位：{sorted(unknown)}"


def test_metadata_tags_are_non_empty_strings(gen: ModuleType) -> None:
    """tags 供插件市场分类/搜索（官方发布文档字段）。

    宿主 StarMetadata 不解析它（由市场侧消费），故不在必填/可选字段校验内；
    但格式仍须是字符串列表，否则市场侧解析会拿到脏值。
    """
    tags = yaml.safe_load(gen.build_metadata())["tags"]
    assert isinstance(tags, list) and tags, "tags 不应为空列表"
    assert all(isinstance(tag, str) and tag.strip() for tag in tags), tags


def test_readme_upstream_passthrough_and_minimal_bridge(
    gen: ModuleType,
    analyzer: ModuleType,
) -> None:
    """README 契约：上游正文逐字直通 + 桥接说明行数上限锁死（含核准的
    「安全说明」披露节：SSRF/ffmpeg 例外、TLS 姿态、渲染数据外发）。"""
    ANALYSIS_PATH.write_text(analyzer.build_analysis(), encoding="utf-8")
    upstream = json.loads(analyzer.build_analysis())["upstream_meta"]["readme"]
    generated = gen.build_readme()
    assert upstream in generated, "上游 README 必须逐字直通"
    bridge_extra = generated.replace(upstream, "")
    assert len(bridge_extra.splitlines()) <= 40, (
        "桥接说明超出上限（≤40 行，含安全披露节），手写面失控"
    )


def test_upstream_meta_rejects_yaml_unsafe(
    analyzer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """注入防火墙：上游 description 含换行/引号等 YAML 不安全字符时响亮拒绝。"""
    upstream = tmp_path / "vendor" / "_upstream"
    upstream.mkdir(parents=True)
    (upstream / "pyproject.toml").write_text(
        '[project]\ndescription = "bad\\nline"\nlicense = "MIT"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(analyzer, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="YAML 不安全"):
        analyzer._vendor_meta()


# ---- 文本注入层：上游 main 显示文本 → texts.py ----


def test_analysis_includes_upstream_texts(analyzer: ModuleType) -> None:
    """分析数据摄取显示文本提取产物：值与入库 JSON 同源、来源注记随行。"""
    analysis = json.loads(analyzer.build_analysis())
    committed = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "display_texts.json").read_text(encoding="utf-8")
    )
    assert set(analysis["upstream_texts"]) == set(committed["texts"])
    for key, entry in committed["texts"].items():
        assert analysis["upstream_texts"][key]["value"] == entry["value"]
        assert analysis["upstream_texts"][key]["source"] == entry["source"]


def test_extractor_follows_anchors(extractor: ModuleType) -> None:
    """锚点规则在代表性上游结构上提取全部文案（f-string 归一为 {n} 占位模板）。"""
    render_src = textwrap.dedent(
        """\
        def f(result, embed, item, cont, option, pct):
            msg = UniMessage(image_seg or "图片渲染失败")
            msg += "\\n在线播放: " + embed
            yield UniMessage(f"媒体太大啦，还是去{result.platform.display_name}看看吧~")
            nodes.append(f"[媒体加载失败：{type(cont).__name__}]")
            append_text(item.desc or "[表情]")
            poll_parts = [f"【投票】{item.title or '投票'}"]
            poll_parts.extend(
                f"- {option.text}: {option.votes} 票 "
                f"({pct:.1f}%)"
                for option in item.options
            )
            status = ["已结束" if item.closed else "进行中"]
            if item.multiple:
                status.append("多选")
            if item.total_voters is not None:
                status.append(f"{item.total_voters} 人参与")
            poll_parts.append(" · ".join(status))
            ordered.append(">>>>>原帖<<<<<")
            if failed_count > 0:
                message = f"{failed_count} 项媒体下载失败"
            logger.warning("日志不是用户文案：%s", item)
        """
    )
    matchers_src = textwrap.dedent(
        """\
        async def handler():
            await UniMessage(
                f"请在{LazyManager.TIMEOUT_SECONDS}秒内发送以下命令之一来获取媒体资源: "
                f"\\n{download_cmd}"
            ).send()
        """
    )
    exception_src = textwrap.dedent(
        """\
        class DownloadException(ParseException):
            def __init__(self, message: str | None = None):
                super().__init__(message or "媒体下载失败")
        """
    )
    helper_src = textwrap.dedent(
        """\
        class UniHelper:
            async def video_seg(cls, file, thumbnail=None):
                stat = await file.stat()
                if stat.st_size == 0:
                    return Text("视频文件大小为 0")
        """
    )
    bilibili_src = textwrap.dedent(
        """\
        bangumi = {"danmaku": ("弹幕", "1"), "coin": ("硬币", "2")}
        video = {"danmaku": ("弹幕", "3"), "coin": ("硬币", "4")}
        """
    )
    context_src = textwrap.dedent(
        """\
        async def _display_size(item):
            try:
                return await item.get_display_size()
            except Exception:
                return "未知大小"

        async def _serialize_content(item, is_cover=False):
            if is_cover:
                content["alt"] = "专辑封面"
        """
    )
    payload = extractor.extract(
        render_src,
        matchers_src,
        "<span>评论</span>",
        exception_src,
        helper_src,
        bilibili_src,
        context_src,
    )
    texts = {key: entry["value"] for key, entry in payload["texts"].items()}
    assert texts == {
        "render_failed": "图片渲染失败",
        "online_play": "\n在线播放: ",
        "repost_marker": ">>>>>原帖<<<<<",
        "oversized_hint": "媒体太大啦，还是去{0}看看吧~",
        "media_failed": "[媒体加载失败：{0}]",
        "download_failed": "媒体下载失败",
        "download_failed_count": "{0} 项媒体下载失败",
        "sticker_placeholder": "[表情]",
        "poll_header": "【投票】{0}",
        "poll_title_fallback": "投票",
        "poll_option_line": "- {0}: {1} 票 ({2:.1f}%)",
        "poll_status_closed": "已结束",
        "poll_status_open": "进行中",
        "poll_status_multiple": "多选",
        "poll_voters": "{0} 人参与",
        "status_separator": " · ",
        "lazy_download_prompt": "请在{0}秒内发送以下命令之一来获取媒体资源: \n{1}",
        "video_zero_size": "视频文件大小为 0",
        "extra_label_danmaku": "弹幕",
        "extra_label_coin": "硬币",
        "unknown_size": "未知大小",
        "cover_alt": "专辑封面",
    }
    assert payload["template_texts"] == ["评论"]


def test_extractor_zero_anchor_hit_fails_loudly(extractor: ModuleType) -> None:
    """上游改写文案导致锚点零命中时响亮失败（sync PR 变红交人工复核）。"""
    with pytest.raises(SystemExit, match="零命中"):
        extractor.extract("x = '无关'", "", "", "")


def test_extractor_ambiguous_anchor_fails_loudly(extractor: ModuleType) -> None:
    """锚点命中多个**值不同**的候选（真失去区分度）时响亮失败。"""
    found = [("const", "在线播放一", 1), ("const", "在线播放二", 2)]
    with pytest.raises(SystemExit, match="区分度"):
        extractor._select(found, "online_play", ("contains", "在线播放"))


def test_extractor_identical_duplicate_anchor_passes(extractor: ModuleType) -> None:
    """多处命中但值全同（上游复制同文案）：镜像结果与命中数无关，放行。

    上游 Theme API 重构（1.3.8rc6）把「媒体加载失败」模板复制进两条渲染
    路径，锚点按值取用不取位置——区分度只对「值分叉」有意义。
    """
    found = [("tmpl", "[媒体加载失败：{0}]", 444), ("tmpl", "[媒体加载失败：{0}]", 494)]
    assert extractor._select(found, "media_failed", ("contains", "媒体加载失败")) == (
        "[媒体加载失败：{0}]"
    )


def test_extractor_rejects_hostile_value(extractor: ModuleType) -> None:
    """提取值含回车符或超长（疑似异常快照）时在最前端响亮拒绝。"""
    with pytest.raises(SystemExit, match="回车符"):
        extractor._validate_value("key", "a\rb")
    with pytest.raises(SystemExit, match="上限"):
        extractor._validate_value("key", "长" * 2000)


def test_display_texts_rejects_hostile_ingest(
    analyzer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提取产物被手改（伪 provenance/坏键/回车值）时第一层摄取响亮拒绝。"""
    fake = tmp_path / "display_texts.json"
    monkeypatch.setattr(analyzer, "_DISPLAY_TEXTS_PATH", fake)
    header = "scripts/extract_display_texts.py 生成（上游 main 显示文本提取，勿手改）"
    good_entry = {"value": "图片渲染失败", "source": "render/__init__.py@main"}

    fake.write_text(json.dumps({"_provenance": "x", "texts": {"a": good_entry}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="不是提取脚本产物"):
        analyzer._upstream_texts()

    fake.write_text(
        json.dumps({"_provenance": header, "texts": {'bad key": import os': good_entry}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="合法 Python 标识符"):
        analyzer._upstream_texts()

    fake.write_text(
        json.dumps(
            {"_provenance": header, "texts": {"render_failed": {**good_entry, "value": "a\rb"}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="回车符"):
        analyzer._upstream_texts()


def test_generator_rejects_handmodified_texts_at_sink(
    gen: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """手改分析产物伪造显示文本（回车符/超长）时第二层汇点处同样响亮拒绝。"""
    forged = tmp_path / "vendor_analysis.json"
    forged.write_text(
        json.dumps(
            {
                "_provenance": "scripts/analyze_vendor.py 生成（第一层注入产物，勿手改）",
                "upstream_version": "1.3.6",
                "config_fields": {},
                "upstream_meta": {"description": "d", "license": "MIT", "readme": "r"},
                "upstream_texts": {
                    "render_failed": {"value": "bad\rline", "source": "render/__init__.py@main"}
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "ANALYSIS_PATH", forged)
    with pytest.raises(SystemExit, match="upstream_texts"):
        gen._load_analysis()


def test_texts_module_is_pure_data() -> None:
    """texts.py 只含 __future__ 导入 + 常量赋值（生成模块零依赖、可导入）。"""
    tree = ast.parse((REPO_ROOT / "bridge" / "texts.py").read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert all(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" for node in imports
    ), "生成模块必须保持零依赖（模板产出不得引入 import）"

    from astrbot_plugin_parser_lite.bridge.texts import LAZY_DOWNLOAD_PROMPT, RENDER_FAILED

    assert RENDER_FAILED == "图片渲染失败"
    assert "{0}" in LAZY_DOWNLOAD_PROMPT and "{1}" in LAZY_DOWNLOAD_PROMPT


def test_display_texts_match_upstream_when_clone_present(extractor: ModuleType) -> None:
    """按入库快照记录的 source_revision 现场复提比对（手改或漂移即红）。

    锚定快照自身的 source_revision——本地 origin/main ref 可能因镜像缓存
    竞态滞后于真实上游。
    """
    clone = REPO_ROOT / ".sync-work" / "upstream"
    probe = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("无上游 main 引用（CI 无克隆），跳过提取复检")
    committed = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "display_texts.json").read_text(encoding="utf-8")
    )
    revision = committed["source_revision"]

    def show(path: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(clone), "show", f"{revision}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("快照 source_revision 对象不在本地克隆，跳过提取复检")
        return result.stdout

    pkg = "src/nonebot_plugin_parser_lite"
    context = subprocess.run(
        ["git", "-C", str(clone), "show", f"{revision}:{pkg}/render/context.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    # 前 Theme API 上游无 context.py：与提取器的可选源语义一致回退空串
    context_src = context.stdout if context.returncode == 0 else ""
    fresh = extractor.extract(
        show(f"{pkg}/render/__init__.py"),
        show(f"{pkg}/matchers/__init__.py"),
        show(f"{pkg}/render/templates/macros.jinja"),
        show(f"{pkg}/exception.py"),
        show(f"{pkg}/helper.py"),
        show(f"{pkg}/parsers/bilibili/__init__.py"),
        context_src,
    )
    assert fresh["texts"] == committed["texts"]
    assert fresh["template_texts"] == committed["template_texts"]


def test_schema_covers_all_vendor_fields(gen: ModuleType) -> None:
    """schema 覆盖上游全部 plite_* 字段，类型合法、默认值与 pydantic 同源。"""
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    vendor_names = {n for n in Config.model_fields if n.startswith("plite_")}
    missing = vendor_names - set(schema)
    assert not missing, f"schema 缺上游字段，请重跑生成器：{sorted(missing)}"

    for name in vendor_names:
        entry = schema[name]
        assert entry["description"], f"{name} 缺描述文本"
        assert entry["type"] in gen._ASTRBOT_TYPES, f"{name} 类型非法：{entry['type']}"
        default = entry["default"]
        if entry["type"] == "list":
            assert isinstance(default, list), f"{name} 的 list 类型默认值应为列表"
        elif entry["type"] == "bool":
            assert isinstance(default, bool), f"{name} 的 bool 类型默认值应为布尔"
        elif entry["type"] == "int":
            assert isinstance(default, int) and not isinstance(default, bool), (
                f"{name} 的 int 类型默认值应为整数"
            )


def test_enum_options_match_enum_members() -> None:
    """枚举字段 options 与上游枚举成员同源（独立于流水线实现重推）。"""
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    platforms = schema["plite_disabled_platforms"]
    assert platforms["options"] == [e.value for e in PlatformEnum]

    quality_ann = Config.model_fields["plite_bili_video_quality"].annotation
    assert isinstance(quality_ann, type) and issubclass(quality_ann, Enum)
    assert schema["plite_bili_video_quality"]["options"] == [e.value for e in quality_ann]
    assert (
        schema["plite_bili_video_quality"]["default"]
        == Config.model_fields["plite_bili_video_quality"].default.value
    )


def test_schema_no_orphan_keys() -> None:
    """schema 键集合 = 生成模块的 VENDOR_FIELDS ∪ BRIDGE_FIELDS（无孤儿键）。"""
    from astrbot_plugin_parser_lite.bridge.gen_config import BRIDGE_FIELDS, VENDOR_FIELDS

    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert set(schema) == set(VENDOR_FIELDS) | set(BRIDGE_FIELDS)


def test_gen_config_module_is_pure_data() -> None:
    """gen_config.py 只含 __future__ 导入的纯数据模块（main.py 与测试环境皆可导入）。"""
    tree = ast.parse((REPO_ROOT / "bridge" / "gen_config.py").read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert all(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" for node in imports
    ), "生成模块必须保持零依赖（模板产出不得引入 import）"

    from astrbot_plugin_parser_lite.bridge.gen_config import VENDOR_FIELDS

    vendor_names = {n for n in Config.model_fields if n.startswith("plite_")}
    assert set(VENDOR_FIELDS) == vendor_names


def test_bridge_vendor_key_clash_fails_loudly(
    gen: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游新增与桥自有同名字段时响亮失败，而非静默覆盖桥条目。"""
    monkeypatch.setattr(
        gen,
        "BRIDGE_FIELDS",
        {"plite_append_url": dict(gen.BRIDGE_FIELDS["plite_render"])},
    )
    with pytest.raises(SystemExit, match="同名"):
        gen.build_conf_schema()


def test_generator_rejects_bad_version_at_sink(
    gen: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """手改产物伪造版本号（如换行逃逸 YAML/注释）时，渲染汇点处同样响亮拒绝。"""
    forged = tmp_path / "vendor_analysis.json"
    forged.write_text(
        json.dumps(
            {
                "_provenance": "scripts/analyze_vendor.py 生成（第一层注入产物，勿手改）",
                "upstream_version": '1.3.6"\nimport os',
                "config_fields": {
                    "plite_x": {"kind": "bool", "description": "说明", "default": True}
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "ANALYSIS_PATH", forged)
    with pytest.raises(SystemExit, match="upstream_version"):
        gen._load_analysis()


# ---------------------------------------------------------------------------
# sync-upstream 在役契约：roll 序列单实现，工作流只做编排
# ---------------------------------------------------------------------------
#
# 本节判据各自对应一个真实故障模式：
#
#   · 提交清单漏项 ⇒ roll 带旧桥工件配新 vendor（上游文案/参数静默失效）：
#     清单住在 roll_local.ROLL_ADD_PATHS，本文件钉它对生成工件的覆盖；
#   · state 缺尾换行 ⇒ end-of-file-fixer 在 sync PR 上判红 ⇒ automerge
#     永不满足：_state_write 的字节公式由功能测试钉住；
#   · automerge 不设「触碰 vendor 之外即转人工」式的排除清单——sync 只
#     合并自己产出的 PR，bot PR 的内容域就是 ROLL_ADD_PATHS，
#     无需这道二级防线。


def _load_roll_local() -> ModuleType:
    return _load_module("roll_local", REPO_ROOT / "scripts" / "roll_local.py")


def test_sync_workflow_delegates_roll_sequence_to_shared_scripts() -> None:
    """工作流必须委派 roll_local + upstream_sync，不得抄第二份注入/判据实现。"""
    assert WORKFLOW_PATH.exists(), "sync-upstream.yml 缺失（工作流集合契约见 test_branch_model.py）"
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "scripts/roll_local.py" in text, "roll 序列必须走 roll_local 单实现"
    assert "scripts/upstream_sync.py" in text, "供应链判据必须走 upstream_sync 单实现"
    # 注入/校验是 roll_local 的内部步骤；工作流直呼其名 = 出现第二份编排事实
    for forbidden in ("run_injection.py", "verify_vendor.py"):
        assert forbidden not in text, f"工作流抄了 roll_local 内部步骤：{forbidden}"


def test_sync_workflow_pr_title_uses_reserved_merge_prefix() -> None:
    """sync PR 标题带 [merge] 保留前缀——squash 合并后 promote 的触发开关。"""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "[merge] sync:" in text


def test_roll_commit_list_covers_all_generated_artifacts() -> None:
    """ROLL_ADD_PATHS 必须覆盖全部生成工件——漏一项 = sync PR 带旧工件配新 vendor。"""
    roll = _load_roll_local()
    committed = {str(p).rstrip("/") for p in roll.ROLL_ADD_PATHS}
    required = {
        "vendor",
        "metadata.yaml",
        "_conf_schema.json",
        "bridge/gen_config.py",
        "bridge/texts.py",
        "bridge/render_params.py",
        "templates",
        "README.md",
        ".github/sync-state.json",
        "requirements.txt",
    }
    missing = {
        path
        for path in required
        if not any(path == c or path.startswith(c + "/") for c in committed)
    }
    assert not missing, f"roll 提交清单缺生成工件：{sorted(missing)}"


def test_state_write_keeps_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync-state.json 必须带尾换行——否则 end-of-file-fixer 在 sync PR 上判红。"""
    roll = _load_roll_local()
    state = tmp_path / "sync-state.json"
    monkeypatch.setattr(roll, "STATE_PATH", state)
    roll._state_write({"standalone_sha": "x" * 40})
    assert state.read_bytes().endswith(b"\n")
