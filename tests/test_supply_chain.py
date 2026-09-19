"""供应链与合规基线的机械钉扎（CONTRIBUTING.md 第 13 节）。

背景（2026-09-19 重构）：仓库从 **private 转为 public**，平台侧的供应链防线
从「大多不可用」变成「基本可用」。两边的事实都要记牢，否则会写出错的断言：

| 能力 | private + Free（旧） | public + Free（新） |
|---|---|---|
| `/rulesets`、`/branches/*/protection` | **403** Upgrade to GitHub Pro | 可用 |
| `security_and_analysis` | null（无 Code Security） | secret scanning 可用 |
| CodeQL（SAST） | 需 GHAS，不可用 | **免费** |
| `/vulnerability-alerts` | 404 | 可用 |
| 环境 required reviewers | 仅 public 可用 | 可用 |

**注意 CodeQL 那一行的口径**：转 public 后它**技术上可用**，但本项目在
2026-09-19 的第二次重构里**主动选择不做** —— 用户要求「更简单的 CICD」，
而 CodeQL 会引入一个异步的、需要运维理解的失败来源。所以：

- 本文件不再断言「codeql.yml 存在」；
- 但仍断言「不做 SAST」这件事**被如实记录**（见
  ``test_unsupported_standards_are_still_documented``），
  避免下次有人以为基线是完整的。

于是本文件守护的东西分成两类：
- **写进仓库、由 CI 与本地门禁强制**的（钉 SHA / 最小权限 / 依赖更新工具 /
  安全政策 / 发布物签名与 SBOM / 依赖安装单一实现）—— 这部分完全不变；
- **仍属平台侧、本项目主动不做的**（SAST / 每日配置巡检 / 签名提交）——
  必须有一条断言保证「不做的清单」存在且是最新的。

本文件把上面每一条都变成断言。**依据的标准条款写在每个测试的 docstring 里**，
并注明该条款来自哪个权威来源，避免以后有人「觉得多余」而删掉。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"

SETUP_ENV_PATH = ACTIONS / "setup-env" / "action.yml"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
SECURITY_PATH = REPO_ROOT / ".github" / "SECURITY.md"
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"
PINNED_TOOL_SCRIPT = REPO_ROOT / "scripts" / "pinned_tool_versions.py"
PRECOMMIT_CONFIG = REPO_ROOT / "config" / ".pre-commit-config.yaml"

# 依赖安装的调用方：必须全部委派给复合 action，不得各抄一份清单。
# ci.yml 的 lint job 不在其列 —— 它走 pre-commit，ruff 由钩子的隔离环境提供。
#
# **名单会变，所以另有一条断言按「行为」对账**（见
# ``test_delegating_workflow_list_is_not_stale``）：名单只应包含**确实装依赖**
# 的工作流，两边必须互相印证，不许沦为摆设。
#
# 2026-09-19 第二次重构：``promote-dev-to-main.yml`` 移出名单 —— 它变成了纯
# ref 手术（decide → fetch → cherry → push），不装依赖也不跑门禁。
# ``sync-upstream.yml`` 随本版移除（它依赖 lkg 分支）。若将来恢复，记得加回这里。
DELEGATING_WORKFLOWS = ("ci.yml", "release.yml")

# 门禁期需要「裸 python -m <mod>」的工具：模块名 → pip 包名。
GATE_MODULE_TO_PACKAGE = {"ruff": "ruff", "pytest": "pytest", "mypy": "mypy"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _all_steps(doc: dict) -> list[dict]:
    """展平一个 workflow 的所有 job 步骤（含 needs/if 等无关字段也一并返回）。"""
    steps: list[dict] = []
    for job in doc.get("jobs", {}).values():
        steps.extend(job.get("steps", []) or [])
    return steps


def _setup_env_steps() -> list[dict]:
    return _load_yaml(SETUP_ENV_PATH)["runs"]["steps"]


def _code_lines(text: str) -> str:
    """剥掉注释行，只留可执行内容。

    本仓库的注释里大量提到包名与文件名，直接 substring 断言会被**注释本身**
    满足（2026-09-19 反向验证抓到过：删掉安装行测试照绿）。
    """
    return "\n".join(
        ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    )


def _install_commands(text: str) -> str:
    """只保留**真正执行安装**的 pip 行，供包名断言使用。

    为什么还要再收窄一层：剥掉注释仍不够 —— 变量名与 echo 文案里也含包名。
    例如 gate 档里 `ruff_version="$(python scripts/pinned_tool_versions.py ruff)"`
    与 `echo "ruff 版本（...）"` 都含 "ruff"，于是 `"ruff" in gate_branch` 被它们
    满足：把 `pip install ruff` 删掉，测试照样绿。反向验证抓到过这个漏洞
    （case「setup-env 的 gate 档漏装 ruff」MISSED，2026-09-19）。
    """
    return "\n".join(ln for ln in _code_lines(text).splitlines() if "pip install" in ln)


# ---------------------------------------------------------------------------
# ① 依赖安装的唯一实现（DRY；消灭「清单漂移 → 假红锁死提权通道」这一类事故）
# ---------------------------------------------------------------------------


def _needs_dependencies(doc: dict) -> bool:
    """这个工作流是否**真的**需要装 Python 依赖？

    判据是行为而非声明：出现 ``pip install``、``python -m <mod>``、
    ``python3 -m <mod>`` 之一就算。用它取代「按名单点名」——
    **名单会漂移，行为不会**。
    """
    for step in _all_steps(doc):
        body = str(step.get("run", ""))
        if "pip install" in body:
            return True
        if re.search(r"python3?\s+-m\s+[a-z_]", body):
            return True
    return False


def test_every_workflow_that_needs_deps_delegates_to_the_shared_setup_action() -> None:
    """**凡是真的装依赖的工作流**，都必须委派给 `.github/actions/setup-env`。

    依据：GitHub 官方推荐用 composite action / reusable workflow 复用 CI 逻辑，
    避免同一份配置在多处漂移。本项目为此付过学费 —— 4 处安装清单各抄一遍，
    提权那份漏了 ruff，门禁以 `No module named ruff` 假红，**整条提权通道被锁死**
    （main 永远推不动），而那是环境缺失、不是代码问题。

    **判据为什么从「按名单」改成「按事实」**：2026-09-19 第二次重构后，
    ``promote-dev-to-main.yml`` 变成纯 ref 手术（decide → fetch → cherry →
    push），不装依赖也不跑 ``python -m``。此时「promote 必须委派」这条断言
    的事实前提**消失了**：它既不该被满足（那要凭空加回依赖安装），
    也不该被简单删掉（会连「以后加回来的门禁必须委派」一起丢掉）。
    改成按行为判定后，两个方向都守住了。

    引用形式是 `./...`（workspace-relative）：zizmor 的 self-repository 审计
    建议改用 `$/...`（GitHub 2026-07 起的新语法，不受运行时文件系统状态影响，
    且被平台视作 pinning），但 **actionlint 最新版 v1.7.12（2026-03-30）尚不认识
    `$/`**，会直接判 "ref is missing"；而 actionlint 是本地与 CI 双端硬门禁。
    故这里保留 `./` 并要求调用点带**显式行内豁免**（`# zizmor: ignore[...]`），
    而不是静默降级。复评触发条件：actionlint 支持 `$/` 后改回并撤掉豁免。
    """
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load_yaml(path)
        if not _needs_dependencies(doc):
            continue
        uses = [str(s.get("uses", "")) for s in _all_steps(doc)]
        assert "./.github/actions/setup-env" in uses, (
            f"{path.name} 里出现了依赖安装/门禁命令，却未委派给复合 action "
            ".github/actions/setup-env —— 这就是清单漂移事故的入口。"
        )
        # 必须带行内豁免注释：否则 zizmor --pedantic 会红，而豁免理由要写在
        # 工作流里（可读），不能只存在于某处文档。
        text = path.read_text(encoding="utf-8")
        assert "zizmor: ignore[self-repository]" in text, (
            f"{path.name} 未对 self-repository 建议写明行内豁免与理由"
        )
        assert "$/.github/actions/setup-env" not in text, (
            f"{path.name} 用了 `$/` 语法 —— actionlint v1.7.12 不认识它，会让 lint 门禁红"
        )


def test_delegating_workflow_list_is_not_stale() -> None:
    """DELEGATING_WORKFLOWS 名单必须与事实一致 —— 不许沦为摆设。

    名单只应包含**确实装依赖**的工作流。若某工作流被移出名单，但它仍在装依赖，
    就说明有人为了让测试变绿而把名字删了（本仓库 2026-09-19 差点这么干：
    promote 不再委派后，「把 promote 从名单删掉」是最省事的做法，
    但那会同时放过「promote 悄悄又装上了依赖」的可能）。

    所以这里逐个对账，并要求每个名字都能给出理由。
    """
    for name in DELEGATING_WORKFLOWS:
        doc = _load_yaml(WORKFLOWS / name)
        assert _needs_dependencies(doc), (
            f"{name} 被列在 DELEGATING_WORKFLOWS 里，但它并不需要依赖 —— "
            "请把它从名单移出，并在注释里说明它为什么不需要。"
        )

    # 反向：装了依赖却没进名单的工作流同样要暴露
    offenders = [
        p.name
        for p in sorted(WORKFLOWS.glob("*.yml"))
        if _needs_dependencies(_load_yaml(p)) and p.name not in DELEGATING_WORKFLOWS
    ]
    assert not offenders, (
        f"这些工作流装了依赖却没进 DELEGATING_WORKFLOWS：{offenders}。"
        "名单是给「谁该委派」做人工可读索引的，漏了它就等于漏了守护。"
    )


def test_no_workflow_hand_rolls_a_dependency_install_list() -> None:
    """不得在任何 workflow 里手抄安装清单（否则漂移入口又回来了）。

    允许 `python -m pip install pre-commit`（ci.yml 的 lint job 自装 pre-commit，
    它自己管钩子环境）；禁止的是把 requirements / 测试依赖清单再抄一遍。
    """
    for name in DELEGATING_WORKFLOWS:
        doc = _load_yaml(WORKFLOWS / name)
        for step in _all_steps(doc):
            body = str(step.get("run", ""))
            hand_rolled = (
                "pip install -r requirements.txt",
                "pip install -r tests/requirements",
            )
            for forbidden in hand_rolled:
                assert forbidden not in body, (
                    f"{name} 的步骤「{step.get('name', '?')}」手抄了安装清单：{forbidden}"
                )


def test_setup_env_installs_the_base_dependency_set() -> None:
    """复合 action 的基础档必须含运行期依赖 + pytest 三件套。

    pytest 三件套是硬要求：漏装会让全量契约测试以 `No module named pytest`
    直接红（sync-upstream 曾连续 3 次失败并触发 MC-11 熔断）。
    """
    body = _install_commands("\n".join(str(s.get("run", "")) for s in _setup_env_steps()))
    base_needles = (
        "requirements.txt",
        "requirements/host-provided.txt",
        "pytest",
        "pytest-asyncio",
        "syrupy",
    )
    for needle in base_needles:
        assert needle in body, f"setup-env 基础档缺 {needle}"


def test_setup_env_extras_levels_install_what_they_promise() -> None:
    """extras 三档的语义必须与文档一致：none / type / gate。

    gate 档是本仓库最关键的一处：promote 的门禁直接 `python -m ruff`，
    所以 ruff / mypy / types-qrcode 必须进 venv。
    """
    body = _code_lines("\n".join(str(s.get("run", "")) for s in _setup_env_steps()))
    # 三档都要在 case 分支里出现（否则拼写错误会静默落到 *) 的报错分支）
    for level in ("none)", "type)", "gate)"):
        assert level in body, f"setup-env 缺 extras 分支 {level}"
    # gate 档的工具（在 gate) 到 ;; 之间，且只看真正的 pip 安装行 ——
    # 否则 `ruff_version` 这个变量名就会满足「装了 ruff」的断言）
    gate_branch = body.split("gate)", 1)[1].split(";;", 1)[0]
    gate_installs = _install_commands(gate_branch)
    for pkg in ("ruff", "mypy", "types-qrcode"):
        assert pkg in gate_installs, f"extras=gate 未装 {pkg}——提权门禁会假红并锁死通道"
    type_branch = body.split("type)", 1)[1].split(";;", 1)[0]
    type_installs = _install_commands(type_branch)
    for pkg in ("mypy", "types-qrcode"):
        assert pkg in type_installs, f"extras=type 未装 {pkg}——mypy 会报 import-untyped"


def test_setup_env_derives_ruff_version_from_precommit_not_hardcoded() -> None:
    """ruff 版本必须从 pre-commit 的 rev **推导**，不得手抄。

    依据：pre-commit 的 ruff 钩子跑在自己的隔离环境里，版本由 `rev:` 决定；
    job 里裸 `python -m ruff` 装的是 pip 版本。两处不一致会让同一个文件
    「一边判过、一边判不过」（格式化判定分歧）。把版本写成单一来源是唯一
    不会漂移的做法。
    """
    body = _code_lines("\n".join(str(s.get("run", "")) for s in _setup_env_steps()))
    assert "scripts/pinned_tool_versions.py" in body, (
        "setup-env 未调用 scripts/pinned_tool_versions.py —— ruff 版本又变成手抄了"
    )
    installs = _install_commands(body)
    assert "ruff==${ruff_version}" in installs, (
        "gate 档的 ruff 安装行没有用推导出来的版本变量 —— 版本可能被写死了"
    )
    assert not re.search(r"ruff==\d", installs), (
        "setup-env 里出现了写死的 ruff 版本（ruff==<数字>）——应改为从 pre-commit 的 rev 推导"
    )


def test_pinned_tool_versions_agrees_with_yaml_parse() -> None:
    """`pinned_tool_versions` 的正则解析必须与 PyYAML 解析结果一致。

    该脚本刻意不依赖 PyYAML（scripts/ 不依赖 PyYAML 是本仓库既有约定，
    见 tests/requirements-test.txt 对 PyYAML 的注释），所以它的解析是正则。
    这条测试用 PyYAML 做**交叉验证**，把正则的脆弱性兜住。
    """
    spec = importlib.util.spec_from_file_location("pinned_tool_versions", PINNED_TOOL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    got = module.pinned_versions()
    doc = _load_yaml(PRECOMMIT_CONFIG)
    expected = {
        "ruff": next(
            repo["rev"].lstrip("v")
            for repo in doc["repos"]
            if "ruff-pre-commit" in str(repo.get("repo", ""))
        )
    }
    assert got == expected, f"正则解析 {got} 与 PyYAML 解析 {expected} 不一致"
    # 结果必须真的是版本号形态，而不是把整个 rev 串带进来
    assert re.fullmatch(r"\d+\.\d+\.\d+", got["ruff"]), f"ruff 版本形态可疑：{got['ruff']}"


def test_gate_extras_covers_every_python_m_run_by_gated_workflows() -> None:
    """逐条对账：跑门禁的工作流用什么 `python -m <mod>`，gate 档就得装什么。

    **判据为什么改**：原断言只看 promote，因为那时 promote 是「唯一带门禁的
    提权通道」。2026-09-19 第二次重构后 promote 变成纯 ref 手术，
    `ran` 恒为空集 ⇒ 断言恒真 ⇒ **空断言**（永远绿、毫无守护力）。
    空断言比没有断言更危险：它让人以为这里有守护。

    改法两条：
      A. 对账范围从「promote」扩到**所有跑 `python -m` 的工作流**，
         这样将来谁加了门禁都自动纳入对账；
      B. 把「promote 刻意不跑门禁」这件事本身钉住 —— 它是本设计的关键简化，
         不是疏漏，必须由断言显式声明，否则下次有人会「顺手补上」。

    依据：``setup-env`` 的 gate 档必须覆盖门禁期所有裸 ``python -m <mod>``，
    否则会以 ``No module named...`` 假红 —— 而那是环境问题不是代码问题，
    会让门禁失去可信度（本项目真实踩过：提权通道被假红锁死）。
    """
    # A. 全仓对账
    ran: dict[str, set[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load_yaml(path)
        mods: set[str] = set()
        for step in _all_steps(doc):
            mods.update(re.findall(r"python3?\s+-m\s+([a-z_]+)", str(step.get("run", ""))))
        if mods:
            ran[path.name] = mods

    assert ran, (
        "没有任何工作流跑 `python -m <mod>`？本仓库的门禁（ruff/mypy/pytest）"
        "不可能全在别处。若确实改成了别的调用方式，请同步更新本断言。"
    )

    body = _code_lines("\n".join(str(s.get("run", "")) for s in _setup_env_steps()))
    base_branch = body.split("case ", 1)[0]
    gate_branch = body.split("gate)", 1)[1].split(";;", 1)[0]
    # 只看真正的 pip 安装行：变量名/echo 文案里的包名不算「装了」
    effective = _install_commands(base_branch + gate_branch)

    all_mods = {m for mods in ran.values() for m in mods}
    # 排除两类，各有理由：
    #   · `pip`  —— `python -m pip install …` 是**安装手段**，不是门禁工具，
    #              它天然不会出现在 gate 档的安装清单里（自己不用装自己）。
    #   · `pytest` —— 在**基础档**而非 gate 档，所以不在 GATE_MODULE_TO_PACKAGE 里。
    non_gate_modules = {"pip", "pytest"}
    unknown = all_mods - set(GATE_MODULE_TO_PACKAGE) - non_gate_modules
    assert not unknown, (
        f"这些模块被 `python -m` 调用，但不在 GATE_MODULE_TO_PACKAGE 的映射里："
        f"{sorted(unknown)}。需要确认它是否在 gate 档装上了，"
        "或在 non_gate_modules 里给出不装的理由。"
    )
    missing = [
        pkg
        for mod, pkg in GATE_MODULE_TO_PACKAGE.items()
        if mod in all_mods and pkg not in effective
    ]
    assert not missing, (
        f"工作流跑了 python -m {sorted(all_mods)}，但 extras=gate 未装 {missing}——会假红"
    )


def test_ci_is_the_only_gated_workflow_after_promote_was_simplified() -> None:
    """promote **刻意**不跑门禁 —— 这是简化的一部分，必须显式钉住。

    为什么可以安全地不跑：本模型里 promote 只做「把 main 指针移到 dev 所在的
    那个提交」。而那个提交在 dev 上**已经跑过完整 CI**（ci.yml 的 push:dev）。
    所以 promote 阶段的重复门禁不增加任何保证，只增加两样东西：
      · 一个会因为「runner 网络抖动 / pip 源故障」而红的失败面；
      · 一整块需要运维理解的配置（装什么、装哪个版本）。

    代价如实声明：若有人绕过保护直接往 dev 推而 CI 尚未跑完就触发 promote，
    main 会短暂指向一个「还没验证完」的提交。缓解：dev 上的 required checks
    让「未通过就合并」不可能；且 main 与 dev 始终同 sha，真出问题回退一次即可。
    复评触发条件：若将来 promote 需要做「发布前额外校验」（如版本号递增检查、
    changelog 存在性），那时才需要给它装依赖并委派 setup-env。
    """
    promote = _load_yaml(WORKFLOWS / "promote-dev-to-main.yml")
    assert not _needs_dependencies(promote), (
        "promote-dev-to-main.yml 又开始装依赖/跑门禁了。本版刻意让它保持纯 ref 手术；"
        "若确需在提权前校验，请在 doc/BRANCHING.md §5 记录理由，"
        "并把它加回 DELEGATING_WORKFLOWS（届时 test_delegating_workflow_list_is_not_stale "
        "会要求你这么做）。"
    )

    # 同时确认：确实还有别的工作流在跑门禁（否则「简化」变成了「删光」）
    ci = _load_yaml(WORKFLOWS / "ci.yml")
    assert _needs_dependencies(ci), "ci.yml 应当仍在跑门禁；否则门禁整体消失了"


# ---------------------------------------------------------------------------
# ② 依赖更新工具（Scorecard: Dependency-Update-Tool）
# ---------------------------------------------------------------------------


def test_dependabot_covers_actions_and_hand_maintained_python_deps() -> None:
    """Dependabot 必须覆盖 GitHub Actions 与**手工维护**的测试依赖。

    依据：Scorecard 的 Dependency-Update-Tool 检查要求仓库启用 Dependabot 或
    Renovate。本仓库把 action 钉到 SHA（不可变），SHA 不跟版，所以必须有工具开
    升级 PR —— 否则钉扎就从安全措施退化成版本冻结。

    刻意**不**覆盖根目录 pip：requirements.txt 与 requirements/host-provided.txt
    是 vendor/_upstream/pyproject.toml 的派生产物（文件头写明「勿手改」），
    由 tests/test_requirements_sync.py 逐字节守护。让 Dependabot 改它们，
    每个 PR 都注定违反派生契约 → 纯噪声。
    """
    doc = _load_yaml(DEPENDABOT_PATH)
    assert doc.get("version") == 2, "dependabot.yml 的 version 必须是 2"
    entries = doc["updates"]
    seen = {(e["package-ecosystem"], e["directory"]) for e in entries}
    assert ("github-actions", "/") in seen, "Dependabot 未覆盖根目录的 GitHub Actions"
    assert ("pip", "/tests") in seen, "Dependabot 未覆盖手工维护的 tests/requirements-test.txt"
    assert ("pip", "/") not in seen, (
        "Dependabot 不应覆盖根目录 pip：那两个文件是派生产物（勿手改），"
        "改它们必然违反 tests/test_requirements_sync.py 的契约"
    )
    for entry in entries:
        assert entry.get("target-branch") == "dev", (
            f"{entry['package-ecosystem']} 的 target-branch 必须是 dev——"
            "main 是发布指针，只能由 promote 快进推进"
        )
        assert entry["schedule"]["interval"] == "weekly", "依赖更新应为周更"
        # 冷却期（zizmor 的 dependabot-cooldown 审计，置信度 High）：新版本发布后
        # 先等一段时间再开 PR，让社区有时间发现恶意投毒或「发布即坏」的版本。
        # 本仓库把 action 钉到不可变 SHA，等于把「跟上安全修复」全交给 Dependabot，
        # 那就更不能抢在社区验证之前把新版本拉进来。
        cooldown = entry.get("cooldown", {})
        days = cooldown.get("default-days")
        assert isinstance(days, int) and days >= 7, (
            f"{entry['package-ecosystem']} 缺 cooldown 或冷却期不足 7 天：{cooldown}"
        )


# ---------------------------------------------------------------------------
# ③ 安全政策（Scorecard: Security-Policy）
# ---------------------------------------------------------------------------


def test_security_policy_exists_where_scorecard_looks() -> None:
    """SECURITY.md 必须在 Scorecard 会查找的位置之一。

    依据：Scorecard 只认根目录 / `.github/` / `docs/` 下的
    SECURITY.{md,markdown,adoc,rst}（不区分大小写，见 scorecard 的
    checks/raw/security_policy.go）。本仓库 `docs/` 是 gitignore 的，
    所以只能是 `.github/SECURITY.md`。
    """
    assert SECURITY_PATH.is_file(), "缺 .github/SECURITY.md"


def test_security_policy_satisfies_every_scorecard_scoring_tier() -> None:
    """SECURITY.md 要同时满足 Scorecard 的三档评分要素。

    依据（scorecard 源码 security_policy.go 的 collectPolicyHits）：
      - 6/10：含**链接**（http/https URL 或邮箱）
      - +3/10：链接之外还有自由文本
      - +1/10：含 "Vuln"/"Disclos" 字样，且含 1-4 位数字（时限）
    这里逐档断言，避免写成「有链接就完事」的空壳政策。
    """
    text = SECURITY_PATH.read_text(encoding="utf-8")

    # 链接档：Scorecard 的 URL / 邮箱正则（与其源码保持一致）
    url_re = re.compile(r"(http|https)://[a-zA-Z0-9./?=_%:-]*")
    email_re = re.compile(r"\b[A-Za-z0-9._%+-]+(@|\\?\[at\\?\])[A-Za-z0-9.-]+\.[A-Za-z]{2,6}\b")
    assert url_re.search(text), "SECURITY.md 缺 http/https 链接（Scorecard 链接档）"
    assert email_re.search(text), "SECURITY.md 缺邮箱（Scorecard 链接档）"

    # 自由文本档：去掉链接与邮箱后，仍有实质文字
    stripped = email_re.sub("", url_re.sub("", text))
    prose = _code_lines(stripped)
    assert len(prose) > 200, "SECURITY.md 除链接外几乎没有自由文本（Scorecard 自由文本档）"

    # 政策专项档：Vuln/Disclos + 数字（时限）
    assert re.search(r"(?i)(vuln|disclos)", text), "SECURITY.md 缺 vulnerability/disclosure 表述"
    assert re.search(r"\b[0-9]{1,4}\b", text), "SECURITY.md 缺时限数字（如 90 天）"


def test_security_policy_assets_match_the_release_workflow() -> None:
    """SECURITY.md 里承诺的 release 资产，必须与 release.yml 实际产出的名字一致。

    防的是「文档说能离线验证，实际 release 里没有那个文件」——那比不写更糟。
    """
    policy = SECURITY_PATH.read_text(encoding="utf-8")
    release = _code_lines((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    for asset in (".zip.intoto.jsonl", "sbom.spdx.json", "sbom.spdx.json.intoto.jsonl"):
        assert asset in policy, f"SECURITY.md 未声明资产 {asset}"
        assert asset in release, f"release.yml 未产出 SECURITY.md 承诺的资产 {asset}"


# ---------------------------------------------------------------------------
# ④ 发布信任链（Scorecard: Signed-Releases / SBOM）
# ---------------------------------------------------------------------------


def test_release_generates_and_attests_an_sbom() -> None:
    """发布必须产出 SBOM，并对它做 Sigstore 证明。

    依据：Scorecard 的 SBOM 检查；以及 NTIA/CISA 的 SBOM 最低要素要求
    「SBOM 应作为发布产物发布」。用 Syft（anchore/sbom-action）产出 SPDX JSON。
    """
    release = _code_lines((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    assert "anchore/sbom-action@" in release, "release.yml 未用 Syft 生成 SBOM"
    assert "format: spdx-json" in release, "SBOM 应为 SPDX JSON 格式"
    assert "sbom-path: sbom.spdx.json" in release, "未对 SBOM 做 attestation"


def test_release_attaches_provenance_bundle_to_the_release() -> None:
    """证明 bundle 必须落成 **release 附件**，不能只写进 attestation store。

    依据：Scorecard 的 Signed-Releases 只扫 release **资产的文件名后缀**
    （provenance 认 `.intoto.jsonl`，签名认 `.asc/.minisig/.sig/.sign/
    .sigstore/.sigstore.json`，见 probes/releasesHaveProvenance 与
    releasesAreSigned 的 impl.go）。只写 store 的话，「有证明」会被读成
    「没证明」；而且官方离线验证流程要求手上先有 bundle 文件。
    """
    release = _code_lines((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    assert ".intoto.jsonl" in release, "未把 provenance bundle 归档为 .intoto.jsonl 附件"
    assert "bundle-path" in release, "未取 actions/attest 的 bundle-path 输出"
    assert "gh release create" in release, "缺 gh release create"
    # 归档出来的每个资产都必须真的挂到 release 上（写了文件却没上传 = 白做）。
    # 这里比对的是上传段里的实际字面量：zip 相关资产用 ${ZIP} 变量，SBOM 用字面名。
    upload = release.split("gh release create", 1)[1]
    uploaded_assets = (
        "${ZIP}.intoto.jsonl",
        "${ZIP}.sha256",
        "sbom.spdx.json.intoto.jsonl",
        '"sbom.spdx.json"',
    )
    for asset in uploaded_assets:
        assert asset in upload, f"release.yml 归档了 {asset} 但没传给 gh release create"


def test_release_keeps_least_privilege_and_protected_environment() -> None:
    """发布 job 的权限必须是「顶层只读 + job 级按需」，且经人工放行环境。

    依据：Scorecard 的 Token-Permissions（顶层只读、写权限下放到 job 级得满分）；
    以及 GitHub 的 Secure use 指南（用 environment + required reviewer 保护
    能签发 id-token 的发布作业）。
    """
    doc = _load_yaml(WORKFLOWS / "release.yml")
    top = doc.get("permissions")
    assert top == {"contents": "read"}, f"release.yml 顶层权限必须只有 contents: read，实为 {top}"
    job = doc["jobs"]["release"]
    assert job.get("environment") == "release", "发布 job 必须走受保护环境 release"
    assert set(job["permissions"]) == {"contents", "id-token", "attestations"}, (
        "发布 job 的写权限集合变了——请同步复核是否需要"
    )
    assert job["permissions"]["contents"] == "write"


def test_release_environment_is_a_real_gate_for_public_repos() -> None:
    """`environment: release` 在 public 下是**真的**门；注释不得再声称它不可用。

    背景：旧仓库是 private + Free，官方文档明确写「如需在私有或内部仓库中访问
    环境、环境机密和部署分支，必须使用 GitHub Pro、GitHub Team 或 GitHub
    Enterprise」。也就是说那个 `environment:` 声明当时是**一道不存在的门**，
    而注释承诺了「人工放行」——典型的「文档写了但平台没给」。

    转 public 后它真实生效。两条要守：
      ① 注释必须反映现状（否则误导后人以为门是假的，从而不配 reviewer）；
      ② timeout 必须覆盖人工审批（见 test_branch_model.py 的同类断言）。
    """
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    # 旧方案的过时陈述必须消失
    for stale in ("部署保护规则", "私有仓库", "GitHub Pro"):
        # 允许出现在「解释历史」的语境里，但不得出现在断言现状的句子中；
        # 这里用较宽松的口径：只要「必须使用 GitHub Pro」这类硬陈述不在即可
        assert f"必须使用 {stale}" not in text, f"release.yml 仍含过时的可用性陈述：{stale}"
    # 现状必须写清
    assert "required reviewers" in text or "required reviewer" in text, (
        "release.yml 必须说明 environment 的 required reviewers 会真的阻塞 job"
    )
    assert "超时" in text or "timeout" in text, "必须说明超时包含人工审批等待时间"


def test_sast_is_deliberately_absent_and_recorded_as_such() -> None:
    """CodeQL **不在**本仓库，且这件事必须被记录为「主动不做」而非遗忘。

    Scorecard 的 SAST 检查只认 `github/codeql-action` 或 SonarCloud。
    转 public 后 CodeQL 技术上是免费的，本项目仍选择不做 —— 理由是
    用户要求「更简单的 CICD」：CodeQL 会引入一个**异步**的失败来源
    （首次扫描 5–10 分钟），且它的结果需要人来判读，对单人维护 + 运维使用的
    场景收益低于理解成本。

    这条断言的作用是**防止沉默漂移**：若有人悄悄加回 codeql.yml，
    会在这里变红，从而被迫显式更新这一决策与本文件。

    与之配套的是 ``test_unsupported_standards_are_still_documented``：
    「不做」必须写在文档里，不能让读者以为基线是完整的。
    """
    assert not (WORKFLOWS / "codeql.yml").exists(), (
        "codeql.yml 出现了。本版刻意不做 SAST —— 若确实要加回，"
        "请同时更新本测试、doc/BRANCHING.md 的「仍未做」清单与 CONTRIBUTING.md。"
    )
    # Scorecard 的 SAST 项因此为空 —— 这必须在文档里如实声明
    doc = (REPO_ROOT / "doc" / "BRANCHING.md").read_text(encoding="utf-8")
    assert "SAST" in doc or "CodeQL" in doc, (
        "doc/BRANCHING.md 必须写明 SAST/CodeQL 未做，否则读者会以为基线完整"
    )


def test_enforcement_surfaces_are_declared_impossible_or_covered() -> None:
    """每一项强制力都必须要么有对应实现，要么在文档里明确声明不做。

    这是「不许悄悄略过」的机械保证。本版移除了 CodeQL 与每日巡检之后，
    有两项从「已覆盖」退回「主动不做」，必须仍然出现在清单里：

    - **SAST**：撤下 codeql.yml
    - **配置漂移巡检**：撤下 protection-audit.yml

    另有两项属服务端开关（不在代码里），本版也**不做巡检**，
    但应在文档的「仍未做」清单里点名，说明是靠人工在 Settings 确认的。
    """
    doc = (REPO_ROOT / "doc" / "BRANCHING.md").read_text(encoding="utf-8")
    assert "仍未做" in doc or "不做" in doc, (
        "doc/BRANCHING.md 必须有「仍未做/不做」的清单——否则读者会以为基线是完整的"
    )
    # SAST 与巡检必须被点名（本版的两处主动回退）
    for item in ("SAST", "巡检"):
        assert item in doc, f"doc/BRANCHING.md 未说明「{item}」是否在做"
    # 且这两个工作流确实不在
    for gone in ("codeql.yml", "protection-audit.yml"):
        assert not (WORKFLOWS / gone).exists(), f"{gone} 应已移除"


def test_protection_drift_is_acknowledged_as_unmonitored() -> None:
    """必须如实声明：服务端配置**没有**自动巡检。

    这是本版一个**真实存在的缺口**，不能含糊过去。分支保护可以被人在
    Settings 上点两下关掉，而代码仓库里没有任何痕迹。上一版用
    protection-audit.yml 每日巡检来堵它；本版为了「更简单」撤掉了巡检，
    代价就是**这个缺口重新打开**。

    诚实声明它，比假称「已有巡检」好得多 —— 后者会让维护者放弃手动复核。
    缓解措施（写在文档里）：promote 是唯一改 main 的路径，而它每次运行都会
    真的去推 main；一旦保护被关掉且有人直推，`git cherry` 检查会让下次
    promote 响亮失败，从而暴露问题。
    """
    doc = (REPO_ROOT / "doc" / "BRANCHING.md").read_text(encoding="utf-8")
    assert "漂移" in doc, "必须写明「服务端配置可能被点掉且无巡检」，否则维护者不会去手动确认"
    assert "apply_branch_protection.sh" in doc, "必须指出重新应用配置的命令，让维护者知道如何修复"


# ---------------------------------------------------------------------------
# ⑤ 工作流文件安全基线（Pinned-Dependencies / Dangerous-Workflow）
# ---------------------------------------------------------------------------

# 第三方 action 的 uses 形态：owner/repo[/subpath...]@<40 位 sha>
#
# 注意 `subpath` 这一步不能省：形如 `owner/repo/subpath@<sha>` 是**合法**且
# 常见的形态（例如 codeql 把 init / analyze 拆成同一仓库的子路径）。
# 早期版本的正则只允许 owner/repo@sha，于是引入这类 action 后这条断言会
# **误报** ——把已正确钉扎的 action 判成未钉扎。
# 教训：断言的正则必须覆盖平台允许的**全部**合法形态，否则它拦的不是违规，
# 而是「用的形态我没预料到」。本版虽然不再用 codeql，但正则保留 subpath
# 支持——下一次引入任何多级路径的 action 时不会再踩同一个坑。
_THIRD_PARTY_USE = re.compile(
    r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*@([0-9a-f]{40})$"
)


def _uses_values() -> list[tuple[str, str]]:
    """(文件相对路径, uses 值) —— 覆盖 workflows 与复合 action。"""
    found: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        found.extend(
            (path.name, str(step["uses"]))
            for step in _all_steps(_load_yaml(path))
            if "uses" in step
        )
    for path in sorted(ACTIONS.glob("*/action.yml")):
        found.extend(
            (f"{path.parent.name}/action.yml", str(step["uses"]))
            for step in _load_yaml(path)["runs"]["steps"]
            if "uses" in step
        )
    return found


def test_every_third_party_action_is_pinned_to_a_full_commit_sha() -> None:
    """第三方 action 必须钉到完整 40 位 commit SHA。

    依据：GitHub Secure use 指南 —— 「将 action 固定到完整长度的 commit SHA
    是目前把 action 当作不可变发布的唯一方式」；Scorecard 的
    Pinned-Dependencies 同样要求。tag 可被移动或删除，SHA 不能。

    仓库内 action 用 `$/` 前缀（self-repository 语法），不适用 SHA 钉扎。
    """
    unpinned = [
        (where, uses)
        for where, uses in _uses_values()
        if not uses.startswith(("$/", "./")) and not _THIRD_PARTY_USE.match(uses)
    ]
    assert not unpinned, f"存在未钉到完整 SHA 的 action：{unpinned}"


def _pins_without_version_comment(path: Path, label: str) -> list[tuple[str, str]]:
    """找出「钉了 SHA 但同一行没有 `# vX.Y.Z` 注释」的引用。"""
    found: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.search(r"uses:\s*(\S+@[0-9a-f]{40})\s*(#.*)?$", line)
        if m and not (m.group(2) or "").strip():
            found.append((label, m.group(1)))
    return found


def test_pinned_actions_keep_a_version_comment_for_dependabot() -> None:
    """钉 SHA 的同一行必须留 `# vX.Y.Z` 注释。

    依据：GitHub 文档明确说明 Dependabot 依赖行内注释来更新版本文档 ——
    「当注释在同一行时更新版本文档：actions/checkout@<commit> #<tag>」。
    没有注释，升级 PR 就只能看到一个裸 SHA，评审无从判断升了什么。
    """
    missing: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        missing.extend(_pins_without_version_comment(path, path.name))
    for path in sorted(ACTIONS.glob("*/action.yml")):
        missing.extend(_pins_without_version_comment(path, f"{path.parent.name}/action.yml"))
    assert not missing, f"以下钉扎缺版本注释：{missing}"


def test_workflows_avoid_dangerous_triggers() -> None:
    """不得使用 `pull_request_target` / `workflow_run` 检出不可信代码。

    依据：Scorecard 的 Dangerous-Workflow 是**唯二的 Critical 风险项**之一；
    GitHub Secure use 指南指出这两个触发器与显式 PR checkout 组合会让 PR 作者
    能攻陷仓库。本仓库用不到它们，所以这里做的是「防止以后被引入」。
    """
    offenders: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load_yaml(path)
        triggers = doc.get("on", doc.get(True, {})) or {}
        offenders.extend(
            (path.name, bad) for bad in ("pull_request_target", "workflow_run") if bad in triggers
        )
    assert not offenders, f"出现高危触发器：{offenders}"


def test_workflows_declare_top_level_read_only_permissions() -> None:
    """每个工作流的顶层 permissions 必须存在且只读。

    依据：Scorecard 的 Token-Permissions —— 顶层设为 read-only、写权限下放
    job 级得满分；「只有 job 级定义、顶层缺失」会被扣分，因为新增 job 时容易
    因人为失误漏定义。
    """
    problems: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load_yaml(path)
        top = doc.get("permissions")
        if top is None:
            problems.append(f"{path.name}: 缺顶层 permissions")
        elif top != {"contents": "read"}:
            problems.append(f"{path.name}: 顶层 permissions 非只读 -> {top}")
    assert not problems, "顶层权限不合规：" + "; ".join(problems)


# ---------------------------------------------------------------------------
# ⑥ CODEOWNERS（GitHub Secure use: 用 CODEOWNERS 控制工作流文件的变更）
# ---------------------------------------------------------------------------


def test_codeowners_covers_the_supply_chain_surface() -> None:
    """CODEOWNERS 必须覆盖「改动即改变发布/提权/门禁行为」的路径。

    依据：GitHub Secure use 指南建议把 `.github/workflows` 纳入代码所有者，
    使工作流变更需指定审核人批准。

    如实声明局限：本仓库是私仓 + Free 计划，Rulesets 实测 403，所以
    CODEOWNERS **没有平台级强制力**，只有 PR 界面的自动请求评审。强制力由
    pre-push 钩子与 promote 自带门禁补偿。
    """
    text = CODEOWNERS_PATH.read_text(encoding="utf-8")
    patterns = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        assert len(fields) >= 2, f"CODEOWNERS 行缺少 owner：{line!r}"
        patterns[fields[0]] = fields[1:]

    for required in ("*", "/.github/workflows/", "/.github/actions/", "/scripts/", "/vendor/"):
        assert required in patterns, f"CODEOWNERS 未覆盖 {required}"
    for pattern, owners in patterns.items():
        assert all(o.startswith("@") for o in owners), f"{pattern} 的 owner 格式不对：{owners}"


@pytest.mark.parametrize("path", [SETUP_ENV_PATH, DEPENDABOT_PATH, CODEOWNERS_PATH])
def test_supply_chain_files_are_tracked_by_git(path: Path) -> None:
    """新增的供应链文件必须真的能被 git 跟踪（不能被 .gitignore 吞掉）。

    这条防的是「文件写好了但没进版本库」：`.gitignore` 里有 `docs/` 之类的
    目录级规则，一旦放错位置，CI 里读到的是不存在的文件。
    """
    rel = path.relative_to(REPO_ROOT).as_posix()
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for raw in gitignore.splitlines():
        rule = raw.strip()
        if not rule or rule.startswith("#") or rule.startswith("!"):
            continue
        assert not rel.startswith(rule.rstrip("/") + "/"), (
            f"{rel} 被 .gitignore 的规则 {rule!r} 排除——CI 里读不到它"
        )
