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
git switch dev            # 工作分支（也是默认分支）；打到 main 的 PR 会被服务端分支保护拒绝

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
| `dev` | **工作分支 + 默认分支**。所有 PR（含 `sync-upstream` 的 roll PR）以此为 base | 合并 PR；`sync-upstream` 的 roll PR 与 keepalive | ❌ 禁止 |
| `main` | **发布指针**。只承载「已通过全量门禁、可发布」的快照 | **仅** `promote-dev-to-main` 工作流 | ❌ 禁止 |
| `lkg` | 回滚锚点。每次 roll 前留档上一已验证快照 | `sync-upstream`（每次覆写） | ✅ 允许 force（语义即覆写） |

**贡献者请一律向 `dev` 开 PR。** 打到 `main` 的 PR 会被**服务端分支保护直接拒绝**
（不是靠某个 CI 判红——见 8.2）。

> 默认分支是 `dev` 而非 `main`，理由是**权威版本就是 `dev`**。
> 这个设置还顺带消除了一整类坑：`schedule`（cron）与不带 `--ref` 的
> `workflow_dispatch` 都读**默认分支**上的定义，默认分支设为 `dev` 后，
> 改了定时工作流**立即生效**，无需等一次 promote。

### 8.2 强制力在服务端 —— 分支保护

**本仓库是 public。** public 仓库在 GitHub Free 计划下可用完整的分支保护
（private 则不可用：`/branches/main/protection` 与 `/rulesets` 会返回
`403 Upgrade to GitHub Pro`）。

`main` 的保护配置**期望值**记在 `doc/BRANCHING.md`，由
`scripts/apply_branch_protection.sh` 幂等应用：

| 配置 | 值 | 含义 |
|---|---|---|
| `enforce_admins` | `true` | **管理员也不能绕过**——这是本设计的核心承诺 |
| `required_status_checks` | 4 个必需检查 | 见 8.3 |
| `strict` | `true` | 分支必须最新才能合并 |
| `allow_force_pushes` / `allow_deletions` | `false` | `main` 历史不可改写、不可删除 |
| `required_linear_history` | `true` | 与快进提权模型一致 |

纵深防御仍有另外两层（**保留**，但已不是主要防线）：

| 层次 | 机制 | 拦截什么 |
|---|---|---|
| 服务端（**主**） | 分支保护 + 必需检查 | 直推 `main`、绕过检查的合并 |
| `promote-dev-to-main` 的 job 内门禁 | promote 前自带 lint / mypy / vendor 校验 / 全量 pytest | 把未验证的快照推上 `main`（**不可逆动作前的独立复算**） |
| 本地 `pre-push` 钩子（`scripts/no-force-push-main.sh`） | 拒绝对 `main`/`dev` 的非快进推送 | 就地改写历史的误操作（**可被 `--no-verify` 绕过**，只作快速反馈） |

> **服务端配置会漂移。** 它可以被人在 Settings 上随手点掉，且**不留任何代码
> 痕迹**——Git 有历史和 review，配置没有。所以
> `.github/workflows/protection-audit.yml` 每日巡检实际配置是否仍等于期望值，
> 发现漂移就开 `protection drift` issue。**这是本方案唯一新增的「守护配置的
> 配置」**，不要删。

### 8.3 必需检查（required checks）

分支保护上的必需检查**四条**（名字必须与 job `name` 逐字一致）：

- `lint (ruff / actionlint / zizmor)`
- `typecheck (mypy)`
- `test (pytest + vendor verify)`
- `analyze (python)` ← CodeQL，public 后免费可用（private 需 GHAS）

> ⚠️ **改名是最容易踩的坑。** context 指的是 job 的 `name`（不是文件名、
> 不是 job id、不是 workflow name）。写错的后果**不是报错**，而是 PR 永久停在
> `Expected — Waiting for status to be reported`。改任何 job 名时，必须同步改
> **四处**：`doc/BRANCHING.md`、`scripts/apply_branch_protection.sh`、
> `protection-audit.yml`、以及本节。`tests/test_branch_model.py` 会断言前三处一致。

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
  | `anchore/sbom-action` | v0.24.2 | `3ad7283483fc7af8ff2b4ea19663c2d5ca935e26` |

  升级 action 时：改到新 tag 并更新 SHA，走 PR 人工审阅。依赖升级 PR 由
  Dependabot 每周开（target-branch = `dev`，见第 13 节）。

