#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏

    def desensitize_body(
        body,
        roles=("system",),
        desensitize_harness_user=False,
        desensitize_tools=False,
        compact_harness=False,
        strip_tool_metadata=False,
    ):
        return body


from anthropic_adapter import (
    AnthropicStreamConverter,
    anthropic_request_to_chat,
)
from checkin import billing_ua
from model_registry import (
    FALLBACK_MODELS,
    ModelFetchError,
    ModelRegistry,
)
from responses_adapter import (
    ResponsesStreamConverter,
    responses_request_to_chat,
)
from responses_projection import project_responses_chat_body
from task_scheduler import TASK_KEYS, TASK_LABELS, TaskConfig, TaskScheduler

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------


def auth_dirs() -> list[Path]:
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [
            home
            / "Library"
            / "Application Support"
            / "CodeBuddyExtension"
            / "Data"
            / "Public"
            / "auth"
        ]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                return f
    return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self, within_ms: int = 60_000) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 within_ms 判定过期；within_ms <= 0 表示只看硬过期
        return time.time() * 1000 >= (expires_at - max(0, within_ms))

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = (
                int(time.time() * 1000) + new_auth["expiresIn"] * 1000
            )
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = (
                int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
            )
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken', '')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self, within_ms: int = 60_000) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。

        within_ms：提前多少毫秒判定「即将过期」并主动刷新。默认 60s（聊天路径，
        宁可请求时再刷）；签到等定时任务传 2h 窗口，避免到点时 token 已过期。
        """
        with self._lock:
            if self._is_expired(within_ms):
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
#
# 解析顺序（前者可用即返回，逐级降级）：
#   1. 上游动态拉取   GET {BACKEND}/console/enterprises/personal/models（CLI 白名单裁剪）
#   2. 本地快照       models-snapshot.json（上游失败时的上一份真实列表，标记 stale-*）
#   3. 本机 product.json（WorkBuddy 安装目录，见 _load_models_from_workbuddy）
#   4. 内置回退表     DEFAULT_MODELS（仅当以上全不可用）
# ---------------------------------------------------------------------------

#: 最终兜底表；内容与 model_registry.FALLBACK_MODELS 同源（上游实测，2026-09-12）。
DEFAULT_MODELS = list(FALLBACK_MODELS)

# 标识非聊天模型的 tag（需要过滤掉）
NON_CHAT_MODEL_TAGS = {
    "text-to-image",
    "image-to-image",
    "text-to-video",
}


def _find_workbuddy_product_json() -> Path | None:
    """
    查找本机 WorkBuddy 应用的 product.json 配置文件。

    WorkBuddy 在安装时会自动解压 asar 到 app.asar.unpacked 目录，
    因此无需用户手动提取。

    macOS: /Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json
    Windows: %LOCALAPPDATA%\\Programs\\WorkBuddy\\resources\\app.asar.unpacked\\cli\\product.json
    Linux: /opt/WorkBuddy/resources/app.asar.unpacked/cli/product.json

    Returns:
        Path 对象如果找到配置文件，否则 None
    """
    possible_paths = []

    if sys.platform == "darwin":  # macOS
        possible_paths.extend(
            [
                # 标准安装路径（WorkBuddy 自动解压）
                Path(
                    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/product.json"
                ),
                # 开发/调试：本地提取的目录
                Path.home()
                / "Desktop/workspace/opensource/codebuddy2api/workbuddy_extracted/cli/product.json",
            ]
        )
    elif sys.platform == "win32":  # Windows
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        possible_paths.extend(
            [
                local_app_data
                / "Programs/WorkBuddy/resources/app.asar.unpacked/cli/product.json",
                Path(
                    "C:/Program Files/WorkBuddy/resources/app.asar.unpacked/cli/product.json"
                ),
            ]
        )
    else:  # Linux
        possible_paths.extend(
            [
                Path("/opt/WorkBuddy/resources/app.asar.unpacked/cli/product.json"),
                Path.home() / ".local/share/WorkBuddy/cli/product.json",
            ]
        )

    for path in possible_paths:
        if path.exists() and path.is_file():
            return path

    return None


def _load_models_from_workbuddy() -> list[str]:
    """
    从本机 WorkBuddy product.json 读取模型列表。

    过滤规则：
    1. 只保留聊天模型（排除 text-to-image, text-to-video 等）
    2. 排除 vendor 为 "tencent" 的内部模型（通常是补全/内部专用）
    3. 返回模型 ID 列表

    Returns:
        模型 ID 列表，如果加载失败返回空列表
    """
    product_json_path = _find_workbuddy_product_json()

    if product_json_path is None:
        return []

    try:
        with open(product_json_path, encoding="utf-8") as f:
            data = json.load(f)

        models = data.get("models", [])
        chat_models = []

        for model in models:
            model_id = model.get("id")
            if not model_id:
                continue

            # 过滤掉非聊天模型
            tags = model.get("tags", [])
            if any(tag in NON_CHAT_MODEL_TAGS for tag in tags):
                continue

            # 过滤掉内部模型（vendor 为 tencent 的通常是补全/跳转等内部功能）
            vendor = model.get("vendor", "")
            if vendor == "tencent":
                continue

            # 过滤掉名称中明显是补全/内部功能的模型
            name_lower = model_id.lower()
            if any(
                keyword in name_lower
                for keyword in ["completion", "rewrite", "jump", "codewise"]
            ):
                continue

            chat_models.append(model_id)

        return chat_models

    except Exception as e:
        # 解析失败时静默降级，不影响服务启动
        print(
            f"Warning: Failed to load models from WorkBuddy product.json: {e}",
            file=sys.stderr,
        )
        return []


def _safe_cred_headers(within_ms: int = 60_000) -> dict | None:
    """取一份带最新 token 的请求头；取不到返回 None（调用方自行回退）。"""
    cred = CONFIG.get("cred")
    if cred is None:
        return None
    try:
        return cred.get_headers(within_ms=within_ms)
    except Exception as e:  # noqa: BLE001
        _log(f"[models] WARN 取凭据失败，走回退：{e}")
        return None


def get_available_models() -> list[str]:
    """
    获取可用的模型 ID 列表（兼容旧调用）。

    优先动态拉取，其次本机 product.json，最后内置表。详见 resolve_models()。
    """
    models, _source = resolve_models()
    return [m["id"] for m in models]


def resolve_models(force: bool = False) -> tuple[list[dict], str]:
    """
    解析模型列表，返回 (OpenAI 格式条目, 来源标记)。

    来源标记：upstream / cache / stale-memory / stale-snapshot / local-product / static。
    """
    reg = CONFIG.get("registry")
    if reg is not None:
        headers = _safe_cred_headers()
        try:
            infos, source = reg.resolve(headers, force=force)
            return [i.to_dict() for i in infos], source
        except ModelFetchError as e:
            _log(f"[models] WARN 动态拉取不可用：{e}")
        except Exception as e:  # noqa: BLE001
            _log(f"[models] WARN 动态拉取异常：{e}")

    local = _load_models_from_workbuddy()
    if local:
        return [{"id": m, "object": "model", "owned_by": "codebuddy"} for m in local], "local-product"
    return [{"id": m, "object": "model", "owned_by": "codebuddy"} for m in DEFAULT_MODELS], "static"


# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "stream",
    "stream_options",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "n",
    "response_format",
    "seed",
    "user",
    "reasoning_effort",
    "verbosity",
    "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
CONFIG: dict = {
    "api_key": "",
    "cred": None,
    "log_path": None,
    "desensitize": False,
    "no_compact": False,
    "registry": None,
    "checkin": None,
    "tasks": None,
}
# cred: CredentialManager | None；registry: ModelRegistry | None
# checkin: CheckinScheduler | None（签到执行体，供 /admin/checkin 沿用旧结构）
# tasks:   TaskScheduler | None（六类定时任务的统一排程）


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程


def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _pad_label(s: str, width: int = 16) -> str:
    """按显示宽度（CJK 算 2 列）左侧补齐，让启动横幅里的中文标签能对齐。"""
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(1, width - w)


def _check_auth(authorization: str | None, x_api_key: str | None):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "invalid api key", "type": "auth_error"}},
        )


def _cred() -> CredentialManager:
    if CONFIG["cred"] is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy",
                    "type": "auth_error",
                }
            },
        )
    return CONFIG["cred"]


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {
        "status": "ok",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "auth_file": str(find_auth_file() or "(未找到)"),
        "mode": "direct-proxy (native function calling)",
    }
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    reg = CONFIG.get("registry")
    if reg is not None:
        st = reg.status()
        info["models"] = {
            "source": st["source"],
            "count": st["models"],
            "age_seconds": st["age_seconds"],
            "fresh": st["fresh"],
        }
    sched = CONFIG.get("checkin")
    if sched is not None:
        st = sched.status()
        info["checkin"] = {
            "enabled": st["enabled"],
            "hours": st["hours"],
            "next_fire_at": st["next_fire_at"],
            "last": st["last"],
        }
    tasks = CONFIG.get("tasks")
    if tasks is not None:
        st = tasks.status()
        info["tasks"] = {
            "alive": st["alive"],
            "next_wake_at": st["next_wake_at"],
            "next_wake_tasks": st["next_wake_tasks"],
            "enabled": [k for k in TASK_KEYS if st["tasks"][k]["enabled"]],
            "last": {k: (st["tasks"][k]["last"] or {}).get("summary") for k in TASK_KEYS},
        }
    return info


@app.get("/v1/models")
def list_models(
    refresh: int = 0,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI 兼容模型列表。

    默认走缓存（TTL 1h，见 model_registry）；`?refresh=1` 强制重拉上游。
    """
    _check_auth(authorization, x_api_key)
    data, source = resolve_models(force=bool(refresh))
    for item in data:
        item.setdefault("created", 1700000000)
    resp = {"object": "list", "data": data}
    if source.startswith("stale-") or source in ("static", "local-product"):
        # 非新鲜来源时给出标记，方便客户端判断列表是否可能过期
        resp["x_models_source"] = source
    return resp


