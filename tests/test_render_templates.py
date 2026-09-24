"""模板数据面契约：提取防火墙 → 入库快照 → 生成端逐字节直通。

桥 templates/ 与上游 main render/templates 逐字节等价（2026-09-13 活体核验
sha256 全等）；本模块守护三层：提取端文件名/内容防火墙与 digest 公式、
生成端逐字节直通（不做 EOF 归一化）与桥自有模板白名单、入库快照与磁盘
模板族的漂移反证。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def templates_mod() -> ModuleType:
    return _load(
        "extract_render_templates",
        REPO_ROOT / "scripts" / "extract_render_templates.py",
    )


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load("generate_config_tpl", REPO_ROOT / "scripts" / "generate_config.py")


_FILES = {
    "default.html.jinja": "<html>{{ result.title }}</html>\n",
    "macros.jinja": "{% macro cover(item) %}{{ item | safe_src }}{% endmacro %}\n",
    "tailwind.css": "body{margin:0}\n",
}


# ---- 提取端：防火墙与 digest 公式 ----


def test_build_payload_shape_and_digest(templates_mod: ModuleType) -> None:
    payload = templates_mod.build_payload(_FILES, "deadbeef" * 5)
    assert payload["_provenance"].startswith("scripts/extract_render_templates.py")
    assert payload["files"] == dict(sorted(_FILES.items()))
    assert payload["source_revision"] == "deadbeef" * 5
    joined = "".join(_FILES[name] for name in sorted(_FILES))
    assert payload["source_digest"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()


def test_name_firewall(templates_mod: ModuleType) -> None:
    for bad in ("../evil.jinja", "sub/inner.css", "ghost.sh", ".hidden.css", ""):
        with pytest.raises(SystemExit, match="文件名越界"):
            templates_mod.validate_name(bad)
    templates_mod.validate_name("default.html.jinja")
    templates_mod.validate_name("tailwind.css")
    # Theme API v1：主题清单与模板同目录，属模板平面的字节快照面
    templates_mod.validate_name("theme.json")


def test_content_firewall(templates_mod: ModuleType) -> None:
    with pytest.raises(SystemExit, match="为空"):
        templates_mod.validate_content("a.css", "   \n")
    with pytest.raises(SystemExit, match="上限"):
        templates_mod.validate_content("a.css", "x" * (512 * 1024 + 1))
    templates_mod.validate_content("a.css", "body{}\n")


# ---- 生成端：逐字节直通与白名单 ----


def test_gen_template_files_verbatim(gen: ModuleType) -> None:
    """模板工件不做 EOF 归一化——内容与快照逐字节等价（含无尾换行的文件）。"""
    analysis = {
        "upstream_render_templates": {
            "files": {"a.css": "body{}", "b.jinja": "x\n"},
            "source_revision": "deadbeef" * 5,
        },
    }
    out = gen.build_template_files(analysis)
    assert out[gen.RENDER_TEMPLATES_DIR / "a.css"] == "body{}"
    assert out[gen.RENDER_TEMPLATES_DIR / "b.jinja"] == "x\n"


def test_gen_expected_template_names_whitelist(
    gen: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = {
        "upstream_render_templates": {
            "files": {"a.css": "body{}"},
            "source_revision": "deadbeef" * 5,
        },
    }
    assert gen.expected_template_names(analysis) == frozenset({"a.css"})
    monkeypatch.setattr(gen, "BRIDGE_TEMPLATE_EXTRAS", frozenset({"bridge.css"}))
    assert gen.expected_template_names(analysis) == frozenset({"a.css", "bridge.css"})


# ---- 入库快照与磁盘模板族：漂移反证 ----


def test_committed_snapshot_matches_disk_templates() -> None:
    """入库快照形态齐备，且桥 templates/ 与快照逐字节等价（漂移即红）。"""
    data = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "render_templates.json").read_text(encoding="utf-8")
    )
    assert set(data) == {"_provenance", "files", "source_revision", "source_digest"}
    assert data["_provenance"].startswith("scripts/extract_render_templates.py")
    files = data["files"]
    on_disk = {
        p.name: p.read_text(encoding="utf-8")
        for p in (REPO_ROOT / "templates").iterdir()
        if p.is_file()
    }
    assert on_disk == files, "桥 templates/ 与入库快照漂移（请重跑 scripts/run_injection.py）"
    joined = "".join(files[name] for name in sorted(files))
    assert data["source_digest"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()
