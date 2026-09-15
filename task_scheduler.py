#!/usr/bin/env python3
"""task_scheduler.py — 六类定时积分任务的统一排程（对应参考实现 internal/scheduler/scheduler.go）。

六类任务：签到 / 活跃上报 / 猫猫旅行 / token 保活 / 开学季 / 夜猫子。
**各自独立时点、独立开关**（`--xxx-hours` / `--no-xxx`，等价参考实现的 `schedule.*_enabled`），
互不影响；配到同一整点时当批并行派发（每类一个线程），慢任务不再阻塞同槽其他任务。

排程语义（与签到调度器同口径，六类共用一套）：
  - hours 缺省 / 空 / 非法 → 回落该类默认时点，**不是禁用**；真正关闭用 enabled=False；
  - 到点后按下发时点执行（不依赖唤醒时刻的小时数），休眠/挂起错过的班次在 6h 窗口内补跑；
  - 只看「今天 + 昨天」，睡过头超过窗口就不补，避免重启翻出昨天的班次；
  - 每类结果统一收敛成 TaskOutcome 并落盘留档（`tasks-log.json`），`/admin/tasks` 可查。

与参考实现的取舍：
  - 参考实现用 Go 的 `nextWake()` 挑最近时点、time.Timer 睡到点；这里用分段等待
    （≤30s 一轮）实现同样的语义，但能及时响应 stop()，也容忍系统休眠造成的时间跳变；
  - 参考实现是「账号池 × 多次重试」，本项目是「桌面端单一登录态」，所以没有账号轮转与
    熔断，单账号失败就是失败，如实记一条。

六类任务的执行体各自独立成模块（checkin / activity / travel / keepalive / school / blackcat），
本模块只做「什么时候跑、跑完留档、怎么查」，不掺业务逻辑。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from checkin import CheckinScheduler, DEFAULT_CHECKIN_HOURS, normalize_hours
from task_result import TaskOutcome, outcome_from_entry

import activity
import blackcat
import keepalive
import school
import travel

HISTORY_NAME = "tasks-log.json"
HISTORY_LIMIT = 200

#: 迟到唤醒的补跑窗口：时点距今超过该时长就不再补跑（与签到调度器同口径）。
DEFAULT_CATCHUP_WINDOW = 6 * 3600.0

#: 六类任务的展示名（key 与 CLI 参数 / 配置键同名）。
TASK_LABELS = {
    "checkin": "签到",
    "activity": "活跃上报",
    "travel": "猫猫旅行",
    "keepalive": "token 保活",
    "school": "开学季任务",
    "cat": "夜猫子任务",
}

#: 任务执行顺序（也是状态输出的顺序）。
TASK_KEYS = ("checkin", "activity", "travel", "keepalive", "school", "cat")


@dataclass
class TaskConfig:
    """六类任务的排程与执行参数（默认值即「开箱可用」的那一套）。"""

    checkin_hours: object = DEFAULT_CHECKIN_HOURS
    travel_hours: object = travel.DEFAULT_TRAVEL_HOURS
    activity_hours: object = activity.DEFAULT_ACTIVITY_HOURS
    keepalive_hours: object = keepalive.DEFAULT_KEEPALIVE_HOURS
    school_hours: object = school.DEFAULT_SCHOOL_HOURS
    cat_hours: object = blackcat.DEFAULT_CAT_HOURS
    #: 独立开关（缺省全开）。参考实现的语义：空数组/null 表示「未配置 → 回落默认」，
    #: 关闭必须用显式开关，两者不混淆。
    checkin_enabled: bool = True
    travel_enabled: bool = True
    activity_enabled: bool = True
    keepalive_enabled: bool = True
    school_enabled: bool = True
    cat_enabled: bool = True

    activity_report_count: int = activity.DEFAULT_REPORT_COUNT
    activity_report_gap: float = activity.DEFAULT_REPORT_GAP
    school_gap: float = school.DEFAULT_GAP
    travel_location_id: int = 4
    timeout: float = 60.0


_HOURS_FIELD = {
    "checkin": "checkin_hours",
    "travel": "travel_hours",
    "activity": "activity_hours",
    "keepalive": "keepalive_hours",
    "school": "school_hours",
    "cat": "cat_hours",
}
_ENABLED_FIELD = {
    "checkin": "checkin_enabled",
    "travel": "travel_enabled",
    "activity": "activity_enabled",
    "keepalive": "keepalive_enabled",
    "school": "school_enabled",
    "cat": "cat_enabled",
}
_DEFAULT_HOURS = {
    "checkin": DEFAULT_CHECKIN_HOURS,
    "travel": travel.DEFAULT_TRAVEL_HOURS,
    "activity": activity.DEFAULT_ACTIVITY_HOURS,
    "keepalive": keepalive.DEFAULT_KEEPALIVE_HOURS,
    "school": school.DEFAULT_SCHOOL_HOURS,
    "cat": blackcat.DEFAULT_CAT_HOURS,
}


class TaskScheduler(threading.Thread):
    """六类任务的后台排程线程。"""

    def __init__(
        self,
        cred,
        *,
        config: TaskConfig | None = None,
        cache_dir: Path | str | None = None,
        catchup_window: float = DEFAULT_CATCHUP_WINDOW,
        run_on_start: tuple[str, ...] | list[str] = (),
        transport: object | None = None,
        runners: dict | None = None,
        log: Callable[[str], None] | None = None,
    ):
        super().__init__(name="task-scheduler", daemon=True)
        self.cred = cred
        self.cfg = config or TaskConfig()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.catchup_window = float(catchup_window)
        self.run_on_start = tuple(run_on_start or ())
        self.transport = transport
        self._log = log or (lambda _m: None)

        self.hours: dict[str, tuple[int, ...]] = {
            k: normalize_hours(getattr(self.cfg, _HOURS_FIELD[k]), _DEFAULT_HOURS[k])
            for k in TASK_KEYS
        }
        self.enabled: dict[str, bool] = {
            k: bool(getattr(self.cfg, _ENABLED_FIELD[k])) for k in TASK_KEYS
        }

        #: runners 显式注入时原样采用（测试替身 / 自定义装配），否则按配置构建。
        self.runners = runners if runners is not None else self._build_runners()
        #: 签到执行体单独留一个引用：/admin/checkin 需要它（且要保持旧状态结构）。
        self.checkin_runner: CheckinScheduler | None = self.runners.get("checkin")  # type: ignore[assignment]

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fired: set[tuple[str, int]] = set()
        self.last: dict[str, dict] = {}
        self.history: list[dict] = self._load_history()

    # ------------------------------------------------------------------ 构建

    def _build_runners(self) -> dict:
        """按配置构建六类执行体。

        activity 需要「补满对话量后重试领养」，这个动作属于 travel（旅行状态机），
        但 activity 不该 import travel——所以在装配处注入回调（依赖方向单向）。
        """
        runners: dict = {}
        if self.enabled["checkin"] and self.cred is not None:
            runners["checkin"] = CheckinScheduler(
                self.cred,
                hours=self.hours["checkin"],
                enabled=True,
                timeout=self.cfg.timeout,
                cache_dir=self.cache_dir,
                log=self._log,
            )

        travel_runner = None
        if self.enabled["travel"]:
            travel_runner = travel.TravelRunner(
                self.cred,
                location_id=self.cfg.travel_location_id,
                timeout=self.cfg.timeout,
                transport=self.transport,
                log=self._log,
            )
            runners["travel"] = travel_runner

        if self.enabled["activity"]:
            runners["activity"] = activity.ActivityRunner(
                self.cred,
                count=self.cfg.activity_report_count,
                report_gap=self.cfg.activity_report_gap,
                timeout=self.cfg.timeout,
                transport=self.transport,
                log=self._log,
                adopt=travel_runner.adopt_force if travel_runner is not None else None,
            )

        if self.enabled["keepalive"]:
            runners["keepalive"] = keepalive.KeepaliveRunner(
                self.cred, log=self._log
            )

        if self.enabled["school"]:
            runners["school"] = school.SchoolRunner(
                self.cred,
                gap=self.cfg.school_gap,
                timeout=self.cfg.timeout,
                transport=self.transport,
                log=self._log,
            )

        if self.enabled["cat"]:
            runners["cat"] = blackcat.BlackCatRunner(
                self.cred,
                timeout=self.cfg.timeout,
                transport=self.transport,
                log=self._log,
            )
        return runners

    # ------------------------------------------------------------ 持久化

    @property
    def history_path(self) -> Path | None:
        return self.cache_dir / HISTORY_NAME if self.cache_dir else None

    def _load_history(self) -> list[dict]:
        p = self.history_path
        if not p:
            return []
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [x for x in raw if isinstance(x, dict)][-HISTORY_LIMIT:]

    def _append_history(self, entry: dict) -> None:
        self.history.append(entry)
        del self.history[:-HISTORY_LIMIT]
        p = self.history_path
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            self._log(f"[tasks] WARN 结果落盘失败：{e}")

    # ------------------------------------------------------------ 执行

    def run_task(self, key: str, reason: str = "manual") -> TaskOutcome:
        """立即执行某一类任务（不影响既定排程）。"""
        if key not in TASK_KEYS:
            raise KeyError(f"未知任务：{key}（可选 {', '.join(TASK_KEYS)}）")
        if not self.enabled.get(key):
            return TaskOutcome(key, False, f"{TASK_LABELS[key]}已关闭")
        runner = self.runners.get(key)
        if runner is None:
            reason_text = "未找到登录凭据" if self.cred is None else "执行体未构建"
            return TaskOutcome(key, False, f"{TASK_LABELS[key]}不可用：{reason_text}")
        return self._run_kind(key, reason, runner)

    def run_all_tasks(self, reason: str = "manual") -> list[TaskOutcome]:
        """立即执行全部已启用任务（互不阻塞，并行派发）。"""
        keys = [k for k in TASK_KEYS if k in self.runners]
        return self._fire(keys, reason)

    def _run_kind(self, key: str, reason: str, runner=None) -> TaskOutcome:
        runner = runner or self.runners[key]
        started = time.time()
        try:
            raw = runner.run_once(reason=reason)
        except Exception as e:  # noqa: BLE001 — 单类异常不该带崩排程线程
            oc = TaskOutcome(key, False, f"{TASK_LABELS[key]}执行异常：{e}")
        else:
            oc = outcome_from_entry(key, raw)
        entry = {"reason": reason, "elapsed_ms": int((time.time() - started) * 1000)}
        oc.detail = {**oc.detail, **entry}
        with self._lock:
            self.last[key] = oc.as_dict()
            self._append_history(oc.as_dict())
        if not oc.ok:
            self._log(f"[tasks] {TASK_LABELS[key]} 未成功：{oc.summary}")
        return oc

    def _fire(self, keys: list[str], reason: str) -> list[TaskOutcome]:
        """并行派发一批任务，等全部收尾。

        同一时点配了多类任务时（如签到与旅行都在 09 点），一个慢任务不能把其他任务顶到下一轮。
        """
        out: list[TaskOutcome] = []
        if not keys:
            return out
        threads: list[threading.Thread] = []
        for k in keys:
            t = threading.Thread(
                target=lambda kk: out.append(self._run_kind(kk, reason)),
                args=(k,),
                name=f"task-{k}",
                daemon=True,
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return out

    # ------------------------------------------------------------ 排程

    def _slots(self, key: str, date) -> list[tuple[float, tuple[str, int]]]:
        return [
            (datetime.combine(date, dtime(h, 0)).timestamp(), (date.isoformat(), h))
            for h in self.hours[key]
        ]

    def _next_fire(self, key: str, now: float) -> float | None:
        today = datetime.fromtimestamp(now).date()
        cands: list[float] = []
        for offset in (0, 1, 2):
            date = today + timedelta(days=offset)
            for ts, _k in self._slots(key, date):
                if ts > now:
                    cands.append(ts)
        return min(cands) if cands else None

    def _due_slots(self, now: float) -> list[tuple[float, str, int, str]]:
        """返回此刻应补跑的班次：(时点, 任务 key, 小时, 自然日)。

        只看「今天 + 昨天」，且时点距今不超过 catchup_window——
        既能补上休眠/挂起错过的班次，又不会让一次重启把昨天早上的班次也翻出来跑。

        班次标识按 **(任务, 自然日, 小时)** 三元组去重：多类任务常常配到同一整点
        （签到与旅行都在 09 点），只按「自然日+小时」去重会把后一类吃掉。
        """
        floor_ts = now - self.catchup_window
        today = datetime.fromtimestamp(now).date()
        out: list[tuple[float, str, int, str]] = []
        for key in TASK_KEYS:
            if key not in self.runners:
                continue
            for offset in (0, -1):
                date = today + timedelta(days=offset)
                for ts, (_day, hour) in self._slots(key, date):
                    if floor_ts <= ts <= now and (key, date.isoformat(), hour) not in self._fired:
                        out.append((ts, key, hour, date.isoformat()))
        out.sort(key=lambda x: (x[0], x[1]))
        return out

    def _prune_fired(self, now: float) -> None:
        cutoff = (datetime.fromtimestamp(now).date() - timedelta(days=3)).isoformat()
        self._fired = {k for k in self._fired if k[1] >= cutoff}

    def next_wake(self, now: float | None = None) -> tuple[float | None, list[str]]:
        """下一次唤醒时刻 + 该时刻要跑的任务 key 列表（参考实现 nextWake 的等价物）。"""
        now = now if now is not None else time.time()
        nxt: float | None = None
        for key in TASK_KEYS:
            if key not in self.runners:
                continue
            ts = self._next_fire(key, now)
            if ts is not None and (nxt is None or ts < nxt):
                nxt = ts
        if nxt is None:
            return None, []
        keys = []
        for key in TASK_KEYS:
            if key not in self.runners:
                continue
            ts = self._next_fire(key, now)
            if ts is not None and abs(ts - nxt) < 0.5:
                keys.append(key)
        return nxt, keys

    def run(self) -> None:
        if self.cred is None:
            self._log("[tasks] WARN 无可用凭据，任务排程不启动")
            return
        if not self.runners:
            self._log("[tasks] 六类任务全部关闭，排程线程不空转")
            return
        self._log(
            "[tasks] 排程启动："
            + "，".join(
                f"{TASK_LABELS[k]} {', '.join(f'{h:02d}:00' for h in self.hours[k])}"
                for k in TASK_KEYS
                if k in self.runners
            )
        )
        for key in self.run_on_start:
            if key in self.runners:
                self._run_kind(key, reason="startup")

        while not self._stop.is_set():
            now = time.time()
            due = self._due_slots(now)
            if due:
                # 同一时点的多类任务合成一批（并行），逐批处理完再继续
                batch_ts = due[0][0]
                batch = [x for x in due if abs(x[0] - batch_ts) < 0.5]
                for _ts, key, hour, day in batch:
                    self._fired.add((key, day, hour))
                keys = [x[1] for x in batch]
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(batch_ts))
                self._log(f"[tasks] 触发 {stamp}：{'、'.join(TASK_LABELS[k] for k in keys)}")
                self._fire(keys, reason=f"scheduled {datetime.fromtimestamp(batch_ts).strftime('%Y-%m-%d %H:%M')}")
                self._prune_fired(time.time())
                continue
            nxt, keys = self.next_wake(now)
            wait = max(1.0, (nxt - now)) if nxt else 3600.0
            # 分段等待：保证 stop() 及时生效，也容忍系统休眠导致的时间跳变
            self._stop.wait(min(wait, 30.0))

        self._log("[tasks] 排程线程退出")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 状态

    @staticmethod
    def _fmt(ts: float | None) -> str | None:
        if not ts:
            return None
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def task_status(self, key: str) -> dict:
        runner = self.runners.get(key)
        st = {
            "key": key,
            "label": TASK_LABELS[key],
            "enabled": bool(self.enabled.get(key)) and runner is not None,
            "hours": list(self.hours[key]),
            "next_fire_at": self._fmt(self._next_fire(key, time.time())) if runner else None,
            "last": self.last.get(key),
        }
        if runner is not None and hasattr(runner, "status"):
            try:
                st["runner"] = runner.status()
            except Exception as e:  # noqa: BLE001
                st["runner_error"] = str(e)
        return st

    def status(self) -> dict:
        nxt, keys = self.next_wake()
        return {
            "enabled": bool(self.runners),
            "alive": self.is_alive(),
            "next_wake_at": self._fmt(nxt),
            "next_wake_ts": int(nxt) if nxt else None,
            "next_wake_tasks": keys,
            "run_on_start": list(self.run_on_start),
            "catchup_window": self.catchup_window,
            "tasks": {k: self.task_status(k) for k in TASK_KEYS},
            "history_tail": self.history[-5:],
        }


__all__ = [
    "HISTORY_NAME",
    "HISTORY_LIMIT",
    "DEFAULT_CATCHUP_WINDOW",
    "TASK_KEYS",
    "TASK_LABELS",
    "TaskConfig",
    "TaskScheduler",
]