# ---------------------------------------------------------------------------
# 管理端点（模型缓存 / 自动签到 / 六类定时任务）
# ---------------------------------------------------------------------------


@app.get("/admin/models")
def admin_models(
    refresh: int = 0,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """模型注册表状态；`?refresh=1` 强制重拉一次。"""
    _check_auth(authorization, x_api_key)
    reg = CONFIG.get("registry")
    if reg is None:
        return {"enabled": False, "reason": "动态模型未启用（--no-dynamic-models）"}
    if refresh:
        try:
            resolve_models(force=True)
        except Exception as e:  # noqa: BLE001
            _log(f"[models] WARN 强制刷新失败：{e}")
    st = reg.status()
    if refresh:
        st["source"] = "manual-refresh"
    return st


@app.get("/admin/checkin")
def admin_checkin_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """签到调度状态 + 最近结果。

    签到的**排程**现在由六类任务调度器统一负责（保持与旅行/活跃同槽并行、共用补跑窗口），
    这里返回的 `alive` 因此反映的是任务调度线程；`scheduled_by` 标明这一点，
    免得看到 `alive=false` 误以为签到没在跑。
    """
    _check_auth(authorization, x_api_key)
    sched = CONFIG.get("checkin")
    if sched is None:
        return {"enabled": False, "reason": CONFIG.get("checkin_reason") or "自动签到未启用"}
    st = sched.status()
    tasks = CONFIG.get("tasks")
    if tasks is not None:
        st["scheduled_by"] = "task_scheduler"
        st["alive"] = bool(tasks.is_alive() and tasks.enabled.get("checkin"))
    return st


@app.post("/admin/checkin")
def admin_checkin_run(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """立即执行一次签到 + 余额查询（不影响既定排程）。"""
    _check_auth(authorization, x_api_key)
    sched = CONFIG.get("checkin")
    if sched is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": CONFIG.get("checkin_reason") or "自动签到未启用",
                    "type": "disabled",
                }
            },
        )
    return sched.run_once(reason="manual")


