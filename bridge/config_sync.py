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


def rearm_runtime() -> None:
    """shutdown_runtime 后在新插件实例里重建下载器出站客户端。

    vendor 的 ``DOWNLOADER`` 单例跨实例共享：AstrBot 保存配置会重载插件，
    旧实例 terminate 已把 DOWNLOADER.client aclose（不可逆）；新实例若直接
    复用该 client，所有下载静默失败。此处以构造参数重建 UniHttpClient
    （与 download/__init__ 同款）。新 client 天然无 ``_ssrf_guarded``，
    install_ssrf_guard 会重新包装钉扎面。
    """
    from ..vendor.nonebot_plugin_parser_lite.constants import DOWNLOAD_TIMEOUT
    from ..vendor.nonebot_plugin_parser_lite.download import DOWNLOADER, UniHttpClient

    DOWNLOADER.client = UniHttpClient(timeout=DOWNLOAD_TIMEOUT)
    DOWNLOADER._active_downloads = {}
