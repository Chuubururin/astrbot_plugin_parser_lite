"""第二轮长任务工作链测试：更复杂的连续操作与失败注入。

新增覆盖链路（区别于 test_workchains.py 的首轮链路）：
- 结果缓存逐出链：51 条入缓存触发 FIFO 逐出；更新已有键不刷新新旧序（上游契约）
- 下载失败注入链：重试 + fallback URL 轮换、断点续传（Range/Content-Range 校验）、
  416 断点失效重下、超限/零字节立即终止不重试、并发同键下载单飞、文件缓存命中跳网
- 调度链：PeriodicScheduler.add_job 幂等（重复 parse 不堆叠清理任务）
- SSRF 逐跳链：follow_redirects 每跳重进 _PinnedTransport，302 跳内网被拦
- 派发链：14 平台黄金 URL 连续 match 稳定命中 + 整段文本最长关键词权威
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from anyio import Path
from astrbot_plugin_parser_lite.bridge import ssrf
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import pipeline
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.constants import (
    PlatformEnum,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.data import (
    ImageContent,
    ParseResult,
    Platform,
    VideoContent,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download import (
    DOWNLOADER,
    StreamDownloader,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download.client import (
    HTTPStatusError,
    RetryableDownloadError,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.download.task import (
    DownloadTaskWrapper,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.exception import (
    SizeLimitException,
    ZeroSizeException,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.base import (
    BaseParser,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.parsers.bilibili import (
    BilibiliParser,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.cache import (
    CacheManager,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.common import (
    LimitedSizeDict,
    generate_file_name,
)
from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite.utils.scheduler import (
    PeriodicScheduler,
)

# 与 test_match_golden.py 同源的黄金样本（离线 match，不触网）
GOLDEN_URLS: tuple[tuple[str, str], ...] = (
    ("https://www.bilibili.com/video/BV1GJ411x7h7/?p=1", "bilibili"),
    ("https://v.douyin.com/iRNBho6/", "douyin"),
    ("https://m.weibo.cn/status/5000000000000000", "weibo"),
    ("https://x.com/user/status/1234567890123456789", "x"),
    ("https://music.163.com/song?id=1901371647", "netease"),
    ("https://www.zhihu.com/question/123456789", "zhihu"),
    ("https://tieba.baidu.com/p/1234567890", "tieba"),
    ("https://xhslink.com/abcdef", "rednote"),
    ("https://www.kuaishou.com/short-video/3xabcdef", "kuaishou"),
    ("https://www.acfun.cn/v/ac12345678", "acfun"),
    ("https://bbs.hupu.com/12345678.html", "hupu"),
    ("https://www.miyoushe.com/ys/article/12345678", "miyoushe"),
    ("https://www.coolapk.com/feed/12345678", "coolapk"),
    ("https://linux.do/t/topic/123456", "linuxdo"),
)


@pytest.fixture(autouse=True)
def _restore_state() -> Iterator[None]:
    saved_max_retries = StreamDownloader.MAX_RETRIES
    yield
    StreamDownloader.MAX_RETRIES = saved_max_retries
    pipeline.clear_result_cache()


# ---------------------------------------------------------------- 结果缓存链


def test_result_cache_evicts_oldest_beyond_max_size() -> None:
    cache = pipeline._RESULT_CACHE
    cache.clear()
    try:
        for index in range(51):  # 容量 50：写入 51 条触发逐出
            cache[f"k{index}"] = index
        assert len(cache) == 50
        assert "k0" not in cache  # 最旧被逐出
        assert cache["k1"] == 1
        assert cache["k50"] == 50  # 最新保留
    finally:
        cache.clear()


def test_limited_size_dict_update_keeps_insertion_order() -> None:
    """上游契约钉扎：更新已有键不移动位置（FIFO 而非 LRU）——
    反复命中的热门解析结果不会因刷新而免于逐出。"""
    d: LimitedSizeDict[str, int] = LimitedSizeDict(max_size=2)
    d["a"] = 1
    d["b"] = 2
    d["a"] = 3  # 更新已有键：位置不动
    d["c"] = 4  # 超容量：逐出插入序最前的 a（尽管它刚被更新过）
    assert "a" not in d
    assert d["b"] == 2
    assert d["c"] == 4


# ---------------------------------------------------------------- 派发链


async def test_multi_platform_consecutive_match_chain() -> None:
    """14 平台黄金 URL 连续 match：平台归属稳定，二次 match 幂等。"""
    parser = pipeline.Parser()
    for url, platform_name in GOLDEN_URLS:
        matched = parser.match(url)
        assert matched.parser_type.platform.name == PlatformEnum(platform_name), url
        again = parser.match(url)
        assert again.keyword == matched.keyword  # 关键词长度排序确定性
        assert again.searched.url == matched.searched.url
        assert again.searched.params == matched.searched.params


async def test_match_whole_text_prefers_longest_keyword() -> None:
    """单条消息含多个分享链接时：整段文本参与匹配，最长关键词的平台权威。"""
    parser = pipeline.Parser()
    text = "看这个 https://b23.tv/BV1xx411c7mD 还有 https://xhslink.com/abcdef"
    matched = parser.match(text)
    # xhslink.com(11) > b23.tv(6)：rednote 胜出
    assert matched.parser_type.platform.name == PlatformEnum.REDNOTE
    assert matched.keyword == "xhslink.com"


# ---------------------------------------------------------------- 下载失败注入链


class _FakeResponse:
    """UniResponse 鸭子替身：status_code/headers/raise_for_status/aiter_bytes"""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"data",),
        chunk_delay: float = 0.0,
    ):
        self.status_code = status_code
        self._headers = headers or {}
        self._chunks = chunks
        self._chunk_delay = chunk_delay

    @property
    def headers(self) -> dict[str, str | None]:
        return {k.lower(): v for k, v in self._headers.items()}

    def raise_for_status(self) -> None:
        if self.status_code >= 400 or self.status_code < 200:
            raise HTTPStatusError(f"HTTP {self.status_code}", response=self)

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            yield chunk


class _FakeStreamClient:
    """UniHttpClient.stream 鸭子替身：按 URL 脚本化响应/异常序列并记录请求。"""

    def __init__(self, script: dict[str, list[Any]] | None = None, default: Any = None) -> None:
        self.script = script or {}
        self.default = default
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    @contextlib.asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        cookies: dict[str, str] | None = None,
        use_curl_cffi: bool = False,
    ) -> AsyncIterator[Any]:
        self.calls.append((method, url, dict(headers)))
        queue = self.script.get(url)
        item = queue.pop(0) if queue else self.default
        if isinstance(item, Exception):
            raise item
        yield item


@contextlib.asynccontextmanager
async def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[float]]:
    """把重试退避 sleep 换成记录器，避免测试真实等待 2^n 秒。"""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    yield recorded


def _media_dir() -> Any:
    return CacheManager.cache_dir(CacheManager.MEDIA)


async def _make_partial(cache_key: str, data: bytes) -> Any:
    """预置断点文件（streamd 命名规则：{md5}.part）。"""
    partial = _media_dir() / f"{generate_file_name('http://x', cache_key)}.part"
    await partial.write_bytes(data)
    return partial


async def test_download_retries_rotate_fallback_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url_a = "http://8.8.8.8/rotate-a"
    url_b = "http://8.8.8.8/rotate-b"
    client = _FakeStreamClient(
        script={
            url_a: [RetryableDownloadError("线路 A 故障", keep_part=False)],
            url_b: [_FakeResponse(chunks=(b"video-", b"bytes"))],
        }
    )
    downloader = StreamDownloader()
    downloader.client = client
    StreamDownloader.MAX_RETRIES = 1

    async with _no_sleep(monkeypatch):
        path = await downloader.streamd(
            url=url_a,
            fallback_urls=[url_b],
            cache_key="wc-rotate",
        )

    assert await path.read_bytes() == b"video-bytes"
    # retry 0 → 首选线路 A；retry 1 → 轮换到 fallback B
    assert [call[1] for call in client.calls] == [url_a, url_b]


async def test_download_resumes_from_partial_then_hits_file_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_key = "wc-resume"
    url = "http://8.8.8.8/resume"
    await _make_partial(cache_key, b"AB")
    client = _FakeStreamClient(
        script={
            url: [
                _FakeResponse(
                    status_code=206,
                    headers={"content-range": "bytes 2-4/5", "content-length": "3"},
                    chunks=(b"cde",),
                )
            ]
        }
    )
    downloader = StreamDownloader()
    downloader.client = client

    path = await downloader.streamd(url=url, cache_key=cache_key, default_suffix=".bin")
    assert path.suffix == ".bin"
    assert await path.read_bytes() == b"ABcde"  # ab 追加模式接续断点
    assert client.calls[0][2]["Range"] == "bytes=2-"

    # 二次下载同资源：.meta 文件缓存命中，零网络请求
    again = await downloader.streamd(url=url, cache_key=cache_key, default_suffix=".bin")
    assert again == path
    assert len(client.calls) == 1


async def test_download_416_restarts_from_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_key = "wc-416"
    url = "http://8.8.8.8/restart"
    await _make_partial(cache_key, b"AB")
    client = _FakeStreamClient(
        script={
            url: [
                _FakeResponse(status_code=416),
                _FakeResponse(headers={"content-length": "9"}, chunks=(b"full-body",)),
            ]
        }
    )
    downloader = StreamDownloader()
    downloader.client = client
    StreamDownloader.MAX_RETRIES = 1

    async with _no_sleep(monkeypatch) as sleeps:
        path = await downloader.streamd(url=url, cache_key=cache_key)

    assert await path.read_bytes() == b"full-body"  # 丢弃断点后完整重下
    assert sleeps == [1.0]  # 退避 2^0
    # 首跳带 Range（断点 2），416 后重下不带 Range
    assert client.calls[0][2].get("Range") == "bytes=2-"
    assert "Range" not in client.calls[1][2]


async def test_download_size_limit_terminates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "http://8.8.8.8/too-big"
    client = _FakeStreamClient(
        default=_FakeResponse(headers={"content-length": str(91 * 1024 * 1024)})
    )
    downloader = StreamDownloader()
    downloader.client = client
    StreamDownloader.MAX_RETRIES = 3
    partial = _media_dir() / f"{generate_file_name('http://x', 'wc-toolarge')}.part"

    with pytest.raises(SizeLimitException):
        await downloader.streamd(url=url, cache_key="wc-toolarge")

    assert len(client.calls) == 1  # 超限立即终止，不进重试循环
    assert not await partial.exists()  # partial 被清理（size/zero 分支语义）


async def test_download_zero_size_terminates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "http://8.8.8.8/empty"
    client = _FakeStreamClient(default=_FakeResponse(headers={"content-length": "0"}))
    downloader = StreamDownloader()
    downloader.client = client
    StreamDownloader.MAX_RETRIES = 3

    with pytest.raises(ZeroSizeException):
        await downloader.streamd(url=url, cache_key="wc-zero")

    assert len(client.calls) == 1


async def test_concurrent_same_file_downloads_share_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 base_path 的并发下载只发一次网络请求（_active_downloads 单飞）。"""
    url = "http://8.8.8.8/slow"
    client = _FakeStreamClient(
        default=_FakeResponse(
            headers={"content-length": "6"},
            chunks=(b"ab", b"cd", b"ef"),
            chunk_delay=0.05,
        )
    )
    downloader = StreamDownloader()
    downloader.client = client

    async with _no_sleep(monkeypatch):
        first = downloader.streamd(url=url, cache_key="wc-single")
        second = downloader.streamd(url=url, cache_key="wc-single")
        path_a, path_b = await asyncio.gather(first, second)

    assert path_a == path_b
    assert len(client.calls) == 1


