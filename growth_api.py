#!/usr/bin/env python3
"""growth_api.py — 上游 growth / billing 域接口层（五类积分任务的公共 HTTP 底座）。

对应 Go 参考实现：
  - internal/upstream/travel.go    growth 域「猫档案 / 猫猫旅行 / 连登」
  - internal/upstream/report.go    billing 域「对话活跃上报」
  - internal/upstream/headers.go   BillingHeaders（两个域共用的请求头）
  - internal/auth + client.go      账号视图与信封解析

域与路径（实测）：

    growth 域   https://copilot.tencent.com
      GET  /activity/growth/buddy/info           当前猫档案（data.buddy 为 null = 无猫）
      GET  /activity/growth/buddy/travel/status  旅行状态
      POST /activity/growth/buddy/travel/depart  {"location_id": 4}
      POST /activity/growth/buddy/travel/claim   {"record_id": N}
      POST /activity/growth/buddy/agreement      {"agree": true}
      POST /activity/growth/buddy/first          领养（无猫且对话量达标送 300 分）
      GET  /activity/growth/streak               连登天数（只读 oracle）
      GET  /v2/activity/growth/tasks             成长任务列表
      POST /v2/activity/growth/tasks/accept      {"task_codes": [...]}
      POST /activity/growth/tasks/{code}/claim   领奖（无 body）
    billing 域  https://www.codebuddy.cn
      POST /v2/report                            对话活跃上报（必带 userId）
    web 域      https://www.workbuddy.cn
      POST /activity/growth/tasks/{code}/claim   chat 域 400 时的领奖降级路径

为什么单独一层：活跃上报 / 猫猫旅行 / 开学季 / 夜猫子都要打这两个域，但它们彼此独立
（互不 import）。把「账号视图 + 请求头 + 信封/错误语义」收在这里，上层任务模块只管业务
状态机，不重复实现 HTTP。

错误语义（沿用参考实现，必须遵守）：
  - **业务错误常常是 HTTP 200 + body 里的 code**，所以绝不能只看状态码；
  - HTTP 非 2xx，或 body 里 code 存在且 != 0 → ok=False；
  - 上游会把幂等命中（如任务已领 already_claimed）也按错误形状返回，判定交给调用方；
  - 网络异常收敛成 status=-1 的 ApiResult，不向上抛（定时任务不该因一次抖动崩掉）。

billing 域 UA 注意事项见 checkin.py 的模块 docstring：不显式设置 UA 会被 403 拒，
故这里复用 checkin.to_billing_headers()（CLI UA + 丢掉 CLI 指纹头），不另起一套。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from checkin import to_billing_headers

# ---------------------------------------------------------------------------
# 域与常量
# ---------------------------------------------------------------------------

GROWTH_BASE = "https://copilot.tencent.com"
BILLING_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"

DEFAULT_TIMEOUT = 60.0

#: 任务路径取账号时的预刷新窗口：与签到同口径（2h）。
#: token 距过期不足 2h 就先刷，避免到点时已过期、整趟任务白跑。
REFRESH_WINDOW_MS = 2 * 60 * 60 * 1000

PATH_BUDDY_INFO = "/activity/growth/buddy/info"
PATH_TRAVEL_STATUS = "/activity/growth/buddy/travel/status"
PATH_TRAVEL_DEPART = "/activity/growth/buddy/travel/depart"
PATH_TRAVEL_CLAIM = "/activity/growth/buddy/travel/claim"
PATH_BUDDY_AGREEMENT = "/activity/growth/buddy/agreement"
PATH_BUDDY_FIRST = "/activity/growth/buddy/first"
PATH_STREAK = "/activity/growth/streak"
PATH_TASKS = "/v2/activity/growth/tasks"
PATH_ACCEPT_TASKS = "/v2/activity/growth/tasks/accept"
PATH_ENERGY = "/v2/activity/growth/energy"
PATH_REPORT = "/v2/report"

#: 领奖路径（chat 域）：路径里带 task_code，无 body（实测端点，勿用 /tasks/reward/claim）。
CLAIM_PATH_FMT = "/activity/growth/tasks/{code}/claim"

#: 领养门槛未达标的业务错误关键词（HTTP 400 时出现）。属预期行为，当日不重试。
BUDDY_INCOMPLETE_MARKER = "first_buddy task not completed yet"

#: 旅行派出地点固定 4（古镇客栈）：1~4 号地点收益/时长区间完全相同，无最优解。
DEFAULT_TRAVEL_LOCATION_ID = 4

#: 活跃上报事件里的模型标识（与参考实现 report.go 逐字一致）。
DEFAULT_REPORT_MODEL_ID = "deepseek-v4-flash"
DEFAULT_REPORT_MODEL_NAME = "DeepSeek V4 Flash"
#: 夜猫子任务用 GLM-5.2 + mode=night（参考实现 task_runner.py black_cat 口径）。
NIGHT_MODEL_ID = "glm-5.2"
NIGHT_MODEL_NAME = "GLM-5.2"

#: 上游所有「每日重置」都按自然日 00:00 CST（Asia/Shanghai）。中国无夏令时，固定 +8 即可，
#: 不依赖容器 tzdata，也不读 TZ 环境变量（Windows 上 Python 根本不认 TZ，见项目备忘）。
CST = timezone(timedelta(hours=8))


def cst_now(ts: float | None = None) -> datetime:
    """取 CST 当前时间（带时区），供自然日判定 / 时段窗口判定。"""
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).astimezone(CST)


def cst_day(ts: float | None = None) -> str:
    """上游自然日（CST），格式 2006-01-02 → 2026-09-16。"""
    return cst_now(ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class ApiResult:
    """一次上游调用的结果。

    ok    ：HTTP 2xx 且业务 code 为 0/缺省。
    status：HTTP 状态码；-1 表示网络层失败（msg 里有原因）。
    code  ：上游业务码（可能为 None：响应不是 JSON 信封）。
    data  ：`data` 字段，按接口语义做轻量归一（如 buddy 为 null / days 为 int）。
    """

    ok: bool
    status: int = 0
    code: int | None = None
    msg: str = ""
    data: Any = None

    def summary(self) -> str:
        if self.ok:
            return "OK"
        if self.status == -1:
            return self.msg or "网络失败"
        return f"HTTP {self.status} code={self.code} {self.msg}".strip()


@dataclass
class Account:
    """任务用的账号视图（由 CredentialManager 的请求头派生，不持有 token 字段）。

    为什么不直接传 CredentialManager：任务模块只关心「打哪个域、带哪些头、uid 是多少」，
    用这个只读快照可以让它们互不依赖，也让离线测试不必伪造凭据管理器。
    """

    uid: str = ""
    nickname: str = ""
    domain: str = ""
    headers: dict = field(default_factory=dict)

    def billing_headers(self, *, ua: str | None = None) -> dict:
        """billing 域头（CLI UA + 账号头，丢 CLI 指纹头）。"""
        return to_billing_headers(self.headers, ua=ua)

    def growth_headers(self, *, ua: str | None = None) -> dict:
        """growth 域头：与 billing 域同族，另加官方客户端的 X-CodeBuddy-Request 声明。"""
        h = to_billing_headers(self.headers, ua=ua)
        h["X-CodeBuddy-Request"] = "1"
        return h

    def web_headers(self, *, referer: str) -> dict:
        """web 域头（领奖降级路径用）：浏览器指纹 + x-client-platform: web。"""
        h = {
            "Authorization": self.headers.get("Authorization", ""),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_BASE,
            "Referer": referer or f"{WEB_BASE}/profile/growth-center",
            "x-client-platform": "web",
            "X-User-Id": self.uid,
            "X-Domain": WEB_BASE,
        }
        return {k: v for k, v in h.items() if v}


def account_of(
    cred,
    *,
    within_ms: int = REFRESH_WINDOW_MS,
    log: Callable[[str], None] | None = None,
) -> Account:
    """从凭据管理器取一个任务账号视图（必要时先刷新 token）。

    异常收敛成 None 由调用方判定——与 checkin 的「凭据缺失记一条失败」同口径。
    """
    headers = cred.get_headers(within_ms=within_ms)
    acct = Account(
        uid=str(headers.get("X-User-Id") or ""),
        domain=str(headers.get("X-Domain") or ""),
        headers=dict(headers),
    )
    try:
        acct.nickname = str((cred.summary() or {}).get("nickname") or "")
    except Exception:  # noqa: BLE001 — 昵称只用于日志，取不到不算错
        if log:
            log("[tasks] WARN 读取账号昵称失败（不影响任务执行）")
    return acct


# ---------------------------------------------------------------------------
# HTTP 底座
# ---------------------------------------------------------------------------


def _parse_envelope(status: int, text: str) -> tuple[int | None, str, Any]:
    try:
        env = json.loads(text)
    except Exception:  # noqa: BLE001
        return None, (text or "")[:200], None
    if not isinstance(env, dict):
        return None, str(env)[:200], None
    code = env.get("code")
    msg = str(env.get("msg") or env.get("message") or "")
    return (code if isinstance(code, int) else None), msg, env.get("data")


def api_call(
    base: str,
    path: str,
    headers: dict,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """打一次上游接口并解信封。

    body=None 表示**不带请求体**（如领奖端点是空 POST）；这一点必须区分，
    否则 httpx 会把 None 序列化成 `null` 发出去。
    """
    url = base.rstrip("/") + path
    kwargs: dict = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.request(method, url, **kwargs)
    except Exception as e:  # noqa: BLE001 — 网络异常不该让调度线程崩
        return ApiResult(False, -1, None, f"网络失败：{e}")

    code, msg, data = _parse_envelope(r.status_code, r.text)
    ok = r.status_code < 400 and code in (0, None)
    return ApiResult(ok, r.status_code, code, msg, data)


def growth_call(
    acct: Account,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    base_url: str = GROWTH_BASE,
) -> ApiResult:
    return api_call(
        base_url,
        path,
        acct.growth_headers(),
        method=method,
        body=body,
        timeout=timeout,
        transport=transport,
    )


def billing_call(
    acct: Account,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    base_url: str = BILLING_BASE,
) -> ApiResult:
    return api_call(
        base_url,
        path,
        acct.billing_headers(),
        method=method,
        body=body,
        timeout=timeout,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# growth 域：猫档案 / 旅行 / 连登
# ---------------------------------------------------------------------------


def fetch_buddy(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """查当前猫档案。ok=True 且 data is None 表示**无猫**（data.buddy 为 null）。"""
    res = growth_call(acct, PATH_BUDDY_INFO, timeout=timeout, transport=transport)
    if not res.ok:
        return res
    buddy = (res.data or {}).get("buddy") if isinstance(res.data, dict) else None
    res.data = buddy if isinstance(buddy, dict) and buddy else None
    return res


def fetch_travel_status(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """查旅行状态：data = {state, daily_limit_reached, record_id, reward_credit}。"""
    res = growth_call(acct, PATH_TRAVEL_STATUS, timeout=timeout, transport=transport)
    if not res.ok:
        return res
    d = res.data if isinstance(res.data, dict) else {}
    res.data = {
        "state": str(d.get("state") or ""),
        "daily_limit_reached": bool(d.get("daily_limit_reached")),
        "record_id": int(d.get("record_id") or 0),
        "reward_credit": int(d.get("reward_credit") or 0),
    }
    return res


def travel_depart(
    acct: Account,
    location_id: int = DEFAULT_TRAVEL_LOCATION_ID,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """派出猫旅行。"""
    return growth_call(
        acct,
        PATH_TRAVEL_DEPART,
        method="POST",
        body={"location_id": int(location_id)},
        timeout=timeout,
        transport=transport,
    )


def travel_claim(
    acct: Account,
    record_id: int,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """领取到站奖励。data 归一为 reward_credit（奖励字段缺失记 0，不算失败）。"""
    res = growth_call(
        acct,
        PATH_TRAVEL_CLAIM,
        method="POST",
        body={"record_id": int(record_id)},
        timeout=timeout,
        transport=transport,
    )
    if res.ok:
        res.data = int((res.data or {}).get("reward_credit") or 0) if isinstance(res.data, dict) else 0
    return res


def buddy_agreement(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """同意领养协议（幂等，重复调用无副作用）。"""
    return growth_call(
        acct,
        PATH_BUDDY_AGREEMENT,
        method="POST",
        body={"agree": True},
        timeout=timeout,
        transport=transport,
    )


def buddy_first(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """领养第一只猫（无猫且对话量达标时送 300 分）。"""
    return growth_call(
        acct, PATH_BUDDY_FIRST, method="POST", body={}, timeout=timeout, transport=transport
    )


def fetch_streak(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """查连登天数（只读 oracle）。data = int；缺字段记 0（= 疑似上报被静默丢弃）。"""
    res = growth_call(acct, PATH_STREAK, timeout=timeout, transport=transport)
    if not res.ok:
        return res
    streak = (res.data or {}).get("streak") if isinstance(res.data, dict) else None
    res.data = int((streak or {}).get("days") or 0) if isinstance(streak, dict) else 0
    return res


def fetch_energy(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """查能量余额（只读 oracle，仅用于日志对账）。data = balance。"""
    res = growth_call(acct, PATH_ENERGY, timeout=timeout, transport=transport)
    if res.ok:
        d = res.data if isinstance(res.data, dict) else {}
        res.data = d.get("balance")
    return res


def is_buddy_task_incomplete(res: ApiResult) -> bool:
    """判定「领养门槛未达标」：HTTP 400 + first_buddy 关键词。当日不应重试。"""
    if res.status != 400:
        return False
    return BUDDY_INCOMPLETE_MARKER in (res.msg or "").lower()


# ---------------------------------------------------------------------------
# growth 域：成长任务（开学季 / 夜猫子共用）
# ---------------------------------------------------------------------------


def list_tasks(
    acct: Account, *, timeout: float = DEFAULT_TIMEOUT, transport: object | None = None
) -> ApiResult:
    """拉成长任务列表。data = [task dict]（原始字段，不强转）。"""
    res = growth_call(acct, PATH_TASKS, timeout=timeout, transport=transport)
    if not res.ok:
        return res
    tasks = (res.data or {}).get("tasks") if isinstance(res.data, dict) else None
    res.data = [t for t in (tasks or []) if isinstance(t, dict)]
    return res


def find_task(tasks: list, code: str) -> dict | None:
    """从任务列表里按 task_code 找一条。"""
    for t in tasks or []:
        if isinstance(t, dict) and t.get("task_code") == code:
            return t
    return None


def accept_tasks(
    acct: Account,
    codes: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """接任务（not_accepted → accepted）。"""
    return growth_call(
        acct,
        PATH_ACCEPT_TASKS,
        method="POST",
        body={"task_codes": list(codes)},
        timeout=timeout,
        transport=transport,
    )


def claim_task(
    acct: Account,
    code: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """领奖：chat 域优先，HTTP 400 时降级 web 域（带完整 web 头）。

    两个域的成功判定都看 `code == 0`；`data.already_claimed` 为真表示幂等命中，
    由调用方当作「已领」而不是失败。
    """
    res = growth_call(
        acct, CLAIM_PATH_FMT.format(code=code), method="POST", timeout=timeout, transport=transport
    )
    if res.ok or res.status != 400:
        return res
    return api_call(
        WEB_BASE,
        CLAIM_PATH_FMT.format(code=code),
        acct.web_headers(referer=f"{WEB_BASE}/profile/growth-center"),
        method="POST",
        timeout=timeout,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# billing 域：对话活跃上报
# ---------------------------------------------------------------------------


def chat_request_event(
    uid: str,
    *,
    conversation_id: str,
    request_id: str = "",
    model_id: str = DEFAULT_REPORT_MODEL_ID,
    model_name: str = DEFAULT_REPORT_MODEL_NAME,
    mode: str = "craft",
    input_length: int = 12,
    activity_id: str = "",
) -> dict:
    """官方客户端 chat_request_send 事件的完整形状（照抄 report.go，勿用最小 3 字段）。

    **必须带 userId**（= 账号 uid）：缺了它服务端照样 200，但静默丢弃，连登不涨。
    activity_id 仅开学季小程序任务需要（否则服务端不把事件关联到该活动任务）。
    """
    now = int(time.time() * 1000)
    rid = request_id or conversation_id
    ev = {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": mode,
        "conversationId": conversation_id,
        "requestId": rid,
        "inputLength": int(input_length),
        "requestModelId": model_id,
        "requestModelName": model_name,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now,
        "traceId": "",
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "agentType": "conversation",
        "userId": uid,
    }
    if activity_id:
        ev["activityId"] = activity_id
    return ev


def report_events(
    acct: Account,
    events: list[dict],
    *,
    base_url: str = BILLING_BASE,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """上报一批事件（body 为事件数组）。默认走 billing 域。"""
    return api_call(
        base_url,
        PATH_REPORT,
        headers or acct.billing_headers(),
        method="POST",
        body=list(events),
        timeout=timeout,
        transport=transport,
    )


def report_chat_activity(
    acct: Account,
    conversation_id: str,
    request_id: str = "",
    *,
    model_id: str = DEFAULT_REPORT_MODEL_ID,
    model_name: str = DEFAULT_REPORT_MODEL_NAME,
    mode: str = "craft",
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
) -> ApiResult:
    """发一条对话活跃上报（点亮连登 + 解锁领养前置 first_buddy）。"""
    ev = chat_request_event(
        acct.uid,
        conversation_id=conversation_id,
        request_id=request_id,
        model_id=model_id,
        model_name=model_name,
        mode=mode,
    )
    return report_events(acct, [ev], timeout=timeout, transport=transport)


__all__ = [
    "GROWTH_BASE",
    "BILLING_BASE",
    "WEB_BASE",
    "DEFAULT_TIMEOUT",
    "REFRESH_WINDOW_MS",
    "DEFAULT_TRAVEL_LOCATION_ID",
    "DEFAULT_REPORT_MODEL_ID",
    "DEFAULT_REPORT_MODEL_NAME",
    "NIGHT_MODEL_ID",
    "NIGHT_MODEL_NAME",
    "PATH_BUDDY_INFO",
    "PATH_TRAVEL_STATUS",
    "PATH_TRAVEL_DEPART",
    "PATH_TRAVEL_CLAIM",
    "PATH_STREAK",
    "PATH_TASKS",
    "PATH_ACCEPT_TASKS",
    "PATH_REPORT",
    "CLAIM_PATH_FMT",
    "BUDDY_INCOMPLETE_MARKER",
    "CST",
    "cst_now",
    "cst_day",
    "ApiResult",
    "Account",
    "account_of",
    "api_call",
    "growth_call",
    "billing_call",
    "fetch_buddy",
    "fetch_travel_status",
    "travel_depart",
    "travel_claim",
    "buddy_agreement",
    "buddy_first",
    "fetch_streak",
    "fetch_energy",
    "is_buddy_task_incomplete",
    "list_tasks",
    "find_task",
    "accept_tasks",
    "claim_task",
    "chat_request_event",
    "report_events",
    "report_chat_activity",
]
