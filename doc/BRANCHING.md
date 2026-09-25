# 分支模型与分支保护

> 本文是**预期配置的唯一事实源**。本文与
> `scripts/apply_branch_protection.sh`、`CONTRIBUTING.md` §8 三处必须一致，
> `tests/test_branch_model.py` 会断言这一点。

**仓库可见性**：public。这决定了下面所有「能做 / 不能做」的边界。

---

## 1. 只有两个长期分支

| 分支 | 角色 | 谁能推 | 保护 |
|---|---|---|---|
| **`dev`** | **工作分支 + 默认分支**。所有功能、修复、文档变更先到这里 | 开发人员经 PR；管理员可直推救急 | required checks、禁止删除、禁止强推 |
| **`main`** | **发布指针**。永远与某次 promote 当时的 `dev` 尖端**是同一个提交** | **只有 CI** | required checks、`enforce_admins`、禁止删除 |

```
  feat/xxx  ──PR──▶  dev（默认分支）  ──[merge] 前缀──▶  promote  ──▶  main  ──▶  tag v*（自动）──▶  release
  sync cron ──PR──▶   ▲                  （bot 合 PR）      （尾部自动打 tag）
  （上游 roll）        └────────────── 日常开发都在这边 ──────────────────────┘
                                                     发布指针，不直接改
```

**没有 `lkg`、没有 `release`、没有 `staging`。** 回滚靠 `v*` tag + `git revert`（见 §7）。

### 1.1 上游同步轨道（sync 是一名全自动的普通开发者）

`sync-upstream.yml` 每日检测上游 standalone roll：供应链判据（
`scripts/upstream_sync.py`，异常即红）→ 完整 roll 序列（委派
`scripts/roll_local.py`，与本地同一实现）→ 开 PR（标题带 `[merge]` 前缀）
挂 auto-merge。人与 sync 走**同一条**PR→CI→promote→tag→release 轨道，
没有特权通道。

维护契约：**人不审阅上游内容，只修「管道红」**。管道红 = required checks
失败或供应链判据命中——前者修桥（桥的 bug 归我们）或把 bug 提给上游
（vendor 零修改铁律），后者经 `confirm=<新上游 sha>` 手动放行或等上游澄清。
「上游不 bump 版本时桥接修复怎么发」不是问题：用户更新通道是 dev 分支本身
（见 §7）。

---

## 2. `main` 是「指针」，不是「合并结果」

这是本模型最反直觉的一点，务必先读。

### 2.1 怎么更新 `main`

`promote-dev-to-main.yml` 用**指针移动**更新 `main`：

```bash
git push --force-with-lease="refs/heads/main:${main_sha}" \
  origin "origin/dev:refs/heads/main"
```

即：把 `origin/main` 这个 ref **指到** `origin/dev` 当前的位置。
**不产生 merge commit。**

### 2.2 为什么不用 merge

| | 指针移动（本方案） | merge commit |
|---|---|---|
| `main` 与 `dev` 的关系 | 永远**同一个 sha** | 永远不同（main 多一个 merge commit） |
| 历史可读性 | `main` 的历史 = `dev` 的历史 | network graph 出现来回交叉 |
| 「main 上的东西验证过吗」 | **一定验证过**（逐字节等于某个 dev 提交） | 需额外论证 |
| 回滚 | 对比两个 sha 即可 | 要区分 merge 前后 |

### 2.3 三条触发方式

| 方式 | 怎么做 | 说明 |
|---|---|---|
| **A. 推 `dev`（推荐）** | 提交信息**以** `[merge]` 或 `chore(release):` **开头** | 前缀匹配是**锚定行首**的：`fix: xxx [merge]` **不会**触发 |
| **B. 打 `chore.*` tag** | `git tag chore.merge-20260919 && git push origin chore.merge-20260919` | 把 promote 与代码提交解耦 |
| **C. 手动触发** | Actions → Promote Dev to Main → Run workflow | 应急 |

> ⚠️ `[merge]` 与 `chore(release):` 是**保留前缀**。开发人员**不得**在自己的
> 提交信息里使用它们 —— 那会意外触发发布会把 `dev` 当成发布点。

### 2.4 ⚠️ `main` **不能**开启 PR 保护

**这是全套配置里最重要的一条，也是唯一与通用最佳实践相反的地方。**

