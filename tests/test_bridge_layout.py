"""布局契约：目录分类 + 导入可落地（迁移类改动的机械守护）。

根目录只留入口文件、桥接模块住 ``bridge/``——这类布局下引用极易被静默漏改，
且全是 pytest/ruff/mypy 看不见的形态：

- ``main.py`` 的 ``from .vendor_patches import ...`` 与
  ``bridge/vendor_patches.py`` 的 8 处 ``from .vendor.``——都在函数体或
  ``TYPE_CHECKING`` 分支里，只有真执行到那条分支才 ImportError；
- 7 个测试模块的 ``from astrbot_plugin_parser_lite import ssrf`` 一类绝对导入——
  收集期就 ImportError，但**只在跑全量时才暴露**，定向跑改动文件不会红。

所以本测试不复核「某个字符串是否被替换」，而是把每条导入**解析到文件系统**：
相对导入按层级回退、绝对导入剥掉插件包前缀，之后必须命中真实模块文件或包目录。

残余边界（如实声明）：``from PKG import X`` 里的 ``X`` 可能是属性而非子模块
（``vendor`` 子包大量如此），静态无法区分，故仅在 ``PKG`` 为插件包根时展开
``X``；``importlib.import_module`` / ``__import__`` 等动态导入不在覆盖面内。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_PACKAGE = PLUGIN_DIR.name
BRIDGE_DIR = PLUGIN_DIR / "bridge"

# 仓库根目录白名单（约定：根目录只承载入口与元数据文件）。
# logo.png 是唯一的二进制例外：AstrBot 的 star_manager 把 logo 文件名硬编码
# 为 ``logo.png`` 且只在插件根目录查找（找到后覆写 metadata.logo_path），
# 位置不可改，故必须常驻根目录。
ROOT_ALLOWED = frozenset(
    {
        ".gitignore",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "README.md",
        "_conf_schema.json",
        "logo.png",
        "main.py",
        "metadata.yaml",
        "requirements.txt",
    },
)

# 桥接层模块清单（main.py 在仓库根，其余八模块在 bridge/）
BRIDGE_MODULES = frozenset(
    {
        "config_sync",
        "gen_config",
        "render",
        "render_params",
        "sender",
        "ssrf",
        "texts",
        "vendor_patches",
    },
)


def _module_file(dotted: str) -> Path | None:
    """点分模块名 → 源文件；``.py`` 与包 ``__init__.py`` 两种形态都接受。"""
    rel = Path(*dotted.split("."))
    for candidate in (PLUGIN_DIR / rel.with_suffix(".py"), PLUGIN_DIR / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_dir(dotted: str) -> Path | None:
    """点分模块名 → 包目录（命名空间包无 ``__init__.py``，故需单独识别）。"""
    path = PLUGIN_DIR / Path(*dotted.split("."))
    return path if path.is_dir() else None


def _resolvable(dotted: str) -> bool:
    return _module_file(dotted) is not None or _package_dir(dotted) is not None


def _resolve_relative(current: str, level: int, module: str | None) -> str | None:
    """相对导入 → 包内点分名；层级回退出包根时返回 None。

    ``current`` 为当前模块的包内点分名（如 ``bridge.render``）；``level`` 为
    ``ast.ImportFrom.level``（1 = 当前包，2 = 上一级，依此类推）。
    """
    parts = current.split(".")[:-1]
    up = level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base)


def _unresolved_targets(current: str, tree: ast.AST) -> list[str]:
    """列出无法落地的导入目标（含层级越界者），供测试与反向验证共用。"""
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            rendered = ast.unparse(node)
            if node.level >= 1:
                base = _resolve_relative(current, node.level, node.module)
                if base is None:
                    problems.append(f"{rendered} -> 相对层级越出包根")
                    continue
                if node.module:
                    if not _resolvable(base):
                        problems.append(f"{rendered} -> {base}")
                    continue
                for alias in node.names:
                    target = f"{base}.{alias.name}" if base else alias.name
                    if not _resolvable(target):
                        problems.append(f"{rendered} -> {target}")
                continue
            module = node.module or ""
            if module == PLUGIN_PACKAGE:
                problems.extend(
                    f"{rendered} -> {alias.name}"
                    for alias in node.names
                    if not _resolvable(alias.name)
                )
            elif module.startswith(f"{PLUGIN_PACKAGE}."):
                inner = module[len(PLUGIN_PACKAGE) + 1 :]
                if not _resolvable(inner):
                    problems.append(f"{rendered} -> {inner}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PLUGIN_PACKAGE or alias.name.startswith(f"{PLUGIN_PACKAGE}."):
                    inner = alias.name[len(PLUGIN_PACKAGE) :].lstrip(".")
                    if inner and not _resolvable(inner):
                        problems.append(f"{ast.unparse(node)} -> {inner}")
    return problems


def _scanned_files() -> list[Path]:
    """受检文件：插件运行面（main.py + bridge/）+ scripts/ + tests/。"""
    return [
        PLUGIN_DIR / "main.py",
        *sorted(BRIDGE_DIR.glob("*.py")),
        *sorted((PLUGIN_DIR / "scripts").glob("*.py")),
        *sorted((PLUGIN_DIR / "tests").glob("**/*.py")),
    ]


def _dotted_of(path: Path) -> str:
    return ".".join(path.relative_to(PLUGIN_DIR).with_suffix("").parts)


def test_all_plugin_imports_resolve() -> None:
    """全仓每条插件导入都必须能落到真实模块上（迁移漏改的机械守护）。"""
    problems = [
        f"{path.relative_to(PLUGIN_DIR)}: {problem}"
        for path in _scanned_files()
        for problem in _unresolved_targets(
            _dotted_of(path),
            ast.parse(path.read_text(encoding="utf-8")),
        )
    ]
    assert not problems, "存在无法落地的导入：" + chr(10) + chr(10).join(problems)


def test_bridge_module_inventory_is_exact() -> None:
    """bridge/ 的模块集合必须与契约清单一致（新增模块需同步引用与发布白名单）。"""
    actual = {path.stem for path in BRIDGE_DIR.glob("*.py")}
    assert actual == BRIDGE_MODULES, (
        f"bridge/ 模块集合变化：多出 {sorted(actual - BRIDGE_MODULES)}，"
        f"缺少 {sorted(BRIDGE_MODULES - actual)}；变更需同步 main.py 引用与发布白名单"
    )


def test_plugin_root_holds_only_entry_files() -> None:
    """根目录只允许承载入口与元数据文件（分类放置的机械守护）。

    以受版本控制的条目为准，而非工作区目录——开发机上根目录还会有大量被
    ``.gitignore`` 排除的运行期产物（``.venv``/``data``/``out`` 等）。
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PLUGIN_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"无 git 可用，跳过根目录白名单校验：{exc}")
    # NUL 分隔（-z）：文件名含换行时不会被误切
    tracked = {name for name in listing.split(chr(0)) if name and "/" not in name}
    extra = sorted(tracked - ROOT_ALLOWED)
    missing = sorted(ROOT_ALLOWED - tracked)
    assert not extra, f"根目录出现白名单外条目，请分类放置到文件夹内：{extra}"
    assert not missing, f"根目录缺少约定条目：{missing}"


