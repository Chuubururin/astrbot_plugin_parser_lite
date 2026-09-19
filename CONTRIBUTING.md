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
唯一升级方式是**整树替换**（由 vendoring 流程完成），
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

### 8.1 只有两条长期分支

```
feature 分支 ──PR(base=dev)──▶ dev ──promote-dev-to-main──▶ main ──tag v*──▶ release
                              ↑                              ↑
                        工作分支：所有变更                发布指针：
                        在这里被评审与验证                永远与某个已验证的
                                                          dev 快照是同一个提交
```

| 分支 | 角色 | 谁可以推进 | 历史可否改写 |
|---|---|---|---|
| `dev` | **工作分支 + 默认分支**。所有 PR 以此为 base | 合并 PR；管理员可直推救急 | ❌ 禁止 |
| `main` | **发布指针**。只承载「已通过全量门禁、可发布」的快照 | **仅** `promote-dev-to-main` 工作流 | ❌ 他人禁止（工作流用 lease 移动） |

**贡献者请一律向 `dev` 开 PR。** 打到 `main` 的 PR 会被
`main-pr-target-guard.yml` **判红**（并给出「请把 PR 开向 dev」的提示）。

> 默认分支是 `dev` 而非 `main`，理由是**权威版本就是 `dev`**。
> 这个设置还顺带消除了一整类坑：`schedule`（cron）与不带 `--ref` 的
> `workflow_dispatch` 都读**默认分支**上的定义，默认分支设为 `dev` 后，
> 改了工作流**立即生效**，无需等一次 promote。

### 8.2 强制力在服务端 —— 分支保护

**本仓库是 public。** public 仓库在 GitHub Free 计划下可用完整的分支保护
（private 则不可用：`/branches/main/protection` 与 `/rulesets` 会返回
`403 Upgrade to GitHub Pro`）。

期望值记在 `doc/BRANCHING.md`，由 `scripts/apply_branch_protection.sh` 幂等应用。

**`main`：**

| 配置 | 值 | 含义 |
|---|---|---|
| `enforce_admins` | `true` | **管理员也不能绕过**——这是本设计的核心承诺 |
| `required_status_checks` | 3 个必需检查 | 见 8.3 |
| `required_pull_request_reviews` | **`null`** | ★ **必须不开**，见下方警告 |
| `allow_force_pushes` | `true` | ★ promote 用 `--force-with-lease` 移动指针需要它 |
| `allow_deletions` | `false` | 不可删除 |
| `strict` | `false` | 不要求分支最新（单人维护场景减少摩擦） |

> ### ⚠️ `main` 必须**不开**「Require a pull request before merging」
>
> 这是本模型唯一与通用最佳实践相反的地方，**改了会静默搞坏发布**。
>
> 该选项（API 字段 `required_pull_request_reviews`）会拒绝**所有**直接推送到
> `main` 的 ref 更新 —— **包括 `promote-dev-to-main` 工作流自己的推送**。
> 一旦开启，**Promote 直接失效**，`main` 会永远停在旧位置，而工作流红在
> 「推送被拒」上，看起来像权限问题。
>
> 所以 `main` 的「不可直接修改」是靠 **`enforce_admins: true`** 实现的，
> **不是**靠 PR 保护。
>
> 参考实现 [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma) 的
> `CONTRIBUTING.md` 有同样警告。

**`dev`：**

| 配置 | 值 | 含义 |
|---|---|---|
| `enforce_admins` | `false` | 工作分支：CI 故障时管理员可直推救急 |
| `required_status_checks` | 3 个必需检查 | 同上 |
| `required_approving_review_count` | `0` | 单人维护者无法批准自己的 PR，设 1 会自我死锁 |
| `allow_force_pushes` / `allow_deletions` | `false` | 保护他人的提交 |

纵深防御仍有另外三层（**保留**，但已不是主要防线）：

