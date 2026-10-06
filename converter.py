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
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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
import environments
import model_registry
from model_registry import (
    ModelFetchError,
    ModelRegistry,
    fallback_models,
)
from responses_adapter import (
    ResponsesStreamConverter,
    responses_request_to_chat,
)
from responses_projection import project_responses_chat_body
import model_capabilities
import reasoning
import state_snapshot
from task_scheduler import TASK_KEYS, TASK_LABELS, TaskConfig, TaskScheduler

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#
# 域与代理都交给 environments 解析（国内版 / 国际版只差域名）：
#   cn   chat → copilot.tencent.com   billing → www.codebuddy.cn
#   intl chat/billing → www.codebuddy.ai（收敛到同一个域）
# 环境取值优先级：--env / CODEBUDDY2OPENAI_ENV → 凭据 auth.domain 自动 → 兜底 cn。

BACKEND = environments.base_for("chat")
DEFAULT_DOMAIN = environments.domain_for()
USER_AGENT = "codebuddy2openai/2.0"

#: 刷新 token 失败后的冷却秒数（对齐 CLI 的 REFRESH_ATTEMPT_COOLDOWN_MS = 30000）。
REFRESH_COOLDOWN_S = 30

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------


def auth_dirs(env: str | None = None) -> list[Path]:
    """候选凭据目录，按优先级排列（先命中的先用）。

    国内版与国际版是**两个账号的两份凭据**，必须分开放置，否则同一个目录里
    两个 `*.info` 被 `sorted()` 取第一个纯属碰运气，token 与域不匹配就是全链路 401。
    三档顺序：

        1. 显式指定          CODEBUDDY_AUTH_DIR（老变量，最高优先级，单目录用法不变）
                             + CODEBUDDY2OPENAI_AUTH_CN / _INTL 指向目录或 .info 文件
        2. 分环境子目录约定   CODEBUDDY_AUTH_DIR/<env>/   （NAS 推荐）
        3. 平台默认目录       macOS/Windows/Linux 的 CodeBuddyExtension auth 目录

    env 为 None 时按 resolve_env 推断（环境变量 → 兜底 cn）。
    """
    e = environments.resolve_env(env)
    out: list[Path] = []

    # 1) 显式路径（目录或文件都行；文件由 find_auth_file 直接采信）
    for raw in (
        environments.resolve_auth_path(e),
        os.environ.get("CODEBUDDY_AUTH_DIR"),
    ):
        if raw:
            p = Path(raw)
            if p not in out:
                out.append(p)
        # 分环境子目录约定：<根>/cn、<根>/intl
        if raw:
            sub = Path(environments.auth_subdir(raw, e))
            if sub not in out:
                out.append(sub)

    # 2) 平台默认目录
    for d in _platform_auth_dirs():
        if d not in out:
            out.append(d)
    return out


def _platform_auth_dirs() -> list[Path]:
    """平台默认的 CodeBuddy / WorkBuddy 凭据目录（不含任何显式配置）。"""
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


def find_auth_file(env: str | None = None) -> Path | None:
    """定位该环境的凭据文件。

    命中规则（**先环境专属，再通用兜底**，避免两个环境互相抢文件）：
      1. 候选路径本身就是 `.info` 文件 → 直接返回；
      2. 候选目录里存在 `auth.domain` 归属当前环境的 `.info` → 返回它；
      3. 退回候选目录里第一个 `*.info`（兼容单环境的老部署）。

    第 2 条是分开放置的关键：即使两个环境的凭据被放进同一个目录，
    也能各自认领到属于自己的那一份，而不是按文件名字典序先到先得。
    """
    e = environments.resolve_env(env)
    first_any: Path | None = None
    for d in auth_dirs(e):
        if d.is_file() and d.suffix == ".info":
            return d
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.info")):
            if _info_env(f) == e:
                return f
            if first_any is None:
                first_any = f
        if first_any is not None:
            # 当前目录已有可用凭据（虽未认出域），不再跨目录找
            return first_any
    return first_any


def find_all_auth_files(env: str | None = None) -> list[Path]:
    """列出候选目录里所有 `*.info`（去重、保持目录优先级顺序）。

    仅供 `--env auto` 的域推断与「同目录多凭据」告警使用，不参与常规取凭据。
    """
    seen: list[Path] = []
    for d in auth_dirs(env):
        if d.is_file() and d.suffix == ".info":
            if d not in seen:
                seen.append(d)
            continue
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.info")):
            if f not in seen:
                seen.append(f)
    return seen


def _info_env(path: Path) -> str | None:
    """读 `.info` 里的 `auth.domain` 反推环境；读不动返回 None（不抛异常）。"""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return environments.env_of_domain((raw.get("auth") or {}).get("domain"))


def read_auth_domain(path: Path | None) -> str | None:
    """只读地取一份凭据的 `auth.domain`（不触发刷新）。"""
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return ((json.load(f).get("auth") or {}).get("domain")) or None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。

    env / proxy 缺省为 None，此时按 environments 的默认链解析（环境变量 → 兜底 cn）。
    刷新 token 打的是 chat 后端（国内 copilot.tencent.com / 国际 codebuddy.ai），
    所以国际版部署必须让刷新请求也走代理 —— 这两个字段就是为此存在的。
    """

    def __init__(self, path: Path | str, *, env: str | None = None, proxy: str | None = None):
        # 归一成 Path —— 这里**必须**做，因为 `_load_if_stale()` 会立刻调 `.stat()`，
        # 传 str 会在第一次取 header 时炸 `AttributeError: 'str' object has no attribute 'stat'`，
        # 而报错点离调用处很远（看起来像是文件读取问题，实际是类型问题），排查成本很高。
        self.path = Path(path)
        self.env = env
        self.proxy = proxy
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0
        # 刷新失败退避（对齐 CLI 契约 REFRESH_ATTEMPT_COOLDOWN_MS = 30s）：
        # 刷新连续失败时（代理抖动 / refreshToken 被吊销），不让每个请求都去打
        # 刷新端点 —— 冷却期内直接用现有 token 发请求（可能 401，但比打挂上游强）。
        self._refresh_fail_at: float = 0.0

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
            with environments.make_client(
                self.env, proxy=self.proxy, timeout=15
            ) as c:
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

    def get_headers(
        self,
        within_ms: int = 60_000,
        *,
        env: str | None = None,
        proxy: str | None = None,
    ) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。

        within_ms：提前多少毫秒判定「即将过期」并主动刷新。默认 60s（聊天路径，
        宁可请求时再刷）；签到等定时任务传 2h 窗口，避免到点时 token 已过期。

        env / proxy：单次调用覆盖构造时的取值（任务模块按自己的环境传进来）。
        传 None 表示沿用实例上已配置的。
        """
        use_env = env if env is not None else self.env
        use_proxy = proxy if proxy is not None else self.proxy
        with self._lock:
            if self._is_expired(within_ms):
                if time.time() - self._refresh_fail_at < REFRESH_COOLDOWN_S:
                    # 刷新刚失败过：冷却期内不重试，用现有 token 硬闯
                    # （上游可能仍认这个 token；真过期了这次请求 401，等冷却后自动再试）
                    s = self._session()
                    return self._build_headers_from(
                        s.get("auth") or {}, s.get("account") or {}
                    )
                prev_env, prev_proxy = self.env, self.proxy
                self.env, self.proxy = use_env, use_proxy
                try:
                    self._refresh()
                    self._refresh_fail_at = 0.0
                except Exception:
                    self._refresh_fail_at = time.time()
                    raise
                finally:
                    self.env, self.proxy = prev_env, prev_proxy
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
#   1. 上游动态拉取   GET {BACKEND}/v3/config（CLI 白名单裁剪，需 CLI UA）
#   2. 本地快照       models-snapshot.json（上游失败时的上一份真实列表，标记 stale-*）
#   3. 本机 product.json（WorkBuddy 安装目录，见 _load_models_from_workbuddy）
#   4. 内置回退表     fallback_models(env)（仅当以上全不可用）
# ---------------------------------------------------------------------------

