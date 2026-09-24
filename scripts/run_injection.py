"""两层注入单命令入口：提取（三数据面）→ 分析 → 生成 → 校验。

sync-upstream 工作流与本地开发的编排层：一次进程完成上游 main 数据面提取
（显示文本 + 渲染参数 + 渲染模板，共享上游文件读取与单次 rev-parse）、
第一层分析、第二层生成。锚点规则、防火墙与产物公式归属各自脚本模块
（单一事实源），本层只编排与传递，不复制任何提取/生成规则。

模式（可组合）：

- 全量（默认）：从上游克隆（--repo，需已 fetch --ref，默认 origin/main）现场
  提取入库数据件 → 分析 → 生成；提取产物与入库件不一致即为上游锚点/快照
  漂移信号（全量 --check 时点名并以退出码 1 失败，不写入）；
- --offline：跳过提取，直接消费入库提取产物（本地无克隆、CI 无 fetch 时）；
- --check：全流程 dry-run——生成工件与工作树比对，有漂移时点名并退出码 1，
  不写任何文件。

离线 --check 的分析源：优先消费管线既有的分析产物（vendor_analysis.json，
第一层全量运行的产物、不入库），故无需 vendor 导入环境（离线
承诺）；产物缺失时回退现场分析（此时才按需导入第一层，需 vendor 可导入）。
分析产物自身的新鲜度由全量路径把关（全量 --check 现场复分析 + 契约测试
test_repo_artifacts_are_fresh），离线 --check 覆盖第二层：生成工件与工作树
逐字节一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPTS.parent

# 平级共享底座（git 只读管道 + 产物字节公式）：scripts/ 非包，importlib 按
# 路径加载形态靠 sys.path 垫片导入
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from pipeline_common import dump_json, rev_parse  # noqa: E402

RENDER_PARAMS_OUT = REPO_ROOT / "vendor" / "_upstream" / "render_params.json"
DISPLAY_TEXTS_OUT = REPO_ROOT / "vendor" / "_upstream" / "display_texts.json"
RENDER_TEMPLATES_OUT = REPO_ROOT / "vendor" / "_upstream" / "render_templates.json"
RENDER_TEMPLATES_DIR = REPO_ROOT / "templates"


def _load(name: str) -> ModuleType:
    """按文件路径加载同目录脚本模块（scripts/ 非包；模块常量保持可 monkeypatch）。"""
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载脚本模块：{name}")
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


display_mod = _load("extract_display_texts")
params_mod = _load("extract_render_params")
templates_mod = _load("extract_render_templates")
gen = _load("generate_config")

# 第一层按需加载（模块级不 import vendor）：--offline 消费入库分析产物时无需
# vendor 导入环境（实证模块级加载会拉起 31 个
# nonebot_plugin_parser_lite.* 模块）
_analyze: ModuleType | None = None


def _analyze_mod() -> ModuleType:
    """按需加载第一层（analyze_vendor 模块级零 vendor 依赖）。"""
    global _analyze
    if _analyze is None:
        _analyze = _load("analyze_vendor")
    return _analyze


def __getattr__(name: str) -> Any:
    """延迟导出 analyze_mod（既有访问点：测试沙箱夹具重定向 ANALYSIS_PATH）。"""
    if name == "analyze_mod":
        return _analyze_mod()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _extract(
    repo: Path,
    ref: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    """上游 main 三数据面提取：共享文件读取与单次 rev-parse，公式在提取模块。"""
    sources = display_mod.read_sources(repo, ref)
    render_source = params_mod.read_source(repo, ref)
    render_context = params_mod.read_context_source(repo, ref)
    template_files = templates_mod.read_sources(repo, ref)
    revision = rev_parse(repo, ref)
    return (
        params_mod.build_payload(render_source, revision, render_context),
        display_mod.build_payload(sources, revision),
        templates_mod.build_payload(template_files, revision),
        sources,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(
            f"入库提取产物缺失：{path.name}，请先运行全量模式（默认，需上游克隆）现场提取",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _committed_drift(path: Path, payload: dict[str, Any]) -> bool:
    """入库件缺失或与现场提取 payload 语义不一致（值级比对，与格式无关）。"""
    if not path.is_file():
        return True
    return json.loads(path.read_text(encoding="utf-8")) != payload


def _analysis_text(*, offline: bool, check: bool) -> str:
    """分析产物文本：离线 --check 消费既有产物（零 vendor 导入），其余现场分析。

    离线产物缺失时回退现场分析（此时才按需导入第一层）——宁可退化为全量语义，
    也不静默跳过新鲜度校验。
    """
    if offline and check:
        path = _analyze_mod().ANALYSIS_PATH
        if path.is_file():
            return path.read_text(encoding="utf-8")
        print(
            f"入库分析产物缺失（{path.name}），回退第一层现场分析（需 vendor 可导入）",
            file=sys.stderr,
        )
    return _analyze_mod().build_analysis()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="两层注入单命令入口：提取 → 分析 → 生成（--check 只校验）",
    )
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT / ".sync-work" / "upstream"),
        help="上游克隆目录（需已 fetch 目标 ref）",
    )
    # 编排层（roll_local）可用 PLITE_SYNC_REF 指定提取源（如远端 main 真值
    # sha，绕开本地 ref 的镜像缓存竞态）；默认 origin/main
    parser.add_argument(
        "--ref",
        default=os.environ.get("PLITE_SYNC_REF", "origin/main"),
        help="上游 main 提取源引用",
    )
    parser.add_argument("--offline", action="store_true", help="跳过提取，直接消费入库提取产物")
    parser.add_argument("--check", action="store_true", help="只比对不写入，有漂移时退出码 1")
    args = parser.parse_args(argv)

    if args.offline:
        params_payload = _read_json(RENDER_PARAMS_OUT)
        display_payload = _read_json(DISPLAY_TEXTS_OUT)
        # 模板快照只经分析层进入生成（generate_config ← analyze_vendor），
        # 此处读取仅验证入库件在位（缺失即响亮退出）
        _read_json(RENDER_TEMPLATES_OUT)
    else:
        params_payload, display_payload, templates_payload, sources = _extract(
            Path(args.repo),
            args.ref,
        )
        if args.check:
            drift = [
                path.name
                for path, payload in (
                    (RENDER_PARAMS_OUT, params_payload),
                    (DISPLAY_TEXTS_OUT, display_payload),
                    (RENDER_TEMPLATES_OUT, templates_payload),
                )
                if _committed_drift(path, payload)
            ]
            if drift:
                print(
                    f"提取产物与入库件漂移（上游锚点/快照已变，请再生入库数据件）：{drift}",
                    file=sys.stderr,
                )
                return 1
        else:
            dump_json(RENDER_PARAMS_OUT, params_payload)
            dump_json(DISPLAY_TEXTS_OUT, display_payload)
            dump_json(RENDER_TEMPLATES_OUT, templates_payload)

    analysis_text = _analysis_text(offline=args.offline, check=args.check)
    analysis = json.loads(analysis_text)
    artifacts = gen.build_artifacts(analysis)
    expected_templates = gen.expected_template_names(analysis)
    template_extras = sorted(
        p.name for p in RENDER_TEMPLATES_DIR.iterdir() if p.name not in expected_templates
    )

    if args.check:
        stale = [
            str(path)
            for path, content in artifacts.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if stale or template_extras:
            print(
                f"生成工件漂移（请重跑 scripts/run_injection.py 再生）：{stale or '无'}；"
                f"templates/ 陈旧文件（上游已删除/改名）：{template_extras}",
                file=sys.stderr,
            )
            return 1
        print("两层注入校验通过：提取产物与生成工件均新鲜")
        return 0

    _analyze_mod().ANALYSIS_PATH.write_text(analysis_text, encoding="utf-8")
    for path, content in artifacts.items():
        path.write_text(content, encoding="utf-8")
    # 陈旧模板清理：上游删除/改名的模板文件从 templates/ 移除（快照之外的
    # 桥自有文件经 gen.BRIDGE_TEMPLATE_EXTRAS 白名单保留）
    for name in template_extras:
        entry = RENDER_TEMPLATES_DIR / name
        if entry.is_file():
            entry.unlink()
        else:
            raise SystemExit(
                f"templates/ 出现非文件的陈旧条目且不在上游快照内：{entry}，请人工复核",
            )
    # 上游新增文案候选（advisory，只在全量提取模式提示）：sync 日志据此
    # 扩充锚点表，无需人工重读上游 diff
    if not args.offline:
        candidates = display_mod.scan_candidates(
            sources, {entry["value"] for entry in display_payload["texts"].values()}
        )
        if candidates:
            print(f"上游文案候选（advisory，供锚点表复核，共 {len(candidates)} 条）：")
            for value in candidates[:20]:
                print(f"  - {value!r}")
            if len(candidates) > 20:
                print(f"  …（其余 {len(candidates) - 20} 条略）")
    print(
        f"两层注入完成（{'离线' if args.offline else '全量'}模式）："
        f"{_analyze_mod().ANALYSIS_PATH.name} + 生成 {len(artifacts)} 工件"
        + ("" if args.offline else " + 3 件入库提取产物"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
