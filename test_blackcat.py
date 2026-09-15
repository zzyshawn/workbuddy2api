#!/usr/bin/env python3
"""test_blackcat.py — 验证夜猫子任务：夜猫窗口判定、窗口内外行为与领奖幂等。

直接运行：python3 test_blackcat.py
"""

import json
import sys
from datetime import datetime

import httpx

sys.path.insert(0, ".")

import blackcat  # noqa: E402
import growth_api  # noqa: E402
from blackcat import BlackCatRunner, in_night_window  # noqa: E402

CLAIM_PATH = growth_api.CLAIM_PATH_FMT.format(code="black_cat")


class _Cred:
    def get_headers(self, within_ms=60_000):
        return {"Authorization": "Bearer t", "X-User-Id": "u-1"}

    def summary(self):
        return {"nickname": "测试号"}


class _Fake:
    """上游桩：任务状态随时间推进，记录所有请求。"""

    def __init__(self, cur=1, target=3, accept_status="not_accepted", claim=None, report_fails=False):
        self.cur = cur
        self.target = target
        self.accept_status = accept_status
        self.claim = claim if claim is not None else {"code": 0, "data": {"credit": 6, "energy": 20}}
        self.report_fails = report_fails
        self.report_bodies: list[list[dict]] = []
        self.calls: list[str] = []
        self.accepts = 0
        self.claims = 0

    def task_payload(self):
        return {
            "code": 0,
            "data": {
                "tasks": [
                    {
                        "task_code": "black_cat",
                        "accept_status": self.accept_status,
                        "progress": {"current": self.cur, "target": self.target},
                    }
                ]
            },
        }

    def handler(self, req):
        path = req.url.path
        self.calls.append(f"{req.method} {path}")
        if path == growth_api.PATH_TASKS:
            return httpx.Response(200, json=self.task_payload())
        if path == growth_api.PATH_ACCEPT_TASKS:
            self.accepts += 1
            self.accept_status = "accepted"
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path == growth_api.PATH_REPORT:
            if self.report_fails:
                return httpx.Response(500, json={"code": 500, "msg": "boom"})
            body = json.loads(req.content)
            self.report_bodies.append(body)
            self.cur += 1                     # 上报一条进度 +1（服务端语义）
            return httpx.Response(200, json={"code": 0})
        if path == CLAIM_PATH:
            self.claims += 1
            if self.claim.get("code") != 0:
                return httpx.Response(400, json=self.claim)
            return httpx.Response(200, json=self.claim)
        return httpx.Response(404, json={"code": 404})


def _runner(fake, window=True):
    """构造 runner，并把夜猫窗口判定固定成 window（避免测试受真实时刻影响）。

    settle=0：归账等待是给上游留的时间，测试里的桩是同步的，不必真等。
    """
    orig = blackcat.in_night_window
    blackcat.in_night_window = lambda ts=None: window
    r = BlackCatRunner(_Cred(), settle=0, transport=httpx.MockTransport(fake.handler))
    r._restore = orig  # 供用例还原
    return r


def _restore(r):
    blackcat.in_night_window = r._restore


def test_night_window_boundaries():
    """夜猫窗口 = CST 23:00–08:00。"""
    def at(h, m=0):
        return datetime(2026, 9, 16, h, m, tzinfo=growth_api.CST).timestamp()

    assert in_night_window(at(23, 0)) is True
    assert in_night_window(at(23, 59)) is True
    assert in_night_window(at(0, 0)) is True
    assert in_night_window(at(7, 59)) is True
    assert in_night_window(at(8, 0)) is False
    assert in_night_window(at(12, 0)) is False
    assert in_night_window(at(22, 59)) is False
    print("✅ test_night_window_boundaries")


def test_claimed_task_is_skipped():
    fake = _Fake(cur=3, accept_status="claimed")
    r = _runner(fake, window=True)
    try:
        oc = r.run_once(reason="scheduled")
    finally:
        _restore(r)
    assert oc.ok and "已领取" in oc.summary, oc.summary
    assert fake.report_bodies == [] and fake.claims == 0
    print("✅ test_claimed_task_is_skipped")


def test_out_of_window_does_not_touch_upstream():
    """窗口外一律不发写请求（上游窗口外上报不计进度，白打还吃风控）。"""
    fake = _Fake(cur=1)
    r = _runner(fake, window=False)
    try:
        oc = r.run_once(reason="12:00")
    finally:
        _restore(r)
    assert oc.ok and "非夜猫窗口" in oc.summary, oc.summary
    assert oc.detail["in_night_window"] is False
    assert fake.report_bodies == [] and fake.accepts == 0 and fake.claims == 0
    assert fake.calls == [f"GET {growth_api.PATH_TASKS}"], fake.calls
    print("✅ test_out_of_window_does_not_touch_upstream")


