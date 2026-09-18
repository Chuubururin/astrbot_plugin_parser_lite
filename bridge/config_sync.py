"""vendor 导入期配置快照回写（standalone 桥适配层）。

上游 nonebot 环境里插件配置在模块导入前已就绪，vendor 若干类属性在类体
执行时（=导入时）对 pconfig 求值快照是正确的；standalone 桥的 configure()
晚于 vendor 导入运行，这些快照会停留在环境变量默认值上，导致对应配置项
静默失效。vendor 零修改约束下，桥在 configure() 之后统一回写最新值。

幂等：插件重载时随 configure() 再次调用，回写为最新配置。
快照点清单由 tests/test_workchains.py 的 AST 扫描守护——上游同步若新增
导入期快照，测试即红。
"""

from __future__ import annotations

from ..vendor.nonebot_plugin_parser_lite.config import pconfig


def sync_import_time_config() -> None:
    """把 pconfig 当前值回写到 vendor 的导入期快照点（消费点均为
    `self.<attr>` 查找，类上回写即全部实例生效）。"""
    from ..vendor.nonebot_plugin_parser_lite.download import StreamDownloader
    from ..vendor.nonebot_plugin_parser_lite.parsers.linuxdo import LinuxDoParser
    from ..vendor.nonebot_plugin_parser_lite.parsers.zhihu import ZhiHuParser
    from ..vendor.nonebot_plugin_parser_lite.utils.cookie import ck2dict

    StreamDownloader.MAX_RETRIES = pconfig.max_retries
    LinuxDoParser.linuxdo_ck = ck2dict(pconfig.linuxdo_ck) if pconfig.linuxdo_ck else {}
    ZhiHuParser.zhihu_ck = ck2dict(pconfig.zhihu_ck) if pconfig.zhihu_ck else {}
