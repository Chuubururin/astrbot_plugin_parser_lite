"""release advisory 契约：跨越区间语义、节解析与出站钉扎。

scripts/release_advisory.py 把上游「1.3.2+ release notes 机器可读」的
规律接进 roll 输出面。本文件钉住三类不可让渡的语义：

- 区间 (old, new] 只收 stable release：预发布滚动零跨越；跨越进 stable
  1.3.7 的那次 roll 才呈现 1.3.7 的 notes（预发布内容不误记）；
- 💥 节必带「需人工确认桥接面影响」标记（工作流 automerge 分层以该
  字符串为门），🐛/💫 不进 advisory（修复项由契约测试门把关）；
- 出站 URL 钉死 https + api.github.com + 拒绝 IP 字面量；连接层拒绝
  非公网解析结果与漂移出白名单的目标（SSRF 红线，与 ssrf.py 同策略）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def adv() -> ModuleType:
    return _load("release_advisory", REPO_ROOT / "scripts" / "release_advisory.py")


def _release(version: str, body: str = "", **flags: Any) -> dict[str, Any]:
    return {
        "tag_name": version,
        "name": version,
        "prerelease": bool(flags.get("prerelease", False)),
        "draft": bool(flags.get("draft", False)),
        "published_at": "2026-09-07T16:52:41Z",
        "body": body,
    }


BODY_136 = """## 💥 破坏性变更

- feat(generate\\_standalone): 移除独立版本中的渲染模块 @molanp (#290)

## 🚀 新功能

- feat(bilibili): 增强PCDN链接过滤功能 @molanp (#300)

## 🐛 Bug 修复

- fix(bilibili/quality): 修复哔哩视频质量配置不生效 @molanp (#299)
"""

RELEASES = [
    _release("1.3.6", BODY_136),
    _release("1.3.5", "## 🚀 新功能\n\n- feat(miyoushe): UGC (#281)\n"),
    _release("1.3.4", "## 💥 破坏性变更\n\n- 统一下载函数协议 (#261)\n"),
    _release("1.3.7", "## 💥 破坏性变更\n\n- 示例 breaking (#310)\n"),
    _release("1.3.2", "misc body", prerelease=True),
    _release("1.3.1", "draft body", draft=True),
]


# ---- 版本序 ----


def test_version_key_semver_order(adv: ModuleType) -> None:
    k = adv._version_key
    assert k("1.3.6") < k("1.3.7-pre-release.1")
    assert k("1.3.7-pre-release.3") < k("1.3.7-pre-release.4")
    assert k("1.3.7-pre-release.10") < k("1.3.7-stable")
    assert k("1.3.7-pre-release.1") < k("1.3.7")  # 预发布 < 同号 stable
    assert k("1.9.0") < k("1.10.0")  # 数字段按数值不按字典


# ---- 跨越区间 ----


def test_crossed_is_exclusive_lower_inclusive_upper(adv: ModuleType) -> None:
    crossed = adv.crossed_releases(RELEASES, "1.3.4", "1.3.6")
    assert [r["_version"] for r in crossed] == ["1.3.5", "1.3.6"]


def test_prerelease_only_roll_has_no_crossing(adv: ModuleType) -> None:
    assert adv.crossed_releases(RELEASES, "1.3.7-pre-release.3", "1.3.7-pre-release.4") == []


def test_crossing_into_stable_surfaces_its_notes(adv: ModuleType) -> None:
    crossed = adv.crossed_releases(RELEASES, "1.3.7-pre-release.4", "1.3.7")
    assert [r["_version"] for r in crossed] == ["1.3.7"]


def test_prerelease_draft_never_crossed(adv: ModuleType) -> None:
    releases = [_release("1.3.2", "pre", prerelease=True), _release("1.3.3", "draft", draft=True)]
    assert adv.crossed_releases(releases, "1.3.1", "1.3.3") == []


def test_v_prefix_tag_tolerated(adv: ModuleType) -> None:
    releases = [_release("v1.3.6", BODY_136)]
    crossed = adv.crossed_releases(releases, "1.3.5", "1.3.6")
    assert [r["_version"] for r in crossed] == ["1.3.6"]


# ---- 节解析与渲染 ----


def test_parse_sections_cuts_by_emoji_header(adv: ModuleType) -> None:
    sections = adv.parse_sections(BODY_136)
    assert set(sections) == {"💥 破坏性变更", "🚀 新功能", "🐛 Bug 修复"}
    assert "#290" in sections["💥 破坏性变更"]


def test_render_surfaces_breaking_and_feature_not_bugfix(adv: ModuleType) -> None:
    crossed = adv.crossed_releases(RELEASES, "1.3.5", "1.3.6")
    text = adv.render_advisory(crossed)
    assert "💥 破坏性变更" in text
    assert "需人工确认桥接面影响" in text  # 工作流 automerge 分层依赖此标记
    assert "🚀 新功能" in text
    assert "Bug 修复" not in text


def test_render_empty_for_zero_crossing(adv: ModuleType) -> None:
    assert adv.render_advisory([]) == ""


# ---- 出站钉扎（SSRF 红线）----


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com/repos/x/y/releases",
        "https://evil.example.com/repos/x/y/releases",
        "https://127.0.0.1/repos/x/y/releases",
        "https://[::1]/repos/x/y/releases",
    ],
)
def test_url_validator_rejects(adv: ModuleType, url: str) -> None:
    with pytest.raises(ValueError):
        adv._validate_releases_url(url)


def test_url_validator_accepts_constant(adv: ModuleType) -> None:
    assert adv._validate_releases_url(adv.RELEASES_URL) == adv.RELEASES_URL


def test_pinned_connect_rejects_nonpublic_resolution(
    adv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_getaddrinfo(*args: Any, **kw: Any) -> list[Any]:
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(adv.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="非公网地址"):
        adv._pinned_connect("api.github.com", 443, 15.0, {"127.0.0.1"})


def test_pinned_connect_rejects_drift_out_of_allowlist(
    adv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单外的公网 IP（DNS 重绑定后漂移）连接前即拒，不发生任何 connect。"""

    def fake_getaddrinfo(*args: Any, **kw: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(adv.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="漂移出解析白名单"):
        adv._pinned_connect("api.github.com", 443, 15.0, {"198.51.100.7"})


# ---- CLI ----


def test_main_failure_is_loud_and_returns_2(
    adv: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: Any, **kw: Any) -> list[dict[str, Any]]:
        raise RuntimeError("network down")

    monkeypatch.setattr(adv, "fetch_releases", boom)
    assert adv.main(["--old", "1.3.5", "--new", "1.3.6"]) == 2
    assert "release advisory 抓取失败" in capsys.readouterr().err


def test_main_success_prints_crossed_advisory(
    adv: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(adv, "fetch_releases", lambda *a, **k: RELEASES)
    assert adv.main(["--old", "1.3.5", "--new", "1.3.6"]) == 0
    out = capsys.readouterr().out
    assert "v1.3.6" in out and "需人工确认桥接面影响" in out
