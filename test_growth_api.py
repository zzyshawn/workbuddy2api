#!/usr/bin/env python3
"""test_growth_api.py — 验证 growth / billing 域接口层：账号视图、请求头、信封与错误语义。

直接运行：python3 test_growth_api.py
"""

import json
import sys

import httpx

sys.path.insert(0, ".")

import growth_api  # noqa: E402
from growth_api import (  # noqa: E402
    Account,
    ApiResult,
    account_of,
    chat_request_event,
    claim_task,
    fetch_buddy,
    fetch_streak,
    fetch_travel_status,
    is_buddy_task_incomplete,
    list_tasks,
    report_chat_activity,
    travel_claim,
)


def _t(handler):
    return httpx.MockTransport(handler)


class _Cred:
    """假凭据管理器：记录刷新窗口，返回 chat 风格公共头。"""

    def __init__(self):
        self.window = None

    def get_headers(self, within_ms=60_000):
        self.window = within_ms
        return {
            "Authorization": "Bearer tok",
            "X-User-Id": "u-1",
            "X-Enterprise-Id": "e-1",
            "X-Tenant-Id": "e-1",
            "X-Domain": "www.codebuddy.cn",
            "User-Agent": "codebuddy2openai/2.0",
            "Origin": "https://www.codebuddy.cn",
        }

    def summary(self):
        return {"nickname": "测试号", "uid": "u-1"}


def test_account_of_uses_refresh_window():
    """账号视图取头时按任务窗口预刷新（2h），并解析 uid / 域 / 昵称。"""
    cred = _Cred()
    acct = account_of(cred)
    assert cred.window == growth_api.REFRESH_WINDOW_MS, cred.window
    assert acct.uid == "u-1" and acct.nickname == "测试号"
    assert acct.domain == "www.codebuddy.cn"
    print("✅ test_account_of_uses_refresh_window")


def test_headers_strip_chat_fingerprint():
    """growth / billing 域头：丢 CLI 指纹头、强制非 Python UA、growth 域带 X-CodeBuddy-Request。"""
    acct = Account(uid="u", headers={"Authorization": "Bearer t", "X-User-Id": "u", "Origin": "https://www.codebuddy.cn"})
    b = acct.billing_headers()
    assert "Origin" not in b and "python" not in b["User-Agent"].lower()
    assert b["Authorization"] == "Bearer t" and b["Accept"] == "application/json"
    g = acct.growth_headers()
    assert g["X-CodeBuddy-Request"] == "1"
    w = acct.web_headers(referer="")
    assert w["x-client-platform"] == "web" and w["Referer"].startswith(growth_api.WEB_BASE)
    print("✅ test_headers_strip_chat_fingerprint")


def test_api_call_envelope_and_error_semantics():
    """HTTP 2xx + code 0 → ok；HTTP 200 + code 非 0 → 失败（业务错误常是 200）。"""
    r = growth_api.api_call("https://x", "/p", {}, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {"a": 1}})))
    assert r.ok and r.data == {"a": 1} and r.code == 0

    r2 = growth_api.api_call("https://x", "/p", {}, transport=_t(lambda req: httpx.Response(200, json={"code": 41000, "msg": "活动已结束"})))
    assert not r2.ok and r2.code == 41000 and "41000" in r2.summary()

    r3 = growth_api.api_call("https://x", "/p", {}, transport=_t(lambda req: (_ for _ in ()).throw(httpx.ConnectError("no route"))))
    assert not r3.ok and r3.status == -1 and "网络失败" in r3.msg
    print("✅ test_api_call_envelope_and_error_semantics")


def test_post_without_body_sends_no_payload():
    """body=None 必须不带请求体（领奖端点是空 POST，发 null 会 400）。"""
    seen = {}

    def handler(req):
        seen["body"] = req.content
        return httpx.Response(200, json={"code": 0, "data": {}})

    growth_api.growth_call(Account(uid="u"), "/p", method="POST", transport=_t(handler))
    assert seen["body"] in (b"", None), seen
    print("✅ test_post_without_body_sends_no_payload")


def test_fetch_buddy_null_means_no_buddy():
    acct = Account(uid="u")
    r = fetch_buddy(acct, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {"buddy": None}})))
    assert r.ok and r.data is None

    r2 = fetch_buddy(acct, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {"buddy": {"id": 7, "name": "咪"}}})))
    assert r2.ok and r2.data == {"id": 7, "name": "咪"}

    # data 整体缺失也按无猫处理，不抛
    r3 = fetch_buddy(acct, transport=_t(lambda req: httpx.Response(200, json={"code": 0})))
    assert r3.ok and r3.data is None
    print("✅ test_fetch_buddy_null_means_no_buddy")


def test_travel_status_and_claim_normalization():
    acct = Account(uid="u")
    payload = {"code": 0, "data": {"state": "arrived", "daily_limit_reached": True, "record_id": 42, "reward_credit": 30}}
    st = fetch_travel_status(acct, transport=_t(lambda req: httpx.Response(200, json=payload)))
    assert st.ok and st.data == {"state": "arrived", "daily_limit_reached": True, "record_id": 42, "reward_credit": 30}

    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content or b"{}")
        return httpx.Response(200, json={"code": 0, "data": {"reward_credit": 66}})

    cl = travel_claim(acct, 42, transport=_t(handler))
    assert cl.ok and cl.data == 66
    assert seen["path"] == "/activity/growth/buddy/travel/claim"
    assert seen["body"] == {"record_id": 42}

    # 奖励字段缺失：记 0，不算失败
    cl2 = travel_claim(acct, 42, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {}})))
    assert cl2.ok and cl2.data == 0
    print("✅ test_travel_status_and_claim_normalization")


