"""SSRF 防护（参照 GitLab lib/gitlab/url_blocker.rb 四层模型）。

覆盖 vendor 的出站 HTTP 面（vendor 文件零修改）：
1. 下载器 DOWNLOADER.client：桥接启动时把其 httpx 客户端替换为带钉扎
   transport 的客户端，并包装 curl_cffi 会话的 request 入口；
2. parser API 面 BaseParser.httpx（28 个平台 parser 的接口请求，含
   kuaishou 等短链重定向目标直连）：包装 BaseParser.__init__，实例化后
   替换为带同一钉扎 transport 的客户端；
3. 辅助客户端：weibo AuthHelper.SESSION、bilibili HTTP_CLIENT /
   GRPC_CLIENT._client（模块级 from-import 绑定，只能就地换 transport，
   不能重绑模块属性）。

四层：
1. scheme 白名单 {http, https}；
2. 端口规则（80/443 与 >=1024 放行）；
3. getaddrinfo 全部结果逐 IP 校验（环回/私网/链路本地/保留/多播/未指定 全拒；
   IPv4-mapped IPv6 归一到 IPv4 再判定，拦 ::ffff:127.0.0.1 等）；
4. 解析即连接钉扎：把 TCP 连接钉在已验证 IP 上（httpx 走 httpcore network
   backend，URL/SNI 保留原 hostname；curl_cffi 走会话级 CurlOpt.RESOLVE），
   DNS 二次解析被绕开，rebinding 失效。

重定向（httpx 与 curl 两条面均在 Python 侧逐跳校验）：
httpx 侧 follow_redirects=True 由 httpx 在 Python 侧逐跳重发，每跳都重回
_PinnedTransport.handle_async_request，即每跳都过完整四层校验。
curl 侧：libcurl 在 C 层内部跟随重定向不会重回被包装的 session.request，
故 wrapper 强制 allow_redirects=False，由 Python 手动跟随，每跳先
validate_url 再发请求——与 httpx 同口径。CurlFollow.SAFE 把跳转判定交给
libcurl，口径弱于 _ip_forbidden（CGNAT/TEST-NET/224/4/0.0.0.0/NAT64
漏拦），不作为重定向依据。

代理：配置代理时连接目标是代理而非对端，httpx 侧退回「请求目标改写为已
验证 IP 字面量」路径（本地校验语义 fail-closed 不变）；curl_cffi 语义同前。
任何校验失败一律拒绝（fail-closed）。

安装：整装锁 + 全部成功后才置 _ssrf_guarded——中途失败时下次 install
仍可重试，不产生半包装通道。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import ipaddress
import logging
import socket
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import anyio
import httpcore
import httpx
from curl_cffi.const import CurlOpt

# anyio 为 vendor 运行链硬依赖（nonebot2 与 render.py 均直接导入），无回退路径
_offload = anyio.to_thread.run_sync

logger = logging.getLogger(__name__)


ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
"""持有后台清理任务引用，防止被垃圾回收中途取消"""

# 继承上游 vendor 的 TLS 校验姿态：上游 UniHttpClient 创建客户端即关闭证书
# 校验（CDN 证书链兼容的既定取舍），vendor 零修改约束下桥内保持同一行为。
# 风险与缓解见 README「安全说明」与 CONTEXT.md。
_INHERIT_VENDOR_VERIFY: bool = False


class UrlBlockedError(BaseException):
    """URL 被 SSRF 策略拒绝。

    继承 BaseException（而非 Exception）：vendor 的 @retry 与各
    ``except Exception`` 包装点不会吞掉或重试被拦截的请求——SSRF 拒绝是
    安全终态，不是可恢复的传输错误。插件入口须显式 except 本类。

    域名解析失败（DNS 瞬断、无记录）不属于本类：那是可恢复的传输层
    故障（见 UrlResolveError），策略拒绝与网络故障共享安全终态会把
    装饰资源的抖动升级为整卡硬失败。
    """


class UrlResolveError(Exception):
    """域名解析失败（gaierror / 空结果），按普通可恢复异常处理。

    解析不到地址即无出站可谈，不构成 SSRF 面；本类走 vendor 与 safe_src
    的 ``except Exception`` 降级通道（重试 / 占位图 / DownloadException），
    与策略拒绝（UrlBlockedError，BaseException 直达插件入口）分道。
    """


EXTRA_FORBIDDEN_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "100.64.0.0/10",  # RFC 6598 CGNAT 共享地址段（stdlib 各版本判定不一）
        "198.18.0.0/15",  # RFC 2544 基准测试段
        "192.0.2.0/24",  # TEST-NET-1
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "2001:db8::/32",  # IPv6 文档段
    )
)


def _ip_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """GitLab validate_internal_addresses 语义：非公网单播一律拒绝。

    IPv4-mapped IPv6（``::ffff:127.0.0.1`` 等）先归一为 IPv4 再判定：
    stdlib 对 mapped 形态的 ``is_loopback/is_private`` 在部分版本不触发，
    会漏拦经该形态表达的环回/内网地址。
    """
    if isinstance(ip, ipaddress.IPv6Address) and (mapped := ip.ipv4_mapped) is not None:
        ip = mapped
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local  # 169.254.0.0/16, fe80::/10（含元数据端点）
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in ipaddress.ip_network("64:ff9b::/96")  # NAT64 可映射内网
    ):
        return True
    return any(ip in net for net in EXTRA_FORBIDDEN_NETWORKS)


@dataclass(slots=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int
    ips: list[str]


def _validate_ip_literal(value: str) -> str:
    ip = ipaddress.ip_address(value)
    if _ip_forbidden(ip):
        raise UrlBlockedError(f"IP 地址被拒绝：{value}")
    return str(ip)


def _resolve_and_validate(hostname: str) -> list[str]:
    """getaddrinfo 全部结果逐 IP 校验，返回已验证 IP 列表。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UrlResolveError(f"域名解析失败：{hostname}") from exc
    if not infos:
        raise UrlResolveError(f"域名解析为空：{hostname}")

    validated: list[str] = []
    for info in infos:
        candidate = str(info[4][0])
        ip = ipaddress.ip_address(candidate.split("%")[0])
        if _ip_forbidden(ip):
            raise UrlBlockedError(f"域名 {hostname} 解析到被拒绝的地址 {candidate}")
        if str(ip) not in validated:
            validated.append(str(ip))
    return validated


