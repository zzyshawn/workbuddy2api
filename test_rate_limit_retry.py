#!/usr/bin/env python3
"""test_rate_limit_retry.py — 429 限流「换号优先 + 整圈退避」（改进 A，b15）。

背景（为什么要有这个测试）：
    2026-10-13 日志：intl 当天 5 次 429（code 14003 too many requests）全部原样
    透传给客户端 —— 旧策略把 429 当「确定性拒绝」不重试，但上游明明给了
    retry_policy.retry=true。用户拍板的新策略：
      1. 429 **优先换下一个账号**（不禁号 —— 号本身是好的，只是打太快）；
      2. 池里整圈全 429 才退避 rate_limit_retry_gap 秒再来一轮；
      3. 至多 rate_limit_retry 轮，用尽才把 429 透传给客户端。
    429 绝不能走 mark_account_fault —— 那会永久禁用一个好号（11140 才是封禁）。

覆盖：
  1. _rate_limit_policy() 默认值（2 轮 / 20s）
  2. classify_account_fault 对 429/14003 返回 None（不是账号级故障）
  3. _with_account_rotation：A 号 429 → 换 B 号成功（不禁号）
  4. _with_account_rotation：整圈全 429 → 退避后第二轮成功
  5. _with_account_rotation：全 429 且轮次用尽 → HTTPException 429（不误报 503）
  6. _with_account_rotation：429 + 账号故障混合 → 故障优先，不做限流退避
  7. _post_backend_with_filter_retry：429 换号成功；全 429 用尽 → 429
  8. _stream_upstream：A 号 429 → B 号 200 正常出流（真 httpx MockTransport）
  9. _stream_upstream：全 429 → 退避轮用尽 → error 事件（rate_limited）

直接运行：python3 test_rate_limit_retry.py
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

import httpx  # noqa: E402

import converter  # noqa: E402
import environments  # noqa: E402
from account_pool import AccountFaultError, RateLimitedError  # noqa: E402

# 测试用退避参数：轮数 2、间隔 10ms（真实默认 20s，测试不能真等）。
RL_RETRIES = 2
RL_GAP = 0.01


class FakeAcct:
    """池条目的最小替身：_with_account_rotation / _account_headers 只碰这些。"""

    def __init__(self, uid: str, token: str = "tok"):
        self.uid = uid
        self.proxy = None
        self.cred = SimpleNamespace(
            get_headers=lambda uid=uid, token=token: {
                "Authorization": f"Bearer {token}-{uid}",
                "X-User-Id": uid,
                "X-Domain": "www.codebuddy.ai",
                "User-Agent": "CLI/2.158.0 CodeBuddy/2.158.0",
            }
        )


class FakePool:
    """AccountPool 的最小替身：只实现换号循环用到的方法。"""

    def __init__(self, accts):
        self.accts = list(accts)
        self.faults: list[str] = []

    def ordered_available(self):
        return [a for a in self.accts if a.uid not in self.faults]

    def note_attempt(self, uid):
        pass

    def mark_account_fault(self, uid, reason, http_status=None):
        self.faults.append(uid)

    def has_accounts(self):
        return bool(self.accts)


def _save_rl_config():
    return (
        converter.CONFIG.get("rate_limit_retry"),
        converter.CONFIG.get("rate_limit_retry_gap"),
    )


def _restore_rl_config(saved):
    converter.CONFIG["rate_limit_retry"] = saved[0]
    converter.CONFIG["rate_limit_retry_gap"] = saved[1]


def test_policy_defaults():
    """_rate_limit_policy() 默认 2 轮 / 20s；0 = 关闭退避轮。"""
    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = 2
        converter.CONFIG["rate_limit_retry_gap"] = 20.0
        n, g = converter._rate_limit_policy()
        assert (n, g) == (2, 20.0), f"默认应为 (2, 20.0)，实际 {(n, g)}"
        converter.CONFIG["rate_limit_retry"] = 0
        n, _ = converter._rate_limit_policy()
        assert n == 0, "0 应表示关闭退避轮"
    finally:
        _restore_rl_config(saved)
    print("✅ test_policy_defaults")


def test_429_is_not_account_fault():
    """429 / 14003 body 不算账号级故障（绝不能把好号禁用）。"""
    assert converter.account_pool.classify_account_fault(
        429, b'{"code":14003,"msg":"too many requests"}'
    ) is None, "429/14003 不是账号级故障"
    assert converter.account_pool.classify_account_fault(
        429, b"anything else"
    ) is None
    # 反向确认：request illegal 仍然算（防回归）。
    assert converter.account_pool.classify_account_fault(
        403, b'{"code":11140,"msg":"request illegal"}'
    ) is not None
    print("✅ test_429_is_not_account_fault")


def test_rotation_switches_account_on_429():
    """A 号 429 → 同一请求内换 B 号成功；A 号不被禁用。"""
    pool = FakePool([FakeAcct("a"), FakeAcct("b")])
    calls: list[str] = []

    async def attempt(acct):
        calls.append(acct.uid)
        if acct.uid == "a":
            raise RateLimitedError("a", 429, b"too many")
        return f"ok:{acct.uid}"

    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP
        result = asyncio.run(
            converter._with_account_rotation(
                pool, rid="t1", model_name="m", attempt=attempt
            )
        )
    finally:
        _restore_rl_config(saved)
    assert result == "ok:b", f"应换到 B 号成功，实际 {result}"
    assert calls == ["a", "b"], f"调用顺序应为 a→b，实际 {calls}"
    assert pool.faults == [], "429 绝不能禁号"
    print("✅ test_rotation_switches_account_on_429")


def test_rotation_backoff_round_when_all_429():
    """整圈全 429 → 退避后重试第二轮 → 成功。"""
    pool = FakePool([FakeAcct("a"), FakeAcct("b")])
    calls: list[str] = []
    state = {"round": 0}

    async def attempt(acct):
        calls.append(acct.uid)
        if state["round"] == 0:
            raise RateLimitedError(acct.uid, 429, b"too many")
        return f"ok:{acct.uid}"

    async def run():
        # 第一轮结束后把 round 翻成 1（模拟上游限流窗口过去）。
        async def wrapped(acct):
            if len(calls) >= 2:
                state["round"] = 1
            return await attempt(acct)

        return await converter._with_account_rotation(
            pool, rid="t2", model_name="m", attempt=wrapped
        )

    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP
        result = asyncio.run(run())
    finally:
        _restore_rl_config(saved)
    assert result == "ok:a", f"第二轮应成功，实际 {result}"
    assert calls[:2] == ["a", "b"], f"第一轮应按序试完全部账号，实际 {calls}"
    print("✅ test_rotation_backoff_round_when_all_429")


def test_rotation_429_exhausted_passthrough():
    """全 429 且退避轮用尽 → HTTPException 429（不是 503）。"""
    pool = FakePool([FakeAcct("a"), FakeAcct("b")])
    calls: list[str] = []

    async def attempt(acct):
        calls.append(acct.uid)
        raise RateLimitedError(acct.uid, 429, b"too many")

    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP
        try:
            asyncio.run(
                converter._with_account_rotation(
                    pool, rid="t3", model_name="m", attempt=attempt
                )
            )
            raise AssertionError("应抛 HTTPException(429)")
        except converter.HTTPException as e:
            assert e.status_code == 429, f"应透传 429，实际 {e.status_code}"
    finally:
        _restore_rl_config(saved)
    # 首发 1 轮 + 退避 2 轮 = 3 轮 × 2 号 = 6 次调用。
    assert len(calls) == 6, f"应共试 3 轮 × 2 号 = 6 次，实际 {calls}"
    print("✅ test_rotation_429_exhausted_passthrough")


def test_rotation_fault_beats_rate_limit():
    """429 + 账号故障混合：故障是确定性的，不做限流退避，直接按故障语义失败。"""
    pool = FakePool([FakeAcct("a"), FakeAcct("b")])
    calls: list[str] = []

    async def attempt(acct):
        calls.append(acct.uid)
        if acct.uid == "a":
            raise RateLimitedError("a", 429, b"too many")
        raise AccountFaultError("b", "账号被上游授权封禁", 403, b"request illegal")

    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP
        try:
            asyncio.run(
                converter._with_account_rotation(
                    pool, rid="t4", model_name="m", attempt=attempt
                )
            )
            raise AssertionError("应抛 HTTPException(503)")
        except converter.HTTPException as e:
            assert e.status_code == 503, f"混合结局应按账号池耗尽 503，实际 {e.status_code}"
    finally:
        _restore_rl_config(saved)
    assert calls == ["a", "b"], f"混合结局不应退避重试，实际 {calls}"
    assert pool.faults == ["b"], "只有真故障（B 号）应被禁用"
    print("✅ test_rotation_fault_beats_rate_limit")


def test_post_backend_filter_retry_429():
    """responses/anthropic 缓冲共用路径：429 换号成功；全 429 用尽 → 429。"""
    saved = _save_rl_config()
    saved_desens = converter.CONFIG.get("desensitize")
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP
        converter.CONFIG["desensitize"] = False

        pool = FakePool([FakeAcct("a"), FakeAcct("b")])
        status_by_uid = {"a": 429, "b": 200}
        seen: list[str] = []

        async def fake_once(url, headers, body, *, proxy, rid="", model_name="?"):
            uid = headers["X-User-Id"]
            seen.append(uid)
            return status_by_uid[uid], b'{"ok":true}'

        orig_once = converter._post_backend_once
        converter._post_backend_once = fake_once
        try:
            status, raw, _ = asyncio.run(
                converter._post_backend_with_filter_retry(
                    "http://x/v2/chat/completions", pool, {}, rid="t5", model_name="m"
                )
            )
            assert status == 200 and seen == ["a", "b"], f"应换号成功，实际 {status} {seen}"
        finally:
            converter._post_backend_once = orig_once

        # 全 429 用尽 → 429。
        pool2 = FakePool([FakeAcct("a"), FakeAcct("b")])
        seen2: list[str] = []

        async def fake_once_all429(url, headers, body, *, proxy, rid="", model_name="?"):
            seen2.append(headers["X-User-Id"])
            return 429, b"too many"

        converter._post_backend_once = fake_once_all429
        try:
            try:
                asyncio.run(
                    converter._post_backend_with_filter_retry(
                        "http://x/v2/chat/completions", pool2, {}, rid="t6", model_name="m"
                    )
                )
                raise AssertionError("应抛 HTTPException(429)")
            except converter.HTTPException as e:
                assert e.status_code == 429, f"应透传 429，实际 {e.status_code}"
            assert len(seen2) == 6, f"3 轮 × 2 号 = 6 次，实际 {seen2}"
        finally:
            converter._post_backend_once = orig_once
    finally:
        _restore_rl_config(saved)
        converter.CONFIG["desensitize"] = saved_desens
    print("✅ test_post_backend_filter_retry_429")


def _sse_client(handler):
    """替换 environments.make_async_client 为 MockTransport 版（测试后不自动恢复）。"""
    orig = environments.make_async_client
    environments.make_async_client = lambda *a, **k: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return orig


def test_stream_429_switches_account():
    """流式：A 号 429 → 同请求内换 B 号 → 正常出流。"""
    pool = FakePool([FakeAcct("a", token="tok"), FakeAcct("b", token="tok")])

    def handler(request: httpx.Request) -> httpx.Response:
        uid = request.headers.get("X-User-Id", "")
        if uid == "a":
            return httpx.Response(
                429, json={"code": 14003, "msg": "too many requests"}
            )
        return httpx.Response(
            200,
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
        )

    orig = _sse_client(handler)
    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP

        async def run():
            chunks = []
            async for chunk in converter._stream_upstream(
                "http://x/v2/chat/completions", pool, {}, model_name="m", rid="t7"
            ):
                chunks.append(chunk)
            return b"".join(chunks)

        out = asyncio.run(run())
    finally:
        environments.make_async_client = orig
        _restore_rl_config(saved)
    assert b"ok" in out, f"B 号应正常出流，实际 {out[:200]}"
    assert b"429" not in out, "不应把 429 错误事件发给客户端"
    assert pool.faults == [], "429 绝不能禁号"
    print("✅ test_stream_429_switches_account")


def test_stream_all_429_exhausted():
    """流式：全部账号 429 → 退避轮用尽 → error 事件（rate_limited, 429）。"""
    pool = FakePool([FakeAcct("a"), FakeAcct("b")])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"code": 14003, "msg": "too many requests"}
        )

    orig = _sse_client(handler)
    saved = _save_rl_config()
    try:
        converter.CONFIG["rate_limit_retry"] = RL_RETRIES
        converter.CONFIG["rate_limit_retry_gap"] = RL_GAP

        async def run():
            chunks = []
            async for chunk in converter._stream_upstream(
                "http://x/v2/chat/completions", pool, {}, model_name="m", rid="t8"
            ):
                chunks.append(chunk)
            return b"".join(chunks)

        out = asyncio.run(run())
    finally:
        environments.make_async_client = orig
        _restore_rl_config(saved)
    assert b"rate_limited" in out, f"应有 rate_limited 错误事件，实际 {out[:300]}"
    assert pool.faults == [], "429 绝不能禁号"
    print("✅ test_stream_all_429_exhausted")


def test_restore_survives_corrupted_state():
    """回归修复（2026-10-14 启动崩溃）：state 里 account_pool.accounts 为 list
    形态（state_snapshot 旧键写坏）时，恢复必须跳过而非 AttributeError 崩启动。"""
    import json as _json
    import os
    import tempfile
    from account_pool import AccountPool

    with tempfile.TemporaryDirectory() as td:
        auth = os.path.join(td, "a.info")
        with open(auth, "w", encoding="utf-8") as f:
            _json.dump({"account": {"uid": "u1"},
                        "auth": {"domain": "www.codebuddy.ai", "accessToken": "x"}}, f)
        cfg = os.path.join(td, "accounts-intl.json")
        with open(cfg, "w", encoding="utf-8") as f:
            _json.dump({"default_uid": "u1",
                        "accounts": [{"uid": "u1", "auth": "a.info", "priority": 1}]}, f)
        state = os.path.join(td, "state.json")
        # 污染形态：accounts 是 list（快照 status() 写坏的那种）
        with open(state, "w", encoding="utf-8") as f:
            _json.dump({"account_pool": {"multi_mode": True,
                                         "accounts": [{"uid": "u1", "disabled": False}]}}, f)

        pool = AccountPool(
            env="intl", config_path=cfg, state_path=state,
            default_proxy=None, legacy_auth_path=auth,
            cred_factory=lambda p, px: SimpleNamespace(get_headers=lambda: {}),
        )
        assert pool.has_accounts(), "跳过坏段后应正常加载账号"

        # 且随后 save_state() 用正确 dict 形态重写该段
        pool.save_state()
        good = _json.load(open(state, encoding="utf-8"))["account_pool"]["accounts"]
        assert isinstance(good, dict) and "u1" in good, "save_state 应重写为 dict 形态"
    print("✅ test_restore_survives_corrupted_state")


def test_snapshot_uses_status_key():
    """快照观测副本必须用 account_pool_status 键（与池持久化段 account_pool 分离，
    防止 list 形态覆盖 dict 形态 —— 2026-10-14 启动崩溃根因）。"""
    import state_snapshot
    snap = state_snapshot.build_snapshot(account_pool=[{"uid": "u1"}])
    assert "account_pool_status" in snap, "快照应写 account_pool_status 键"
    assert "account_pool" not in snap, "快照不得占用池的持久化键 account_pool"
    print("✅ test_snapshot_uses_status_key")


def test_task_runner_cache_invalidated_on_reload():
    """回归修复（2026-10-14）：配置热加载重建账号后，任务侧 runner 缓存必须
    按新 auth_path 重建 —— 否则定时任务继续用旧凭据路径（chat 正常、任务全挂）。"""
    from pathlib import Path

    import task_scheduler as ts_mod
    from task_scheduler import TaskScheduler

    new_path = "/data/auth/intl/new.info"
    acct_new = SimpleNamespace(
        uid="u1", auth_path=Path(new_path), proxy=None,
        cred=SimpleNamespace(get_headers=lambda: {}),
    )

    class GettablePool(FakePool):
        def get(self, uid):
            return acct_new if uid == "u1" else None

    cfg = ts_mod.TaskConfig(
        checkin_enabled=False, travel_enabled=False, activity_enabled=False,
        keepalive_enabled=False, school_enabled=False, cat_enabled=False,
    )
    ts = TaskScheduler(
        cred=None, config=cfg, cache_dir=None, pool=GettablePool([FakeAcct("u1")]),
        runners={"keepalive": "placeholder"}, log=lambda _m: None,
    )
    # 预填「旧路径」缓存（模拟 reload 前构建的 runner）
    ts.account_runners["u1"] = {"k": "old-runner"}
    ts._account_runner_paths["u1"] = "/data/auth/auth/intl/old.info"

    rs = ts._runners_for_account("u1")

    assert rs != {"k": "old-runner"}, "auth_path 变了必须重建，不能复用旧缓存"
    # Windows 上 str(Path) 会把斜杠归一化成反斜杠，用 Path 比较保证跨平台
    assert Path(ts._account_runner_paths["u1"]) == Path(new_path), "应记录新路径"
    # 路径未变时走缓存
    rs2 = ts._runners_for_account("u1")
    assert rs2 is rs, "路径未变应命中缓存"
    # 事件清空
    ts.invalidate_runner_cache()
    assert ts.account_runners == {} and ts._account_runner_paths == {}
    print("✅ test_task_runner_cache_invalidated_on_reload")


if __name__ == "__main__":
    test_policy_defaults()
    test_429_is_not_account_fault()
    test_rotation_switches_account_on_429()
    test_rotation_backoff_round_when_all_429()
    test_rotation_429_exhausted_passthrough()
    test_rotation_fault_beats_rate_limit()
    test_post_backend_filter_retry_429()
    test_stream_429_switches_account()
    test_stream_all_429_exhausted()
    test_restore_survives_corrupted_state()
    test_snapshot_uses_status_key()
    test_task_runner_cache_invalidated_on_reload()
    print("🎉 全部通过")