#: 最终兜底表 —— **按当前环境取**（国内版 31 个 / 国际版 21 个，两者交集很小）。
#: 绝不能用一张表覆盖两个环境：那会在任一侧都给出调不通的模型 id。
DEFAULT_MODELS = fallback_models()

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
        return cred.get_headers(
            within_ms=within_ms, env=CONFIG.get("env"), proxy=CONFIG.get("proxy")
        )
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

    注意：registry 路径的「登记」不在本函数做 —— registry 的 on_resolve 回调
    （装配于 main）覆盖**所有**产出路径（warm 后台线程 / cache / 回退 / 探针），
    在这里再调一次会重复写能力表。
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
        out = [{"id": m, "object": "model", "owned_by": "codebuddy"} for m in local]
        _remember_models(out, "local-product")
        return out, "local-product"
    ids = fallback_models(CONFIG.get("env"))
    out = [{"id": m, "object": "model", "owned_by": "codebuddy"} for m in ids]
    _remember_models(out, "static")
    return out, "static"


# ---------------------------------------------------------------------------
# 状态快照（logs/state.json）
# ---------------------------------------------------------------------------
#
# 为什么要有：/health 已经把「可用模型 / 积分余额 / 任务状态」聚合好了，但它是
# 只读接口 —— 在群晖上想看一觉得开 SSH 敲 curl；而 converter.log 是流水账，
# 想回答「现在有哪些模型、还剩多少积分」得往上翻。这里把同样的聚合结果落到
# 日志目录，File Station 点开就能看，也方便外部监控脚本直接读。
#
# 触发时机（事件驱动为主，周期刷新兜底）：
#   1. 启动时写一次        —— 保证文件立刻存在
#   2. 模型清单刷新后      —— resolve_models 每次成功都请求一次
#   3. 每类定时任务跑完后  —— 由 TaskScheduler 的 on_result 回调触发
#   4. 周期兜底            —— 默认 1h（--state-interval 可调，0 关闭）


def _remember_models(items: list[dict], source: str) -> None:
    """记住本次解析出的模型清单，并请求刷新状态快照。

    为什么要在内存里记一份：快照如果自己去调 resolve_models，就会和请求路径
    互相触发（快照→拉模型→请求快照→…）。改成「谁拉了模型谁登记」，快照只读内存。
    """
    CONFIG["models_snapshot"] = {
        "ids": [str(i.get("id")) for i in items if isinstance(i, dict)],
        "source": source,
        "at": time.time(),
        "error": None,
    }
    _write_capabilities(items, source)
    _request_snapshot()


def _write_capabilities(items: list[dict], source: str) -> None:
    """把模型能力表（上下文窗口 + 思考强度）落盘到 logs/model_capabilities.json。

    为什么放这里而不是快照里：快照是「运行状态」（积分、任务、缓存新鲜度），
    会高频重写；能力表是「准静态数据」，只有模型集合变化才值得重写。
    分开两个文件，监控脚本可以放心地高频读快照，而能力表按需看。

    去重：按 (来源, 模型id序列) 做签名，相同就跳过 —— 否则每次 /v1/models
    都写一遍盘（缓存命中路径每次请求都会走到这里）。
    """
    target = CONFIG.get("capabilities_path")
    if not target:
        return
    sig = (source, tuple(str(i.get("id")) for i in items if isinstance(i, dict)))
    if sig == CONFIG.get("capabilities_sig"):
        return
    CONFIG["capabilities_sig"] = sig
    try:
        table = model_capabilities.build_from_registry(
            items, env=CONFIG.get("env") or "", source=source
        )
        path = model_capabilities.write_table(table, target)
        _log(f"[capabilities] 模型能力表已更新 → {path}")
    except Exception as e:  # noqa: BLE001
        # 能力表是附属产物，写失败不影响服务
        _log(f"[capabilities] WARN 写能力表失败：{e}")


def _request_snapshot() -> None:
    """请求写一次状态快照。非阻塞、可合并，任意线程可调。"""
    w = CONFIG.get("snapshot")
    if w is not None:
        w.request()


def _collect_state() -> dict:
    """汇总当前状态，交给 `state_snapshot.build_snapshot` 落盘。

    只读内存里的状态，**不发上游请求** —— 联网拉新积分是 writer 的
    `before_build` 钩子负责的，这样「采集」和「刷新」两件事互不纠缠。
    """
    cred = CONFIG.get("cred")
    cred_summary = None
    if cred is not None:
        try:
            cred_summary = cred.summary()
        except Exception as e:  # noqa: BLE001
            cred_summary = {"error": str(e)}
    checkin = CONFIG.get("checkin")
    tasks = CONFIG.get("tasks")
    return state_snapshot.build_snapshot(
        env=CONFIG.get("env"),
        app={
            "requests_total": CONFIG.get("requests_total", 0),
            "auth_file": CONFIG.get("auth_file"),
            "mode": "direct-proxy (native function calling)",
        },
        models=CONFIG.get("models_snapshot"),
        credential=cred_summary,
        checkin=checkin.status() if checkin is not None else None,
        tasks=tasks.status() if tasks is not None else None,
    )


def _refresh_points() -> None:
    """写快照前把积分拉新（会联网）。

    只走 checkin 执行体的 refresh_points（纯查询），**不会触发签到** ——
    观测动作不能变成写操作。
    """
    checkin = CONFIG.get("checkin")
    refresh = getattr(checkin, "refresh_points", None)
    if callable(refresh):
        refresh()


# 国际版要求首条消息是 system prompt（否则 400 + code 11128），
# 客户端没给 system 时用这条兜底。与 anthropic_adapter 的同名常量保持一致。
FALLBACK_SYSTEM_PROMPT = "You are a helpful assistant."


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
    # 思考强度：上游 Chat 传输只认**扁平** `reasoning_effort` + `verbosity`
    # （见 reasoning.py 模块 docstring）。这两个直接透传即可。
    # 嵌套 `reasoning` / `text` / Anthropic 的 `thinking` 不在此处透传 ——
    # 它们形状不对，由 _normalize_reasoning 归一成扁平字段后再发。
    "reasoning_effort",
    "verbosity",
    "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")


def _cors_kwargs(origins: list[str]) -> dict:
    """CORSMiddleware 的统一参数。鉴权走 Bearer/api-key 头、不用 cookie，
    所以 allow_credentials 必须 False（True 与 "*" 组合会被浏览器直接拒绝）。"""
    return {
        "allow_origins": origins,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "allow_credentials": False,
    }