def validate_url(url: str) -> ValidatedUrl:
    """校验 URL 的 scheme/端口/解析结果。

    策略拒绝抛 UrlBlockedError（BaseException，安全终态直达插件入口）；
    域名解析失败抛 UrlResolveError（普通 Exception，随传输故障降级）。

    两类拒绝在此统一落审计日志（httpx transport / curl guard / HLS guard
    三条通道都经本函数收口）：日志是被拒目标与原因的唯一运维入口。
    """
    try:
        return _validate_url(url)
    except (UrlBlockedError, UrlResolveError) as exc:
        logger.warning("SSRF 拒绝出站请求: url=%s 原因=%s", url, exc)
        raise


def _validate_url(url: str) -> ValidatedUrl:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlBlockedError(f"scheme 不在白名单：{scheme or '(空)'}")

    hostname = parts.hostname
    if not hostname:
        raise UrlBlockedError(f"URL 缺少主机名：{url}")
    try:
        port = parts.port  # 显式端口；非法值（如 :abc）在 urlsplit 即抛 ValueError
    except ValueError as exc:
        raise UrlBlockedError(f"端口非法：{url}") from exc

    if port is None:
        port = DEFAULT_PORTS[scheme]
    if port != DEFAULT_PORTS[scheme] and port < 1024:
        raise UrlBlockedError(f"端口被拒绝：{port}")

    trimmed = hostname.strip("[]").rstrip(".")
    try:
        # IP 字面量：直接校验，不经解析（防止字面量伪装）
        ips = [_validate_ip_literal(trimmed)]
    except ValueError:
        ips = _resolve_and_validate(trimmed)

    return ValidatedUrl(url=url, scheme=scheme, host=hostname, port=port, ips=ips)


