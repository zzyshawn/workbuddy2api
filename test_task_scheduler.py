#!/usr/bin/env python3
"""test_task_scheduler.py — 验证六类任务统一排程：时点、补跑窗口、同槽并行、独立开关、留档。

直接运行：python3 test_task_scheduler.py
"""

import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from task_result import TaskOutcome  # noqa: E402
from task_scheduler import (  # noqa: E402
    DEFAULT_CATCHUP_WINDOW,
    HISTORY_NAME,
    TASK_KEYS,
    TaskConfig,
    TaskScheduler,
)


class _Runner:
    """假执行体：记录调用、可注入耗时与失败；status() 供管理员端点取细节。"""

    def __init__(self, key, *, ok=True, delay=0.0, entry=None):
        self.key = key
        self.ok = ok
        self.delay = delay
        self.entry = entry
        self.calls: list[str] = []

    def run_once(self, reason="manual"):
        self.calls.append(reason)
        if self.delay:
            time.sleep(self.delay)
        if self.entry is not None:
            return self.entry
        return TaskOutcome(self.key, self.ok, f"{self.key} {'完成' if self.ok else '失败'}")

    def status(self):
        return {"key": self.key}


def _sched(runners, **cfg_kw):
    cfg_kw.setdefault("school_gap", 0)
    cfg = TaskConfig(**cfg_kw)
    tmp = tempfile.mkdtemp()
    return TaskScheduler(object(), config=cfg, cache_dir=tmp, runners=runners)


def test_hours_defaults_and_normalization():
    s = _sched({})
    assert s.hours["checkin"] == (9, 21)
    assert s.hours["travel"] == (9, 21)
    assert s.hours["activity"] == (10,)
    assert s.hours["keepalive"] == (22,)
    assert s.hours["school"] == (12,)
    assert s.hours["cat"] == (1,)
    assert s.cfg.activity_report_count == 5

    # 空 / 非法 → 回落该类默认（不是禁用）；显式 0 点生效
    s2 = _sched({}, activity_hours="", cat_hours=["x", 99], school_hours="0")
    assert s2.hours["activity"] == (10,) and s2.hours["cat"] == (1,)
    assert s2.hours["school"] == (0,)
    print("✅ test_hours_defaults_and_normalization")


def test_independent_switches():
    s = _sched({"checkin": _Runner("checkin"), "cat": _Runner("cat")}, travel_enabled=False, school_enabled=False)
    assert s.enabled["travel"] is False and s.enabled["cat"] is True
    st = s.status()
    assert st["tasks"]["travel"]["enabled"] is False
    assert st["tasks"]["checkin"]["enabled"] is True
    assert [k for k in TASK_KEYS if st["tasks"][k]["enabled"]] == ["checkin", "cat"]

    # 关闭的任务 run_task 直接返回不可用，不调用执行体
    oc = s.run_task("travel")
    assert not oc.ok and "已关闭" in oc.summary
    print("✅ test_independent_switches")


def test_next_wake_groups_same_hour_kinds():
    """签到与旅行都在 09/21 → 同一时刻应一并唤醒（参考实现 nextWake 语义）。"""
    s = _sched({"checkin": _Runner("checkin"), "travel": _Runner("travel"), "activity": _Runner("activity")})
    base = datetime(2026, 9, 16, 8, 30, 0).timestamp()
    nxt, keys = s.next_wake(base)
    assert datetime.fromtimestamp(nxt).strftime("%H:%M") == "09:00", nxt
    assert set(keys) == {"checkin", "travel"}, keys

    # 10 点只剩活跃上报
    after9 = datetime(2026, 9, 16, 9, 30, 0).timestamp()
    nxt2, keys2 = s.next_wake(after9)
    assert datetime.fromtimestamp(nxt2).strftime("%H:%M") == "10:00" and keys2 == ["activity"]
    print("✅ test_next_wake_groups_same_hour_kinds")