- **依赖安装只有一处实现**：`.github/actions/setup-env/`（复合 action）。
  新增 job 需要装依赖时委派给它，不要手抄清单 —— 见第 13.1 节。

- **工作流清单**（`.github/workflows/`）：

  | 文件 | 触发 | 作用 |
  | --- | --- | --- |
  | `ci.yml` | PR（任意 base）+ push 到 `dev` | 三道 required checks：`lint` / `typecheck` / `test` |
  | `codeql.yml` | PR（任意 base）+ push 到 `dev` + 每周一 cron | 第四道 required check `analyze (python)`：CodeQL SAST |
  | `promote-dev-to-main.yml` | push 到 `dev`（`[promote]` 前缀）+ 手动 | 门禁后把 `main` 快进到 `dev`；推完断言分支保护仍在位 |
  | `release.yml` | push tag `v*` | 构建 zip + Syft SBOM + SLSA attestation（provenance 与 SBOM 各一）→ 证明落成 release 附件 → **等 `release` 环境人工放行** → 发布 |
  | `sync-upstream.yml` | 每日 cron（`17 17 * * *`）+ 手动 | 滚动同步上游快照 → PR(base=dev) |
  | `protection-audit.yml` | 每日 cron（`41 5 * * *`）+ 手动 | **巡检服务端强制力是否漂移**（分支保护 / 必需检查 / secret scanning / Dependabot / 环境 reviewer / 工作流文件齐全） |

  依赖关系（谁等谁）：

  ```
  PR → dev ──ci.yml + codeql.yml（必过）──┬── promote-dev-to-main.yml ──→ main ──→ tag v* ──→ release.yml
                                          └──（PR 合入 dev 后）sync-upstream.yml 的产出也走同一条 ci 门

  protection-audit.yml ──（独立，每日巡检强制力配置）
  ```

  `main` 不被任何工作流直接推：唯一的写入者是 `promote-dev-to-main.yml`。
  **服务端分支保护**（不是某个工作流）保证没有 PR 能以 `main` 为 base、
  也没有人能直推 `main`。
  注意 **用 `GITHUB_TOKEN` 推的提交不会再触发工作流**，所以 promote 自己也带
  全套门禁，而不是「推完等 CI」。

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
# 手动重跑同步（绕过熔断）——**必须带 --ref dev**，理由见 12.2
gh workflow run sync-upstream --ref dev -f force=true
# 停用滚子
gh workflow disable sync-upstream
# 提权 dev → main（预演 / 实际）——**必须带 --ref dev**，理由见 12.2
gh workflow run promote-dev-to-main --ref dev -f dry_run=true
gh workflow run promote-dev-to-main --ref dev
```

**熔断**：`sync-upstream` 连续 3 次失败 → cron 自动空转（读 run 历史判定，
不回写文件）。修复后 `force=true` 手动恢复。

**keepalive**：GitHub 对 60 天无活动的仓库自动停用 scheduled workflow。
`sync-upstream` 在「上游无变化」时会检查仓库最近提交时间，超过 50 天自动推一个
空 keepalive 提交（`[skip ci]`）到 **`dev`**（不是 `main`——`main` 只由 promote
推进）；推送失败则降级为开提醒 issue。

**daily roll 与 promote 的关系**：`sync-upstream` 每日把新快照以 PR 形式合入
`dev`（分层 automerge 可用时自动合）。**它不会自动改 `main`** —— 何时发布由人
决定：`gh workflow run promote-dev-to-main`。故意如此：上游更新与对外发布是两件
节奏不同的事。

### 12.1 告警 issue 的生命周期

`sync-upstream` 只有两类自动开 issue 的告警，**两类都会在问题解决后自动关闭**：

| 标题前缀 | 含义 | 开启条件 |
| --- | --- | --- |
| `sync-upstream roll failed` | 本轮 run 自身失败（vendor / 注入 / 契约测试红） | `failure()`，带排查 playbook |
| `upstream lagging` | 上游已领先，但上次成功同步距今超过 `STALENESS_HOURS`（48h） | 变化已检出却长期追不平 |

关闭由 **第 16 步「告警对账」**（`id: reconcile`，`if: always() && breaker 未 skip`）
统一负责。它先给本 run 算一个**健康度**，健康才关闭：

- **不健康**（保守判定，宁可漏关不可误关）：任一关键步骤（`detect` / `roll_pr` /
  `roll`）结果为 `failure` 或 `cancelled`；或 `job.status` 为 `failure` /
  `cancelled`。
- **健康**：上述都不成立。注意 **`skipped` 与「上游无变化空退出」都算健康** ——
  幂等收敛的 run 本来就不该动告警。

健康时，按标题前缀查 `state:open` 并逐条 `gh issue close` + 留言指认关闭它的
`run_id`；单条关闭失败只发 `::warning::`，留待下一轮对账（不会因一个 API 抖动
把整轮 run 判红）。

**先关后开**：对账（第 16 步）刻意排在两个开 issue 的步骤（17 / 18）**之前**，
否则本轮新开的告警会被同一轮立刻关掉。

**为什么以前不自动关**（已修）：旧实现把「关闭」写在 `roll_pr` 的**成功分支**
里，而告警恰恰只在失败分支产生 —— 关闭代码永远不可达；`roll failed` 这类更是
压根没有关闭路径。两处叠加后，告警一旦开出就是**永久 OPEN**（仓库里同时出现过
2 条重复的 `upstream lagging`，因为创建侧也没有去重）。现在创建侧已加去重守卫
（开前先查同名 open issue），关闭侧改由对账统一负责。

**人工处置**：确属误报可直接关；下次成功 run 的对账不会重开。若想强制刷新，
`gh workflow run sync-upstream -f force=true` 会跳过熔断跑一轮完整对账。

### 12.2 不同事件读不同 ref 的 workflow 定义

**默认分支是 `dev`**，而不同事件读的是不同 ref 上的 workflow 定义。
把这张表记牢，本节的其余内容都是它的推论：

| 事件 | 读哪个 ref 的定义 | 文件必须在默认分支上？ |
| --- | --- | --- |
| `push` | **被推送的那个 ref** | 否（文档明说含未合入默认分支的工作流） |
| `pull_request` | **PR 的合并提交**（head 并入 base 的结果） | 否 |
| `schedule` | **默认分支的最新提交** | **是** |
| `workflow_dispatch`（不带 `--ref`） | **默认分支** | **是** |
| `workflow_dispatch --ref X` | **`X`** | 是（须先满足上一条） |

**默认分支设为 `dev` 带来的好处**（这是本次重构的一个有意选择）：
`schedule` 与无 `--ref` 的 dispatch 都读**默认分支**，而 `dev` 正是改动的落点。
于是「改了定时工作流、cron 却仍跑旧版本」这个坑**直接消失**——
改动 push 到 `dev` 就生效，不必等一次 promote。

（在旧的 `main`=默认分支方案下，这里有整整一类坑：`schedule` 读 `main` 的旧定义，
改动必须 promote 之后才对 cron 生效，极易被误判成「改动无效」。
把默认分支改成 `dev` 后，这类困惑不再存在。）

**仍需注意的一点**：`pull_request` 读的是**合并提交**的定义。
所以「PR 的 head 侧新增/修改的 workflow」在本 PR 上就能生效（合并提交含它），
这是期望的行为。

**手动触发一律带 `--ref dev`**（除非你明确想跑某个别的 ref）：

```bash
gh workflow run sync-upstream --ref dev -f force=true
gh workflow run promote-dev-to-main --ref dev -f dry_run=true
gh workflow run protection-audit --ref dev
```

### 12.3 各分支上有哪些工作流

工作流文件是**按分支存的**，某个分支上能不能跑某个工作流，先看该分支有没有这个文件：

| 工作流文件 | `dev` | `lkg` | `main` |
| --- | :-: | :-: | :-: |
| `ci.yml` | ✓ | ✓ | ✓ |
| `codeql.yml` | ✓ | ✓ | ✓ |
| `promote-dev-to-main.yml` | ✓ | ✓ | ✓ |
| `release.yml` | ✓ | ✓ | ✓ |
| `sync-upstream.yml` | ✓ | ✓ | ✓ |
| `protection-audit.yml` | ✓ | ✓ | ✓ |

**六个文件在每个长期分支上都齐全。** 这是 public 重构带来的一个附带改善：
旧方案里「PR 目标守卫」与 promote 两个工作流只在 `dev` / `lkg` 上，导致
「默认分支缺文件 → 无 ref 的 dispatch 判 422」这类坑；现在默认分支是 `dev`，
且首次 push 时就建好全部三个长期分支，不再有「文件不在默认分支上」的状态。
（若某天发现默认分支缺某个工作流文件，那就是一次**部署事故**——
`protection-audit.yml` 会把它报出来。）

再叠加各工作流的 `on:` 过滤器，得到「实际会跑」的矩阵：

| 工作流 | 触发 | `dev` | `main` | 其他分支 / tag |
| --- | --- | :-: | :-: | --- |
| `ci.yml` | PR（任意 base） | — | — | **任何 PR 都跑**（读合并提交） |
| `ci.yml` | push | **✓** | ✗ | ✗（过滤器只认 `dev`） |
| `codeql.yml` | PR（任意 base） | — | — | **任何 PR 都跑** |
| `codeql.yml` | push | **✓** | ✗ | ✗ |
| `codeql.yml` | cron（周一 04:23） | **✓**（默认分支即 `dev`） | ✓（旧定义，但同文件） | ✗ |
| `promote-dev-to-main.yml` | push | **✓** | ✗ | ✗ |
| `promote-dev-to-main.yml` | 手动 | ✓ | ✓ | 需 `--ref` |
| `release.yml` | push tag `v*` | ✓ | ✓ | **任何 ref 的 tag** |
| `sync-upstream.yml` | cron（每日 17:17） | **✓**（默认分支即 `dev`） | ✓ | ✗ |
| `sync-upstream.yml` | 手动 | ✓ | ✓ | 需 `--ref` |
| `protection-audit.yml` | cron（每日 05:41） | **✓**（默认分支即 `dev`） | ✓ | ✗ |

三条要点：

1. **`ci.yml` 的 push 触发只认 `dev`**，所以 `main` 上永远不会因 push 而跑 CI
   —— 这不是缺陷，而是刻意为之（`GITHUB_TOKEN` 推的提交本就不触发工作流，
   把 `main` 留在列表里只会制造「main 有 CI 保护」的错觉）。
2. **`lkg` 上虽然文件都在，但它不该被用来跑任何东西** —— 它是回滚锚点，
   不是工作分支。`lkg` 存在是为了 `checkout` 出「上一已验证快照」。
3. **cron 读的是默认分支（`dev`）的定义** —— 而 `dev` 正是改动落点，
   所以改动**立即对 cron 生效**，无需等 promote（见 12.2）。

---

## 13. 供应链与合规基线

本仓库是**私有仓库 + Free 计划**，平台侧的供应链防线大多不可用（见 13.4）。
所以这一节列的每一条，都必须是「写进仓库、由 CI 与本地门禁强制」的东西。
条款来源与落地位置的对照如下，**机械钉扎在 `tests/test_supply_chain.py`**：

| 标准条款（来源） | 落地位置 | 钉扎 |
| --- | --- | --- |
| 第三方 action 钉完整 commit SHA（GitHub Secure use；Scorecard `Pinned-Dependencies`） | 全部 `uses:` | `test_every_third_party_action_is_pinned_to_a_full_commit_sha` |
| 钉扎行保留 `# vX.Y.Z` 注释（Dependabot 依赖它更新版本文档） | 同上 | `test_pinned_actions_keep_a_version_comment_for_dependabot` |
| 顶层 `permissions` 只读、写权限下放 job 级（Scorecard `Token-Permissions` 满分口径） | 6 个工作流 | `test_workflows_declare_top_level_read_only_permissions` |
| 不用 `pull_request_target` / `workflow_run` 检出不可信代码（Scorecard `Dangerous-Workflow`，Critical） | 6 个工作流 | `test_workflows_avoid_dangerous_triggers` |
| 依赖更新工具（Scorecard `Dependency-Update-Tool`） | `.github/dependabot.yml` | `test_dependabot_covers_actions_and_hand_maintained_python_deps` |
| 依赖更新冷却期（zizmor `dependabot-cooldown`，置信度 High） | 同上 `cooldown` 段 | 同上 |
| 安全政策（Scorecard `Security-Policy`，按其三档评分写全） | `.github/SECURITY.md` | `test_security_policy_satisfies_every_scorecard_scoring_tier` |
| 代码所有者约束工作流变更（GitHub Secure use） | `.github/CODEOWNERS` | `test_codeowners_covers_the_supply_chain_surface` |
| 发布产物带签名/证明（Scorecard `Signed-Releases`；**后缀 `.intoto.jsonl` 才是满分档**） | `release.yml` 归档 `*.intoto.jsonl` | `test_release_attaches_provenance_bundle_to_the_release` |
| 发布 SBOM（Scorecard `SBOM`；NTIA/CISA 最低要素） | `release.yml`（Syft → SPDX） | `test_release_generates_and_attests_an_sbom` |
| 复用 CI 逻辑而非多处手抄（GitHub 官方推荐 composite action） | `.github/actions/setup-env/` | `test_every_delegating_workflow_uses_the_shared_setup_action` |
| **SAST**（Scorecard `SAST`，只认 `github/codeql-action` 或 SonarCloud） | `codeql.yml` | `test_codeql_fills_the_sast_gap_that_private_plan_left` |
| **分支保护**（Scorecard `Branch-Protection`） | 服务端配置，期望值见 `doc/BRANCHING.md` | `test_protection_script_enforces_admins_and_reads_back` |
| **环境人工放行**（GitHub environments） | `release.yml` 的 `environment: release` | `test_release_environment_is_a_real_gate_for_public_repos` |
| **强制力不漂移**（本仓库独有，见 13.4） | `protection-audit.yml` | `test_protection_audit_covers_every_enforcement_surface` |

