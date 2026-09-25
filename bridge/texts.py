"""桥显示文本：由 scripts/generate_config.py 生成——勿手改。

全部值逐字来自上游 main 分支（vendor/_upstream/display_texts.json，
文本注入层），随上游 roll 自动再生；模板项用 ``str.format``
位置参数填充。桥自有文案（如「解析失败：」前缀）不在此模块，见
main.py/sender.py 的规则层声明。
"""

from __future__ import annotations

# render/__init__.py@main
RENDER_FAILED = "图片渲染失败"
# render/__init__.py@main
ONLINE_PLAY = "\n在线播放: "
# render/__init__.py@main
REPOST_MARKER = ">>>>>原帖<<<<<"
# render/__init__.py@main
OVERSIZED_HINT = "媒体太大啦，还是去{0}看看吧~"
# render/__init__.py@main
MEDIA_FAILED = "[媒体加载失败：{0}]"
# nonebot_plugin_parser_lite/exception.py@main
DOWNLOAD_FAILED = "媒体下载失败"
# render/__init__.py@main
STICKER_PLACEHOLDER = "[表情]"
# render/__init__.py@main
POLL_HEADER = "【投票】{0}"
# render/__init__.py@main
POLL_TITLE_FALLBACK = "投票"
# render/__init__.py@main
POLL_OPTION_LINE = "- {0}: {1} 票 ({2:.1f}%)"
# render/__init__.py@main
POLL_STATUS_CLOSED = "已结束"
# render/__init__.py@main
POLL_STATUS_OPEN = "进行中"
# render/__init__.py@main
POLL_STATUS_MULTIPLE = "多选"
# render/__init__.py@main
POLL_VOTERS = "{0} 人参与"
# render/__init__.py@main
STATUS_SEPARATOR = " · "
# matchers/__init__.py@main
LAZY_DOWNLOAD_PROMPT = "请在{0}秒内发送以下命令之一来获取媒体资源: \n{1}"
# nonebot_plugin_parser_lite/helper.py@main
VIDEO_ZERO_SIZE = "视频文件大小为 0"
# render/__init__.py@main
DOWNLOAD_FAILED_COUNT = "{0} 项媒体下载失败"
# bilibili/__init__.py@main
EXTRA_LABEL_DANMAKU = "弹幕"
# bilibili/__init__.py@main
EXTRA_LABEL_COIN = "硬币"
# render/context.py@main
UNKNOWN_SIZE = "未知大小"
# render/context.py@main
COVER_ALT = "专辑封面"
