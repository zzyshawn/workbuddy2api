#!/usr/bin/env python3
"""test_school.py — 验证开学季任务：活动下线跳过、人工/未知任务跳过、点亮 + 领奖 + 抽奖收尾。

直接运行：python3 test_school.py
"""

import json
import sys
import time as _time

import httpx

sys.path.insert(0, ".")

import growth_api  # noqa: E402
import school  # noqa: E402
from school import (  # noqa: E402
    ACTIVITY_ID,
    KNOWN_TASKS,
    MIN_GAP,
    PATH_CONFIG,
    PATH_TASKS,
    PATH_WHEEL_DRAW,
    SchoolRunner,
)


class _TimeShim:
    """time 替身：只把 sleep 变成空操作（写动作间隔不真睡），其余透传。

    只替换 school 模块看到的那一份引用，不污染全局 time 模块。
    """

    def __init__(self, real):
        self._real = real

    def sleep(self, _seconds):  # noqa: D102
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Cred:
    def get_headers(self, within_ms=60_000):
        return {"Authorization": "Bearer tok-tok-tok", "X-User-Id": "u-1"}

    def summary(self):
        return {"nickname": "测试号"}


class _Fake:
    """活动接口桩：按端点推送状态，记录全部调用。"""

    def __init__(self, in_period=True, tasks=None, balance=0, draw=None):
        self.in_period = in_period
        self.tasks = tasks if tasks is not None else []
        self.balance = balance
        self.draw = draw or (lambda _i, _bal: {"code": 0, "data": {"prize_code": "school_credit_6", "credit_amount": 6, "chance_balance": _bal - 1}})
        self.calls: list[str] = []
        self.reports: list[tuple[str, list[dict], str]] = []
        self.draws = 0
        self.expert_hits = 0

    # ---------------- 路由 ----------------

    def handler(self, req):
        path = req.url.path
        host = req.url.host
        self.calls.append(f"{req.method} {host}{path}")
        if path == PATH_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"in_period": self.in_period, "tasks": self.tasks}})
        if path.endswith("/viewed"):
            code = path.split("/")[-2]
            self._task(code)["status"] = "in_progress"
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path.endswith("/share-complete"):
            self._advance("share_invite", 1)
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path.endswith("/claim"):
            code = path.split("/")[-2]
            self._task(code)["status"] = "claimed"
            return httpx.Response(200, json={"code": 0, "data": {"chance_granted": 1}})
        if path == PATH_CONFIG:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "in_period": self.in_period,
                        "chance": {"balance": self.balance, "total_earned": self.balance},
                        "prizes": [],
                    },
                },
            )
        if path == PATH_WHEEL_DRAW:
            self.draws += 1
            r = self.draw(self.draws, self.balance)
            if r.get("code") == 0:
                self.balance = int((r.get("data") or {}).get("chance_balance", self.balance - 1))
            return httpx.Response(409 if r.get("code") == 40900 else 200, json=r)
        if path == school.PATH_MARKET_EXPERT_LIST:
            self.expert_hits += 1
            return httpx.Response(200, json={"code": 0, "data": {"experts": [{"expert_id": "ex_test_1", "display_name_zh": {"zh": "论小舟"}}]}})
        if path == growth_api.PATH_REPORT:
            events = json.loads(req.content)
            self.reports.append((host, events, path))
            if any(e.get("eventCode") == "chat_request_send" and e.get("source") == "mini_program" for e in events):
                self._advance("chat_3_times", 1)
            if any(e.get("ideName") == "WorkBuddy" for e in events):
                self._task("desktop_chat_1_time")["status"] = "completed"
                self._task("desktop_chat_1_time")["progress"] = 1
            if any(e.get("eventCode") == "expert_actual_use" for e in events):
                self._advance("expert_use", 1)
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(404, json={"code": 404})

    # ---------------- 工具 ----------------

    def _task(self, code):
        for t in self.tasks:
            if t.get("task_code") == code:
                return t
        raise AssertionError(f"无用例任务 {code}")

    def _advance(self, code, n):
        t = self._task(code)
        t["progress"] = int(t.get("progress") or 0) + n
        if t["progress"] >= int(t.get("target_count") or 1):
            t["status"] = "completed"


def _task(code, status="pending", progress=0, target=1, task_type="single"):
    return {
        "task_code": code,
        "status": status,
        "progress": progress,
        "target_count": target,
        "task_type": task_type,
        "reward_credit": 10,
        "title": code,
    }