| 层次 | 机制 | 拦截什么 |
|---|---|---|
| 服务端（**主**） | 分支保护 + 必需检查 + `enforce_admins` | 直推 `main`、绕过检查的合并 |
| `promote-dev-to-main` 的 `decide` job | 只有带保留前缀的提交 / `chore.*` tag / 手动触发才 promote | `dev` 的每次普通推进都去改 `main` |
| `main-pr-target-guard.yml` | 拒绝以 `main` 为 base 的 PR | 在 `main` 上造出 `dev` 没有的提交 |
| 本地 `pre-push` 钩子（`scripts/no-force-push-main.sh`） | 拒绝对 `main`/`dev` 的非快进推送 | 就地改写历史的误操作（**只作快速反馈**） |

> ### ⚠️ 服务端配置没有自动巡检（已知缺口）
>
> 分支保护可以被人在 Settings 上**随手点掉，且不留任何代码痕迹**——Git 有
> 历史和 review，配置没有。
>
> 本方案为了「更简单」**撤掉了**每日巡检工作流，**所以这个缺口是打开的**。
> 如实声明比假称「已有巡检」好——后者会让维护者放弃手动确认。
>
> **缓解**：若保护被关掉且有人直推 `main`，`main` 会多出 `dev` 没有的提交，
> 于是**下一次 promote 会响亮失败**并列出那些提交。这能在下次 promote 时
> 暴露问题（不是实时）。
>
> **修复**：改动仓库 Settings 后重放
> `bash scripts/apply_branch_protection.sh` 即可恢复。

### 8.3 必需检查（required checks）

分支保护上的必需检查**三条**（名字必须与 job `name` 逐字一致）：

- `lint (ruff / actionlint / zizmor)`
- `typecheck (mypy)`
- `test (pytest + vendor verify)`

> ⚠️ **改名是最容易踩的坑。** context 指的是 job 的 `name`（不是文件名、
> 不是 job id、不是 workflow name）。写错的后果**不是报错**，而是 PR 永久停在
> `Expected — Waiting for status to be reported`。改任何 job 名时，必须同步改
> **四处**：`doc/BRANCHING.md`、`scripts/apply_branch_protection.sh`、
> `tests/test_branch_model.py` 的 `REQUIRED_CHECKS`、以及本节。
> 测试会断言这几处一致。

「确定性三类契约」（import 面 AST 白名单 / API 签名快照 / requirements 派生一致性）
包含在 `test` job 中；网络隔离区快照（`network` 标记）默认跳过，不作为 required。

`push` 触发只跟 `dev`：`main` 由 promote 工作流推进，而 `GITHUB_TOKEN` 推的提交
**不触发**工作流——所以 `main` 上不会出现新的 CI 运行。这是刻意的：
`main` 与 `dev` 是同一个提交，`dev` 上的 CI 已经跑过（8.4）。

### 8.4 指针移动：`promote-dev-to-main`

```bash
# 方式 A（推荐）：向 dev 推一个**以保留前缀开头**的提交，自动触发
git commit --allow-empty -m "[merge] release v1.3.7"
git push origin dev

# 方式 B：打 chore.* tag（与代码提交解耦）
git tag chore.merge-20260919 && git push origin chore.merge-20260919

# 方式 C：手动触发（应急）
gh workflow run promote-dev-to-main --ref dev
```

> ⚠️ **`[merge]` 与 `chore(release):` 是保留前缀，贡献者不得使用。**
> 它们是**锚定行首**的匹配：`[merge] fix: xxx` 触发，`fix: xxx [merge]` 不触发。
> 在自己的提交信息里用这两个前缀会**意外触发发布**，把 `dev` 当成发布点。

流程：**判定是否该 promote** → 校验 `main` 是 `dev` 的祖先（可快进）→
`git push --force-with-lease` 把 `main` **移动**到 `dev` 的 sha。

三个刻意的设计决定：