def test_in_window_reports_one_and_accepts_when_needed():
    """窗口内：未接先 accept，补 1 条（cap=1），进度未满如实标注。"""
    fake = _Fake(cur=1, target=3, accept_status="not_accepted")
    r = _runner(fake, window=True)
    try:
        oc = r.run_once(reason="01:00")
    finally:
        _restore(r)
    assert oc.ok and fake.accepts == 1 and len(fake.report_bodies) == 1, (oc.summary, fake.calls)
    assert oc.detail["sent"] == 1 and oc.detail["after"] == "2/3"
    assert "本趟上限 1 条" in oc.summary and fake.claims == 0, oc.summary
    # 上报事件形状：GLM-5.2 + mode=night + userId
    ev = fake.report_bodies[0][0]
    assert ev["requestModelId"] == growth_api.NIGHT_MODEL_ID and ev["mode"] == "night"
    assert ev["userId"] == "u-1" and ev["eventCode"] == "chat_request_send"
    print("✅ test_in_window_reports_one_and_accepts_when_needed")


def test_in_window_claims_when_progress_reaches_target():
    fake = _Fake(cur=2, target=3, accept_status="accepted")
    r = _runner(fake, window=True)
    try:
        oc = r.run_once(reason="01:00")
    finally:
        _restore(r)
    assert oc.ok and fake.claims == 1, oc.summary
    assert "领奖成功" in oc.summary and "credit=+6" in oc.summary, oc.summary
    print("✅ test_in_window_claims_when_progress_reaches_target")


def test_already_completed_claims_without_reporting():
    """已完成但未领：直接领奖，不再补上报。"""
    fake = _Fake(cur=3, target=3, accept_status="completed")
    r = _runner(fake, window=False)
    try:
        oc = r.run_once(reason="12:00")
    finally:
        _restore(r)
    assert oc.ok and fake.claims == 1 and fake.report_bodies == [], oc.summary
    print("✅ test_already_completed_claims_without_reporting")


def test_claim_is_idempotent():
    fake = _Fake(
        cur=3,
        target=3,
        accept_status="completed",
        claim={"code": 0, "data": {"already_claimed": True, "credit": 6}},
    )
    r = _runner(fake, window=False)
    try:
        oc = r.run_once(reason="12:00")
    finally:
        _restore(r)
    assert oc.ok and "已领过" in oc.summary, oc.summary
    print("✅ test_claim_is_idempotent")


def test_report_failure_is_reported():
    fake = _Fake(cur=1, report_fails=True)
    r = _runner(fake, window=True)
    try:
        oc = r.run_once(reason="01:00")
    finally:
        _restore(r)
    assert not oc.ok and "夜猫上报失败" in oc.summary, oc.summary
    print("✅ test_report_failure_is_reported")


def test_missing_task_is_skipped_not_failed():
    """任务列表里没有 black_cat（活动下线/new 用户）→ 跳过，不算失败。"""

    def handler(req):
        if req.url.path == growth_api.PATH_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": [{"task_code": "other"}]}})
        return httpx.Response(404, json={"code": 404})

    r = BlackCatRunner(_Cred(), transport=httpx.MockTransport(handler))
    oc = r.run_once()
    assert oc.ok and "没有 black_cat" in oc.summary, oc.summary
    assert r.status()["night_window"].endswith("CST")
    print("✅ test_missing_task_is_skipped_not_failed")


def test_missing_credential():
    r = BlackCatRunner(None)
    oc = r.run_once()
    assert not oc.ok and "登录凭据" in oc.summary
    assert blackcat.DEFAULT_CAT_HOURS == (1,) and blackcat.REPORT_CAP == 1
    print("✅ test_missing_credential")


if __name__ == "__main__":
    import time

    t0 = time.time()
    test_night_window_boundaries()
    test_claimed_task_is_skipped()
    test_out_of_window_does_not_touch_upstream()
    test_in_window_reports_one_and_accepts_when_needed()
    test_in_window_claims_when_progress_reaches_target()
    test_already_completed_claims_without_reporting()
    test_claim_is_idempotent()
    test_report_failure_is_reported()
    test_missing_task_is_skipped_not_failed()
    test_missing_credential()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
