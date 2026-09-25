"""生成文件：scripts/generate_config.py 经模板注入生成，勿手改。

数据源：vendor nonebot_plugin_parser_lite 1.3.8rc6（第一层
分析数据 vendor_analysis.json，scripts/analyze_vendor.py 从上游快照现场
再生，不入库）。
消费方：main.py —— 区分上游字段与桥自有字段，并在启动时告警陈旧配置键。
"""

from __future__ import annotations

# 上游 Config 的全部 plite_* 字段（上游演进时随 roll 自动再生）
VENDOR_FIELDS: frozenset[str] = frozenset(
    {
        "plite_append_qrcode",
        "plite_append_url",
        "plite_bili_cdn_domain",
        "plite_bili_cdn_region",
        "plite_bili_video_codes",
        "plite_bili_video_quality",
        "plite_blacklist_users",
        "plite_day_range",
        "plite_disabled_platforms",
        "plite_download_command",
        "plite_embed_url",
        "plite_forward_text_threshold",
        "plite_lazy_download",
        "plite_lazy_download_timeout",
        "plite_lazy_download_tip",
        "plite_linuxdo_ck",
        "plite_live_photo",
        "plite_max_comments",
        "plite_max_retries",
        "plite_max_size",
        "plite_need_forward_contents",
        "plite_need_upload",
        "plite_need_upload_audio",
        "plite_need_upload_video",
        "plite_render_theme",
        "plite_summary_in_forward",
        "plite_theme_dirs",
        "plite_use_base64",
        "plite_video_in_forward",
        "plite_x_ck",
        "plite_zhihu_ck",
    },
)
# 桥自有配置键（不透传 vendor configure()；语义见 _conf_schema.json）
BRIDGE_FIELDS: frozenset[str] = frozenset(
    {
        "plite_render",
        "plite_verbose_error",
        "plite_video_file_threshold_mb",
    },
)
# 桥自有配置默认值（main.py 统一从此取回退，避免默认字面量双份漂移）
BRIDGE_DEFAULTS: dict[str, object] = {
    "plite_render": True,
    "plite_verbose_error": False,
    "plite_video_file_threshold_mb": 100,
}