1. **指针移动，不产生 merge commit。** 因此 `main` 上每个提交都逐字节等于
   `dev` 上某个已过 CI 的提交，两者可直接比对。若 `main` 存在 `dev` 没有的
   提交（有人绕过工作流直推），工作流**响亮失败**并列出那些提交——不做静默
   merge，因为那会把一个 `dev` 从未验证过的合并提交送进发布线。
2. **`--force-with-lease` 钉住校验过的 sha。** 若有人在校验与推送之间动了
   `main`，推送被拒而不是抹掉对方的提交。
3. **`main` 上不会出现新的 CI 运行。** `GITHUB_TOKEN` 推的提交不触发工作流。
   这不是缺陷：`main` 与 `dev` 是同一个提交，「`main` 是绿的」这一结论由
   **`dev` 上那次 CI** 承载（`ci.yml` 的 `push: dev`），不是由 promote 承载 ——
   promote 只做 ref 手术，不装依赖、不跑门禁，因此它本身不会产生任何测试信号。

### 8.5 发布 tag 与 promote 的关系

`release` 工作流由 `v*` tag 触发，tag 必须与 `metadata.yaml` 的 `version` 锁步。
推荐顺序：**先 promote，再在 `main` 的 sha 上打 tag**，这样 tag 指向的提交必定
就是 `dev` 上那个跑过全量 CI 的提交（逐字节相同）。

> 顺序不能反：**先在 `dev` 上把 CI 跑绿 → promote → 打 tag**。
> 若先打 tag，`release` 会构建出一个 `main` 尚未跟上的提交对应的产物；
> 若 `dev` 的 CI 没绿就 promote，`main` 会指向一个未经完整验证的提交，
> 而 promote 不会再帮你拦一次（见 §8.4）。

> `chore.*` tag 只触发 promote；`v*` tag 只触发 release。两者互不干扰——
> 这是把「推进指针」与「发版」解耦的关键。

## 9. Secret 与受保护环境

### 本版**不需要**任何 Secret

`promote-dev-to-main` 推 `main` 用的是内置 `GITHUB_TOKEN`，
`release` 用 `id-token` + `attestations`（OIDC，无需长期凭据）。
所以本仓库在 Settings → Secrets 里**不需要配置任何东西**。

> 上一版有一个 `SYNC_PAT`，用来支持已移除的 `sync-upstream` 工作流
> （它需要 PAT 才能让推送的分支触发 CI 检查）。本版没有自动上游同步，
> 因此该 Secret 不再需要。若将来恢复上游同步，再一并恢复它。

- **类型**：Classic PAT，scope 仅 `repo`，绑定维护者个人账号；设过期提醒。
- 一值一 secret，不打包进 JSON（掩码对结构化数据失效）。
- **缺省行为**：不配置也能跑，同步降级为「开 PR + 人工合并」。