分支保护的 **"Require a pull request before merging"**
（API 字段 `required_pull_request_reviews`）会拒绝**所有**直接推送到 `main`
的 ref 更新 —— 包括 promote 工作流自己的推送。**一旦开启，Promote 直接失效。**

失效的表现极具误导性：`main` 永远停在旧位置，而工作流会红在「推送被拒」上，
看起来像是权限问题，实际上是被自己配的保护挡住了。

所以 `main` 的策略是：

- ✅ 用 `enforce_admins: true` 实现「只有 CI 能改 main」
- ✅ 用 `main-pr-target-guard.yml` 拦住以 `main` 为 base 的 PR
- ❌ **不用** `required_pull_request_reviews`
- ❌ **也不用** `required_status_checks` —— `main` 上不跑 CI（见下）

### 为什么 `main` 也不挂必需检查

`main` 与 `dev` 是**同一个提交**。三条必需检查是在 `dev` 推这个提交时跑出来的
（`ci.yml` 的 `push` 只跟 `dev`；且 `GITHUB_TOKEN` 推的提交不触发工作流）。
所以 **`main` 上永远不会出现 CI 运行** —— 这是模型的设计，不是缺陷。

给 `main` 挂必需检查会引出两个问题：

1. 它让读配置的人以为「`main` 上会跑 CI」，与事实相反；
2. 一旦某个 promote 目标的 sha 上缺对应的 check run，`main` 会永久卡在
   `Expected — Waiting for status to be reported`，**且不会有任何报错**说明原因。

> 「`main` 是绿的」这个结论由 **`dev` 上那次 CI** 承载 —— 两者同 sha，所以等价。

