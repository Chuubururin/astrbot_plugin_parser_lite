"""类型门禁：mypy 的包基必须显式指定（根目录无 ``__init__.py``）。

背景：插件以 PEP 420 命名空间包被宿主（``data.plugins.<dir>.<module>``）与
测试导入，因此根目录不再有 ``__init__.py``。这会让 mypy 默认的「路径 → 模块名」
推断失效：``bridge/render.py`` 被认成顶层模块 ``render``，于是 ``from . import``
报 ``No parent module -- cannot perform relative import``。

修法：把**父目录**当包基（``explicit_package_bases``），并让 cwd 就是那个父目录
——mypy 会把 cwd 也算作包基，两者共存时同一个文件被映射到两个模块名（
``bridge`` 与 ``astrbot_plugin_parser_lite.bridge``）而直接报错。故本脚本 chdir 到
父目录后再调 mypy，并把缓存目录钉回仓库内（否则会落在父目录）。

用法：python3 scripts/typecheck.py [mypy 额外参数]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
CONFIG = PACKAGE / "config" / "pyproject.toml"
EXPECTED_NAME = "astrbot_plugin_parser_lite"


def main(argv: list[str]) -> int:
    if PACKAGE.name != EXPECTED_NAME:
        print(
            f"目录名 {PACKAGE.name!r} 与包名 {EXPECTED_NAME!r} 不一致："
            "mypy 的模块名推断会错位（测试也以该包名导入），请把仓库目录改成包名",
            file=sys.stderr,
        )
        return 1
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(CONFIG),
        "--cache-dir",
        str(PACKAGE / ".mypy_cache"),
        *argv,
        EXPECTED_NAME,
    ]
    return subprocess.run(command, cwd=PACKAGE.parent, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