def test_fetch_streak_and_task_incomplete_detection():
    acct = Account(uid="u")
    r = fetch_streak(acct, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {"streak": {"days": 6}}})))
    assert r.ok and r.data == 6
    r2 = fetch_streak(acct, transport=_t(lambda req: httpx.Response(200, json={"code": 0, "data": {}})))
    assert r2.ok and r2.data == 0

    incomplete = ApiResult(False, 400, None, "first_buddy task not completed yet")
    assert is_buddy_task_incomplete(incomplete)
    assert not is_buddy_task_incomplete(ApiResult(False, 400, None, "其他错误"))
    assert not is_buddy_task_incomplete(ApiResult(False, 500, None, "first_buddy task not completed yet"))
    print("✅ test_fetch_streak_and_task_incomplete_detection")


def test_list_tasks_filters_non_dict_items():
    payload = {"code": 0, "data": {"tasks": [{"task_code": "black_cat"}, "junk", None]}}
    r = list_tasks(Account(uid="u"), transport=_t(lambda req: httpx.Response(200, json=payload)))
    assert r.ok and r.data == [{"task_code": "black_cat"}]
    assert growth_api.find_task(r.data, "black_cat")["task_code"] == "black_cat"
    assert growth_api.find_task(r.data, "nope") is None
    print("✅ test_list_tasks_filters_non_dict_items")


def test_claim_task_falls_back_to_web_on_400():
    """chat 域 400 → 降级 web 域；其他状态码不降级。"""
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "copilot.tencent.com" in str(req.url):
            return httpx.Response(400, json={"code": 400, "msg": "chat 域不认"})
        return httpx.Response(200, json={"code": 0, "data": {"credit": 5}})

    r = claim_task(Account(uid="u", headers={"Authorization": "Bearer t"}), "black_cat", transport=_t(handler))
    assert r.ok and len(calls) == 2 and "workbuddy.cn" in calls[1], calls

    calls.clear()

    def handler_404(req):
        calls.append(str(req.url))
        return httpx.Response(404, json={"code": 404, "msg": "无此任务"})

    r2 = claim_task(Account(uid="u", headers={"Authorization": "Bearer t"}), "black_cat", transport=_t(handler_404))
    assert not r2.ok and len(calls) == 1
    print("✅ test_claim_task_falls_back_to_web_on_400")


def test_chat_event_shape_requires_user_id():
    """事件必须带 userId（缺了服务端 200 但静默丢弃），且 activityId 只在传入时出现。"""
    ev = chat_request_event("u-9", conversation_id="cid", request_id="cid-r1")
    assert ev["eventCode"] == "chat_request_send" and ev["userId"] == "u-9"
    assert ev["conversationId"] == "cid" and ev["requestId"] == "cid-r1"
    assert ev["rootRequestId"] == "cid" and ev["parentConversationId"] == "cid"
    assert ev["requestModelId"] == growth_api.DEFAULT_REPORT_MODEL_ID
    assert "activityId" not in ev

    ev2 = chat_request_event("u-9", conversation_id="cid", activity_id="school_open_day_2026")
    assert ev2["activityId"] == "school_open_day_2026" and ev2["requestId"] == "cid"
    print("✅ test_chat_event_shape_requires_user_id")


def test_report_chat_activity_posts_array_with_cli_ua():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["ua"] = req.headers.get("user-agent", "")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"code": 0})

    r = report_chat_activity(Account(uid="u", headers={"Authorization": "Bearer t"}), "cid", "cid-r1", transport=_t(handler))
    assert r.ok and seen["path"] == "/v2/report"
    assert isinstance(seen["body"], list) and seen["body"][0]["userId"] == "u"
    assert "python" not in seen["ua"].lower()
    print("✅ test_report_chat_activity_posts_array_with_cli_ua")


def test_cst_day_uses_fixed_offset():
    """自然日按 CST 固定 +8 计算（不吃容器 TZ / Windows 不认 TZ 的坑）。"""
    assert growth_api.cst_day(0) == "1970-01-01"
    # UTC 2026-09-15 17:00 = CST 2026-09-16 01:00
    ts = 1789491600.0
    assert growth_api.cst_now(ts).hour == 1 and growth_api.cst_day(ts) == "2026-09-16", growth_api.cst_now(ts)
    print("✅ test_cst_day_uses_fixed_offset")


if __name__ == "__main__":
    import time

    t0 = time.time()
    test_account_of_uses_refresh_window()
    test_headers_strip_chat_fingerprint()
    test_api_call_envelope_and_error_semantics()
    test_post_without_body_sends_no_payload()
    test_fetch_buddy_null_means_no_buddy()
    test_travel_status_and_claim_normalization()
    test_fetch_streak_and_task_incomplete_detection()
    test_list_tasks_filters_non_dict_items()
    test_claim_task_falls_back_to_web_on_400()
    test_chat_event_shape_requires_user_id()
    test_report_chat_activity_posts_array_with_cli_ua()
    test_cst_day_uses_fixed_offset()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