</details>

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
  | `main-pr-target-guard.yml` | PR base=`main` | 判红并提示「请把 PR 开向 dev」 |
  | `promote-dev-to-main.yml` | push 到 `dev`（保留前缀）+ `chore.*` tag + 手动 | 把 `main` **指针移动**到 `dev` 尖端 |
  | `release.yml` | push tag `v*` | 构建 zip + Syft SBOM + SLSA attestation → 证明落成 release 附件 → 发布 |

  **本版只有四个工作流**（用户要求「更简单」）。上一版的 `codeql.yml`、
  `protection-audit.yml`、`sync-upstream.yml` 已移除，理由记在
  `doc/BRANCHING.md` §10「明确不做的项」。

  依赖关系（谁等谁）：

  ```
  PR → dev ──ci.yml（必过）──┬── promote-dev-to-main.yml ──→ main ──→ tag v* ──→ release.yml
                             │
  PR(base=main) ──main-pr-target-guard.yml（判红，拦住错误方向）
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
因为 promote 只是**移动指针**（§8.4），tag 指向的提交就是 dev 上那个已经跑过
全量 CI 的提交 —— 逐字节相同，且 `main` 不会落后于发布物。

> ⚠️ **不要把「promote 会再校验一遍」当成安全网。** 本版的 promote **不跑任何
> 门禁**（它只做 `git fetch` / `git cherry` 判定 / `git push`）。这是刻意的简化：
> 同一个提交在 dev 上已经验证过，再验一遍只增加「runner 抖动 → 假红」的失败面。
> 结果是：**发布物是否可信，完全取决于 dev 上那次 CI**。所以绕过 dev 直接往
> `main` 写（保护会拦，但若被改动过配置就不一定）等于绕过全部验证。

tag 必须与 `metadata.yaml` 的 `version` 锁步（工作流会校验并拒绝不一致）。
版本号沿用上游 `nonebot-plugin-parser-lite` 的对应版本。
本版没有自动上游同步，所以升级 vendor 快照时需要手工确认版本号与上游一致。

白名单与 `main.py` 桥接导入闭包的一致性由 `tests/test_release_manifest.py`
机械钉扎 —— 新增桥接模块忘了登白名单，该测试会先红，而不是发布 zip 装载时
`ModuleNotFoundError`。

### 回滚

```
在 dev 上 git revert <bad-commit>  →  重新 promote  →  打新的 patch 版本 tag
```

`revert-first`：先恢复绿，再排查坏因。

**三条不可能碰的东西**：

1. **不改写 `main`** —— 它只由 promote 推进，把修复合回 `dev` 后重新 promote 即可。
2. **不移动已发布的 tag**（永不 `git tag -f` / `--force` 重推）——
   故障版本的 zip 与 SLSA attestation 保留在 Release 里，供追溯与降级下载。
3. **不删除分支** —— 回滚不需要新建或删除任何分支，`dev` 与 `main` 两个就够。

**回滚锚点是 `v*` tag，不是分支。** tag 在 GitHub 上是不可变引用
（要移动必须显式删了重建，那本身就是个显式动作）；而分支可以被 force push。
所以「上一已验证快照」这件事由 tag 承载，不需要额外的 `lkg` 分支。

---

## 12. 运维速查

```bash
# 看失败日志
gh run view <run_id> --log-failed

# 提权 dev → main（本版只有这一条路径）
gh workflow run promote-dev-to-main --ref dev

# 重新应用分支保护（配置被误改后）
bash scripts/apply_branch_protection.sh --dry-run
bash scripts/apply_branch_protection.sh
```

> **为什么都带 `--ref dev`**：默认分支已是 `dev`，不带 `--ref` 也能工作。
> 显式写出来是为了避免「默认分支被改回 `main` 时静默跑旧定义」——
> 那个失败形态极难排查（见 12.2）。

**发布（两步）**：

```bash
# 1) promote：推一个以保留前缀开头的提交到 dev
git checkout dev && git pull
git commit --allow-empty -m "[merge] release v1.2.0"
git push origin dev