# ---------------------------------------------------------------- 媒体内容链


def _content(url: str, **kwargs: Any) -> VideoContent | ImageContent:
    wrapper = DownloadTaskWrapper(func=_fake_download, args=(), kwargs={}, url=url)
    cls = VideoContent if "cover" in kwargs or kwargs.get("kind") == "video" else ImageContent
    kwargs.pop("kind", None)
    return cls(path_task=wrapper, **kwargs)


_DUMMY_FILE = Path("/tmp/parser-lite-dummy")  # 仅验链路优先级，不真实读写


async def _fake_download() -> Any:
    return _DUMMY_FILE


async def test_display_size_caches_head_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sender 阈值分流的依赖契约：_size_bytes 只探一次 HEAD。"""
    calls = 0

    async def fake_head_size(**kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        return 2048

    monkeypatch.setattr(DOWNLOADER, "head_size", fake_head_size)
    cont = _content("http://8.8.8.8/v", kind="video")
    first = await cont.get_display_size()
    second = await cont.get_display_size()
    assert calls == 1
    assert first == second == "2.00KB"


async def test_display_size_head_failure_degrades_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD 探测失败不抛出（不影响下载主流程），显示未知大小。"""

    async def boom(**kwargs: Any) -> int:
        raise RuntimeError("HEAD 探测失败")

    monkeypatch.setattr(DOWNLOADER, "head_size", boom)
    cont = _content("http://8.8.8.8/v", kind="video")
    assert await cont.get_display_size() == "未知大小"


