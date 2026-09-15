#!/usr/bin/env python3
"""keepalive.py — token 保活（对应参考实现 internal/scheduler/scheduler.go RunKeepaliveNow）。

做什么：每天固定时点（默认 22 点，排在签到 21 点之后）**主动刷新一次 access token**，
把续期动作从「下一个请求撞上过期」提前到低峰时点。

参考实现是「遍历账号池 → 逐个 RefreshToken → session dead 连续 3 次才禁用」。本项目
读的是桌面端单一登录态、没有账号池，所以保留两件事、放下两件事：

  保留
    ① 主动刷新：靠一个大到必然命中的预刷新窗口实现（见 FORCE_REFRESH_WINDOW_MS）；
    ② 连续计数语义：刷新失败时累计，**连续 3 次**才判 session 失效（一次失败很可能是
       网络抖动/上游抖动，直接判死会造成「历史误判」——参考实现踩过这个坑）；
       一旦刷新成功立即清零。
  放下
    ③ 「自动禁用账号」：单账号禁用等于服务整体不可用，判死没有意义，改成醒目的 WARN
       + status 里暴露 session_dead，让人去重新登录；
    ④ 多账号限速：只有一个账号，无需账号间间隔。

凭证回写由 CredentialManager 自己完成（原子写 + 落盘），本模块不重复实现。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from task_result import TaskOutcome

KEY = "keepalive"

DEFAULT_KEEPALIVE_HOURS: tuple[int, ...] = (22,)

#: session 失效连续阈值：连续这么多次刷新失败才判定凭据已失效（与参考实现同口径，3 次）。
SESSION_DEAD_THRESHOLD = 3

#: 「强制刷新」用的预刷新窗口：只要这个窗口比任何 token 的生命周期都长，判定必然命中，
#: 于是 get_headers() 一定会走刷新分支。用 400 天（access token 实测约 14 天）留足余量，
#: 既避免给 CredentialManager 加一个只为保活存在的方法，也不需要读 token 的具体有效期。
FORCE_REFRESH_WINDOW_MS = 400 * 24 * 60 * 60 * 1000

#: 判定「session 失效（refresh token 已不可用）」的响应关键词，命中即认为是凭据问题
#: 而不是网络抖动——这类失败重复刷新救不回来，只该提示重新登录。
SESSION_DEAD_MARKERS = ("12153", "session dead", "session 失效", "refresh token", "invalid_grant")


def looks_session_dead(msg: str) -> bool:
    low = (msg or "").lower()
    return any(m.lower() in low for m in SESSION_DEAD_MARKERS)


class KeepaliveRunner:
    """单账号 token 保活。线程安全（定时与手动触发互斥）。"""

    def __init__(
        self,
        cred,
        *,
        threshold: int = SESSION_DEAD_THRESHOLD,
        log: Callable[[str], None] | None = None,
    ):
        self.cred = cred
        self.threshold = max(1, int(threshold))
        self._log = log or (lambda _m: None)

        self._lock = threading.Lock()
        self.consecutive_failures = 0
        self.session_dead = False
        self.last: TaskOutcome | None = None

    # ------------------------------------------------------------------ 入口

    def run_once(self, reason: str = "manual") -> TaskOutcome:
        with self._lock:
            if self.cred is None:
                return self._store(
                    TaskOutcome(KEY, False, "未找到登录凭据（请先在桌面端登录 CodeBuddy / WorkBuddy 后重启）")
                )
            detail: dict = {"reason": reason}
            try:
                # 大窗口 ⇒ 必然进刷新分支（见 FORCE_REFRESH_WINDOW_MS 的说明）
                headers = self.cred.get_headers(within_ms=FORCE_REFRESH_WINDOW_MS)
            except Exception as e:  # noqa: BLE001
                return self._store(self._on_failure(str(e), detail))

            self.consecutive_failures = 0
            was_dead = self.session_dead
            self.session_dead = False
            detail["token_expires_at"] = self._expires_at()
            detail["uid"] = headers.get("X-User-Id")
            summary = "token 已刷新"
            if self._expires_at():
                summary += f"，新有效期至 {self._fmt_ms(detail['token_expires_at'])}"
            if was_dead:
                summary += "（已从 session 失效状态恢复）"
            return self._store(TaskOutcome(KEY, True, summary, detail))

    def status(self) -> dict:
        return {
            "key": KEY,
            "consecutive_failures": self.consecutive_failures,
            "session_dead": self.session_dead,
            "threshold": self.threshold,
            "last": self.last.as_dict() if self.last else None,
        }

    # ------------------------------------------------------------------ 内部

    def _on_failure(self, err: str, detail: dict) -> TaskOutcome:
        self.consecutive_failures += 1
        detail["consecutive_failures"] = self.consecutive_failures
        detail["error"] = err
        dead = looks_session_dead(err)
        detail["session_dead_hint"] = dead
        if dead or self.consecutive_failures >= self.threshold:
            self.session_dead = True
        summary = f"token 刷新失败：{err}"
        if self.session_dead and dead:
            summary += "（上游报 session 失效：凭据已不可用，请重新登录桌面端）"
            self._log(f"[keepalive] ERR {summary}")
        elif self.session_dead:
            summary += (
                f"（已连续 {self.consecutive_failures} 次失败 ≥ 阈值 {self.threshold}，"
                "判定凭据失效，请重新登录桌面端）"
            )
            self._log(f"[keepalive] ERR {summary}")
        return TaskOutcome(KEY, False, summary, detail)

    def _expires_at(self) -> int:
        try:
            return int((self.cred.summary() or {}).get("token_expires_at") or 0)
        except Exception:  # noqa: BLE001 — 只用于日志
            return 0

    @staticmethod
    def _fmt_ms(ms: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))

    def _store(self, oc: TaskOutcome) -> TaskOutcome:
        self.last = oc
        if oc.ok:
            self._log(f"[keepalive] {oc.summary}")
        return oc


__all__ = [
    "KEY",
    "DEFAULT_KEEPALIVE_HOURS",
    "SESSION_DEAD_THRESHOLD",
    "FORCE_REFRESH_WINDOW_MS",
    "SESSION_DEAD_MARKERS",
    "looks_session_dead",
    "KeepaliveRunner",
]