# 2) 在 promote 出来的 main sha 上打 tag
git fetch origin
git tag v1.2.0 origin/main
git push origin v1.2.0
```

**回滚**：见第 11 节。要点是**不改写 `main`，不改写 tag** ——
`git revert` 到 `dev`，重新 promote，打新的 patch 版本号。

### 12.1 没有自动告警 issue

上一版的 `sync-upstream` 会开两类告警 issue（`upstream lagging` /
`sync-upstream roll failed`）并自动对账关闭。**本版没有 `sync-upstream`，
因此没有自动告警 issue。**

替代手段：

- 工作流失败会发到 **GitHub 的通知**（你订阅了该仓库就会收到邮件/站内通知）
- 分支持续失败可以在 **Actions 页面**按分支筛选查看

> 这是「更简单」的一个代价：**没有聚合成 issue 的告警面板**。
> 若后来觉得需要，恢复 `sync-upstream` 的工作量不小（它依赖 `lkg` 分支、
> `SYNC_PAT`、以及一整套告警生命周期逻辑），建议先评估是否真的需要。

### 12.2 不同事件读不同 ref 的 workflow 定义

**这是最容易踩、最难排查的一类坑**，务必理解。

| 事件 | 读哪个 ref 上的工作流定义 | 文件必须在默认分支上？ |
| --- | --- | --- |
| `push` | 被推送的那个 ref | 否 |
| `pull_request` | PR 的合并提交（head 并入 base） | 否 |
| `schedule`（cron） | **默认分支的最新提交** | **是** |
| `workflow_dispatch`（带 `--ref X`） | `X` | 是（须先满足上一条） |
| `workflow_dispatch`（不带 `--ref`） | **默认分支** | **是** |

**本仓库的默认分支是 `dev`。** 这个选择的直接收益：

- 你改了 `dev` 上的工作流 → **cron 与手动触发立即用新定义**，无需等 promote
- 上一版默认分支为 `main` 时，改了 `dev` 上的定时工作流但 cron 仍跑 `main` 上的
  旧版本 —— 表现为「我明明改了，为什么行为没变」，且 run 的 `headSha` 显示的是
  `main` 的 sha，极易误判

**实务建议**：手动触发时**总是显式带 `--ref dev`**，不要依赖默认分支。
这样即使有人改了默认分支，你的命令行为也不变。

### 12.3 各分支上有哪些工作流

工作流文件**按分支存储**。`main` 是从 `dev` 移动过去的，所以两边文件必然一致
（同一批提交）。下表描述的是**当前状态**：

| 工作流文件 | `dev` | `main` |
| --- | :-: | :-: |
| `ci.yml` | ✓ | ✓ |
| `main-pr-target-guard.yml` | ✓ | ✓ |
| `promote-dev-to-main.yml` | ✓ | ✓ |
| `release.yml` | ✓ | ✓ |

**首次推送时的特例**：仓库刚建好、`main` 还指向初始提交时，
`main` 上可能**缺**那几个随分支模型引入的工作流（因为初始提交是它们之前的）。
这是「尚未首次 promote」的正常状态 —— 跑一次 promote 之后两边就一致了。

**事件矩阵**（哪个事件在哪个分支上真的会跑）：

| 工作流 | 事件 | `dev` | `main` | 其他分支 |
| --- | --- | :-: | :-: | --- |
| `ci.yml` | `pull_request` | — | — | **✓（任何 base）** |
| `ci.yml` | `push` | **✓** | ✗ | ✗ |
| `main-pr-target-guard.yml` | `pull_request`（base=main） | — | — | **✓（仅 base=main）** |
| `promote-dev-to-main.yml` | `push`（保留前缀） | **✓** | ✗ | ✗ |
| `promote-dev-to-main.yml` | `push` tag `chore.*` | ✓（tag） | ✓（tag） | — |
| `promote-dev-to-main.yml` | 手动 | ✓ | ✓ | 需 `--ref` |
| `release.yml` | `push` tag `v*` | ✓（tag） | ✓（tag） | — |

> **注意 `push` 那一列**：`ci.yml` 的 `push` 只跟 `dev`。
> 这不是遗漏 —— promote 用 `GITHUB_TOKEN` 推 `main`，而
> `GITHUB_TOKEN` 推的提交**不触发工作流**，所以把 `main` 写进触发列表也永远不会跑，
> 只会让人误以为「main 上有 CI」。见 8.4 第 3 点。

## 13. 供应链与合规基线

本仓库是 **public**。平台侧的多数防线因此可用，但本版**主动放弃**了其中几项
（理由见 13.4）。所以这一节要分清两类：

- **写进仓库、由 CI 与本地门禁强制**的 —— 完全受版本控制，这部分照旧；
- **属服务端配置或主动不做的** —— 必须**如实声明**，不能让读者以为基线是完整的。

条款来源与落地位置的对照如下，**机械钉扎在 `tests/test_supply_chain.py`**：

| 标准条款（来源） | 落地位置 | 钉扎 |
| --- | --- | --- |
| 第三方 action 钉完整 commit SHA（GitHub Secure use；Scorecard `Pinned-Dependencies`） | 全部 `uses:` | `test_every_third_party_action_is_pinned_to_a_full_commit_sha` |
| 钉扎行保留 `# vX.Y.Z` 注释（Dependabot 依赖它更新版本文档） | 同上 | `test_pinned_actions_keep_a_version_comment_for_dependabot` |
| 顶层 `permissions` 只读、写权限下放 job 级（Scorecard `Token-Permissions` 满分口径） | 4 个工作流 | `test_workflows_declare_top_level_read_only_permissions` |
| 不用 `pull_request_target` / `workflow_run` 检出不可信代码（Scorecard `Dangerous-Workflow`，Critical） | 4 个工作流 | `test_workflows_avoid_dangerous_triggers` |
| 依赖更新工具（Scorecard `Dependency-Update-Tool`） | `.github/dependabot.yml` | `test_dependabot_covers_actions_and_hand_maintained_python_deps` |
| 依赖更新冷却期（zizmor `dependabot-cooldown`，置信度 High） | 同上 `cooldown` 段 | 同上 |
| 安全政策（Scorecard `Security-Policy`，按其三档评分写全） | `.github/SECURITY.md` | `test_security_policy_satisfies_every_scorecard_scoring_tier` |
| 代码所有者约束工作流变更（GitHub Secure use） | `.github/CODEOWNERS` | `test_codeowners_covers_the_supply_chain_surface` |
| 发布产物带签名/证明（Scorecard `Signed-Releases`；**后缀 `.intoto.jsonl` 才是满分档**） | `release.yml` 归档 `*.intoto.jsonl` | `test_release_attaches_provenance_bundle_to_the_release` |
| 发布 SBOM（Scorecard `SBOM`；NTIA/CISA 最低要素） | `release.yml`（Syft → SPDX） | `test_release_generates_and_attests_an_sbom` |
| 复用 CI 逻辑而非多处手抄（GitHub 官方推荐 composite action） | `.github/actions/setup-env/` | `test_every_delegating_workflow_uses_the_shared_setup_action` |
| ~~**SAST**（Scorecard `SAST`）~~ **本版主动不做** | 无（见 13.4） | `test_sast_is_deliberately_absent_and_recorded_as_such` |
| **分支保护**（Scorecard `Branch-Protection`） | 服务端配置，期望值见 `doc/BRANCHING.md` | `test_protection_script_enforces_admins_and_reads_back` |
| **环境人工放行**（GitHub environments，可选增强） | `release.yml` 的 `environment: release` | `test_release_timeout_covers_human_approval_if_environment_used` |
| ~~**强制力不漂移**每日巡检~~ **本版已移除**（缺口见 13.5） | 无 | `test_protection_drift_is_acknowledged_as_unmonitored` |

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
从 `config/.pre-commit-config.yaml` 的 `ruff-pre-commit` rev 推导。

