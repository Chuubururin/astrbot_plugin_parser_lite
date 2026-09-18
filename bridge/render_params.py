"""桥渲染参数：由 scripts/generate_config.py 生成——勿手改。

全部值逐字来自上游 main 分支渲染模块（vendor/_upstream/render_params.json，
渲染参数注入层），随上游 roll 自动再生：渲染缓存键版本、远程 t2i
视口适配基准、超大渲染图 img_seg/file_seg 分流阈值、二维码点阵参数。桥侧
t2i 适配补丁等桥自有语义不在此模块，见 bridge/render.py 的规则层声明。
"""

from __future__ import annotations

# render/__init__.py:RENDER_TEMPLATE_VERSION
RENDER_TEMPLATE_VERSION = "20260913"
# render/__init__.py:get_new_page viewport.width
VIEWPORT_WIDTH = 620
# render/__init__.py:get_new_page viewport.height
VIEWPORT_HEIGHT = 1000
# render/__init__.py:cache_or_render_image st_size 阈值
OVERSIZED_IMAGE_BYTES = 5242880
# render/__init__.py:qrcode.QRCode.version
QRCODE_VERSION = 1
# render/__init__.py:qrcode.QRCode.error_correction
QRCODE_ERROR_CORRECTION = 1
# render/__init__.py:qrcode.QRCode.box_size
QRCODE_BOX_SIZE = 10
# render/__init__.py:qrcode.QRCode.border
QRCODE_BORDER = 1
# render/__init__.py:MAX_FORWARD_TEXT_LEN
MAX_FORWARD_TEXT_LEN = 30000
# render/__init__.py:MAX_FORWARD_NODES
MAX_FORWARD_NODES = 90
# render/__init__.py:TEXT_SPLIT_PUNCTUATION
TEXT_SPLIT_PUNCTUATION = "。！？!?；;，,、…"
