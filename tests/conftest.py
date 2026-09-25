"""测试公共夹具：先于一切导入设好 vendor 环境变量与导入路径。

vendor 的 path.py 在「导入时」读取 PARSER_LITE_BASE_DIR，因此本文件必须
在所有测试模块之前被 pytest 加载（conftest.py 语义保证）。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
# 与 AstrBot 运行时同构：插件目录本身是一个包，测试按
# astrbot_plugin_parser_lite.<module> 导入
sys.path.insert(0, str(PLUGIN_DIR.parent))
os.environ.setdefault("PARSER_LITE_BASE_DIR", tempfile.mkdtemp(prefix="parser-lite-test-"))


def pytest_collection_modifyitems(config, items):
    """收集期标记处理：syrupy 缺失降级 + ``network`` 隔离区语义落地。

    1. 缺 syrupy 时跳过快照测试：容器运行环境只装运行依赖，``snapshot``
       夹具不存在会让 4 个快照用例报 ERROR（不是 skip）——契约由开发环境
       （装了 syrupy）守护，缺插件的环境应显式跳过而非伪装成失败。
    2. ``network`` marker 的语义（pyproject.toml 已声明「默认跳过；
       RUN_NETWORK_TESTS=1 启用」）必须有统一执行点：若只靠各用例自行
       ``skipif`` / ``pytest.skip`` 静默降级，只有
       tests/test_parse_snapshot.py 一家实现，其余依赖真实 DNS 的用例
       会随环境漂移，导致 CI 的 skip 数不确定、
       ``pytest -m network`` 也选不全。
       此处统一落地该语义，使 skip 数确定（ci.yml 的 skip 预算断言依赖此确定性）。
    """
    try:
        import syrupy  # noqa: F401
    except ImportError:
        skip = pytest.mark.skip(reason="未安装 syrupy，快照契约测试在开发环境运行")
        for item in items:
            if "snapshot" in getattr(item, "fixturenames", ()):
                item.add_marker(skip)

    if os.getenv("RUN_NETWORK_TESTS") != "1":
        skip_network = pytest.mark.skip(reason="network 隔离区；设 RUN_NETWORK_TESTS=1 启用")
        for item in items:
            if "network" in item.keywords:
                item.add_marker(skip_network)