原因：`ci.yml` 的 lint job 走 **pre-commit 的隔离环境**，ruff 版本由钩子的
`rev:` 决定；而需要门禁的其他场景（本版是 `release.yml` 的确定性测试）跑的是
**裸 `python -m ruff`**，版本由 pip 决定。两处不一致会让同一个文件「一边判过、
一边判不过」—— 而且报错通常是几百行格式差异，很难看出根因是版本。
单一来源是唯一不会漂移的做法。

> 注：`promote-dev-to-main.yml` 自 2026-09-19 起**不再跑门禁**，因此不再涉及
> 版本对账。但本机制仍然必要 —— 只要还有任何一处裸 `python -m ruff`，
> 就必须与 pre-commit 的 rev 锁步。

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

### 13.4 平台能力矩阵：哪些用、哪些不用

**2026-09-19 第二次重构**：用户要求**更简单**的 CICD 流程。转 public 后平台侧
多数防线技术上解锁，但「能用」不等于「该用」——本版**主动放弃**了其中几项。
两张表如实分开写，避免两种误判：「以为没做」和「以为做了」。

**本版实际启用的**：

| 能力 | 落点 | 谁能改 | 失效探测 |
| --- | --- | --- | --- |
| 分支保护 + 必需检查 | 服务端 / `scripts/apply_branch_protection.sh` | 仓库 admin | ⚠️ **无自动巡检**（见 13.5） |
| 禁止直推 `main`（含管理员） | 同上 `enforce_admins: true` | 同上 | 直推会被服务端拒绝（即时可见） |
| Secret scanning / push protection | 服务端开关 | 仓库 admin | ⚠️ 无自动巡检，靠人工确认 |
| Dependabot alerts / 版本 PR | 服务端开关 + `.github/dependabot.yml` | 仓库 admin / 代码 | ⚠️ 无自动巡检 |

