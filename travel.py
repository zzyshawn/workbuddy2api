#!/usr/bin/env python3
"""travel.py — 猫猫旅行巡检状态机（对应参考实现 internal/scheduler/travel.go）。

每个旅行时点（默认 09 / 21 点）对账号推进**一趟**状态机，不轮询、不等待：

    查猫档案  GET /activity/growth/buddy/info
      无猫 → 同意协议 + 领养（POST buddy/agreement → buddy/first）
      有猫 → 查旅行状态 GET /activity/growth/buddy/travel/status
               arrived   → 领奖（POST travel/claim，必须带 record_id）
               idle      → 派出（POST travel/depart，地点固定 4）
               traveling → 跳过（在途，等下一趟到站）
               其他      → 跳过并如实记日志

两个刻意保留的语义（照抄参考实现的设计边界）：
  - **领养防抖**：门槛未达标（HTTP 400 `first_buddy task not completed yet`）是预期行为，
    记「当日已试」后当天不再重试，避免同日多趟对上游轰炸；进程重启即清零（无需持久化）。
  - **本任务不刷 token**：查询失败只跳过本轮（401 交 token 保活任务处理），
    免得旅行巡检因刷新接口抖动整体失败。

对外入口：
    TravelRunner.run_once()      走一趟状态机（调度器按时点调用 / 管理员手动触发）
    TravelRunner.adopt_force()   活跃上报把对话量补满后立即重试领养（豁免当日防抖）
"""

from __future__ import annotations

import threading
from typing import Callable

import growth_api
from growth_api import DEFAULT_TIMEOUT, DEFAULT_TRAVEL_LOCATION_ID
from task_result import TaskOutcome

KEY = "travel"

#: 默认排程时点：09 点派出、21 点领奖，一趟派出 + 一趟领奖闭环。
DEFAULT_TRAVEL_HOURS: tuple[int, ...] = (9, 21)

#: 状态机状态（上游实测值）。
STATE_IDLE = "idle"
STATE_TRAVELING = "traveling"
STATE_ARRIVED = "arrived"