# ---------------------------------------------------------------------------
# CORS（浏览器直连支持）
# ---------------------------------------------------------------------------
# 浏览器网页直连本服务时，跨域请求前会先发 OPTIONS 预检；没有这段时预检
# 落不到任何路由 → 405，浏览器随即把真正的 POST 拦下，表现为「请求根本
# 没发出去」（2026-10-07 cn 实例实测）。服务端程序（New API / Codex）不发
# 预检，所以此前只在浏览器场景现形。
#
# 注册时机：必须在下方 `@app.middleware("http")` 访问日志**之前**。本 starlette
# 版本的 add_middleware 是 insert(0)（后注册者更外层），所以访问日志在外、
# CORS 在内、紧贴路由——预检 OPTIONS 由 CORS 直接短路（不再 405），日志里
# 只会以 2xx 出现（默认静默，log_requests 开启时可见）。
#
# 默认全放行（`*`）：鉴权靠 KEY 而非 cookie，CORS 只约束浏览器、不影响
# curl / SDK。需要收窄或禁用时用 --cors-origins / CODEBUDDY2OPENAI_CORS_ORIGINS
# （见 _apply_cors_config）。
app.add_middleware(CORSMiddleware, **_cors_kwargs(["*"]))


def _apply_cors_config(spec: str) -> None:
    """按 --cors-origins / CODEBUDDY2OPENAI_CORS_ORIGINS 重设 CORS 中间件。

    spec：`*`（全放行）| `off`（禁用，恢复无 CORS 行为）| 逗号分隔来源列表。
    直接改 user_middleware 而不是 add_middleware：后者在应用启动过一次后
    （TestClient / lifespan）会抛 "Cannot add middleware after an application
    has started"；直接构造 Middleware 无此限制，且语义完全等价。
    只能在服务启动前调用（uvicorn.run 之前）；重挂后固定放列表末尾
    （最内层、紧贴路由），与默认注册的顺序一致。
    """
    from starlette.middleware import Middleware

    spec = (spec or "").strip()
    app.user_middleware = [
        m for m in app.user_middleware if m.cls is not CORSMiddleware
    ]
    if spec in ("", "off"):
        return
    origins = (
        ["*"]
        if spec == "*"
        else [o.strip() for o in spec.split(",") if o.strip()]
    )
    app.user_middleware.append(Middleware(CORSMiddleware, **_cors_kwargs(origins)))
