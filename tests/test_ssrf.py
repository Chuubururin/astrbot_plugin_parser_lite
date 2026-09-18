"""ssrf.py 单元测试：四层校验全拒/放行清单 + 钉扎 + 幂等安装（工单 08/09）。

放行样本用公网 IP 字面量（1.1.1.1 / 8.8.8.8）保持用例密闭；仅钉扎用例
按「mock 只在系统边界」原则触真实 DNS（example.com），这两个用例打
``@pytest.mark.network`` 归入隔离区（默认跳过；RUN_NETWORK_TESTS=1 启用），
避免 CI 的 skip 数随环境 DNS 可用性浮动（2026-09-17 评审 L11）。
钉扎契约（规格 step 8）：连接钉在已验证 IP 上，URL/SNI 保留原 hostname。

curl 通道用例（H1/H2 回归）一律使用**真实 curl_cffi.AsyncSession**，不再用
带 **kwargs 的假会话——正是那个 **kwargs 把「curl_options 被当作
AsyncSession.request 关键字实参」的 TypeError 吞成了假绿，掩盖了 H1。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpcore
import httpx
import pytest
from astrbot_plugin_parser_lite.bridge import ssrf
from astrbot_plugin_parser_lite.bridge.ssrf import UrlBlockedError, validate_url
from curl_cffi import AsyncSession
from curl_cffi.const import CurlFollow, CurlOpt

BLOCKED_URLS = (
    "ftp://example.com/file",  # scheme 白名单外
    "javascript:alert(1)",
    "//example.com/no-scheme",
    "http://localhost",  # 解析到环回
    "http://127.0.0.1:8080",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",  # v4 映射环回
    "http://10.0.0.1",
    "http://172.16.0.1",
    "http://192.168.1.1",
    "http://169.254.169.254/latest/meta-data/",  # 云元数据端点
    "http://0.0.0.0",
    "http://100.64.0.1",  # CGNAT（stdlib 谓词遗漏，扩展网段兜住）
    "http://198.18.0.1",  # 基准测试段
    "http://192.0.2.1",  # TEST-NET-1
    "http://203.0.113.7",  # TEST-NET-3
    "http://224.0.0.1",  # 多播
    "http://240.0.0.1",  # 保留
    "http://[fe80::1]/",  # 链路本地
    "http://[64:ff9b::7f00:1]/",  # NAT64 映射
    "http://[2001:db8::1]/",  # IPv6 文档段
    "http://1.1.1.1:22",  # 低端口
    "http://1.1.1.1:abc",  # 端口非法
)

ALLOWED_URLS = (
    "http://1.1.1.1",  # 默认 80
    "https://8.8.8.8",  # 默认 443
    "https://8.8.8.8:8443",
    "http://1.0.0.1:8080",
    "http://8.8.4.4:3128",  # ≥1024 任意端口
    "http://1.1.1.1:6379",  # ≥1024 放行；内网 Redis 由 IP 层拦截
)


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_blocked(url: str):
    with pytest.raises(UrlBlockedError):
        validate_url(url)


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed(url: str):
    validated = validate_url(url)
    assert validated.ips


@pytest.mark.network  # 触真实 DNS（example.com）；默认跳过，隔离区语义见 conftest
def test_allowed_with_dns():
    try:
        validated = validate_url("http://example.com/")
    except UrlBlockedError as exc:
        pytest.skip(f"环境 DNS 不可用：{exc}")
    assert validated.host == "example.com"
    assert validated.ips


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.network  # 触真实 DNS（example.com）；默认跳过，隔离区语义见 conftest
def test_pinned_transport_preserves_url_and_pinned_ips(monkeypatch):
    """第 4 层契约：URL/SNI 保留原 hostname（不再改写），校验 IP 经上下文传递。"""
    captured: list[httpx.Request] = []
    seen_ips: list[list[str] | None] = []

    async def fake_upstream(self, request):
        captured.append(request)
        seen_ips.append(ssrf._PINNED_IPS.get())
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    try:
        validated_ips = set(validate_url("http://example.com/").ips)
    except UrlBlockedError as exc:
        pytest.skip(f"环境 DNS 不可用：{exc}")

    transport = ssrf._PinnedTransport()
    _run(transport.handle_async_request(httpx.Request("GET", "http://example.com/x")))

    req = captured[0]
    assert req.url.host == "example.com"  # URL 不改写 → TLS SNI 保持 hostname
    assert req.headers["host"] == "example.com"
    pinned_ips = seen_ips[0]
    assert pinned_ips is not None
    assert set(pinned_ips) == validated_ips  # 钉扎 IP 经 contextvar 传递
    assert ssrf._PINNED_IPS.get() is None  # 请求结束后上下文复位


def test_pinned_transport_no_rewrite_for_ip_literal(monkeypatch):
    captured: list[httpx.Request] = []

    async def fake_upstream(self, request):
        captured.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    transport = ssrf._PinnedTransport()
    # IP 字面量：无 DNS；URL/Host 头全程不被触碰
    _run(transport.handle_async_request(httpx.Request("GET", "http://8.8.8.8:8080/x")))

    pinned = captured[0]
    assert pinned.url.host == "8.8.8.8"
    assert pinned.headers["host"] == "8.8.8.8:8080"


def test_blocked_request_never_reaches_transport(monkeypatch):
    reached = False

    async def fake_upstream(self, request):
        nonlocal reached
        reached = True
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    transport = ssrf._PinnedTransport()
    with pytest.raises(UrlBlockedError):
        _run(transport.handle_async_request(httpx.Request("GET", "http://127.0.0.1/")))
    assert reached is False


def test_dns_resolving_to_private_is_blocked(monkeypatch):
    """第 3 层：公网域名解析到私网 IP 必须拒绝（rebinding 的前置形态）。"""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlBlockedError):
        validate_url("http://attacker.example.com/")


class _FakeBaseBackend(httpcore.AsyncNetworkBackend):
    """拨号桩：记录 dial 序列，可指定首个 IP 拒连（模拟故障转移）。"""

    def __init__(self, fail_for: set[str] | None = None):
        self.dials: list[str] = []
        self.fail_for = fail_for or set()

    # 桩签名须与 httpcore connect_tcp 一致，timeout 参数名不可改
    async def connect_tcp(
        self,
        host,
        port,
        timeout=None,  # noqa: ASYNC109
        local_address=None,
        socket_options=None,
    ):
        self.dials.append(host)
        if host in self.fail_for:
            raise OSError(f"connection refused: {host}")
        return object()


def test_pinned_backend_dials_validated_ip(monkeypatch):
    """第 4 层：连接层直接拨已验证 IP（contextvar 钉扎），零二次 DNS。"""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    transport = ssrf._PinnedTransport()
    fake = _FakeBaseBackend()
    pinned_backend = transport._pool._network_backend
    assert isinstance(pinned_backend, ssrf._PinnedNetworkBackend)
    pinned_backend._base = fake

    with contextlib.suppress(Exception):  # 桩 stream 不支持后续 IO，仅看拨号序列
        _run(transport.handle_async_request(httpx.Request("GET", "http://attacker.example.com/")))

    assert fake.dials == ["93.184.216.34"]  # 钉在 mock 解析出的 IP
    assert ssrf._PINNED_IPS.get() is None  # 请求结束后上下文复位


def test_pinned_backend_failover_in_validated_order():
    """多 IP 故障转移：按校验顺序逐个尝试，与 curl RESOLVE 列表语义对齐。"""
    base = _FakeBaseBackend(fail_for={"93.184.216.34"})
    backend = ssrf._PinnedNetworkBackend(base)
    token = ssrf._PINNED_IPS.set(["93.184.216.34", "8.8.8.8"])
    try:
        stream = _run(backend.connect_tcp("example.com", 443))
    finally:
        ssrf._PINNED_IPS.reset(token)
    assert base.dials == ["93.184.216.34", "8.8.8.8"]
    assert stream is not None


def test_pinned_backend_without_context_fails_closed(monkeypatch):
    """无预验证上下文（如代理连接目标）：现场解析并逐 IP 校验，fail-closed。"""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    backend = ssrf._PinnedNetworkBackend(httpcore.AsyncNetworkBackend())
    with pytest.raises(UrlBlockedError):
        _run(backend.connect_tcp("attacker.example.com", 443))


def test_pinned_transport_proxy_rewrites_target(monkeypatch):
    """经代理：连接目标是代理而非对端，退回「已验证 IP 字面量」路径。"""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    captured: list[httpx.Request] = []

    async def fake_upstream(self, request):
        captured.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    transport = ssrf._PinnedTransport(proxy="http://1.2.3.4:3128")
    _run(transport.handle_async_request(httpx.Request("GET", "http://attacker.example.com/x")))

    req = captured[0]
    assert req.url.host == "93.184.216.34"  # 交给代理的目标是已验证 IP 字面量
    assert req.headers["host"] == "attacker.example.com"  # Host 头保留原 hostname


# ---------------------------------------------------------------------------
# curl 通道回归（H1：curl_options 必须挂会话级 / H2：重定向必须由 SAFE 拦）
#
# 这些用例一律用真实 curl_cffi.AsyncSession + 真实（本地）HTTPServer。
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_resolve_table():
    """RESOLVE 是模块级累积表：curl 用例从空表开始，断言才能精确。"""
    with ssrf._CURL_OPTIONS_LOCK:
        ssrf._CURL_RESOLVE_ENTRIES.clear()
    yield
    with ssrf._CURL_OPTIONS_LOCK:
        ssrf._CURL_RESOLVE_ENTRIES.clear()


@contextlib.contextmanager
def _local_http_server(redirect_to: str | None = None):
    """本地 HTTP 服务：/redir 返回 302（Location 可配），/secret 返回敏感串。"""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/redir" and redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return
            payload = b"SECRET-LEAKED" if self.path == "/secret" else b"OK-FROM-SERVER"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _redirect_pair():
    """两跳场景：第一跳 /redir 302 → 第二跳（另一端口）的 /secret。"""
    with _local_http_server() as (secret_server, secret_hits):
        location = f"http://127.0.0.1:{secret_server.server_address[1]}/secret"
        with _local_http_server(redirect_to=location) as (hop_server, hop_hits):
            yield hop_server.server_address[1], hop_hits, secret_hits


def _stub_request_once(session, sink: dict) -> None:
    """打桩底层 _request_once 以免真实出网（签名/选项类用例用）。"""

    async def fake_request_once(*args, **kwargs):
        sink.update(kwargs)
        return "stub"

    session._request_once = fake_request_once


def _pin_host_to_local(monkeypatch, host: str, port: int) -> list[str]:
    """把 validate_url 打桩为「校验通过」，并让 RESOLVE 把 host 钉到 127.0.0.1。

    本机没有公网可达的监听端口，无法构造真实公网第一跳；第 1~3 层由上面的
    IP 清单用例覆盖，这里只针对第 4 层（钉扎）与重定向跟随行为。
    """
    calls: list[str] = []

    def fake_validate(url: str) -> ssrf.ValidatedUrl:
        calls.append(url)
        return ssrf.ValidatedUrl(url=url, scheme="http", host=host, port=port, ips=["127.0.0.1"])

    monkeypatch.setattr(ssrf, "validate_url", fake_validate)
    return calls


def test_curl_guard_injected_kwargs_are_legal_request_params(monkeypatch, clean_resolve_table):
    """H1 冒烟：wrapper 注入的 kwargs 必须全是 AsyncSession.request 的合法形参。

    修复前 wrapper 传 `curl_options=`；curl_cffi 0.16.3 的 AsyncSession.request
    既无该形参也无 **kwargs → 本用例必红。
    """
    request_params = set(inspect.signature(AsyncSession.request).parameters)
    assert "curl_options" not in request_params, "curl_cffi 语义已变，复核 H1 修复前提"
    assert "allow_redirects" in request_params

    injected: list[dict] = []
    session: Any = AsyncSession(impersonate="chrome146", verify=False, allow_redirects=True)

    async def recording_original(method, url, **kwargs):
        injected.append(dict(kwargs))
        # 用真实签名做绑定校验：非法形参在此抛 TypeError（修复前即 curl_options）
        inspect.signature(AsyncSession.request).bind(session, method, url, **kwargs)
        return "stub"

    monkeypatch.setattr(session, "request", recording_original)
    ssrf._wrap_curl_session(session)
    try:
        assert _run(session.request("GET", "http://8.8.8.8/dns-query")) == "stub"
    finally:
        _run(session.close())

    assert injected, "wrapper 未把调用透传给底层 request"
    assert set(injected[0]) <= request_params
    assert injected[0]["allow_redirects"] is CurlFollow.SAFE
    assert session.curl_options[CurlOpt.RESOLVE] == ["8.8.8.8:80:8.8.8.8"]


def test_curl_guard_pins_resolve_on_real_session(monkeypatch, clean_resolve_table):
    """H1 修复后：RESOLVE 挂在 session 上，真实请求经钉扎 IP 抵达本地服务。"""
    with _local_http_server() as (server, hits):
        port = server.server_address[1]
        calls = _pin_host_to_local(monkeypatch, "hop.example.com", port)
        session: Any = AsyncSession(impersonate="chrome146", verify=False, allow_redirects=True)
        ssrf._wrap_curl_session(session)
        try:
            resp = _run(session.get(f"http://hop.example.com:{port}/ok"))
        finally:
            _run(session.close())

    assert resp.status_code == 200
    assert resp.content == b"OK-FROM-SERVER"
    assert hits == ["/ok"]
    assert session.curl_options[CurlOpt.RESOLVE] == [f"hop.example.com:{port}:127.0.0.1"]
    assert calls == [f"http://hop.example.com:{port}/ok"]


def test_curl_guard_brackets_ipv6_resolve(monkeypatch, clean_resolve_table):
    """RESOLVE 的 ADDRESS 为 IPv6 字面量时必须带 []：libcurl 按冒号切分
    HOST:PORT:ADDRESS，裸 IPv6 会产生歧义而解析失败（curl 后端整体不可用）。"""
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::2222", 0, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    session: Any = AsyncSession(impersonate="chrome146", verify=False)
    sink: dict = {}
    _stub_request_once(session, sink)
    ssrf._wrap_curl_session(session)
    try:
        assert _run(session.request("GET", "https://ipv6.example.com/x")) == "stub"
    finally:
        _run(session.close())

    assert sink, "未透传到 _request_once"
    assert session.curl_options[CurlOpt.RESOLVE] == [
        "ipv6.example.com:443:[2606:4700::1111]",
        "ipv6.example.com:443:[2606:4700::2222]",
    ]


def test_curl_guard_preserves_session_level_options(clean_resolve_table):
    """session 上已有的 curl_options 必须保留（RESOLVE 只是新增/覆盖一项）。"""
    session: Any = AsyncSession(
        impersonate="chrome146", verify=False, curl_options={CurlOpt.TIMEOUT: 5000}
    )
    sink: dict = {}
    _stub_request_once(session, sink)
    ssrf._wrap_curl_session(session)
    try:
        assert _run(session.request("GET", "http://8.8.8.8/x")) == "stub"
    finally:
        _run(session.close())

    assert session.curl_options[CurlOpt.TIMEOUT] == 5000
    assert session.curl_options[CurlOpt.RESOLVE] == ["8.8.8.8:80:8.8.8.8"]


def test_curl_guard_accumulates_resolve_entries(clean_resolve_table):
    """RESOLVE 是 host→IP 映射表，跨请求累积复用（同键以最新校验结果覆盖）。"""
    session: Any = AsyncSession(impersonate="chrome146", verify=False)
    sink: dict = {}
    _stub_request_once(session, sink)
    ssrf._wrap_curl_session(session)
    try:
        _run(session.request("GET", "http://8.8.8.8/a"))
        first = list(session.curl_options[CurlOpt.RESOLVE])
        _run(session.request("GET", "http://1.1.1.1/b"))
    finally:
        _run(session.close())

    assert first == ["8.8.8.8:80:8.8.8.8"]
    assert session.curl_options[CurlOpt.RESOLVE] == ["8.8.8.8:80:8.8.8.8", "1.1.1.1:80:1.1.1.1"]


def test_curl_guard_blocks_private_before_any_io():
    """校验失败时不得发起任何请求（fail-closed）。"""
    session: Any = AsyncSession(impersonate="chrome146", verify=False)
    sink: dict = {}
    _stub_request_once(session, sink)
    ssrf._wrap_curl_session(session)
    try:
        with pytest.raises(UrlBlockedError):
            _run(session.request("GET", "http://192.168.1.1/"))
    finally:
        _run(session.close())
    assert sink == {}


def test_curl_guard_blocks_redirect_to_internal(monkeypatch, clean_resolve_table):
    """H2 端到端：第一跳真实抵达，第二跳重定向到内网被 libcurl 拒绝。

    本机没有公网可达的监听端口，无法构造「第一跳公网、第二跳内网」；这里用
    RESOLVE 把 host 钉到 127.0.0.1 让第一跳落地，重定向目标仍是字面量
    127.0.0.1——对 CurlFollow.SAFE 而言即「第一跳放行、第二跳内网」，
    正是要防的形态（实测 SAFE 只审查重定向目标，不审查首个 URL）。
    """
    with _redirect_pair() as (port, hop_hits, secret_hits):
        calls = _pin_host_to_local(monkeypatch, "hop.example.com", port)
        session: Any = AsyncSession(impersonate="chrome146", verify=False, allow_redirects=True)
        ssrf._wrap_curl_session(session)
        error: Exception | None = None
        try:
            _run(session.get(f"http://hop.example.com:{port}/redir"))
        except Exception as exc:
            error = exc
        finally:
            _run(session.close())

    assert hop_hits == ["/redir"], "第一跳必须真实抵达，否则本用例是假绿"
    assert secret_hits == [], "重定向到内网被放行（H2 回归）"
    assert error is not None and "SSRF protection" in str(error), f"未由 SAFE 拦截：{error!r}"
    # SAFE 在 libcurl C 层拦截，Python 侧不会对被跟随的跳转二次调用 validate_url
    assert calls == [f"http://hop.example.com:{port}/redir"]


def test_redirect_leak_is_real_without_safe(monkeypatch, clean_resolve_table):
    """敏感度对照：重定向策略退回 True（= 未修复 H2）时 secret 必然泄漏。

    证明上一条「重定向被拒」不是恒绿——它真的能抓到 H2。
    """
    monkeypatch.setattr(ssrf, "_safe_redirect_policy", lambda: True)
    with _redirect_pair() as (port, hop_hits, secret_hits):
        _pin_host_to_local(monkeypatch, "hop.example.com", port)
        session: Any = AsyncSession(impersonate="chrome146", verify=False, allow_redirects=True)
        ssrf._wrap_curl_session(session)
        try:
            resp = _run(session.get(f"http://hop.example.com:{port}/redir"))
        finally:
            _run(session.close())

    assert hop_hits == ["/redir"]
    assert secret_hits == ["/secret"], "对照失效：无 SAFE 也没泄漏，上一条用例无区分度"
    assert resp.content == b"SECRET-LEAKED"


def test_curl_guard_fails_closed_without_safe_member(monkeypatch, clean_resolve_table):
    """老版本 curl_cffi 无 CurlFollow.SAFE：退化为不跟随重定向，绝不静默放行。"""

    class _LegacyCurlFollow:  # 仅缺 SAFE 成员
        ALL = 1
        OBEYCODE = 2
        FIRSTONLY = 3

    monkeypatch.setattr(ssrf, "CurlFollow", _LegacyCurlFollow)
    assert ssrf._safe_redirect_policy() is False

    injected: list[dict] = []
    session: Any = AsyncSession(impersonate="chrome146", verify=False, allow_redirects=True)

    async def recording_original(method, url, **kwargs):
        injected.append(dict(kwargs))
        return "stub"

    session.request = recording_original
    ssrf._wrap_curl_session(session)
    try:
        assert _run(session.request("GET", "http://8.8.8.8/x")) == "stub"
    finally:
        _run(session.close())
    assert injected[0]["allow_redirects"] is False


def test_install_ssrf_guard_idempotent():
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
        DOWNLOADER,
    )

    client = DOWNLOADER.client
    orig_httpx = client._httpx
    orig_curl_request = client._curl.request
    guarded_httpx = None
    try:
        ssrf.install_ssrf_guard()
        guarded_httpx = client._httpx
        guarded_curl_request = client._curl.request
        assert guarded_httpx is not orig_httpx
        assert getattr(client, "_ssrf_guarded", False) is True

        ssrf.install_ssrf_guard()  # 二次安装必须为 no-op（插件重载场景）
        assert client._httpx is guarded_httpx
        assert client._curl.request is guarded_curl_request
    finally:
        client._httpx = orig_httpx
        client._curl.request = orig_curl_request
        client._ssrf_guarded = False
        if guarded_httpx is not None:
            _run(ssrf._aclose_quietly(guarded_httpx))
