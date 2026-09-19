# 分支模型与强制力

本文是**期望配置**的单一事实来源。`scripts/apply_branch_protection.sh`
（应用）与 `.github/workflows/protection-audit.yml`（巡检）都以此为准，
`tests/test_branch_model.py` 断言三者一致。

---

## 1. 模型

```
feature/*  ──PR──▶  dev  ──promote（fast-forward）──▶  main
                      │                                  │
                      │                                  └── tag v* ──▶ release
                      └── 每日 sync-upstream roll（上游镜像）
```

- **`dev`** 是工作分支，也是**默认分支**。所有 PR 打到 `dev`。
- **`main`** 是发布指针，**永远等于**某个已通过全量门禁的 `dev` 提交
  （快进提权，不产生新提交，因此 `main` 的每个提交都逐字节等于 `dev` 上的某提交）。
- **`lkg`** 是 last-known-good 回滚锚点，每次 roll 覆写，**不受**保护。
- **`sync/standalone-*`** 是同步流水线开出的短生命周期分支，允许 force。

### 为什么默认分支是 `dev` 而不是 `main`

不同事件读的工作流定义来自不同 ref：

| 事件 | 读哪个 ref 的定义 |
|---|---|
| `push` | 被推送的那个 ref |
| `pull_request` | PR 的合并提交 |
| `schedule`（cron） | **默认分支** |
| `workflow_dispatch`（无 `--ref`） | **默认分支** |

把默认分支设为 `dev`，则 cron 与无 ref 的 dispatch 都读 **`dev` 的定义** ——
而 `dev` 正是改动的落点。于是「改了定时工作流但 cron 仍跑旧版本」
这个坑**直接消失**，不需要等一次 promote 才生效。

代价：GitHub UI 默认展示 `dev` 而非 `main`。这是**正确的语义** ——
本仓库的权威版本就是 `dev`，`main` 只是发布指针。

---

## 2. `main` 的分支保护（期望值）

| 字段 | 期望值 | 为什么 |
|---|---|---|
| `required_status_checks.strict` | `true` | 分支必须最新才能合并，防「绿着过期」 |
| `required_status_checks.contexts` | 4 个（见下） | job name 逐字一致，写错会永久卡住 |
| `enforce_admins` | **`true`** | 核心承诺：管理员也不能绕过 |
| `required_pull_request_reviews` | `null` | 见下方决策记录 |
| `restrictions` | `null` | 不限用户 |
| `allow_force_pushes` | `false` | main 历史不可改写 |
| `allow_deletions` | `false` | main 不可删除 |
| `required_linear_history` | `true` | 与快进提权模型一致 |

### 必需状态检查（context 必须与 job name 逐字一致）

```
lint (ruff / actionlint / zizmor)     ← ci.yml  的 job: lint
typecheck (mypy)                      ← ci.yml  的 job: typecheck
test (pytest + vendor verify)         ← ci.yml  的 job: test
analyze (python)                      ← codeql.yml 的 job: analyze
```

> **这是最容易配错的一处。** context 指的是 workflow 里的 **job name**
> （`jobs.<id>.name`），不是文件名、不是 workflow name、也不是 job id。
> 写错的后果不是报错，而是 PR 永久停在
> `Expected — Waiting for status to be reported` —— 没有任何错误信息。

**改名纪律**：改任何 workflow 的 `jobs.<id>.name`，必须同步改
① 本文件 ② `scripts/apply_branch_protection.sh` 的 `REQUIRED_CHECKS`
③ `protection-audit.yml` 的 `EXPECTED_CHECKS`。
三处不同步 → PR 卡死或巡检误报。

### 决策记录：为什么 `required_approving_review_count` 是 0

本模型的强制力核心是**「必需检查必须过」**，而不是「必须有人 approve」。

- 单人维护下 `count >= 1` 会**自我死锁**：你无法批准自己开的 PR。
- 绕过它需要 bypass 权限，而 bypass 与 `enforce_admins: true` **语义冲突** ——
  后者正是本设计的核心承诺。
- 因此取 `0`：强制力全落在「检查必须过」上，这在单人仓库里已足够
  （4 个 job 覆盖 lint / 类型 / 契约测试 / SAST）。

**协作方变多后**（≥2 人常驻）应提到 `1`，届时需重新评估
`enforce_admins` 与 bypass 的取舍，并把 `prevent_self_review` 打开。