> 参考实现 [SnowLuma/SnowLuma](https://github.com/SnowLuma/SnowLuma) 的
> `CONTRIBUTING.md` 有同样的警告：「不要给 `main` 套『必须走 PR』…
> 如果给 `main` 打开 Require a pull request before merging 或禁止更新引用，
> Promote 会失效。」

---

## 3. 分支保护期望值

### 3.1 `main`

```jsonc
{
  "required_status_checks": null,          // ★ 必须为 null，见 §2.4（main 上不跑 CI）
  "enforce_admins": true,                  // 管理员也不能绕过（核心承诺）
  "required_pull_request_reviews": null,   // ★ 必须为 null，见 §2.4
  "restrictions": null,
  "allow_force_pushes": true,              // ★ promote 需要
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
```

这四个字段里有两个是 `null`、一个是 `true`，全部**与常规最佳实践相反**。
理由逐条见 §3.3。

### 3.2 `dev`

```jsonc
{
  "required_status_checks": {
    "strict": false,
    "contexts": [ /* 同上三项 */ ]
  },
  "enforce_admins": false,                 // 工作分支：CI 故障时管理员可救急
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,  // ★ 必须是 0，见 §4.3
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
```

### 3.3 四个反直觉的取值，逐条说明

| 取值 | 为什么 |
|---|---|
| `main: allow_force_pushes: true` | promote 用 `--force-with-lease`。快进语义下这不构成 force push，但**非快进场景**（例如 `main` 曾被移动过）会被 `false` 挡住。宁可显式允许（`enforce_admins` 已保证只有 CI 能推），也不让 promote 在异常历史下静默失败 |
| `required_approving_review_count: 0` | 单人维护者**无法批准自己的 PR**。设 `1` 会导致你自己永远合不了自己的 PR —— **自我死锁**，且 GitHub 不给任何解释 |
| `strict: false` | `strict: true` 要求 PR 合并前已包含 base 最新提交，每次别人合了 PR 你的 PR 就卡在 `BEHIND`。单人维护场景纯摩擦。**代价**：可能出现「两次 PR 分绿、合起来坏」的语义冲突；若将来多人协作应改回 `true` |
| `dev: enforce_admins: false` | `dev` 是工作分支。CI 因基础设施故障卡住时，维护者需要能直推救急。对开发人员（非管理员）保护仍然生效 |

---

## 4. 必需检查

### 4.1 三项，名字必须逐字一致

| 检查名 | 来自 |
|---|---|
| `lint (ruff / actionlint / zizmor)` | `ci.yml` |
| `typecheck (mypy)` | `ci.yml` |
| `test (pytest + vendor verify)` | `ci.yml` |

**取值来源是工作流里 `jobs.<id>.name`。** 对不上时 GitHub **不报错**，
PR 只会永远卡在：

```
Expected — Waiting for status to be reported
```

这是全套配置里**最容易错、且报错最不友好**的地方。所以这三个名字被钉在**四处**
并由 `tests/test_branch_model.py` 断言一致：

1. `.github/workflows/ci.yml` 的 `jobs.<id>.name` ← 事实来源
2. `scripts/apply_branch_protection.sh` 的 `REQUIRED_CHECKS`
3. `.github/workflows/*.yml` 的 `jobs.<id>.name`（实际存在的 job name 集合）
4. `CONTRIBUTING.md` §8.3 与本文件

**改名时必须同时改这四处**，否则会出现「PR 莫名卡住」或「必需检查根本不存在」。

### 4.2 `main-pr-target-guard.yml` 不是必需检查

它只在 base=`main` 的 PR 上触发并 `exit 1`，作用是**显式、可读的拒绝**。
因为 `main` 不开 PR 保护（§2.4），服务端不会拦以 `main` 为 base 的 PR ——
若不恢复这个守卫，有人合并一个 base=`main` 的 PR 就会在 `main` 上造出
`dev` 没有的提交，随后 promote 会失败（§5.2）。守卫让错误**在开 PR 时就暴露**。

### 4.3 为什么批准数是 0

见 §3.3。补充一点：强制力**不靠**人工批准，而靠
`enforce_admins` + 「只有 promote 这一条路能进 main」。

---

## 5. promote 工作流的行为

### 5.1 完整逻辑

```
decide job
  ├─ workflow_dispatch        → 跑
  ├─ push chore.* tag         → 跑
  ├─ push dev + 保留前缀      → 跑
  └─ push dev + 普通提交      → 跳过（main 不动）

promote job（仅当 decide 说跑）
  ├─ checkout dev（persist-credentials: false）
  ├─ 注入凭据
  ├─ fetch origin dev main
  ├─ main == dev ?            → 提示「无需 promote」并成功退出
  ├─ main 有 dev 之外提交 ?    → ::error:: 并 exit 1
  ├─ git push --force-with-lease … origin/dev:refs/heads/main
  └─ 自动打发布 tag：metadata.yaml 版本 ≠ 已发布 tag
       → 在 main 尖端打 v* + 显式 dispatch release.yml
       （同名 tag 已存在则跳过——tag 是不可变锚，不顺指不重发）
       Release 的 prerelease 标记由版本号机械派生（PEP440 前奏形态
       rc/alpha/beta/dev/pre-release → 预发布；人不判内容，台账如实）
```

### 5.2 `main` 有独有提交时必须**响亮失败**

若静默改用 merge，就会造出一个 `dev` 从未验证过的合并提交进入发布线 ——
破坏「`main` 上的每个提交都逐字节等于某个已过 CI 的 `dev` 提交」这一保证。
破坏它，整个模型就失去意义。

出现这种情况说明**有人绕过工作流直接写了 `main`**。处理方式：

```bash
# 1) 看 main 上哪些提交是 dev 没有的
git fetch origin
git cherry origin/dev origin/main | awk '/^\+/ {print $2}' | while read -r sha; do
  git log --oneline -1 "$sha"
done

# 2) 把这些提交收回 dev（cherry-pick 或重新提交）
# 3) 回到 Actions 重新跑 promote
```

### 5.3 `--force-with-lease` 的作用

它钉住「工作流校验过的那个 `main`」。若有人在校验与推送之间动了 `main`，
推送会被拒，而不是把对方的提交抹掉。这是 race-free 的提权。

**为什么用 `--force-with-lease` 而不是裸 `git push`**：
裸 push 在非快进时直接失败（好），但在 lease 语义下能给出更精确的失败原因，
且它是唯一能在「校验→推送」窗口内提供保护的形态。

### 5.4 `GITHUB_TOKEN` 的代价

用 `GITHUB_TOKEN` 推的提交**不会触发**后续工作流。

**影响**：`main` 移动之后**不会**出现新的 CI 运行。
**这是刻意的**：`main` 与 `dev` 是同一个提交，`dev` 上的 CI 已经跑过，再跑一遍纯属冗余。
「`main` 是绿的」这一事实由 promote run 自身承载。

---

## 6. 合并规则一览

| # | 规则 | 强制力来自 |
|---|---|---|
| R1 | 开发人员只能向 `dev` 提 PR | `dev` 是默认分支 + required checks |
| R2 | PR 必须以 `dev` 为 base | `main-pr-target-guard.yml`（判红） |
| R3 | 合入 `dev` 必须通过三项必需检查 | `dev` 的分支保护 |
| R4 | 合并方式统一 **squash merge** | 仓库设置（关闭其他合并方式） |
| R5 | **任何人**不得直接推送 `main` | `main` 的 `enforce_admins: true` |
| R6 | `main` 只能由 promote 移动 | 唯一有权推 `main` 的路径 |
| R7 | `main` 有 `dev` 之外的提交 → promote 失败 | promote 内的 `git cherry` 检查 |
| R8 | 禁止删除 `main` / `dev` | `allow_deletions: false` |
| R9 | 发布 tag 由 promote 尾部自动打在 main 尖端 | `release.yml` 校验 tag=main 尖端 + metadata 版本锁步 |

---

## 7. 回滚策略

**先认清用户在哪**：AstrBot 的插件安装/更新拉的是**默认分支（`dev`）的源码包**
（`zip_updater` → `archive/refs/heads/<default>.zip`），**不消费 GitHub
Releases**。v* tag 与 Release 是审计账本与手动安装通道。所以「救用户」的
动作永远是把好内容送回 `dev`；处理 Release 面只是修账。

| 场景 | 做法 |
|---|---|
| 刚 promote，发现 `dev` 有严重问题 | 在 `dev` 上 `git revert <bad>` → 合入 → 重新 promote |
| 已发 `v1.2.0`，线上出问题 | `git revert` 到 `dev` → promote → 打 `v1.2.1` |
| **上游坏 roll 占用了版本号**（测试全绿但行为坏/事后投毒） | 三件套：① revert 那条 sync 提交，经 PR 轨道合入 dev → promote 收回 main（用户即刻回到好源码包）；② `gh release edit <坏tag>` 降格（不删 tag——不可变锚），latest 指回好版本；③ 修复归上游，等它 bump 同号才能再发 |
| 用户需要旧版本 | 直接装旧 `v*` Release 的产物 |
| `main` 被搞坏 | `v*` tag 仍不可变，从旧 tag 重新指：`git push origin <good-tag>:refs/heads/main`（注意可能需 `--force`） |

**为什么不需要 `lkg` 分支**：`v*` tag 是真正的不可变锚点 —— 分支可以被 force
push，tag 需要显式删了重建。本设计中 `main` 本身就是「最后一次过全门禁的
sha」，tag 另兜一层；「上一已验证快照」已由 `main` + `v*` tag 承载，
额外的指针分支没有剩余职责。

---

## 8. 运维速查

### 8.1 日常

```bash
# 开 PR（base = dev）
git checkout dev && git pull
git checkout -b feat/my-change
# ...写代码...
git push origin feat/my-change
# → GitHub 上开 PR，base 选 dev，等三项检查绿，Squash and merge

# 发布（一步：让内容进 dev 并 promote）
git checkout dev && git pull
git commit --allow-empty -m "[merge] release"   # 或直接推带前缀的提交
git push origin dev                              # → promote 移动 main
                                                 # → 尾部自动打 v* tag → release 全自动
```

> tag 名取 `metadata.yaml` 的 `version`（随上游 roll 再生），人不构造版本号。
> promote 尾部发现「版本 ≠ 已发布 tag」才会打 tag；同版本重复 promote 是
> 空操作（不可变锚已存在）。

### 8.2 手动 promote（应急）

```bash
gh workflow run promote-dev-to-main --ref dev
```

> **为什么带 `--ref dev`**：默认分支是 `dev`，所以不带 `--ref` 也能工作；
> 但显式写出来可以避免「默认分支被改回 `main` 时静默跑旧定义」。

### 8.3 重新应用分支保护（配置被误改后）

```bash
bash scripts/apply_branch_protection.sh --dry-run   # 先预览
bash scripts/apply_branch_protection.sh             # 应用并回读校验
```

---

## 9. ⚠️ 服务端配置没有自动巡检（已知缺口）

**这是本方案一个真实存在的缺口，如实声明。**

分支保护是**服务端配置**：它真的硬（`enforce_admins` 下连管理员都绕不过），
但它**可以被人手在 Settings 上点两下关掉，而代码仓库里没有任何痕迹**。

本方案不做每日配置巡检（`protection-audit.yml` 式的工作流已移除，为「更简单」
主动取舍），**所以缺口是打开的**。

### 缓解措施

1. **promote 是唯一改 `main` 的路径，而它每次运行都会真的推 `main`。**
   若保护被关掉，promote **反而会更顺畅地成功**（不再有服务端拒绝），
   所以 promote 本身**不**能作为探测手段。

2. **真正的探测器是 §5.2 的 `git cherry` 检查。**
   若保护被关掉且有人直推 `main`，那么 `main` 会多出 `dev` 没有的提交，
   下一次 promote 会**响亮失败**并列出那些提交。这会把问题暴露出来 ——
   但**只在下次 promote 时**，不是实时的。

3. **人工确认**：改动仓库 Settings 后，跑一遍
   `bash scripts/apply_branch_protection.sh --dry-run` 对比，或直接重放脚本。

### 若要把缺口堵上

重新引入一个每日巡检工作流，检查
`/branches/main/protection` 的 `enforce_admins`、`required_pull_request_reviews`
未被启用、三项必需检查齐全，以及 `/secret-scanning` 与 `/vulnerability-alerts`
的开闭状态。这需要 `issues: write` 权限来开告警。

> **取舍记录**：这是「简单」与「完备」之间的一次明确取舍，用户选择了简单。
> 记录在此，以便将来需要时能快速加回。

---

## 10. 明确**不做**的项

| 项 | 为什么不做 |
|---|---|
| **SAST / CodeQL** | public 下免费可用，但会引入一个异步的失败来源（首次扫描 5–10 分钟）且需人判读。对单人维护 + 运维使用收益低于理解成本 |
| **配置漂移每日巡检** | 见 §9。为「简单」撤掉，缺口已如实声明 |
| **secret scanning / Dependabot alerts** | 不做自动巡检，靠人工在 Settings 确认（**建议**开启，属平台开关、零维护） |
| **提交签名 / vigilant mode** | 单人仓库收益低、日常摩擦高 |
| **CODEOWNERS 强制** | 需要 `count ≥ 1`，会与 §3.3 的「避免自我死锁」冲突 |
| **上游 roll 的人工内容审阅** | 维护契约（§1.1）：人不批内容、只修管道红。安全面由机械判据承接——`scripts/upstream_sync.py` 的供应链「异常即红」+ vendor 三层校验 + 全量契约测试 |
| **`release` 环境人工放行** | 单人全自动发布下它是停摆点（超时含审批等待、`prevent_self_review` 可致永久死锁），且在 private+Free 时代属付费方案的门、根本不可用。安全闸已换为机器判定：sync 判据 → tag 必在 main 历史线 → required checks |

---

## 11. 已知陷阱速查

| 现象 | 原因 | 处理 |
|---|---|---|
| PR 卡在 `Expected — Waiting for status to be reported` | 必需检查名与 `jobs.<id>.name` 不逐字一致 | 对照 §4.1 四处取值，注意括号与空格 |
| promote 报「推送被拒」 | 有人给 `main` 开了 PR 保护 | 移除 `required_pull_request_reviews`（§2.4） |
| promote 报「main 存在 dev 之外的提交」 | 有人绕过工作流直接写了 `main` | 按 §5.2 收回提交 |
| 推 `dev` 之后 `main` 没动 | 提交信息没有 `[merge]` / `chore(release):` **前缀** | 前缀必须锚定行首（§2.3） |
| 你自己 `git push origin main` 竟然成功 | `enforce_admins` 没生效 | 重跑 `apply_branch_protection.sh` |
| sync 的 PR 挂着不合并 | required checks 红——**这正是「管道红、人工介入」的落点**：修桥或把 bug 提给上游，不要绕过检查硬合 |
| 同版本的桥接修复没有新 Release | tag 是不可变锚、版本号已被上游占用：修复经 dev 源码包触达用户（§7），Release 面等上游 bump |
| sync 被供应链判据拦红 | 人工确认无害后，手动运行 sync-upstream 并填 `confirm=<本次新 sha>` 放行一次 |
| 改了工作流但 cron / 手动触发仍跑旧版本 | 默认分支不是改动的那个分支 | 默认分支已设为 `dev`，改动会在 `dev` 生效 |
