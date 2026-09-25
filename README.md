
<!-- 生成文件：scripts/generate_config.py 经模板注入生成，勿手改。
     手写面仅「桥接说明」一节；正文全部为上游 README（随 roll 再生）。 -->

# astrbot_plugin_parser_lite

nonebot-plugin-parser-lite 的 AstrBot 桥接插件：在 QQ 群聊中自动解析
B 站/抖音/小红书等平台的分享链接，并转发为内容卡片。

## 桥接说明（AstrBot 用户只需看本节）

- **安装**：AstrBot WebUI「插件」页上传发布 zip，或解压到 `data/plugins/` 后重启
- **配置**：WebUI 插件页共 34 个配置项（3 桥接 + 31 上游），保存后自动重载
- **版本与许可证**：随上游快照滚动自动再生（当前上游 v1.3.8rc6），与 release tag 锁步；许可证随上游（MIT），vendor 快照零修改
- **架构与滚动同步机制**：见仓库 `doc/CONTEXT.md`（两层注入与各数据面的术语定义）

### 常见问题

- **私聊收不到解析结果（`verify identify fail`）**：QQ 服务端拒绝非好友或未开通
  临时会话的机器人私信（协议错误码 170019003），代码层无法绕过，解法在账号层：
  加机器人为好友或开通临时会话权限；群聊不受影响。
- **首次加载自动安装依赖**：AstrBot 依赖自恢复机制的一次性行为，无需处理。

### 安全说明

- **出站 SSRF 防护**：媒体下载经四层校验（scheme 白名单 → 端口规则 →
  逐 IP 校验 → 「解析即连接」钉扎防 DNS rebinding）；守卫挂载失败即**拒启**
  （fail-closed）。唯一例外：ffmpeg HLS 子进程——仅入口 URL 校验 + 启动后
  自毁监听，存在无法钉扎 IP 的 DNS TOCTOU 残窗（vendor_patches 头注声明）。
- **TLS 校验姿态**：vendor 下载链继承上游 `verify=False`（CDN 证书链兼容的
  既定取舍）；风险敞口与缓解见「安全说明」一节。
- **渲染数据外发**：渲染经宿主 `html_render` 远程 t2i 服务截图——卡面 HTML
  与内联媒体会发送到宿主配置的渲染端点。

上游 README 原文附后——其中安装/配置说明面向 NoneBot 与「复制目录使用」场景，
AstrBot 用户无需操作：

---

# Parser Lite Standalone

这是从 `main` 自动生成的独立模块版本，发布在 `standalone` 分支。它保留
`nonebot_plugin_parser_lite` 包路径与各平台 Parser 路径，但不依赖 NoneBot、适配器或
任何 NoneBot 插件。

> [!IMPORTANT]
>
> 严禁将本项目用于任何非法用途
>
> 由于使用不当造成的一切责任由使用者承担，本项目维护者无任何责任

## 复制到项目中使用

将 `src/nonebot_plugin_parser_lite` 整个目录复制到目标项目中。目标
项目需要安装 `requirements.txt` 中列出的普通 Python 运行依赖，但不需要安装 NoneBot、
适配器或任何 NoneBot 插件，也不依赖本仓库中的其他目录。

复制后目录结构示例：

```text
your_project/
├── nonebot_plugin_parser_lite/
│   ├── parsers/
│   ├── utils/
│   ├── __init__.py
│   └── ...
└── your_code.py
```
或
```text
your_project/
├── utils/
│   └── nonebot_plugin_parser_lite/
│       ├── parsers/
│       ├── utils/
│       ├── __init__.py
│       └── ...
└── your_code.py
```

## 文本解析流水线

解析入口只接受文本。`until` 可停在匹配或结构化解析阶段，默认停留在解析阶段。

```python
from nonebot_plugin_parser_lite import ParseStep, Parser

async with Parser() as parser:
    matched = await parser.parse(text, until=ParseStep.MATCH)
    result = await parser.parse(text, until=ParseStep.PARSE)

# 长期运行的应用可以复用 Parser 实例，并在退出前调用 await parser.aclose()
```

只使用一个平台时应直接导入对应 Parser，避免加载其他平台模块：

```python
from nonebot_plugin_parser_lite import Parser
from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser

async with Parser([BilibiliParser]) as parser:
    result = await parser.parse("看看这个 https://www.bilibili.com/video/BV1xx411c7mD")
```

也可以保留原有的底层调用方式：

```python
from nonebot_plugin_parser_lite.parsers.bilibili import BilibiliParser

parser = BilibiliParser()
keyword, searched = parser.search_url("https://www.bilibili.com/video/BV1xx411c7mD")
result = await parser.parse(keyword, searched)
await parser.aclose()
```

## 配置

配置默认从同名环境变量读取，例如 `PLITE_BILI_CK`、`PLITE_MAX_COMMENTS`。
列表和布尔值使用 JSON 格式。也可在导入后更新共享配置：

```python
from nonebot_plugin_parser_lite import configure

configure(plite_max_comments=10, plite_disabled_platforms=["x"])
```

缓存根目录默认是当前目录的 `.parser-lite`，可通过 `PARSER_LITE_BASE_DIR` 修改。
解析结果会保留最近 50 项；首次进入异步解析时会在当前事件循环注册每两小时执行一次
的缓存清理任务。应用退出前可调用 `await shutdown_runtime()` 关闭定时任务。

## 独立版边界

消息发送、权限、事件、回复和表情回应属于机器人框架职责，不包含在独立版中。独立版
仅保留解析运行所需的 asyncio 周期任务。解析结果中的媒体任务仍是惰性的：只有显式
等待媒体路径时才会下载。
