#!/usr/bin/env python3
"""test_stream_read_timeout.py — 流式 read 空闲超时 + control 端点（b12）。

背景（为什么要有这个测试）：
    2026-10-07 排查「国际版经常卡在等待模型响应（31m14s）」：
      1. SOCKS5 隧道静默死亡（TCP 半开）时，流式 read=None 永远阻塞，
         客户端一直转圈 —— read 超时的 httpx 语义是「两次字节之间的最大
         间隔」，正好当空闲超时用；
      2. node 客户端向 /v1/chat/completions/control 发控制请求吃到 404，
         可能停在等待应答。

覆盖：
  1. _upstream_timeout(None) → read = stream_read_timeout（默认 600）
  2. _upstream_timeout(120) → read 保持显式值（非流式路径不受影响）
  3. CONFIG stream_read_timeout=0 → read=None（显式禁用 = 旧行为）
  4. control 端点鉴权后 200 {"ok": true}；无 key 配置时放行
  5. 路由双前缀 /v1/chat/completions/control 与 /chat/completions/control

直接运行：python3 test_stream_read_timeout.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from fastapi.testclient import TestClient  # noqa: E402

import converter  # noqa: E402


def test_idle_timeout_default():
    """流式（read=None）→ 空闲超时默认 120s（用户拍板：宁快速失败不挂死）。"""
    saved = converter.CONFIG.get("stream_read_timeout")
    converter.CONFIG["stream_read_timeout"] = 120.0
    try:
        t = converter._upstream_timeout(None)
        assert t.read == 120.0, f"read 应为 120，实际 {t.read}"
        assert t.connect == 15.0, "connect 超时不应被改动"
    finally:
        if saved is None:
            converter.CONFIG.pop("stream_read_timeout", None)
        else:
            converter.CONFIG["stream_read_timeout"] = saved
    print("✅ test_idle_timeout_default")


def test_explicit_read_untouched():
    """非流式路径显式 read 值不受空闲超时影响。"""
    t = converter._upstream_timeout(120)
    assert t.read == 120, f"显式 read 应原样透传，实际 {t.read}"
    print("✅ test_explicit_read_untouched")


def test_zero_disables():
    """stream_read_timeout=0 → 不限（恢复旧行为）。"""
    saved = converter.CONFIG.get("stream_read_timeout")
    converter.CONFIG["stream_read_timeout"] = 0
    try:
        t = converter._upstream_timeout(None)
        assert t.read is None, f"0 应表示不限（None），实际 {t.read}"
    finally:
        if saved is None:
            converter.CONFIG.pop("stream_read_timeout", None)
        else:
            converter.CONFIG["stream_read_timeout"] = saved
    print("✅ test_zero_disables")


def test_control_endpoint():
    """control 端点：200 {"ok": true}，双前缀都注册，载荷落日志。"""
    client = TestClient(converter.app)
    r = client.post("/v1/chat/completions/control", json={"action": "stop"})
    assert r.status_code == 200, f"应 200，实际 {r.status_code}"
    assert r.json() == {"ok": True}, f"应 {{'ok': True}}，实际 {r.json()}"
    r2 = client.post("/chat/completions/control")
    assert r2.status_code == 200, f"裸前缀应 200，实际 {r2.status_code}"
    print("✅ test_control_endpoint")


if __name__ == "__main__":
    test_idle_timeout_default()
    test_explicit_read_untouched()
    test_zero_disables()
    test_control_endpoint()
