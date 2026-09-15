#!/usr/bin/env python3
"""school.py — 开学季活动任务（对应参考实现 scripts/school_open_day_2026.py）。

上游把「开学季任务」当第五类定时任务（默认 12 点）跑：点亮活动任务 → 领奖 → 把 claim
发下来的抽奖次数**抽到空**。本模块把那份一次性脚本的语义搬进服务里，保持三条底线：

  1. **只碰可自动完成的动作**：`task_student_verify`（微信学生认证）是人工环节，
     一律跳过，不伪造；服务端新增的未知 task_code 无分类证据 → 保守跳过。
  2. **活动下线即退出**：`in_period=false` 时任务与抽奖全部跳过、正常返回，不算失败——
     开学季是会结束的活动，下线后每天报失败只是噪音。
  3. **写动作有间隔**：每次写请求之间 gap（默认 1.5s，最小 1.0s），抽奖抽到余额归零即止；
     余额不降（服务端异常）连续 3 次即提前停，避免死循环。

端点（都在 www.codebuddy.cn 的 /portal/activity/school 下，小程序 UA 形状）：
    GET  /tasks                    任务列表 + in_period
    POST /tasks/{code}/viewed      接任务（pending → in_progress）
    POST /tasks/share-complete     share_invite 的完成判据 {"channel":"wechat"}
    POST /tasks/{code}/claim       领奖
    GET  /config                   盘面 + 抽奖余额 data.chance.balance
    POST /wheel/draw               {"draw_uuid": uuid} 抽一次（409/40900 = 次数耗尽，正常收尾）
    POST /v2/report                事件上报（小程序 / 桌面指纹 / 专家三类判据）

与参考脚本的差异（有意为之，不改变对外行为）：
  - 网络层换成 httpx + transport 注入，离线测试可以完整跑状态机；
  - 账号来源从 auths/ 目录换成 CredentialManager（本项目读桌面端单一登录态）；
  - global 账号门控交给调度器统一处理（本模块只负责 CN 活动）。
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid as uuidlib
from typing import Callable

import growth_api
from growth_api import DEFAULT_TIMEOUT
from task_result import TaskOutcome

KEY = "school"

DEFAULT_SCHOOL_HOURS: tuple[int, ...] = (12,)

SCHOOL_BASE = "https://www.codebuddy.cn"
SCHOOL_PREFIX = "/portal/activity/school"
#: 活动标识：事件必须带 activityId，否则服务端不把事件关联到活动任务。
ACTIVITY_ID = "school_open_day_2026"

PATH_TASKS = f"{SCHOOL_PREFIX}/tasks"
PATH_CONFIG = f"{SCHOOL_PREFIX}/config"
PATH_WHEEL_DRAW = f"{SCHOOL_PREFIX}/wheel/draw"
PATH_SHARE_COMPLETE = f"{SCHOOL_PREFIX}/tasks/share-complete"
PATH_VIEWED_FMT = f"{SCHOOL_PREFIX}/tasks/{{code}}/viewed"
PATH_CLAIM_FMT = f"{SCHOOL_PREFIX}/tasks/{{code}}/claim"
PATH_MARKET_EXPERT_LIST = "/v2/operation-platform/market/expert/list"

#: 小程序由微信注入 UA；桌面上报用桌面 UA。
MP_UA = "Mozilla/5.0 (Linux; Android 14; MicroMessenger/8.0.49 WeChat/0.8.0 MiniProgramEnv/android; wkbrowser xweb)"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"

DEFAULT_GAP = 1.5
MIN_GAP = 1.0
#: 网络/5xx 重试次数与退避基数（参考脚本口径）。
RETRIES = 3
RETRY_GAP = 1.0
#: 抽奖「余额不降」连续次数上限：服务端异常时提前停，防死循环。
STALL_LIMIT = 3

#: 任务分类表（task_code → 处理方式）。未在表内（服务端新增任务）按 unknown 保守跳过。
#:   manual  人工环节（学生认证/验证码/审核），一律跳过
#:   share   share-complete 端点点亮
#:   report  /v2/report 事件上报点亮（mini chat / 桌面指纹 6 连 / 专家事件）
KNOWN_TASKS = {
    "task_student_verify": {"mode": "manual", "note": "微信学生认证（人工，不碰）"},
    "share_invite": {"mode": "share", "note": "分享活动给好友；share-complete 点亮"},
    "chat_3_times": {
        "mode": "report",
        "note": "与 AI 对话 3 次；小程序 chat_request_send + activityId 点亮",
        "report_kind": "mini_chat",
    },
    "desktop_chat_1_time": {
        "mode": "report",
        "note": "桌面端对话 1 次；桌面指纹 6 连 + activityId 点亮",
        "report_kind": "desktop_seq",
    },
    "expert_use": {
        "mode": "report",
        "note": "召唤开学季专家并对话；BackToSchool 专家 + expert_actual_use 点亮",
        "report_kind": "expert",
    },
}

#: 抽奖奖品映射（/config prizes 的 prize_code → 标签）。未知码原样显示，不编造。
LOTTERY_PRIZE_LABELS = {
    "school_credit_6": {"label": "6积分", "type": "credit"},
    "school_credit_66": {"label": "66积分", "type": "credit"},
    "school_voucher_luckin": {"label": "瑞幸咖啡15元券", "type": "voucher"},
    "school_voucher_kfc_ok": {"label": "肯德基OK餐券", "type": "voucher"},
    "school_voucher_kfc_ice": {"label": "肯德基冰淇淋券", "type": "voucher"},
    "school_voucher_kugou": {"label": "酷狗会员月卡券", "type": "voucher"},
}

#: 开学季专家分类（带数字前缀，list API 不做归一化）。
SCHOOL_EXPERT_CATEGORY = "16-BackToSchool"
#: 分类专家列表拉取失败时的已知回落。
SCHOOL_EXPERT_FALLBACK = [
    ("ex_jB0dyFIQJEWa", "论小舟", "论文写作导师"),
    ("ex_lQjkerakvIex", "英语学习教练", "大学英语学习教练"),
]


# ---------------------------------------------------------------------------
# 请求头与 HTTP
# ---------------------------------------------------------------------------


def _mp_headers(acct: growth_api.Account, extra: dict | None = None) -> dict:
    """小程序域请求头。

    刻意**不加** X-Domain / X-Product 等桌面指纹头：活动接口对小程序与会话形状敏感，
    这里保持参考脚本实测通过的形状（Bearer + 小程序 UA）。
    """
    h = {
        "Authorization": acct.headers.get("Authorization", ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": MP_UA,
    }
    if extra:
        h.update(extra)
    return {k: v for k, v in h.items() if v}


def _desktop_headers(acct: growth_api.Account) -> dict:
    """桌面指纹上报头（小程序头 + X-Product/SaaS + 桌面 UA）。"""
    return _mp_headers(acct, {"X-Product": "SaaS", "User-Agent": DESKTOP_UA})


def _call(
    acct: growth_api.Account,
    method: str,
    path: str,
    body=None,
    *,
    base_url: str = SCHOOL_BASE,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """活动接口调用：网络失败与 5xx 按 RETRY_GAP 退避重试 RETRIES 次，其余直接返回。"""
    last: growth_api.ApiResult | None = None
    for i in range(1, RETRIES + 1):
        res = growth_api.api_call(
            base_url,
            path,
            headers or _mp_headers(acct),
            method=method,
            body=body,
            timeout=timeout,
            transport=transport,
        )
        if res.status == -1 or 500 <= res.status < 600:
            last = res
            if i < RETRIES:
                if log:
                    log(f"[school] WARN {path} 第 {i}/{RETRIES} 次失败（{res.summary()}），重试")
                time.sleep(RETRY_GAP * i)
            continue
        return res
    return last or growth_api.ApiResult(False, -1, None, "未知失败")


def derive_device_id(uid: str, salt: str) -> str:
    """由 uid 稳定派生设备标识（36 位 hex）。

    幂等：同一账号每次生成相同值（模拟固定设备），只用于事件指纹，不参与任何业务逻辑。
    """
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()[:36]


def mask_token(token: str) -> str:
    """脱敏 token，只留首 8 位（日志用）。"""
    return (token[:8] + "...") if token else "(none)"


# ---------------------------------------------------------------------------
# 只读盘点
# ---------------------------------------------------------------------------


def fetch_school_tasks(
    acct: growth_api.Account,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict], bool, growth_api.ApiResult]:
    """拉活动任务列表。返回 (tasks, in_period, ApiResult)。"""
    res = _call(acct, "GET", PATH_TASKS, timeout=timeout, transport=transport, log=log)
    if not res.ok:
        return [], False, res
    data = res.data if isinstance(res.data, dict) else {}
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    return tasks, bool(data.get("in_period")), res


def fetch_lottery_config(
    acct: growth_api.Account,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[dict, dict, growth_api.ApiResult]:
    """查抽奖盘面与余额。返回 (config, chance, ApiResult)；chance.balance 即抽奖次数。"""
    res = _call(acct, "GET", PATH_CONFIG, timeout=timeout, transport=transport, log=log)
    if not res.ok:
        return {}, {}, res
    data = res.data if isinstance(res.data, dict) else {}
    return data, (data.get("chance") or {}), res


# ---------------------------------------------------------------------------
# 点亮动作
# ---------------------------------------------------------------------------


def post_viewed(
    acct: growth_api.Account,
    code: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """接任务（pending → in_progress）。"""
    return _call(
        acct, "POST", PATH_VIEWED_FMT.format(code=code), timeout=timeout, transport=transport, log=log
    )


def post_share_complete(
    acct: growth_api.Account,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """share_invite 的完成判据。"""
    return _call(
        acct,
        "POST",
        PATH_SHARE_COMPLETE,
        {"channel": "wechat"},
        timeout=timeout,
        transport=transport,
        log=log,
    )


def post_claim(
    acct: growth_api.Account,
    code: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """领奖（completed → claimed，会发抽奖机会）。"""
    return _call(
        acct, "POST", PATH_CLAIM_FMT.format(code=code), timeout=timeout, transport=transport, log=log
    )


# ---------------------------------------------------------------------------
# 上报事件
# ---------------------------------------------------------------------------


def _desktop_fingerprint(acct: growth_api.Account) -> dict:
    """桌面指纹：注入每个桌面事件（覆盖同名业务键）。"""
    now = int(time.time() * 1000)
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": acct.uid,
        "username": acct.nickname,
        "userNickname": acct.nickname,
        "product": "SaaS",
        "releaseDate": 1789036585355,
        "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": "5.5.6",
        "machineId": derive_device_id(acct.uid, "machine"),
        "sessionId": derive_device_id(acct.uid, "session"),
        "extName": "workbuddy-desktop",
        "extVersion": "5.5.6",
        "os": "win32",
        "arch": "x64",
        "osVersion": "10.0.26220",
        "cpuCores": 20,
        "memorySize": 24,
        "timestamp": now,
        "presentAt": now,
    }


def _mini_chat_event(acct: growth_api.Account, conversation_id: str) -> dict:
    """小程序域 chat_request_send（点亮 chat_3_times）。必须带 activityId。"""
    now = int(time.time() * 1000)
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "source": "mini_program",
        "ideName": "wx_app_cloud",
        "ideType": "WorkBuddy_MP",
        "extName": "workbuddy-mp",
        "extVersion": "SaaS",
        "mode": "chat",
        "conversationId": conversation_id,
        "requestId": conversation_id,
        "inputLength": 12,
        "activityId": ACTIVITY_ID,
        "mentionContexts": [],
        "mentionContextCount": 0,
        "userId": acct.uid,
    }


def _desktop_chat_sequence(
    acct: growth_api.Account, conversation_id: str, request_id: str, message_id: str
) -> list[dict]:
    """桌面端成功对话 6 连事件链（一次 6 连 = 一次桌面对话，点亮 desktop_chat_1_time）。"""
    now = int(time.time() * 1000)
    ev: list[dict] = []

    def mk(code: str, extra: dict) -> None:
        e = {"eventCode": code}
        e.update(extra)
        ev.append(e)

    mk(
        "agent_task_created",
        {
            "source": "LOCAL", "name": "working", "task_target": "local", "mode": "craft",
            "requestModelId": "fast-model", "requestModelName": "fast-model",
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [],
            "has_mention": False, "mention_types": [],
            "has_template": False, "action": "", "template_name": "",
            "has_expert": False, "expert_id": "", "expert_name": "", "expert_industry_id": "",
            "has_skill": False, "skill_names": [],
            "conversationId": conversation_id, "messageId": message_id,
            "buddyId": "", "buddyName": "",
        },
    )
    mk(
        "chat_message_send",
        {
            "messageId": message_id + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1,
            "traceId": request_id, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
        },
    )
    mk(
        "chat_request_send",
        {
            "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": request_id, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        },
    )
    mk(
        "chat_message_response",
        {
            "messageId": message_id + "-assistant", "responseModelId": "fast-model",
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now, "traceId": request_id,
            "conversationId": conversation_id,
            "rootRequestId": request_id, "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conversation_id,
            "codebuddy.conversation_request_id": request_id,
        },
    )
    mk(
        "chat_message_status",
        {
            "messageId": message_id + "-assistant", "messageErrorCode": "0",
            "traceId": request_id, "rootRequestId": request_id,
            "parentConversationId": conversation_id,
            "agentName": "cli", "agentType": "main",
        },
    )
    mk(
        "chat_request_response",
        {
            "mode": "craft", "toolCallCount": 0,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": request_id, "parentConversationId": conversation_id,
        },
    )
    return ev


def _expert_event(acct: growth_api.Account, expert_id: str, expert_name: str, conversation_id: str) -> dict:
    """expert_actual_use 事件（点亮 expert_use），必须带 activityId。"""
    now = int(time.time() * 1000)
    return {
        "eventCode": "expert_actual_use",
        "timestamp": now,
        "reportDelay": 0,
        "source": "mini_program",
        "ideName": "wx_app_cloud",
        "ideType": "WorkBuddy_MP",
        "extName": "workbuddy-mp",
        "extVersion": "SaaS",
        "machineId": derive_device_id(acct.uid, "machine"),
        "os": "android",
        "osVersion": "14",
        "arch": "arm64",
        "timezone": "Asia/Shanghai",
        "userId": acct.uid,
        "userNickname": acct.nickname,
        "id": expert_id,
        "name": expert_id,
        "expertTitle": expert_name,
        "type": "send_message",
        "characterCount": 12,
        "expertType": "agent",
        "conversationId": conversation_id,
        "activityId": ACTIVITY_ID,
    }


def fetch_school_expert(
    acct: growth_api.Account,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str, str]:
    """拉一个 BackToSchool 分类的真实开学季专家；失败回落已知专家。"""
    body = {
        "edition_mode": "all,domestic",
        "page": 1,
        "page_size": 20,
        "sort_by": "use_count",
        "sort_order": "desc",
        "categories": [SCHOOL_EXPERT_CATEGORY],
        "expert_type": "agent",
    }
    res = _call(
        acct,
        "POST",
        PATH_MARKET_EXPERT_LIST,
        body,
        timeout=timeout,
        transport=transport,
        log=log,
    )
    experts = (res.data or {}).get("experts") if (res.ok and isinstance(res.data, dict)) else None
    for e in experts or []:
        eid = e.get("expert_id")
        if not eid:
            continue
        dn = e.get("display_name_zh") or {}
        name = (dn.get("zh") if isinstance(dn, dict) else dn) or eid
        pf = e.get("profession_zh") or {}
        prof = (pf.get("zh") if isinstance(pf, dict) else pf) or ""
        return str(eid), str(name), str(prof)
    if not res.ok:
        if log:
            log(f"[school] WARN 拉取 BackToSchool 专家失败（{res.summary()}），回落已知专家")
    eid, name, prof = SCHOOL_EXPERT_FALLBACK[0]
    return eid, name, prof


def build_report_events(
    acct: growth_api.Account,
    kind: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict], str, dict | None]:
    """按 report_kind 构造事件数组。返回 (events, base_url, extra_headers)。

    desktop_seq 走 copilot 域（发 codebuddy.cn 域不点亮桌面任务）；另两类走活动域。
    """
    now = int(time.time() * 1000)
    if kind == "mini_chat":
        return [_mini_chat_event(acct, f"wbmp-{now}")], SCHOOL_BASE, None
    if kind == "expert":
        eid, name, _prof = fetch_school_expert(
            acct, timeout=timeout, transport=transport, log=log
        )
        return [_expert_event(acct, eid, name, f"wbexp-{now}")], SCHOOL_BASE, None
    if kind == "desktop_seq":
        conv = f"wbdesk-{now}"
        seq = _desktop_chat_sequence(acct, conv, conv, conv)
        fp = _desktop_fingerprint(acct)
        events = []
        for e in seq:
            m = dict(e)
            m.update(fp)
            m["activityId"] = ACTIVITY_ID
            events.append(m)
        return events, growth_api.GROWTH_BASE, {"X-Product": "SaaS", "User-Agent": DESKTOP_UA}
    raise ValueError(f"未知 report_kind: {kind}")


def report_events(
    acct: growth_api.Account,
    events: list[dict],
    *,
    base_url: str = SCHOOL_BASE,
    extra_headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """把事件数组发到指定域。"""
    headers = _mp_headers(acct, extra_headers)
    return _call(
        acct,
        "POST",
        growth_api.PATH_REPORT,
        events,
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        transport=transport,
        log=log,
    )


# ---------------------------------------------------------------------------
# 抽奖
# ---------------------------------------------------------------------------


def lottery_prize_text(prize_code: str, credit_amount: int) -> str:
    """prize_code → 语义化文本；未知码原样返回，不编造。"""
    info = LOTTERY_PRIZE_LABELS.get(prize_code)
    if not info:
        return f"{prize_code}"
    if info["type"] == "credit":
        return f"{info['label']}（+{credit_amount} Credit）"
    return info["label"]


def draw_once(
    acct: growth_api.Account,
    draw_uuid: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    transport: object | None = None,
    log: Callable[[str], None] | None = None,
) -> growth_api.ApiResult:
    """抽一次转盘（draw_uuid 每轮一次性，抽后即弃）。"""
    return _call(
        acct,
        "POST",
        PATH_WHEEL_DRAW,
        {"draw_uuid": draw_uuid},
        timeout=timeout,
        transport=transport,
        log=log,
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


class SchoolRunner:
    """单账号开学季任务：点亮 + 领奖 + 抽空抽奖余额。线程安全。"""

    def __init__(
        self,
        cred,
        *,
        gap: float = DEFAULT_GAP,
        timeout: float = DEFAULT_TIMEOUT,
        transport: object | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.cred = cred
        self.gap = max(MIN_GAP, float(gap))
        self.timeout = float(timeout)
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
            "activity_id": ACTIVITY_ID,
            "gap": self.gap,
            "known_tasks": sorted(KNOWN_TASKS),
            "last": self.last.as_dict() if self.last else None,
        }

    # ------------------------------------------------------------------ 内部

    def _store(self, oc: TaskOutcome) -> TaskOutcome:
        self.last = oc
        self._log(f"[school] {oc.summary}")
        return oc

    def _run(self, acct: growth_api.Account, reason: str) -> TaskOutcome:
        counters = {
            "accounts": 1,
            "ok": 0,
            "already": 0,
            "skip": 0,
            "pending": 0,
            "fail": 0,
        }
        detail: dict = {"reason": reason, "uid": acct.uid, "counters": counters, "steps": []}

        tasks, in_period, res = fetch_school_tasks(
            acct, timeout=self.timeout, transport=self.transport, log=self._log
        )
        if not res.ok:
            counters["fail"] += 1
            return TaskOutcome(KEY, False, f"拉活动任务失败：{res.summary()}", detail)
        detail["in_period"] = in_period
        detail["tasks"] = len(tasks)
        if not in_period:
            return TaskOutcome(KEY, True, "活动非进行期（in_period=false），跳过", detail)

        for task in tasks:
            code = task.get("task_code")
            if not code:
                continue
            self._handle_task(acct, task, counters, detail)

        lottery = self._lottery(acct)
        detail["lottery"] = lottery

        ok = counters["fail"] == 0
        summary = (
            f"任务 {len(tasks)} 个：点亮 {counters['ok']} / 已领 {counters['already']} / "
            f"跳过 {counters['skip']} / 待做 {counters['pending']} / 失败 {counters['fail']}"
        )
        if lottery:
            summary += f"；抽奖 {lottery.get('draws', 0)} 次（积分 +{lottery.get('credit', 0)}）"
        return TaskOutcome(KEY, ok, summary, detail)

    def _handle_task(
        self, acct: growth_api.Account, task: dict, counters: dict, detail: dict
    ) -> None:
        code = str(task.get("task_code"))
        status = str(task.get("status") or "?").lower()
        spec = KNOWN_TASKS.get(code, {})
        mode = spec.get("mode", "unknown")
        progress = int(task.get("progress") or 0)
        target = int(task.get("target_count") or 1)
        step = {"task_code": code, "status": status, "progress": f"{progress}/{target}", "mode": mode}

        if status in ("completed", "claimed"):
            counters["already"] += 1
            step["result"] = "already"
            detail["steps"].append(step)
            return

        if mode == "manual":
            counters["skip"] += 1
            step.update({"result": "skip", "reason": spec.get("note")})
            detail["steps"].append(step)
            return

        if mode not in ("share", "report"):
            counters["skip"] += 1
            step.update({"result": "skip", "reason": "未知任务类型，无分类证据，保守跳过"})
            detail["steps"].append(step)
            return

        # 1) pending → viewed 激活（接单，H5 行为）
        if status == "pending":
            r = post_viewed(acct, code, timeout=self.timeout, transport=self.transport, log=self._log)
            step["viewed"] = {"ok": r.ok, "status": r.status, "code": r.code}
            if not r.ok:
                counters["fail"] += 1
                step["result"] = "fail"
                detail["steps"].append(step)
                return
            time.sleep(self.gap)

        # 2) 触发完成判据：share 一次即完成；report 按 rounds 循环（每次事件 +1）
        if mode == "share":
            rounds = 1
        elif spec.get("report_kind") == "desktop_seq":
            rounds = 1  # 6 连一次 = 一次桌面对话
        else:
            rounds = max(1, target - progress)

        changed = False
        cur = progress
        triggers: list[dict] = []
        for i in range(rounds):
            if mode == "share":
                r = post_share_complete(
                    acct, timeout=self.timeout, transport=self.transport, log=self._log
                )
            else:
                events, base_url, extra = build_report_events(
                    acct,
                    spec.get("report_kind"),
                    timeout=self.timeout,
                    transport=self.transport,
                    log=self._log,
                )
                r = report_events(
                    acct,
                    events,
                    base_url=base_url,
                    extra_headers=extra,
                    timeout=self.timeout,
                    transport=self.transport,
                    log=self._log,
                )
            triggers.append({"round": i + 1, "ok": r.ok, "status": r.status, "code": r.code})
            if not r.ok:
                break
            time.sleep(self.gap)
            # 每轮回读一次，判断是否已到 target/completed，提早终止
            t2 = self._refetch(acct, code)
            if not t2:
                continue
            after_prog = int(t2.get("progress") or 0)
            if after_prog > cur:
                cur = after_prog
                changed = True
            if str(t2.get("status") or "").lower() in ("completed", "claimed") or after_prog >= target:
                break
        step["triggers"] = triggers

        # 3) 最终回读：确认是否点亮
        t2 = self._refetch(acct, code)
        if not t2:
            counters["fail"] += 1
            step["result"] = "fail"
            detail["steps"].append(step)
            return
        after = str(t2.get("status") or "?").lower()
        after_prog = int(t2.get("progress") or 0)
        step["after"] = f"{after}/{after_prog}"
        if after in ("completed", "claimed") or changed:
            counters["ok"] += 1
            step["result"] = "lit"
        else:
            counters["pending"] += 1
            step["result"] = "pending"

        # 4) completed → claim 领奖（发抽奖机会）
        if after == "completed":
            rc = post_claim(acct, code, timeout=self.timeout, transport=self.transport, log=self._log)
            step["claim"] = {"ok": rc.ok, "status": rc.status, "code": rc.code}
            if not rc.ok:
                counters["fail"] += 1
            else:
                time.sleep(self.gap)
        detail["steps"].append(step)

    def _refetch(self, acct: growth_api.Account, code: str) -> dict | None:
        tasks, _in_period, _res = fetch_school_tasks(
            acct, timeout=self.timeout, transport=self.transport, log=self._log
        )
        for t in tasks:
            if t.get("task_code") == code:
                return t
        return None

    def _lottery(self, acct: growth_api.Account) -> dict:
        """抽奖段：查余额 → 抽到空/异常 → 汇总。活动非进行期或余额 0 直接返回。"""
        cfg, chance, res = fetch_lottery_config(
            acct, timeout=self.timeout, transport=self.transport, log=self._log
        )
        if not res.ok:
            return {"ok": False, "error": res.summary()}
        if not cfg.get("in_period"):
            return {"ok": True, "skipped": "in_period=false"}
        bal = int(chance.get("balance") or 0)
        total_earned = int(chance.get("total_earned") or 0)
        if bal <= 0:
            return {"ok": True, "balance": 0, "total_earned": total_earned, "draws": 0}

        results: list[dict] = []
        credit_total = 0
        prev = bal
        stall = 0
        while bal > 0:
            r = draw_once(
                acct,
                str(uuidlib.uuid4()),
                timeout=self.timeout,
                transport=self.transport,
                log=self._log,
            )
            if not r.ok:
                if r.code == 40900:  # 次数耗尽（no chance）：边界，正常收尾
                    bal = 0
                    break
                return {
                    "ok": False,
                    "draws": len(results),
                    "credit": credit_total,
                    "results": results,
                    "error": r.summary(),
                    "balance": bal,
                }
            data = r.data if isinstance(r.data, dict) else {}
            prize = str(data.get("prize_code") or "?")
            credit = int(data.get("credit_amount") or 0)
            bal = int(data.get("chance_balance", bal))
            if bal >= prev:
                stall += 1
                if stall >= STALL_LIMIT:
                    self._log("[school] WARN 抽奖余额未递减（服务端异常），提前停")
                    break
            else:
                stall = 0
            prev = bal
            results.append(
                {"prize_code": prize, "credit": credit, "label": lottery_prize_text(prize, credit)}
            )
            credit_total += credit
            if bal > 0:
                time.sleep(self.gap)

        self._log(
            f"[school] 抽奖 {len(results)} 次，积分增量 {credit_total}，剩余次数 {bal}"
        )
        for x in results:
            self._log(f"[school]   - {x['label']}")
        return {
            "ok": bal == 0,
            "draws": len(results),
            "credit": credit_total,
            "balance": bal,
            "total_earned": total_earned,
            "results": results,
        }


__all__ = [
    "KEY",
    "DEFAULT_SCHOOL_HOURS",
    "SCHOOL_BASE",
    "SCHOOL_PREFIX",
    "ACTIVITY_ID",
    "MP_UA",
    "DESKTOP_UA",
    "DEFAULT_GAP",
    "KNOWN_TASKS",
    "LOTTERY_PRIZE_LABELS",
    "SCHOOL_EXPERT_CATEGORY",
    "SCHOOL_EXPERT_FALLBACK",
    "fetch_school_tasks",
    "fetch_lottery_config",
    "post_viewed",
    "post_share_complete",
    "post_claim",
    "build_report_events",
    "report_events",
    "draw_once",
    "lottery_prize_text",
    "fetch_school_expert",
    "derive_device_id",
    "mask_token",
    "SchoolRunner",
]
