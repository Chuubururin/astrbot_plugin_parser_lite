"""Logging adapter used by the standalone build (AstrBot 上架合规版).

上架规范要求插件日志必须且只能来自 astrbot.api logger（禁止内置 logging
模块）。本文件与上游原版的偏差是已声明的快照合规层：由 roll 流水线在整树
重建后自动应用（_apply_log_compliance），verify_vendor 按同集合豁免并断言
改写在位。
"""

from astrbot.api import logger as _astrbot_logger


class ParserLogger:
    """vendor 调用面的日志适配（组合替代上游的 LoggerAdapter 继承）：

    ``success`` 是 vendor 调用面的 info 别名，其余属性透传宿主 logger。
    """

    def __init__(self, base=None) -> None:
        self._base = base if base is not None else _astrbot_logger

    def success(self, message, *args, **kwargs) -> None:
        self._base.info(message, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._base, item)


logger = ParserLogger(_astrbot_logger)
