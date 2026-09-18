# 维护清单（agent 维护手册）

| 角色 | 文件 |
|---|---|
| 机器可读事实源 | [`maintenance-checklist.json`](./maintenance-checklist.json) |
| 检测器 / 编排入口 | [`scripts/maintenance_check.py`](../../scripts/maintenance_check.py) |
| 活性守护 | [`tests/test_maintenance_checklist.py`](../tests/test_maintenance_checklist.py) |

本文件是设计说明与渲染。**字段与内容以 JSON 为准**，此处不镜像条目正文。

---

## 1. 它解决什么问题

本仓库的上游同步是全自动的（`.github/workflows/sync-upstream.yml`）：roll → 两层注入 →
契约测试 → PR（base=`dev`）→ 分层 automerge。绝大多数上游演进（新增配置字段、新增依赖、
schema 选项变化）零人工。

但有一类变化**故意**被设计成红的：新增可命中平台、平台正则漂移、vendor 公开 API 变化、
破坏性变更跨越。这些关卡不是缺陷，而是刻意的 tripwire——它们要求有人读 diff 后做判断。

于是产生两个问题：

1. **谁**识别「现在是维护时刻」？
2. 识别之后，**谁**执行？

清单把这两件事都交给 agent：一次命令输出全部关卡状态，每条状态自带操作步骤、改动
白名单、停手条件与验收标准。

**分支模型与清单的关系**：roll 只改 `dev`，`main` 由 `promote-dev-to-main` 显式提权
（CONTRIBUTING.md 第 8 节）。所以「同步完成」不等于「已发布」——MC-11 绿只说明滚子
没熔断，要不要 promote 是人的决定。清单**不**替人做这个决定。

---

## 2. 设计原则（四条）

**P1 判据复用，不另起一套。** 每个 detector 必须指向仓库既有的关卡（pytest 节点、既有
脚本）。若清单自带判据，就会出现「清单绿但仓库红」的分裂事实——两份互相矛盾的真相。

**P2 命令可执行，不是散文。** `action` 与 `acceptance` 是可直接复制执行的命令行，不是
「请检查一下……」这类描述。

**P3 每条都要有停手边界。** `escalate_if` 必填。没有它，agent 会在「我大概知道怎么做」的
地方越界，而越界的代价是它自己无法评估的。

**P4 清单自身是契约。** 清单会腐烂。守护测试断言每个 detector 指向的 pytest 节点真实
存在——节点被改名后 CI 必红，而不是清单静默报绿。

> P4 不是理论担忧。写这份清单时守护测试当场抓到三条全局禁令缺 `why` 字段。

---

## 3. 条目类型：三个正交轴

分类不用来分类，用来**决定 agent 的行为**。

### `family` —— 问题发生在哪一层

| 值 | 含义 | 典型触发 |
|---|---|---|
| `plane` | 注入平面（配置/schema/依赖/生成工件） | 上游新增配置字段、依赖变更 |
| `platform` | 平台面（枚举/注册表/解析正则） | 新增平台、正则漂移 |
| `contract` | 桥接契约（import 面/挂载点/公开 API） | 上游改内部结构、破坏性变更 |
| `ops` | 运维面（同步流水线/发布） | 熔断、发布清单缺失 |

### `class` —— agent 可以走多远（最关键的一轴）

| 值 | 自主度 | agent 行为 |
|---|---|---|
| `auto` | 完全自主 | 直接执行 action，跑 acceptance，无需请示 |
| `assisted` | 自主执行 + 人工评审 | 执行 action，但产物必须进 PR 等人工 review 后才合入 |
| `escalate` | 不动代码 | 只产出诊断报告，交人工判断 |

分界线是「错了我能不能自己发现」。机械变换（重跑注入、重跑派生）错了会被幂等断言抓住，
所以可以 `auto`；正则语义、公开 API 面、架构不变量错了没人能自动发现，所以只能 `escalate`。

### `severity` —— 是否阻塞

| 值 | 含义 |
|---|---|
| `blocking` | 阻塞 roll；不处理则流水线持续失败 |
| `advisory` | 不阻塞，但必须人工确认影响面 |

---

## 4. 字段规范

### 条目级

