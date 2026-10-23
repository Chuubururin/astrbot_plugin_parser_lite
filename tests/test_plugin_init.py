"""插件启动生命周期门禁：安全面失败必须拒启（fail-closed）。

若 SSRF 守卫安装失败只记 error 日志、插件继续无保护出站，即与
``bridge/ssrf.py`` 声明的 fail-closed 姿态矛盾。本模块钉住两条语义：

- 守卫安装失败 → ``__init__`` 抛出（AstrBot 显示初始化失败，不注册处理），
  安装函数本身可重入，修好后重载即恢复；
- vendor 补丁挂载失败 → 仍按降级启动（正确性补丁失败不值得全域拒启，
  不对称是刻意的），error 日志可见。

手法与 test_on_message_flow 同源：``__new__`` 构造实例后显式调 ``__init__``，
协作者全部 monkeypatch，只测启动编排的失败语义。
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("astrbot")

from astrbot_plugin_parser_lite import main as main_mod


class _Boom(Exception):
    pass


def _init_plugin(monkeypatch: pytest.MonkeyPatch, **over: Any) -> Any:
    """在全部协作者被替身隔离的状态下执行一次 ParserLitePlugin.__init__。"""
    plugin = main_mod.ParserLitePlugin.__new__(main_mod.ParserLitePlugin)
    monkeypatch.setattr(main_mod, "rearm_runtime", lambda: None)
    monkeypatch.setattr(main_mod, "configure", lambda **_kw: None)
    monkeypatch.setattr(main_mod, "sync_import_time_config", lambda: None)
    monkeypatch.setattr(main_mod, "Parser", lambda *a, **k: object())
    monkeypatch.setattr(main_mod, "set_video_file_threshold_mb", lambda _mb: 100)
    monkeypatch.setattr(main_mod, "apply_vendor_patches", lambda: None)
    for name, value in over.items():
        monkeypatch.setattr(main_mod, name, value)
    main_mod.ParserLitePlugin.__init__(plugin, None, {"plite_render": False})
    return plugin


def test_init_refuses_startup_when_ssrf_guard_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """守卫装不上 = 出站全裸，禁止静默启动：异常必须原样上抛。"""

    def _fail_install() -> None:
        raise _Boom("seam 漂移")

    with pytest.raises(_Boom):
        _init_plugin(monkeypatch, install_ssrf_guard=_fail_install)


def test_init_degrades_when_vendor_patches_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """补丁挂载失败仅记录：正确性缺陷不升级为全域拒启（刻意不对称）。"""

    def _fail_patches() -> None:
        raise _Boom("补丁 seam 漂移")

    plugin = _init_plugin(
        monkeypatch,
        install_ssrf_guard=lambda: None,
        apply_vendor_patches=_fail_patches,
    )
    assert plugin is not None, "vendor 补丁失败不应阻断启动"


def test_config_error_summary_never_carries_input_values() -> None:
    """配置异常摘要只给类型名与字段名。

    pydantic 的 ``errors()`` 每项同时带 ``loc`` 与 ``input``，``%r`` 会把 input
    一并 repr 出来——而 vendor 配置面携带 linuxdo/zhihu cookie，凭据不得落日志。
    """

    class _ValidationLike(Exception):
        def errors(self) -> list[dict[str, Any]]:
            return [
                {
                    "loc": ("plite_zhihu_ck", 3),
                    "input": "SECRET-COOKIE-VALUE",
                    "msg": "字段过长",
                }
            ]

    summary = main_mod._config_error_summary(_ValidationLike("SECRET-COOKIE-VALUE"))
    assert "SECRET-COOKIE-VALUE" not in summary, f"输入值进了摘要：{summary}"
    assert "plite_zhihu_ck" in summary, f"摘要丢了出错字段名：{summary}"
    assert main_mod._config_error_summary(ValueError("SECRET-COOKIE-VALUE")) == "ValueError"
