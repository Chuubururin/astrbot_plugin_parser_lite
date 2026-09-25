"""JSON 卡片 URL 抽取（``main._urls_from_json``）的边界回归。

任何嵌套深度的 ``jump_url`` 都必须被抽出——带深度上限的截断会让深层容器内
的链接被静默丢弃（用户侧表现为「发了卡片没反应」）；而单纯抬高上限又会把
病态深层输入变成 ``RecursionError``。因此实现为显式栈遍历
（先序，与递归逐项同序，无深度上限）。

main.py 依赖 astrbot.api（见 tests/requirements-test.txt，CI 装真实 AstrBot）。
本模块只做纯函数行为断言，不触网络。
"""

from __future__ import annotations

import pytest

pytest.importorskip("astrbot.api")

from astrbot_plugin_parser_lite import main as main_mod


def _nested(depth: int, leaf: object) -> object:
    """把 leaf 包进 depth 层 ``{"child": ...}``；depth=0 时原样返回。

    root 位于第 0 层，leaf 位于第 depth 层。
    """
    node: object = leaf
    for _ in range(depth):
        node = {"child": node}
    return node


def test_deep_card_link_is_not_dropped() -> None:
    """第 9 层容器内的链接不得被任何深度截断丢弃。"""
    data = _nested(9, {"jump_url": "https://b23.tv/AbCdEf"})
    assert main_mod._urls_from_json(data) == ["https://b23.tv/AbCdEf"]


def test_pathological_depth_does_not_recurse() -> None:
    """病态深层嵌套不得触发 ``RecursionError``（显式栈无深度上限）。"""
    data = _nested(3000, {"jump_url": "https://b23.tv/AbCdEf"})
    assert main_mod._urls_from_json(data) == ["https://b23.tv/AbCdEf"]


def test_preorder_order_matches_recursive_implementation() -> None:
    """遍历顺序 = URL 优先级，必须与递归先序一致（先 dict 插入序，子树先）。"""
    data = {"a": {"c": "https://x.com/1"}, "b": "https://x.com/2"}
    assert main_mod._urls_from_json(data) == ["https://x.com/1", "https://x.com/2"]


def test_known_key_raw_and_other_strings_through_regex() -> None:
    """已知协议字段取原值；其余字符串叶子走正则兜底。"""
    data = {
        "jump_url": "https://b23.tv/AbCdEf",
        "desc": "\u770b\u8fd9\u4e2a https://x.com/9 \u5c3e\u5df4",
    }
    assert main_mod._urls_from_json(data) == [
        "https://b23.tv/AbCdEf",
        "https://x.com/9",
    ]


def test_known_key_without_http_scheme_is_not_emitted() -> None:
    """已知字段值不是 http(s) 时按普通字符串走正则（不裸发非 URL 值）。"""
    assert main_mod._urls_from_json({"jump_url": "not-a-url"}) == []


def test_bare_string_in_list_is_not_a_candidate() -> None:
    """契约：字符串仅在 dict 值位置被抽取（与递归实现一致，勿「顺手修掉」）。"""
    assert main_mod._urls_from_json({"a": ["https://x.com/1"]}) == []


def test_known_key_inside_list_of_dicts_is_found() -> None:
    assert main_mod._urls_from_json({"a": [{"jump_url": "https://x.com/1"}]}) == ["https://x.com/1"]


def test_non_container_inputs_yield_nothing() -> None:
    assert main_mod._urls_from_json({}) == []
    assert main_mod._urls_from_json("https://x.com/1") == []
    assert main_mod._urls_from_json(None) == []
    assert main_mod._urls_from_json(42) == []


# ---- 广度与混合形态 ----


def test_wide_fanout_is_fully_collected() -> None:
    """大扇出卡片（数千子项）不得丢项——显式栈遍历无递归/截断上限。"""
    data = {"items": [{"jump_url": f"https://x.com/{i}"} for i in range(5000)]}
    urls = main_mod._urls_from_json(data)
    assert len(urls) == 5000
    assert urls[0] == "https://x.com/0"
    assert urls[-1] == "https://x.com/4999"


def test_non_string_leaves_are_ignored() -> None:
    """int/float/bool/None 叶子安全跳过，不影响同层容器继续抽取。"""
    data = {"a": 1, "b": None, "c": True, "d": 3.5, "e": {"jump_url": "https://x.com/1"}}
    assert main_mod._urls_from_json(data) == ["https://x.com/1"]


def test_mixed_nested_containers_keep_preorder() -> None:
    """dict/list 混合嵌套保持先序；list 内裸字符串仍不是候选（契约不漂移）。"""
    data = {
        "list": [
            {"jump_url": "https://x.com/1"},
            ["deep", {"url": "https://x.com/2"}],
        ],
        "tail": "见 https://x.com/3",
    }
    assert main_mod._urls_from_json(data) == [
        "https://x.com/1",
        "https://x.com/2",
        "https://x.com/3",
    ]