### 13.1 依赖安装只有一处定义

`.github/actions/setup-env/action.yml` 是**唯一**的依赖安装实现，四个工作流的
五个 job 全部委派给它。三个输入：

| 输入 | 取值 | 用途 |
| --- | --- | --- |
| `astrbot` | `true`（默认）/ `false` | 是否以 `--no-deps` 装宿主框架 |
| `test-reqs` | `true`（默认）/ `false` | 是否装 `tests/requirements-test.txt` |
| `extras` | `none`（默认）/ `type` / `gate` | `type` = mypy + types-qrcode；`gate` = ruff + mypy + types-qrcode |

**为什么必须收成一处**：2026-09-19 的事故复盘 —— 同一份清单在 4 个工作流里
各抄一遍，`promote` 那份漏了 ruff，门禁第一步 `lint（ruff check）` 以
`No module named ruff` 假红。那是**环境缺失、不是代码问题**，却把整条提权通道
锁死（门禁过不了 → `main` 永远推不动）。断言因此从「逐包检查」改成「必须委派
给唯一实现」+「不得再手抄清单」，两条互补。

**ruff 版本不手抄**：`extras=gate` 时版本由 `scripts/pinned_tool_versions.py`
从 `config/.pre-commit-config.yaml` 的 `ruff-pre-commit` rev 推导。原因见
`ci.yml` 的 lint job —— 它走 pre-commit 的隔离环境，ruff 版本由 `rev:` 决定；
而 promote 是裸 `python -m ruff`。两处不一致会让同一个文件「一边判过、一边判
不过」。单一来源是唯一不会漂移的做法。

