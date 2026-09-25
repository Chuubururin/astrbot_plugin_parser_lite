"""上游 release 通道 advisory：把 roll 跨越区的 release notes 从「请自行查阅」变成机器呈现。

规律依据（CONTEXT.md「同步」节的上游迭代规律）：

- 1.3.2+ 的 release notes 节标题机器可读：``## 💥 破坏性变更`` /
  ``## 🚀 新功能`` / ``## 🐛 Bug 修复`` / ``## 📦 依赖更新`` / ``## 💫 杂项``；
- vendor 轨（standalone 预发布）持续领先 release 轨：跨越某个 stable 版本
  号的那一次 roll，恰是该版 release notes（含 💥）应到达人工视野的时刻；
- release 节奏中位约 7.5 天，多数 roll 只滚预发布（零跨越），advisory
  只在新版本周期边界出现——人工成本天然摊薄在周级节奏上。

语义：给定 old/new vendor 版本串，筛出区间 (old, new] 跨越的 stable
release（semver 序：1.3.6 < 1.3.7-pre-release.N < 1.3.7——预发布内容
不会误记到 1.3.7 的 notes 上，其 notes 等跨越 stable 时才出）。

出站姿态与 ssrf.py 四层钉扎同策略（本仓安全红线：动态 URL 不得裸进
urlopen）：目标钉死 API_HOST 常量、仅 https、3xx 一律拒绝、IP 字面量
与私网/环回/链路本地/保留/组播段在解析层拒绝，连接层经自定义
HTTPSConnection 只准连预解析白名单 IP 并复核已连接对端——封死验证与
使用间隙的 DNS 重绑定窗口。advisory 是辅助通道不是门槛：网络失败不
阻断 roll（git 侧另有镜像缓存兜底），但失败本身必须响亮——CLI 以退出
码 2 报出，由调用方接入输出面。注入面的真正闸门仍是 roll 后全量契约
测试。
"""

from __future__ import annotations

import argparse
import functools
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_HOST = "api.github.com"
RELEASES_PATH = "/repos/sokoko-org/nonebot-plugin-parser-lite/releases"
RELEASES_URL = f"https://{API_HOST}{RELEASES_PATH}"
# 注入面相关性：💥（桥接契约）、🚀（行为面）、📦（requirements 派生面）进
# advisory；🐛/💫 不进——修复项由 vendor 快照 + 契约测试把关，无需人读。
ADVISORY_HEADERS = ("💥", "🚀", "📦")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _assert_public_ip(addr: str) -> None:
    ip = ipaddress.ip_address(addr)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(f"advisory 出站目标解析到非公网地址：{addr}")


def _validate_releases_url(url: str) -> str:
    """请求前 URL 钉扎：仅 https、host 精确匹配常量、URL 不得内嵌 IP 字面量。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"advisory 仅允许 https：{url!r}")
    if parsed.hostname != API_HOST:
        raise ValueError(f"advisory 目标 host 钉死 {API_HOST}：{url!r}")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass  # 正常：hostname 是域名，非 IP 字面量
    else:
        raise ValueError(f"advisory 目标必须是域名而非 IP 字面量：{url!r}")
    return url


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SSRF 重定向防线：3xx 一律拒绝，不跟随任何 Location。"""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _pinned_connect(
    host: str, port: int, timeout: float | None, allowed_ips: set[str]
) -> socket.socket:
    """只准连接预解析白名单内的 IP；已连接对端再复核（防 DNS rebinding）。"""
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    last_err: OSError | None = None
    for info in infos:
        addr = str(info[4][0])
        _assert_public_ip(addr)
        if addr not in allowed_ips:
            raise ValueError(f"advisory 连接目标漂移出解析白名单：{addr}")
        sock = socket.socket(info[0], info[1], info[2])
        sock.settimeout(timeout or 15.0)
        try:
            sock.connect((addr, port))
        except OSError as err:
            sock.close()
            last_err = err
            continue
        peer = sock.getpeername()[0]
        _assert_public_ip(peer)
        if peer not in allowed_ips:
            sock.close()
            raise ValueError(f"advisory 实际对端漂移出解析白名单：{peer}")
        return sock
    if last_err:
        raise last_err
    raise OSError("advisory 无可用解析结果")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        allowed_ips: set[str],
        context: ssl.SSLContext | None = None,
        **kw: Any,
    ):
        super().__init__(host, port, context=context, **kw)
        self._allowed_ips = allowed_ips
        # 自持 TLS context：不依赖 HTTP(S)Connection 私有属性名（跨版本不稳）
        self._tls_context = context or ssl.create_default_context()

    def connect(self) -> None:
        sock = _pinned_connect(str(self.host), int(self.port), self.timeout, self._allowed_ips)
        try:
            self.sock = self._tls_context.wrap_socket(sock, server_hostname=str(self.host))
        except Exception:
            sock.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allowed_ips: set[str], context: ssl.SSLContext | None = None):
        self._allowed_ips = allowed_ips
        self._tls_context = context
        super().__init__(context=context)

    def https_open(
        self, req: urllib.request.Request
    ) -> Any:  # do_open 返回 http.client.HTTPResponse，类型面宽化为 Any
        return self.do_open(
            functools.partial(_PinnedHTTPSConnection, allowed_ips=self._allowed_ips),
            req,
            context=self._tls_context,
        )


