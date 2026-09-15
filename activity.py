#!/usr/bin/env python3
"""activity.py — 对话活跃上报（对应参考实现 internal/scheduler/scheduler.go runActivity）。

做什么：向 billing 域 `POST /v2/report` 连发 N 条 `chat_request_send` 事件，
一条上报同时点亮 growth 连登天数、解锁领养前置任务（first_buddy）。
领猫前置要求 5 次对话，所以默认连发 5 条并**共用一个 conversationId**
（模拟同一会话内多轮对话），requestId 各自独立；条间间隔 1.5s，避免秒发触发风控。

跑完两件收尾（缺一不可）：
  1. **streak 自检**：回读 `GET /activity/growth/streak`。上游实测存在「上报 200 但静默丢弃」
     （事件缺 userId 时 progress 不动），只看 200 会把静默失败当成功，必须回读对账。
     days == 0 或回读失败 → WARN，但不重试（上报按天幂等，重试没意义）。
  2. **无猫立即重试领养**：上报把对话量刚补满，「门槛刚达成」是新状态，
     调用注入的 adopt 回调（travel.TravelRunner.adopt_force）就地闭环，不等下一趟旅行排程。

风控口径：每号每天 1 个时点（默认 10 点）即可，不做多时点高频上报。

依赖方向：本模块不 import travel —— 领养回调由上层（task_scheduler）注入，避免能力模块互相耦合。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import growth_api
from growth_api import DEFAULT_TIMEOUT
from task_result import TaskOutcome

KEY = "activity"

DEFAULT_ACTIVITY_HOURS: tuple[int, ...] = (10,)
#: 每号每次上报的条数：领猫前置需 5 次对话，默认 5 条把 chat_5 刷满。
DEFAULT_REPORT_COUNT = 5
#: 同一账号内连续上报的间隔（秒）：5 连发模拟同会话多轮，秒发易触发风控。
DEFAULT_REPORT_GAP = 1.5


def normalize_report_count(raw) -> int:
    """上报条数归一：0 / 负数 / 非法 → 1 条（兼容「每号每天 1 条只点亮连登」的旧行为）。

    与参考实现一致：**显式配 0 = 旧行为**，不是「禁用上报」，禁用请用 activity_enabled=false。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


class ActivityRunner:
    """单账号活跃上报。线程安全（定时与手动触发互斥）。"""

    def __init__(
        self,
        cred,
        *,
        count: int = DEFAULT_REPORT_COUNT,
        report_gap: float = DEFAULT_REPORT_GAP,
        timeout: float = DEFAULT_TIMEOUT,
        transport: object | None = None,
        log: Callable[[str], None] | None = None,
        adopt: Callable[[growth_api.Account], TaskOutcome] | None = None,
    ):
        self.cred = cred
        self.count = normalize_report_count(count)
        self.report_gap = max(0.0, float(report_gap))
        self.timeout = float(timeout)
        self.transport = transport
        self._log = log or (lambda _m: None)
        #: 上报把对话量补满后重试领养的回调（由 task_scheduler 注入 travel 的 adopt_force）。
        self._adopt = adopt

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
            return self._store(self._report_all(acct, reason))

    def status(self) -> dict:
        return {
            "key": KEY,
            "count": self.count,
            "report_gap": self.report_gap,
            "last": self.last.as_dict() if self.last else None,
        }

    # ------------------------------------------------------------------ 内部

    def _store(self, oc: TaskOutcome) -> TaskOutcome:
        self.last = oc
        self._log(f"[activity] {oc.summary}")
        return oc

    def _report_all(self, acct: growth_api.Account, reason: str) -> TaskOutcome:
        # N 条共用同一 conversationId（同一会话），requestId 各自独立（每条一个）。
        cid = f"wb2api-{int(time.time() * 1000)}"
        detail: dict = {"reason": reason, "uid": acct.uid, "conversation_id": cid, "sent": 0}
        sent = 0
        for i in range(1, self.count + 1):
            res = growth_api.report_chat_activity(
                acct,
                cid,
                f"{cid}-r{i}",
                timeout=self.timeout,
                transport=self.transport,
            )
            if not res.ok:
                # 本号上报失败：不再续发（streak 自检与领养都失去意义）
                detail["error"] = res.summary()
                detail["sent"] = sent
                return TaskOutcome(
                    KEY, False, f"上报 {i}/{self.count} 失败：{res.summary()}", detail
                )
            sent += 1
            if i < self.count:
                time.sleep(self.report_gap)

        detail["sent"] = sent

        streak_days, streak_ok = self._check_streak(acct)
        detail["streak_days"] = streak_days
        detail["streak_ok"] = streak_ok

        adopt_summary = ""
        if self._adopt is not None:
            try:
                adopt_oc = self._adopt(acct)
                adopt_summary = adopt_oc.summary
                detail["adopt"] = adopt_oc.as_dict()
            except Exception as e:  # noqa: BLE001 — 领养失败不该抹掉已成功的上报
                adopt_summary = f"领养回调异常：{e}"
                detail["adopt"] = {"ok": False, "summary": adopt_summary}

        summary = f"上报 {sent}/{self.count} 成功"
        if streak_days is not None:
            summary += f"，连登 {streak_days} 天"
        if not streak_ok:
            summary += "（⚠️ streak 回读异常，疑似上报被静默丢弃）"
        if adopt_summary:
            summary += f"；{adopt_summary}"
        return TaskOutcome(KEY, True, summary, detail)

    def _check_streak(self, acct: growth_api.Account) -> tuple[int | None, bool]:
        """回读连登天数（只读 oracle）。返回 (days, 是否正常)。

        days == 0 → 可疑（`report OK but streak.days=0 (silent drop?)`）；
        回读失败 → 也可疑，但不影响主流程（上报本身已成功，按天幂等，不重试）。
        """
        res = growth_api.fetch_streak(acct, timeout=self.timeout, transport=self.transport)
        if not res.ok:
            self._log(f"[activity] WARN streak 回读失败（上报已成功）：{res.summary()}")
            return None, False
        days = int(res.data or 0)
        if days == 0:
            self._log("[activity] WARN 上报 200 但 streak.days=0（疑似被静默丢弃，检查 userId）")
            return 0, False
        return days, True


__all__ = [
    "KEY",
    "DEFAULT_ACTIVITY_HOURS",
    "DEFAULT_REPORT_COUNT",
    "DEFAULT_REPORT_GAP",
    "normalize_report_count",
    "ActivityRunner",
]