| 字段 | 必填 | 语义 |
|---|---|---|
| `id` | ✓ | 稳定标识 `MC-NN`；引用与报告用它，不用标题 |
| `title` | ✓ | 一句话，面向人 |
| `family` | ✓ | 见上 |
| `class` | ✓ | 见上 |
| `severity` | ✓ | 见上 |
| `detector` | ✓ | 机器可判定的触发条件（见下） |
| `scope` | ✓ | **改动白名单**：本条允许触碰的路径；空数组 = 不得改任何文件 |
| `action` | ✓ | 预期操作，有序命令/步骤 |
| `acceptance` | 建议 | 局部验收：命令 + 期望退出码 |
| `escalate_if` | ✓ | 停手条件 |
| `forbidden` | 建议 | 引用的全局禁令 id（`P-01`…） |
| `diagnosis` | 建议 | 如何确认根因 |
| `root_cause_candidates` | 可选 | 本条可能是哪些条目的**症状**（因果链） |
| `evidence` | 建议 | 证据留痕要求 |
| `known_good` | 可选 | 上次正确处理的先例，供 agent 对齐风格 |
| `doc_ref` | 建议 | 关联 ADR / 文档 |

### `detector` —— 触发条件

四种 `kind`，覆盖「工作树可判」到「只能在事件上下文里判」：

| `kind` | 必需字段 | 语义 |
|---|---|---|
| `pytest` | `nodes[]` | 任一节点失败即红；节点不存在 = 清单过时（退出码 2） |
| `cmd` | `cmd`, `expect_exit` | 退出码不符即红 |
| `gh_runs` | `workflow`, `limit`, `red_when_failures_at_least` | 查 workflow 最近 N 次结论；可带 `fallback` |
| `event` | `source`, `match` | 工作树中不可求值（如 sync PR 正文的 advisory），只提示上下文 |

### 全局节

| 节 | 语义 |
|---|---|
| `definition_of_done` | 收工判据：三项全绿才算完成，单条 acceptance 只是局部证据 |
| `prohibited_actions` | 全局禁令 `P-NN`：`rule` + `why` + 被谁抓住 |
| `loop_guard` | 循环防护：同一条目修复后仍红 ≥N 次则停手 |

### 命令占位符 `{py}`

所有可执行命令用 `{py}` 指代当前解释器，由检测器展开为 `sys.executable`。CI 提供 `python`，
开发机往往只有 `python3`——硬编码任一个都会在另一侧以 `FileNotFoundError` 响亮失败。

---

## 5. 三件套：`scope` / `forbidden` / `escalate_if`

这三个字段是 agent 能安全自主的全部原因，缺一不可：

- **`scope`** 回答「我能改什么」——白名单，越界即停。
- **`forbidden`** 回答「我绝对不能做什么」——引用全局禁令，其中最重要的是 **P-03：
  不得删除、跳过、xfail 或弱化任何断言来让测试变绿**。这是 reward hacking 的头号路径。
- **`escalate_if`** 回答「什么时候我必须停手」——它是**每个条目自己的**边界，比全局规则更精确。

举例：MC-07（新增平台补样本）`class=assisted`、`scope=[tests/test_match_golden.py]`。
如果 agent 把样本补到 `main.py` 里，scope 就拦住了；如果它想删掉元测试让它变绿，P-03 拦住了；
如果新平台的 pattern 需要鉴权、无法离线构造必然命中的 URL，`escalate_if` 拦住了。

---

## 6. agent 的机制：理解 → 决策 → 执行

### 阶段 1 · 发现（Discover）

不猜。一次调用拿到全部关卡状态：

```bash
python scripts/maintenance_check.py --json
```

退出码即结论：`0` 全绿 / `1` 有红条目 / `2` 清单过时或环境不可用。
输出每条含 `state` / `detail` / `action` / `acceptance` / `scope` / `forbidden` / `escalate_if`。

实现上，pytest 类条目共享**一次**全量 `pytest --junit-xml` 运行再按 node id 映射，
而不是逐条起一次 pytest。

### 阶段 2 · 分流（Triage）

按 `class` 路由，`blocking` 优先于 `advisory`。

### 阶段 3 · 定位（Diagnose）

先读 `detail`——它已经是关卡的原话（失败节点名、断言消息最后一行、退出码）。
**不要重新发明诊断。** 再按需读 `diagnosis` 与 `doc_ref`，必要时看 `root_cause_candidates`：
MC-02 红往往是 MC-07/MC-08 的症状，直接修 MC-02 是治标。

### 阶段 4 · 执行（Act）

按 `action` 逐步执行。每一步都在 `scope` 内；越界即停。`forbidden` 是硬约束。

### 阶段 5 · 验收（Verify）

