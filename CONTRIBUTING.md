# 贡献指南

`astrbot_plugin_parser_lite` 是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的链接解析插件：
基于上游 [nonebot-plugin-parser-lite](https://github.com/sokoko-org/nonebot-plugin-parser-lite)
的 `standalone` 分支快照（vendored，零修改）加一层 ACL 桥接，在 QQ 群聊中自动解析
B 站/抖音/小红书等平台的分享链接并转发为内容卡片。

本文件是仓库**唯一的流程与治理声明** —— 开发环境、门禁、铁律、评审规则、
分支保护、发布与运维都在这里。术语与系统设计见仓库根的 `doc/CONTEXT.md`。

---

## 1. 架构一句话

```
上游 standalone 快照 (vendor/)  →  两层注入管线  →  生成工件
                                      ↓
                       ACL 桥接层（ParseResult → AstrBot 消息组件）
```

- **桥接层（ACL）**：`main.py` / `bridge/sender.py` / `bridge/render.py` / `bridge/ssrf.py` /
  `bridge/config_sync.py` / `bridge/vendor_patches.py`，只做**翻译**，不引入解析业务规则。
- **两层注入**：`scripts/run_injection.py` 单命令编排「提取 → 分析 → 生成」，
  产出 `_conf_schema.json`、`bridge/gen_config.py`、`bridge/texts.py`、`bridge/render_params.py`、
  `metadata.yaml`、`README.md`、`templates/`。
- **发布**：`release` 工作流按白名单把桥接模块 + 生成工件 + vendor 打进 zip，
  并做结构断言（单顶层目录 + 关键文件在位）。

---

## 2. 开发环境

```bash
git clone https://github.com/Chuubururin/astrbot_plugin_parser_lite.git
cd astrbot_plugin_parser_lite
git switch dev            # 工作分支；直接改 main 的 PR 会被 main-pr-target-guard 判红

python3 -m venv .venv && . .venv/bin/activate        # Python 3.12
pip install -r requirements.txt -r requirements/host-provided.txt
pip install pytest pytest-asyncio syrupy
pip install --no-deps "astrbot>=4.28,<5"             # 桥接层行为契约测试需要
pip install -r tests/requirements-test.txt

pre-commit install --install-hooks
pre-commit install --hook-type pre-push
```

`astrbot` 不在 `requirements.txt` 里（由宿主 AstrBot 提供）。本地若不装它，
`test_event_stop` / `test_sender_chain` / `test_json_card_urls` 会整模块
`importorskip` **静默跳过** —— 而它们恰是桥接层的行为契约，所以请务必装上。
`scripts/check_test_deps.py` 就是为这条静默降级设的守卫。

---

## 3. 门禁（10 道，全部必须 exit 0）

| # | 门禁 | 命令 | 说明 |
|---|---|---|---|
| 1 | 测试 | `python -m pytest -c config/pyproject.toml --rootdir=. -q` | 本机基线 **450 passed / 3 skipped**；全新克隆（含 CI）为 **446 passed / 7 skipped**——多出的 4 条需要本地 `.sync-work/` 上游克隆（跳过原因写作「无上游克隆」），两者均为设计内 |
| 2 | Lint | `ruff check --config config/pyproject.toml .` | 选 `E,F,W,I,UP,B,SIM,RUF,ASYNC,C4,COM,FURB,PERF,RET` |
| 3 | 格式 | `ruff format --config config/pyproject.toml --check .` | `line-length = 100`，`target-version = "py312"` |
| 4 | 类型 | `python scripts/typecheck.py` | 根目录无 `__init__.py`，需显式包基（见脚本 docstring）；`warn_unused_ignores` 打开，多余的 `# type: ignore` 会变红 |
| 5 | 快照 | `python scripts/verify_vendor.py` | vendor 三层逐字节校验 |
| 6 | 注入 | `python scripts/run_injection.py --offline --check` | 提取产物与生成工件均新鲜 |
| 7 | 生成 | `python scripts/generate_config.py --check` | 生成工件与重新生成一致 |
| 8 | 维护 | `python scripts/maintenance_check.py` | 读 `maintenance/checklist.json` 逐条求值 |
| 9 | 依赖 | `python scripts/check_test_deps.py` | astrbot 导入链完整（10/10 模块） |
| 10 | 钩子 | `pre-commit run --config config/.pre-commit-config.yaml --all-files` | 9 hooks，含 `zizmor --pedantic`、`actionlint` |

CI 把门禁拆成三个 required check（`lint` / `typecheck` / `test`），与本地同构。

> **踩坑提醒**：别写 `cmd | tail; echo $?` —— 那拿到的是 `tail` 的退出码。
> 正确写法是先重定向再取码：`cmd >/tmp/o 2>&1; echo "rc=$?"; tail -3 /tmp/o`。

---

## 4. 两条铁律

### 4.1 `vendor/` 是逐字节零修改的上游快照

`vendor/nonebot_plugin_parser_lite/` 必须与上游 `standalone` 分支**逐字节一致**。
唯一升级方式是**整树替换**（由 `sync-upstream` 工作流自动完成），
**禁止 merge / patch / 手改**，也禁止任何工具格式化它 —— 改了 `verify_vendor.py`
立刻变红，这是设计如此。

桥接侧需要触达的 vendor 内部结构（「缝合点」）只有两处，由
`tests/test_import_contract.py::test_vendor_seam_exists` 守护；上游改名时该测试先红。

### 4.2 生成工件勿手改

`_conf_schema.json`、`bridge/gen_config.py`、`bridge/texts.py`、`bridge/render_params.py`、
`templates/`、`metadata.yaml`、`README.md` 都是**生成物**。
要改它们，请改**数据源或模板**再跑管线：

| 想改什么 | 改哪里 |
|---|---|
| 桥的配置项结构 | `scripts/analyze_vendor.py` 的分析规则 |
| 桥内用户可见文案 | `scripts/extract_display_texts.py` 的锚点规则表 |
| 桥内渲染/裁剪数值 | `scripts/extract_render_params.py` 的锚点规则表 |
| README / metadata 文案 | `scripts/templates/*.jinja` |
| 卡面模板 | 不在此仓库维护，来自上游快照 |

改完跑 `python scripts/run_injection.py --offline` 再生，再用门禁 6/7 复检。

---

## 5. 领域文档与术语

- **`doc/CONTEXT.md`**（仓库根）是本仓库唯一的术语表与系统设计文档。
  代码、测试、workflow 中的术语以它为准；**新增术语先落这里再进代码**。
- 写出领域概念时请沿用 `doc/CONTEXT.md` 的词，不要漂移到它明确避开的同义词。
- 若某个概念在术语表里找不到，这本身是个信号：要么你在发明项目不用的语言
  （重新考虑），要么确实有缺口（补进 `doc/CONTEXT.md`）。

---

## 6. Issue 与规格（本地 markdown）

Issue 与规格以本地 markdown 形式存放于 `.scratch/`（该目录不入版本控制）。

- 一个特性一个目录：`.scratch/<feature-slug>/`
- 规格是 `.scratch/<feature-slug>/spec.md`
- 实现票据一票一文件：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，
  从 `01` 起编号，**不要**合并成一个 tickets 文件
- 票据顶部用 `Status:` 行记录状态
- 对话历史追加在文件底部的 `## Comments` 之下

---

## 7. Triage 标签

五个规范角色串，直接用原名：

| 标签 | 含义 |
|---|---|
| `needs-triage` | 维护者需要评估此 issue |
| `needs-info` | 等待报告者补充信息 |
| `ready-for-agent` | 规格完整，可交给无人值守的 agent |
| `ready-for-human` | 需要人工实现 |
| `wontfix` | 不予处理 |

---

## 8. 分支模型与必需检查

### 8.1 两条长期分支

```
feature 分支 ──PR(base=dev)──▶ dev ──promote-dev-to-main──▶ main
                              ↑                              ↑
                        工作分支：所有变更                 发布指针：
                        在这里被评审与验证                 永远等于某个
                                                          已验证的 dev 快照
```

| 分支 | 角色 | 谁可以推进 | 历史可否改写 |
|---|---|---|---|
| `dev` | **工作分支**。所有 PR（含 `sync-upstream` 的 roll PR）以此为 base | 合并 PR；`sync-upstream` 的 roll PR 与 keepalive | ❌ 禁止（本地 `pre-push` 拦截） |
| `main` | **发布指针**。只承载「已通过全量门禁、可发布」的快照 | **仅** `promote-dev-to-main` 工作流 | ❌ 禁止（同上） |
| `lkg` | 回滚锚点。每次 roll 前留档上一已验证快照 | `sync-upstream`（每次覆写） | ✅ 允许 force（语义即覆写） |

**贡献者请一律向 `dev` 开 PR。** 打到 `main` 的 PR 会被
`main-pr-target-guard` 工作流直接判红（见 8.3）。

### 8.2 远端没有分支保护 —— 门禁在工作流里

本仓库是 **private 且未开通 GitHub Pro**，GitHub 的分支保护与 rulesets 对私有仓库
仅对付费计划开放：`GET /repos/{owner}/{repo}/branches/main/protection` 与
`/rulesets` 均返回 `403 Upgrade to GitHub Pro`。

所以这里的强制力**不在 GitHub 侧**，而由三件事共同构成：

| 强制点 | 机制 | 拦截什么 |
|---|---|---|
| `main-pr-target-guard` 工作流 | 对 base=main 的 PR 直接 `exit 1` | 把变更绕过 dev 直接提给 main |
| `promote-dev-to-main` 的 job 内门禁 | promote 前自带 lint / mypy / vendor 校验 / 全量 pytest，任一红即不推 main | 把未验证的快照推上 main |
| 本地 `pre-push` 钩子（`scripts/no-force-push-main.sh`） | 拒绝对 `main`/`dev` 的非快进推送 | 就地改写两条长期分支的历史 |

> **知道的边界**：以上都不阻止**有写权限的人**用 `git push` 直接推 `main`
> （GitHub 侧没有规则可拦）。这条护栏防的是误操作与自动化，不是恶意。真需要
> 硬隔离时，把仓库转 public 或升级计划后启用分支保护即可——届时本节的三个
> 强制点仍应保留（纵深防御）。

### 8.3 必需检查（required checks）

CI（`.github/workflows/ci.yml`）在**每个** PR 上运行，三个 job 即 required checks
（名字必须与 job `name` 逐字一致）：

- `lint (ruff / actionlint / zizmor)`
- `typecheck (mypy)`
- `test (pytest + vendor verify)`

「确定性三类契约」（import 面 AST 白名单 / API 签名快照 / requirements 派生一致性）
包含在 `test` job 中；网络隔离区快照（`network` 标记）默认跳过，不作为 required。

`push` 触发只跟 `dev`：`main` 由 promote 工作流推进，而 `GITHUB_TOKEN` 推的提交
**不触发**工作流——所以 promote 的 job 内必须自带同构门禁，不能指望 CI 在 main 上
兜底（8.4）。

### 8.4 提权：`promote-dev-to-main`

```bash
# 手动提权（推荐；先看 dry-run 预演）
gh workflow run promote-dev-to-main -f dry_run=true
gh workflow run promote-dev-to-main

# 或：向 dev 推一个带 [promote] 前缀的提交，自动触发
git commit --allow-empty -m "[promote] release v1.3.7"
```

流程：**job 内跑全套门禁** → 校验 `main` 是 `dev` 的祖先（可快进）→
`git push --force-with-lease` 把 `main` 推到 `dev` 的 sha。

两个刻意的设计决定：

1. **提权 = 快进，不产生新提交。** 因此 `main` 上每个提交都逐字节等于 `dev` 上
   某个已过 CI 的提交，两者可比对。若 `main` 存在 `dev` 没有的提交（有人绕过
   工作流直推），工作流**响亮失败**并列出那些提交——不做静默 merge，因为那会
   把一个 dev 从未验证过的合并提交送进发布线。
2. **`main` 上不会出现新的 CI 运行。** `GITHUB_TOKEN` 推的提交不触发工作流。
   这不是缺陷：「main 是绿的」这一结论由 promote run 自身承载，它跑的就是
   `ci.yml` 的同构命令集。

### 8.5 发布 tag 与提权的关系

`release` 工作流由 `v*` tag 触发，tag 必须与 `metadata.yaml` 的 `version` 锁步。
推荐顺序：**先 promote，再在 `main` 的 sha 上打 tag**（promote 的 Step Summary
会打印现成命令），这样 tag 指向的提交必定已经过门禁。

---

## 9. Secret 与受保护环境

### `SYNC_PAT`（Settings → Secrets and variables → Actions）

- **用途**：`sync-upstream` 用它推 `sync/standalone-*` 分支 —— 用 PAT 推送的分支
  才会触发 CI required checks，auto-merge（合并到 **`dev`**）才能生效。用
  `GITHUB_TOKEN` 推的分支不触发工作流，PR 只能人工合并（工作流会打印 warning
  并降级）。
- **类型**：Classic PAT，scope 仅 `repo`，绑定维护者个人账号；设过期提醒。
- 一值一 secret，不打包进 JSON（掩码对结构化数据失效）。
- **缺省行为**：不配置也能跑，同步降级为「开 PR + 人工合并」。

> `promote-dev-to-main` **不需要** `SYNC_PAT`：它推 `main` 用的是内置
> `GITHUB_TOKEN`（`contents: write`），代价是推上去后不触发后续工作流——
> 门禁已在 promote job 内跑过（8.4）。

### 受保护环境 `release`（Settings → Environments）

- 名称必须为 `release`（`release.yml` 引用）。
- Required reviewers：勾选维护者本人 —— 发布物（zip + SLSA attestation）
  出炉前强制人工放行。
- 不配置任何环境 secret（当前无发布型 secret）。

---

## 10. 权限基线与 action 锁定

- **Workflow permissions**：Settings → Actions → General 设为
  **Read repository contents and packages permissions**（默认只读）。
  工作流内所有写权限均在 job 级显式声明并附注释
  （zizmor `undocumented-permissions` 审计通过）。
- 全部第三方 action 以 **commit SHA** 锁定（`unpinned-uses` 0 findings）：

  | Action | 版本 | SHA |
  | --- | --- | --- |
  | `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
  | `actions/setup-python` | v7.0.0 | `5fda3b95a4ea91299a34e894583c3862153e4b97` |
  | `actions/attest` | v4.2.2 | `1e69f48acb82d1966a394da916b4c1698aa569d6` |

  升级 action 时：改到新 tag 并更新 SHA，走 PR 人工审阅。

- **工作流清单**（`.github/workflows/`）：

  | 文件 | 触发 | 作用 |
  | --- | --- | --- |
  | `ci.yml` | PR（任意 base）+ push 到 `dev` | 三道 required checks |
  | `release.yml` | tag `v*` | 构建 zip + SLSA attestation + 发布 |
  | `sync-upstream.yml` | 每日 cron + 手动 | 滚动同步上游快照 → PR(base=dev) |
  | `main-pr-target-guard.yml` | PR(base=main) | 拒绝打到 `main` 的 PR（见 8.2） |
  | `promote-dev-to-main.yml` | push 到 `dev`（`[promote]` 前缀）+ 手动 | 门禁后把 `main` 快进到 `dev` |

---

## 11. 发布与回滚

### 发布

`release` 工作流由 `v*` tag 触发：确定性测试 → 构建 zip（**显式白名单**）→
`actions/attest` 生成 SLSA provenance → 受保护环境人工放行 → `gh release create`。

推荐顺序：**先在 `dev` 上验证 → promote 到 `main` → 在 `main` 的 sha 上打 tag**。
这样 tag 指向的提交必定经过 promote 的门禁（8.4），且 `main` 不会落后于发布物。

tag 必须与 `metadata.yaml` 的 `version` 锁步（工作流会校验并拒绝不一致）。
版本号直接沿用上游 `nonebot-plugin-parser-lite`，由 sync PR 自动跟随，
**不要手工 bump**。

白名单与 `main.py` 桥接导入闭包的一致性由 `tests/test_release_manifest.py`
机械钉扎 —— 新增桥接模块忘了登白名单，该测试会先红，而不是发布 zip 装载时
`ModuleNotFoundError`。

### 回滚

```
revert 最近的 sync PR（base=dev）
  → 以最近 release tag（v*）为 LKG 重新发布
```

`revert-first`：先恢复绿，再排查坏因。

回滚**不要求**改写 `main`：`main` 只由 promote 推进，把修复合回 `dev` 后重新
promote 即可。已发布的 tag 永不移动（`--verify-tag`），故障版本的 zip 与
SLSA attestation 保留在 Release 里可追溯。

---

## 12. 运维速查

```bash
# 看失败日志
gh run view <run_id> --log-failed
# 手动重跑同步（绕过熔断）
gh workflow run sync-upstream -f force=true
# 停用滚子
gh workflow disable sync-upstream
# 提权 dev → main（预演 / 实际）
gh workflow run promote-dev-to-main -f dry_run=true
gh workflow run promote-dev-to-main
```

**熔断**：`sync-upstream` 连续 3 次失败 → cron 自动空转（读 run 历史判定，
不回写文件）。修复后 `force=true` 手动恢复。

**staleness**：上游已领先但上次成功同步距今超过 2 个同步周期 → 自动开 issue；
追平后自动关闭。

**keepalive**：GitHub 对 60 天无活动的仓库自动停用 scheduled workflow。
`sync-upstream` 在「上游无变化」时会检查仓库最近提交时间，超过 50 天自动推一个
空 keepalive 提交（`[skip ci]`）到 **`dev`**（不是 `main`——`main` 只由 promote
推进）；推送失败则降级为开提醒 issue。

**daily roll 与 promote 的关系**：`sync-upstream` 每日把新快照以 PR 形式合入
`dev`（分层 automerge 可用时自动合）。**它不会自动改 `main`** —— 何时发布由人
决定：`gh workflow run promote-dev-to-main`。故意如此：上游更新与对外发布是两件
节奏不同的事。

---

## 13. 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
