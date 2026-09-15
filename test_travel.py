#!/usr/bin/env python3
"""test_travel.py — 验证猫猫旅行巡检状态机：四态分派、领养、当日防抖与强制重试。

直接运行：python3 test_travel.py
"""

import sys

import httpx

sys.path.insert(0, ".")

import growth_api  # noqa: E402
import travel  # noqa: E402
from travel import TravelRunner  # noqa: E402


class _Cred:
    def __init__(self, fail=False):
        self.fail = fail

    def get_headers(self, within_ms=60_000):
        if self.fail:
            raise RuntimeError("凭据读取失败")
        return {"Authorization": "Bearer t", "X-User-Id": "u-1", "X-Domain": "www.codebuddy.cn"}

    def summary(self):
        return {"nickname": "测试号"}


def _router(routes: dict):
    """按 path → (status, body) 或 callable(req) 路由，并记录全部请求路径。"""
    calls: list[str] = []

    def handler(req):
        path = req.url.path
        calls.append(path)
        r = routes.get(path, (200, {"code": 0, "data": {}}))
        if callable(r):
            return r(req)
        return httpx.Response(r[0], json=r[1])

    return handler, calls


def _runner(routes, cred=None, **kw):
    handler, calls = _router(routes)
    r = TravelRunner(cred or _Cred(), transport=httpx.MockTransport(handler), **kw)
    return r, calls


def test_no_buddy_adopts():
    """无猫 → 同意协议 + 领养成功（+300）。"""
    routes = {
        growth_api.PATH_BUDDY_INFO: (200, {"code": 0, "data": {"buddy": None}}),
        growth_api.PATH_BUDDY_AGREEMENT: (200, {"code": 0, "data": {}}),
        growth_api.PATH_BUDDY_FIRST: (200, {"code": 0, "data": {"credit": 300}}),
    }
    r, calls = _runner(routes)
    oc = r.run_once(reason="test")
    assert oc.ok and "领养成功" in oc.summary, oc.summary
    assert calls == [
        growth_api.PATH_BUDDY_INFO,
        growth_api.PATH_BUDDY_AGREEMENT,
        growth_api.PATH_BUDDY_FIRST,
    ], calls
    print("✅ test_no_buddy_adopts")


def test_adopt_threshold_not_reached_is_debounced_same_day():
    """门槛未达标（400 first_buddy…）→ 当日只试一次，第二趟连上游都不打。"""
    routes = {
        growth_api.PATH_BUDDY_INFO: (200, {"code": 0, "data": {"buddy": None}}),
        growth_api.PATH_BUDDY_AGREEMENT: (200, {"code": 0, "data": {}}),
        growth_api.PATH_BUDDY_FIRST: (400, {"code": 400, "msg": "first_buddy task not completed yet"}),
    }
    r, calls = _runner(routes)
    oc = r.run_once(reason="09:00")
    assert oc.ok and "未达门槛" in oc.summary, oc.summary
    assert oc.detail["steps"][0]["adopt"] == "not_ready"
    n = len(calls)

    oc2 = r.run_once(reason="21:00")
    assert oc2.ok and "当日不再重试" in oc2.summary, oc2.summary
    assert len(calls) == n + 1, calls  # 只多了一次 buddy/info
    assert r.adopt_tried_uids() == ["u-1"]
    print("✅ test_adopt_threshold_not_reached_is_debounced_same_day")


def test_adopt_force_bypasses_debounce():
    """活跃上报补满对话量后的强制领养：豁免当日防抖，且已有猫时直接跳过。"""
    state = {"buddy": None}

    def buddy_info(req):
        return httpx.Response(200, json={"code": 0, "data": {"buddy": state["buddy"]}})

    routes = {
        growth_api.PATH_BUDDY_INFO: buddy_info,
        growth_api.PATH_BUDDY_AGREEMENT: (200, {"code": 0, "data": {}}),
        growth_api.PATH_BUDDY_FIRST: (400, {"code": 400, "msg": "first_buddy task not completed yet"}),
    }
    r, _calls = _runner(routes)
    r.run_once(reason="09:00")  # 第一次：门槛未达，记当日已试

    state["buddy"] = None
    routes[growth_api.PATH_BUDDY_FIRST] = (200, {"code": 0, "data": {"credit": 300}})
    oc = r.adopt_force(reason="activity")
    assert oc.ok and "领养成功" in oc.summary, oc.summary

    state["buddy"] = {"id": 1, "name": "咪"}
    oc2 = r.adopt_force(reason="activity")
    assert oc2.ok and "已有猫" in oc2.summary, oc2.summary
    print("✅ test_adopt_force_bypasses_debounce")


