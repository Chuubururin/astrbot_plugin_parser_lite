"""发布白名单与桥接导入闭包的一致性（release.yml 机械钉扎）。

release.yml 的 cp 白名单与 zip 结构断言 required 清单必须覆盖 main.py 的
桥接运行时导入闭包——新增桥接模块忘登白名单时此处变红，而不是发布 zip
装载时 ModuleNotFoundError（CONTEXT.md 发布契约：桥接模块 + 生成工件 +
vendor 按白名单注入发布 zip 并做结构断言）。

桥接模块位于 ``bridge/``、不平铺于仓库根：闭包解析按
**包内点分名**进行（``main`` → ``bridge.sender`` → …），比对前再换算成发布
物内的相对路径（``bridge/sender.py``）。cp 侧以整目录 ``bridge`` 入白名单，
目录级拷贝视为已覆盖；required 侧仍逐文件钉扎，是真正会拦住漂移的那一处。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"

# 白名单里的非 Python 工件（元数据/清单/模板/快照/生成物），与 .py 闭包一起必须入选
_NON_PY_ENTRIES = (
    "metadata.yaml",
    "requirements.txt",
    "templates",
    "vendor",
    "_conf_schema.json",
    "README.md",
    "logo.png",
)

# main.py 出发的桥接导入闭包（vendor 分支不展开；延迟导入也算）
_EXPECTED_CLOSURE = frozenset(
    {
        "main",
        "bridge.config_sync",
        "bridge.gen_config",
        "bridge.render",
        "bridge.render_params",
        "bridge.sender",
        "bridge.ssrf",
        "bridge.texts",
        "bridge.vendor_patches",
    },
)

_BACKSLASH = chr(92)


def _cp_section(release_text: str) -> str:
    """release.yml 的「构建插件 zip」步骤体（cp 白名单所在段）。"""
    return release_text.split("构建插件 zip", 1)[1].split("- name:", 1)[0]


def _required_section(release_text: str) -> str:
    """zip 结构断言的 required = [...] 清单原文。"""
    return release_text.split("required = [", 1)[1].split("]", 1)[0]


def _cp_entries(release_text: str) -> set[str]:
    """cp 白名单的源条目集合（跨行续行按空白切分，止于目标目录）。"""
    section = _cp_section(release_text)
    assert "cp -a" in section, "release.yml 的构建步骤里未找到 `cp -a` 白名单"
    head = section.split("cp -a", 1)[1].split("dist/", 1)[0]
    return {token for token in head.split() if token != _BACKSLASH}


def _release_relative(dotted: str) -> str:
    """包内点分名 → 发布物内相对路径（``bridge.sender`` → ``bridge/sender.py``）。"""
    return "/".join(dotted.split(".")) + ".py"


def _cp_covers(entries: set[str], relative: str) -> bool:
    """cp 白名单是否覆盖该发布物相对路径：整目录拷贝也算覆盖。"""
    if relative in entries:
        return True
    return "/" in relative and relative.split("/")[0] in entries


def _module_file(dotted: str) -> Path | None:
    """点分模块名 → 源文件（仅 ``.py``；命名空间包目录另行判断）。"""
    path = REPO_ROOT / Path(*dotted.split(".")).with_suffix(".py")
    return path if path.is_file() else None


def _package_dir(dotted: str) -> Path | None:
    """点分模块名 → 包目录（``bridge/`` 无 ``__init__.py``，需单独识别）。"""
    path = REPO_ROOT / Path(*dotted.split("."))
    return path if path.is_dir() else None


def _resolve_relative(current: str, level: int, module: str | None) -> str | None:
    """相对导入 → 包内点分名；层级回退出包根时返回 None。"""
    parts = current.split(".")[:-1]
    up = level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base)


def _bridge_import_closure() -> set[str]:
    """main.py 出发的桥接模块闭包（``.vendor`` 分支不展开；延迟导入也算）。"""
    seen: set[str] = set()
    queue = ["main"]
    while queue:
        dotted = queue.pop()
        if dotted in seen:
            continue
        source = _module_file(dotted)
        if source is None:
            continue
        seen.add(dotted)
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.ImportFrom) and node.level >= 1):
                continue
            base = _resolve_relative(dotted, node.level, node.module)
            if base is None or base.split(".")[0] == "vendor":
                continue
            if node.module is None:
                candidates = [
                    f"{base}.{alias.name}" if base else alias.name for alias in node.names
                ]
            elif _package_dir(base) is not None:
                # `from .bridge import render_params, texts`：base 是命名空间包，名字即子模块
                candidates = [f"{base}.{alias.name}" for alias in node.names]
            else:
                candidates = [base]
            queue.extend(
                candidate for candidate in candidates if _module_file(candidate) is not None
            )
    return seen


def test_bridge_import_closure_is_exact() -> None:
    """闭包本身必须与契约清单一致：漏一个模块，下面的白名单比对就失去意义。"""
    closure = _bridge_import_closure()
    assert closure == set(_EXPECTED_CLOSURE), (
        f"main.py 桥接导入闭包变化：多出 {sorted(closure - _EXPECTED_CLOSURE)}，"
        f"缺少 {sorted(_EXPECTED_CLOSURE - closure)}"
    )


def test_release_cp_whitelist_covers_bridge_import_closure() -> None:
    """cp 白名单必须覆盖 main.py 导入闭包（含整目录拷贝形态）。"""
    entries = _cp_entries(RELEASE_YML.read_text(encoding="utf-8"))
    missing = [
        relative
        for relative in sorted(_release_relative(d) for d in _bridge_import_closure())
        if not _cp_covers(entries, relative)
    ]
    assert not missing, (
        f"release.yml cp 白名单缺桥接模块（发布 zip 装载即 ModuleNotFoundError，"
        f"请同步登记 cp 与 required 两处）：{missing}"
    )


def test_release_required_list_covers_bridge_import_closure() -> None:
    """zip 结构断言 required 清单必须逐文件覆盖导入闭包。"""
    required = _required_section(RELEASE_YML.read_text(encoding="utf-8"))
    missing = [
        relative
        for relative in sorted(_release_relative(d) for d in _bridge_import_closure())
        if f"astrbot_plugin_parser_lite/{relative}" not in required
    ]
    assert not missing, f"release.yml zip 结构断言缺桥接模块：{missing}"


def test_release_whitelist_covers_non_py_entries() -> None:
    """非 Python 关键工件（元数据/清单/模板/快照/生成物）必须入选 cp 白名单。"""
    entries = _cp_entries(RELEASE_YML.read_text(encoding="utf-8"))
    missing = [entry for entry in _NON_PY_ENTRIES if entry not in entries]
    assert not missing, f"release.yml cp 白名单缺关键工件：{missing}"


def test_zip_required_list_covers_non_py_entries() -> None:
    """zip 结构断言 required 清单至少覆盖元数据/生成物/快照入口。"""
    required = _required_section(RELEASE_YML.read_text(encoding="utf-8"))
    for entry in ("metadata.yaml", "main.py", "bridge/gen_config.py", "_conf_schema.json"):
        assert f"astrbot_plugin_parser_lite/{entry}" in required, f"zip 结构断言缺关键文件：{entry}"