def _runner(fake, **kw):
    kw.setdefault("gap", 0)
    return SchoolRunner(_Cred(), transport=httpx.MockTransport(fake.handler), **kw)


def test_out_of_period_skips_everything():
    fake = _Fake(in_period=False, tasks=[_task("share_invite")], balance=3)
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and "活动非进行期" in oc.summary, oc.summary
    assert fake.calls == [f"GET {httpx.URL(school.SCHOOL_BASE).host}{PATH_TASKS}"], fake.calls
    print("✅ test_out_of_period_skips_everything")


def test_manual_and_unknown_tasks_are_skipped():
    fake = _Fake(tasks=[_task("task_student_verify"), _task("brand_new_task")])
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and oc.detail["counters"]["skip"] == 2, oc.detail["counters"]
    steps = {s["task_code"]: s for s in oc.detail["steps"]}
    assert steps["task_student_verify"]["mode"] == "manual"
    assert "无分类证据" in steps["brand_new_task"]["reason"]
    assert not any("viewed" in c or "claim" in c for c in fake.calls), fake.calls
    print("✅ test_manual_and_unknown_tasks_are_skipped")


def test_share_task_viewed_then_share_complete_then_claim():
    fake = _Fake(tasks=[_task("share_invite", status="pending")])
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and oc.detail["counters"]["ok"] == 1, oc.detail["counters"]
    step = oc.detail["steps"][0]
    assert step["viewed"]["ok"] and step["after"] == "completed/1"
    assert step.get("claim", {}).get("ok") is True, step
    assert "点亮 1" in oc.summary, oc.summary
    paths = [c for c in fake.calls if c.startswith("POST")]
    assert any("viewed" in p for p in paths) and any("share-complete" in p for p in paths)
    assert any("share_invite/claim" in p for p in paths), paths
    print("✅ test_share_task_viewed_then_share_complete_then_claim")


def test_report_task_uses_mini_events_with_activity_id():
    fake = _Fake(tasks=[_task("chat_3_times", status="in_progress", progress=0, target=3)])
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and len(fake.reports) == 3, (oc.summary, len(fake.reports))
    host, events, _p = fake.reports[0]
    assert host == httpx.URL(school.SCHOOL_BASE).host and len(events) == 1
    assert events[0]["eventCode"] == "chat_request_send"
    assert events[0]["activityId"] == ACTIVITY_ID and events[0]["userId"] == "u-1"
    assert events[0]["source"] == "mini_program"
    assert oc.detail["steps"][0].get("claim", {}).get("ok") is True
    print("✅ test_report_task_uses_mini_events_with_activity_id")


def test_desktop_sequence_reports_to_copilot_with_desktop_headers():
    fake = _Fake(tasks=[_task("desktop_chat_1_time", status="in_progress")])
    seen = {}

    def handler(req):
        if req.url.path == growth_api.PATH_REPORT:
            seen["host"] = req.url.host
            seen["ua"] = req.headers.get("user-agent")
            seen["product"] = req.headers.get("x-product")
        return fake.handler(req)

    oc = SchoolRunner(_Cred(), gap=0, transport=httpx.MockTransport(handler)).run_once(reason="12:00")
    assert oc.ok, oc.summary
    body = [e for h, ev, _p in fake.reports if h == "copilot.tencent.com" for e in ev]
    assert len(body) == 6, len(body)                        # 桌面 6 连整体上报
    assert all(e["activityId"] == ACTIVITY_ID for e in body)
    assert seen["host"] == "copilot.tencent.com" and "WorkBuddy" in seen["ua"]
    assert seen["product"] == "SaaS"
    print("✅ test_desktop_sequence_reports_to_copilot_with_desktop_headers")


def test_expert_task_pulls_school_expert_first():
    fake = _Fake(tasks=[_task("expert_use", status="in_progress")])
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and fake.expert_hits == 1, (oc.summary, fake.expert_hits)
    ev = [e for _h, evs, _p in fake.reports for e in evs][0]
    assert ev["eventCode"] == "expert_actual_use" and ev["id"] == "ex_test_1"
    print("✅ test_expert_task_pulls_school_expert_first")


