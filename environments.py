#!/usr/bin/env python3
"""environments.py — 国内版 / 国际版「环境映射 + 分环境代理」，本项目的依赖最底层。

为什么需要这一层：
    CodeBuddy 有国内版与国际版两套部署。两者 **API 路径结构完全同源，只有域名不同**
    （实测：`/v2/chat/completions` 在三个域上都返回 401 —— 端点存在、需鉴权）。
    但两者有一个关键结构差异：

        国内版  chat/billing **分属两个域**
                  chat    → copilot.tencent.com
                  billing → www.codebuddy.cn
        国际版  chat/billing **收敛到同一个域**
                  both    → www.codebuddy.ai

    所以不能简单「替换一个域名字符串」，必须是「一个环境 → 一组域名」的映射。

域映射（实测，详见 docs-cn-intl-domains.md）：

    用途                     cn                          intl
    chat / 模型 / token 刷新  copilot.tencent.com         www.codebuddy.ai
    billing / 签到 / 上报     www.codebuddy.cn            www.codebuddy.ai
    web 降级领奖             www.workbuddy.cn            www.codebuddy.ai
    X-Domain 头              www.codebuddy.cn            www.codebuddy.ai

代理（本项目在 NAS 上做中转时的核心诉求）：
    代理**按环境**取，不按目标域名取 —— 即 `--env intl` 时所有出站统一走国际代理。
    这样语义简单可预测，也允许「国内版直连 + 国际版走代理」这种最常用的形态
    （实测走代理会比直连慢 3~10 倍，国内流量没必要绕道）。

依赖方向：本模块是**最底层**，不 import 任何业务模块（避免循环导入）。
各业务模块单向依赖它，且 `env` / `proxy` 一律**参数注入**，不引入全局可变状态。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# 域映射
# ---------------------------------------------------------------------------

CN = "cn"
INTL = "intl"
AUTO = "auto"

#: 环境 → 域名组。key 是「用途」，value 是完整 base（含 scheme，不带尾斜杠）。
ENVIRONMENTS: dict[str, dict[str, str]] = {
    CN: {
        #: chat / 模型列表 / token 刷新 / growth 域
        "chat": "https://copilot.tencent.com",
        #: billing / 签到 / 余额 / 活跃上报
        "billing": "https://www.codebuddy.cn",
        #: 领奖降级路径（chat 域返回 400 时改打这里）
        "web": "https://www.workbuddy.cn",
        #: X-Domain 请求头的值（凭据缺失 domain 时的兜底）
        "domain": "www.codebuddy.cn",
    },
    INTL: {
        #: 国际版把 chat 与 billing 收敛到同一个域
        "chat": "https://www.codebuddy.ai",
        "billing": "https://www.codebuddy.ai",
        #: 国际版不存在 workbuddy.cn；这里填自身域名作降级兜底 ——
        #: 真走到这条路径会 404，与「不降级」等效，不会比现状更糟。
        "web": "https://www.codebuddy.ai",
        "domain": "www.codebuddy.ai",
    },
}

#: 用途 → 读取哪个环境变量（显式覆盖单个域的进阶用法，一般用不到）。
_ENV_VARS = {
    "chat": "CODEBUDDY2OPENAI_ENV_CHAT_BASE",
    "billing": "CODEBUDDY2OPENAI_ENV_BILLING_BASE",
    "web": "CODEBUDDY2OPENAI_ENV_WEB_BASE",
    "domain": "CODEBUDDY2OPENAI_ENV_DOMAIN",
}

#: 环境选择的环境变量名。
ENV_VAR_ENV = "CODEBUDDY2OPENAI_ENV"

#: 环境 → 该环境专用代理的环境变量名。
PROXY_ENV_VARS = {
    CN: "CODEBUDDY2OPENAI_PROXY_CN",
    INTL: "CODEBUDDY2OPENAI_PROXY_INTL",
}

#: 两个环境都没单独配时的统一代理兜底。
ENV_VAR_PROXY_FALLBACK = "CODEBUDDY2OPENAI_PROXY"

#: 环境 → 该环境专用凭据目录/文件的环境变量名。
#: 分开放置的原因：国内版与国际版的凭据是**两个不同账号的两份 token**
#: （文件名也不同：`workbuddy-desktop.info` vs `Tencent-Cloud.coding-copilot.info`），
#: 都丢进一个目录时 `*.info` 通配会取到不确定的一份，token 与域不匹配 → 全链路 401。
AUTH_ENV_VARS = {
    CN: "CODEBUDDY2OPENAI_AUTH_CN",
    INTL: "CODEBUDDY2OPENAI_AUTH_INTL",
}

#: 分环境凭据目录的约定子目录名（在 CODEBUDDY_AUTH_DIR 下）。
#: 即 <CODEBUDDY_AUTH_DIR>/cn/ 与 <CODEBUDDY_AUTH_DIR>/intl/。
AUTH_SUBDIRS = {CN: "cn", INTL: "intl"}

#: 缺省扫描凭据的目录名（相对 HOME 的平台差异见 converter.auth_dirs）。
AUTH_DIR_NAME = "auth"


# ---------------------------------------------------------------------------
# 环境解析
# ---------------------------------------------------------------------------


def normalize_env(raw: Any) -> str | None:
    """把用户输入的环境名归一化。合法值 cn / intl / auto，其余返回 None。

    容忍大小写与空白（`"INTL "` → `"intl"`）；`auto` 也认。
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in (CN, INTL, AUTO):
        return s
    return None


