# CONTEXT.md — 术语表

本仓库的专有词汇。代码、测试、workflow 中的术语以本表为准；
新增术语先落这里再进代码。

## 架构

**ACL（Anti-Corruption Layer，防腐层）**
桥接六模块（`main.py` / `bridge/sender.py` / `bridge/render.py` / `bridge/ssrf.py` /
`bridge/config_sync.py` / `bridge/vendor_patches.py`）构成的翻译边界。两个方向：R1 = 桥只做
ParseResult → AstrBot 组件的**翻译**，不引入解析业务规则；R2 = vendor 类型
不越桥（Fan-in=1）。机器化守护：`tests/test_import_contract.py` 的 import 面
AST 白名单（`VENDOR_CONSUMERS` 常量即本清单的真值源，增删模块须同步两处）。

**vendored snapshot（vendor 快照）**
`vendor/nonebot_plugin_parser_lite` —— 上游 standalone 分支的逐字节
零修改副本。唯一升级方式是整树替换（0002），禁止 merge/patch。
`vendor/_upstream/` 存上游发布产物副本，供逐字节审计与 requirements
派生。

**缝合点（seam）**
桥接侧允许触达的 vendor 内部结构，当前仅两处：`DOWNLOADER.client
._httpx/_curl`（bridge/ssrf.py 的 SSRF 防护挂载点）与 `vendor/.../utils/log`
（bridge/sender.py 的失败日志）。由 `test_import_contract.py::test_vendor_seam_exists`
守护；上游改名时此测试先红。

**上游平面（plane）**
渲染模板/显示文本/渲染参数三个数据面，提取源直接跟上游 main 真值，
与 vendor 轨（standalone 发布节奏）的 skew 由 roll 时全量契约测试把关。

## 同步

**roll（滚动同步）**
上游同步管道的一次执行：克隆上游 standalone → 比对 sync-state 的 sha →
供应链判据 → 整树重建 vendor → 派生 requirements → 平面提取（跟上游 main
真值）→ vendor 三层校验 → 全量契约测试 → 开 sync PR（base = **dev**，标题
带 `[merge]` 保留前缀），required checks 全绿后由 bot squash-merge。
roll 序列的**唯一实现**是 `scripts/roll_local.py`，`sync-upstream.yml`
只做编排，不抄第二份。metadata.yaml 版本随 roll 直接沿用上游版本号。
roll 只改 dev，改 main 是 promote 的事；tag 是 promote 尾部的机械推论。

**sync PR / roll commit**
vendor 快照的升级 PR。内容域 = `roll_local.ROLL_ADD_PATHS`（vendor +
生成工件 + sync-state），提交清单完整性由
`test_codegen.py::test_roll_commit_list_covers_all_generated_artifacts`
钉扎——漏一项 = 旧桥工件配新 vendor，上游文案/参数静默失效。

**供应链判据（异常即红）**
`scripts/upstream_sync.py` 对新旧两棵构建树的三条机械判定：依赖清单变化、
结构规模超阈值（增删 >50 文件或 diff >5000 行）、LICENSE 变化——命中即
不开 PR、红并开去重 Issue。人工放行通道：手动运行 sync-upstream 并填
`confirm=<本次新 sha>`。这是全自动链上「绿≠无恶意」的唯一机械防线。

**维护契约（maintenance contract）**
人不审阅上游 roll 的内容，只修「管道红」：required checks 红或判据命中
才是人工介入点；桥的 bug 归我们修，上游代码的 bug 交付上游维护
（vendor 零修改铁律的延伸）。安全闸全部机器化：供应链判据 →
tag 必在 main 历史线 → 确定性测试门禁；不存在人工放行环节。