def test_idle_departs_and_daily_limit_skips():
    def mk(daily_limit):
        routes = {
            growth_api.PATH_BUDDY_INFO: (200, {"code": 0, "data": {"buddy": {"id": 1, "name": "咪"}}}),
            growth_api.PATH_TRAVEL_STATUS: (
                200,
                {"code": 0, "data": {"state": "idle", "daily_limit_reached": daily_limit, "record_id": 0}},
            ),
            growth_api.PATH_TRAVEL_DEPART: (200, {"code": 0, "data": {}}),
        }
        return _runner(routes)

    r, calls = mk(False)
    oc = r.run_once(reason="09:00")
    assert oc.ok and "派出成功" in oc.summary and oc.detail["location_id"] == 4, oc.summary
    assert growth_api.PATH_TRAVEL_DEPART in calls

    r2, calls2 = mk(True)
    oc2 = r2.run_once(reason="21:00")
    assert oc2.ok and "今日已派出过" in oc2.summary
    assert growth_api.PATH_TRAVEL_DEPART not in calls2
    print("✅ test_idle_departs_and_daily_limit_skips")


def test_arrived_claims_with_record_id():
    routes = {
        growth_api.PATH_BUDDY_INFO: (200, {"code": 0, "data": {"buddy": {"id": 1, "name": "咪"}}}),
        growth_api.PATH_TRAVEL_STATUS: (
            200,
            {"code": 0, "data": {"state": "arrived", "record_id": 88, "reward_credit": 30}},
        ),
        growth_api.PATH_TRAVEL_CLAIM: (200, {"code": 0, "data": {"reward_credit": 30}}),
    }
    r, calls = _runner(routes)
    oc = r.run_once(reason="21:00")
    assert oc.ok and "领奖成功" in oc.summary and oc.detail["reward"] == 30, oc.summary
    assert growth_api.PATH_TRAVEL_CLAIM in calls

    # arrived 但没有 record_id：跳过领奖，不算失败
    routes2 = dict(routes)
    routes2[growth_api.PATH_TRAVEL_STATUS] = (200, {"code": 0, "data": {"state": "arrived", "record_id": 0}})
    r2, calls2 = _runner(routes2)
    oc2 = r2.run_once(reason="21:00")
    assert oc2.ok and "缺 record_id" in oc2.summary
    assert growth_api.PATH_TRAVEL_CLAIM not in calls2
    print("✅ test_arrived_claims_with_record_id")


def test_traveling_and_unknown_state_skip():
    def mk(state):
        routes = {
            growth_api.PATH_BUDDY_INFO: (200, {"code": 0, "data": {"buddy": {"id": 1, "name": "咪"}}}),
            growth_api.PATH_TRAVEL_STATUS: (200, {"code": 0, "data": {"state": state, "record_id": 5}}),
        }
        return _runner(routes)

    r, _c = mk("traveling")
    oc = r.run_once()
    assert oc.ok and "在途" in oc.summary and oc.detail["action"] == "skip"

    r2, _c2 = mk("weird")
    oc2 = r2.run_once()
    assert oc2.ok and "未知状态" in oc2.summary
    print("✅ test_traveling_and_unknown_state_skip")


def test_query_failure_is_reported_not_raised():
    handler = lambda req: httpx.Response(500, json={"code": 500, "msg": "boom"})  # noqa: E731
    r = TravelRunner(_Cred(), transport=httpx.MockTransport(handler))
    oc = r.run_once()
    assert not oc.ok and "查猫档案失败" in oc.summary, oc.summary
    assert r.status()["last"]["ok"] is False
    print("✅ test_query_failure_is_reported_not_raised")


def test_missing_credential_and_broken_credential():
    r = TravelRunner(None)
    oc = r.run_once()
    assert not oc.ok and "登录凭据" in oc.summary

    r2 = TravelRunner(_Cred(fail=True))
    oc2 = r2.run_once()
    assert not oc2.ok and "取凭据/刷新失败" in oc2.summary
    print("✅ test_missing_credential_and_broken_credential")


def test_default_hours_are_9_and_21():
    assert travel.DEFAULT_TRAVEL_HOURS == (9, 21)
    assert travel.STATE_ARRIVED == "arrived" and travel.STATE_IDLE == "idle"
    print("✅ test_default_hours_are_9_and_21")


if __name__ == "__main__":
    import time

    t0 = time.time()
    test_no_buddy_adopts()
    test_adopt_threshold_not_reached_is_debounced_same_day()
    test_adopt_force_bypasses_debounce()
    test_idle_departs_and_daily_limit_skips()
    test_arrived_claims_with_record_id()
    test_traveling_and_unknown_state_skip()
    test_query_failure_is_reported_not_raised()
    test_missing_credential_and_broken_credential()
    test_default_hours_are_9_and_21()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