**技术可用但本版主动不做的**（每项都记了理由，不是遗忘）：

| 能力 | 为什么不做 | 恢复成本 |
| --- | --- | --- |
| **CodeQL / SAST** | 会引入一个**异步**的失败来源（首次扫描 5–10 分钟）且结果需人判读。对单人维护 + 运维使用的场景，收益低于理解成本 | 低（加一个工作流文件 + 把它写进必需检查） |
| **配置漂移每日巡检** | 为了「更简单」撤掉。缺口已如实声明（见 13.5） | 中（要重建巡检逻辑 + `issues: write`） |
| **上游自动同步** | 依赖已移除的 `lkg` 分支与 `SYNC_PAT` | 高（要一并恢复分支、Secret、告警生命周期） |
| **`release` 环境人工放行** | **能力保留**（`release.yml` 仍声明 `environment: release`），但需你去 Settings 创建环境才生效。属可选增强 | 低（建环境 + 填 reviewer） |
| **CODEOWNERS 强制 review** | 需要 `required_approving_review_count ≥ 1`，而单人维护者**无法批准自己的 PR** —— 会自我死锁 | 中（协作方变多后应开启，同时重评 `enforce_admins`） |
| commit signing / vigilant mode | 需签名 key 分发，单人仓库收益有限；发布侧已由 SLSA attestation 覆盖 | 低 |
| 历史泄漏扫描（gitleaks 等） | 建仓前手动扫过一次全历史（凭证 / 大文件 / 内网地址，均无命中） | 低 |
| 签名类资产（`.asc` / `.sigstore`） | 已用 attestation（`.intoto.jsonl`）满足 Scorecard `Signed-Releases` 的**最高档**，无需再加 | — |
| 分支保护之外再做 Rulesets | `branch protection` 已覆盖需求；Rulesets 是其超集，迁移收益不足以抵消复杂度 | 中 |
| 第三方 SAST（Semgrep 等） | Scorecard 的 `SAST` 项只认 `github/codeql-action` 或 SonarCloud，加第三方工具拿不到分且增加噪声面 | — |

### 13.5 已知缺口、工具冲突与复评触发条件

#### 缺口：服务端配置没有自动巡检

分支保护可以被人在 Settings 上**随手点掉，且不留任何代码痕迹**。
上一版用 `protection-audit.yml` 每日巡检堵这个缺口；本版为「更简单」撤掉了，
**所以缺口是打开的**。

**如实声明**比假称「已有巡检」好 —— 后者会让维护者放弃手动确认。

