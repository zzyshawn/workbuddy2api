#!/usr/bin/env python3
"""test_activity.py — 验证活跃上报：同会话多轮上报、失败即停、streak 自检与领养回调。

直接运行：python3 test_activity.py
"""

import json
import sys

import httpx

sys.path.insert(0, ".")

import growth_api  # noqa: E402
from activity import (  # noqa: E402
    DEFAULT_REPORT_COUNT,
    ActivityRunner,
    normalize_report_count,
)
from task_result import TaskOutcome  # noqa: E402


class _Cred:
    def get_headers(self, within_ms=60_000):
        return {"Authorization": "Bearer t", "X-User-Id": "u-1"}

    def summary(self):
        return {"nickname": "测试号"}


class _Recorder:
    """记录 /v2/report 的 body 与 streak 是否被回读。"""

    def __init__(self, fail_at=None, streak_days=6, streak_ok=True):
        self.posts: list[list[dict]] = []
        self.streak_reads = 0
        self.fail_at = fail_at
        self.streak_days = streak_days
        self.streak_ok = streak_ok

    def handler(self, req):
        path = req.url.path
        if path == growth_api.PATH_REPORT:
            if self.fail_at is not None and len(self.posts) + 1 == self.fail_at:
                return httpx.Response(500, json={"code": 500, "msg": "boom"})
            self.posts.append(json.loads(req.content))
            return httpx.Response(200, json={"code": 0})
        if path == growth_api.PATH_STREAK:
            self.streak_reads += 1
            if not self.streak_ok:
                return httpx.Response(500, json={"code": 500})
            return httpx.Response(200, json={"code": 0, "data": {"streak": {"days": self.streak_days}}})
        return httpx.Response(404, json={"code": 404})


def _runner(rec, **kw):
    kw.setdefault("count", 5)
    kw.setdefault("report_gap", 0)
    return ActivityRunner(_Cred(), transport=httpx.MockTransport(rec.handler), **kw)


def test_reports_share_conversation_and_keep_request_ids_distinct():
    rec = _Recorder()
    adopt_calls = []
    r = _runner(rec, adopt=lambda acct: (adopt_calls.append(acct.uid), TaskOutcome("travel", True, "已领养"))[1])
    oc = r.run_once(reason="10:00")

    assert oc.ok and oc.detail["sent"] == 5, oc.detail
    assert len(rec.posts) == 5
    cids = [p[0]["conversationId"] for p in rec.posts]
    rids = [p[0]["requestId"] for p in rec.posts]
    assert len(set(cids)) == 1, cids               # 同一会话
    assert len(set(rids)) == 5, rids               # 各自独立的 requestId
    assert cids[0].startswith("wb2api-") and rids[0].startswith(cids[0])
    assert all(p[0]["userId"] == "u-1" for p in rec.posts)   # 缺 userId 会被静默丢弃
    assert rec.streak_reads == 1
    assert adopt_calls == ["u-1"]
    assert "上报 5/5 成功" in oc.summary and "连登 6 天" in oc.summary, oc.summary
    print("✅ test_reports_share_conversation_and_keep_request_ids_distinct")


def test_failure_stops_further_reports_and_skips_followups():
    """任一条失败：不续发、不做 streak 自检、不触发领养。"""
    rec = _Recorder(fail_at=3)
    adopt_calls = []
    r = _runner(rec, adopt=lambda acct: (adopt_calls.append(1), TaskOutcome("travel", True, "x"))[1])
    oc = r.run_once(reason="10:00")
    assert not oc.ok and oc.detail["sent"] == 2, oc.detail
    assert len(rec.posts) == 2 and rec.streak_reads == 0 and adopt_calls == []
    assert "上报 3/5 失败" in oc.summary, oc.summary
    print("✅ test_failure_stops_further_reports_and_skips_followups")


def test_streak_zero_is_flagged_but_not_failed():
    """上报 200 但 streak 回读为 0 → 告警（疑似静默丢弃），本次上报本身仍算成功。"""
    rec = _Recorder(streak_days=0)
    oc = _runner(rec).run_once()
    assert oc.ok and oc.detail["streak_days"] == 0 and oc.detail["streak_ok"] is False
    assert "疑似上报被静默丢弃" in oc.summary, oc.summary

    rec2 = _Recorder(streak_ok=False)
    oc2 = _runner(rec2).run_once()
    assert oc2.ok and oc2.detail["streak_ok"] is False and oc2.detail["streak_days"] is None
    print("✅ test_streak_zero_is_flagged_but_not_failed")


def test_report_count_normalization():
    assert normalize_report_count(0) == 1
    assert normalize_report_count(-3) == 1
    assert normalize_report_count("x") == 1
    assert normalize_report_count(None) == 1
    assert normalize_report_count(5) == 5
    assert DEFAULT_REPORT_COUNT == 5

    rec = _Recorder()
    oc = _runner(rec, count=0).run_once()
    assert oc.ok and oc.detail["sent"] == 1 and len(rec.posts) == 1
    print("✅ test_report_count_normalization")


def test_adopt_callback_exception_does_not_undo_report():
    def boom(_acct):
        raise RuntimeError("领养炸了")

    rec = _Recorder()
    oc = _runner(rec, adopt=boom).run_once()
    assert oc.ok and "领养回调异常" in oc.summary, oc.summary
    print("✅ test_adopt_callback_exception_does_not_undo_report")


def test_missing_credential():
    r = ActivityRunner(None, transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    oc = r.run_once()
    assert not oc.ok and "登录凭据" in oc.summary
    print("✅ test_missing_credential")


if __name__ == "__main__":
    import time

    t0 = time.time()
    test_reports_share_conversation_and_keep_request_ids_distinct()
    test_failure_stops_further_reports_and_skips_followups()
    test_streak_zero_is_flagged_but_not_failed()
    test_report_count_normalization()
    test_adopt_callback_exception_does_not_undo_report()
    test_missing_credential()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
