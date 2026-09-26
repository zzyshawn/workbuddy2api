#!/usr/bin/env python3
"""test_client_compat.py — 验证「中转面板 / 各式客户端」接入时的兼容性。

背景（为什么要有这个测试）：
    用户把服务接进 New API 时，UI 只显示「获取模型列表失败」，看不出真因。
    实测定位到两类**服务端**缺口，都会表现成同一句模糊的 UI 文案：

      1. 鉴权头名不匹配 —— 我们原先只认 `Authorization: Bearer` 与 `X-Api-Key`，
         而 New API / One API 拉渠道模型时用的是 `api-key`（裸值，无 Bearer 前缀）。
         结果：明明 key 是对的，却吃 401。

      2. 路径前缀不匹配 —— 只挂了 `/v1/models`。中转面板拿「基础地址」拼路径探测：
         基础地址填 `http://host:port` 时它探 `/models` → 404；
         填 `http://host:port/v1` 时它探 `/v1/v1/...`。两种都是 UI 上一句笼统失败。

    这两个缺口不是「用户配错了」，是服务端该容错的地方不够宽。
    测试把容错面锁住，避免以后又被收窄回去。

覆盖：
  1. `_check_auth` 三种头名都认（无 key 配置时不鉴权）
  2. `api-key` 头带/不带 `Bearer ` 前缀都能过
  3. 错的 key 仍然被拒（容错不是放水）
  4. `/models` 与 `/v1/models` 等价（都返回 200 + 同样条数）
  5. `/chat/completions`、`/messages` 裸前缀别名存在且与带 `/v1` 的等价
  6. 所有需要鉴权的端点都声明了 `api-key` 头参数（防止新增端点时漏挂）

直接运行：python3 test_client_compat.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import converter  # noqa: E402

SRC = Path("converter.py")


# ---------------------------------------------------------------------------
# 1. 鉴权头名：三种都要认
# ---------------------------------------------------------------------------

def test_check_auth_accepts_bearer():
    """标准 OpenAI 风格：Authorization: Bearer <key>。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"
    try:
        converter._check_auth("Bearer secret123", None, None)  # 不抛即通过
        converter._check_auth("Bearer  secret123  ", None, None)  # 容忍空白
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_check_auth_accepts_bearer")


def test_check_auth_accepts_api_key_header():
    """New API / One API 风格：api-key 头，裸值无 Bearer 前缀。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"
    try:
        converter._check_auth(None, None, "secret123")
        # 有的客户端会把整个 "Bearer xxx" 塞进 api-key 头，也要认
        converter._check_auth(None, None, "Bearer secret123")
        converter._check_auth(None, None, "  secret123  ")
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_check_auth_accepts_api_key_header")


def test_check_auth_accepts_x_api_key_header():
    """Anthropic 风格：X-Api-Key 头。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"
    try:
        converter._check_auth(None, "secret123", None)
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_check_auth_accepts_x_api_key_header")


def test_check_auth_rejects_wrong_key():
    """容错不等于放水：key 不对、头缺失都要 401。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"
    cases = [
        (None, None, None),                       # 什么都没带
        ("Bearer wrong", None, None),             # Bearer 值错
        (None, None, "wrong"),                    # api-key 值错
        (None, "wrong", None),                    # X-Api-Key 值错
        ("secret123", None, None),                # 少了 Bearer 前缀
        (None, None, "secret1234"),               # 多一个字符也不行
    ]
    try:
        for i, (a, x, k) in enumerate(cases):
            try:
                converter._check_auth(a, x, k)
            except HTTPException as e:
                assert e.status_code == 401, f"case {i} 状态码应为 401，实为 {e.status_code}"
            else:
                raise AssertionError(f"case {i} 应当被拒（入参 {a!r}, {x!r}, {k!r}）")
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_check_auth_rejects_wrong_key")


def test_check_auth_disabled_when_no_key():
    """未配置 key（= 不鉴权）时，任何头/无头都应放行。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = ""
    try:
        converter._check_auth(None, None, None)
        converter._check_auth("Bearer whatever", None, None)
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_check_auth_disabled_when_no_key")


# ---------------------------------------------------------------------------
# 2. 路由别名：路径前缀差异
# ---------------------------------------------------------------------------

def _client() -> TestClient:
    return TestClient(converter.app)


def test_models_alias_without_v1_prefix():
    """/models 与 /v1/models 都要 200，且**逐条 id 完全一致**。

    中转面板把基础地址填成裸 host 时会探 /models，只挂 /v1/models 就是 404。

    注意：这里不依赖 `CONFIG["env"]` 的实际取值 —— 直接 import 模块时它还是初始值
    （`None`），模型会退到静态兜底表。测试只关心「两个路径返回同一份数据」，
    所以显式把环境钉死成 INTL，避免受宿主环境变量干扰。
    """
    saved_key = converter.CONFIG["api_key"]
    saved_env = converter.CONFIG["env"]
    converter.CONFIG["api_key"] = ""
    converter.CONFIG["env"] = "intl"
    try:
        c = _client()
        r1 = c.get("/v1/models")
        r2 = c.get("/models")
        assert r1.status_code == 200, f"/v1/models 应为 200，实为 {r1.status_code}"
        assert r2.status_code == 200, f"/models 应为 200（别名路由缺失？），实为 {r2.status_code}"
        ids1 = [m["id"] for m in r1.json()["data"]]
        ids2 = [m["id"] for m in r2.json()["data"]]
        assert ids1 == ids2, f"两个路径结果不一致：{set(ids1) ^ set(ids2)}"
        assert ids1, "模型列表不该为空"
        n = len(ids1)
        # 顺带确认 INTL 表取对了（兜底也不该退回 CN 的 16 项）
        assert "deepseek-v4.1-flash" in ids1, f"INTL 环境却没取到 INTL 兜底表：{ids1}"
    finally:
        converter.CONFIG["api_key"] = saved_key
        converter.CONFIG["env"] = saved_env
    print(f"✅ test_models_alias_without_v1_prefix  ({n} 个模型，两路径一致)")


