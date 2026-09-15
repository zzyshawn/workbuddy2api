#!/usr/bin/env python3
"""blackcat.py — 夜猫子任务（对应参考实现 scripts/task_runner.py 的 `--only black_cat`）。

上游把它当成第六类定时任务（默认 01 点）跑，做的事是「在夜猫窗口内把 black_cat
任务补一次上报，凑满进度后领奖」：

    GET  /v2/activity/growth/tasks                        读任务状态（accept_status / progress）
    POST /v2/activity/growth/tasks/accept                 {"task_codes":["black_cat"]}（未接时）
    POST {billing}/v2/report                              1 条 chat_request_send（GLM-5.2, mode=night）
    POST {growth}/activity/growth/tasks/black_cat/claim   领奖（400 时降级 web 域）

**时段敏感**是本任务的灵魂：夜猫窗口为 CST 23:00–08:00，窗口外一律不发写请求
（上游窗口外上报不计入进度，白打还吃风控）。窗口内每次最多补 1 条（cap=1），
剩下的交给后续时点或次日窗口——这条 cap 是参考实现明确写死的行为，不要放开。

幂等与失败语义：
  - 已领（accept_status=claimed）→ 跳过；
  - 已满（progress 到 target 或 completed）→ 直接 claim（服务端 already_claimed 幂等，算 ok）；
  - 上报后回读，只有进度真的满了才 claim，未满如实标注 WARN，不强行刷。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import growth_api
from growth_api import DEFAULT_TIMEOUT, NIGHT_MODEL_ID, NIGHT_MODEL_NAME
from task_result import TaskOutcome

KEY = "cat"

DEFAULT_CAT_HOURS: tuple[int, ...] = (1,)

#: 任务码（上游 task_runner.py 的映射表键）。
TASK_CODE = "black_cat"
#: 兜底目标次数（服务端 progress.target 缺失时用；任务列表里一般是 3）。
DEFAULT_TARGET = 3

#: 夜猫窗口（CST）：23:00 起，到次日 08:00 前。
NIGHT_WINDOW_START_HOUR = 23
NIGHT_WINDOW_END_HOUR = 8

#: 每次进任务最多补的上报条数（参考实现 cap=1）。
REPORT_CAP = 1

#: 上报后的等待秒数：上游对上报事件的**归账是异步的**，立刻回读会拿到旧进度
#: （参考实现 task_runner.py 在回读前固定 sleep 2.0 秒，实测照抄）。
SETTLE_SECONDS = 2.0


def in_night_window(ts: float | None = None) -> bool:
    """当前（或给定时刻）是否落在夜猫窗口 23:00–08:00 CST 内。"""
    hour = growth_api.cst_now(ts).hour
    return hour >= NIGHT_WINDOW_START_HOUR or hour < NIGHT_WINDOW_END_HOUR


class BlackCatRunner:
    """单账号夜猫子任务。线程安全（定时与手动触发互斥）。"""

    def __init__(
        self,
        cred,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        settle: float = SETTLE_SECONDS,
        transport: object | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.cred = cred
        self.timeout = float(timeout)
        self.settle = max(0.0, float(settle))
        self.transport = transport
        self._log = log or (lambda _m: None)

        self._lock = threading.Lock()
        self.last: TaskOutcome | None = None

    # ------------------------------------------------------------------ 入口

    def run_once(self, reason: str = "manual") -> TaskOutcome:
        with self._lock:
            if self.cred is None:
                return self._store(
                    TaskOutcome(KEY, False, "未找到登录凭据（请先在桌面端登录 CodeBuddy / WorkBuddy 后重启）")
                )
            try:
                acct = growth_api.account_of(self.cred, log=self._log)
            except Exception as e:  # noqa: BLE001
                return self._store(TaskOutcome(KEY, False, f"取凭据/刷新失败：{e}"))
            return self._store(self._run(acct, reason))

    def status(self) -> dict:
        return {
            "key": KEY,
            "task_code": TASK_CODE,
            "in_night_window": in_night_window(),
            "night_window": f"{NIGHT_WINDOW_START_HOUR:02d}:00–{NIGHT_WINDOW_END_HOUR:02d}:00 CST",
            "last": self.last.as_dict() if self.last else None,
        }

    # ------------------------------------------------------------------ 内部

    def _store(self, oc: TaskOutcome) -> TaskOutcome:
        self.last = oc
        self._log(f"[cat] {oc.summary}")
        return oc

    def _run(self, acct: growth_api.Account, reason: str) -> TaskOutcome:
        detail: dict = {"reason": reason, "uid": acct.uid, "task_code": TASK_CODE}

        tasks = growth_api.list_tasks(acct, timeout=self.timeout, transport=self.transport)
        if not tasks.ok:
            return TaskOutcome(KEY, False, f"拉任务列表失败：{tasks.summary()}", detail)

        task = growth_api.find_task(tasks.data, TASK_CODE)
        if task is None:
            detail["state"] = "missing"
            return TaskOutcome(KEY, True, "任务列表里没有 black_cat（活动未开始或已下线），跳过", detail)

        accept_status = str(task.get("accept_status") or "?")
        progress = task.get("progress") or {}
        cur = int(progress.get("current") or 0)
        target = int(progress.get("target") or DEFAULT_TARGET)
        detail.update(
            {"accept_status": accept_status, "current": cur, "target": target, "progress": f"{cur}/{target}"}
        )

        if accept_status == "claimed":
            return TaskOutcome(KEY, True, f"black_cat 已领取（{cur}/{target}），跳过", detail)

        if cur >= target or accept_status == "completed":
            return self._claim(acct, cur, target, detail, reason="任务已完成")

        window = in_night_window()
        detail["in_night_window"] = window
        if not window:
            detail["state"] = "out-of-window"
            return TaskOutcome(
                KEY,
                True,
                f"非夜猫窗口（{NIGHT_WINDOW_START_HOUR:02d}:00–{NIGHT_WINDOW_END_HOUR:02d}:00 CST），"
                f"本趟跳过（{cur}/{target}）",
                detail,
            )

        if accept_status == "not_accepted":
            acc = growth_api.accept_tasks(
                acct, [TASK_CODE], timeout=self.timeout, transport=self.transport
            )
            detail["accept"] = {"ok": acc.ok, "status": acc.status, "code": acc.code}
            if not acc.ok:
                return TaskOutcome(KEY, False, f"接任务失败：{acc.summary()}", detail)

        need = min(max(0, target - cur), REPORT_CAP)
        sent = 0
        for _ in range(need):
            cid = f"wb-cat-{int(time.time() * 1000)}"
            res = growth_api.report_chat_activity(
                acct,
                cid,
                cid,
                model_id=NIGHT_MODEL_ID,
                model_name=NIGHT_MODEL_NAME,
                mode="night",
                timeout=self.timeout,
                transport=self.transport,
            )
            if not res.ok:
                detail.update({"sent": sent, "error": res.summary()})
                return TaskOutcome(KEY, False, f"夜猫上报失败：{res.summary()}", detail)
            sent += 1
        detail["sent"] = sent

        # 回读确认：上游归账是异步的，先等 settle 秒，只有进度真的满了才 claim，未满如实标注、不强行刷
        if sent and self.settle:
            time.sleep(self.settle)
        after = growth_api.list_tasks(acct, timeout=self.timeout, transport=self.transport)
        if not after.ok:
            return TaskOutcome(KEY, False, f"回读任务失败：{after.summary()}", detail)
        task2 = growth_api.find_task(after.data, TASK_CODE) or {}
        prog2 = task2.get("progress") or {}
        cur2 = int(prog2.get("current") or 0)
        target2 = int(prog2.get("target") or target)
        status2 = str(task2.get("accept_status") or "?")
        detail.update({"after": f"{cur2}/{target2}", "accept_status_after": status2})

        if status2 == "claimed":
            return TaskOutcome(KEY, True, f"black_cat 已领取（{cur2}/{target2}）", detail)
        if status2 == "completed" or cur2 >= target2:
            return self._claim(acct, cur2, target2, detail, reason=f"补 {sent} 条后进度已满")
        return TaskOutcome(
            KEY,
            True,
            f"补了 {sent} 条上报，进度 {cur2}/{target2}（本趟上限 {REPORT_CAP} 条，未满属预期）",
            detail,
        )

    def _claim(
        self,
        acct: growth_api.Account,
        cur: int,
        target: int,
        detail: dict,
        *,
        reason: str,
    ) -> TaskOutcome:
        res = growth_api.claim_task(acct, TASK_CODE, timeout=self.timeout, transport=self.transport)
        detail["claim"] = {"ok": res.ok, "status": res.status, "code": res.code, "data": res.data}
        if not res.ok:
            return TaskOutcome(KEY, False, f"领奖失败：{res.summary()}", detail)
        data = res.data if isinstance(res.data, dict) else {}
        if data.get("already_claimed"):
            return TaskOutcome(
                KEY, True, f"black_cat 已领过（幂等命中，credit=+{data.get('credit') or 0}）", detail
            )
        return TaskOutcome(
            KEY,
            True,
            f"black_cat 领奖成功（{reason}，{cur}/{target}，"
            f"credit=+{data.get('credit') or 0} energy=+{data.get('energy') or 0}）",
            detail,
        )


__all__ = [
    "KEY",
    "DEFAULT_CAT_HOURS",
    "TASK_CODE",
    "DEFAULT_TARGET",
    "NIGHT_WINDOW_START_HOUR",
    "NIGHT_WINDOW_END_HOUR",
    "REPORT_CAP",
    "SETTLE_SECONDS",
    "in_night_window",
    "BlackCatRunner",
]