### 13.2 发布信任链与离线验证

`release.yml` 在 tag `v*` 时产出五个资产：

| 资产 | 说明 |
| --- | --- |
| `astrbot_plugin_parser_lite-<tag>.zip` | 插件包（显式白名单构建） |
| `…<tag>.zip.sha256` | 校验和 |
| `…<tag>.zip.intoto.jsonl` | SLSA provenance（Sigstore bundle） |
| `sbom.spdx.json` | SPDX 软件物料清单 |
| `sbom.spdx.json.intoto.jsonl` | SBOM 的 Sigstore 证明 |

**为什么证明必须落成附件**：`actions/attest` 默认只把证明写进 GitHub 的
attestation store，而 store 要联网查询、且不在 release 资产里。后果有两个：
消费方无法离线验证；供应链扫描器只看 release 资产的**文件名后缀**，于是
「有证明」被读成「没证明」。后缀取 `.intoto.jsonl` 的依据：GitHub 官方离线
验证文档里 bundle 下载下来的扩展名就是 `.jsonl`（内容为单行 JSON，即合法
JSONL），而 SLSA 生态对 provenance bundle 的约定名是 `<artifact>.intoto.jsonl`。

离线验证：

```bash
gh attestation trusted-root > trusted_root.jsonl
gh attestation verify astrbot_plugin_parser_lite-<tag>.zip \
  -R Chuubururin/astrbot_plugin_parser_lite \
  --bundle astrbot_plugin_parser_lite-<tag>.zip.intoto.jsonl \
  --custom-trusted-root trusted_root.jsonl
```

