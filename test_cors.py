#!/usr/bin/env python3
"""test_cors.py — 验证浏览器直连场景的 CORS 预检处理（b11）。

背景（为什么要有这个测试）：
    浏览器网页直连本服务时，跨域请求前会先发 OPTIONS 预检。b10 及之前
    服务端没有 CORS 中间件，预检落不到路由 → 405，浏览器随即把真正的
    POST 拦下，表现为「请求根本没发出去」（2026-10-07 cn 实例实测）。
    服务端程序（New API / Codex）不发预检，所以此前只在浏览器场景现形。

覆盖：
  1. OPTIONS 预检 → 204 + Access-Control-Allow-Origin（修复前是 405）
  2. 普通响应带 Origin 时附带 CORS 头（浏览器才接受后续响应）
  3. --cors-origins 逗号列表 / off / * 三种 spec 的重挂行为
  4. CORS 项挂在访问日志中间件内侧（顺序契约，防回归）

直接运行：python3 test_cors.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import converter  # noqa: E402


def test_preflight_options_returns_204():
    """浏览器预检：OPTIONS + Origin + Access-Control-Request-* → 204（修复前 405）。"""
    client = TestClient(converter.app)
    r = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert 200 <= r.status_code < 300, f"预检应 2xx，实际 {r.status_code}"  # starlette 版本不同返回 200 或 204，均合规
    assert r.headers.get("access-control-allow-origin") is not None, "缺 allow-origin 头"
    assert "POST" in (r.headers.get("access-control-allow-methods") or ""), "缺 allow-methods"
    print("✅ test_preflight_options_returns_204")


def test_plain_response_carries_cors_header():
    """普通带 Origin 请求的响应也要带 allow-origin 头（浏览器才收）。"""
    client = TestClient(converter.app)
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200, f"/health 应 200，实际 {r.status_code}"
    assert r.headers.get("access-control-allow-origin") is not None, "普通响应缺 CORS 头"
    print("✅ test_plain_response_carries_cors_header")


def _cors_entry():
    """从 user_middleware 里找 CORS 项；找不到返回 None。"""
    for m in converter.app.user_middleware:
        if m.cls is CORSMiddleware:
            return m
    return None


def test_apply_cors_config_specs():
    """三种 spec：逗号列表收窄 / off 摘除 / * 恢复全放行。"""
    saved = list(converter.app.user_middleware)
    try:
        # 逗号列表
        converter._apply_cors_config("https://a.example, https://b.example")
        m = _cors_entry()
        assert m is not None, "列表 spec 应重挂 CORS"
        assert m.kwargs["allow_origins"] == ["https://a.example", "https://b.example"]

        # off = 摘除
        converter._apply_cors_config("off")
        assert _cors_entry() is None, "off 应移除 CORS 中间件"
        converter._apply_cors_config("")  # 空串等价 off
        assert _cors_entry() is None

        # * = 恢复全放行
        converter._apply_cors_config("*")
        m = _cors_entry()
        assert m is not None and m.kwargs["allow_origins"] == ["*"]
    finally:
        converter.app.user_middleware = saved
    print("✅ test_apply_cors_config_specs")


def test_cors_sits_inside_access_log():
    """顺序契约：CORS 在访问日志内侧（实测本 starlette 的 add_middleware 是
    insert(0)，先注册者更内层）。

    行为效果：预检 OPTIONS 由 CORS 直接短路（不再 405），访问日志在外层
    仍能看到（默认静默，log_requests 开启时以 2xx 可见）。
    """
    names = [m.cls.__name__ for m in converter.app.user_middleware]
    assert "CORSMiddleware" in names, f"缺 CORS 中间件: {names}"
    assert "BaseHTTPMiddleware" in names, f"缺访问日志中间件: {names}"
    assert names.index("BaseHTTPMiddleware") < names.index("CORSMiddleware"), (
        f"CORS 应在访问日志内侧（紧贴路由）: {names}"
    )
    print("✅ test_cors_sits_inside_access_log")


if __name__ == "__main__":
    test_preflight_options_returns_204()
    test_plain_response_carries_cors_header()
    test_apply_cors_config_specs()
    test_cors_sits_inside_access_log()