---

## 3. `release` 环境的保护规则

public + Free 下环境保护规则**可用**（private 下不可用 —— 这正是本次重构
之前 `environment: release` 是一道**不存在的门**的原因）。

| 项 | 期望 | 为什么 |
|---|---|---|
| required reviewers | ≥ 1 | 发布需人工放行 |
| prevent self review | **关** | 单人维护下开了会**永久卡死发布**（无法批准自己的 run） |
| deployment branches | `v*` tag | 只允许从发布 tag 部署 |

**超时注意**：`release` job 的 `timeout-minutes` **包含等待人工审批的时间**。
默认 15 分钟意味着 reviewer 必须在 15 分钟内点批准，否则 job 直接失败。
本仓库设为 **60 分钟**，并在 workflow 注释里写明这一点。

---

## 4. 强制力清单（哪些是真的，哪些没有）

仓库为 **public + Free** 后的实际能力：

| 能力 | 状态 | 落点 |
|---|---|---|
| 分支保护 + 必需检查 | ✅ **真** | 服务端，本文件第 2 节 |
| 禁止直推 main | ✅ **真** | `enforce_admins: true` |
| Rulesets | ✅ 可用 | 未启用（branch protection 已够；rulesets 是超集，迁移收益不足以抵消复杂度） |
| CodeQL (SAST) | ✅ 免费 | `codeql.yml` |
| secret scanning | ✅ 免费 | 服务端开关 |
| Dependabot alerts | ✅ 免费 | 服务端开关 |
| 环境 required reviewers | ✅ 可用 | 第 3 节 |
| CODEOWNERS 强制 review | ✅ 可用 | 需配合 `count >= 1`，当前为 0 → **写了但不强制** |
| 私有安全公告 | ✅ 可用 | `.github/SECURITY.md` 指向 |

### 仍然做不到 / 未做的

| 项 | 原因 |
|---|---|
| CODEOWNERS 强制 | `count = 0`（单人维护决策），见第 2 节 |
| gitleaks 类历史扫描 | 未接入 CI；push 前手动扫过一次（无命中），列为可选增强 |
| commit signing / vigilant mode | 未强制；需签名 key 分发，单人仓库收益有限 |
| Sigstore 之外的签名（`.asc`） | 用 attestation（`.intoto.jsonl`）已满足最高档 |

---

## 5. 本地钩子（软约束，仍然保留）

服务端已有硬约束，本地钩子作为**快速反馈**保留：

- `no-force-push-main`：拦对 `main` / `dev` 的非快进推送（`pre-push` 阶段）
- 它**可以被 `--no-verify` 绕过** —— 这没关系，因为它不再是唯一防线，
  服务端的 `allow_force_pushes: false` 才是。

保留的价值：本地拦截提供**即时反馈**，而服务端拒绝发生在推送之后，
排查成本更高。

---

## 6. 配置漂移的处理流程

巡检（`protection-audit`）报 `protection drift` issue 时：

1. **分支保护类**：重放期望配置
   ```bash
   bash scripts/apply_branch_protection.sh
   ```
   它是幂等的（PUT 全量替换）且**自带回读校验** —— 写成功 ≠ 生效，
   API 返回 200 但字段被服务端规范化是真实发生过的。
2. **安全开关类**（secret scanning / Dependabot）：Settings → Code security 手动开。
3. **环境类**：Settings → Environments → `release` 检查 reviewers。
4. issue 会在下一轮巡检全绿时**自动关闭**。

---

## 7. 常见故障

| 症状 | 根因 | 处理 |
|---|---|---|
| PR 停 `Expected — Waiting for status to be reported` | 分支保护的 context 与 job name 不一致 | 核对第 2 节的四处同步点 |
| 无法合并自己的 PR | 误开了 `required_approving_review_count >= 1` | 改回 0，或加 bypass（但要重新评估 enforce_admins） |
| release 卡住不结束 | ① `prevent_self_review` 开着 ② reviewer 超时 | 见第 3 节 |
| `gh workflow run <wf>` 报 422 | 该工作流文件不在默认分支上 | 用 `--ref dev`，或先 promote |
| cron 跑了旧版本 | 默认分支不是 `dev` | 确认 default branch 设置 |
| 直推 main 成功了 | 分支保护失效 | 跑巡检看漂移原因，重放配置 |
