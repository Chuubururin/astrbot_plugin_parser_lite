"""供应链与合规基线的机械钉扎（CONTRIBUTING.md 第 13 节）。

背景：本仓库是**私有仓库 + Free 计划**，平台侧的供应链防线大多不可用 ——
实测 `/rulesets` 返回 403（"Upgrade to GitHub Pro or make this repository
public"）、`security_and_analysis` 为 null（无 GitHub Code Security，故 CodeQL
与私密漏洞报告都不可用）、`/vulnerability-alerts` 404。于是能落地的只剩
「写进仓库、由 CI 与本地门禁强制」的那一部分：

- 第三方 action 钉完整 commit SHA（Pinned-Dependencies）
- 顶层 permissions 最小权限（Token-Permissions）
- 依赖更新工具（Dependency-Update-Tool）
- 安全政策（Security-Policy）
- 发布产物带签名与 SBOM（Signed-Releases / SBOM）
- 依赖安装只有一处定义（DRY，消灭「假红锁死提权通道」的入口）

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
DELEGATING_WORKFLOWS = ("ci.yml", "promote-dev-to-main.yml", "release.yml", "sync-upstream.yml")

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


def test_every_delegating_workflow_uses_the_shared_setup_action() -> None:
    """需要装依赖的 job 必须委派给 `.github/actions/setup-env`。

    依据：GitHub 官方推荐用 composite action / reusable workflow 复用 CI 逻辑，
    避免同一份配置在多处漂移。本项目为此付过学费 —— 4 处安装清单各抄一遍，
    promote 那份漏了 ruff，门禁以 `No module named ruff` 假红，**整条提权通道
    被锁死**（main 永远推不动），而那是环境缺失、不是代码问题。

    引用形式是 `./...`（workspace-relative）：zizmor 的 self-repository 审计
    建议改用 `$/...`（GitHub 2026-07 起的新语法，不受运行时文件系统状态影响，
    且被平台视作 pinning），但 **actionlint 最新版 v1.7.12（2026-03-30）尚不认识
    `$/`**，会直接判 "ref is missing"；而 actionlint 是本地与 CI 双端硬门禁。
    故这里保留 `./` 并要求调用点带**显式行内豁免**（`# zizmor: ignore[...]`），
    而不是静默降级。复评触发条件：actionlint 支持 `$/` 后改回并撤掉豁免。
    """
    for name in DELEGATING_WORKFLOWS:
        doc = _load_yaml(WORKFLOWS / name)
        uses = [str(s.get("uses", "")) for s in _all_steps(doc)]
        assert "./.github/actions/setup-env" in uses, (
            f"{name} 未委派给复合 action .github/actions/setup-env"
        )
        # 必须带行内豁免注释：否则 zizmor --pedantic 会红，而豁免理由要写在
        # 工作流里（可读），不能只存在于某处文档。
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "zizmor: ignore[self-repository]" in text, (
            f"{name} 未对 self-repository 建议写明行内豁免与理由"
        )
        assert "$/.github/actions/setup-env" not in text, (
            f"{name} 用了 `$/` 语法 —— actionlint v1.7.12 不认识它，会让 lint 门禁红"
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


def test_gate_extras_covers_every_python_m_the_promote_gate_runs() -> None:
    """逐条对账：promote 门禁跑什么 `python -m <mod>`，gate 档就得装什么。

    这条比「列举包名」耐用：以后往 promote 里加门禁步骤时会自动暴露缺装。
    生效的安装集 = 基础档 + gate 分支（pytest 在基础档、ruff/mypy 在 gate 档，
    两者合起来才是 extras=gate 时真正装上的东西）。
    """
    doc = _load_yaml(WORKFLOWS / "promote-dev-to-main.yml")
    ran: set[str] = set()
    for step in _all_steps(doc):
        ran.update(re.findall(r"python\s+-m\s+([a-z_]+)", str(step.get("run", ""))))

    body = _code_lines("\n".join(str(s.get("run", "")) for s in _setup_env_steps()))
    base_branch = body.split("case ", 1)[0]
    gate_branch = body.split("gate)", 1)[1].split(";;", 1)[0]
    # 只看真正的 pip 安装行：变量名/echo 文案里的包名不算「装了」
    effective = _install_commands(base_branch + gate_branch)
    missing = [
        pkg for mod, pkg in GATE_MODULE_TO_PACKAGE.items() if mod in ran and pkg not in effective
    ]
    assert not missing, (
        f"promote 跑了 python -m {sorted(ran)}，但 extras=gate 未装 {missing}——会假红"
    )


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


# ---------------------------------------------------------------------------
# ⑤ 工作流文件安全基线（Pinned-Dependencies / Dangerous-Workflow）
# ---------------------------------------------------------------------------

_THIRD_PARTY_USE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@([0-9a-f]{40})$")


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