def test_plugin_root_has_no_package_marker() -> None:
    """根目录不设 ``__init__.py``：插件以 PEP 420 命名空间包被宿主导入。

    依据：AstrBot ``star_manager`` 以 ``data.plugins.<dir>.<module>`` 导入本插件，
    其内置插件（``builtin_stars/astrbot``、``builtin_stars/builtin_commands``）与
    已装的第三方插件同样没有 ``__init__.py``；该形态导入实测正常。
    """
    marker = PLUGIN_DIR / "__init__.py"
    assert not marker.exists(), "根目录不应有 __init__.py：入口文件只有 main.py"


@pytest.mark.parametrize(
    ("current", "source", "expected"),
    [
        # 迁移后的正确形态：桥接模块下移一层，vendor 需回退到包根
        ("bridge.render", "from ..vendor.nonebot_plugin_parser_lite import Parser", []),
        (
            "bridge.vendor_patches",
            "from ..vendor.nonebot_plugin_parser_lite.creator import Creator",
            [],
        ),
        ("main", "from .bridge import sender", []),
        ("bridge.render", "from . import render_params", []),
        ("bridge.vendor_patches", "from . import ssrf", []),
        # 插件包绝对导入：已随迁移更新的正确形态
        ("main", "from astrbot_plugin_parser_lite.bridge import sender", []),
        ("main", "from astrbot_plugin_parser_lite.bridge.ssrf import validate_url", []),
        (
            "main",
            "from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Parser",
            [],
        ),
        # 迁移漏改形态（一）：相对层级回退不足，落到不存在的子包
        (
            "bridge.vendor_patches",
            "from .vendor.nonebot_plugin_parser_lite.creator import Creator",
            [
                "from .vendor.nonebot_plugin_parser_lite.creator import Creator"
                " -> bridge.vendor.nonebot_plugin_parser_lite.creator"
            ],
        ),
        (
            "main",
            "from .vendor_patches import apply_vendor_patches",
            ["from .vendor_patches import apply_vendor_patches -> vendor_patches"],
        ),
        # 迁移漏改形态（二）：绝对导入仍指旧位置
        (
            "tests.test_ssrf",
            "from astrbot_plugin_parser_lite import ssrf",
            ["from astrbot_plugin_parser_lite import ssrf -> ssrf"],
        ),
        (
            "tests.test_ssrf",
            "from astrbot_plugin_parser_lite.ssrf import validate_url",
            ["from astrbot_plugin_parser_lite.ssrf import validate_url -> ssrf"],
        ),
        (
            "main",
            "import astrbot_plugin_parser_lite.config_sync",
            ["import astrbot_plugin_parser_lite.config_sync -> config_sync"],
        ),
        # 目标模块本就不存在
        ("bridge.render", "from . import nope", ["from . import nope -> bridge.nope"]),
        # 层级越出包根
        (
            "bridge.render",
            "from ...vendor import x",
            ["from ...vendor import x -> 相对层级越出包根"],
        ),
        # 非插件包导入一律不入账
        ("bridge.render", "from astrbot.api import star", []),
        ("bridge.render", "import json", []),
        ("bridge.render", "from nonebot_plugin_parser_lite import Parser", []),
    ],
)
def test_unresolved_detector_covers_known_forms(
    current: str,
    source: str,
    expected: list[str],
) -> None:
    """守护的守护：检测器必须同时认对「能落地」与「落不了地」两类形态。"""
    assert _unresolved_targets(current, ast.parse(source)) == expected