| 缓解手段 | 有效程度 |
| --- | --- |
| promote 每次会真的推 `main` | ❌ **无效**：保护被关掉后 promote 反而更顺畅 |
| `main` 出现 `dev` 没有的提交 → 下次 promote 响亮失败 | ⚠️ **延迟生效**：要等到下次 promote 才发现 |
| 改动 Settings 后重放 `apply_branch_protection.sh` | ✅ 人工流程，需纪律 |
| `git push origin main` 被拒 | ✅ 即时（只要你去试） |

**复评触发条件：若出现「保护被误关且造成实际影响」，立刻恢复每日巡检。**

#### `$/` vs actionlint

zizmor 的 `self-repository` 审计（v1.30.0 起）建议把仓库内 action 的引用从
`./…` 改成 `$/…`（GitHub 2026-07 引入的语法，不受运行时文件系统状态影响，
且被平台视作 pinning）。但 **actionlint 最新版 v1.7.12（2026-03-30）尚不认识
`$/`**，会判 `ref is missing`；而 actionlint 是本地与 CI 双端硬门禁。
故当前保留 `./…` 并在调用点写**显式行内豁免**
`# zizmor: ignore[self-repository]`，理由写在 `ci.yml` 里。
**复评触发条件：actionlint 支持 `$/` 后立刻改回并撤掉豁免。**

#### Dependabot 不覆盖根目录 pip

`requirements.txt` 与 `requirements/host-provided.txt` 是
`vendor/_upstream/pyproject.toml` 的派生产物（文件头写明「勿手改」），
一致性由 `tests/test_requirements_sync.py` 逐字节守护。让 Dependabot 改它们，
每个 PR 都注定违反派生契约 —— 那是噪声不是防线。
上游版本漂移的正道是重跑 `scripts/derive_requirements.py`。

#### `pinned-SHA` 正则必须允许子路径

`owner/repo/subpath@<sha>` 是**合法**且常见的形态（例如
`github/codeql-action/init@<sha>` 把 init / analyze 拆成同仓库子路径）。
`tests/test_supply_chain.py` 的钉扎正则在 2026-09-19 因此**误报过**——
正则只允许 `owner/repo@sha`。已修正为允许任意深度的子路径。

**教训：断言的正则必须覆盖平台允许的全部合法形态，否则它拦的不是违规，
而是「用的形态我没预料到」。** 本版虽然不再用 CodeQL，但正则保留 subpath
支持——下次引入任何多级路径的 action 时不会再踩同一个坑。

### 13.6 从零到可用的执行路径（最该先读）

```
1. 建 public 空仓库（不勾 README / .gitignore / license）
2. push dev 与 main 两个分支              ← 四个 workflow 文件随之就位
3. Settings → General：默认分支改为 dev
4. Settings → General：只留 squash merge，开自动删除 head 分支
5. Settings → Code security：开 secret scanning + Dependabot alerts（建议）
6. Settings → Environments：建 release 环境 + required reviewers（可选，建议）
     ⚠️ 不要勾 Prevent self-review —— 单人维护者将无法批准自己的发布
7. bash scripts/apply_branch_protection.sh    ← 打分支保护（幂等 + 回读校验）
8. 验证四件事（见下表）
```

**第 8 步的验证清单** —— 不要跳过，尤其是后两项：

| # | 验证 | 期望 |
| --- | --- | --- |
| 1 | 开一个 base=`dev` 的测试 PR | 三项必需检查阻塞合并 |
| 2 | 开一个 base=`main` 的测试 PR | `main-pr-target-guard` 判红 |
| 3 | `git push origin main` | **被服务端拒绝**（证明 `enforce_admins` 生效） |
| 4 | 往 `dev` 推一个 `[merge] xxx` 提交 | `main` 被移动到 `dev` 尖端 |

第 7 步**必须在第 2 步之后**：要先把工作流文件推上去，必需检查的 context
才有对应的 job 存在。第 3 步是唯一能证明「强制力真实存在」的动作。

## 14. 行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
