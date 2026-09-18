"""上游 main 分支显示文本提取 → vendor/_upstream/display_texts.json。

三层文本注入的最前端：桥内全部面向用户的运行时文案（渲染降级、懒下载问询、
合并转发文本族、投票格式串、下载失败提示等）逐字来自上游 main 分支
render/matchers/exception 模块；
standalone 快照剥离了发送层，因此提取源是 main 分支本体（vendor 单一权威
上游的另一条数据面）。本脚本只在 sync-upstream 工作流中运行（那里已克隆
上游仓库）；产物 JSON **入库**（与 vendor/_upstream/pyproject.toml 同类的
上游元数据快照），使 analyze_vendor → generate_config 两层注入在本地与
CI 均可离线再生。

提取规则（唯一的锚点规则表，属桥规则层——值是上游数据，锚点指向哪个值是
桥的语义知识）：

- AST 遍历上游模块，收集字符串常量与 f-string 模板（FormattedValue 归一为
  ``{0}``/``{1}`` 占位，含 ``!conv``/``:spec``），排除 docstring 与 logger.*
  调用子树（日志不是用户可见文案）；
- 每个锚点（equals/contains）必须恰好命中一个候选：零命中说明上游改写了
  该文案位置，多命中说明锚点失去区分度——都响亮失败，让 sync PR 变红交由
  人工复核，绝不静默携带错误文案。

jinja 模板文本（卡面「评论/多选」等）单独提取为 template_texts 全集，供
契约测试断言桥内模板文案是上游子集（上游漂移 → sync PR 变红）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_ROOT / "vendor" / "_upstream" / "display_texts.json"
PROVENANCE = "scripts/extract_display_texts.py 生成（上游 main 显示文本提取，勿手改）"

# 平级共享底座（git 只读管道 + 产物字节公式）：scripts/ 非包，直接运行与
# importlib 按路径加载两种形态都靠 sys.path 垫片导入
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pipeline_common import dump_json, git_show, rev_parse  # noqa: E402

_UPSTREAM_PKG = "src/nonebot_plugin_parser_lite"
_SOURCES = {
    "render": f"{_UPSTREAM_PKG}/render/__init__.py",
    "matchers": f"{_UPSTREAM_PKG}/matchers/__init__.py",
    "macros": f"{_UPSTREAM_PKG}/render/templates/macros.jinja",
    "exception": f"{_UPSTREAM_PKG}/exception.py",
    "helper": f"{_UPSTREAM_PKG}/helper.py",
    # B 站 stats.extra 标签（弹幕/硬币）的宿主模块：vendor standalone 的
    # extra 值为标量，桥内翻译表消费的标签文案逐字来自这里
    "bilibili": f"{_UPSTREAM_PKG}/parsers/bilibili/__init__.py",
}

# 锚点规则表：key → (匹配方式, 针子)。声明顺序即 texts.py 常量顺序。
# equals 只匹配字符串常量；contains 匹配常量与模板。改锚点 = 人工复核上游。
_TEXT_ANCHORS: dict[str, tuple[str, str]] = {
    "render_failed": ("equals", "图片渲染失败"),
    "online_play": ("contains", "在线播放"),
    "repost_marker": ("contains", "原帖"),
    "oversized_hint": ("contains", "媒体太大啦"),
    "media_failed": ("contains", "媒体加载失败"),
    "download_failed": ("equals", "媒体下载失败"),
    "sticker_placeholder": ("equals", "[表情]"),
    "poll_header": ("contains", "【投票】"),
    "poll_title_fallback": ("equals", "投票"),
    "poll_option_line": ("contains", " 票 "),
    "poll_status_closed": ("equals", "已结束"),
    "poll_status_open": ("equals", "进行中"),
    "poll_status_multiple": ("equals", "多选"),
    "poll_voters": ("contains", "人参与"),
    "status_separator": ("equals", " · "),
    "lazy_download_prompt": ("contains", "秒内发送以下命令"),
    "video_zero_size": ("equals", "视频文件大小为 0"),
    # 下载失败计数整句（render.py send_content 尾段）：桥发送编排的计数
    # 聚合语义与上游同构，整句直接收编（2026-09-13 候选扫描发现，
    # 原「上游无对应整句」声明作废）
    "download_failed_count": ("contains", "项媒体下载失败"),
    # stats.extra 形状翻译表的标签（render.py _EXTRA_LABELS 消费）：上游在
    # 番剧/视频两条 stats 路径合法重复同值，用 equals_repeated 容忍重复。
    # 该规则同时断言「确实重复」（命中数 ≥ 2）：上游合并两条
    # 路径后规则即失效，须降级为 equals（2026-09-17 评审 M10）
    "extra_label_danmaku": ("equals_repeated", "弹幕"),
    "extra_label_coin": ("equals_repeated", "硬币"),
}

# 模板常量的占位符语义（texts.py 行尾注释；顺序即 {0}/{1}/… 顺序）
_PLACEHOLDER_DOCS: dict[str, str] = {
    "oversized_hint": "{0}=平台显示名",
    "media_failed": "{0}=内容类型名",
    "poll_header": "{0}=投票标题",
    "poll_option_line": "{0}=选项文本 {1}=票数 {2}=占比",
    "poll_voters": "{0}=参与人数",
    "lazy_download_prompt": "{0}=超时秒数 {1}=命令列表",
    "download_failed_count": "{0}=失败项数",
}

# 锚点 → 提取源（默认 render）；来源注记进 texts.py 行注释
_ANCHOR_SOURCE: dict[str, str] = {
    "lazy_download_prompt": "matchers",
    "download_failed": "exception",
    "video_zero_size": "helper",
    "extra_label_danmaku": "bilibili",
    "extra_label_coin": "bilibili",
}

# 注入防火墙：显示文本经分析数据进入生成代码字符串字面量，回车符破坏行结构、
# 超长值疑似异常快照——都在最前端响亮拒绝
_TEXT_MAX_BYTES = 1024

_LOG_ATTRS = frozenset({"debug", "info", "warning", "error", "exception"})


def _template(node: ast.JoinedStr) -> str:
    """f-string → 模板串：FormattedValue 按出现序归一为 {n!conv:spec} 占位。"""
    parts: list[str] = []
    index = 0
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            conversion = ""
            if value.conversion not in (-1, None):
                conversion = "!" + chr(value.conversion)
            spec = ""
            if isinstance(value.format_spec, ast.JoinedStr):
                spec = _template(value.format_spec)
            parts.append("{" + str(index) + conversion + (":" + spec if spec else "") + "}")
            index += 1
    return "".join(parts)


def _docstring_ids(tree: ast.Module) -> set[int]:
    """模块/类/函数首语句字符串的节点 id（docstring 不是用户可见文案）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _is_log_call(node: ast.AST) -> bool:
    """logger.debug/info/warning/error/exception 调用（子树整体剪枝）。"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_ATTRS
        and (
            (isinstance(node.func.value, ast.Name) and "log" in node.func.value.id.lower())
            or (
                isinstance(node.func.value, ast.Attribute) and "log" in node.func.value.attr.lower()
            )
        )
    )


def _candidates(tree: ast.Module) -> list[tuple[str, str, int]]:
    """(形态, 文案, 行号) 候选全集：const/tmpl，剪掉 docstring 与日志子树。"""
    skip = _docstring_ids(tree)
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST) -> None:
        if _is_log_call(node):
            return
        # 赋值后的孤立字符串注记（PEP 224 属性 docstring 惯用法，如常量
        # 下方的说明串）不是运行时文案——整句剪枝
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found.append(("const", node.value, node.lineno))
        elif isinstance(node, ast.JoinedStr):
            found.append(("tmpl", _template(node), node.lineno))
            # 模板常量部件不再作为 const 候选（已并入模板）；但占位表达式里的
            # 嵌套常量（如 title or '投票' 的回退值）仍是独立候选
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    visit(value.value)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _validate_value(key: str, value: str) -> None:
    if "\r" in value:
        raise SystemExit(f"显示文本 {key} 含回车符，拒绝写入提取产物：{value!r}")
    if not value or len(value.encode("utf-8")) > _TEXT_MAX_BYTES:
        raise SystemExit(
            f"显示文本 {key} 为空或超过 {_TEXT_MAX_BYTES} 字节上限，疑似异常快照：{value!r}",
        )


def _select(found: list[tuple[str, str, int]], key: str, rule: tuple[str, str]) -> str:
    kind, needle = rule
    if kind == "equals":
        matches = [c for c in found if c[0] == "const" and c[1] == needle]
    elif kind == "equals_repeated":
        # equals 的重复容忍变体：该文案在上游**多处**合法重复（如番剧/视频
        # 两条 stats 路径），故允许多于一个候选。
        #
        # 2026-09-17 评审 M10：原实现在按值过滤后对值取集合判「不一致」，
        # 集合恒为 {needle}，该分支是死代码——真正的语义分叉不会被检出。改为断言
        # **重复本身**（命中数 ≥ 2），检查在下方共用区。
        matches = [c for c in found if c[0] == "const" and c[1] == needle]
    else:
        matches = [c for c in found if needle in c[1]]
    if not matches:
        raise SystemExit(
            f"锚点 {key}（{needle!r}）在上游 main 显示文本中零命中——上游可能已改写该文案，"
            "请复核 scripts/extract_display_texts.py 的锚点规则表",
        )
    if kind == "equals_repeated" and len(matches) < 2:
        raise SystemExit(
            f"锚点 {key}（{needle!r}）声明为「多处合法重复」，实际只命中 "
            f"{len(matches)} 处：{matches}，请复核 scripts/extract_display_texts.py 的锚点规则表——"
            "若该文案在上游已不再重复（stats 路径被合并），应把规则降级为 equals",
        )

    if len(matches) > 1 and kind != "equals_repeated":
        raise SystemExit(
            f"锚点 {key}（{needle!r}）命中 {len(matches)} 个候选，失去区分度：{matches}，"
            "请复核 scripts/extract_display_texts.py 的锚点规则表",
        )
    _, value, _lineno = matches[0]
    _validate_value(key, value)
    return value


def _template_texts(macros_src: str) -> list[str]:
    """卡面模板的用户可见文案全集（含 CJK 的标签间文本），供子集漂移断言。"""
    import re

    texts = {
        fragment.strip()
        for fragment in re.findall(r">([^<>{}\n]*[\u4e00-\u9fff][^<>{}\n]*)<", macros_src)
        if fragment.strip()
    }
    return sorted(texts)


def extract(
    render_src: str,
    matchers_src: str,
    macros_src: str,
    exception_src: str = "",
    helper_src: str = "",
    bilibili_src: str = "",
) -> dict[str, Any]:
    """纯函数：上游源码 → 提取产物数据（幂等确定）。"""
    found = (
        _candidates(ast.parse(render_src))
        + _candidates(ast.parse(matchers_src))
        + _candidates(ast.parse(exception_src))
        + _candidates(ast.parse(helper_src))
        + _candidates(ast.parse(bilibili_src))
    )
    texts: dict[str, dict[str, str]] = {}
    for key, rule in _TEXT_ANCHORS.items():
        source_file = _SOURCES[_ANCHOR_SOURCE.get(key, "render")]
        module_path = "/".join(Path(source_file).parts[-2:])
        texts[key] = {
            "value": _select(found, key, rule),
            "source": f"{module_path}@main",
        }
    for key in _TEXT_ANCHORS:
        if key in _PLACEHOLDER_DOCS:
            texts[key]["placeholders"] = _PLACEHOLDER_DOCS[key]
    return {
        "_provenance": PROVENANCE,
        "texts": texts,
        "template_texts": _template_texts(macros_src),
    }


# 快照注记的拼接顺序（source_digest 公式的一部分；新增提取源时须显式扩展）
_DIGEST_ORDER = ("render", "matchers", "macros", "exception", "helper", "bilibili")


def build_payload(sources: dict[str, str], revision: str) -> dict[str, Any]:
    """提取产物 + 快照注记（入库 JSON 完整形态；CLI 与编排层共用同一公式）。"""
    payload = extract(
        sources["render"],
        sources["matchers"],
        sources["macros"],
        sources["exception"],
        sources["helper"],
        sources["bilibili"],
    )
    payload["source_revision"] = revision
    payload["source_digest"] = hashlib.sha256(
        "".join(sources[name] for name in _DIGEST_ORDER).encode("utf-8"),
    ).hexdigest()
    return payload


def read_sources(repo: Path, ref: str) -> dict[str, str]:
    """读上游显示文本提取源全集（公开给编排层；键 = _SOURCES 键）。"""
    return {name: git_show(repo, ref, path) for name, path in _SOURCES.items()}


# 候选扫描面向 CJK 文案（桥显示文本皆为中文用户可见文案）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def scan_candidates(sources: dict[str, str], known_values: set[str]) -> list[str]:
    """上游新增文案候选扫描（advisory：只提示，不阻塞管线）。

    收集桥消费面模块中含 CJK 的字符串常量/f-string 模板，剔除已被锚点
    收编的值。上游迭代规律（2026-09-13 近 60 提交分析）显示用户可见文案
    偶发新增于 render/matchers/exception/helper——候选清单把「人工重读
    上游 diff 找新文案」收敛为「看 sync 日志扩充锚点表」。
    """
    found = (
        _candidates(ast.parse(sources["render"]))
        + _candidates(ast.parse(sources["matchers"]))
        + _candidates(ast.parse(sources["exception"]))
        + _candidates(ast.parse(sources["helper"]))
        + _candidates(ast.parse(sources["bilibili"]))
    )
    seen: set[str] = set()
    out: list[str] = []
    for _kind, value, _lineno in found:
        if value in known_values or value in seen or not _CJK_RE.search(value):
            continue
        seen.add(value)
        out.append(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取上游 main 分支显示文本（文本注入最前端）")
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT / ".sync-work" / "upstream"),
        help="上游克隆目录（需已 fetch 目标 ref）",
    )
    parser.add_argument("--ref", default="origin/main", help="显示文本提取源分支引用")
    args = parser.parse_args(argv)

    sources = read_sources(Path(args.repo), args.ref)
    revision = rev_parse(Path(args.repo), args.ref)
    payload = build_payload(sources, revision)
    dump_json(OUTPUT_PATH, payload)
    print(
        f"已提取 {len(payload['texts'])} 条显示文本 + "
        f"{len(payload['template_texts'])} 条模板文案 → {OUTPUT_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