CONFIG: dict = {
    "api_key": "",
    "cred": None,
    "log_path": None,
    "desensitize": False,
    "no_compact": False,
    "registry": None,
    "checkin": None,
    "tasks": None,
    #: 生效环境（cn / intl）与代理 URL。凭据拿不到 domain 时按这里的值兜底，
    #: 由 main() 在启动时解析（--env / --proxy-cn / --proxy-intl）。
    "env": None,
    "proxy": None,
    #: 最近一次成功解析出的模型清单（供状态快照读取，避免快照为了写盘再打一次上游）。
    #: 结构：{"ids": [...], "source": "upstream", "at": <ts>, "error": None}
    "models_snapshot": None,
    #: 状态快照写线程（state_snapshot.SnapshotWriter）。None = 未启用（没配日志路径）。
    "snapshot": None,
    #: 请求处理计数（快照里顺带记录，方便判断服务是否在被使用）
    "requests_total": 0,
    #: 请求级日志开关（默认关）。默认只记错误/异常（含 401、上游非 200、网络错误），
    #: 打开后每个请求额外记一行「调用 + 返回状态」。
    "log_requests": False,
    #: 完整报文开关（默认关）。打开后把发往后端的 body / 上游原始 SSE 全文落盘。
    #: 排障用，量很大；开着会把请求级日志重新淹掉。
    "log_bodies": False,
    #: 网络错误自动重试次数（默认 5，0 = 关闭）。**只对「还没向客户端发出任何字节」
    #: 的失败生效** —— 建连失败 / SOCKS 隧道抖动 / 上游在出首字节前重置。
    #: 已开始转发后再断流不能重试（客户端已收到部分数据，重发会内容重复）；
    #: 上游明确回非 200 也不重试（确定性拒绝，立刻重试结果一样）。
    "stream_retry": 5,
    #: 重试间隔基数（秒），指数退避：2/4/8/16/30（封顶 30s）。0 = 不等待立即重试。
    "stream_retry_gap": 2.0,
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


def _log_req(msg: str):
    """请求级日志（每次调用的摘要行）。只在 `--log-requests` 打开时记录。

    为什么单独收口：默认日志只留错误与异常（量小、排障够用），
    每请求一行的「调了哪个模型 / 返回什么状态」是可选观测，开关控制。
    """
    if CONFIG.get("log_requests"):
        _log(msg)


def _log_body(msg: str):
    """完整报文日志（body / 原始 SSE 全文）。只在 `--log-bodies` 打开时记录。

    这类日志动辄几十 KB 一条，是「日志太多」的主要来源 —— 与请求级摘要分开控制，
    开 --log-requests 排查调用状态时不必连带吃下报文。
    """
    if CONFIG.get("log_bodies"):
        _log(msg)


def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _upstream_timeout(read) -> httpx.Timeout:
    """上游 httpx 客户端超时：connect/pool 快速失败，read 按路径单独给。

    为什么：2026-09-26 排查「新会话第一遍没反应、要问两遍」发现，流式路径
    `timeout=None` 意味着**连接阶段也没有超时** —— TLS 握手经代理挂死时，
    服务端无限傻等，而客户端有自己的首字节超时，等不到任何字节就放弃重发，
    表现就是「第一遍只是联通了一下」。connect 限时快速失败后交给重试循环；
    read 不能限（SSE 长流，思考模型出首字节前后间隔可达数分钟）。
    """
    return httpx.Timeout(connect=15.0, read=read, write=30.0, pool=15.0)


async def _retry_wait(attempt: int) -> None:
    """第 attempt 次（从 1 计）网络失败后的退避等待。

    指数退避：2/4/8/16/30…封顶 30s。2026-09-26 线上实测代理链路是「抖一阵就好」，
    立即重试经常撞在同一个抖动窗口里，加间隔后才真正吃得下抖动恢复期。
    """
    gap = max(0.0, float(CONFIG.get("stream_retry_gap", 2)))
    if gap > 0:
        await asyncio.sleep(min(gap * (2 ** (attempt - 1)), 30.0))


def _pad_label(s: str, width: int = 16) -> str:
    """按显示宽度（CJK 算 2 列）左侧补齐，让启动横幅里的中文标签能对齐。"""
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(1, width - w)


# ---------------------------------------------------------------------------
# 访问日志（谁连的 / 连了什么 / 结果如何）
# ---------------------------------------------------------------------------
#
# 为什么要在中间件这一层单独做：端点的 `▶ REQUEST` 只覆盖「解析成功且鉴权通过」
# 之后的聊天请求，而排障时最想知道的几件事恰恰都落在日志外 ——
#   - 谁连的：容器里只有 remote_addr 能区分是哪个客户端 / 哪台机器；
#   - 拉模型列表：客户端报「获取模型失败」时，第一步就看它有没有真的打进来；
#   - 401 被拒：鉴权在进端点前就返回了，端点里根本记不到；
#   - 上游把请求拒了（非 200）：以前只在流式路径记，非流式/异常路径会漏。
# 所以统一在 ASGI 层记一行，保证**每个请求都留下痕迹**。

#: 不记访问日志的路径。`/health` 会被容器的 healthcheck 每 30s 打一次，
#: 记进去会把有效信息淹掉（实测：日志被 healthcheck 刷到几万行）。
ACCESS_LOG_SKIP = {"/health"}


def _client_ip(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 首个非 unknown 项，否则用 remote_addr。

    中转面板（New API）转发过来时 remote_addr 是面板的地址，
    真实来源在 X-Forwarded-For 里 —— 不取它就看不出「到底是谁在用」。
    """
    xff = request.headers.get("x-forwarded-for") or ""
    for part in xff.split(","):
        p = part.strip()
        if p and p.lower() != "unknown":
            return p
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "-"


def _mark_model(request: Request, model: str | None) -> None:
    """把本次请求的模型名挂到 request.state，供访问日志中间件读取。

    为什么不直接在中间件里解析 body：那得先把 body 读完再塞回去，
    对 SSE 流式响应是实打实的风险。端点本来就已经解析出 model 了，顺手记一下最省事。
    """
    try:
        request.state.model = model
    except Exception:  # noqa: BLE001 — 记不上模型名不该影响请求
        pass


@app.middleware("http")
async def access_log(request: Request, call_next):
    """每个 HTTP 请求记一行：客户端 → 路径 → 状态码 → 耗时 → 模型 → UA。

    记录失败静默吞掉：观测代码不能影响主流程。
    """
    path = request.url.path
    skip = path in ACCESS_LOG_SKIP
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception as e:  # noqa: BLE001 — 记完再抛给上层
        if not skip:
            _log(
                f"[{_client_ip(request)}] {request.method} {path} ✗ 异常 "
                f"| {int((time.time() - t0) * 1000)}ms | {type(e).__name__}: {_truncate(str(e), 120)}"
            )
        raise
    try:
        CONFIG["requests_total"] = int(CONFIG.get("requests_total") or 0) + 1
    except Exception:  # noqa: BLE001
        pass
    if not skip:
        ok = response.status_code < 400
        # 默认只记异常请求（4xx/5xx —— 401、模型列表失败这些排障第一现场）；
        # 成功请求的逐行记录由 --log-requests 控制。
        if not ok or CONFIG.get("log_requests"):
            model = getattr(request.state, "model", None)
            ua = _truncate(request.headers.get("user-agent") or "-", 40)
            _log(
                f"[{_client_ip(request)}] {request.method} {path} → {response.status_code}"
                f" | {int((time.time() - t0) * 1000)}ms"
                + (f" | model={model}" if model else "")
                + f" | ua={ua}"
            )
    return response


def _check_auth(
    authorization: str | None,
    x_api_key: str | None,
    api_key: str | None = None,
):
    """校验客户端鉴权。

    为什么同时认三种头：不同客户端拉模型/发请求时用的头名不一样，
    少了任何一个都会表现成「莫名其妙 401 / 获取模型列表失败」——

      - `Authorization: Bearer <key>` —— OpenAI SDK、绝大多数客户端；
      - `X-Api-Key` —— 部分客户端（如 New API 拉渠道模型时）；
      - `api-key`   —— New API / One API 的默认头名（**不带前缀**，就是裸值）。

    实测：只认前两种时，New API 走 `api-key` 头会吃 401，
    而它在 UI 上只会显示「获取模型列表失败」，很难定位到是头名不匹配。
    """
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    # 裸值兜底：有些客户端会把 `Bearer xxx` 整个塞进 api-key 头里。
    for cand in (api_key, x_api_key):
        if not token and cand:
            c = cand.strip()
            token = c[7:].strip() if c.startswith("Bearer ") else c
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
        "service_version": model_registry.SERVICE_VERSION,
        "ua_version": model_registry.effective_cli_version(),
        "ua_version_file": model_registry._ua_file_path,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "auth_file": str(find_auth_file(CONFIG.get("env")) or "(未找到)"),
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
    # 状态快照（logs/state.json）—— 把落盘路径和写入统计回显出来，
    # 免得用户翻文档才知道文件在哪、有没有在写。
    snap = CONFIG.get("snapshot")
    if snap is not None:
        info["snapshot"] = snap.status()
    info["requests_total"] = CONFIG.get("requests_total", 0)
    return info


@app.get("/models")
@app.get("/v1/models")
def list_models(
    refresh: int = 0,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """OpenAI 兼容模型列表。

    默认走缓存（TTL 1h，见 model_registry）；`?refresh=1` 强制重拉上游。

    同时挂 `/models` 与 `/v1/models`：中转面板（New API / One API）添渠道时
    会拿「基础地址」拼路径探测，若基础地址填的是裸 `http://host:port`
    它可能直接探 `/models`；只挂 `/v1/models` 时这里会 404，
    UI 表现就是「获取模型列表失败」，看不出是路径问题。
    """
    _check_auth(authorization, x_api_key, api_key)
    data, source = resolve_models(force=bool(refresh))
    for item in data:
        item.setdefault("created", 1700000000)
    resp = {"object": "list", "data": data}
    # 记一行：客户端报「获取模型列表失败」时，先看这里到底返回了几个、走的哪条来源。
    _log(f"[models] 返回 {len(data)} 个（来源 {source}{'，强制刷新' if refresh else ''}）")
    if source in ("stale-memory", "stale-snapshot") or source.startswith("stale-") or source in ("static", "local-product"):
        # 非新鲜来源时给出标记，方便客户端判断列表是否可能过期。
        # 注意 "probe" 不算过期 —— 它是**当前真实可用**的集合，只是获取方式不同。
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """模型注册表状态；`?refresh=1` 强制重拉一次。"""
    _check_auth(authorization, x_api_key, api_key)
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """签到调度状态 + 最近结果。

    签到的**排程**现在由六类任务调度器统一负责（保持与旅行/活跃同槽并行、共用补跑窗口），
    这里返回的 `alive` 因此反映的是任务调度线程；`scheduled_by` 标明这一点，
    免得看到 `alive=false` 误以为签到没在跑。
    """
    _check_auth(authorization, x_api_key, api_key)
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """立即执行一次签到 + 余额查询（不影响既定排程）。"""
    _check_auth(authorization, x_api_key, api_key)
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """六类定时任务的排程状态 + 最近结果。

    六类 = 签到 / 活跃上报 / 猫猫旅行 / token 保活 / 开学季 / 夜猫子，
    各自独立时点、独立开关，互不影响。
    """
    _check_auth(authorization, x_api_key, api_key)
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """立即执行任务（不影响既定排程）。

    `?task=all` 跑全部已启用任务；`?task=travel` 等跑单类。
    兼容写法：`?key=travel`（与 `?task=` 等价，二者取其一即可）。
    """
    _check_auth(authorization, x_api_key, api_key)
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


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    api_key: str | None = Header(default=None, alias="api-key"),
):
    _check_auth(authorization, x_api_key, api_key)
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

    # 思考强度归一：把客户端各种写法（扁平/嵌套/Anthropic thinking）
    # 统一成上游认的扁平 reasoning_effort + verbosity。
    _normalize_reasoning(body, payload)

    # 腾讯后端不支持 developer role，遇到会触发安全策略拦截（11128），统一映射为 system
    if "messages" in body and isinstance(body["messages"], list):
        body["messages"] = [
            dict(m, role="system")
            if isinstance(m, dict) and m.get("role") == "developer"
            else m
            for m in body["messages"]
        ]

    # 国际版（www.codebuddy.ai）硬性要求**首条**消息必须是 system prompt，
    # 否则直接 400 + code 11128「first message is not system prompt」
    # —— 报错文案会伪装成「请求被安全策略拦截」，极易误判成敏感词问题，
    #    实测纯 `[{"role":"user",...}]` 必然触发。
    #
    # 为什么在这里补：/v1/responses 与 /v1/messages 两条路径已分别在
    # responses_projection / anthropic_adapter 里兜底，只有直连的
    # /v1/chat/completions 没覆盖 —— OpenAI SDK、各种桌面客户端走的正是这条。
    # developer → system 的映射解决不了它（客户端压根不带 system 时无消息可映射）。
    #
    # 国内版没有这条约束，但补上无害，所以不做环境判断，统一补。
    _ensure_leading_system(body)

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
    _mark_model(request, model_name)
    tool_names = [
        t.get("function", {}).get("name")
        for t in (payload.get("tools") or [])
        if isinstance(t, dict)
    ]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log_req(
        f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
        + (f" | tools={tool_names}" if tool_names else "")
        + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else "")
    )
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log_body(
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

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应。
    # 响应聚合完才返回给客户端 —— 任何时刻的网络错误都可以安全重试。
    collected = None
    retries = max(0, int(CONFIG.get("stream_retry", 1)))
    for attempt in range(1, retries + 2):
        try:
            async with environments.make_async_client(CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=_upstream_timeout(300)) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        _log(
                            f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8', 'replace'), 200)}"
                        )
                        _log_body(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8', 'replace')}")
                        raise HTTPException(
                            status_code=r.status_code,
                            detail=_safe_err_raw(raw, r.status_code),
                        )
                    collected = await _collect_stream(r)
            break
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            if attempt > retries:
                _log(
                    f"[{rid}] ✗ 网络错误 | {model_name} | {type(e).__name__}: {e}；重试后仍失败"
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {"message": f"upstream error: {e}", "type": "upstream_error"}
                    },
                ) from None
            _log(
                f"[{rid}] ↻ 网络错误 | {model_name} | {type(e).__name__}: {e}"
                f" | 自动重试 {attempt}/{retries}"
            )
            await _retry_wait(attempt)
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _ensure_leading_system(body: dict) -> bool:
    """确保 `body["messages"]` 首条是 system prompt。已满足返回 False，补了返回 True。

    为什么需要：国际版（`www.codebuddy.ai`）硬性要求首条消息是 system prompt，
    否则 `400 + code 11128 "first message is not system prompt"`。
    这个报错**文案是误导的** —— `displayMsg` 会说「请求被安全策略拦截」，
    看起来像敏感词问题，实际只是缺一条 system。

    与 `responses_projection._ensure_leading_system`、
    `anthropic_adapter._ensure_leading_system` 是同一份契约，
    三处各有一份是因为三条协议入口各自独立（刻意不互相 import，避免耦合）。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "system":
        return False
    body["messages"] = [{"role": "system", "content": FALLBACK_SYSTEM_PROMPT}] + messages
    return True


def _normalize_reasoning(body: dict, payload: dict) -> bool:
    """把客户端各种「思考强度」写法归一成上游认的**扁平**字段。

    为什么需要：上游 Chat 传输只认顶层 `reasoning_effort` + `verbosity`
    （报告 §9③A，本机实测印证）。但不同客户端用不同写法：

      OpenAI 风格 : 扁平 `reasoning_effort: "high"`
      Codex 风格  : 嵌套 `reasoning: {effort: "high", summary: "auto"}`
      Claude Code : `thinking: {type: "enabled", budget_tokens: 8000}`
      其它        : `thinking: "high"` / `reasoning_effort: "auto"` …

    这些写法里只有第一种能直接透传，其余形状不对，上游要么忽略要么报
    400 + code 11133。所以统一收口到这里归一，再产出扁平字段。

    返回：是否实际设置了思考字段（供日志用）。
    """
    nested = payload.get("reasoning")
    thinking = payload.get("thinking")
    flat = payload.get("reasoning_effort")

    # 优先级：嵌套/Anthropic 形态只在没有扁平值时参与（扁平最直白，尊重它）
    if nested is not None:
        spec = reasoning.from_responses_reasoning(nested, flat)
    elif thinking is not None:
        spec = reasoning.from_anthropic_thinking(thinking, flat)
    else:
        spec = reasoning.from_responses_reasoning(None, flat)

    spec = reasoning.resolve(spec)
    fields = reasoning.to_openai_fields(spec)

    # 清掉客户端原始形态，避免把形状不对的字段一起发上去
    for k in ("reasoning", "text", "thinking"):
        body.pop(k, None)

    if not fields:
        # 关闭语义（off/none/disabled）→ 明确删掉，不要留残留的默认档
        body.pop("reasoning_effort", None)
        body.pop("verbosity", None)
        return False

    body.update(fields)
    return True


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
    # 成功完成的逐行记录随 --log-requests 开关；内容审核拦截是异常信号，始终记。
    if tag or CONFIG.get("log_requests"):
        _log(
            f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
            + (f" | tool_calls={tc_names}" if tc_names else "")
            + f" | tokens={usage.get('total_tokens', '?')}"
        )
    # 完整响应体
    _log_body(
        f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}"
    )


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。

    ⚠️ 必须同时收集 `reasoning_content`（思维链）—— 否则推理型模型会出现「截断且
    内容全空」：上游把 token 预算先花在 reasoning 上（`finish_reason=length`、
    `completion_tokens_details.reasoning_tokens > 0`），真正的 `content` 一个都没轮到。
    实测（国际版 hy4-preview）：只收 content → `content=null / finish=length`；
    把 reasoning_content 兜底接上后才有可见输出。这与流式路径的对外形态保持一致
    （流式转发是原样透传 delta，reasoning_content 本来就在里面）。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
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
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
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

    message: dict = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    # 推理型模型：content 为空但有思维链时，把思维链兜底成可见内容。
    # 不这么做客户端拿到的是「空回复」，看起来像服务坏了。
    reasoning = "".join(reasoning_parts)
    if not message["content"] and reasoning:
        message["content"] = reasoning
    if reasoning:
        # 保留原始字段，方便客户端区分「正文」与「思维链」
        message["reasoning_content"] = reasoning
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
            # 内容审核拦截常以 content-filter 或特殊中文文案返回。
            # 只扫**正文 / 错误字段**，不扫整行 —— tool_calls 的参数里经常带
            # 「敏感 / 审核」这些词（Bash 命令、代码内容、grep 目标……），
            # 整行扫会把正常完成的工具调用误标成「内容审核拦截」（2026-09-26 实测）。
            filtered = False
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or ch.get("message") or {}
                for key in ("content", "reasoning_content"):
                    val = delta.get(key)
                    if isinstance(val, str) and _looks_like_content_filter_text(val):
                        filtered = True
            err = obj.get("error")
            if isinstance(err, dict) and _looks_like_content_filter_text(
                str(err.get("message") or "")
            ):
                filtered = True
            if isinstance(obj.get("msg"), str) and _looks_like_content_filter_text(
                obj["msg"]
            ):
                filtered = True
            if filtered:
                saw_filter = True

    # —— 网络错误自动重试 ——
    # 规则（2026-09-26 定）：只有在**还没向客户端转发过任何字节**时失败才重试
    # （建连失败 / SOCKS 隧道抖动 / 上游在出首字节前重置——正是线上见到的那种
    # 「✗ 网络错误」。已开始转发后再断流，客户端已收到部分数据，重试会造成
    # 内容重复，只能报错。上游明确回非 200 也不重试（确定性拒绝）。
    terminal = False  # True = 非 200 的错误事件已发给客户端，直接结束

    async def _attempt():
        nonlocal terminal
        async with environments.make_async_client(
            CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=_upstream_timeout(None)
        ) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(
                        f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8', 'replace'), 200)}"
                    )
                    _log_body(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8', 'replace')}")
                    terminal = True
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk

    retries = max(0, int(CONFIG.get("stream_retry", 1)))
    for attempt in range(1, retries + 2):
        sent_any = False
        try:
            async for chunk in _attempt():
                sent_any = True
                yield chunk
            break  # 正常走完
        except httpx.HTTPError as e:
            if sent_any or attempt > retries:
                _log(
                    f"{prefix}✗ 网络错误 | {model_name} | {type(e).__name__}: {e}"
                    + ("；已转发部分数据，无法安全重试" if sent_any else "；重试后仍失败")
                )
                yield _err_event(str(e).encode(), 502)
                return
            _log(
                f"{prefix}↻ 网络错误（首字节前）| {model_name} | {type(e).__name__}: {e}"
                f" | 自动重试 {attempt}/{retries}"
            )
            await _retry_wait(attempt)
    if terminal:
        return

    # 流结束：输出完成日志（成功行的记录随 --log-requests；内容审核拦截始终记）
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    if tag or CONFIG.get("log_requests"):
        _log(
            f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
            + (f" | tool_calls={tool_names}" if tool_names else "")
            + f" | tokens={usage.get('total_tokens', '?')}"
        )
    # 完整原始 SSE（后端返回的全部内容）
    _log_body(
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


async def _post_backend_once(
    url: str, headers: dict, body: dict, *, rid: str = "", model_name: str = "?"
) -> tuple[int, bytes]:
    """单次请求后端并**缓冲完整响应**。

    网络层失败自动重试（次数 = CONFIG["stream_retry"]）：本函数把响应整个缓冲完
    才返回，调用方还没向客户端发出任何东西，重发是安全的。非 200 不重试
    （那是确定性拒绝，交由调用方决定）。
    """
    prefix = f"[{rid}] " if rid else ""
    retries = max(0, int(CONFIG.get("stream_retry", 1)))
    for attempt in range(1, retries + 2):
        try:
            async with environments.make_async_client(CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=_upstream_timeout(120)) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    chunks: list[bytes] = []
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            chunks.append(chunk)
                    return r.status_code, b"".join(chunks)
        except httpx.HTTPError as e:
            if attempt > retries:
                raise
            _log(
                f"{prefix}↻ 网络错误 | {model_name} | {type(e).__name__}: {e}"
                f" | 自动重试 {attempt}/{retries}"
            )
            await _retry_wait(attempt)
    raise RuntimeError("unreachable")  # pragma: no cover


async def _post_backend_with_filter_retry(
    url: str, headers: dict, body: dict, rid: str = "", model_name: str = "?"
) -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body, rid=rid, model_name=model_name)
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
        _log_body(
            f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}"
        )
        retry_status, retry_raw = await _post_backend_once(
            url, headers, retry_body, rid=rid, model_name=model_name
        )
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
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key, api_key)
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
    _mark_model(request, model_name)
    rid = os.urandom(4).hex()
    _log_req(
        f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}"
    )
    _log_req(
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
    _log_body(
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
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {type(e).__name__}: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {"message": f"upstream error: {e}", "type": "upstream_error"}
            },
        )

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log_req(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log_body(
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
        _log(f"{prefix}✗ 网络错误 | {model_name} | {type(e).__name__}: {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log_req(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log_body(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------


@app.post("/messages")
@app.post("/v1/messages")
async def create_message(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key, api_key)
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
    _mark_model(request, model_name)
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log_req(
        f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)} | user_stream={user_stream}"
    )
    _log_body(
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
    prefix = f"[{rid}] " if rid else ""

    # 整个响应聚合完才返回给客户端 —— 网络错误可安全重试（converter 每次新建）。
    retries = max(0, int(CONFIG.get("stream_retry", 1)))
    converter = None
    for attempt in range(1, retries + 2):
        converter = AnthropicStreamConverter(model=model_name)
        try:
            async with environments.make_async_client(CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=_upstream_timeout(120.0)) as c:
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
            break
        except httpx.HTTPError as e:
            if attempt > retries:
                _log(
                    f"{prefix}✗ 网络错误 | {model_name} | {type(e).__name__}: {e}；重试后仍失败"
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": {"message": str(e)[:500], "type": "api_error", "code": 502}
                    },
                ) from None
            _log(
                f"{prefix}↻ 网络错误 | {model_name} | {type(e).__name__}: {e}"
                f" | 自动重试 {attempt}/{retries}"
            )
            await _retry_wait(attempt)

    elapsed = time.time() - t0 if t0 else 0
    _log_req(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | nonstream done")
    return converter.get_nonstream_response()


async def _stream_anthropic(
    url: str,
    headers: dict,
    body: dict,
    model_name: str = "?",
    t0: float = 0.0,
    rid: str = "",
):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。

    网络错误自动重试（与 _stream_upstream 同规则）：只在**还没向客户端发出任何
    字节**时重试。converter 每次尝试都新建 —— feed_line 可能已缓冲事件但尚未吐出，
    复用旧实例会把上一次尝试的状态带进来。
    """
    prefix = f"[{rid}] " if rid else ""
    terminal = False  # True = 非 200 的错误事件已发给客户端
    converter = None

    retries = max(0, int(CONFIG.get("stream_retry", 1)))
    for attempt in range(1, retries + 2):
        sent_any = False
        converter = AnthropicStreamConverter(model=model_name)
        try:
            async with environments.make_async_client(CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=_upstream_timeout(None)) as c:
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
                        terminal = True
                        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
                        break
                    async for line in r.aiter_lines():
                        events = converter.feed_line(line)
                        if events:
                            sent_any = True
                            yield events.encode("utf-8")
            break  # 正常走完
        except httpx.HTTPError as e:
            if sent_any or attempt > retries:
                _log(
                    f"{prefix}✗ 网络错误 | {model_name} | {type(e).__name__}: {e}"
                    + ("；已转发部分数据，无法安全重试" if sent_any else "；重试后仍失败")
                )
                error_evt = {
                    "type": "error",
                    "error": {"message": str(e)[:500], "type": "api_error", "code": 502},
                }
                yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode()
                return
            _log(
                f"{prefix}↻ 网络错误（首字节前）| {model_name} | {type(e).__name__}: {e}"
                f" | 自动重试 {attempt}/{retries}"
            )
            await _retry_wait(attempt)
    if terminal:
        return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log_req(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    api_key: str | None = Header(default=None, alias="api-key"),
):
    """Anthropic token 计数端点。

    Claude Code 在发送消息前调用此端点获取 token 计数。
    后端只支持流式请求，所以我们发送流式请求并从中提取 usage。
    """
    _check_auth(authorization, x_api_key, api_key)
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
    _mark_model(request, chat_body.get("model"))

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
        async with environments.make_async_client(CONFIG.get("env"), proxy=CONFIG.get("proxy"), timeout=30.0) as client:
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
    af = find_auth_file(CONFIG.get("env"))
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs(CONFIG.get("env")):
        sys.stderr.write(
            f"已查目录  : {', '.join(str(d) for d in auth_dirs(CONFIG.get('env')))}\n"
        )
    ok = True
    if af is None:
        sys.stderr.write(
            "\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n"
        )
        ok = False
    else:
        try:
            cm = CredentialManager(
                af, env=CONFIG.get("env"), proxy=CONFIG.get("proxy")
            )
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
        "不传则不记日志。默认只记错误/异常（含 401、上游非 200、网络错误、任务告警）。",
    )
    ap.add_argument(
        "--log-requests",
        action="store_true",
        default=bool(os.environ.get("CODEBUDDY2OPENAI_LOG_REQUESTS")),
        help="记录每次模型调用的请求摘要与返回状态（每请求 1~2 行）。"
        "也可用环境变量 CODEBUDDY2OPENAI_LOG_REQUESTS=1。默认关闭。",
    )
    ap.add_argument(
        "--log-bodies",
        action="store_true",
        default=bool(os.environ.get("CODEBUDDY2OPENAI_LOG_BODIES")),
        help="记录完整报文（发往后端的 body / 上游原始 SSE 全文）。"
        "排障用，量很大；也可用环境变量 CODEBUDDY2OPENAI_LOG_BODIES=1。默认关闭。",
    )
    ap.add_argument(
        "--stream-retry",
        type=int,
        default=int(os.environ.get("CODEBUDDY2OPENAI_STREAM_RETRY", 5)),
        metavar="N",
        help="网络错误自动重试次数（默认 5，0 = 关闭）。只对**尚未向客户端发出任何"
        "字节**的失败生效（建连失败 / 隧道抖动 / 上游在出首字节前重置）；"
        "已开始转发后再断流无法安全重试。也可用环境变量 CODEBUDDY2OPENAI_STREAM_RETRY。",
    )
    ap.add_argument(
        "--stream-retry-gap",
        type=float,
        default=float(os.environ.get("CODEBUDDY2OPENAI_STREAM_RETRY_GAP", 2)),
        metavar="SEC",
        help="重试间隔基数（秒，默认 2），按 2^n 指数退避、封顶 30s；0 = 立即重试。"
        "也可用环境变量 CODEBUDDY2OPENAI_STREAM_RETRY_GAP。",
    )
    ap.add_argument(
        "--ua-version",
        default=os.environ.get("CODEBUDDY2OPENAI_UA_VERSION") or None,
        metavar="VER",
        help="对上游声明 CLI/<VER> CodeBuddy/<VER> 的版本号。"
        "上游只校验 UA 形态不校验版本号，CodeBuddy CLI 升级时改这个配置即可，无需改代码。"
        "也可用环境变量 CODEBUDDY2OPENAI_UA_VERSION。缺省用内置值。",
    )
    ap.add_argument(
        "--ua-version-file",
        default=os.environ.get("CODEBUDDY2OPENAI_UA_VERSION_FILE") or None,
        metavar="PATH",
        help="版本文件（热读取，改完即生效，无需重启）。"
        "默认与 --log 同目录的 ua-version.txt；文件不存在/为空 = 用内置默认版本。"
        "内容取第一个非 # 非空行。",
    )
    ap.add_argument(
        "--state-file",
        default=os.environ.get("CODEBUDDY2OPENAI_STATE_FILE") or None,
        metavar="PATH",
        help="状态快照路径（记录可用模型 / 积分余额 / 任务状态）。"
        "默认与 --log 同目录的 state.json；给目录则用其中的 state.json。",
    )
    ap.add_argument(
        "--state-interval",
        type=int,
        default=int(os.environ.get("CODEBUDDY2OPENAI_STATE_INTERVAL") or 3600),
        metavar="SECONDS",
        help="状态快照的周期刷新间隔秒数，默认 3600。"
        "设 0 表示只在事件发生时刷新（启动 / 模型刷新 / 任务跑完）。",
    )
    ap.add_argument(
        "--cors-origins",
        default=os.environ.get("CODEBUDDY2OPENAI_CORS_ORIGINS"),
        metavar="SPEC",
        help="浏览器直连的 CORS 允许来源：* 全放行（默认）、off 禁用、"
        "或逗号分隔来源列表（如 https://a.example,https://b.example）。"
        "也可用环境变量 CODEBUDDY2OPENAI_CORS_ORIGINS。",
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
        "--env",
        choices=("cn", "intl", "auto"),
        default=os.environ.get("CODEBUDDY2OPENAI_ENV") or "auto",
        metavar="cn|intl|auto",
        help="上游环境：cn=国内版（copilot.tencent.com + www.codebuddy.cn）、"
        "intl=国际版（www.codebuddy.ai）、auto=按凭据 auth.domain 自动判断（默认）。"
        "也可用环境变量 CODEBUDDY2OPENAI_ENV。",
    )
    ap.add_argument(
        "--proxy-cn",
        default=os.environ.get("CODEBUDDY2OPENAI_PROXY_CN"),
        metavar="URL",
        help="国内版出站代理，如 http://127.0.0.1:7890 或 socks5://user:pass@host:1080。"
        "留空=直连（国内直连通常最快）。",
    )
    ap.add_argument(
        "--proxy-intl",
        default=os.environ.get("CODEBUDDY2OPENAI_PROXY_INTL"),
        metavar="URL",
        help="国际版出站代理。NAS 在国内时访问 www.codebuddy.ai 一般需要它。"
        "也可用统一兜底 CODEBUDDY2OPENAI_PROXY。",
    )
    ap.add_argument(
        "--auth-cn",
        default=os.environ.get("CODEBUDDY2OPENAI_AUTH_CN"),
        metavar="PATH",
        help="国内版凭据路径（目录或 .info 文件）。不传则按 CODEBUDDY_AUTH_DIR/cn 约定查找。",
    )
    ap.add_argument(
        "--auth-intl",
        default=os.environ.get("CODEBUDDY2OPENAI_AUTH_INTL"),
        metavar="PATH",
        help="国际版凭据路径（目录或 .info 文件）。不传则按 CODEBUDDY_AUTH_DIR/intl 约定查找。",
    )
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

    # ---- 环境与代理解析（必须在任何出站请求之前完成）--------------------
    # 两轮解析，因为「环境」和「用哪份凭据」互为因果：
    #   第 1 轮：先按 CLI/环境变量定环境；若是 auto，就从**各环境候选凭据目录**
    #            里找出唯一能认领的那份 `.info`（读它的 auth.domain 反推）。
    #   第 2 轮：环境定了，再收敛到该环境专属的凭据文件与代理。
    # 分开放置（auth/cn、auth/intl 或 --auth-cn/--auth-intl）让第 1 轮无歧义；
    # 万一两份凭据同处一目录，退化为「按文件名字典序」的旧行为并给出警告。
    env_name = environments.resolve_env(args.env)

    if environments.normalize_env(args.env) in (None, environments.AUTO):
        # auto：优先看当前环境的专属目录能不能认领到凭据
        _probe = find_auth_file(env_name)
        _probe_domain = read_auth_domain(_probe)
        _probe_env = environments.env_of_domain(_probe_domain)
        if _probe_env:
            env_name = _probe_env
        else:
            # 专属目录没命中，再扫一遍全部候选，取第一份能认出域的凭据
            for _cand in find_all_auth_files():
                _ce = _info_env(_cand)
                if _ce:
                    env_name = _ce
                    break

    proxy_url = environments.resolve_proxy(
        env_name,
        explicit=(args.proxy_intl if env_name == environments.INTL else args.proxy_cn),
    )
    CONFIG["env"] = env_name
    CONFIG["proxy"] = proxy_url

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # CORS 收窄/禁用（默认全放行，见 app 定义处的说明）；未指定时保持默认
    if args.cors_origins is not None:
        _apply_cors_config(args.cors_origins)
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = (
        args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    )
    CONFIG["log_requests"] = bool(args.log_requests)
    CONFIG["log_bodies"] = bool(args.log_bodies)
    CONFIG["stream_retry"] = max(0, int(args.stream_retry))
    CONFIG["stream_retry_gap"] = max(0.0, float(args.stream_retry_gap))
    af = find_auth_file(env_name)
    # 同目录多凭据 → 认领结果可能不是你想要的，明确警告而不是静默取值。
    _siblings = find_all_auth_files(env_name)
    if len(_siblings) > 1:
        _others = [p.name for p in _siblings if p != af]
        sys.stderr.write(
            f"[警告] 环境 {env_name} 的凭据目录里有 {len(_siblings)} 份 .info"
            f"（{', '.join(p.name for p in _siblings)}），已选 {af.name if af else '(无)'}。\n"
            f"       国内版/国际版请分开放置：--auth-cn / --auth-intl，"
            f"或把凭据放进 CODEBUDDY_AUTH_DIR/cn 与 /intl 两个子目录\n"
            f"       （未选中的：{', '.join(_others) or '无'}）。\n"
        )
    CONFIG["auth_file"] = str(af) if af else None
    CONFIG["cred"] = (
        CredentialManager(af, env=env_name, proxy=proxy_url) if af else None
    )

    # 动态模型注册表（上游拉取 + 内存缓存 + 落盘快照 + 多级回退）
    # on_resolve：registry 的任何产出路径（含 warm 后台线程）都登记一次，
    # 能力表与状态快照才不会漏写首刷。
    registry = ModelRegistry(
        base_url=BACKEND,
        cache_dir=args.cache_dir,
        ttl=args.models_ttl,
        enabled=not args.no_dynamic_models,
        log=_log,
        env=env_name,
        proxy=proxy_url,
        on_resolve=lambda infos, source: _remember_models(
            [i.to_dict() for i in infos], source
        ),
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
        env=env_name,
        proxy=proxy_url,
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
            # 每类任务跑完就刷新一次状态快照 —— 积分正是在这些时点变化的
            on_result=lambda _k, _r: _request_snapshot(),
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

    # 状态快照（logs/state.json）：把「可用模型 / 积分余额 / 任务状态」落成文件。
    # 路径默认跟 --log 同目录 —— 这样不必新增挂载：compose 已经挂了 logs/，
    # 快照自然跟着可见、跟着持久化。--state-file 可显式覆盖。
    state_target = args.state_file or CONFIG.get("log_path")
    state_path = state_snapshot.snapshot_path(state_target)
    # 模型能力表（logs/model_capabilities.json + .md）：上下文窗口 + 思考强度。
    # 与快照同目录策略 —— 不新增挂载。没配日志目录就不启用。
    # ⚠️ 必须在 writer.tick() 之前赋值：tick 会经 _collect_state 触发首次模型解析，
    # 解析路径里就要写能力表 —— 晚了第一轮就静默跳过。
    if state_path is not None:
        CONFIG["capabilities_path"] = model_capabilities.default_path(state_target)
    if state_path is None:
        CONFIG["snapshot"] = None
        sys.stderr.write(
            "[提示] 未配置 --log / --state-file，状态快照未启用"
            "（想记录可用模型与积分请加 --log /logs/converter.log）。\n"
        )
    else:
        writer = state_snapshot.SnapshotWriter(
            state_path,
            _collect_state,
            interval=args.state_interval,
            before_build=_refresh_points,
            log=_log,
        )
        CONFIG["snapshot"] = writer
        writer.start()
        # 启动时同步写一次：让文件立刻存在，而不是等第一个周期到点。
        writer.tick()
        _log(f"[state] 状态快照已启用 → {state_path}（周期 {args.state_interval}s）")

    if not args.skip_check:
        preflight()

    # UA 版本运行时覆盖：上游 CLI 升级时改配置即可，不必改代码重新打包。
    # （/v3/config 的 UA 门槛只校验形态不校验版本号，所以这只影响「对齐语义」。）
    model_registry.set_cli_version(args.ua_version)
    # 版本文件（热读取）：默认与日志同目录（logs/ 已挂载，File Station 直接改）。
    # 改完下一个请求就生效 —— 不用重启容器，更不用重建。
    _ua_file = args.ua_version_file or (
        str(Path(CONFIG["log_path"]).with_name("ua-version.txt"))
        if CONFIG.get("log_path") else None
    )
    model_registry.set_cli_version_file(_ua_file)

    _eff_cli = model_registry.effective_cli_version()
    if model_registry._ua_version_override:
        _cli_src = "--ua-version / 环境变量覆盖"
    elif _ua_file and os.path.exists(_ua_file):
        _cli_src = f"版本文件 {_ua_file}"
    else:
        _cli_src = "内置默认"
    sys.stderr.write(
        f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n"
        f"   服务版本 {model_registry.SERVICE_VERSION}"
        f"（构建号 {model_registry.SERVICE_BUILD}）\n"
        f"   上游 UA  : CLI/{_eff_cli}（{_cli_src}）\n"
        + (
            f"   版本文件 : {_ua_file}（改完即生效，无需重启；空文件=恢复内置默认）\n"
            if _ua_file else ""
        )
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
    _env_desc = environments.describe(env_name, proxy=proxy_url)
    _env_label = {"cn": "国内版", "intl": "国际版"}.get(env_name, env_name)
    sys.stderr.write(f"   环境      : {_env_label}（{env_name}）\n")
    sys.stderr.write(f"   chat 域   : {_env_desc['bases']['chat']}\n")
    sys.stderr.write(f"   billing 域: {_env_desc['bases']['billing']}\n")
    sys.stderr.write(f"   凭据文件  : {af or '(未找到)'}\n")
    sys.stderr.write(
        f"   代理      : {_env_desc['proxy'] or '直连'}"
        + ("（密码已脱敏）" if "@" in (proxy_url or "") else "")
        + "\n"
    )
    if CONFIG["log_path"]:
        mode = "详细（请求+状态）" if CONFIG["log_requests"] else (
            "详细（含报文）" if CONFIG["log_bodies"] else "简单（只记错误/异常）"
        )
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}（{mode}）\n")
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
    if CONFIG.get("snapshot") is not None:
        sys.stderr.write(
            f"   状态快照  : {CONFIG['snapshot'].path}"
            f"（{'每 %ds' % args.state_interval if args.state_interval else '仅事件触发'}，"
            f"含模型清单 / 积分 / 任务）\n"
        )
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
