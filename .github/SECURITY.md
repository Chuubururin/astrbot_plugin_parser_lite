# 安全政策（Security Policy）

本文件说明如何报告 **astrbot_plugin_parser_lite** 的安全漏洞（vulnerability），
以及我们处理安全披露（disclosure）的期望与时间线。请在报告前完整阅读。

## 支持的版本

只有最新发布版本接受安全修复。版本号沿用上游
[nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite)
（如 `v1.3.7-pre-release.4`），并随每日同步流水线滚动。

| 版本 | 是否接受安全修复 |
|---|---|
| 最新 release（见 Releases 页） | 是 |
| 更早的 release | 否，请先升级 |

## 如何报告漏洞

**请不要用公开 issue 报告安全漏洞。** 公开披露会在他人在此之前利用该问题。

请改用以下**私密**渠道之一：

1. **GitHub 私密安全公告**（首选）：
   <https://github.com/Chuubururin/astrbot_plugin_parser_lite/security/advisories/new>
2. **邮件**：<318842991+Chuubururin@users.noreply.github.com>
3. 若上述渠道都不可用（例如私有安全公告未对本仓库启用），请开一个**不含漏洞
   细节**的公开 issue，只说明「需要私密沟通渠道」：
   <https://github.com/Chuubururin/astrbot_plugin_parser_lite/issues>

报告中请尽量包含：受影响版本、复现步骤或 PoC、影响面（信息泄露 / SSRF /
任意文件读写 / 远程代码执行 / 拒绝服务）、以及你建议的修复方向。

## 我们的处理时间线

| 阶段 | 期望时限 |
|---|---|
| 确认收到报告 | 7 天内 |
| 初步评估与严重性分级 | 14 天内 |
| 修复或缓解措施 | 90 天内（严重问题尽快） |
| 公开披露 | 修复发布后，与你协商日期 |

我们会与你保持同步；若 90 天内无法修复，会说明原因与新的时间点，而不是
让报告静默沉底。

## 披露政策

- 我们遵循**协调披露**（coordinated disclosure）：在修复发布前不公开技术细节。
- 修复发布后，我们会在 release notes 中致谢报告者（除非你要求匿名）。
- 我们不会对善意遵守本政策的安全研究采取法律行动。

## 范围说明

本插件的桥接层（`bridge/`）处理来自社交平台的**不可信输入**，以下区域是我们
最关注的安全面，也是最有价值的报告方向：

- SSRF 防护（`bridge/ssrf.py`）与其绕过；
- 上游内容解析与转发链路（`bridge/sender.py`、`bridge/render.py`）中的注入；
- 配置同步与代码生成（`bridge/config_sync.py`、`bridge/gen_config.py`）中的
  路径穿越或模板注入；
- 发布与更新链路的完整性（见下）。

**不在范围内**：`vendor/` 目录是上游 `nonebot-plugin-parser-lite` 的逐字节
快照（零修改铁律）。若漏洞源于上游代码，请同时向上游仓库报告；本仓库会随
每日同步流水线接收上游修复。

## 供应链与发布完整性

发布产物可用 Sigstore 证明离线验证：

```bash
gh attestation verify astrbot_plugin_parser_lite-<tag>.zip \
  -R Chuubururin/astrbot_plugin_parser_lite \
  --bundle astrbot_plugin_parser_lite-<tag>.zip.intoto.jsonl
```

每个 release 附带的资产：

| 资产 | 说明 |
|---|---|
| `*.zip` | 插件包（显式白名单构建） |
| `*.zip.sha256` | 校验和 |
| `*.zip.intoto.jsonl` | SLSA provenance（Sigstore bundle，离线可验） |
| `sbom.spdx.json` | SPDX 格式软件物料清单（SBOM） |
| `sbom.spdx.json.intoto.jsonl` | SBOM 的 Sigstore 证明 |

工作流文件的安全基线（第三方 action 全部钉到完整 commit SHA、顶层
`permissions` 最小权限、无 `pull_request_target`、发布走受保护环境 + 人工
放行）见 `CONTRIBUTING.md` 的「供应链与合规基线」一节。
