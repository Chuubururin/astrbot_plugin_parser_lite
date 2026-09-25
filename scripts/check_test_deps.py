#!/usr/bin/env python3
"""环境守卫：断言以 --no-deps 安装的 astrbot 其导入链完整。

为什么需要它
------------
``astrbot/core/__init__.py`` 在导入期即执行副作用——建 SQLite 库、初始化
logger / 偏好存储 / t2i 渲染器——因此任何 ``import astrbot.core.*`` 都会拉起
大半个框架。CI 为避开 astrbot 声明的 63 个运行依赖而以 ``--no-deps`` 安装，
就必须把该导入闭包所需的第三方包显式补齐（清单见 tests/requirements-test.txt）。

缺依赖的失败形态很隐蔽：pytest 收集期报错，其余模块静默 skip，而 skip 预算（3）
恰好容忍这种退化。本脚本把「cryptic traceback」换成「缺哪个包、被谁需要」。

用法::

    python3 scripts/check_test_deps.py

退出码 0 = 齐备；1 = 有缺失（并打印可直接粘贴的 pip 命令）。
仅用标准库，不联网，不写文件。
"""

import importlib
import sys

# 本仓实际触达的 astrbot 模块：桥接层（main.py / sender.py / render.py）与
# tests/ 中 import 的并集。新增对 astrbot 的 import 时应同步补进这里。
TOUCHED_MODULES = (
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.message_components",
    "astrbot.core.message.message_event_result",
    "astrbot.core.platform.astrbot_message",
    "astrbot.core.platform.astr_message_event",
    "astrbot.core.platform.message_type",
    "astrbot.core.platform.platform_metadata",
    "astrbot.core.utils.astrbot_path",
    "astrbot.core.utils.session_waiter",
)


def probe() -> tuple[dict[str, list[str]], dict[str, str]]:
    """返回（缺失的顶层模块 -> 需要它的模块列表，非缺依赖的导入异常）。"""
    missing: dict[str, list[str]] = {}
    broken: dict[str, str] = {}
    for name in TOUCHED_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing.setdefault(exc.name or "<unknown>", []).append(name)
        except Exception as exc:
            # 导入期副作用失败（数据目录不可写、配置损坏 …）不属本脚本职责，
            # 但仍要报出来，免得被误读成「依赖齐备」。
            broken[name] = f"{type(exc).__name__}: {exc}"
    return missing, broken


def main() -> int:
    missing, broken = probe()
    total = len(TOUCHED_MODULES)

    for name, detail in sorted(broken.items()):
        print(f"::warning::{name} 导入失败（非缺依赖）：{detail}")

    if not missing:
        if broken:
            # broken 非空时「完整」是假绿：导入期副作用失败（数据目录不可写、
            # 配置损坏）同样让 astrbot 不可用，只是原因不是缺包
            print(
                f"::error::astrbot 导入链有 {len(broken)} 个模块导入失败（非缺依赖），"
                "见上方 warning",
            )
            return 1
        print(f"astrbot 导入链完整（{total}/{total} 模块）")
        return 0

    for dep, users in sorted(missing.items()):
        print(f"::error::缺依赖 {dep}（被 {', '.join(users)} 需要）")

    print()
    if any(dep.startswith("astrbot") for dep in missing):
        print("astrbot 本体未安装。先执行：")
        print('  python -m pip install --no-deps "astrbot>=4.28,<5"')
        return 1

    print("把上面列出的包补进 tests/requirements-test.txt，然后重跑本脚本。")
    print("注意：本脚本每次报出的是导入链上的**第一层**缺口，补齐后可能暴露下一层。")
    print("要一次拿全，做静态导入闭包分析：从 TOUCHED_MODULES 出发，沿 astrbot")
    print("模块级 import 递归，收集所有非 stdlib 顶层名（sys.stdlib_module_names）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