### 13.3 新增工作流或 action 时的检查单

1. 第三方 action 钉到完整 SHA，同行写 `# vX.Y.Z`；
2. 顶层 `permissions` 只有 `contents: read`，写权限在 job 级声明并注释理由；
3. 需要装依赖就 `uses: ./.github/actions/setup-env`，**不要**手抄清单；
4. 确认 `tests/test_supply_chain.py` 与 `tests/test_branch_model.py` 全绿
   （它们会替你把上面三条机械核一遍）。

### 13.4 平台能力现状：哪些能做、哪些仍做不到

**2026-09-19 重构**：仓库从 private 转为 **public**，平台侧的多数防线随之解锁。
两张表如实分开写，避免两种误判——「以为没做」和「以为做了」。

**public + Free 下现在能做的**（本次新增，属**服务端配置**而非仓库内代码）：

| 能力 | 落点 | 谁能改 | 失效探测 |
| --- | --- | --- | --- |
| 分支保护 + 必需检查 | 服务端 / `scripts/apply_branch_protection.sh` | 仓库 admin | `protection-audit.yml` 每日巡检 |
| 禁止直推 `main`（含管理员） | 同上 `enforce_admins: true` | 同上 | 同上 |
| CodeQL 代码扫描（Scorecard `SAST`） | `codeql.yml` | 代码 | CI required check `analyze (python)` |
| Secret scanning / push protection | 服务端开关 | 仓库 admin | 同上巡检 |
| Dependabot alerts | 服务端开关 | 仓库 admin | 同上巡检 |
| 环境 required reviewers | 服务端 `release` 环境 | 仓库 admin | 同上巡检 |
| 私有安全公告 | 服务端 | 仓库 admin | — |