**两层注入（two-layer injection）**
发布物上游一致性的两层自动化，roll 序列内单命令完成（工作流与本地同轨）。
第一层注入=从上游自动生成代码注入模板：sync 轨道（roll 序列，单实现
`scripts/roll_local.py`）把上游源码注入 vendor/（逐字节校验）后，自动分析其配置面产出模板数据
`vendor_analysis.json`（管线临时产物，不入库，上游元信息不由本仓库维护）。
第二层注入=利用代码注入模板和 AstrBot 插件模板注入仓库代码实现迭代修改：
`scripts/run_injection.py`（单命令编排：三数据面提取→分析→生成）产出
`_conf_schema.json`、`bridge/gen_config.py`、
`metadata.yaml`（desc 为桥身份手写，许可证/版本随上游注入）、`README.md`（正文上游直通）、
`bridge/texts.py`（桥显示文本）、`bridge/render_params.py`（桥渲染参数）与 `templates/`
卡面模板族（逐字节再生），手写面仅桥身份与桥接说明，机械镜像面随上游演进
自动再生（一次维护长期有效）。两层各自幂等（重生成 diff=0），由
`tests/test_codegen.py` 与 `tests/test_render_templates.py` 守护；设计取舍
发布（release）再把
桥接模块 + 生成工件 + vendor 按白名单注入发布 zip 并做结构断言
（单顶层目录 + 关键文件在位；白名单与 main.py 导入闭包的一致性由
`tests/test_release_manifest.py` 钉扎）。

**显示文本注入（display-text injection）**
两层注入的第三条数据面：桥内面向用户的运行时文案（渲染降级、懒下载问询、
合并转发文本族、投票格式串、B 站 stats.extra 标签等）逐字来自上游 main
分支 render/matchers/parsers-bilibili 模块。`scripts/extract_display_texts.py`
在 sync 工作流中按锚点规则表
AST 提取 → `vendor/_upstream/display_texts.json`（入库的上游元数据快照，
含 source_revision）→ 第一层摄取进分析数据 → 第二层渲染 `bridge/texts.py`
（第五工件，纯数据模块）→ main.py/sender.py/render.py 消费。锚点零命中/
歧义（重复一致命中由 equals_repeated 容忍）、
卡面模板文案脱离上游子集、桥运行时新写 CJK 文案（白名单外）——三者都
响亮失败。全量提取模式附带**上游文案候选扫描**（advisory）：含 CJK 的
未收编文案打印进 sync 日志，锚点表扩充从「人工重读上游 diff」收敛为
「看候选清单」（2026-09-13 该扫描即发现 download_failed_count 整句可归
源）。见 `tests/test_user_texts.py`。

**渲染参数注入（render-param injection）**
两层注入的第四条数据面：桥内渲染/裁剪与发送编排关键数值（渲染缓存键版本、
远程 t2i 视口适配基准、超大渲染图 img/file 分流阈值、二维码点阵参数、
合并转发文本/节点上限、长文本标点切分集）逐字来自上游
main 分支 render 模块。`scripts/extract_render_params.py` 在 sync 工作流中
按锚点规则表 AST 提取（乘法阈值表达式归一为字节值、frozenset 实参归一为
标点串）→
`vendor/_upstream/render_params.json`（入库的上游元数据快照）→ 第一层摄取
进分析数据 → 第二层渲染 `bridge/render_params.py`（第六工件，纯数据模块）→
bridge/render.py / main.py / bridge/sender.py 消费、桥内不再硬编码。锚点零命中/歧义、取值
形态异常
（回车/负数/超界）——都响亮失败；上游随模板变更递增缓存键版本时，生成物
再生、渲染缓存整体失效重建。见 `tests/test_render_params.py`。

**渲染模板注入（render-template injection）**
两层注入的第五条数据面：桥内 `templates/` 卡面模板族（default/music 模板、
macros 宏、CSS）逐字节来自上游 main 分支 render/templates（2026-09-13
活体核验 sha256 全等；桥的适配面全在 bridge/render.py 的 safe_src 过滤器与数据
翻译）。`scripts/extract_render_templates.py` 在 sync 工作流中 git ls-tree
动态发现文件全集（新增/删除/改名自动跟随）→ 逐字快照
`vendor/_upstream/render_templates.json`（入库的上游元数据快照）→ 第一层
摄取进分析数据 → 第二层 `build_template_files` 逐字节再生（不经过 Jinja
二次渲染、不做 EOF 归一化），上游删除/改名的陈旧文件自动清理（桥自有
文件经 `BRIDGE_TEMPLATE_EXTRAS` 白名单保留）。文件名/内容防火墙越界、
磁盘模板与快照漂移——都响亮失败。黄金基准活体对照见
`scripts/upstream_render_probe.py`（单命令：出图 + HTML 截获 +
`--verify-digest` 自校验）。见 `tests/test_render_templates.py`。