先跑该条目的 `acceptance`，再跑 `definition_of_done` 三项。
**单条 acceptance 绿不等于收工**——DoD 才是收工判据。

### 阶段 6 · 收尾（Report）

输出结构化报告：处理了哪些条目、改了哪些文件（应全在 scope 内）、验收证据、
未处理的 escalation 及其原因。

### 循环防护

同一条目修复后仍红 ≥2 次 → 停手上报。连续失败说明**根因判断错误**；继续试错会把改动
扩散到 scope 之外，而那时已经没人能审得清。

---

## 7. 示例格式

以「上游新增可命中平台」为例——这是本仓库最高频的人工维护场景：

```json
{
  "id": "MC-07",
  "title": "上游新增可命中平台，黄金样本未覆盖",
  "family": "platform",
  "class": "assisted",
  "severity": "blocking",
  "detector": {
    "kind": "pytest",
    "nodes": ["tests/test_match_golden.py::test_golden_covers_every_matchable_platform"]
  },
  "scope": ["tests/test_match_golden.py"],
  "diagnosis": "断言右侧多出的键即新平台名；用 load_all() 反查对应 parser 类",
  "action": [
    "读新 parser 的 @handle 装饰器，取出 keyword（域名）与 pattern",
    "由 pattern 构造必然命中的样本 URL：https://{keyword}/{pattern 的固定字面前缀}{占位值}",
    "在 GOLDEN_MATCHES 里补一条「平台键: 样本 URL」，位置与文件既有排序一致",
    "跑 acceptance"
  ],
  "acceptance": [
    { "cmd": "{py} -m pytest -q tests/test_match_golden.py", "expect_exit": 0 }
  ],
  "forbidden": ["P-03"],
  "escalate_if": "新 parser 的 pattern 含必需 query 参数或需要鉴权（无法离线构造必然命中的 URL）→ 交人工",
  "evidence": "附新 parser 的 @handle 原文 + 新样本 URL + 实测命中输出",
  "known_good": "2026-09 快照下 27 个平台的样本均按此法反推并逐条实测确认",
  "doc_ref": "doc/CONTEXT.md「黄金样本」"
}
```

读法：agent 看到 MC-07 红 → `class=assisted` 意味着它自己做完但要进 PR → 按 `action` 四步走 →
用 `acceptance` 验 → 只在 `tests/test_match_golden.py` 里改（scope）→ 不能动断言（P-03）→
若 URL 构不出来就停手（`escalate_if`）。

---

## 8. 如何新增一条

1. 先找到**已有的关卡**（pytest 节点 / 脚本退出码）。找不到就先加关卡，再加条目——
   顺序反了会造出没有判据的清单。
2. 跑一次 `{py} -m pytest --collect-only -q` 确认节点名拼写。
3. 想清楚 `class`：错了能不能自己发现？不能就是 `escalate`。
4. 写 `scope`——把允许改的路径写到最小。
5. 写 `escalate_if`。写不出来说明你还没想清楚这条维护的边界。
6. 跑 `{py} -m pytest -q tests/test_maintenance_checklist.py`——守护测试会告诉你字段是否齐备。
7. **反向验证**：故意制造该条目的触发条件，确认清单真的变红，再恢复。
   一个从未红过的 detector 不是检测器，是装饰。

---

## 9. 已知边界（如实声明）

- **`event` 类条目不可本地求值。** MC-10（破坏性变更跨越）只在 sync PR 正文里可判，
  检测器只能提示上下文。agent 必须在 PR 场景下处理它，不能依赖本地跑绿。
- **`gh_runs` 依赖 gh 与鉴权。** 不可用时回退本地 `sync-state.json` 的 `consecutive_failures`；
  注意权威判据是 `gh run list`，state 字段只是降级近似。
- **动态导入无法静态守护。** P-01 的机械防线是 `verify_vendor.py` 的逐字节比对，
  但 `importlib.import_module` 类写法不在其覆盖面内。
- **MC-14 需要刷新过的远端引用。** 判据读 `origin/main` 与 `origin/dev`，本地
  未 `git fetch` 时结论可能过时；且远端**没有**分支保护（private 且无 GitHub Pro），
  所以它是信号而非阻断——绕过 promote 直推 main 在服务端不会失败，只会在下一次
  跑清单时被 MC-14 抓到。
- **清单不代替判断。** `auto` 类条目可以无人值守执行；`escalate` 类**永远**要人。
  把 `escalate` 改成 `auto` 来让流水线变绿，是比 P-03 更隐蔽的作弊——不要做。