def test_due_slots_catchup_window_and_dedupe():
    s = _sched({"checkin": _Runner("checkin"), "travel": _Runner("travel")})
    # 21:10 醒来：21:00 的班次在 6h 窗口内 → 签到与旅行都要补跑
    wake = datetime(2026, 9, 16, 21, 10, 0).timestamp()
    due = s._due_slots(wake)
    assert [(k, h) for _ts, k, h, _d in due] == [("checkin", 21), ("travel", 21)], due

    # 标记已跑后不再重复（两类各自独立记账，不会互相吃掉）
    for _ts, k, h, d in due:
        s._fired.add((k, d, h))
    assert s._due_slots(wake) == []

    # 补跑窗口边界（只配 21 点，避开「今天 9 点刚落在这个钟点」的干扰）：
    s2 = _sched({"checkin": _Runner("checkin")}, checkin_hours="21")
    assert DEFAULT_CATCHUP_WINDOW == 6 * 3600.0
    # 5 小时后醒来：21:00 在窗口内 → 补
    assert s2._due_slots(datetime(2026, 9, 17, 2, 0, 0).timestamp()) == [
        (datetime(2026, 9, 16, 21, 0, 0).timestamp(), "checkin", 21, "2026-09-16")
    ]
    # 7 小时后醒来：超出窗口 → 不补（重启不该把昨天早班翻出来）
    assert s2._due_slots(datetime(2026, 9, 17, 4, 0, 0).timestamp()) == []
    # 12 小时后醒来同样不补
    assert s2._due_slots(datetime(2026, 9, 17, 9, 30, 0).timestamp()) == []
    print("✅ test_due_slots_catchup_window_and_dedupe")


def test_fire_dispatches_parallel():
    """同一批多类任务并行派发：总耗时约等于最慢的一个，而不是逐个相加。"""
    s = _sched({"activity": _Runner("activity", delay=0.25), "school": _Runner("school", delay=0.25)})
    t0 = time.time()
    out = s.run_all_tasks(reason="manual")
    elapsed = time.time() - t0
    assert len(out) == 2 and elapsed < 0.45, elapsed
    print(f"✅ test_fire_dispatches_parallel（{elapsed:.2f}s）")


def test_run_task_records_history_and_last():
    r = _Runner("activity")
    s = _sched({"activity": r})
    oc = s.run_task("activity", reason="manual")
    assert oc.ok and r.calls == ["manual"]
    assert s.status()["tasks"]["activity"]["last"]["summary"] == "activity 完成"
    assert s.status()["tasks"]["activity"]["last"]["detail"]["reason"] == "manual"

    # 落盘留档 + 新实例可读回
    p = s.cache_dir / HISTORY_NAME
    assert p.exists()
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved[-1]["key"] == "activity"

    s2 = TaskScheduler(object(), cache_dir=s.cache_dir, runners={"activity": r})
    assert len(s2.history) >= 1 and s2.history[-1]["key"] == "activity"
    print("✅ test_run_task_records_history_and_last")


def test_run_task_unknown_key_and_runner_exception():
    s = _sched({"activity": _Runner("activity")})
    try:
        s.run_task("nope")
    except KeyError as e:
        assert "未知任务" in str(e)
    else:
        raise AssertionError("未知任务应抛 KeyError")

    class _Boom:
        def run_once(self, reason="manual"):
            raise RuntimeError("炸了")

    s2 = _sched({"activity": _Boom()})
    oc = s2.run_task("activity")
    assert not oc.ok and "执行异常" in oc.summary, oc.summary
    print("✅ test_run_task_unknown_key_and_runner_exception")