## 分支模型

**dev（工作分支）**
所有变更的落点：feature 分支与 `sync/standalone-*` 的 PR 都以 dev 为 base，
`ci.yml` 的三个 required check 在这里把关。dev 的每个提交都可能成为 main。
历史不可改写（本地 `pre-push` 护栏拦非快进推送）——它是 promote 的输入，
历史可变会让两条线失去可比性。

**main（发布指针）**
不承载开发，永远等于某个已通过全量门禁的 dev 快照。唯一推进通道是
`promote-dev-to-main`（见「提权」）。「main 上出现 dev 没有的提交」视为
有人绕过工作流直推，promote 会响亮失败而不是静默 merge。

**提权（promote）**
`promote-dev-to-main` 工作流：job 内跑全套门禁（lint / mypy / vendor 三层 /
全量 pytest）→ 校验 main 是 dev 的祖先 → `--force-with-lease` 把 main
快进到 dev 的 sha。提权**不产生新提交**，故 main 上每个提交逐字节等于一个
已过 CI 的 dev 提交。用内置 `GITHUB_TOKEN` 推（无需 App/PAT），代价是推上去
后不触发后续工作流——门禁结论由 promote run 自身承载（CONTRIBUTING.md 8.4）。

**PR 目标守卫（main-pr-target-guard）**
对 base=main 的 PR 直接 exit 1。存在的原因：`main` 刻意**不开** PR 保护
（否则 promote 失效，见 BRANCHING.md §2.4），服务端不会拦以 `main` 为
base 的 PR；守卫让这类 PR 的错误**在开出来时就显式暴露**，而不是等到
promote 在 `main` 上撞 `git cherry` 判红。

**滚动同步的唯一实现（roll_local）**
`scripts/roll_local.py` 承载完整 roll 序列——检测 standalone_sha 变化后
执行整树重建 vendor → 派生 requirements → 两层注入 → 三层校验 → 全量
契约测试 → 更新 sync-state → 提交（连续失败计数 consecutive_failures；
网络抖动自动重试）。`sync-upstream.yml` 只编排它（+ 判据 + PR 轨道），
人手动跑同一脚本。双轨语义：vendor 轨跟 standalone
发布节奏，渲染模板/文本/参数三个平面轨直接跟 origin/main tip（提取源
即 main），两者的 skew 由全量契约测试在 roll 时把关。
**祖先校验门（2026-09-14 release 通道分析）**：standalone 分支除 main
构建外还会发布未合入 PR 的预览构建（f9e8c67 即 PR #306 中间态的预览）
——构建源不在 origin/main 祖先内时跳过 vendor roll（平面照常跟进、
state 留痕 last_skipped_build），防止未合入代码进入生产。release
通道（10 版全稳定、周~双周节奏）作为 breaking 项的阅读锚点：版本段
变更时 roll 日志提示查阅对应 release notes。

**消息结构对照（message-structure probe）**
最终发送消息的两侧活体对照工装：`scripts/message_structure_probe.py`
三个子命令（`fixture`/`url` 捕获 + `compare` 判定）。合成夹具零网络双
profile 覆盖 send_content 分支：forward（生产默认主路径：文本/投票/引用/
链接/图片/图集 alt/转发嵌套）与 flat（探针进程内临时置
`plite_need_forward_contents=False` 的平铺次路径）；`url` 模式活体解析
真实分享链接（含媒体下载）。上游侧在 nonebot 容器运行（桩 bot 上下文绕
开无适配器环境），桥侧在 astrbot 容器运行（桩事件不真发）。归一化词表：
上游 alconna Reference ≡ 桥 Comp.Nodes ≡ forward、图集「img+alt 复合段」
≡ 桥 _AltMedia、相邻文本段拼接（上游 UniMessage 合并/桥逐段）；媒体桩
必须返回 anyio.Path（上游 main 默认 use_base64=True 走异步读——2026-09-14
对照器抓到的第一处运行栈差异）。bot 名/uin 为配置面不参判。2026-09-13/14
结论：逐消息/逐段/逐节点等价（forward 8 段 + flat 3 段 + 真实 URL 消息）。
切分算法与 _ForwardText.split/text 另有 AST 结构指纹钉扎。
见 `tests/test_message_probe.py`。

