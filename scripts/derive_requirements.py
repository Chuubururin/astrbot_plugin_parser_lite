"""派生 requirements.txt 与 host-provided.txt。

单一事实源 = vendor/_upstream/pyproject.toml [project.dependencies]（上游 standalone 产物）。
本脚本把它拆成两个清单：
- requirements.txt: 宿主（AstrBot）不提供、需随插件安装的依赖，版本界取上游原文；
  另显式追加 h2（httpx[http2] extra 在宿主环境未验证，显式钉住以保证 HTTP/2）
  与 BRIDGE_REQUIREMENTS（桥接层自身依赖，如 jinja2）——两者与上游 deps 取**差集**，
  上游若自行声明同名包则不重复列出（否则清单不再是「上游 + 桥增量」的干净并集）。
- host-provided.txt: 宿主已提供的依赖（声明性清单，供审计与契约测试比对）。
幂等：重复运行零 diff。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_PYPROJECT = REPO_ROOT / "vendor" / "_upstream" / "pyproject.toml"

# AstrBot v4 宿主已提供的依赖（对照 .research/astrbot/requirements.txt）。
# 判定标准：AstrBot 直依赖，或其直依赖的核心传递依赖（无 marker、无 extra）。
# beautifulsoup4 依据：AstrBot 直依赖 markitdown-no-magika>=0.1.2 的核心依赖
# （PyPI METADATA 实证 Requires-Dist: beautifulsoup4）。失效模式：宿主移除
# markitdown 时需把 bs4 从本集合回收为自装。
# rich 依据：AstrBot 直依赖 dashscope>=1.23.2 的核心依赖（METADATA 实证
# rich>=13.0.0，无 marker/extra）。注意 mcp 的 rich 是 extra 门控，不采信。
HOST_PROVIDED = frozenset(
    {
        "httpx",
        "aiofiles",
        "anyio",
        "beautifulsoup4",
        "cryptography",
        "qrcode",
        "pydantic",
        "rich",
        "typing-extensions",
        "yarl",
    },
)

# httpx 的 [http2] extra 在宿主安装形态未验证；显式钉住 h2 保证 HTTP/2 可用
EXTRA_GUARANTEE = ("h2>=4.0.0,<5.0.0",)

# 桥接层自身依赖（非 vendor 依赖，不参与上游对账）：jinja2 用于渲染模板与
# 代码生成；宿主 quart 核心传递已提供（METADATA 实证），显式声明零成本且
# 让 CI/测试环境与运行环境一致
BRIDGE_REQUIREMENTS = ("jinja2>=3.1.0",)

HEADER = "# 由 scripts/derive_requirements.py 从 vendor/_upstream/pyproject.toml 派生，勿手改\n"


def parse_requires(dist: str) -> str:
    """取依赖的 name 段（去 extras/版本约束/marker），保留原 dist 供输出。"""
    head = dist.split(";")[0]
    return re.split(r"[\[<>=!~\s]", head, maxsplit=1)[0].strip()


def main() -> None:
    data = tomllib.loads(UPSTREAM_PYPROJECT.read_text(encoding="utf-8"))
    deps: list[str] = data["project"]["dependencies"]

    added: list[str] = []
    host: list[str] = []
    for dep in deps:
        (host if parse_requires(dep) in HOST_PROVIDED else added).append(dep)

    unknown_host = HOST_PROVIDED - {parse_requires(d) for d in deps}
    if unknown_host:
        raise SystemExit(f"host-provided 清单与上游依赖不匹配，请复核：{sorted(unknown_host)}")

    # 差集：上游若自行声明 h2/jinja2，本清单不再重复列出
    upstream_names = {parse_requires(dep) for dep in deps}
    extra = [dep for dep in EXTRA_GUARANTEE if parse_requires(dep) not in upstream_names]
    bridge = [dep for dep in BRIDGE_REQUIREMENTS if parse_requires(dep) not in upstream_names]
    for dep in (*EXTRA_GUARANTEE, *BRIDGE_REQUIREMENTS):
        name = parse_requires(dep)
        if name in upstream_names:
            print(f"{name} 已由上游声明，跳过桥侧重复追加（L6）")

    req_lines = [HEADER, "# 新增安装（宿主未提供）", *sorted(added)]
    if extra:
        req_lines += ["", "# HTTP/2 保证", *extra]
    if bridge:
        req_lines += ["", "# 桥接层依赖", *bridge]
    host_lines = [HEADER, "# AstrBot 宿主已提供（声明性清单，不重复安装）", *sorted(host)]

    (REPO_ROOT / "requirements.txt").write_text("\n".join(req_lines) + "\n", encoding="utf-8")
    (REPO_ROOT / "requirements").mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "requirements" / "host-provided.txt").write_text(
        "\n".join(host_lines) + "\n", encoding="utf-8"
    )
    print(f"requirements.txt: {len(added) + len(extra) + len(bridge)} 项")
    print(f"host-provided.txt: {len(host)} 项")


if __name__ == "__main__":
    main()