# ---------------------------------------------------------------------------
# 类型门禁的调用契约
# ---------------------------------------------------------------------------

TYPECHECK_WRAPPER = "scripts/typecheck.py"
MYPY_CONFIG = Path("config") / "pyproject.toml"

# 扫描范围：跟踪入库的文本文件，跳过上游快照（vendor/）与不入库目录（docs/）。
# tests/ 也不扫——测试里的夹具字符串天然长得像命令，扫了会自己判自己红。
_COMMAND_SCAN_SKIP = ("vendor/", "docs/", "tests/")


def _tracked_texts() -> list[tuple[str, str]]:
    """git 索引中的文本文件 → [(相对路径, 内容)]（读索引而非工作区）。"""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    items: list[tuple[str, str]] = []
    for rel in out.split(chr(0)):
        if not rel or rel.startswith(_COMMAND_SCAN_SKIP):
            continue
        path = PLUGIN_DIR / rel
        if not path.is_file():
            continue
        try:
            items.append((rel, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return items


def _mypy_invocations(text: str) -> list[tuple[int, str]]:
    """识别「真的在调 mypy」的行。

    只认命令形态：带 ``-m`` 调 mypy 的，或整行以 mypy 开头的。中文散文里
    提到 mypy 的句子以 ``#`` 或普通汉字开头，因此不会误伤。
    """
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if "-m mypy" in line or line == "mypy" or line.startswith("mypy "):
            hits.append((lineno, line))
    return hits


def test_mypy_is_invoked_only_through_the_wrapper() -> None:
    """根目录无 ``__init__.py`` 后，裸调 mypy 会退化成
    ``No parent module -- cannot perform relative import``：mypy 把 cwd 当包基，
    于是 ``bridge/render.py`` 被认成顶层模块 ``render``，相对导入直接失败。

    该退化的表现是整个类型门禁变红（而不是某条断言失败），极易被误读成代码
    问题；故把「所有调用点都必须走 wrapper」固化成契约。
    """
    offenders: list[str] = []
    for rel, text in _tracked_texts():
        for lineno, line in _mypy_invocations(text):
            if TYPECHECK_WRAPPER not in line:
                offenders.append(f"{rel}:{lineno}: {line}")
    assert not offenders, "mypy 必须经 scripts/typecheck.py 调用；违规行：" + "; ".join(offenders)


def test_mypy_config_declares_explicit_package_bases() -> None:
    """wrapper 只是换个 cwd；包基还得靠配置里的 ``explicit_package_bases``。

    缺了它 wrapper 也救不回来（实测：去掉该行 → rc=2，报同一处 No parent module）。
    """
    text = (PLUGIN_DIR / MYPY_CONFIG).read_text(encoding="utf-8")
    head = text.split("[[tool.mypy.overrides]]", 1)[0]
    assert "explicit_package_bases = true" in head


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("run: python -m mypy --config-file config/pyproject.toml .", 1),
        ("        run: python -m mypy .", 1),
        ("mypy .", 1),
        ("python scripts/typecheck.py", 0),
        ("# mypy 的检查范围含 tests/（pyproject 仅排除 vendor/.research/.agents），", 0),
        ("被静默漏改，全是 pytest/ruff/mypy 看不见的形态：", 0),
        ('        "mypy",', 0),
    ],
)
def test_mypy_invocation_detector_covers_known_forms(line: str, expected: int) -> None:
    """守护的守护：检测器必须同时认对「调用」与「只是提到」。"""
    assert len(_mypy_invocations(line)) == expected
