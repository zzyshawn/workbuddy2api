#!/usr/bin/env python3
"""task_result.py — 六类定时任务的统一执行结果结构。

为什么单独一个模块：六类任务模块（checkin / activity / travel / keepalive / school /
blackcat）彼此独立、互不 import；调度器（task_scheduler.py）要用同一口径汇总「跑了什么、
成没成、为什么」。把结果结构放这里，调度器与各任务模块都只依赖本模块，依赖方向保持单向：

    task_scheduler ──▶ activity / travel / keepalive / school / blackcat / checkin ──▶ task_result

字段口径：
  key     ：任务标识，取 checkin / activity / travel / keepalive / school / cat（与 CLI、配置键同名）；
  ok      ：本次执行是否没有出错（「今天已签到」「已领」这类幂等命中算 ok）；
  summary ：一行中文结论，直接进日志；
  detail  ：结构化细节（计数、余额、每条动作的结果），落盘留档供事后审计。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcome:
    """一次任务执行的结果。"""

    key: str
    ok: bool
    summary: str
    detail: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "at": int(self.at),
            "ok": bool(self.ok),
            "summary": self.summary,
            "detail": self.detail,
        }


def outcome_from_entry(key: str, entry: Any) -> TaskOutcome:
    """把「已经是 dict」的执行结果（签到调度器的返回）包成 TaskOutcome。

    签到（checkin.py）早于本模块存在，run_once() 返回的是裸 dict（含 ok/msg/balance），
    为了不给它加一层壳，这里做一次单向适配。
    """
    if isinstance(entry, TaskOutcome):
        return entry
    if not isinstance(entry, dict):
        return TaskOutcome(key, False, f"未知结果类型：{type(entry).__name__}", {"raw": str(entry)[:400]})
    ok = bool(entry.get("ok"))
    if entry.get("error"):
        summary = str(entry["error"])
    elif entry.get("already"):
        summary = "今日已签到"
    elif ok:
        summary = "签到成功"
        if entry.get("balance") is not None:
            summary += f"，可用积分 {entry['balance']}"
    else:
        summary = f"签到失败 HTTP {entry.get('status')} code={entry.get('code')} {entry.get('msg') or ''}"
    return TaskOutcome(key, ok, summary, entry, float(entry.get("at") or time.time()))


__all__ = ["TaskOutcome", "outcome_from_entry"]