def test_checkin_style_dict_entry_is_adapted():
    """签到执行体返回裸 dict（历史遗留），要能收敛成 TaskOutcome。"""
    entry = {"at": time.time(), "reason": "manual", "ok": True, "already": False, "balance": 2680}
    s = _sched({"checkin": _Runner("checkin", entry=entry)})
    oc = s.run_task("checkin")
    assert oc.ok and "可用积分 2680" in oc.summary, oc.summary

    entry2 = {"at": time.time(), "reason": "manual", "ok": True, "already": True, "msg": "今天已签到"}
    s2 = _sched({"checkin": _Runner("checkin", entry=entry2)})
    assert s2.run_task("checkin").summary == "今日已签到"
    print("✅ test_checkin_style_dict_entry_is_adapted")


def test_thread_exits_when_all_disabled_or_no_cred():
    s = _sched({}, checkin_enabled=False)
    s.start()
    s.join(timeout=3)
    assert not s.is_alive()

    s2 = TaskScheduler(None, runners={})
    s2.start()
    s2.join(timeout=3)
    assert not s2.is_alive()
    print("✅ test_thread_exits_when_all_disabled_or_no_cred")


def test_thread_runs_due_slot_and_stops():
    """把一个时点放到「刚刚」：排程线程启动后应补跑一次，并能被 stop() 立刻叫停。"""
    r = _Runner("activity")
    tmp = tempfile.mkdtemp()
    s = TaskScheduler(
        object(),
        config=TaskConfig(activity_hours=datetime.now().hour),
        cache_dir=tmp,
        runners={"activity": r},
        log=lambda _m: None,
    )
    s.start()
    deadline = time.time() + 5
    while time.time() < deadline and not r.calls:
        time.sleep(0.05)
    s.stop()
    s.join(timeout=3)
    assert r.calls, "到点（或补跑窗口内）应触发一次"
    assert not s.is_alive()
    assert "activity" in s.last
    print("✅ test_thread_runs_due_slot_and_stops")


def test_run_on_start_only_runs_listed_keys():
    r_ck, r_cat = _Runner("checkin"), _Runner("cat")
    tmp = tempfile.mkdtemp()
    s = TaskScheduler(
        object(),
        config=TaskConfig(checkin_hours=23, cat_hours=22),
        cache_dir=tmp,
        runners={"checkin": r_ck, "cat": r_cat},
        run_on_start=("checkin",),
        log=lambda _m: None,
    )
    s.start()
    time.sleep(0.3)
    s.stop()
    s.join(timeout=3)
    # 只有 checkin 被要求「启动即跑一次」；cat 即使因补跑窗口触发，也不该带 startup 理由
    assert "startup" in r_ck.calls, r_ck.calls
    assert "startup" not in r_cat.calls, r_cat.calls
    assert r_ck.calls.count("startup") == 1, r_ck.calls
    print("✅ test_run_on_start_only_runs_listed_keys")


def test_status_shape_is_json_serializable():
    s = _sched({"checkin": _Runner("checkin"), "travel": _Runner("travel")})
    st = s.status()
    json.dumps(st, ensure_ascii=False)
    assert set(st["tasks"]) == set(TASK_KEYS)
    assert st["enabled"] is True and st["alive"] is False
    assert st["next_wake_tasks"] and st["next_wake_at"]
    assert st["tasks"]["travel"]["next_fire_at"]
    assert st["tasks"]["travel"]["label"] == "猫猫旅行"
    assert isinstance(threading.active_count(), int)
    print("✅ test_status_shape_is_json_serializable")


if __name__ == "__main__":
    t0 = time.time()
    test_hours_defaults_and_normalization()
    test_independent_switches()
    test_next_wake_groups_same_hour_kinds()
    test_due_slots_catchup_window_and_dedupe()
    test_fire_dispatches_parallel()
    test_run_task_records_history_and_last()
    test_run_task_unknown_key_and_runner_exception()
    test_checkin_style_dict_entry_is_adapted()
    test_thread_exits_when_all_disabled_or_no_cred()
    test_thread_runs_due_slot_and_stops()
    test_run_on_start_only_runs_listed_keys()
    test_status_shape_is_json_serializable()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
