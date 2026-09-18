# AGENTS.md

AstrBot 插件：`astrbot_plugin_parser_lite`。基于 nonebot-plugin-parser-lite 的 standalone
分支快照（vendored，零修改）加桥接层（ACL），自动解析 QQ 群内 B 站/抖音/小红书等平台的
分享链接并转发媒体内容。

**贡献流程、门禁、评审规则与仓库治理约定一律见 `CONTRIBUTING.md`** —— 本文件不重复它，
只补充 agent 在本仓库工作时需要的工作区约定。

## 工作区约定

### Issue 与规格

以本地 markdown 形式存放于 `.scratch/<feature-slug>/`（该目录不入版本控制）。
一个特性一个目录，规格为 `spec.md`，实现票据为 `issues/<NN>-<slug>.md`
（从 `01` 起编号，一票一文件，不要合并）；票据顶部的 `Status:` 行记录状态，
对话历史追加在文件底部的 `## Comments` 之下。

### Triage 标签

五个规范角色串原样使用：`needs-triage` / `needs-info` / `ready-for-agent` /
`ready-for-human` / `wontfix`。语义见 `CONTRIBUTING.md` 第 7 节。

### 领域文档

单上下文仓库：术语表与系统设计在仓库根的 `doc/CONTEXT.md`。
写出领域概念时沿用其中的词，不要漂移；新增术语先落 `doc/CONTEXT.md` 再进代码。

## 工作流

工程变更遵循 **Grill → Spec → Tickets → Implement → Review** 主线：

1. **Grill** —— 先拷问需求本身：边界、失败模式、不可逆点、非目标。
2. **Spec** —— 落成可评审的规格，明确验收判据。
3. **Tickets** —— 拆成可独立验证的票据，一票一个全新上下文实现。
4. **Implement** —— 一次一票；新增行为先写测试（红 → 绿 → 重构）。
5. **Review** —— 由独立评审做双轴检查：**规范轴**（是否符合本仓库的文档化约定）
   与**规格轴**（是否真的实现了票面要求）。

琐碎修复可直接实施，但仍须过门禁。

## 硬性约束（详见 CONTRIBUTING.md 第 4 节）

- `vendor/` 是逐字节零修改的上游快照，**禁止任何工具格式化或手改**。
- `_conf_schema.json` / `bridge/gen_config.py` / `bridge/texts.py` / `bridge/render_params.py` /
  `templates/` / `metadata.yaml` / `README.md` 都是**生成工件**，改数据源或模板后
  跑 `python scripts/run_injection.py --offline` 再生，不要直接编辑。

改动完成前，`CONTRIBUTING.md` 第 3 节列出的 **10 道门禁必须全绿**。
