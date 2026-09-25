"""上游 main 分支 README 提取 → vendor/_upstream/upstream_readme.json。

上游文档数据面的最前端：生成 README 的正文直通源。项目正典文档在 main 分支
根 README.md（平台支持矩阵、配置说明、能力表），standalone 分支的 README 是
面向「复制目录」场景的改写版，不作为桥 README 的来源——故本面与渲染模板/
显示文本/渲染参数同走 origin/main 提取轨。产物 JSON **入库**（上游元数据快照），
使两层注入在本地与 CI 均可离线再生。

`vendor/_upstream/README.md`（standalone 快照自带、verify_vendor 逐字节审计的
那个文件）与本提取件是两个独立角色：前者审计 vendor 完整性，后者供文档注入，
互不兼任。

提取规则（唯一的 README 读取规则，属桥规则层）：

- 逐字快照上游 ``README.md``（不做任何归一化——README 正文的权威是上游本体），
  仅做非空与尺寸上限防火墙（疑似异常快照响亮失败交人工复核）；
- 产物含 source_revision 与 source_digest（正文 sha256），编排层 --check 用它
  检测上游文档漂移。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_ROOT / "vendor" / "_upstream" / "upstream_readme.json"
PROVENANCE = "scripts/extract_upstream_readme.py 生成（上游 main README 提取，勿手改）"

# 平级共享底座（git 只读管道 + 产物字节公式）：scripts/ 非包，直接运行与
# importlib 按路径加载两种形态都靠 sys.path 垫片导入
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from pipeline_common import dump_json, git_show, rev_parse  # noqa: E402

_UPSTREAM_README = "README.md"

# 注入防火墙：README 逐字节直通进生成文档——非空且限尺寸上限（疑似异常快照）。
_README_MAX_BYTES = 262_144


def read_source(repo: Path, ref: str) -> str:
    """读上游 ``ref`` 根 README.md 逐字原文（公开给编排层）。"""
    content = git_show(repo, ref, _UPSTREAM_README)
    if not content.strip():
        raise SystemExit(f"上游 {ref}:{_UPSTREAM_README} 为空，疑似异常快照，拒绝写入提取产物")
    if len(content.encode("utf-8")) > _README_MAX_BYTES:
        raise SystemExit(
            f"上游 {ref}:{_UPSTREAM_README} 超过 {_README_MAX_BYTES} 字节上限，"
            "疑似异常快照，拒绝写入提取产物",
        )
    return content


def build_payload(content: str, revision: str) -> dict[str, Any]:
    """提取产物 + 快照注记（入库 JSON 完整形态；CLI 与编排层共用同一公式）。"""
    return {
        "_provenance": PROVENANCE,
        "readme": content,
        "source_revision": revision,
        "source_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取上游 main 分支 README（文档数据面最前端）")
    parser.add_argument(
        "--repo",
        default=str(REPO_ROOT / ".sync-work" / "upstream"),
        help="上游克隆目录（需已 fetch 目标 ref）",
    )
    parser.add_argument("--ref", default="origin/main", help="README 提取源分支引用")
    args = parser.parse_args(argv)

    content = read_source(Path(args.repo), args.ref)
    revision = rev_parse(Path(args.repo), args.ref)
    dump_json(OUTPUT_PATH, build_payload(content, revision))
    print(f"已提取上游 README（{len(content.encode('utf-8'))} 字节） → {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
