"""上游 roll 的供应链异常判据（sync 管道的「异常即红」闸门，纯本地无网络）。

对上游 standalone 的两个构建树（旧 sync-state sha → 新 fetch sha）做三类
机械判定，命中任一即要求 sync 管道停下开 Issue——全自动同步链里「门禁全绿
≠ 无恶意」，本脚本是唯一的投毒/劫持机械防线。owner/repo 变更判定归工作流
（比对 sync-state 的上游坐标），不在本脚本。

判据（阈值导出为常量，CLI 可覆盖）：

1. dep-manifest：requirements.txt 或 pyproject.toml 依赖清单任何变化
   （含文件出现/消失、TOML 解析失败——一律 fail-closed）；
2. structural：新增+删除文件数 > MAX_FILE_CHANGES，或行级 diff
   （增+删合计）> MAX_DIFF_LINES（二进制条目按 1 行计）；
3. license：LICENSE 内容变化或出现/消失。

standalone 是孤儿单提交分支，两次构建无父子关系，但 tree-to-tree diff
对任意两提交成立，语义不受影响。

CLI（工作流侧只经此处调用，不把判定语义抄进 YAML）：

    python scripts/upstream_sync.py classify --repo .sync-work/upstream \
        --old <旧sha> --new <新sha> [--json report.json]
        # 可选覆盖：--max-file-changes N --max-diff-lines N

退出码：0=无异常（报告可作 PR body 摘要）；1=判据命中；2=目标/用法失败。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

MAX_FILE_CHANGES = 50
MAX_DIFF_LINES = 5000

_REPORT_VERSION = 1
_EVIDENCE_ITEM_LIMIT = 10


def _git(repo: Path, args: list[str]) -> tuple[int, bytes]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    return result.returncode, result.stdout


def _blob_id(repo: Path, ref: str, path: str) -> str | None:
    """ref 树内 path 的 blob sha；文件/树不存在返回 None（缺失语义由判据层决定）。"""
    code, out = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}:{path}"])
    if code != 0:
        return None
    blob = out.decode("ascii", "replace").strip()
    return blob if re.fullmatch(r"[0-9a-f]{40}", blob) else None


def _show_text(repo: Path, ref: str, path: str) -> str | None:
    code, out = _git(repo, ["show", f"{ref}:{path}"])
    if code != 0:
        return None
    return out.decode("utf-8", "replace")


def _norm_requirement(line: str) -> str:
    """单条 PEP 508/requirements 行的归一化：名小写、去全部空白、去行内注释。"""
    text = re.sub(r"(^|\s)#.*$", "", line.strip())
    if not text or text.startswith("-"):
        return ""
    return re.sub(r"\s+", "", text).lower()


def _requirements_items(text: str) -> set[str]:
    return {n for line in text.splitlines() if (n := _norm_requirement(line))}


def _pyproject_items(text: str) -> set[str]:
    """pyproject 的 project.dependencies + optional-dependencies 全集。

    解析失败不在此响亮退出——调用方（依赖判据）把它归入 fail-closed 命中。
    """
    data: dict[str, Any] = tomllib.loads(text)
    project = data.get("project", {})
    items = {n for raw in project.get("dependencies", []) if (n := _norm_requirement(raw))}
    for extra in project.get("optional-dependencies", {}).values():
        items |= {n for raw in extra if (n := _norm_requirement(raw))}
    return items


def _diff_sets(old: set[str], new: set[str]) -> list[str]:
    added = sorted(new - old)
    removed = sorted(old - new)
    evidence = [f"+{item}" for item in added[:_EVIDENCE_ITEM_LIMIT]]
    evidence += [f"-{item}" for item in removed[:_EVIDENCE_ITEM_LIMIT]]
    overflow = len(added) + len(removed) - len(evidence)
    if overflow > 0:
        evidence.append(f"…另有 {overflow} 项")
    return evidence


def classify_manifest(repo: Path, old: str, new: str) -> list[dict[str, Any]]:
    """判据 1：依赖清单变化（缺失/解析失败一律命中，fail-closed）。"""
    hits: list[dict[str, Any]] = []
    for path, parse in (
        ("requirements.txt", _requirements_items),
        ("pyproject.toml", _pyproject_items),
    ):
        old_id, new_id = _blob_id(repo, old, path), _blob_id(repo, new, path)
        if old_id is None and new_id is None:
            continue
        if old_id is None or new_id is None:
            hits.append({"kind": "dep-manifest", "evidence": [f"{path} 在两棵树间出现/消失"]})
            continue
        if old_id == new_id:
            continue
        try:
            old_items = parse(_show_text(repo, old, path) or "")
            new_items = parse(_show_text(repo, new, path) or "")
        except (tomllib.TOMLDecodeError, AttributeError):
            hits.append({"kind": "dep-manifest", "evidence": [f"{path} 内容变化且无法解析比对"]})
            continue
        evidence = _diff_sets(old_items, new_items)
        if evidence:
            hits.append(
                {"kind": "dep-manifest", "evidence": [f"{path}: {item}" for item in evidence]}
            )
    return hits


def classify_license(repo: Path, old: str, new: str) -> list[dict[str, Any]]:
    """判据 3：LICENSE 内容变化或出现/消失。"""
    old_id, new_id = _blob_id(repo, old, "LICENSE"), _blob_id(repo, new, "LICENSE")
    if old_id is None and new_id is None:
        return []
    if old_id is None or new_id is None:
        return [{"kind": "license", "evidence": ["LICENSE 在两棵树间出现/消失"]}]
    if old_id != new_id:
        return [{"kind": "license", "evidence": ["LICENSE 内容变化"]}]
    return []


def classify_structural(
    repo: Path, old: str, new: str, max_files: int, max_lines: int
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """判据 2：结构规模。返回 (统计, 命中)。重命名不折叠，churn 如实计。"""
    code, out = _git(repo, ["diff", "--name-status", "--no-renames", old, new, "--"])
    if code != 0:
        raise SystemExit("git diff --name-status 失败（两 sha 是否都在库内？）")
    statuses = [line.split("\t")[0] for line in out.decode("utf-8", "replace").splitlines() if line]
    files_added = statuses.count("A")
    files_removed = statuses.count("D")

    code, out = _git(repo, ["diff", "--numstat", old, new, "--"])
    if code != 0:
        raise SystemExit("git diff --numstat 失败")
    diff_lines = 0
    for line in out.decode("utf-8", "replace").splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        added, removed = fields[0], fields[1]
        # 二进制条目为 "-"：按 1 行保守计
        diff_lines += (int(added) if added.isdigit() else 1) + (
            int(removed) if removed.isdigit() else 1
        )

    stats = {
        "files_added": files_added,
        "files_removed": files_removed,
        "files_changed": len(statuses),
        "diff_lines": diff_lines,
    }
    hits: list[dict[str, Any]] = []
    if files_added + files_removed > max_files:
        hits.append(
            {
                "kind": "structural",
                "evidence": [
                    (
                        f"增删文件 {files_added + files_removed} > 阈值 {max_files}"
                        f"（增 {files_added} / 删 {files_removed}）"
                    )
                ],
            }
        )
    if diff_lines > max_lines:
        hits.append(
            {
                "kind": "structural",
                "evidence": [f"行级 diff {diff_lines} > 阈值 {max_lines}（增+删合计）"],
            }
        )
    return stats, hits


def classify(
    repo: Path,
    old: str,
    new: str,
    *,
    max_files: int = MAX_FILE_CHANGES,
    max_lines: int = MAX_DIFF_LINES,
) -> dict[str, Any]:
    """三判据全集 → 报告。verdict=blocked 即 sync 侧的「红」。"""
    hits: list[dict[str, Any]] = []
    hits += classify_manifest(repo, old, new)
    hits += classify_license(repo, old, new)
    stats, structural_hits = classify_structural(repo, old, new, max_files, max_lines)
    hits += structural_hits
    return {
        "report_version": _REPORT_VERSION,
        "old_sha": old,
        "new_sha": new,
        "verdict": "blocked" if hits else "ok",
        "stats": stats,
        "hits": hits,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上游 roll 供应链异常判据（异常即红）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("classify", help="比对两棵构建树的三判据")
    p.add_argument("--repo", required=True, type=Path, help="上游克隆目录")
    p.add_argument("--old", required=True, help="旧 standalone sha（sync-state 记录）")
    p.add_argument("--new", required=True, help="新 standalone sha（本次 fetch）")
    p.add_argument("--json", type=Path, default=None, help="报告落盘路径")
    p.add_argument("--max-file-changes", type=int, default=MAX_FILE_CHANGES)
    p.add_argument("--max-diff-lines", type=int, default=MAX_DIFF_LINES)
    args = parser.parse_args(argv)

    if args.command == "classify":
        for sha in (args.old, args.new):
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
                print(f"sha 形态异常：{sha!r}", file=sys.stderr)
                return 2
        report = classify(
            args.repo,
            args.old,
            args.new,
            max_files=args.max_file_changes,
            max_lines=args.max_diff_lines,
        )
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(payload, encoding="utf-8")
        if report["verdict"] == "blocked":
            for hit in report["hits"]:
                for line in hit["evidence"]:
                    print(f"判据命中 [{hit['kind']}] {line}", file=sys.stderr)
            return 1
        stats = report["stats"]
        print(
            f"判据通过：{args.old[:12]} → {args.new[:12]}"
            f"（变更文件 {stats['files_changed']}，行 {stats['diff_lines']}）"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
