"""门禁工具版本的单一定义源：从 pre-commit 配置的 ``rev:`` 推导 pip 版本。

为什么需要：pre-commit 的 ruff 钩子在**自己的隔离
环境**里跑，版本由 ``config/.pre-commit-config.yaml`` 的 ``rev:`` 决定；而
``promote-dev-to-main`` 与 ``ci.yml`` 的 typecheck job 是在 venv 里直接装
ruff 与 mypy 来跑（后者经 ``scripts/typecheck.py`` 调用），装的是 pip 版本。
两处一旦不一致，同一个文件会出现「一边判过、一边判不过」的格式化判定分歧；
而漏装更糟 —— 门禁以 ``No module named ruff`` 假红，把**整条提权通道锁死**
（门禁过不了 → main 永远推不动），因为那是环境缺失、不是代码问题。

所以版本不再手抄第二份，而是从 rev 解析：本模块是唯一实现，复合 action
``.github/actions/setup-env`` 与 ``tests/test_supply_chain.py`` 共用。

用法::

    python scripts/pinned_tool_versions.py          # ruff==0.16.7
    python scripts/pinned_tool_versions.py ruff     # 0.16.7

刻意用正则而非 ``yaml.safe_load``：本脚本在依赖安装**过程中**被调用
（composite action 的 extras=gate 分支），此时 PyYAML 未必已在位；而
``scripts/`` 不依赖 PyYAML 是本仓库的既有约定（见 tests/requirements-test.txt
里对 ``PyYAML>=6.0`` 的注释：那是 astrbot 侧的需要）。配置格式由本仓库自己
维护，且 ``tests/test_supply_chain.py`` 会用 PyYAML 交叉验证两种解析结果一致，
所以正则的脆弱性被测试兜住了。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRECOMMIT_CONFIG = REPO_ROOT / "config" / ".pre-commit-config.yaml"

# pre-commit 的钩子仓库标识 → pip 包名。
# ruff-pre-commit 的 tag 就是 ruff 的版本号（仅多一个 v 前缀），故可直接映射。
HOOK_REPO_TO_PACKAGE = {"ruff-pre-commit": "ruff"}


def _rev_of(repo_fragment: str, config_path: Path) -> str:
    """取 ``repo: ...<fragment>`` 紧随其后的 ``rev:`` 值。"""
    text = config_path.read_text(encoding="utf-8")
    pattern = rf"repo:\s*\S*{re.escape(repo_fragment)}\s*\n\s*rev:\s*(\S+)"
    match = re.search(pattern, text)
    if match is None:
        raise LookupError(f"{config_path.name} 中找不到 {repo_fragment} 的 rev")
    return match.group(1)


def pinned_versions(config_path: Path = PRECOMMIT_CONFIG) -> dict[str, str]:
    """``{pip 包名: 版本号}``，全部取自 pre-commit 的 ``rev:``（去掉 v 前缀）。"""
    return {
        pkg: _rev_of(frag, config_path).lstrip("v") for frag, pkg in HOOK_REPO_TO_PACKAGE.items()
    }


def main(argv: list[str]) -> int:
    versions = pinned_versions()
    if argv:
        for name in argv:
            if name not in versions:
                sys.stderr.write(f"未知工具：{name}（可选：{', '.join(sorted(versions))}）\n")
                return 2
            print(versions[name])
        return 0
    for pkg, version in versions.items():
        print(f"{pkg}=={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
