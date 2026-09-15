#!/usr/bin/env python3
"""
checkin.py — 每日自动签到 + 积分余额查询（billing 域，www.codebuddy.cn）。

上游（对照 Go 参考实现 internal/upstream + scheduler）：

    POST /v2/billing/meter/daily-checkin        body {}      → 签到
    POST /v2/billing/meter/get-user-resource    body {...}   → 积分余额

两个端点的公共请求头（BillingHeaders）：Authorization / Accept / Content-Type /
X-User-Id /（有 enterpriseId 时）X-Enterprise-Id + X-Tenant-Id /（有 domain 时）X-Domain。
**不带** Origin / Referer / X-Requested-With / X-Product 那套 CLI 指纹头。

实测补充（2026-09-12，本机真账号）：
  billing 域对 UA 有校验——不显式设置 UA（Python 默认 UA / python-httpx 默认 UA）
  会直接 403 + code 10085「请求不合法」。故此处**强制**写入 CLI UA。
  注意这与 Go 版「billing 域默认不发 UA」的结论不同：Go 的默认 UA（Go-http-client/1.1）
  恰好放行，Python 的默认 UA 不放行。若上游 UA 策略再变，用 CODEBUDDY2OPENAI_BILLING_UA 覆盖。

调度语义（沿用参考实现的设计边界）：
  - checkin_hours 缺省 / 空 / null 一律回落到 (9, 21)，**不是禁用**；真正关闭用 enabled=False；
  - 到点执行「签到 → 查余额」；迟到唤醒（休眠/挂起）会补跑当日漏掉的时点；
  - 签到前按 2h 窗口预刷新 token（修掉参考实现 D3：定时路径不刷 token 会导致当天不解冻）；
  - 签到结果 + 余额落盘留档（修掉参考实现 D4：结果被丢弃、事后无法审计）；
  - 签到幂等但按错误返回：重复签到是 HTTP 400 + code 10001「今天已签到，请明天再来」，
    这种情况归类为 already，不算失败。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

import httpx

BILLING_BASE = "https://www.codebuddy.cn"
CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
RESOURCE_PATH = "/v2/billing/meter/get-user-resource"

#: 签到接口不需要授权 UA 指纹校验，但必须是一个非 Python 默认的 UA（见模块 docstring）。
DEFAULT_BILLING_UA = "CLI/2.63.2 CodeBuddy/2.63.2"

DEFAULT_CHECKIN_HOURS: tuple[int, ...] = (9, 21)
DEFAULT_TIMEOUT = 60.0
#: 迟到唤醒的补跑窗口：时点距今超过该时长就不再补跑（避免重启翻出昨天的班次）。
DEFAULT_CATCHUP_WINDOW = 6 * 3600.0
#: 签到前预刷新窗口：token 距过期不足 2h 就先刷（等价参考实现的 NeedsRefresh(2h)）。
REFRESH_WINDOW_MS = 2 * 60 * 60 * 1000

HISTORY_NAME = "checkin-log.json"
HISTORY_LIMIT = 50

BILLING_ACCOUNT_HEADERS = (
    "Authorization",
    "X-User-Id",
    "X-Enterprise-Id",
    "X-Tenant-Id",
    "X-Domain",
)


def billing_ua() -> str:
    return os.environ.get("CODEBUDDY2OPENAI_BILLING_UA") or DEFAULT_BILLING_UA


def normalize_hours(raw, default: tuple[int, ...] = DEFAULT_CHECKIN_HOURS) -> tuple[int, ...]:
    """小时配置归一化。

    缺省 None / 空串 / 空列表 / 全非法 → 回落 default（**不是禁用**）；
    合法项按升序去重，支持显式 0 点。

    default 可覆盖：六类任务各自的默认时点不同，调度器（task_scheduler）用它复用同一份
    归一逻辑，不必各写一遍。默认值参数化后，签到侧的既有语义逐字不变。
    """
    if raw is None:
        return default
    if isinstance(raw, str):
        if not raw.strip():
            return default
        items = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: list[int] = []
    for it in items:
        if it is None or (isinstance(it, str) and not it.strip()):
            continue
        try:
            h = int(str(it).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= h <= 23 and h not in out:
            out.append(h)
    return tuple(sorted(out)) if out else default


def to_billing_headers(common: dict, *, ua: str | None = None) -> dict:
    """把 chat 风格的公共头收敛成 billing 域请求头（丢掉 CLI 指纹头，强制 UA）。"""
    h: dict = {"Accept": "application/json", "Content-Type": "application/json"}
    for k in BILLING_ACCOUNT_HEADERS:
        v = common.get(k)
        if v:
            h[k] = v
    h["User-Agent"] = ua or billing_ua()
    return h


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class CheckinResult:
    """ok = 本次调用没有出错（「今天已签到」也算 ok，它不是失败）；
    already = 本次是重复签到（幂等命中）。"""

    ok: bool
    already: bool
    status: int
    code: int | None
    msg: str
    at: float = field(default_factory=time.time)
    data: str = ""

    def summary(self) -> str:
        if self.already:
            return f"今日已签到（{self.msg or 'code=%s' % self.code}）"
        if self.ok:
            return "签到成功"
        return f"签到失败 HTTP {self.status} code={self.code} {self.msg}"


@dataclass
class BalanceResult:
    ok: bool
    remain: int = 0
    total_dosage: int = 0
    accounts: list[dict] = field(default_factory=list)
    status: int = 0
    msg: str = ""
    at: float = field(default_factory=time.time)

    def summary(self) -> str:
        if not self.ok:
            return f"余额查询失败 HTTP {self.status} {self.msg}"
        return f"可用积分 {self.remain}"


_ALREADY_MARKERS = ("已签到", "already", "checkin", "check-in", "重复签到")


def _is_already(status: int, code: int | None, msg: str) -> bool:
    if code == 10001:
        return True
    low = (msg or "").lower()
    if any(m in low for m in _ALREADY_MARKERS):
        return True
    return False


def do_checkin(
    headers: dict,
    *,
    base_url: str = BILLING_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> CheckinResult:
    """执行每日签到。重复签到（code 10001 / 「已签到」文案）归为 already，不算失败。

    transport 仅用于测试注入（httpx 的 transport）。
    """
    url = base_url.rstrip("/") + CHECKIN_PATH
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(url, headers=headers, json={})
    except Exception as e:  # noqa: BLE001 — 网络异常不该让调度线程崩
        return CheckinResult(ok=False, already=False, status=-1, code=None, msg=f"网络失败：{e}")

    code: int | None = None
    msg = ""
    data_str = ""
    try:
        env = r.json()
        code = env.get("code")
        msg = env.get("msg") or ""
        if env.get("data") is not None:
            data_str = json.dumps(env["data"], ensure_ascii=False)[:400]
    except Exception:  # noqa: BLE001
        msg = r.text[:200]

    if r.status_code < 400 and (code in (0, None)):
        return CheckinResult(True, False, r.status_code, code, msg or "OK", data=data_str)
    already = _is_already(r.status_code, code, msg)
    return CheckinResult(True if already else False, already, r.status_code, code, msg, data=data_str)


def fetch_balance(
    headers: dict,
    *,
    base_url: str = BILLING_BASE,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> BalanceResult:
    """查询可花费积分余额（所有套餐 CycleCapacityRemain / CapacityRemain 聚合）。

    聚合口径与参考实现一致：CycleCapacitySize > 0 时取 CycleCapacityRemain，否则取
    CapacityRemain；负值钳 0；跨套餐求和。

    transport 仅用于测试注入（httpx 的 transport）。
    """
    url = base_url.rstrip("/") + RESOURCE_PATH
    now = time.time()
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "PackageEndTimeRangeEnd": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(now + 365 * 101 * 86400)
        ),
    }
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(url, headers=headers, json=body)
    except Exception as e:  # noqa: BLE001
        return BalanceResult(ok=False, status=-1, msg=f"网络失败：{e}")

    try:
        env = r.json()
    except Exception:  # noqa: BLE001
        return BalanceResult(ok=False, status=r.status_code, msg=r.text[:200])

    if r.status_code >= 400 or env.get("code") not in (0, None):
        return BalanceResult(
            ok=False,
            status=r.status_code,
            msg=f"code={env.get('code')} {env.get('msg') or ''}".strip(),
        )

    data = ((env.get("data") or {}).get("Response") or {}).get("Data") or {}
    accounts = data.get("Accounts") or []
    remain = 0
    brief: list[dict] = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        try:
            cycle_size = int(a.get("CycleCapacitySize") or 0)
            cycle_remain = int(a.get("CycleCapacityRemain") or 0)
            cap_remain = int(a.get("CapacityRemain") or 0)
        except (TypeError, ValueError):
            continue
        r_ = cycle_remain if cycle_size > 0 else cap_remain
        if r_ < 0:
            r_ = 0
        remain += r_
        brief.append(
            {
                "package": a.get("PackageName") or a.get("ProductName") or "",
                "cycle_start": (a.get("CycleStartTime") or "")[:10],
                "cycle_end": (a.get("CycleEndTime") or "")[:10],
                "remain": int(r_),
                "size": int(cycle_size or a.get("CapacitySize") or 0),
            }
        )
    return BalanceResult(
        ok=True,
        remain=int(remain),
        total_dosage=int(data.get("TotalDosage") or 0),
        accounts=brief,
        status=r.status_code,
        msg="OK",
    )


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class CheckinScheduler(threading.Thread):
    """后台签到线程：按 hours 时点执行「签到 + 查余额」，结果落盘留档。"""

    def __init__(
        self,
        cred,
        *,
        hours=None,
        enabled: bool = True,
        base_url: str = BILLING_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        cache_dir: Path | str | None = None,
        run_on_start: bool = False,
        catchup_window: float = DEFAULT_CATCHUP_WINDOW,
        log: Callable[[str], None] | None = None,
    ):
        super().__init__(name="checkin-scheduler", daemon=True)
        self.cred = cred
        self.hours = normalize_hours(hours)
        self.enabled = bool(enabled)
        self.base_url = base_url
        self.timeout = float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.run_on_start = bool(run_on_start)
        self.catchup_window = float(catchup_window)
        self._log = log or (lambda _m: None)

        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._fired: set[tuple[str, int]] = set()
        self.last: dict | None = None
        self.history: list[dict] = self._load_history()

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
            self._log(f"[checkin] WARN 结果落盘失败：{e}")

    # ------------------------------------------------------------ 执行

    def run_once(self, reason: str = "manual") -> dict:
        """签到一次并查余额；任何异常都被收敛成结果字典，不向外抛。"""
        with self._run_lock:
            started = time.time()
            headers = None
            if self.cred is None:
                err = "未找到登录凭据（请先在桌面端登录 CodeBuddy / WorkBuddy 后重启）"
                self._log(f"[checkin] WARN {err}")
                entry = {"at": int(started), "reason": reason, "ok": False, "error": err}
                self.last = entry
                self._append_history(entry)
                return entry
            try:
                # 签到前预刷新（2h 窗口），避免“token 已过期 → 当天签不上”的老问题
                headers = self.cred.get_headers(within_ms=REFRESH_WINDOW_MS)
            except Exception as e:  # noqa: BLE001
                err = f"取凭据/刷新失败：{e}"
                self._log(f"[checkin] ERR {err}")
                entry = {"at": int(started), "reason": reason, "ok": False, "error": err}
                self.last = entry
                self._append_history(entry)
                return entry

            try:
                bill = to_billing_headers(headers)
                res = do_checkin(bill, base_url=self.base_url, timeout=self.timeout)
                bal = fetch_balance(bill, base_url=self.base_url, timeout=self.timeout)

                self._log(f"[checkin] {reason} → {res.summary()}；{bal.summary()}")
                if bal.ok:
                    self._log(
                        "[checkin] 余额明细 "
                        + json.dumps(bal.accounts, ensure_ascii=False)
                    )
                entry = {
                    "at": int(started),
                    "reason": reason,
                    "ok": res.ok,
                    "already": res.already,
                    "status": res.status,
                    "code": res.code,
                    "msg": res.msg,
                    "data": res.data,
                    "balance": bal.remain if bal.ok else None,
                    "balance_ok": bal.ok,
                    "balance_msg": None if bal.ok else bal.msg,
                    "accounts": bal.accounts,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            except Exception as e:  # noqa: BLE001
                self._log(f"[checkin] ERR {reason} 执行异常：{e}")
                entry = {"at": int(started), "reason": reason, "ok": False, "error": str(e)}

            self.last = entry
            self._append_history(entry)
            return entry

    # ------------------------------------------------------------ 排程

    def _slots(self, date) -> list[tuple[float, tuple[str, int]]]:
        return [
            (datetime.combine(date, dtime(h, 0)).timestamp(), (date.isoformat(), h))
            for h in self.hours
        ]

    def _due_slots(self, now: float) -> list[tuple[str, int]]:
        """返回此刻应补跑的时点。

        只看「今天 + 昨天」，且时点距今不超过 catchup_window（默认 6h）——
        这样既能补上休眠/挂起错过的班次，又不会让一次重启把昨天早上的班次也翻出来跑。
        """
        floor_ts = now - self.catchup_window
        today = datetime.fromtimestamp(now).date()
        out: list[tuple[float, tuple[str, int]]] = []
        for offset in (0, -1):
            date = today + timedelta(days=offset)
            for ts, key in self._slots(date):
                if floor_ts <= ts <= now and key not in self._fired:
                    out.append((ts, key))
        out.sort(key=lambda x: x[0])
        return [k for _ts, k in out]

    def next_fire_at(self, now: float | None = None) -> float | None:
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now).date()
        cands: list[float] = []
        for offset in (0, 1, 2):
            date = today + timedelta(days=offset)
            for ts, _key in self._slots(date):
                if ts > now:
                    cands.append(ts)
        return min(cands) if cands else None

    def _prune_fired(self, now: float) -> None:
        cutoff = (datetime.fromtimestamp(now).date() - timedelta(days=3)).isoformat()
        self._fired = {k for k in self._fired if k[0] >= cutoff}

    def run(self) -> None:
        if not self.enabled:
            self._log("[checkin] 已禁用，调度线程退出")
            return
        if self.cred is None:
            self._log("[checkin] WARN 无可用凭据，调度线程退出")
            return
        self._log(
            f"[checkin] 调度启动，每日 {', '.join(f'{h:02d}:00' for h in self.hours)}，"
            f"下一班 {self._fmt(self.next_fire_at())}"
        )
        if self.run_on_start:
            self.run_once(reason="startup")

        while not self._stop.is_set():
            now = time.time()
            due = self._due_slots(now)
            if due:
                for key in due:
                    self._fired.add(key)
                    self.run_once(reason=f"scheduled {key[0]} {key[1]:02d}:00")
                self._prune_fired(time.time())
                continue
            nxt = self.next_fire_at(now)
            if nxt is None:
                wait = 3600.0
            else:
                wait = max(1.0, nxt - now)
            # 分段等待，保证 stop() 及时生效，也容忍系统休眠导致的时间跳变
            self._stop.wait(min(wait, 30.0))

        self._log("[checkin] 调度线程退出")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 状态

    @staticmethod
    def _fmt(ts: float | None) -> str:
        if not ts:
            return "(无)"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def status(self) -> dict:
        nxt = self.next_fire_at() if self.enabled else None
        return {
            "enabled": self.enabled,
            "hours": list(self.hours),
            "alive": self.is_alive(),
            "next_fire_at": self._fmt(nxt) if nxt else None,
            "next_fire_ts": int(nxt) if nxt else None,
            "run_on_start": self.run_on_start,
            "billing_base": self.base_url,
            "last": self.last,
            "history_tail": self.history[-5:],
        }


__all__ = [
    "BILLING_BASE",
    "CHECKIN_PATH",
    "RESOURCE_PATH",
    "DEFAULT_CHECKIN_HOURS",
    "CheckinResult",
    "BalanceResult",
    "CheckinScheduler",
    "normalize_hours",
    "to_billing_headers",
    "do_checkin",
    "fetch_balance",
    "billing_ua",
]
