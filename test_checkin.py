#!/usr/bin/env python3
"""
test_checkin.py — 验证签到/余额链路、小时配置归一化与调度时点计算。

直接运行：python3 test_checkin.py
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta

import httpx

sys.path.insert(0, ".")

import checkin  # noqa: E402
from checkin import (  # noqa: E402
    BILLING_BASE,
    CheckinScheduler,
    DEFAULT_CHECKIN_HOURS,
    billing_ua,
    do_checkin,
    fetch_balance,
    normalize_hours,
    to_billing_headers,
)


def _t(handler):
    return httpx.MockTransport(handler)


def test_normalize_hours_tristate():
    """缺省 / 空 / 非法 → 默认 (9,21)；显式 0 与显式 N 都生效。"""
    assert normalize_hours(None) == DEFAULT_CHECKIN_HOURS
    assert normalize_hours("") == DEFAULT_CHECKIN_HOURS
    assert normalize_hours([]) == DEFAULT_CHECKIN_HOURS
    assert normalize_hours(["x", 99, -1]) == DEFAULT_CHECKIN_HOURS
    assert normalize_hours("0") == (0,), "显式 0 点必须生效，不能被当成未配置"
    assert normalize_hours("9,21") == (9, 21)
    assert normalize_hours([21, 9, 9]) == (9, 21)
    assert normalize_hours("23,1,7") == (1, 7, 23)
    print("✅ test_normalize_hours_tristate")


def test_billing_headers_strip_chat_fingerprint():
    """billing 头要丢掉 CLI 指纹头，并强制写入非 Python 默认 UA。"""
    common = {
        "Authorization": "Bearer tok",
        "X-User-Id": "u1",
        "X-Enterprise-Id": "e1",
        "X-Tenant-Id": "e1",
        "X-Domain": "www.workbuddy.cn",
        "Origin": "https://www.codebuddy.cn",
        "Referer": "https://www.codebuddy.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "X-Product": "SaaS",
        "User-Agent": "codebuddy2openai/2.0",
    }
    h = to_billing_headers(common)
    for k in ("Origin", "Referer", "X-Requested-With", "X-Product"):
        assert k not in h, k
    assert h["Authorization"] == "Bearer tok" and h["X-User-Id"] == "u1"
    assert h["X-Enterprise-Id"] == "e1" and h["X-Tenant-Id"] == "e1"
    assert h["Accept"] == "application/json" and h["Content-Type"] == "application/json"
    assert h["User-Agent"] == billing_ua() and "python" not in h["User-Agent"].lower()
    # 空值字段不写入
    h2 = to_billing_headers({"Authorization": "Bearer t", "X-Domain": ""})
    assert "X-Domain" not in h2 and "X-Enterprise-Id" not in h2
    print("✅ test_billing_headers_strip_chat_fingerprint")


def test_do_checkin_success():
    def handler(req):
        assert req.url.path == "/v2/billing/meter/daily-checkin"
        assert req.content in (b"{}", b"", None)
        return httpx.Response(
            200, json={"code": 0, "msg": "OK", "data": {"credits": 30}}
        )

    r = do_checkin({"User-Agent": "x"}, transport=_t(handler))
    assert r.ok and not r.already and r.code == 0
    assert "成功" in r.summary()
    print("✅ test_do_checkin_success")


def test_do_checkin_already():
    """重复签到是 4xx + code 10001，属 already，不算失败。"""
    def handler(req):
        return httpx.Response(400, json={"code": 10001, "msg": "今天已签到，请明天再来"})

    r = do_checkin({}, transport=_t(handler))
    assert r.already and r.ok and r.code == 10001
    assert "已签到" in r.summary()
    # 仅靠文案也能识别
    r2 = do_checkin({}, transport=_t(lambda req: httpx.Response(200, json={"code": 1, "msg": "already checked in"})))
    assert r2.already
    print("✅ test_do_checkin_already")


def test_do_checkin_failure():
    def handler(req):
        return httpx.Response(403, json={"code": 10085, "msg": "请求不合法"})

    r = do_checkin({}, transport=_t(handler))
    assert not r.ok and not r.already and r.status == 403 and r.code == 10085
    assert "失败" in r.summary()

    def boom(req):
        raise httpx.ConnectError("no route")

    r2 = do_checkin({}, transport=_t(boom))
    assert not r2.ok and r2.status == -1 and "网络失败" in r2.msg
    print("✅ test_do_checkin_failure")


def test_fetch_balance_aggregation():
    payload = {
        "code": 0,
        "msg": "OK",
        "data": {
            "Response": {
                "Data": {
                    "TotalDosage": 2675,
                    "Accounts": [
                        {"PackageName": "A", "CycleCapacitySize": 500,
                         "CycleCapacityRemain": 500, "CapacityRemain": 999},
                        {"PackageName": "B", "CycleCapacitySize": 0,
                         "CycleCapacityRemain": 0, "CapacityRemain": 120},
                        {"PackageName": "C", "CycleCapacitySize": 100,
                         "CycleCapacityRemain": -5, "CapacityRemain": 0},
                    ],
                }
            }
        },
    }
    r = fetch_balance({}, transport=_t(lambda req: httpx.Response(200, json=payload)))
    assert r.ok and r.remain == 620, r.remain   # 500 + 120 + max(0,-5)
    assert r.total_dosage == 2675 and len(r.accounts) == 3
    assert "620" in r.summary()

    r2 = fetch_balance({}, transport=_t(lambda req: httpx.Response(403, json={"code": 10085, "msg": "x"})))
    assert not r2.ok and r2.status == 403
    print("✅ test_fetch_balance_aggregation")


def test_fetch_balance_empty_accounts():
    r = fetch_balance({}, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {}})))
    assert r.ok and r.remain == 0 and r.accounts == []
    print("✅ test_fetch_balance_empty_accounts")


class _Cred:
    """假凭据管理器：记录刷新窗口，返回可用头。"""

    def __init__(self, fail=False):
        self.window = None
        self.fail = fail

    def get_headers(self, within_ms=60_000):
        self.window = within_ms
        if self.fail:
            raise RuntimeError("凭据读取失败")
        return {"Authorization": "Bearer t", "X-User-Id": "u", "X-Domain": "d"}


def _sched(**kw):
    cred = kw.pop("cred", None) or _Cred()
    td = tempfile.mkdtemp()
    kw.setdefault("hours", "9,21")
    return CheckinScheduler(cred, cache_dir=td, **kw), cred


def test_run_once_pre_refreshes_token():
    """D3 修复：签到前必须按 2h 窗口预刷新，而不是等 token 硬过期。"""
    sched, cred = _sched()
    calls = {"checkin": 0, "balance": 0}

    def fake_checkin(headers, **kw):
        calls["checkin"] += 1
        assert headers["User-Agent"] == billing_ua()
        return checkin.CheckinResult(True, False, 200, 0, "OK")

    def fake_balance(headers, **kw):
        calls["balance"] += 1
        return checkin.BalanceResult(True, remain=1234, total_dosage=9)

    orig_c, orig_b = checkin.do_checkin, checkin.fetch_balance
    checkin.do_checkin, checkin.fetch_balance = fake_checkin, fake_balance
    try:
        entry = sched.run_once(reason="test")
    finally:
        checkin.do_checkin, checkin.fetch_balance = orig_c, orig_b

    assert cred.window == checkin.REFRESH_WINDOW_MS, cred.window
    assert calls == {"checkin": 1, "balance": 1}
    assert entry["ok"] and entry["balance"] == 1234 and entry["reason"] == "test"
    assert sched.last == entry and len(sched.history) == 1
    print("✅ test_run_once_pre_refreshes_token")


def test_run_once_isolation_and_history_persistence():
    """任何异常都要收敛成结果字典，且写盘留档（D4 修复）。"""
    sched, cred = _sched(cred=_Cred(fail=True))
    entry = sched.run_once(reason="cred-fail")
    assert entry["ok"] is False and "凭据" in entry["error"]
    assert len(sched.history) == 1

    # 换一个正常 cred，验证历史落盘后可被新实例读回
    def boom(headers, **kw):
        raise RuntimeError("teapot")

    orig = checkin.do_checkin
    checkin.do_checkin = boom
    try:
        sched2, _ = _sched()
        sched2.cache_dir = sched.cache_dir
        sched2.history = sched2._load_history()
        sched2.run_once(reason="boom")
    finally:
        checkin.do_checkin = orig
    assert (sched.cache_dir / checkin.HISTORY_NAME).exists()

    sched3, _ = _sched()
    sched3.cache_dir = sched.cache_dir
    reloaded = sched3._load_history()
    assert len(reloaded) >= 2, reloaded
    print("✅ test_run_once_isolation_and_history_persistence")


def test_schedule_slots_and_late_catchup():
    """时点计算 + 迟到唤醒补跑（休眠后不能漏签），但重启不能翻出老班次。"""
    sched, _ = _sched(hours="9,21")
    base = datetime(2026, 9, 12, 8, 30, 0)
    ts = base.timestamp()

    nxt = sched.next_fire_at(ts)
    assert datetime.fromtimestamp(nxt).strftime("%H:%M") == "09:00", nxt
    # 8:30 起飞：今天 09:00 还没到，昨天 21:00 已出补跑窗口 → 不补
    assert sched._due_slots(ts) == []

    # 模拟短休眠：21:00 已过 10 分钟 → 补跑该班次
    wake = datetime(2026, 9, 12, 21, 10, 0).timestamp()
    assert sched._due_slots(wake) == [("2026-09-12", 21)]
    for k in sched._due_slots(wake):
        sched._fired.add(k)
    assert sched._due_slots(wake) == []

    # 跨夜补跑：23:00 的班次在次日 01:00 醒来时仍在窗口内
    cross = datetime(2026, 9, 13, 1, 0, 0).timestamp()
    sched2, _ = _sched(hours="23")
    assert sched2._due_slots(cross) == [("2026-09-12", 23)]

    # 但睡过头 12 小时就不补了（昨天的班次不该在重启时翻出来）
    over = datetime(2026, 9, 13, 11, 0, 0).timestamp()
    assert sched2._due_slots(over) == []

    # 次日到下一天
    assert datetime.fromtimestamp(sched.next_fire_at(wake)).strftime("%Y-%m-%d %H:%M") == "2026-09-13 09:00"
    print("✅ test_schedule_slots_and_late_catchup")


def test_fired_set_pruned():
    sched, _ = _sched()
    old = (datetime.now() - timedelta(days=10)).date().isoformat()
    sched._fired = {(old, 9), (datetime.now().date().isoformat(), 9)}
    sched._prune_fired(time.time())
    assert len(sched._fired) == 1, sched._fired
    print("✅ test_fired_set_pruned")


def test_status_and_thread_start_stop():
    sched, cred = _sched(hours=(21,))
    st = sched.status()
    assert st["enabled"] is True and st["hours"] == [21]
    assert st["next_fire_at"] and st["next_fire_ts"] > time.time()
    json.dumps(st, ensure_ascii=False)

    sched.start()
    time.sleep(0.2)
    assert sched.is_alive()
    sched.stop()
    sched.join(timeout=3)
    assert not sched.is_alive()

    off, _ = _sched(enabled=False)
    off.start()
    off.join(timeout=3)
    assert not off.is_alive()
    assert off.status()["next_fire_at"] is None
    print("✅ test_status_and_thread_start_stop")


def test_billing_ua_env_override():
    import os

    old = os.environ.get("CODEBUDDY2OPENAI_BILLING_UA")
    try:
        os.environ["CODEBUDDY2OPENAI_BILLING_UA"] = "Custom/9.9"
        assert billing_ua() == "Custom/9.9"
        assert to_billing_headers({})["User-Agent"] == "Custom/9.9"
    finally:
        if old is None:
            os.environ.pop("CODEBUDDY2OPENAI_BILLING_UA", None)
        else:
            os.environ["CODEBUDDY2OPENAI_BILLING_UA"] = old
    assert billing_ua().startswith("CLI/")
    print("✅ test_billing_ua_env_override")


def test_paths_point_at_billing_domain():
    assert BILLING_BASE == "https://www.codebuddy.cn"
    assert checkin.CHECKIN_PATH == "/v2/billing/meter/daily-checkin"
    assert checkin.RESOURCE_PATH == "/v2/billing/meter/get-user-resource"
    print("✅ test_paths_point_at_billing_domain")


if __name__ == "__main__":
    t0 = time.time()
    test_normalize_hours_tristate()
    test_billing_headers_strip_chat_fingerprint()
    test_do_checkin_success()
    test_do_checkin_already()
    test_do_checkin_failure()
    test_fetch_balance_aggregation()
    test_fetch_balance_empty_accounts()
    test_run_once_pre_refreshes_token()
    test_run_once_isolation_and_history_persistence()
    test_schedule_slots_and_late_catchup()
    test_fired_set_pruned()
    test_status_and_thread_start_stop()
    test_billing_ua_env_override()
    test_paths_point_at_billing_domain()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
