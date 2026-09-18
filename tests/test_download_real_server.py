"""第四轮长任务工作链测试：本地真实 HTTP 服务器驱动下载全链（真实 socket）。

区别于 test_workchains_advanced.py 的 _FakeStreamClient 注入：本文件以
ThreadingHTTPServer 起真实环回源站，UniHttpClient（httpx / curl_cffi 双后端）
走完整真实 HTTP 栈，验证 fake 语义与 vendor 真实实现一致：
- 全量下载字节一致（首跳不带 Range）
- 断点续传：预置 .part 后真实 Range/206/Content-Range 拼接
- .meta 文件缓存命中零请求、并发同键下载单飞（源站只收到一次 GET）
- 超限 Content-Length 在响应头阶段立即终止（body 未消费、partial 被清理）

StreamDownloader() 新实例在 __init__ 自建 UniHttpClient，不受测试套件安装到
DOWNLOADER 单例上的 SSRF guard 影响（guard 拒绝环回地址的语义已在
test_ssrf.py 钉死）；本文件的环回连接因此是「绕过 guard 的白盒测试环境」，
不改变生产语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
    StreamDownloader,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.exception import (
    SizeLimitException,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.cache import (
    CacheManager,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.common import (
    generate_file_name,
)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB 确定性载荷
PARTIAL_SIZE = 2


class _OriginHandler(BaseHTTPRequestHandler):
    """固定载荷源站：支持单区间 Range/206 与超大 Content-Length 声明。"""

    def do_GET(self) -> None:
        origin = self.server
        assert isinstance(origin, _OriginServer)
        origin.requests.append(("GET", self.path, self.headers.get("Range")))
        if self.path == "/oversize":
            # 只声明不发送：vendor 在响应头阶段校验尺寸立即终止，不消费 body
            self.send_response(200)
            self.send_header("Content-Length", str(91 * 1024 * 1024))
            self.end_headers()
            return
        if (rng := self.headers.get("Range")) is not None:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _OriginServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _OriginHandler)
        self.requests: list[tuple[str, str, str | None]] = []

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        assert isinstance(host, str)  # 绑定的是 AF_INET 字符串地址
        return f"http://{host}:{port}"

    @property
    def get_count(self) -> int:
        return sum(1 for entry in self.requests if entry[0] == "GET")


@pytest.fixture()
def origin() -> Iterator[_OriginServer]:
    server = _OriginServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@contextlib.asynccontextmanager
async def _downloader() -> AsyncIterator[StreamDownloader]:
    instance = StreamDownloader()
    try:
        yield instance
    finally:
        await instance.aclose()


def _media_dir() -> Any:
    return CacheManager.cache_dir(CacheManager.MEDIA)


async def test_full_download_over_real_socket(origin: _OriginServer) -> None:
    async with _downloader() as downloader:
        path = await downloader.streamd(
            url=f"{origin.url}/full.bin", cache_key="rs-full", default_suffix=".bin"
        )

    assert path.suffix == ".bin"
    assert await path.read_bytes() == PAYLOAD
    assert origin.get_count == 1
    first_get = next(entry for entry in origin.requests if entry[0] == "GET")
    assert first_get[2] is None  # 全量下载首跳不带 Range


async def test_meta_cache_hit_skips_network(origin: _OriginServer) -> None:
    url = f"{origin.url}/cached.bin"
    async with _downloader() as downloader:
        first = await downloader.streamd(url=url, cache_key="rs-cache", default_suffix=".bin")
        assert await first.read_bytes() == PAYLOAD
        assert origin.get_count == 1

        second = await downloader.streamd(url=url, cache_key="rs-cache", default_suffix=".bin")

    assert second == first
    assert origin.get_count == 1  # .meta 缓存命中，零新增请求


async def test_resume_from_partial_over_real_socket(origin: _OriginServer) -> None:
    url = f"{origin.url}/resume.bin"
    partial = _media_dir() / f"{generate_file_name(url, 'rs-resume')}.part"
    await partial.write_bytes(PAYLOAD[:PARTIAL_SIZE])

    async with _downloader() as downloader:
        path = await downloader.streamd(url=url, cache_key="rs-resume", default_suffix=".bin")

    assert await path.read_bytes() == PAYLOAD  # 断点 + 剩余字节无缝拼接
    gets = [entry for entry in origin.requests if entry[0] == "GET"]
    assert len(gets) == 1
    assert gets[0][2] == f"bytes={PARTIAL_SIZE}-"  # 真实 Range 请求头
    assert gets[0][1] == "/resume.bin"


async def test_concurrent_same_key_single_flight(origin: _OriginServer) -> None:
    url = f"{origin.url}/single.bin"
    async with _downloader() as downloader:
        paths = await asyncio.gather(
            *(
                downloader.streamd(url=url, cache_key="rs-single", default_suffix=".bin")
                for _ in range(3)
            )
        )

    assert len({str(p) for p in paths}) == 1
    assert await paths[0].read_bytes() == PAYLOAD
    assert origin.get_count == 1  # 单飞：源站只收到一次 GET


async def test_oversize_content_length_terminates_before_body(
    origin: _OriginServer,
) -> None:
    url = f"{origin.url}/oversize"
    partial = _media_dir() / f"{generate_file_name(url, 'rs-oversize')}.part"

    async with _downloader() as downloader:
        with pytest.raises(SizeLimitException):
            await downloader.streamd(url=url, cache_key="rs-oversize", default_suffix=".bin")

    assert origin.get_count == 1  # 响应头阶段终止，不进重试循环
    assert not await partial.exists()  # partial 被清理（size 分支语义）


async def test_full_download_via_curl_backend(origin: _OriginServer) -> None:
    """curl_cffi 后端在 fake 测试中从未真实联网，此处走真实 socket 补齐。"""
    async with _downloader() as downloader:
        path = await downloader.streamd(
            url=f"{origin.url}/curl.bin",
            cache_key="rs-curl",
            default_suffix=".bin",
            use_curl_cffi=True,
        )

    assert await path.read_bytes() == PAYLOAD
    assert origin.get_count == 1
