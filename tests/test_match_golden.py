"""行为轨：match() 离线黄金测试（工单 09 ④）。

正负样本均经真实 vendor 正则逐一验证（2026-09 快照）；上游滚动后此处变红
即平台正则行为漂移信号，人工评审后更新黄金表。

覆盖度（2026-09-17 评审 M12 补齐）：样本键集合 == ``parsers.load_all()`` 注册的
全部可命中平台（当前 27 个），由文件末的元测试机械对齐——此前只覆盖 14/29，
过半平台的正则漂移无信号。样本一律从各 parser 的 ``@handle(keyword, pattern,
params)`` 反推并逐条经 ``Parser().match()`` 实测确认命中；params 型平台的必需
query 在条目上方注明。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Parser, configure
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.exception import (
    ParseException,
)

# 平台名 → 已验证可命中的 URL 样本（离线正则匹配，不触网）
GOLDEN_MATCHES: dict[str, tuple[str, ...]] = {
    "bilibili": (
        "https://b23.tv/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1GJ411x7h7/?p=1",
        "https://www.bilibili.com/video/av170001",
        "https://b23.tv/OP0Vh7p",
        "https://www.bilibili.com/opus/930258750048665608",
        "https://www.bilibili.com/dynamic/930258750048665608",
    ),
    "douyin": (
        "https://v.douyin.com/iRNBho6/",
        "https://www.douyin.com/video/7301234567890123456",
        "https://www.douyin.com/note/7301234567890123456",
    ),
    "weibo": (
        "https://m.weibo.cn/status/5000000000000000",
        "https://weibo.com/1234567890/abcdefg",
    ),
    "x": (
        "https://x.com/user/status/1234567890123456789",
        "https://twitter.com/user/status/1234567890123456789",
    ),
    "netease": (
        "https://music.163.com/song?id=1901371647",
        "https://y.music.163.com/m/song/1901371647",
        "https://share.music.163.com/#/song?id=1901371647",
    ),
    "zhihu": ("https://www.zhihu.com/question/123456789",),
    "tieba": ("https://tieba.baidu.com/p/1234567890",),
    "rednote": ("https://xhslink.com/abcdef",),
    "kuaishou": (
        "https://v.kuaishou.com/abcdef",
        "https://www.kuaishou.com/short-video/3xabcdef",
    ),
    "acfun": ("https://www.acfun.cn/v/ac12345678",),
    "hupu": ("https://bbs.hupu.com/12345678.html",),
    "miyoushe": (
        "https://www.miyoushe.com/ys/article/12345678",
        "https://miyoushe.com/ys/article/12345678",
    ),
    "coolapk": ("https://www.coolapk.com/feed/12345678",),
    "linuxdo": ("https://linux.do/t/topic/123456",),
    # --- 2026-09-17 评审 M12 补齐：此前只覆盖 14/29 平台 ---
    "kugou": (
        "https://t1.kugou.com/1abcDEF",
        "https://www.kugou.com/mixsong/abcdef.html",
    ),
    "qsmusic": ("https://qishui.douyin.com/s/abcdef/",),
    "kuwo": ("https://www.kuwo.cn/play_detail/12345678",),
    # params 型：必需 query id（as_int）
    "duitang": (
        "https://www.duitang.com/blog?id=123456",
        "https://www.duitang.com/atlas?id=123456",
    ),
    "heybox": (
        "https://www.xiaoheihe.cn/bbs/post_share?link_id=abc123",
        "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=abcdef",
    ),
    "lofter": (
        "https://www.lofter.com/post/abc123_def456",
        "https://s.lofter.com/-s/abcDEF",
    ),
    # params 型：comment_type 必须等于该分支固定值；其余 id 必填。
    # 样本取自 vendor 内 @handle 上方的原始注释 URL。
    "buff": (
        "https://buff.163.com/s/topic-detail_share.html"
        "?social_topic_post_id=P1093043595&comment_type=239",
        "https://buff.163.com/s/news-detail_share.html?article_id=87832&comment_type=228",
        "https://buff.163.com/s/preview_share.html"
        "?game=csgo&preview_id=V1092280822&comment_type=216",
    ),
    "illu": ("https://illund.com/share.html?articleId%3Dabc123",),
    "douban": ("https://www.douban.com/group/topic/12345678/",),
    "5eplay": ("https://csgo.5eplay.com/forum/123456",),
    # params 型：share_id 与 video_id 均为必需
    "doubao": ("https://www.doubao.com/video-sharing?share_id=abc123&video_id=123",),
    # params 型：id / gameTypeStr 均 as_int
    "wmpvp": (
        "https://news.wmpvp.com/news.html?id=301077&gameTypeStr=2",
        "https://news.wmpvp.com/community-detail.html?id=12345",
    ),
    "zlb": ("https://zlb.ink/topic/12345",),
}

# 已验证「不命中任何平台」的样本（含曾误猜过的形态，防回归放行）
GOLDEN_NEGATIVES: tuple[str, ...] = (
    "hello world",
    "https://example.com/nothing",
    "https://www.zhihu.com/answer/123456789",
    "https://weibo.com/tv/show/1034:1234567",
    "https://www.kugou.com/song/#hash=abcdef",
    "https://www.xiaohongshu.com/explore/abc123",
    "https://www.douban.com/people/somebody/status/1234567",
    "https://www.duitang.com/note/123456/",
    "https://www.doubao.com/share/note/123456",
)


def _platform_of(result) -> str:
    return result.parser_type.platform.name.value


@pytest.fixture
def parser() -> Parser:
    # Parser._types() 在首次 match 时快照禁用清单，改配置的用例须用全新实例
    return Parser()


@pytest.mark.parametrize(
    ("expected_platform", "url"),
    [(platform, url) for platform, urls in GOLDEN_MATCHES.items() for url in urls],
    ids=str,
)
def test_golden_match(parser: Parser, expected_platform: str, url: str):
    assert _platform_of(parser.match(url)) == expected_platform


@pytest.mark.parametrize("text", GOLDEN_NEGATIVES)
def test_golden_negative(parser: Parser, text: str):
    with pytest.raises(ParseException):
        parser.match(text)


def test_disabled_platform_excluded():
    configure(plite_disabled_platforms=["bilibili"])
    try:
        with pytest.raises(ParseException):
            Parser().match("https://b23.tv/BV1xx411c7mD")
    finally:
        configure(plite_disabled_platforms=[])
    assert _platform_of(Parser().match("https://b23.tv/BV1xx411c7mD")) == "bilibili"


def test_empty_text_rejected(parser: Parser):
    with pytest.raises(ParseException):
        parser.match("   ")


# ---------------------------------------------------------------- 元测试

# PlatformEnum 中存在、但 parsers/__init__.py::load_all() 尚未注册 parser 的成员。
# 上游补齐注册后本常量与断言会自然变红，提示同步收紧。
KNOWN_UNREGISTERED_ENUM_MEMBERS = frozenset({"ds", "taptap"})


def _matchable_platforms() -> set[str]:
    """真实可命中的平台集合（``parsers.load_all()`` 注册的全部 parser）。"""
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import parsers
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.base import (
        BaseParser,
    )

    parsers.load_all()
    return {t.platform.name.value for t in BaseParser.get_all_subclass()}


def _schema_platform_options() -> set[str]:
    """WebUI 的 ``plite_disabled_platforms`` 选项集合（由 PlatformEnum 派生）。"""
    schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def find(node: object) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "plite_disabled_platforms":
                    return value
                found = find(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = find(item)
                if found is not None:
                    return found
        return None

    node = find(schema)
    if isinstance(node, dict):
        return {str(option) for option in (node.get("options") or [])}
    if isinstance(node, list):
        return {str(option) for option in node}
    return set()


def test_golden_covers_every_matchable_platform() -> None:
    """元测试（M12）：黄金样本键集合 == 实际可命中的平台集合。

    成本极低、收益极高：上游新增平台却未补样本时立即变红，平台覆盖随上游
    自动对齐。没有这条时「黄金样本守护平台正则漂移」只是一半真的——
    2026-09-17 评审实测 14/29 平台有样本，其余 15 个平台的正则漂移完全无信号。
    """
    assert set(GOLDEN_MATCHES) == _matchable_platforms()


def test_schema_platform_options_track_matchable_parsers() -> None:
    """元测试：schema 平台选项与可命中 parser 的差集必须恰为已知未注册枚举成员。

    精确钉扎（而非「超集」）使双向漂移都可见：上游新增未注册枚举成员、
    或补齐 ds/taptap 的注册，都会让本测试变红并要求人工复核。
    """
    assert _schema_platform_options() - _matchable_platforms() == KNOWN_UNREGISTERED_ENUM_MEMBERS
