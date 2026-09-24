"""上游 main 分支渲染模板提取 → vendor/_upstream/render_templates.json。

卡面模板数据面的最前端：桥内 ``templates/`` 目录（default/music 卡面模板、
macros 宏、CSS 族）逐字节来自上游 main 分支 render/templates（2026-09-13
活体核验：六个文件 sha256 与 origin/main 全等——桥对模板的适配面在
render.py 的 safe_src 过滤器与数据翻译，模板本体零改动）。standalone 快照
剥离了渲染层，因此提取源是 main 分支本体。本脚本只在 sync-upstream 工作流
中运行（那里已 fetch 上游 main）；产物 JSON **入库**（与 display_texts/
render_params 同类的上游元数据快照），使两层注入在本地与 CI 均可离线再生。

提取规则（唯一的模板清单规则，属桥规则层）：

- git ls-tree 动态发现模板文件全集——上游新增/删除/改名模板文件自动跟随，
  无需改本脚本；出现子目录结构或非 jinja/css/json 文件时响亮失败交人工复核
  （json = Theme API v1 的主题清单，与模板同属卡面字节平面）；
- 内容逐字快照（不做任何归一化——模板字节的唯一权威是上游本体），仅做
  非空与尺寸上限防火墙；
- 产物含 source_revision 与 source_digest（按文件名排序拼接的 sha256），
  编排层 --check 用它检测上游模板漂移。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_ROOT / "vendor" / "_upstream" / "render_templates.json"
PROVENANCE = "scripts/extract_render_templates.py 生成（上游 main 渲染模板提取，勿手改）"

# 平级共享底座（git 只读管道 + 产物字节公式）：scripts/ 非包，直接运行与
# importlib 按路径加载两种形态都靠 sys.path 垫片导入
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pipeline_common import dump_json, git_ls_tree, git_show, rev_parse  # noqa: E402

_UPSTREAM_TEMPLATES_DIR = "src/nonebot_plugin_parser_lite/render/templates"

# 注入防火墙：模板文件名渲染进生成路径，内容逐字节写盘——名字限模板族后缀
# 且禁路径分隔（目录穿越/子目录结构都不在上游模板的已知形态内），内容限
# 非空与尺寸上限（疑似异常快照）。与 analyze_vendor 的汇点复检同规则。
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(jinja|css|json)\Z")
_FILE_MAX_BYTES = 512 * 1024


def validate_name(name: str) -> None:
    """模板文件名防火墙：仅平铺的 jinja/css/json 名，拒绝路径分隔与目录穿越。"""
    if "/" in name or ".." in name or not _NAME_RE.fullmatch(name):
        raise SystemExit(
            f"上游模板文件名越界（仅限平铺 jinja/css 名，拒绝路径分隔与目录穿越）：{name!r}",
        )


def validate_content(name: str, content: str) -> None:
    """模板内容防火墙：非空且不超尺寸上限（逐字快照，不做内容归一化）。"""
    if not content.strip():
        raise SystemExit(f"上游模板 {name} 为空，疑似异常快照，拒绝写入提取产物")
    if len(content.encode("utf-8")) > _FILE_MAX_BYTES:
        raise SystemExit(
            f"上游模板 {name} 超过 {_FILE_MAX_BYTES} 字节上限，疑似异常快照，拒绝写入提取产物",
        )


def read_sources(repo: Path, ref: str) -> dict[str, str]:
    """读上游模板文件全集（公开给编排层；键 = 平铺文件名）。"""
    paths = git_ls_tree(repo, ref, _UPSTREAM_TEMPLATES_DIR)
    if not paths:
        raise SystemExit(
            f"上游 {ref}:{_UPSTREAM_TEMPLATES_DIR} 下没有模板文件"
            "（上游可能已移动模板目录），请复核 scripts/extract_render_templates.py",
        )
    files: dict[str, str] = {}
    for path in paths:
        rel = path.removeprefix(_UPSTREAM_TEMPLATES_DIR + "/")
        if "/" in rel:
            raise SystemExit(
                f"上游模板出现子目录结构 {path!r}（已知形态为平铺目录），"
                "请复核 scripts/extract_render_templates.py",
            )
        validate_name(rel)
        content = git_show(repo, ref, path)
        validate_content(rel, content)
        files[rel] = content
    return files


def build_payload(files: dict[str, str], revision: str) -> dict[str, Any]:
    """提取产物 + 快照注记（入库 JSON 完整形态；CLI 与编排层共用同一公式）。"""
    digest = hashlib.sha256(
        "".join(files[name] for name in sorted(files)).encode("utf-8"),
    ).hexdigest()
    return {
        "_provenance": PROVENANCE,
        "files": dict(sorted(files.items())),
        "source_revision": revision,
        "source_digest": digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取上游 main 分支渲染模板（模板数据面最前端）")
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT / ".sync-work" / "upstream"),
        help="上游克隆目录（需已 fetch 目标 ref）",
    )
    parser.add_argument("--ref", default="origin/main", help="渲染模板提取源分支引用")
    args = parser.parse_args(argv)

    files = read_sources(Path(args.repo), args.ref)
    revision = rev_parse(Path(args.repo), args.ref)
    payload = build_payload(files, revision)
    dump_json(OUTPUT_PATH, payload)
    print(f"已提取 {len(payload['files'])} 个上游渲染模板 → {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