@app.get("/admin/tasks")
def admin_tasks_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """六类定时任务的排程状态 + 最近结果。

    六类 = 签到 / 活跃上报 / 猫猫旅行 / token 保活 / 开学季 / 夜猫子，
    各自独立时点、独立开关，互不影响。
    """
    _check_auth(authorization, x_api_key)
    tasks = CONFIG.get("tasks")
    if tasks is None:
        return {
            "enabled": False,
            "reason": CONFIG.get("tasks_reason") or "定时任务未启用",
            "tasks": {},
        }
    return tasks.status()


@app.post("/admin/tasks")
def admin_tasks_run(
    task: str = "all",
    key: str = "",
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """立即执行任务（不影响既定排程）。

    `?task=all` 跑全部已启用任务；`?task=travel` 等跑单类。
    兼容写法：`?key=travel`（与 `?task=` 等价，二者取其一即可）。
    """
    _check_auth(authorization, x_api_key)
    tasks = CONFIG.get("tasks")
    if tasks is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": CONFIG.get("tasks_reason") or "定时任务未启用",
                    "type": "disabled",
                }
            },
        )
    which = (key or task or "all").strip().lower()
    if which in ("all", "", "*"):
        return {"results": [oc.as_dict() for oc in tasks.run_all_tasks(reason="manual")]}
    if which not in TASK_KEYS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"未知任务 {which!r}，可选：all、{', '.join(TASK_KEYS)}",
                    "type": "invalid_request",
                }
            },
        )
    return tasks.run_task(which, reason="manual").as_dict()


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 腾讯后端不支持 developer role，遇到会触发安全策略拦截（11128），统一映射为 system
    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = [
            dict(m, role="system")
            if isinstance(m, dict) and m.get("role") == "developer"
            else m
            for m in body["messages"]
        ]

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(
            body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [
        t.get("function", {}).get("name")
        for t in (payload.get("tools") or [])
        if isinstance(t, dict)
    ]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
        + (f" | tools={tool_names}" if tool_names else "")
        + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else "")
    )
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(
        f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(
                        f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8', 'replace'), 200)}"
                    )
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8', 'replace')}")
                    raise HTTPException(
                        status_code=r.status_code,
                        detail=_safe_err_raw(raw, r.status_code),
                    )
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {"message": f"upstream error: {e}", "type": "upstream_error"}
            },
        )
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(
        f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
        + (f" | tool_calls={tc_names}" if tc_names else "")
        + f" | tokens={usage.get('total_tokens', '?')}"
    )
    # 完整响应体
    _log(
        f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}"
    )


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": None, "name": None, "arguments": ""}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
        ],
        "usage": usage
        or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {
            "error": {
                "message": raw.decode("utf-8", "replace")[:500],
                "type": "upstream_error",
                "code": status,
            }
        }