def fetch_releases(url: str = RELEASES_URL, timeout: int = 15) -> list[dict[str, Any]]:
    _validate_releases_url(url)
    allowed_ips: set[str] = set()
    for info in socket.getaddrinfo(API_HOST, 443, 0, socket.SOCK_STREAM):
        addr = str(info[4][0])
        _assert_public_ip(addr)
        allowed_ips.add(addr)
    if not allowed_ips:
        raise ValueError("advisory 目标无公网解析结果")
    opener = urllib.request.build_opener(
        _NoRedirectHandler(), _PinnedHTTPSHandler(allowed_ips, context=ssl.create_default_context())
    )
    req = urllib.request.Request(url, headers={"User-Agent": "parser-lite-roll-advisory"})
    with opener.open(req, timeout=timeout) as resp:
        return json.load(resp)


def _seg(part: str) -> tuple[int, int | str]:
    return (0, int(part)) if part.isdigit() else (1, part)


def _version_key(version: str) -> tuple:
    """semver 序键：数字段按数值、预发布段整体排在同号 stable 之前。"""
    base, _, pre = version.partition("-")
    nums = [_seg(p) for p in base.split(".")]
    nums += [(0, 0)] * (3 - len(nums))
    stable_rank = 1 if not pre else 0
    pre_key = tuple(_seg(p) for p in re.split(r"[.-]", pre)) if pre else ()
    return (tuple(nums), stable_rank, pre_key)


def crossed_releases(
    releases: list[dict[str, Any]], old_version: str, new_version: str
) -> list[dict[str, Any]]:
    """(old, new] 区间内的已发布 stable release，按版本升序。"""
    lo, hi = _version_key(old_version), _version_key(new_version)
    picked = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        version = str(rel.get("tag_name", "")).lstrip("vV")
        key = _version_key(version)
        if lo < key <= hi:
            picked.append({**rel, "_version": version, "_key": key})
    return sorted(picked, key=lambda r: r["_key"])


def parse_sections(body: str) -> dict[str, str]:
    """按 ``## <emoji> <标题>`` 切 release notes 正文，键为节标题原文。"""
    matches = list(_SECTION_RE.finditer(body or ""))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[match.group(1).strip()] = body[match.end() : end].strip()
    return sections


def render_advisory(crossed: list[dict[str, Any]]) -> str:
    """跨越 release → advisory markdown 块（无跨越返回空串）。"""
    if not crossed:
        return ""
    lines = [f"## 上游 release advisory：本 roll 跨越 {len(crossed)} 个 release", ""]
    for rel in crossed:
        version = rel["_version"]
        date = str(rel.get("published_at", ""))[:10]
        lines.append(f"### v{version}（{date} 发布）")
        sections = parse_sections(rel.get("body") or "")
        for header, text in sections.items():
            if not header.startswith(ADVISORY_HEADERS):
                continue
            if header.startswith("💥"):
                lines.append(f"#### ⚠️ {header}（需人工确认桥接面影响）")
            else:
                lines.append(f"#### {header}")
            lines.append(text)
        if not any(h.startswith(ADVISORY_HEADERS) for h in sections):
            lines.append("（notes 无 💥/🚀/📦 节——上游可能改了节格式，请人工核对）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="roll 跨越区 release notes 机器解析（advisory）")
    parser.add_argument("--old", required=True, help="roll 前 vendor 版本串")
    parser.add_argument("--new", required=True, help="roll 后 vendor 版本串")
    args = parser.parse_args(argv)
    try:
        releases = fetch_releases()
    except Exception as exc:  # 网络失败响亮报出；是否阻断由调用方依退出码决定
        print(f"⚠️ release advisory 抓取失败：{exc}", file=sys.stderr)
        return 2
    advisory = render_advisory(crossed_releases(releases, args.old, args.new))
    if advisory:
        print(advisory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
