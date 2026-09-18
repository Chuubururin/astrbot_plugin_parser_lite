"""行为轨（隔离区）：真实网络 parse 快照，默认跳过（工单 09 ④）。

启用：RUN_NETWORK_TESTS=1 python3 -m pytest -c config/pyproject.toml --rootdir=.
      tests/test_parse_snapshot.py
快照 = stable_view 剥离易变字段（签名 URL、播放计数、时间戳）后的规范化
视图；CI 非必需检查，diff 须人工评审后 --snapshot-update 重建。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.getenv("RUN_NETWORK_TESTS") != "1",
        reason="需要真实外网；设 RUN_NETWORK_TESTS=1 启用（隔离区）",
    ),
]


def _resource_id(obj: Any) -> str:
    """取媒体对象的资源标识：scheme+host+path（剥掉每请求必变的签名 query）。"""
    return str(getattr(obj, "url", obj) or "").split("?", 1)[0]


def _stable_content(items: Any) -> list[dict[str, Any]]:
    """媒体项规范化投影：只保留稳定身份/行为字段。

    签名 URL（path_task/cover 的 query）含 deadline/upsig/trid 等每请求必变的
    授权参数，仅保留 scheme+host+path 资源标识；媒体文件大小保留——它若漂移
    是值得人工评审的真实行为变化。用字典而非 vendor 对象转储，快照不随上游
    repr 格式漂移。
    """
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, str):
            normalized.append({"kind": "str", "text": item})
            continue
        normalized.append(
            {
                "kind": type(item).__name__,
                "duration": getattr(item, "duration", None),
                "need_send": getattr(item, "need_send", None),
                "size_bytes": getattr(item, "_size_bytes", None),
                "path_task": _resource_id(getattr(item, "path_task", None)),
                "cover": _resource_id(getattr(item, "cover", None)),
            }
        )
    return normalized


def stable_view(result) -> dict[str, Any]:
    author = getattr(result, "author", "")
    author_name = author if isinstance(author, str) else getattr(author, "name", "")
    platform = getattr(result, "platform", "")
    platform_name = getattr(platform, "name", "")
    return {
        "platform": str(getattr(platform_name, "value", platform_name) or ""),
        "title": (getattr(result, "title", "") or "")[:120],
        "author": str(author_name or ""),
        "content": _stable_content((getattr(result, "content", "") or "")[:200]),
    }


PARSE_SAMPLES = (pytest.param("https://www.bilibili.com/video/av170001", id="bilibili-av"),)


@pytest.mark.parametrize("url", PARSE_SAMPLES)
async def test_parse_snapshot(url: str, snapshot):
    """真实网络 parse 快照（隔离区）：样本 = 一条稳定平台链接。

    扩平台/扩样本 = 在 PARSE_SAMPLES 加一行，先在启用网络的一次运行中以
    ``RUN_NETWORK_TESTS=1 pytest -c config/pyproject.toml --rootdir=.
    tests/test_parse_snapshot.py --snapshot-update``
    生成快照，diff 人工评审后入库（禁止失败即重建）。
    """
    from astrbot_plugin_parser_lite.vendor.nonebot_plugin_parser_lite import Parser

    result = await Parser().parse(url)
    assert stable_view(result) == snapshot