def env_of_domain(domain: str | None) -> str | None:
    """由域名（`auth.domain` 或 `X-Domain` 头的值）反推环境。

    这是 `auto` 的落点 —— 凭据里的 `domain` 是**天然的环境标识**，跟着它走不会配错。
    """
    if not domain:
        return None
    d = str(domain).strip().lower()
    # 去掉可能的 scheme，兼容传完整 URL 的调用方
    d = d.split("://", 1)[-1].split("/", 1)[0]
    if not d:
        return None
    if d.endswith("codebuddy.ai"):
        return INTL
    if d.endswith("codebuddy.cn") or d.endswith("tencent.com") or d.endswith("workbuddy.cn"):
        return CN
    return None


def resolve_env(explicit: str | None = None, domain: str | None = None) -> str:
    """决定当前生效的环境。优先级：

        1. explicit 参数（CLI `--env` 或函数入参）
        2. 环境变量 CODEBUDDY2OPENAI_ENV
        3. 凭据 auth.domain 自动推断（`auto` 的落点）
        4. 兜底 "cn"（保持改造前的既有行为）

    注意 explicit 传 `auto` 时会跳过第 1 条、落到第 3 条 —— 这正是 `--env auto` 的语义。
    """
    e = normalize_env(explicit)
    if e and e != AUTO:
        return e

    e = normalize_env(os.environ.get(ENV_VAR_ENV))
    if e and e != AUTO:
        return e

    e = env_of_domain(domain)
    if e:
        return e

    return CN


def env_from_headers(headers: dict | None) -> str | None:
    """从请求头里的 `X-Domain` 反推环境（供任务模块用凭据自带的域定位环境）。"""
    if not headers:
        return None
    for k in ("X-Domain", "x-domain"):
        got = headers.get(k)
        if got:
            return env_of_domain(got)
    return None


# ---------------------------------------------------------------------------
# 域名取值
# ---------------------------------------------------------------------------


def base_for(kind: str, env: str | None = None, *, domain: str | None = None) -> str:
    """取某环境某用途的 base URL。

    kind ∈ {chat, billing, web, domain}；env 为 None 时按 resolve_env 推断。
    支持 `CODEBUDDY2OPENAI_ENV_<KIND>_BASE` 显式覆盖单个域（进阶用法）。
    """
    e = resolve_env(env, domain=domain)
    var = _ENV_VARS.get(kind)
    if var:
        override = os.environ.get(var)
        if override and override.strip():
            return override.strip().rstrip("/")
    return ENVIRONMENTS[e][kind]


def domain_for(env: str | None = None, *, domain: str | None = None) -> str:
    """取 X-Domain 头的兜底值。"""
    return base_for("domain", env, domain=domain)


def all_bases(env: str | None = None, *, domain: str | None = None) -> dict[str, str]:
    """一次取全某环境的域映射（便于日志/状态输出与测试）。"""
    e = resolve_env(env, domain=domain)
    return {k: base_for(k, e) for k in ENVIRONMENTS[e]}


# ---------------------------------------------------------------------------
# 代理解析
# ---------------------------------------------------------------------------


def resolve_proxy(env: str | None = None, *, explicit: str | None = None) -> str | None:
    """决定当前生效的代理 URL。优先级：

        1. explicit 参数（CLI --proxy-cn / --proxy-intl 或函数入参）
        2. 按当前环境取 CODEBUDDY2OPENAI_PROXY_CN / _INTL
        3. 统一兜底 CODEBUDDY2OPENAI_PROXY
        4. None（直连）

    返回值统一去掉两端空白；空串视为「未配置」。
    """
    if explicit is not None:
        s = str(explicit).strip()
        if s:
            return s

    e = resolve_env(env)
    var = PROXY_ENV_VARS.get(e)
    if var:
        s = (os.environ.get(var) or "").strip()
        if s:
            return s

    s = (os.environ.get(ENV_VAR_PROXY_FALLBACK) or "").strip()
    return s or None


# ---------------------------------------------------------------------------
# HTTP 客户端工厂
# ---------------------------------------------------------------------------