> ⚠️ **服务端配置会漂移，且不留代码痕迹。** 上面这些可以被人在 Settings 上
> 随手点掉——Git 有历史和 review，配置没有。所以每一条都配了失效探测
> （`protection-audit.yml`）。**这是本方案唯一「守护配置的配置」，不要删**。

**仍做不到 / 未做的**（如实声明，避免以后有人以为「没做 = 忘了」）：

| 标准条款 | 现状 | 原因 / 补偿 |
| --- | --- | --- |
| `CODEOWNERS` **强制** review | ⚠️ 写了但不强制 | 分支保护的 `required_approving_review_count` 刻意取 **0**（单人维护下取 1 会自我死锁——无法批准自己的 PR）。见 `doc/BRANCHING.md` 的决策记录。协作方变多后应提到 1 并重评 `enforce_admins` 与 bypass 的取舍 |
| commit signing / vigilant mode | 未强制 | 需签名 key 分发，单人仓库收益有限；发布侧已由 SLSA attestation 覆盖 |
| 历史泄漏扫描（gitleaks 等） | 未接入 CI | 建仓前手动扫过一次全历史（凭证/大文件/内网地址，均无命中）。列为可选增强 |
| 签名类资产（`.asc` / `.sigstore`） | 未做 | 已用 attestation（`.intoto.jsonl`）满足 Scorecard `Signed-Releases` 的**最高档**，无需再加 |
| 分支保护之外再做 Rulesets | 未做 | `branch protection` 已覆盖需求；Rulesets 是其超集，迁移收益不足以抵消复杂度 |
| 第三方 SAST（Semgrep 等） | 未做 | Scorecard 的 `SAST` 项只认 `github/codeql-action` 或 SonarCloud，加第三方工具拿不到分且增加噪声面 |

