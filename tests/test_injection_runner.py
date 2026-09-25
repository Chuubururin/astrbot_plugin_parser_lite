"""两层注入单命令入口契约：提取（四数据面）→ 分析 → 生成 → 校验。

scripts/run_injection.py 是 sync-upstream 工作流与本地开发的编排层：一次
进程完成上游 main 数据面提取（显示文本 + 渲染参数 + 渲染模板 + 上游文档
README，共享上游文件读取与单次 rev-parse）、第一层分析、第二层生成与新鲜度
校验。锚点规则、防火墙与产物格式仍归属各自脚本模块（单一事实源）；本层漂移
或工件过期时此处变红。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_PATH = REPO_ROOT / "scripts" / "run_injection.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _load("run_injection", RUNNER_PATH)


@pytest.fixture(scope="module")
def sandbox_repo(runner: ModuleType) -> Generator[Path]:
    """写入模式测试的沙箱仓库：生成工件与模板复制自真实仓库，重定向全部写目标。

    run_injection 的写目标（gen 六工件 + templates/ prune + ANALYSIS_PATH）都是
    模块加载时由各自 REPO_ROOT 派生的常量；逐路径重定向到沙箱后
    runner.main(["--offline"]) 不再触碰真实工作树。读取面（vendor/_upstream/*.json、
    templates/ 源）保持指向真实仓库——--check 的新鲜度语义因此不变。
    手工保存/还原（monkeypatch 为函数作用域，不能用于模块级 fixture）。

    对称断言钉住「build_artifacts 的全部键都落在沙箱内」：若编排层将来新增
    写目标而本 fixture 未同步更新，此处先红（而不是悄悄写回真实工作树）。
    """
    sandbox = Path(tempfile.mkdtemp(prefix="plite-runner-sandbox-"))
    for name in (
        "_conf_schema.json",
        "bridge/gen_config.py",
        "metadata.yaml",
        "README.md",
        "bridge/texts.py",
        "bridge/render_params.py",
    ):
        # 沙箱内保持扁平：本 fixture 只关心写目标重定向，不复制目录结构
        shutil.copy2(REPO_ROOT / name, sandbox / Path(name).name)
    templates = sandbox / "templates"
    shutil.copytree(REPO_ROOT / "templates", templates)
    upstream = sandbox / "vendor" / "_upstream"
    upstream.mkdir(parents=True)
    for name in ("display_texts.json", "render_params.json", "render_templates.json"):
        shutil.copy2(REPO_ROOT / "vendor" / "_upstream" / name, upstream / name)
    targets = [
        (runner.gen, "SCHEMA_PATH", sandbox / "_conf_schema.json"),
        (runner.gen, "GEN_CONFIG_PATH", sandbox / "gen_config.py"),
        (runner.gen, "METADATA_PATH", sandbox / "metadata.yaml"),
        (runner.gen, "README_PATH", sandbox / "README.md"),
        (runner.gen, "TEXTS_PATH", sandbox / "texts.py"),
        (runner.gen, "RENDER_PARAMS_PATH", sandbox / "render_params.py"),
        (runner.gen, "RENDER_TEMPLATES_DIR", templates),
        (runner, "RENDER_TEMPLATES_DIR", templates),
        (runner.analyze_mod, "ANALYSIS_PATH", sandbox / "vendor_analysis.json"),
    ]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in targets]
    for mod, name, value in targets:
        setattr(mod, name, value)
    try:
        # 对称断言：产物装配点的全部写键必须已被重定向进沙箱
        analysis = json.loads(runner.analyze_mod.build_analysis())
        artifact_keys = set(runner.gen.build_artifacts(analysis))
        expected = {
            sandbox / n
            for n in (
                "_conf_schema.json",
                "gen_config.py",
                "metadata.yaml",
                "README.md",
                "texts.py",
                "render_params.py",
            )
        } | {templates / p.name for p in (REPO_ROOT / "templates").iterdir() if p.is_file()}
        assert artifact_keys == expected, (
            f"build_artifacts 写目标面与沙箱重定向不一致（新增工件须同步扩展本 fixture）："
            f"{sorted(str(p) for p in artifact_keys ^ expected)}"
        )
        yield sandbox
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)
        shutil.rmtree(sandbox, ignore_errors=True)


@pytest.fixture(scope="module")
def params_mod() -> ModuleType:
    return _load(
        "extract_render_params_runner",
        REPO_ROOT / "scripts" / "extract_render_params.py",
    )


# ---- 提取模块的载荷构造（revision/digest 注记与 CLI 行为一致）----


def test_render_params_build_payload(params_mod: ModuleType) -> None:
    """build_payload = extract + source_revision + source_digest 注记。"""
    # 夹具须覆盖全部渲染参数锚点各恰一次（extract 对零/多命中都响亮失败）
    source = (
        "\n".join(
            [
                'RENDER_TEMPLATE_VERSION = "2"',
                'get_new_page(2, **{"viewport": {"width": 620, "height": 100}})',
                "if img.st_size >= 5 * 1024 * 1024:",
                "    qrcode.QRCode(version=1, error_correction=1, box_size=10, border=1)",
                "MAX_FORWARD_TEXT_LEN = 30000",
                "MAX_FORWARD_NODES = 90",
                'TEXT_SPLIT_PUNCTUATION = frozenset("。！？")',
            ]
        )
        + "\n"
    )
    payload = params_mod.build_payload(source, "deadbeef" * 5)
    assert payload["_provenance"].startswith("scripts/extract_render_params.py")
    assert payload["source_revision"] == "deadbeef" * 5
    import hashlib

    assert payload["source_digest"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert payload["params"]["render_template_version"]["value"] == "2"
    assert payload["params"]["max_forward_text_len"]["value"] == 30000
    assert payload["params"]["max_forward_nodes"]["value"] == 90
    assert payload["params"]["text_split_punctuation"]["value"] == "。！？"


def test_display_texts_build_payload_shape() -> None:
    """display_texts.build_payload 产出入库 JSON 的完整形态（texts+revision+digest）。"""
    display = _load(
        "extract_display_texts_runner",
        REPO_ROOT / "scripts" / "extract_display_texts.py",
    )
    # 夹具须覆盖全部显示文本锚点各恰一次（extract 对零/多命中都响亮失败）
    render_src = (
        "\n".join(
            [
                'a = "图片渲染失败"',
                'b = "点此在线播放"',
                'c = "转发原帖"',
                'd = "媒体太大啦，已转为文件"',
                'e = "媒体加载失败，请稍后再试"',
                'f = "媒体下载失败"',
                'f2 = "5 项媒体下载失败"',
                'g = "[表情]"',
                'h = "【投票】标题"',
                'i = "投票"',
                'j = "选项甲 12 票 40%"',
                'k = "已结束"',
                'l = "进行中"',
                'm = "多选"',
                'n = "520 人参与"',
                'o = " · "',
                'p = "10 秒内发送以下命令"',
                'q = "视频文件大小为 0"',
                'r = "弹幕"',
                's = "硬币"',
            ]
        )
        + "\n"
    )
    # bilibili 夹具复刻上游 stats.extra 形态（标签=元组首元素，番剧/视频两
    # 路径同值重复，equals_repeated 恰好容忍）
    bilibili_src = (
        "\n".join(
            [
                'x = {"danmaku": ("弹幕", "114")}',
                'y = {"coin": ("硬币", "5")}',
                'z = {"danmaku": ("弹幕", "514"), "coin": ("硬币", "1919")}',
            ]
        )
        + "\n"
    )
    # context 夹具覆盖 Theme API v1 数据层锚点（unknown_size/cover_alt）
    context_src = 't = "未知大小"\nw = "专辑封面"\n'
    payload = display.build_payload(
        {
            "render": render_src,
            "matchers": "",
            "macros": "",
            "exception": "",
            "helper": "",
            "bilibili": bilibili_src,
            "context": context_src,
        },
        "deadbeef" * 5,
    )
    assert payload["texts"]["render_failed"]["value"] == "图片渲染失败"
    assert payload["texts"]["extra_label_danmaku"]["value"] == "弹幕"
    assert payload["texts"]["unknown_size"]["value"] == "未知大小"
    assert payload["source_revision"] == "deadbeef" * 5
    import hashlib

    joined = render_src + "" + "" + "" + "" + bilibili_src + context_src
    assert payload["source_digest"] == hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---- 编排层：离线（用入库提取产物）与全量（需上游克隆）两模式 ----


def test_runner_offline_check_fresh(runner: ModuleType) -> None:
    """离线模式 --check：入库提取产物 → 分析 → 生成，仓库工件新鲜时退出码 0。"""
    assert runner.main(["--offline", "--check"]) == 0


def test_runner_offline_writes_idempotent(runner: ModuleType, sandbox_repo: Path) -> None:
    """离线模式写入：连续两次运行工件零漂移（幂等），第二次 --check 仍 0。

    写目标经 sandbox_repo 重定向到 tmp 沙箱——本测试不再触碰真实工作树
    （此前 runner.main(["--offline"]) 直接重写仓库内六个生成文件）。
    """
    assert runner.main(["--offline"]) == 0
    assert runner.main(["--offline", "--check"]) == 0
    # 沙箱工件与真实仓库已提交内容一致（幂等的可观察形态）
    for name in ("bridge/gen_config.py", "bridge/texts.py", "bridge/render_params.py"):
        assert (sandbox_repo / Path(name).name).read_text(encoding="utf-8") == (
            REPO_ROOT / name
        ).read_text(encoding="utf-8")


def test_runner_offline_check_detects_drift(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """生成物被手改（漂移）时离线 --check 退出码 1 并点名漂移文件。"""
    stale = tmp_path / "render_params.py"
    stale.write_text("RENDER_TEMPLATE_VERSION = 'stale'\n", encoding="utf-8")
    monkeypatch.setattr(runner.gen, "RENDER_PARAMS_PATH", stale)
    code = runner.main(["--offline", "--check"])
    assert code == 1


def test_runner_offline_check_detects_template_drift(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模板文件缺失（磁盘漂移）时离线 --check 退出码 1。"""
    empty = tmp_path / "templates"
    empty.mkdir()
    monkeypatch.setattr(runner.gen, "RENDER_TEMPLATES_DIR", empty)
    assert runner.main(["--offline", "--check"]) == 1


def test_runner_offline_check_detects_template_extras(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """templates/ 出现快照之外的陈旧文件时离线 --check 退出码 1。"""
    ghost_dir = tmp_path / "ghost-templates"
    ghost_dir.mkdir()
    (ghost_dir / "ghost.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(runner, "RENDER_TEMPLATES_DIR", ghost_dir)
    assert runner.main(["--offline", "--check"]) == 1


def test_runner_offline_prunes_stale_templates(
    runner: ModuleType,
    sandbox_repo: Path,
) -> None:
    """写入模式清理快照之外的陈旧模板文件；白名单（BRIDGE_TEMPLATE_EXTRAS）保留。

    沙箱的 templates/ 即 prune 目标（sandbox_repo fixture 已重定向
    runner.gen.RENDER_TEMPLATES_DIR），此处只注入陈旧文件并断言清理结果。
    """
    ghost = sandbox_repo / "templates" / "ghost.css"
    ghost.write_text("body{}", encoding="utf-8")
    assert runner.main(["--offline"]) == 0
    assert not ghost.exists()
    assert (sandbox_repo / "templates" / "default.html.jinja").is_file()


def test_runner_full_when_clone_present(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全量模式：本地有上游 main 引用时现场提取 + 分析 + 生成 + 校验（0）。

    这同时是入库提取产物的现场复提反漂移检查：上游锚点漂移时此测试红。
    提取源锚定入库快照的 source_revision（本地 origin/main ref 可能因
    镜像缓存竞态滞后，PLITE_SYNC_REF 由编排层显式指定真值）。
    """
    probe = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT / ".sync-work" / "upstream"),
            "rev-parse",
            "--verify",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("无上游 main 引用（CI 无克隆），跳过全量模式检查")
    revision = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "render_params.json").read_text(encoding="utf-8"),
    )["source_revision"]
    monkeypatch.setenv("PLITE_SYNC_REF", revision)
    assert runner.main(["--check"]) == 0


def test_runner_full_requires_clone(runner: ModuleType, tmp_path: Path) -> None:
    """全量模式遇到未 fetch 的克隆时响亮失败（不静默退化为离线）。"""
    empty = tmp_path / "not-a-clone"
    empty.mkdir()
    with pytest.raises(SystemExit, match="无法读取上游"):
        runner.main(["--repo", str(empty), "--check"])


def test_runner_written_payloads_match_committed(runner: ModuleType) -> None:
    """编排层写入的提取产物与入库 JSON 完全同构（同 provenance/revision/digest 键集）。"""
    committed = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "render_params.json").read_text(encoding="utf-8")
    )
    assert set(committed) == {"_provenance", "params", "source_revision", "source_digest"}
    templates = json.loads(
        (REPO_ROOT / "vendor" / "_upstream" / "render_templates.json").read_text(encoding="utf-8")
    )
    assert set(templates) == {"_provenance", "files", "source_revision", "source_digest"}
    assert runner.RENDER_PARAMS_OUT == REPO_ROOT / "vendor" / "_upstream" / "render_params.json"
    assert runner.DISPLAY_TEXTS_OUT == REPO_ROOT / "vendor" / "_upstream" / "display_texts.json"
    assert (
        runner.RENDER_TEMPLATES_OUT == REPO_ROOT / "vendor" / "_upstream" / "render_templates.json"
    )