def test_lottery_draws_until_nochance():
    """抽奖抽到 40900（no chance）正常收尾，如实记录中奖物。"""
    seq = [
        {"code": 0, "data": {"prize_code": "school_credit_6", "credit_amount": 6, "chance_balance": 1}},
        {"code": 0, "data": {"prize_code": "school_voucher_luckin", "credit_amount": 0, "chance_balance": 0}},
    ]

    def draw(i, _bal):
        return seq[i - 1] if i <= len(seq) else {"code": 40900, "message": "no chance"}

    fake = _Fake(tasks=[], balance=2, draw=draw)
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and fake.draws == 2, (oc.summary, fake.draws)
    lot = oc.detail["lottery"]
    assert lot["draws"] == 2 and lot["credit"] == 6 and lot["ok"] is True
    assert lot["results"][1]["label"] == "瑞幸咖啡15元券"
    assert "抽奖 2 次（积分 +6）" in oc.summary, oc.summary
    print("✅ test_lottery_draws_until_nochance")


def test_lottery_stops_when_balance_stalls():
    """服务端余额不降 → 连续 3 次即提前停（防死循环）。"""
    fake = _Fake(
        tasks=[],
        balance=5,
        draw=lambda _i, bal: {"code": 0, "data": {"prize_code": "x", "credit_amount": 0, "chance_balance": bal}},
    )
    oc = _runner(fake).run_once(reason="12:00")
    assert fake.draws == school.STALL_LIMIT, fake.draws
    assert oc.detail["lottery"]["ok"] is False and oc.detail["lottery"]["balance"] == 5
    assert oc.ok, "抽奖未抽完不算任务失败（活动/服务端问题）"
    print("✅ test_lottery_stops_when_balance_stalls")


def test_lottery_zero_balance_is_noop():
    fake = _Fake(tasks=[], balance=0)
    oc = _runner(fake).run_once(reason="12:00")
    assert oc.ok and fake.draws == 0 and oc.detail["lottery"]["draws"] == 0
    print("✅ test_lottery_zero_balance_is_noop")


def test_gap_is_clamped_to_one_second():
    """写动作间隔下限 1s：配置成 0 也会被抬到 1s（防秒发风控）。"""
    r = SchoolRunner(_Cred(), gap=0)
    assert r.gap == MIN_GAP == 1.0
    assert SchoolRunner(_Cred(), gap=2.5).gap == 2.5
    print("✅ test_gap_is_clamped_to_one_second")


def test_task_list_failure_is_reported():
    fake = _Fake(tasks=[_task("share_invite")])

    def handler(req):
        if req.url.path == PATH_TASKS:
            return httpx.Response(500, json={"code": 500, "msg": "boom"})
        return fake.handler(req)

    oc = SchoolRunner(_Cred(), gap=1.0, transport=httpx.MockTransport(handler)).run_once(reason="12:00")
    assert not oc.ok and "拉活动任务失败" in oc.summary, oc.summary
    print("✅ test_task_list_failure_is_reported")


def test_missing_credential_and_task_table():
    oc = SchoolRunner(None).run_once()
    assert not oc.ok and "登录凭据" in oc.summary

    assert KNOWN_TASKS["task_student_verify"]["mode"] == "manual"
    assert KNOWN_TASKS["chat_3_times"]["report_kind"] == "mini_chat"
    assert school.DEFAULT_SCHOOL_HOURS == (12,)
    assert school.derive_device_id("u-1", "machine") == school.derive_device_id("u-1", "machine")
    assert school.derive_device_id("u-1", "machine") != school.derive_device_id("u-2", "machine")
    assert school.mask_token("abcdefghijkl") == "abcdefgh..."
    print("✅ test_missing_credential_and_task_table")


if __name__ == "__main__":
    t0 = _time.time()
    # 把 school 模块里的 sleep 换成空操作：写动作间隔是给上游看的，测试里不必真等
    school.time = _TimeShim(_time)

    test_out_of_period_skips_everything()
    test_manual_and_unknown_tasks_are_skipped()
    test_share_task_viewed_then_share_complete_then_claim()
    test_report_task_uses_mini_events_with_activity_id()
    test_desktop_sequence_reports_to_copilot_with_desktop_headers()
    test_expert_task_pulls_school_expert_first()
    test_lottery_draws_until_nochance()
    test_lottery_stops_when_balance_stalls()
    test_lottery_zero_balance_is_noop()
    test_gap_is_clamped_to_one_second()
    test_task_list_failure_is_reported()
    test_missing_credential_and_task_table()
    print(f"\n全部通过（{_time.time() - t0:.2f}s）")