### 13.5 已知工具冲突与复评触发条件

- **`$/` vs actionlint**：zizmor 的 `self-repository` 审计（v1.30.0 起）建议把
  仓库内 action 的引用从 `./…` 改成 `$/…`（GitHub 2026-07 引入的语法，不受
  运行时文件系统状态影响，且被平台视作 pinning）。但 **actionlint 最新版
  v1.7.12（2026-03-30）尚不认识 `$/`**，会判 `ref is missing`；而 actionlint 是
  本地与 CI 双端硬门禁。故当前保留 `./…` 并在调用点写**显式行内豁免**
  `# zizmor: ignore[self-repository]`，理由写在 `ci.yml` 里。
  **复评触发条件：actionlint 支持 `$/` 后立刻改回并撤掉豁免。**
- **Dependabot 不覆盖根目录 pip**：`requirements.txt` 与
  `requirements/host-provided.txt` 是 `vendor/_upstream/pyproject.toml` 的派生产物
  （文件头写明「勿手改」），一致性由 `tests/test_requirements_sync.py` 逐字节
  守护。让 Dependabot 改它们，每个 PR 都注定违反派生契约 —— 那是噪声不是防线。
  上游版本漂移的正道是 sync-upstream 滚动 + 重跑 `scripts/derive_requirements.py`。
- **CodeQL 必须排除 `vendor/` 与 `templates/`**：它们是上游快照（零修改铁律：
  只能整树重建，不许就地改），对它们报出的问题我们**改不了**，留在结果里只会
  淹没真问题。已写进 `codeql.yml` 的 `paths-ignore`，并由测试钉住。
- **`github/codeql-action` 的 uses 带子路径**：`codeql-action/init@<sha>` 这种
  形态是合法的（init / analyze 拆成同仓库子路径）。`tests/test_supply_chain.py`
  的钉扎正则在 2026-09-19 因此**误报过**——正则只允许 `owner/repo@sha`。
  已修正为允许任意深度的子路径。**教训：断言的正则必须覆盖平台允许的全部合法
  形态，否则它拦的不是违规，而是「用的形态我没预料到」。**

### 13.6 强制力清单的执行路径（写在最后，但最该先读）

```
1. 建 public 仓库（空仓库，不勾 README）
2. push dev / main / lkg 三个分支  ← 全部 workflow 文件随之就位
3. 设置默认分支为 dev
4. 服务端开启：secret scanning / Dependabot alerts
5. 创建 release 环境 + required reviewers（prevent_self_review 必须关）
6. bash scripts/apply_branch_protection.sh   ← 打分支保护（幂等 + 回读校验）
7. gh workflow run protection-audit --ref dev ← 确认巡检全绿
8. 开一个测试 PR → 确认必需检查阻塞合并
9. 实测 git push origin main → 确认被服务端拒绝
```

第 6 步**必须在第 2 步之后**（要先把文件推上去，必需检查的 context 才有意义）；
第 9 步是唯一能证明「强制力真实存在」的动作 —— 不要跳过。

---

## 14. 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