def _resolve_pinned_fallback(host: str) -> list[str]:
    """无预验证上下文（如代理连接目标）时现场解析并逐 IP 校验；拒绝落审计日志。"""
    trimmed = host.strip("[]").rstrip(".")
    try:
        try:
            return [_validate_ip_literal(trimmed)]
        except ValueError:
            return _resolve_and_validate(trimmed)
    except (UrlBlockedError, UrlResolveError) as exc:
        logger.warning("SSRF 拒绝拨号目标（无预验证上下文）: host=%s 原因=%s", host, exc)
        raise


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore 网络后端包装：把 TCP 连接钉在已验证 IP 上，逐个尝试直至成功。

    包装（而非继承）池默认后端：httpx 的连接池以原 URL hostname 为键
    （TLS SNI/keepalive 语义不变），实际拨号前改拨已验证 IP——优先取
    handle_async_request 预验证的 IP 集合（contextvar 传递，零二次解析），
    无预验证上下文（如代理连接目标）时现场解析并逐 IP 校验。无论哪条
    路径，拨号目标都只可能是已验证公网 IP。
    """

    def __init__(self, base: httpcore.AsyncNetworkBackend) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        if name == "_base":  # 仅常规查找失败时触发；防初始化期递归
            raise AttributeError(name)
        return getattr(self._base, name)  # unix socket 等其余后端方法原样委托

    async def connect_tcp(
        self,
        host: str,
        port: int,
        # 参数名/顺序须与 httpcore.AsyncNetworkBackend.connect_tcp 抽象签名逐位一致
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        pinned = _PINNED_IPS.get()
        # 回退解析内的 getaddrinfo 是阻塞调用，而 connect_tcp 跑在事件循环上：
        # 与 handle_async_request 同口径投线程池，DNS 慢/黑洞不冻结整个循环
        ips = pinned if pinned is not None else await _offload(_resolve_pinned_fallback, host)
        last_error: Exception | None = None
        for ip in ips:
            try:
                return await self._base.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as exc:
                last_error = exc  # 多 IP 故障转移：与 curl RESOLVE 列表语义对齐
        assert last_error is not None
        raise last_error


_PINNED_IPS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "ssrf_pinned_ips", default=None
)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """解析即连接：请求先过四层校验，连接钉在已验证 IP 上，URL/SNI 不改写。

    钉扎在 httpcore network backend 层完成（构造时替换连接池后端）：连接
    池与 TLS 握手都保持原 hostname——SNI 不再退化为 IP，URL 不再被改写。
    配置代理时连接目标是代理而非对端（钉扎无处安放），退回旧路径：请求
    目标改写为已验证 IP 字面量，本地校验语义 fail-closed 保持。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # httpcore 连接池结构漂移时在此响亮失败（缝合点契约，test_ssrf 守护）
        if not hasattr(self._pool, "_network_backend"):
            raise RuntimeError("httpcore 连接池缺少 _network_backend，SSRF 钉扎层需复核")
        self._pool._network_backend = _PinnedNetworkBackend(self._pool._network_backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # validate_url 内的 getaddrinfo 是阻塞调用，放线程池执行避免卡死事件循环
        validated = await _offload(validate_url, str(request.url))
        # 代理池用类型探测：httpx 传入代理时 pool 是 AsyncHTTPProxy/AsyncSOCKSProxy，
        # 其私有 _proxy 属性在 httpx 装配路径下可能为 None，不可依赖
        if isinstance(self._pool, (httpcore.AsyncHTTPProxy, httpcore.AsyncSOCKSProxy)):
            # 经代理：请求以已验证 IP 字面量交给代理（Host 头保留原 hostname）
            original_host = request.url.host or ""
            pinned = request.url.copy_with(host=validated.ips[0])
            if original_host.lower() != validated.ips[0]:
                host_header = (
                    f"{original_host}:{request.url.port}" if request.url.port else original_host
                )
                request.headers["Host"] = host_header
                request.url = pinned
            return await super().handle_async_request(request)
        token = _PINNED_IPS.set(validated.ips)
        try:
            return await super().handle_async_request(request)
        finally:
            _PINNED_IPS.reset(token)


_CURL_OPTIONS_LOCK = threading.Lock()
"""守护 _CURL_RESOLVE_ENTRIES：curl_options 是会话级共享状态，而校验在
anyio 线程池里执行，并发请求可能同时读写该表。"""

_CURL_RESOLVE_ENTRIES: dict[str, list[str]] = {}
"""模块级 RESOLVE 共享表：``host:port`` → 地址列表（IPv6 带 []）。

RESOLVE 是「域名 → IP」映射表，天然可跨请求累积复用；每次校验通过后用
最新结果覆盖同键条目，域名重绑定到新地址时旧钉扎立即失效、不残留。
同一 host:port 保留多个地址时 libcurl 按序故障转移（原行为）。
"""


_MAX_CURL_REDIRECTS = 30
"""curl 通道手动跟随重定向的跳数上限（与 libcurl 默认一致，防循环）。"""


def _publish_resolve_entries(validated: ValidatedUrl) -> list[str]:
    """把本次校验结果并入模块级 RESOLVE 表，返回 libcurl 可用的条目列表。"""
    # libcurl 规定地址为 IPv6 字面量时必须写在 [] 内，否则冒号与
    # HOST:PORT:ADDRESS 的分隔符歧义，curl 直接报格式错误——IPv6 优先
    # 环境下表现为 curl 后端整体下载失败（2026-09-14 评审发现，
    # 原实现裸拼 "host:port:2606:4700::1111"）
    fresh = {
        f"{validated.host}:{validated.port}": [
            (f"[{ip}]" if ":" in ip else ip) for ip in validated.ips[:4]
        ]
    }
    with _CURL_OPTIONS_LOCK:
        _CURL_RESOLVE_ENTRIES.update(fresh)
        return [f"{key}:{addr}" for key, addrs in _CURL_RESOLVE_ENTRIES.items() for addr in addrs]


def _wrap_curl_session(session: Any) -> None:
    """给 curl_cffi AsyncSession 的 request 入口加校验 + 会话级钉扎。

    curl_cffi 的 get/head/post/stream 全部经 self.request（实例属性）路由，
    包一层即可全覆盖。

    curl_options 只能挂**会话**：curl_cffi 0.16.3 的 AsyncSession.request
    签名里既没有 curl_options 也没有 **kwargs，把它当关键字实参传进去会抛
    TypeError（curl 通道整体不可用，2026-09-16 评审实测确认）；真正被
    _request_once 读取的是 self.curl_options。

    重定向在 Python 侧手动跟随（allow_redirects=False）：每跳先 validate_url
    再发请求，与 httpx _PinnedTransport 同口径；CurlFollow.SAFE 判定弱于
    _ip_forbidden（见模块 docstring），不作为重定向依据。
    """
    if getattr(session, "_ssrf_guarded", False):
        return
    original = session.request

    async def guarded_request(method: str, url: str = "", **kwargs: Any) -> Any:
        # 注入的 kwargs 必须全部是 AsyncSession.request 的合法形参（冒烟测试守护）
        kwargs["allow_redirects"] = False
        current_url = str(url)
        current_method = method
        for _hop in range(_MAX_CURL_REDIRECTS + 1):
            validated = await _offload(validate_url, current_url)
            options = dict(getattr(session, "curl_options", None) or {})
            options[CurlOpt.RESOLVE] = _publish_resolve_entries(validated)
            session.curl_options = options
            response = await original(current_method, current_url, **kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            if status not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                return response
            if _hop >= _MAX_CURL_REDIRECTS:
                raise UrlBlockedError(f"重定向超过 {_MAX_CURL_REDIRECTS} 跳：{current_url}")
            # 相对 Location 按当前响应 URL 解析
            base = str(getattr(response, "url", "") or current_url)
            current_url = urljoin(base, location)
            # RFC 7231：303 恒改 GET；301/302 对非安全方法改 GET（与 curl 默认一致）。
            # 改 GET 后请求体参数全部弃用（libcurl 同语义），否则 data/content/
            # files 会让部分服务器拒绝 GET 或以体内容影响响应
            if status == 303 or (
                status in (301, 302) and current_method.upper() not in ("GET", "HEAD")
            ):
                current_method = "GET"
                for body_kwarg in ("data", "json", "content", "files"):
                    kwargs.pop(body_kwarg, None)
        raise UrlBlockedError(f"重定向超过 {_MAX_CURL_REDIRECTS} 跳：{url}")

    session.request = guarded_request
    session._ssrf_guarded = True


def _proxy_pool_origin(pool: Any) -> httpx.Proxy | None:
    """从 httpcore 代理池还原原始 httpx.Proxy（URL + 认证/附加头）。

    httpcore 的 ``_proxy_url`` 是 httpcore.URL（scheme/host 为 bytes，``str()``
    产出 repr 而非 URL 串），须手工重组；httpx 的 Proxy 把 auth 折叠进
    headers（Proxy-Authorization），``_proxy_headers`` 即完整头集合。
    结构漂移（属性缺失/形态不符）返回 None，由调用方退化就地换后端。
    """
    proxy_url = getattr(pool, "_proxy_url", None)
    if proxy_url is None:
        return None
    try:
        scheme = proxy_url.scheme.decode("ascii")
        host = proxy_url.host.decode("ascii")
        if not scheme or not host:
            return None
        url = f"{scheme}://{host}" + (f":{proxy_url.port}" if proxy_url.port else "")
        return httpx.Proxy(url, headers=getattr(pool, "_proxy_headers", None))
    except (AttributeError, UnicodeDecodeError):
        return None


def _pin_existing_httpx(client: Any, *, verify: bool = True, http2: bool = False) -> None:
    """就地把已构造的 httpx.AsyncClient 换到钉扎 transport（幂等）。

    模块级 ``from .client import HTTP_CLIENT`` 绑定的是对象引用，重绑
    模块属性不会传播到已绑定的调用点——必须换对象内部的 transport。
    TLS/http2 姿态按调用方声明重建（见 _wrap_aux_clients 各客户端原构造参数）。

    ``_mounts`` 一并处理：trust_env 客户端构造时读环境代理生成挂载，挂载
    命中的请求不经 ``_transport``——不处理等于「配置了代理的机器上，该
    客户端全部请求绕过钉扎校验」。代理挂载按原代理参数重建为
    _PinnedTransport（与下载器代理路径同语义：目标校验改写 + 代理拨号受
    校验）；重建失败或非代理挂载退化为就地换连接池后端（拨号面仍受逐 IP
    校验，目标 URL 校验缺失按结构漂移告警）。
    """
    if getattr(client, "_ssrf_pinned", False):
        return
    client._transport = _PinnedTransport(verify=verify, http2=http2)
    mounts = getattr(client, "_mounts", None)
    if mounts:
        for pattern, transport in dict(mounts).items():
            pool = getattr(transport, "_pool", None)
            proxy = _proxy_pool_origin(pool)
            if proxy is not None:
                mounts[pattern] = _PinnedTransport(proxy=proxy, verify=verify, http2=http2)
                continue
            if pool is not None and hasattr(pool, "_network_backend"):
                logger.warning(
                    "挂载 transport 无法还原代理参数，退化为就地换后端（目标 URL 校验缺失）: "
                    "pattern=%r pool=%s",
                    str(pattern),
                    type(pool).__name__,
                )
                pool._network_backend = _PinnedNetworkBackend(pool._network_backend)
    client._ssrf_pinned = True


def _wrap_aux_clients() -> None:
    """把 weibo/bilibili 辅助出站客户端一并纳入钉扎（就地换 transport）。"""
    from ..vendor.nonebot_plugin_parser_lite.parsers.weibo.auth import AuthHelper
    from ..vendor.nonebot_plugin_parser_lite.utils.bilibili.client import (
        GRPC_CLIENT,
        HTTP_CLIENT,
    )

    # AuthHelper.SESSION = AsyncClient(timeout=COMMON_TIMEOUT) → 默认 verify=True
    _pin_existing_httpx(AuthHelper.SESSION, verify=True, http2=False)
    # HTTP_CLIENT = AsyncClient(verify=True, trust_env=True, follow_redirects=True)
    _pin_existing_httpx(HTTP_CLIENT, verify=True, http2=False)
    # BiliGRPCClient._client = AsyncClient(http2=True, verify=True, trust_env=False)
    _pin_existing_httpx(GRPC_CLIENT._client, verify=True, http2=True)


def _wrap_parser_clients() -> None:
    """把 BaseParser 自建的 httpx.AsyncClient（28 个 parser 的 API 请求面）
    一并纳入钉扎（vendor 类零修改：包装 __init__，实例化后替换 self.httpx）。

    上游 kuaishou 等 parser 会把短链重定向目标直接交给该客户端请求，原样
    放行等于 SSRF 守卫缺口；langchain SSRFSafeTransport / onyx MCP guard
    的行业惯例即 transport 层全量覆盖出站请求。TLS 姿态保持上游原样
    （httpx 默认 verify=True，与下载器的 _INHERIT_VENDOR_VERIFY 无关）。
    """
    from ..vendor.nonebot_plugin_parser_lite.parsers.base import BaseParser

    if getattr(BaseParser, "_ssrf_guarded", False):
        return
    original_init = BaseParser.__init__

    @functools.wraps(original_init)
    def guarded_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        old = self.httpx
        # TLS 姿态：显式 transport 下 httpx 的 verify 参数不参与装配，
        # _PinnedTransport 内部连接池按 httpx 默认 verify=True 校验证书
        self.httpx = httpx.AsyncClient(
            headers=old.headers,
            timeout=old.timeout,
            follow_redirects=True,
            transport=_PinnedTransport(),
        )
        # old 尚未发出任何请求（__init__ 刚建），事件循环内异步关闭即可
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(_aclose_quietly(old))
            _BACKGROUND_TASKS.add(task)
            task.add_done_callback(_BACKGROUND_TASKS.discard)

    BaseParser.__init__ = guarded_init
    BaseParser._ssrf_guarded = True


_INSTALL_LOCK = threading.Lock()
"""守护 install_ssrf_guard：插件重载可能并发进入；整装成功前不置守卫标志。"""


def install_ssrf_guard() -> None:
    """替换 vendor 下载器客户端为带防护的实例（vendor 文件零修改，幂等）。

    依赖 vendor 内部结构 `DOWNLOADER.client._httpx/_curl`——契约测试守护该
    缝合点；若上游改名，测试红、本函数抛 AttributeError，插件按降级路径运行。

    顺序：锁内先完成全部包装（下载器 + parser + 辅助客户端），**全部成功后**
    才置 ``_ssrf_guarded``——中途失败时下次 install 仍可重试，不产生
    半包装通道（早置标志会让未完成的通道永久失去守卫）。
    """
    with _INSTALL_LOCK:
        from ..vendor.nonebot_plugin_parser_lite.download import DOWNLOADER

        client = DOWNLOADER.client
        if getattr(client, "_ssrf_guarded", False):
            return
        old_httpx = client._httpx
        old_curl = client._curl

        client._httpx = httpx.AsyncClient(
            timeout=old_httpx.timeout,
            # 继承上游 vendor 客户端的 TLS 姿态（_upstream/download/client.py 同款，
            # Chromium "MUST NOT modify" 约束下不改其行为）；CDN 证书链兼容是上游
            # 的既定取舍，风险与缓解见 README 安全小节。
            verify=_INHERIT_VENDOR_VERIFY,
            follow_redirects=True,
            transport=_PinnedTransport(),
        )
        _wrap_curl_session(client._curl)
        _wrap_parser_clients()
        _wrap_aux_clients()
        # 全部成功后才置标志（见 docstring）
        client._ssrf_guarded = True

        # 旧客户端尚未发过请求；在事件循环内异步清理，无循环时交给 GC。
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            for coro_fn, target in (
                (_aclose_quietly, old_httpx),
                (_close_curl_quietly, old_curl),
            ):
                task = loop.create_task(coro_fn(target))
                _BACKGROUND_TASKS.add(task)
                task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _aclose_quietly(client: httpx.AsyncClient) -> None:
    with contextlib.suppress(Exception):
        await client.aclose()


async def _close_curl_quietly(session: Any) -> None:
    with contextlib.suppress(Exception):
        result = session.close()
        if inspect.isawaitable(result):
            await result
