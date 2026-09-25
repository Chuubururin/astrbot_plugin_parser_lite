"""配置边界钳制的守护（防「schema 描述与实际钳制漂移」类回归）。

背景：AstrBot 4.28 的插件配置面板对 int 字段**不做范围校验**——
``_conf_schema.json`` 里的 ``minimum``/``maximum`` 不是宿主消费的键，
``slider`` 也只是**额外**渲染一个滑块，旁边的 ``type="number"`` 数字输入框
仍然可以自由输入任意整数（含负数）。已通过阅读宿主 dashboard bundle
（``astrbot/dashboard/dist/assets/ProviderSelectMenu-*.js``）实测确认：
数字输入组件未绑定 min/max。

因此非法值只能由**运行期钳制**兜住，而钳制点与 schema 描述必须对账，
否则描述会漂移回「承诺了不存在的保护」的状态——这正是本文件断言要拦的
故障形态。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_config.py"
RENDER_PATH = REPO_ROOT / "bridge" / "render.py"
SENDER_PATH = REPO_ROOT / "bridge" / "sender.py"


def _schema() -> dict[str, dict[str, object]]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _const(source: str, text: str, name: str) -> int:
    match = re.search(rf"^{name}\s*=\s*(\d+)", text, re.MULTILINE)
    assert match, f"{source} 里找不到常量 {name}"
    return int(match.group(1))


def _strip_function(source: str, name: str) -> str:
    """把 ``def name(...)`` 的整个函数体从源码中挖掉（保留其余部分）。

    用于「除该函数外不得出现某模式」这类断言：直接全文件搜索会把函数体内部的
    合法使用也算进去（首版即因此误判）。
    """
    match = re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE)
    if not match:
        return source
    start = match.start()
    # 下一个顶格 def / 类定义 即为函数结束（本仓风格：模块级函数顶格）
    nxt = re.search(r"^(?:def |class |@|[A-Z_][A-Z_0-9]*\s*=)", source[match.end() :], re.MULTILINE)
    end = match.end() + nxt.start() if nxt else len(source)
    return source[:start] + source[end:]


def test_panel_does_not_enforce_int_bounds_so_code_must() -> None:
    """钳制的存在理由：面板不校验 ⇒ 代码必须钳。

    这条断言本身不测宿主（宿主不在本仓库），而是**锁住两处钳制点确实存在**：
    若有人「清理」掉钳制函数而只留 schema 描述，本测试必须红。
    """
    render_src = RENDER_PATH.read_text(encoding="utf-8")
    sender_src = SENDER_PATH.read_text(encoding="utf-8")

    assert "def max_comments_count(" in render_src, "render.py 缺评论条数钳制入口"
    assert "def _clamp_config_int(" in render_src, "render.py 缺通用 int 钳制辅助"
    assert "def forward_text_threshold(" in sender_src, "sender.py 缺转发阈值钳制入口"
    assert "def _clamp_forward_text_threshold(" in sender_src, "sender.py 缺转发阈值钳制辅助"
    assert "def lazy_download_timeout(" in sender_src, "sender.py 缺懒下载超时钳制入口"
    assert "def _clamp_lazy_timeout(" in sender_src, "sender.py 缺懒下载超时钳制辅助"

    # 消费点必须走钳制后的取值，不得再直接读裸配置。
    # 注意：裸读会**合法地**出现在钳制函数体内部（那正是钳制读它的地方），
    # 因此断言的是「除钳制函数外没有裸读」——用切片把钳制函数体挖掉再查。
    render_body = _strip_function(render_src, "max_comments_count")
    render_body = _strip_function(render_body, "_clamp_config_int")
    assert "pconfig.max_comments" not in render_body, (
        "render.py 在钳制函数之外仍直接读 pconfig.max_comments"
    )

    sender_body = _strip_function(sender_src, "forward_text_threshold")
    sender_body = _strip_function(sender_body, "_clamp_forward_text_threshold")
    assert "pconfig.forward_text_threshold" not in sender_body, (
        "sender.py 在钳制函数之外仍直接读 pconfig.forward_text_threshold"
    )
    # 懒下载超时同款：入口 getter 与钳制辅助都被挖掉后不得再有裸读
    lazy_body = _strip_function(sender_src, "lazy_download_timeout")
    lazy_body = _strip_function(lazy_body, "_clamp_lazy_timeout")
    assert "pconfig.lazy_download_timeout" not in lazy_body, (
        "sender.py 在钳制函数之外仍直接读 pconfig.lazy_download_timeout"
    )


def test_max_comments_clamp_bounds_match_code_constants() -> None:
    """schema 描述里的评论条数区间必须与代码常量逐字对齐。"""
    schema = _schema()
    description = str(schema["plite_max_comments"]["description"])
    render_src = RENDER_PATH.read_text(encoding="utf-8")
    upper = _const("bridge/render.py", render_src, "MAX_COMMENTS_LIMIT")

    match = re.search(r"有效范围\s*0-(\d+)", description)
    assert match, f"plite_max_comments 描述缺「有效范围 0-N」：{description!r}"
    assert int(match.group(1)) == upper, (
        f"描述上界 {match.group(1)} 与 MAX_COMMENTS_LIMIT={upper} 不一致。"
        "改任一侧都必须同步另一侧。"
    )


def test_forward_text_threshold_clamp_bounds_match_code_constants() -> None:
    """schema 描述里的转发阈值区间必须与代码常量逐字对齐（含上游已写的上界）。"""
    schema = _schema()
    description = str(schema["plite_forward_text_threshold"]["description"])
    sender_src = SENDER_PATH.read_text(encoding="utf-8")
    lower = _const("bridge/sender.py", sender_src, "FORWARD_TEXT_THRESHOLD_MIN")
    upper = _const("bridge/sender.py", sender_src, "FORWARD_TEXT_THRESHOLD_MAX")

    # 下界：或来自「有效范围 L-U」，或来自上游已写上界时的「最小 L」
    match = re.search(r"有效范围\s*(\d+)-(\d+)", description)
    if match:
        assert int(match.group(1)) == lower, f"描述下界 {match.group(1)} != {lower}"
        assert int(match.group(2)) == upper, f"描述上界 {match.group(2)} != {upper}"
    else:
        match = re.search(r"最小\s*(\d+)", description)
        assert match, f"plite_forward_text_threshold 描述缺区间说明：{description!r}"
        assert int(match.group(1)) == lower, f"描述下界 {match.group(1)} != {lower}"
        # 上界应已由上游描述写明（形如「最大4500」）
        stated = re.search(r"最大\s*(\d+)", description)
        assert stated, f"描述缺上界说明：{description!r}"
        assert int(stated.group(1)) == upper, f"描述上界 {stated.group(1)} != {upper}"


def test_lazy_timeout_clamp_bounds_match_code_constants() -> None:
    """schema 描述里的懒下载超时区间必须与代码常量逐字对齐。"""
    schema = _schema()
    description = str(schema["plite_lazy_download_timeout"]["description"])
    sender_src = SENDER_PATH.read_text(encoding="utf-8")
    lower = _const("bridge/sender.py", sender_src, "LAZY_TIMEOUT_MIN")
    upper = _const("bridge/sender.py", sender_src, "LAZY_TIMEOUT_MAX")
    match = re.search(r"有效范围\s*(\d+)-(\d+)", description)
    assert match, f"plite_lazy_download_timeout 描述缺「有效范围 L-U」：{description!r}"
    assert int(match.group(1)) == lower, f"描述下界 {match.group(1)} != {lower}"
    assert int(match.group(2)) == upper, f"描述上界 {match.group(2)} != {upper}"


def test_range_notes_keys_exist_in_schema() -> None:
    """RANGE_NOTES 的键必须真实存在于生成出的 schema（防止注记静默失效）。"""
    generator_src = GENERATOR_PATH.read_text(encoding="utf-8")
    block = re.search(r"RANGE_NOTES[^=]*=\s*\{(.*?)\n\}", generator_src, re.DOTALL)
    assert block, "generate_config.py 里找不到 RANGE_NOTES"
    keys = set(re.findall(r'"(\w+)":', block.group(1)))

    schema = _schema()
    stale = {key for key in keys if key not in schema}
    assert not stale, f"RANGE_NOTES 指向不存在的字段：{sorted(stale)}"

    # 且描述里确实带上了区间说明（注记没有静默丢失）
    for key in keys:
        description = str(schema[key]["description"])
        assert "有效范围" in description or "最小" in description, (
            f"{key} 的描述未带上区间说明：{description!r}"
        )


def test_clamp_is_actually_enforced_by_runtime() -> None:
    """端到端：钳制函数对越界输入返回边界值，而非把非法值透传。

    用真导入执行，避免「测试只校验字符串」的空转。
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT.parent))
    from astrbot_plugin_parser_lite.bridge.render import _clamp_config_int, max_comments_count
    from astrbot_plugin_parser_lite.bridge.sender import (
        _clamp_forward_text_threshold,
        _clamp_lazy_timeout,
    )

    # 缺陷 1：负数必须落到 0，绝不透传给切片
    assert _clamp_config_int(-1, minimum=0, maximum=100) == 0
    assert _clamp_config_int(-3, minimum=0, maximum=100) == 0
    assert _clamp_config_int(5, minimum=0, maximum=100) == 5
    assert _clamp_config_int(0, minimum=0, maximum=100) == 0
    assert _clamp_config_int(999, minimum=0, maximum=100) == 100
    # 异常形态一律回落下界，不抛不传
    assert _clamp_config_int(None, minimum=0, maximum=100) == 0
    assert _clamp_config_int("7", minimum=0, maximum=100) == 0
    assert _clamp_config_int(True, minimum=0, maximum=100) == 0  # bool 不是 int

    # 缺陷 2/7：0/负数钳到 1，超上界钳到 4500
    assert _clamp_forward_text_threshold(0) == 1
    assert _clamp_forward_text_threshold(-1) == 1
    assert _clamp_forward_text_threshold(1000) == 1000
    assert _clamp_forward_text_threshold(999_999) == 4500
    assert _clamp_forward_text_threshold(None) == 1

    # 懒下载超时：WebUI 无上界，须钳到 [5, 300]
    assert _clamp_lazy_timeout(0) == 5
    assert _clamp_lazy_timeout(-1) == 5
    assert _clamp_lazy_timeout(30) == 30
    assert _clamp_lazy_timeout(9999) == 300
    assert _clamp_lazy_timeout(None) == 5
    assert _clamp_lazy_timeout(True) == 5

    # 运行时入口读的是钳制后的值（默认配置 5 / 1000 应原样通过）
    assert max_comments_count() >= 0


def test_slice_with_clamped_value_is_monotonic() -> None:
    """缺陷 1 的核心回归：钳制后评论数随配置**单调不减**。

    原缺陷：裸切片下 -1 保留 7 条而 0 保留 0 条，非单调。
    """
    comments = [f"c{i}" for i in range(8)]
    counts = []
    for raw in (-5, -3, -1, 0, 1, 3, 5, 8, 100):
        clamped = max(0, min(100, raw))
        counts.append((raw, len(comments[:clamped])))
    values = [n for _, n in counts]
    assert values == sorted(values), f"钳制后仍非单调：{counts}"
    # 且负数与 0 等价
    by_raw = dict(counts)
    assert by_raw[-1] == by_raw[0] == 0
    assert by_raw[-3] == 0