async def test_parse_result_cover_priority() -> None:
    """渲染封面优先级链：视频封面 > 图集首图 > 无内容返回 None。"""
    platform = Platform(name=PlatformEnum.BILIBILI, display_name="哔哩哔哩")
    cover_task = DownloadTaskWrapper(
        func=_fake_download, args=(), kwargs={}, url="http://8.8.8.8/cover"
    )
    image_task = DownloadTaskWrapper(
        func=_fake_download, args=(), kwargs={}, url="http://8.8.8.8/img"
    )
    video_first = ParseResult(
        platform=platform,
        author=None,
        url="x",
        content=[
            VideoContent(path_task=image_task, cover=cover_task),
            ImageContent(path_task=image_task),
        ],
    )
    assert await video_first.get_cover_path() == _DUMMY_FILE  # 视频封面胜出

    images_only = ParseResult(
        platform=platform,
        author=None,
        url="x",
        content=[ImageContent(path_task=image_task), "文本"],
    )
    assert await images_only.get_cover_path() == _DUMMY_FILE  # 回落到图集首图

    text_only = ParseResult(platform=platform, author=None, url="x", content=["纯文本"])
    assert await text_only.get_cover_path() is None


# ---------------------------------------------------------------- 调度链


async def test_periodic_scheduler_add_job_idempotent() -> None:
    """重复 parse 反复调用 _ensure_runtime_started 时不得堆叠清理任务。"""
    scheduler = PeriodicScheduler()

    async def noop() -> None:
        return None

    try:
        scheduler.add_job(noop, seconds=3600, id="job")
        first_task = scheduler._jobs["job"].task
        scheduler.add_job(noop, seconds=3600, id="job")  # 未结束 → 保持原任务
        assert scheduler._jobs["job"].task is first_task
        assert scheduler.job_ids == ("job",)
    finally:
        await scheduler.shutdown()
    assert first_task.cancelled()


