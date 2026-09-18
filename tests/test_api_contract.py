"""静态轨契约②：vendor 公开 API 签名快照（Hyrum 防线，工单 09）。

快照即契约：上游快照滚动导致签名/字段变化时本测试变红——先人工评审 diff
确认兼容，再 `pytest --snapshot-update` 重建；禁止失败即重建（Jest 纪律）。
"""

from __future__ import annotations

import importlib
import inspect

MOD_PATH = "astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite"

MODULE_LEVEL_API = ("match", "parse", "configure", "shutdown_runtime", "clear_result_cache")
PARSER_METHODS = ("__init__", "match", "parse", "aclose")


def _describe() -> dict[str, object]:
    mod = importlib.import_module(MOD_PATH)

    def sig(owner: object, name: str) -> str:
        try:
            return str(inspect.signature(getattr(owner, name)))
        except (TypeError, ValueError):
            return "<no-signature>"

    config_fields = {
        name: {
            "annotation": str(field.annotation),
            "required": field.is_required(),
            "default": repr(field.default),
        }
        for name, field in mod.Config.model_fields.items()
    }
    return {
        "module_exports": sorted(getattr(mod, "__all__", ())),
        "module_level": {name: sig(mod, name) for name in MODULE_LEVEL_API},
        "Parser": {name: sig(mod.Parser, name) for name in PARSER_METHODS},
        "MatchResult_surface": sorted(n for n in dir(mod.MatchResult) if not n.startswith("_")),
        "Config_fields": config_fields,
    }


def test_vendor_public_api_snapshot(snapshot):
    assert _describe() == snapshot