async def _stream_upstream(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []  # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if (
                "content-filter" in text_repr
                or "敏感" in text_repr
                or "审核" in text_repr
            ):
                saw_filter = True

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(
                        f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8', 'replace'), 200)}"
                    )
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8', 'replace')}")
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(
        f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
        + (f" | tool_calls={tool_names}" if tool_names else "")
        + f" | tokens={usage.get('total_tokens', '?')}"
    )
    # 完整原始 SSE（后端返回的全部内容）
    _log(
        f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8', 'replace')}"
    )


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {
            "error": {
                "message": r.text[:500],
                "type": "upstream_error",
                "code": r.status_code,
            }
        }


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json

    chunk = {
        "error": {
            "message": msg.decode("utf-8", "replace")[:500],
            "type": "upstream_error",
            "code": status,
        },
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(
    url: str, headers: dict, body: dict, rid: str = "", model_name: str = "?"
) -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if (
        status == 200
        and _looks_like_content_filter_text(text)
        and CONFIG.get("desensitize")
        and CONFIG.get("no_compact")
    ):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(
            f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness"
        )
        _log(
            f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}"
        )
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------


@app.post("/v1/responses")
async def create_response(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}"
    )
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(
        f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(
            url, headers, chat_body, rid, model_name
        )
        if status_code != 200:
            _log(
                f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8', 'replace'), 200)}"
            )
            raise HTTPException(
                status_code=status_code, detail=_safe_err_raw(raw, status_code)
            )
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {"message": f"upstream error: {e}", "type": "upstream_error"}
            },
        )

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(
        f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}"
    )
    return JSONResponse(content=result)