# ---------------------------------------------------------------- SSRF 逐跳链


async def test_redirect_to_loopback_blocked_per_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """follow_redirects 客户端每跳重进 _PinnedTransport：302 跳内网被拦，
    内网目标绝不触达传输层。"""
    inner_targets: list[str] = []

    async def fake_upstream(self: Any, request: httpx.Request) -> httpx.Response:
        inner_targets.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/x"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    client = httpx.AsyncClient(follow_redirects=True, transport=ssrf._PinnedTransport())
    try:
        with pytest.raises(ssrf.UrlBlockedError):
            await client.get("http://8.8.8.8/x")  # 首跳公网 IP 合法
    finally:
        await client.aclose()
    assert inner_targets == ["http://8.8.8.8/x"]  # 第二跳在校验层被拒


async def test_parser_client_get_allowed_ip_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """parser 客户端端到端：合法公网 IP 请求完整走通守卫直达传输层。"""

    async def fake_upstream(self: Any, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_upstream)
    ssrf.install_ssrf_guard()  # 幂等
    parser = BilibiliParser()
    try:
        resp = await parser.httpx.get("http://8.8.8.8/x")
        assert resp.status_code == 200
        assert resp.text == "ok"
    finally:
        await parser.aclose()


async def test_parser_client_guard_not_double_wrapped() -> None:
    """插件重载链：install_ssrf_guard 二次调用不叠加 __init__ 包裹层。"""
    ssrf.install_ssrf_guard()
    wrapped_once = BaseParser.__init__
    ssrf.install_ssrf_guard()
    assert BaseParser.__init__ is wrapped_once
    inner = getattr(wrapped_once, "__wrapped__", None)
    assert inner is not None  # 恰好一层 functools.wraps
    assert getattr(inner, "__wrapped__", None) is None  # 无第二层包裹
    # DOWNLOADER 全局实例未被测试破坏（供其它测试/会话继续使用）
    assert isinstance(DOWNLOADER, StreamDownloader)