class TravelRunner:
    """单账号猫猫旅行巡检。线程安全（run_once 与管理员手动触发互斥）。"""

    def __init__(
        self,
        cred,
        *,
        location_id: int = DEFAULT_TRAVEL_LOCATION_ID,
        timeout: float = DEFAULT_TIMEOUT,
        transport: object | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.cred = cred
        self.location_id = int(location_id)
        self.timeout = float(timeout)
        self.transport = transport
        self._log = log or (lambda _m: None)

        self._lock = threading.Lock()
        #: 领养当日失败记录：uid → CST 自然日。门槛未达的账号当日不再重试。
        self._adopt_tried: dict[str, str] = {}
        self.last: TaskOutcome | None = None

    # ------------------------------------------------------------------ 入口

    def run_once(self, reason: str = "manual") -> TaskOutcome:
        """走一趟旅行巡检；任何异常都收敛成 TaskOutcome，不向外抛。"""
        with self._lock:
            acct, err = self._account()
            if err:
                return self._store(TaskOutcome(KEY, False, err))
            return self._store(self._travel_one(acct, reason))

    def adopt_force(
        self, acct: growth_api.Account | None = None, *, reason: str = "activity"
    ) -> TaskOutcome:
        """无猫账号立即重试领养（豁免当日防抖）。

        为什么需要豁免：旅行 09 点排程先跑，那时对话量往往还没到门槛（skip 并记当日已试）；
        10 点活跃上报把对话量补满后，「门槛刚达成」是新状态，不算对上游重试轰炸。
        """
        if acct is None:
            acct, err = self._account()
            if err:
                return TaskOutcome(KEY, False, err)
        with self._lock:
            buddy = growth_api.fetch_buddy(acct, timeout=self.timeout, transport=self.transport)
            if not buddy.ok:
                return self._store(
                    TaskOutcome(KEY, False, f"查猫档案失败：{buddy.summary()}", {"reason": reason})
                )
            if buddy.data is not None:
                return self._store(
                    TaskOutcome(KEY, True, "已有猫，无需领养", {"reason": reason, "action": "skip"})
                )
            ok, summary, extra = self._adopt(acct, force=True)
            extra.update({"reason": reason})
            return self._store(TaskOutcome(KEY, ok, summary, extra))

    def status(self) -> dict:
        return {
            "key": KEY,
            "location_id": self.location_id,
            "adopt_tried_today": self.adopt_tried_uids(),
            "last": self.last.as_dict() if self.last else None,
        }

    # ------------------------------------------------------------------ 内部

    def adopt_tried_uids(self) -> list[str]:
        """「今日已试过领养（门槛未达）」的 uid 列表，供管理员排查。"""
        day = growth_api.cst_day()
        return [uid for uid, d in self._adopt_tried.items() if d == day]

    def _account(self) -> tuple[growth_api.Account | None, str]:
        if self.cred is None:
            return None, "未找到登录凭据（请先在桌面端登录 CodeBuddy / WorkBuddy 后重启）"
        try:
            return growth_api.account_of(self.cred, log=self._log), ""
        except Exception as e:  # noqa: BLE001
            return None, f"取凭据/刷新失败：{e}"

    def _store(self, oc: TaskOutcome) -> TaskOutcome:
        self.last = oc
        self._log(f"[travel] {oc.summary}")
        return oc

    def _travel_one(self, acct: growth_api.Account, reason: str) -> TaskOutcome:
        steps: list[dict] = []
        detail: dict = {"reason": reason, "uid": acct.uid, "steps": steps}

        buddy = growth_api.fetch_buddy(acct, timeout=self.timeout, transport=self.transport)
        if not buddy.ok:
            return TaskOutcome(KEY, False, f"查猫档案失败：{buddy.summary()}", detail)
        if buddy.data is None:
            ok, summary, extra = self._adopt(acct, force=False)
            steps.append({"action": "adopt", "ok": ok, **extra})
            detail.update({"action": "adopt", "state": "no-buddy"})
            return TaskOutcome(KEY, ok, summary, detail)

        detail["buddy"] = {"id": buddy.data.get("id"), "name": buddy.data.get("name")}
        st = growth_api.fetch_travel_status(acct, timeout=self.timeout, transport=self.transport)
        if not st.ok:
            return TaskOutcome(KEY, False, f"查旅行状态失败：{st.summary()}", detail)

        state = st.data["state"]
        detail["state"] = state
        if state == STATE_ARRIVED:
            return self._claim(acct, st.data, detail)
        if state == STATE_IDLE:
            return self._depart(acct, st.data, detail)
        if state == STATE_TRAVELING:
            detail["action"] = "skip"
            detail["record_id"] = st.data["record_id"]
            return TaskOutcome(
                KEY, True, f"在途（record={st.data['record_id']}），本趟跳过", detail
            )
        detail["action"] = "skip"
        return TaskOutcome(KEY, True, f"未知状态 {state!r}，本趟跳过", detail)

    def _depart(self, acct: growth_api.Account, ts: dict, detail: dict) -> TaskOutcome:
        if ts["daily_limit_reached"]:
            detail["action"] = "skip"
            return TaskOutcome(KEY, True, "今日已派出过（自然日 00:00 CST 重置），跳过", detail)
        res = growth_api.travel_depart(
            acct, self.location_id, timeout=self.timeout, transport=self.transport
        )
        detail["action"] = "depart"
        detail["location_id"] = self.location_id
        if not res.ok:
            detail["error"] = res.summary()
            return TaskOutcome(KEY, False, f"派出失败：{res.summary()}", detail)
        return TaskOutcome(KEY, True, f"派出成功（地点 {self.location_id}）", detail)

    def _claim(self, acct: growth_api.Account, ts: dict, detail: dict) -> TaskOutcome:
        record_id = ts["record_id"]
        if not record_id:
            detail["action"] = "skip"
            return TaskOutcome(KEY, True, "已到站但缺 record_id，跳过领奖", detail)
        res = growth_api.travel_claim(
            acct, record_id, timeout=self.timeout, transport=self.transport
        )
        detail["action"] = "claim"
        detail["record_id"] = record_id
        if not res.ok:
            detail["error"] = res.summary()
            return TaskOutcome(KEY, False, f"领奖失败（record={record_id}）：{res.summary()}", detail)
        detail["reward"] = res.data
        return TaskOutcome(KEY, True, f"领奖成功 record={record_id} reward={res.data}", detail)

    def _adopt(self, acct: growth_api.Account, *, force: bool) -> tuple[bool, str, dict]:
        uid = acct.uid
        if not force and self._adopt_tried.get(uid) == growth_api.cst_day():
            return True, "今日已试过领养（对话量未达门槛），当日不再重试", {"adopt": "debounced"}
        ag = growth_api.buddy_agreement(acct, timeout=self.timeout, transport=self.transport)
        if not ag.ok:
            return False, f"同意协议失败：{ag.summary()}", {"adopt": "agreement_failed"}
        first = growth_api.buddy_first(acct, timeout=self.timeout, transport=self.transport)
        if first.ok:
            return True, "领养成功（+300 积分）", {"adopt": "ok"}
        if growth_api.is_buddy_task_incomplete(first):
            self._adopt_tried[uid] = growth_api.cst_day()
            return True, "领养跳过（对话量未达门槛，明天再试）", {"adopt": "not_ready"}
        return False, f"领养失败：{first.summary()}", {"adopt": "failed", "code": first.code}


__all__ = [
    "KEY",
    "DEFAULT_TRAVEL_HOURS",
    "STATE_IDLE",
    "STATE_TRAVELING",
    "STATE_ARRIVED",
    "TravelRunner",
]