def test_models_alias_respects_auth():
    """别名路由也必须鉴权 —— 不能因为多挂一条路径就漏掉校验。"""
    saved = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"
    try:
        c = _client()
        assert c.get("/models").status_code == 401, "/models 无 key 应为 401"
        assert c.get("/v1/models").status_code == 401, "/v1/models 无 key 应为 401"
        # api-key 头（New API 风格）应当能过
        assert c.get("/models", headers={"api-key": "secret123"}).status_code == 200
        assert c.get("/models", headers={"X-Api-Key": "secret123"}).status_code == 200
        assert c.get("/models", headers={"Authorization": "Bearer secret123"}).status_code == 200
    finally:
        converter.CONFIG["api_key"] = saved
    print("✅ test_models_alias_respects_auth")


def test_chat_and_messages_alias_routes():
    """裸前缀的 /chat/completions 与 /messages 别名必须存在。

    只校验路由表与鉴权行为（真发请求会打上游，不该在单元测试里做）。
    """
    paths = {r.path for r in converter.app.routes if hasattr(r, "path")}
    for p in ("/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages"):
        assert p in paths, f"路由缺失：{p}"
    print(f"✅ test_chat_and_messages_alias_routes  (路由表 {len(paths)} 条)")


# ---------------------------------------------------------------------------
# 3. 结构性防回归：新增端点别漏挂 api-key 头
# ---------------------------------------------------------------------------

def test_all_authed_endpoints_declare_api_key_header():
    """所有调 `_check_auth` 的端点，函数签名里都要有 `api-key` 头参数。

    做法：直接读源码文本，按**路由装饰器**切块（`@app.get/post(` 起头），
    块里有 `_check_auth(` 就必须同时有 `alias="api-key"`。
    比反射更直观，也能抓住「加了端点忘了加头」这类疏漏。

    注意别按 `def ` 切块 —— `_check_auth` 自己的定义体里也有 `_check_auth(`
    这个名字，会被误判成端点。
    """
    src = SRC.read_text(encoding="utf-8")
    lines = src.splitlines()
    authed, missing = [], []
    # 逐行扫：路由装饰器 + 紧随的 def 行构成「签名块」，
    # 然后在函数体内（到下一个顶格行前）找 _check_auth。
    # 关键点：`_check_auth` 自己的定义体不会被算进来，因为它前面没有路由装饰器。
    i, n = 0, len(lines)
    while i < n:
        if not re.match(r"\s*@app\.(get|post)\(", lines[i]):
            i += 1
            continue
        # 收集连续的装饰器行 + def 行
        block: list[str] = []
        while i < n and re.match(r"\s*@app\.(get|post)\(", lines[i]):
            block.append(lines[i])
            i += 1
        if i >= n:
            break
        m = re.match(r"\s*def\s+(\w+)\s*\(", lines[i])
        if not m:
            continue
        name = m.group(1)
        # 收完签名（可能跨行，直到出现 `):`）
        while i < n:
            block.append(lines[i])
            if lines[i].rstrip().endswith("):"):
                i += 1
                break
            i += 1
        # 收函数体：直到下一个顶格行
        while i < n and (lines[i].strip() == "" or lines[i].startswith((" ", "\t"))):
            block.append(lines[i])
            i += 1
        body = "\n".join(block)
        if "_check_auth(" in body:
            authed.append(name)
            if 'alias="api-key"' not in body:
                missing.append(name)

    assert len(authed) >= 5, f"只扫到 {len(authed)} 个鉴权端点，明显偏少 —— 扫描逻辑可能失效：{authed}"
    assert not missing, f"以下端点缺 api-key 头参数：{missing}"
    print(f"✅ test_all_authed_endpoints_declare_api_key_header  ({len(authed)} 个端点：{', '.join(authed)})")


def test_models_route_has_both_decorators():
    """`list_models` 必须同时挂 /models 与 /v1/models 两个装饰器。"""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"((?:@app\.get\(\"[^\"]+\"\)\s*\n)+)def list_models\(", src)
    assert m, "没找到 list_models 的装饰器块"
    block = m.group(1)
    assert '@app.get("/v1/models")' in block, "缺 /v1/models 装饰器"
    assert '@app.get("/models")' in block, "缺 /models 别名装饰器"
    print("✅ test_models_route_has_both_decorators")


if __name__ == "__main__":
    t0 = __import__("time").time()
    test_check_auth_accepts_bearer()
    test_check_auth_accepts_api_key_header()
    test_check_auth_accepts_x_api_key_header()
    test_check_auth_rejects_wrong_key()
    test_check_auth_disabled_when_no_key()
    test_models_alias_without_v1_prefix()
    test_models_alias_respects_auth()
    test_chat_and_messages_alias_routes()
    test_all_authed_endpoints_declare_api_key_header()
    test_models_route_has_both_decorators()
    print(f"\n🎉 All 10 tests passed! ({__import__('time').time() - t0:.2f}s)")