async def _stream_responses(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, _ = await _post_backend_with_filter_retry(
            url, headers, body, rid, model_name
        )
        if status_code != 200:
            _log(
                f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8', 'replace'), 200)}"
            )
            error_evt = {
                "type": "error",
                "error": {
                    "message": raw.decode("utf-8", "replace")[:500],
                    "code": status_code,
                },
            }
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------


@app.post("/v1/messages")
async def create_message(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    chat_body.setdefault("model", "auto")
    # 读取用户的 stream 参数，如果未提供则默认为 True
    user_stream = payload.get("stream", True)
    # 无论用户如何设置，都向后端请求流式响应（后端只支持流式）
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(
            chat_body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(
        f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)} | user_stream={user_stream}"
    )
    _log(
        f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}"
    )

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    # 如果用户请求流式响应，直接返回流式
    if user_stream:
        return StreamingResponse(
            _stream_anthropic(url, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 否则，收集完整响应并返回 JSON
    from fastapi.responses import JSONResponse

    response_data = await _collect_anthropic_nonstream(
        url, headers, chat_body, model_name, t0, rid
    )
    return JSONResponse(content=response_data)


async def _collect_anthropic_nonstream(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
) -> dict:
    """收集完整的流式响应并返回非流式 Anthropic Message 对象。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(
                        f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8', 'replace'), 200)}"
                    )
                    raise HTTPException(
                        status_code=r.status_code,
                        detail={
                            "error": {
                                "message": err.decode("utf-8", "replace")[:500],
                                "type": "api_error",
                                "code": r.status_code,
                            }
                        },
                    )
                async for line in r.aiter_lines():
                    converter.feed_line(line)
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {"message": str(e)[:500], "type": "api_error", "code": 502}
            },
        ) from None

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | nonstream done")
    return converter.get_nonstream_response()


async def _stream_anthropic(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(
                        f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8', 'replace'), 200)}"
                    )
                    error_evt = {
                        "type": "error",
                        "error": {
                            "message": err.decode("utf-8", "replace")[:500],
                            "type": "api_error",
                            "code": r.status_code,
                        },
                    }
                    yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
                    return
                async for line in r.aiter_lines():
                    events = converter.feed_line(line)
                    if events:
                        yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {
            "type": "error",
            "error": {"message": str(e)[:500], "type": "api_error", "code": 502},
        }
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
        return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """Anthropic token 计数端点。

    Claude Code 在发送消息前调用此端点获取 token 计数。
    后端只支持流式请求，所以我们发送流式请求并从中提取 usage。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {"message": f"bad json: {e}", "type": "invalid_request_error"}
            },
        )

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": "messages is required",
                    "type": "invalid_request_error",
                }
            },
        )

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"request conversion error: {e}",
                    "type": "invalid_request_error",
                }
            },
        )

    # 最小化实际生成：只需要 usage 统计
    chat_body.setdefault("model", "auto")
    chat_body["max_tokens"] = 1
    chat_body["stream"] = True  # 后端只支持流式
    chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(
            chat_body,
            roles=("system", "developer"),
            desensitize_harness_user=True,
            desensitize_tools=True,
            compact_harness=not CONFIG.get("no_compact"),
            strip_tool_metadata=True,
        )

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", url, headers=headers, json=chat_body
            ) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    _log(
                        f"✗ count_tokens HTTP {resp.status_code}: {_truncate(err.decode('utf-8', 'replace'), 200)}"
                    )
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail={
                            "error": {
                                "message": err.decode("utf-8", "replace")[:500],
                                "type": "api_error",
                                "code": resp.status_code,
                            }
                        },
                    )

                # 解析 SSE 流，查找 usage 信息
                # message_start 包含初始 usage（0），message_delta 包含真实 usage
                input_tokens = 0
                async for line in resp.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            # message_delta 事件直接包含 usage
                            if "usage" in chunk:
                                usage = chunk.get("usage") or {}
                                tokens = usage.get("prompt_tokens", 0) or usage.get(
                                    "input_tokens", 0
                                )
                                if tokens > 0:
                                    input_tokens = tokens
                            # message_start 事件在 message 对象中包含 usage
                            elif "message" in chunk and "usage" in chunk["message"]:
                                usage = chunk["message"].get("usage") or {}
                                tokens = usage.get("prompt_tokens", 0) or usage.get(
                                    "input_tokens", 0
                                )
                                if tokens > 0:
                                    input_tokens = tokens
                        except json.JSONDecodeError:
                            continue

                return {"input_tokens": input_tokens}

    except httpx.HTTPError as e:
        _log(f"✗ count_tokens network error: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {"message": str(e)[:500], "type": "api_error", "code": 502}
            },
        ) from None


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write(
            "\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n"
        )
        ok = False
    else:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(
                f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n"
            )
            sys.stderr.write(
                f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n"
            )
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    reg = CONFIG.get("registry")
    if reg is None:
        sys.stderr.write("模型列表  : 动态拉取已禁用（仅本地 product.json / 内置表）\n")
    else:
        st = reg.status()
        sys.stderr.write(
            f"模型列表  : 动态拉取（TTL {st['ttl_seconds']}s，"
            f"快照 {st['snapshot_file']}{'' if st['snapshot_exists'] else ' [暂无]'}）\n"
        )
    sched = CONFIG.get("checkin")
    if sched is None:
        sys.stderr.write(f"自动签到  : {CONFIG.get('checkin_reason') or '已禁用'}\n")
    else:
        sys.stderr.write(
            "自动签到  : 已启用，每日 "
            + "、".join(f"{h:02d}:00" for h in sched.hours)
            + f"（billing UA: {billing_ua()}）\n"
        )
    tasks = CONFIG.get("tasks")
    if tasks is None:
        sys.stderr.write(f"定时任务  : {CONFIG.get('tasks_reason') or '已禁用'}\n")
    else:
        for key in TASK_KEYS:
            st = tasks.task_status(key)
            label = TASK_LABELS[key]
            if not st["enabled"]:
                sys.stderr.write(f"定时任务  : {label} 已关闭\n")
                continue
            sys.stderr.write(
                f"定时任务  : {label} 每日 "
                + "、".join(f"{h:02d}:00" for h in st["hours"])
                + "\n"
            )
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="CodeBuddy -> OpenAI 兼容转换器（直连后端）"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument(
        "--api-key",
        default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
        help="可选：要求客户端携带的 API key（默认不校验）",
    )
    ap.add_argument(
        "--log",
        default=None,
        metavar="PATH",
        help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
        "不传则不记日志。",
    )
    ap.add_argument(
        "--desensitize",
        action="store_true",
        help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
        "插入零宽空格，缓解被后端内容审核误拦。默认关闭。",
    )
    ap.add_argument(
        "--no-compact",
        action="store_true",
        help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
        "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
        "但审核误拦风险略高于默认压缩模式。",
    )
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    ap.add_argument(
        "--models-ttl",
        type=int,
        default=int(os.environ.get("CODEBUDDY2OPENAI_MODELS_TTL") or 3600),
        metavar="SECONDS",
        help="动态模型列表的内存缓存时长，默认 3600（1 小时）。",
    )
    ap.add_argument(
        "--no-dynamic-models",
        action="store_true",
        help="关闭上游动态模型拉取，只用本机 product.json / 内置回退表。",
    )
    ap.add_argument(
        "--cache-dir",
        default=os.environ.get("CODEBUDDY2OPENAI_CACHE_DIR") or None,
        metavar="PATH",
        help="落盘目录（模型快照 + 签到留档）。"
        "默认 Windows: %%LOCALAPPDATA%%\\codebuddy2openai，其他平台: ~/.cache/codebuddy2openai。",
    )
    ap.add_argument(
        "--checkin-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_CHECKIN_HOURS"),
        metavar="H,H,...",
        help="每日自动签到的整点时刻，逗号分隔，默认 9,21"
        "（也可用环境变量 CODEBUDDY2OPENAI_CHECKIN_HOURS）。"
        "传空串等同于未配置（仍回落 9,21）。关闭请用 --no-checkin。",
    )
    ap.add_argument(
        "--travel-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_TRAVEL_HOURS"),
        metavar="H,H,...",
        help="猫猫旅行巡检的整点时刻，默认 9,21（一趟派出 + 一趟领奖）。"
        "关闭请用 --no-travel。",
    )
    ap.add_argument(
        "--activity-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_ACTIVITY_HOURS"),
        metavar="H,H,...",
        help="活跃上报的整点时刻，默认 10。关闭请用 --no-activity。",
    )
    ap.add_argument(
        "--keepalive-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_KEEPALIVE_HOURS"),
        metavar="H,H,...",
        help="token 保活（主动刷新）的整点时刻，默认 22。关闭请用 --no-keepalive。",
    )
    ap.add_argument(
        "--school-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_SCHOOL_HOURS"),
        metavar="H,H,...",
        help="开学季任务（点亮 + 领奖 + 抽空抽奖余额）的整点时刻，默认 12。"
        "活动下线时自动跳过。关闭请用 --no-school。",
    )
    ap.add_argument(
        "--cat-hours",
        default=os.environ.get("CODEBUDDY2OPENAI_CAT_HOURS"),
        metavar="H,H,...",
        help="夜猫子任务的整点时刻，默认 1（夜猫窗口 23:00–08:00 CST 内补一次）。"
        "关闭请用 --no-cat。",
    )
    ap.add_argument("--no-travel", action="store_true", help="关闭猫猫旅行任务。")
    ap.add_argument("--no-activity", action="store_true", help="关闭活跃上报任务。")
    ap.add_argument(
        "--no-keepalive", action="store_true", help="关闭 token 保活任务（不主动刷新 token）。"
    )
    ap.add_argument("--no-school", action="store_true", help="关闭开学季任务。")
    ap.add_argument("--no-cat", action="store_true", help="关闭夜猫子任务。")
    ap.add_argument(
        "--no-tasks",
        action="store_true",
        help="关闭全部六类定时任务（含签到），只提供 API 转换能力。",
    )
    ap.add_argument(
        "--activity-report-count",
        type=int,
        default=int(os.environ.get("CODEBUDDY2OPENAI_ACTIVITY_REPORT_COUNT") or 5),
        metavar="N",
        help="每次活跃上报的条数，默认 5（领猫前置需 5 次对话）。"
        "0 / 负数按 1 条处理（旧行为：只点亮连登）。",
    )
    ap.add_argument(
        "--no-checkin", action="store_true", help="关闭每日自动签到线程。"
    )
    ap.add_argument(
        "--checkin-on-start",
        action="store_true",
        help="进程启动时立刻签到一次，之后按 --checkin-hours 排程。",
    )
    ap.add_argument(
        "--checkin-timeout",
        "--task-timeout",
        type=int,
        default=60,
        dest="checkin_timeout",
        metavar="SECONDS",
        help="签到 / 活跃上报 / 旅行 / 保活 / 开学季 / 夜猫子的单次请求超时，默认 60。",
    )
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = (
        args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    )
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None

    # 动态模型注册表（上游拉取 + 内存缓存 + 落盘快照 + 多级回退）
    registry = ModelRegistry(
        base_url=BACKEND,
        cache_dir=args.cache_dir,
        ttl=args.models_ttl,
        enabled=not args.no_dynamic_models,
        log=_log,
    )
    CONFIG["registry"] = registry

    # 六类定时积分任务的统一排程（签到 / 活跃上报 / 猫猫旅行 / token 保活 / 开学季 / 夜猫子）。
    # 各类独立时点、独立开关；无凭据时不建调度器：任务必然失败，且凭据在进程内不会恢复
    # （与聊天路径同一条限制）。
    task_cfg = TaskConfig(
        checkin_hours=args.checkin_hours,
        travel_hours=args.travel_hours,
        activity_hours=args.activity_hours,
        keepalive_hours=args.keepalive_hours,
        school_hours=args.school_hours,
        cat_hours=args.cat_hours,
        checkin_enabled=not args.no_checkin,
        travel_enabled=not args.no_travel,
        activity_enabled=not args.no_activity,
        keepalive_enabled=not args.no_keepalive,
        school_enabled=not args.no_school,
        cat_enabled=not args.no_cat,
        activity_report_count=args.activity_report_count,
        timeout=args.checkin_timeout,
    )
    if args.no_tasks:
        tasks = None
        _tasks_reason = "定时任务未启用（--no-tasks）"
    elif CONFIG["cred"] is None:
        tasks = None
        _tasks_reason = "未找到登录凭据，定时任务未启用"
        sys.stderr.write(f"[警告] {_tasks_reason}。\n")
    else:
        tasks = TaskScheduler(
            CONFIG["cred"],
            config=task_cfg,
            cache_dir=registry.cache_dir,
            run_on_start=("checkin",) if args.checkin_on_start else (),
            log=_log,
        )
        _tasks_reason = ""
    CONFIG["tasks"] = tasks
    CONFIG["tasks_reason"] = _tasks_reason
    # 签到执行体单独留引用：/admin/checkin 沿用旧的结构与语义（排程由 tasks 负责）
    scheduler = tasks.checkin_runner if tasks is not None else None
    CONFIG["checkin"] = scheduler
    if not tasks or not tasks.enabled["checkin"]:
        CONFIG["checkin_reason"] = (
            _tasks_reason or "自动签到未启用（--no-checkin）"
        )
    else:
        CONFIG["checkin_reason"] = ""

    if not args.skip_check:
        preflight()

    sys.stderr.write(
        f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n"
    )
    sys.stderr.write("   GET  /v1/models              (动态拉取，?refresh=1 强制重拉)\n")
    sys.stderr.write(
        "   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n"
    )
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write(
        "   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n"
    )
    sys.stderr.write("   GET  /admin/models          (模型缓存状态)\n")
    sys.stderr.write("   GET  /admin/checkin         (签到状态 / POST 立即签到)\n")
    sys.stderr.write(
        "   GET  /admin/tasks           (六类定时任务状态 / POST ?task=all|travel 立即执行)\n"
    )
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if registry.enabled:
        sys.stderr.write(
            f"   缓存目录  : {registry.cache_dir}（模型快照 + 签到留档 + 任务留档）\n"
        )
    if tasks is not None:
        for key in TASK_KEYS:
            st = tasks.task_status(key)
            if not st["enabled"]:
                continue
            line = f"   {_pad_label(TASK_LABELS[key])}: " + "、".join(
                f"{h:02d}:00" for h in st["hours"]
            )
            if key == "checkin" and args.checkin_on_start:
                line += "（启动时先跑一次）"
            sys.stderr.write(line + "\n")
    elif _tasks_reason:
        sys.stderr.write(f"   定时任务  : {_tasks_reason}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log("==== converter 启动 ====")

    # 启动预热：把模型快照提前装进内存（失败不影响服务）
    if registry.enabled:
        threading.Thread(
            target=registry.warm,
            args=(_safe_cred_headers(),),
            name="models-warm",
            daemon=True,
        ).start()

    if tasks is not None:
        tasks.start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if tasks is not None:
            tasks.stop()


if __name__ == "__main__":
    main()