## 契约

**双轨契约（dual-track contract）**
Hyrum's Law 防线。静态轨：import 面 AST 白名单 + vendor 公开 API 签名
syrupy 快照 + requirements 派生一致性（确定性，required check）。行为轨：
`match()` 离线黄金样本 + 真实网络 parse 快照（隔离区，非必需）。

**黄金样本（golden matches）**
`tests/test_match_golden.py` 中经真实 vendor 正则逐一验证的
平台 → URL 映射（27 平台正例 + 负例；键集合与 `parsers.load_all()` 注册的
可命中平台由文件末元测试机械对齐）。上游正则漂移时变红；上游新增可命中平台
而样本未同步时，覆盖率元测试同样变红——人工评审后补一条样本即可。

**隔离区（quarantine / `network` marker）**
需要真实外网的测试（`RUN_NETWORK_TESTS=1` 启用），默认跳过、不作为
required check——对应 Google Testing Blog 的 flaky 分层准入。

**stable_view（稳定视图）**
parse 快照前对 ParseResult 的规范化：剥离签名 URL、播放计数、时间戳
等易变字段，只保留 platform/title/author/content 截断。快照 diff 必须
人工评审，禁止「失败即重建」。

## 防护

**SSRF 四层防护**
`bridge/ssrf.py`（对齐 GitLab url_blocker）：① scheme 白名单 {http, https}；
② 端口规则（默认端口或 ≥1024）；③ getaddrinfo 全部结果逐 IP 校验
（含 stdlib 判定不到的 CGNAT 100.64.0.0/10 等 EXTRA_FORBIDDEN_NETWORKS）；
④ 解析即连接钉扎。全拒/放行清单见 `tests/test_ssrf.py`。

**覆盖面边界（勿误读为全局防护）**
本防护只挂载 **vendor 的两条出站面**：下载器 `DOWNLOADER.client` 的
httpx/curl_cffi 会话，与 28 个平台 parser 共用的 `BaseParser.httpx`。
不在范围内：AstrBot 框架自身的出站请求（`bridge/render.py` 经 `html_render`
访问 t2i 端点、NapCat 等适配器的上报链路）——这些由框架与部署侧配置负责，
桥不接管。范围随 `bridge/ssrf.py` 模块 docstring 的「覆盖 vendor 的两条出站面」
一段为准，改动挂载点须同步该段。

**钉扎（pinning）**
第 4 层机制：校验通过的 IP 直接成为连接目标。httpx 侧在 httpcore network
backend 层拨号已验证 IP（URL/SNI 保留原 hostname，连接池与 TLS 语义不变；
多 IP 按校验顺序故障转移），代理场景退回「请求目标改写为已验证 IP 字面量」
（fail-closed）；curl_cffi 侧注入 `CurlOpt.RESOLVE`。DNS 二次解析被绕开，
rebinding 失效。

**`_INHERIT_VENDOR_VERIFY`**
`bridge/ssrf.py` 命名常量（False）：显式继承上游 vendor 关闭 TLS 证书校验的
既定姿态（vendor 零修改约束下不做行为分叉）。风险敞口见 README「安全说明」。

**两段式渲染（two-stage render）**
`bridge/render.py`：本地 Jinja 先产出自包含 HTML（媒体内联为 base64 data URI，
24MB 总预算 / 8MB 单文件上限），再经 AstrBot 远程 t2i（`html_render`）
出 PNG。
