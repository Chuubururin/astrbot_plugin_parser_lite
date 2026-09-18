"""入口 URL 抽取的尾字符裁剪：分享文案粘连标点的回归。

生产文案（QQ 转发、平台分享卡）常把链接与标点直接相连，正则排除集只列
独占性强的 CJK 标点，抽取结果因此可能带尾随字符（句点、全角括号、逗号、
右括号）。带脏字符的 URL 交给上游 ``match`` 会不匹配或把脏字符带进解析
目标——用户侧表现为「发了链接没反应」（2026-09-14 双轴评审实测：尾随
句点的小红书链接完全解析失败）。

``main.py`` 依赖 ``astrbot.api``，宿主测试环境不可导入；且不 exec 源码
（动态执行属代码注入面，Mimosa 拦截）。本用例以 ``ast.literal_eval``
读取裁剪常量（仅字面量），驱动一份本地复刻的裁剪逻辑做行为断言；复刻与
真实实现的同步由函数体归一化 AST 指纹钉扎——归一化只豁免函数名、
docstring 与两个常量引用名，字面量值与控制流/操作符形态逐字比对，复刻侧
算法漂移此处先红。另以源码契约断言接线。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SRC = (REPO_ROOT / "main.py").read_text(encoding="utf-8")


def _raw_assign(name: str) -> str:
    """取 ``NAME = <表达式>`` 右侧原文（供 literal_eval 用）。"""
    prefix = f"{name} = "
    for line in MAIN_SRC.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"main.py 中未找到 {name} 定义")


TRAILING = ast.literal_eval(_raw_assign("_URL_TRAILING_STRIP"))
assert isinstance(TRAILING, str), "_URL_TRAILING_STRIP 应为字符串字面量"
BRACKET_PAIRS = ast.literal_eval(_raw_assign("_URL_BRACKET_PAIRS"))
assert isinstance(BRACKET_PAIRS, tuple), "_URL_BRACKET_PAIRS 应为元组字面量"


def strip_trailing(url: str) -> str:
    """本地复刻 ``main.main._strip_url_trailing``（常量取自源码真值，指纹钉扎）。"""
    while url:
        stripped = url
        for opener, closer in BRACKET_PAIRS:
            while stripped.endswith(closer) and stripped.count(opener) < stripped.count(closer):
                stripped = stripped[:-1]
        stripped = stripped.rstrip(TRAILING)
        if stripped == url:
            break
        url = stripped
    return url


def _normalized_ast_dump(source: str) -> str:
    """归一化 AST 指纹：仅豁免三类已知差异，其余逐字参与比对。

    豁免面：函数名（统一 FUNC）、函数 docstring（置空）、两个常量的引用名
    （_URL_TRAILING_STRIP/_URL_BRACKET_PAIRS → 复刻命名）。字面量值不豁免
    ——切片索引等常量级漂移同样会使指纹变红。
    """

    class _FuncNormalizer(ast.NodeTransformer):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
            node.name = "FUNC"
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body[0].value = ast.Constant(value=None)
            self.generic_visit(node)
            return node

    class _NameNormalizer(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.Name:
            if node.id in {"_URL_TRAILING_STRIP", "TRAILING"}:
                node.id = "TRAILING"
            elif node.id in {"_URL_BRACKET_PAIRS", "BRACKET_PAIRS"}:
                node.id = "BRACKET_PAIRS"
            return node

    normalized = _NameNormalizer().visit(_FuncNormalizer().visit(ast.parse(source)))
    return ast.dump(normalized)


def _main_function_source() -> str:
    """用 AST 取 main.py 中 _strip_url_trailing 的函数定义原文。"""
    tree = ast.parse(MAIN_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_strip_url_trailing":
            return ast.unparse(node)
    raise AssertionError("main.py 中未找到 _strip_url_trailing 定义")


def _test_function_source() -> str:
    """本文件中 strip_trailing 复刻的函数定义原文。"""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "strip_trailing":
            return ast.unparse(node)
    raise AssertionError("test 中未找到 strip_trailing 复刻")


def _pattern_source() -> str:
    """用 AST 取 URL_PATTERN 的首个参数（模式字面量），不执行任何代码。"""
    expr = ast.parse(_raw_assign("URL_PATTERN"), mode="eval")
    call = expr.body
    assert isinstance(call, ast.Call), "URL_PATTERN 应为 re.compile(...) 调用"
    first = call.args[0]
    assert isinstance(first, ast.Constant) and isinstance(first.value, str)
    return first.value


PATTERN = re.compile(_pattern_source(), re.IGNORECASE)


def extract(text: str) -> list[str]:
    """复刻 ``_extract_url`` 的正则 + 裁剪两步（不含 vendor match）。"""
    return [u for raw in PATTERN.findall(text) if (u := strip_trailing(raw))]


# ---- 尾随标点 ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "看看这个 https://www.xiaohongshu.com/explore/abc123.",
            "https://www.xiaohongshu.com/explore/abc123",
        ),
        ("https://b23.tv/AbCdEf.", "https://b23.tv/AbCdEf"),
        ("在 https://b23.tv/AbCdEf, 里面", "https://b23.tv/AbCdEf"),
        ("【标题】 https://b23.tv/AbCdEf（分享了视频）", "https://b23.tv/AbCdEf"),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD)",
            "https://www.bilibili.com/video/BV1xx411c7mD",
        ),
        ("链接：https://xhslink.cn/o/abc123。", "https://xhslink.cn/o/abc123"),
        ("https://b23.tv/AbCdEf！", "https://b23.tv/AbCdEf"),
        ("(见 https://b23.tv/AbCdEf)", "https://b23.tv/AbCdEf"),
    ],
)
def test_trailing_punctuation_is_stripped(text: str, expected: str) -> None:
    assert extract(text) == [expected]


# ---- 合法 URL 不得被破坏 ----


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://example.com/a[b]",
        "https://example.com/path?q=1&r=2",
        "https://example.com/path#frag",
    ],
)
def test_legit_urls_are_preserved(url: str) -> None:
    """括号配对时保留右括号；查询串、片段等路径字符不受影响。"""
    assert extract(url) == [url]


def test_query_with_trailing_dot_keeps_query() -> None:
    got = extract("见 https://example.com/p?q=1&r=2.")
    assert got == ["https://example.com/p?q=1&r=2"]


def test_strip_is_idempotent() -> None:
    once = strip_trailing("https://b23.tv/AbCdEf.)")
    assert strip_trailing(once) == once == "https://b23.tv/AbCdEf"


# ---- 接线契约 ----


def test_extraction_point_consumes_helper() -> None:
    """抽取循环必须调用裁剪函数（防回退到裸 URL）。"""
    assert "_strip_url_trailing(raw)" in MAIN_SRC, "_extract_url 未对正则结果做尾字符裁剪"


def test_replica_matches_real_implementation() -> None:
    """复刻与真实实现的同步钉扎：函数体归一化 AST 指纹一致。

    归一化仅豁免函数名、docstring 与两个常量的引用名（_URL_TRAILING_STRIP/
    _URL_BRACKET_PAIRS → TRAILING/BRACKET_PAIRS）；字面量值（切片索引等）
    与控制流、操作符形态逐字比对——复刻侧常量级/操作符级/结构漂移都会变红。
    """
    main_dump = _normalized_ast_dump(_main_function_source())
    test_dump = _normalized_ast_dump(_test_function_source())
    assert main_dump == test_dump, (
        "strip_trailing 复刻与 main._strip_url_trailing 结构漂移，请同步复刻"
    )


def test_helper_defined_in_main() -> None:
    assert "def _strip_url_trailing(" in MAIN_SRC, "裁剪辅助函数缺失"