def _proxy_kwargs(env: str | None, proxy: str | None) -> dict:
    """共用的代理解析：返回要透传给 httpx.Client / AsyncClient 的 kwargs。

    proxy 语义：
      - None  → 走 resolve_proxy 的优先级链（环境变量）
      - ""    → 强制直连（忽略环境变量）
      - 其他   → 原样使用
    """
    if proxy == "":
        eff: str | None = None
    elif proxy is not None:
        eff = proxy.strip() or None
    else:
        eff = resolve_proxy(env)
    return {"proxy": eff} if eff else {}


def make_client(
    env: str | None = None,
    *,
    proxy: str | None = None,
    timeout: float = 60.0,
    transport: object | None = None,
    follow_redirects: bool = False,
) -> httpx.Client:
    """按环境造一个同步 httpx.Client（统一挂代理）。

    为什么要工厂：代理必须**每一处出站都生效**，散落各处手写
    `httpx.Client(timeout=...)` 迟早漏掉一处。收敛成一个函数，改一处全生效。

    proxy 传 None 时会走 resolve_proxy 的优先级链（含环境变量）。
    要**强制直连**（忽略环境变量）传 `proxy=""`。

    ⚠️ 版本约束：httpx 0.28 起只支持 `proxy=`，旧版 `proxies=` 已移除。
    requirements.txt 锁 `httpx>=0.28,<0.29`；用 SOCKS5 还需要 `httpx[socks]`。

    transport 仅用于测试注入（httpx 的 MockTransport）。
    """
    kwargs: dict = {"timeout": timeout, "follow_redirects": follow_redirects}
    if transport is not None:
        kwargs["transport"] = transport
    kwargs.update(_proxy_kwargs(env, proxy))
    return httpx.Client(**kwargs)


def make_async_client(
    env: str | None = None,
    *,
    proxy: str | None = None,
    timeout: float | None = 60.0,
    transport: object | None = None,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """make_client 的异步版（chat 转发 / 流式转发用）。

    timeout 允许 None（流式接口不限时，与既有调用点一致）。
    """
    kwargs: dict = {"timeout": timeout, "follow_redirects": follow_redirects}
    if transport is not None:
        kwargs["transport"] = transport
    kwargs.update(_proxy_kwargs(env, proxy))
    return httpx.AsyncClient(**kwargs)


# ---------------------------------------------------------------------------
# 状态描述（日志 / /health 用）
# ---------------------------------------------------------------------------


def describe(env: str | None = None, *, proxy: str | None = None, domain: str | None = None) -> dict:
    """给出一份可直接打进日志的环境描述（**不泄露代理里的用户名密码**）。"""
    e = resolve_env(env, domain=domain)
    p = resolve_proxy(e, explicit=proxy)
    return {
        "env": e,
        "bases": all_bases(e),
        "proxy": redact_proxy(p),
        "proxy_set": bool(p),
    }


def redact_proxy(url: str | None) -> str:
    """把代理 URL 里的用户名密码脱敏（日志里绝不能出现明文凭据）。"""
    if not url:
        return ""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not _:
        # 没有 scheme，直接处理 user:pass@host
        return "***@" + url.rsplit("@", 1)[1]
    _creds, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------
# 凭据定位（国内版 / 国际版分开放置）
# ---------------------------------------------------------------------------


def resolve_auth_path(env: str | None = None, *, explicit: str | None = None) -> str | None:
    """返回该环境**显式配置**的凭据路径（目录或文件），没配则 None。

    优先级：
        1. explicit 参数（CLI `--auth-cn` / `--auth-intl`）
        2. 按环境取 CODEBUDDY2OPENAI_AUTH_CN / _INTL
        3. None —— 调用方回落到「目录约定」与「老的单目录通配」两档兼容链

    这里的值允许是目录（目录下扫 `*.info`）或直接是某个 `.info` 文件。
    """
    if explicit is not None:
        s = str(explicit).strip()
        if s:
            return s

    var = AUTH_ENV_VARS.get(resolve_env(env))
    if var:
        s = (os.environ.get(var) or "").strip()
        if s:
            return s
    return None


def auth_subdir(base_dir: str | os.PathLike, env: str | None = None) -> str:
    """给定根目录，返回该环境的约定凭据目录（`<base>/cn` 或 `<base>/intl`）。

    用于 NAS 部署：挂一个 volume，里面按环境建两个子目录，互不干扰。
    """
    e = resolve_env(env)
    return os.path.join(str(base_dir), AUTH_SUBDIRS[e])


__all__ = [
    "CN",
    "INTL",
    "AUTO",
    "ENVIRONMENTS",
    "ENV_VAR_ENV",
    "PROXY_ENV_VARS",
    "ENV_VAR_PROXY_FALLBACK",
    "AUTH_ENV_VARS",
    "AUTH_SUBDIRS",
    "AUTH_DIR_NAME",
    "normalize_env",
    "env_of_domain",
    "env_from_headers",
    "resolve_env",
    "base_for",
    "domain_for",
    "all_bases",
    "resolve_proxy",
    "resolve_auth_path",
    "auth_subdir",
    "make_client",
    "make_async_client",
    "describe",
    "redact_proxy",
]
