"""上游 main 渲染对照驱动（黄金基准活体探针，运维锚点）。

在真实 nonebot2 环境（独立容器/venv，不接 QQ）驱动上游插件完整走一遍
解析 → 渲染出图，并输出安装包 render 模源的 sha256 与渲染参数——与仓库
vendor/_upstream/render_params.json 的 source_digest/params 逐项对照，即
「注入数据面 = 真实上游」的活体证明；出图产物可与桥生产渲染缓存视觉对照。

Runbook（zhenxun 容器先例，独立 venv 不碰框架依赖）：

1. 容器内建独立 venv 并装上游依赖链（浏览器版本须与容器 chromium 精确匹配，
   先 dry-run 核对期望路径零下载）::

     uv venv /tmp/plenv
     uv pip install --python /tmp/plenv/bin/python -r <上游 requirements.txt> \\
         fastapi uvicorn "playwright==<容器 chromium 匹配版本>"

2. 上游 main 源码直拷进 site-packages（免网络）::

     git -C .sync-work/upstream archive origin/main | tar -x -C /tmp/pl-src
     cp -r /tmp/pl-src/src/nonebot_plugin_parser_lite <venv site-packages>/

3. 运行本脚本（上游包可导入即可，无需装本仓库）::

     PLAYWRIGHT_BROWSERS_PATH=<容器浏览器目录> <venv python> upstream_render_probe.py \\
         "https://www.bilibili.com/video/BV1enYL6SEtU" --out /tmp/pl-out \\
         --verify-digest <仓库 render_params.json 的 source_digest>

4. 对照（--verify-digest 在进程内完成第 1 项，不符即退出码 1）：
   report.json 的 installed_render_sha256 == 仓库 render_params.json 的
   source_digest（逐字节同源 ⟹ 渲染参数全等）；upstream.html 与桥
   build_html 产物做结构对照；upstream.png 与桥生产渲染缓存
   （cache/render/<uuid5>.jpeg）同模板结构对照。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import nonebot


def _persist_png(out_dir: Path, png: bytes) -> None:
    """PNG 落盘（一次性 CLI，事件循环独占进程，同步 Path I/O 无并发风险）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "upstream.png").write_bytes(png)


def _persist_html(out_dir: Path, html: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "upstream.html").write_text(html, encoding="utf-8")


def _installed_render_sha256(render_mod: object) -> str:
    """安装包 render 模块源码 sha256（对照仓库 render_params.json 的 source_digest）。"""
    src = Path(render_mod.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


_MUSIC_PLATFORMS = {"kugou", "netease", "kuwo", "qsmusic"}


def _upstream_template_name(result: Any, templates_dir: Path) -> str:
    """复刻上游 render_image 的模板选择规则（供 HTML 截获用）。"""
    if not result.platform:
        return "default.html.jinja"
    platform_name = str(result.platform.name).lower()
    if platform_name in _MUSIC_PLATFORMS:
        return "music.html.jinja"
    candidate = f"{platform_name}.html.jinja"
    return candidate if (templates_dir / candidate).is_file() else "default.html.jinja"


async def _capture_upstream_html(render_mod: Any, result: Any, theme: str) -> tuple[str, str]:
    """按上游 IS_DEBUG 分支的权威配方截获卡面 HTML（结构对照的黄金基准）。"""
    from jinja2 import Environment, FileSystemLoader

    renderer = render_mod.RENDERER
    template_data = await renderer.resolve_parse_result(result)
    templates_dir = Path(str(renderer.templates_dir))
    template_name = _upstream_template_name(result, templates_dir)
    env = Environment(loader=FileSystemLoader(templates_dir), enable_async=True)
    env.filters["safe_src"] = render_mod.safe_src
    html = await env.get_template(template_name).render_async(result=template_data, theme=theme)
    return template_name, html


async def _probe(url: str, out_dir: Path, theme: str) -> dict[str, object]:
    nonebot.init(driver="~fastapi")
    nonebot.load_plugin("nonebot_plugin_parser_lite")
    import nonebot_plugin_parser_lite.render as render_mod
    from nonebot_plugin_parser_lite.constants import MatchWithParams
    from nonebot_plugin_parser_lite.parsers import load_enabled_parsers

    result = None
    for cls in load_enabled_parsers():
        for keyword, pattern, _rules in cls._key_patterns:
            m = pattern.search(url)
            if not m:
                continue
            result = await cls().parse(keyword, MatchWithParams(m))
            break
        if result is not None:
            break
    if result is None:
        raise SystemExit(f"无解析器匹配 {url}")

    template_name, html = await _capture_upstream_html(render_mod, result, theme)
    png = await render_mod.RENDERER.render_image(result, theme=theme)
    _persist_png(out_dir, png)
    _persist_html(out_dir, html)

    return {
        "url": url,
        "platform": str(result.platform),
        "title": result.title,
        "png_bytes": len(png),
        "html_bytes": len(html.encode("utf-8")),
        "template_name": template_name,
        "theme": theme,
        "render_template_version": render_mod.RENDER_TEMPLATE_VERSION,
        "installed_render_sha256": _installed_render_sha256(render_mod),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="上游 main 渲染对照驱动（黄金基准活体探针）")
    parser.add_argument("url", nargs="?", default="https://www.bilibili.com/video/BV1enYL6SEtU")
    parser.add_argument(
        "--out", default="/tmp/pl-out", help="产物目录（upstream.png/html + report.json）"
    )
    parser.add_argument("--theme", default="light", choices=("light", "dark"))
    parser.add_argument(
        "--verify-digest",
        default=None,
        help="期望的安装包 render 模块 sha256（仓库 render_params.json 的 source_digest）；"
        "不符时退出码 1",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(_probe(args.url, Path(args.out), args.theme))
    (Path(args.out) / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.verify_digest is not None:
        installed = str(report["installed_render_sha256"])
        if installed != args.verify_digest:
            print(f"对照失败：安装包 render sha256 {installed} != 期望 {args.verify_digest}")
            return 1
        print(f"对照通过：安装包 render sha256 == 期望值（{installed[:12]}…）")
    print("REPORT " + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
