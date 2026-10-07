#!/usr/bin/env python3
"""
model_registry.py — 上游动态模型发现（模型列表不再依赖本地 product.json 或硬编码表）。

权威链路（2026-09-19 实测确立，取代旧的 `/console/enterprises/personal/models`）：

    GET {chat_base}/v3/config
      → 解 {code,msg,data} 信封
      → 取 data.agents[name=="cli"].models 作为白名单
      → 以 data.models 建索引，只输出「白名单内 且 disabled != true」的模型
      → maxInputTokens → context_length，maxOutputTokens → max_tokens

**`/v3/config` 是唯一权威来源，但有硬门槛：User-Agent 必须形如
`CLI/<ver> CodeBuddy/<ver>`**（大小写敏感；版本号不校验，`CLI/1.0.0 CodeBuddy/1.0.0`
与 `CLI/9.9.9 CodeBuddy/9.9.9` 等价）。UA 不合法时接口照样 200，但 `data.models`
恒为 `null` —— 这正是此前误判「国际版拿不到权威清单」的真实原因：

    CLI/2.155.0 CodeBuddy/2.155.0             → 200  21 个（国际版）
    CLI/2.155.0 CodeBuddy/2.155.0 (darwin; arm64) → 200  21 个（后缀不影响）
    CodeBuddy/2.155.0                         → 200  null
    codebuddy/2.155.0（小写）                  → 200  null
    CLI/2.155.0（单独）                        → 400

`x-domain` / `x-product` / `x-user-id` 都**不是**决定项（逐个加测均仍是 null）。

相对 Go 参考实现修掉的三个缺陷：

  D1 静态回退表过期        → FALLBACK_MODELS_BY_ENV 按上游实测重写（分环境）
  D2 失败期回退到过期内置表 → 失败时先回退「上一份真实快照」（带 source=stale-*），拿不到才谈内置表
  D5 快照仅进程内存、重启冷启动 → 快照落盘，新进程启动即可用（首个请求若命中快照则同步返回）

另修掉一个**本项目自造**的缺陷：

  D6 探针候选池跨环境派生    → 探针池一律来自**本环境**回退表，绝不用国内版表去探国际版端点

缓存语义与参考实现一致，仍是**惰性判定、非定时刷新**：
  - 命中新鲜快照（age < ttl，默认 1h）→ 0 次上游调用；
  - 拉取失败后 fail_cooldown（默认 5min）内不再打上游，直接走回退；
  - 快照进程内全局一份，不按账号分片。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import httpx

import environments

#: 上游 models 接口的 CLI 白名单 agent 名
CLI_AGENT = "cli"

DEFAULT_TTL = 3600.0
DEFAULT_FAIL_COOLDOWN = 300.0
DEFAULT_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# User-Agent —— `/v3/config` 的准入证
# ---------------------------------------------------------------------------

#: `/v3/config` 要求 UA 形如 `CLI/<ver> CodeBuddy/<ver>`（大小写敏感）。
#: 版本号不参与校验，但保留一个真实版本号更稳妥（未来上游收紧时不易失效）。
CLI_UA_VERSION = "2.158.0"

#: UA 版本的运行时覆盖（环境变量 / 启动参数）。
#: 为什么要可配：上游 CLI 发新版本（如 2.159.0）时，`/v3/config` 的 UA 门槛
#: **不校验版本号**（只校验 `CLI/x CodeBuddy/x` 形态），所以跟着升版本号是
#: 「对齐语义」而非「功能必需」—— 不该为它改代码重新打包。
#: 优先级：set_cli_version() 注入 > 版本文件（热读取）> 环境变量 > 常量。
#: 常量只作「最后一次实测验证过的版本」默认值。
UA_ENV_KEY = "CODEBUDDY2OPENAI_UA_VERSION"

#: 版本文件（默认与日志同目录的 ua-version.txt，由 converter 装配）。
#: 为什么用文件而不是环境变量：compose 的 environment 改了要**重建容器**才生效；
#: 文件在挂载目录里，File Station 改完下一个请求就用新版本 —— 连重启都不用。
UA_FILE_ENV_KEY = "CODEBUDDY2OPENAI_UA_VERSION_FILE"

_ua_version_override: str | None = None
_ua_file_path: str | None = None
_ua_file_cache: dict = {"mtime": None, "value": None}


def set_cli_version(version: str | None) -> None:
    """程序化设置 UA 版本（converter 启动时从 --ua-version / 环境变量注入）。"""
    global _ua_version_override
    _ua_version_override = (version or "").strip() or None


def set_cli_version_file(path: str | None) -> None:
    """设置版本文件路径（converter 启动时装配）。None = 关闭文件来源。"""
    global _ua_file_path, _ua_file_cache
    _ua_file_path = (path or "").strip() or None
    _ua_file_cache = {"mtime": None, "value": None}


def _read_version_file() -> str | None:
    """读版本文件，带 mtime 缓存（与凭据文件的 _load_if_stale 同一策略）。

    文件内容取**第一个非空且非 # 开头的行**，strip 后为空视作「未设置」——
    所以把文件清空就是「恢复内置默认」，不用删文件。
    读失败（不存在/权限）静默返回上次值或 None：配置文件不能影响主流程。
    """
    if not _ua_file_path:
        return None
    try:
        mtime = os.stat(_ua_file_path).st_mtime
    except OSError:
        return _ua_file_cache["value"]
    if _ua_file_cache["mtime"] == mtime:
        return _ua_file_cache["value"]
    value = None
    try:
        with open(_ua_file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    value = line
                    break
    except OSError:
        return _ua_file_cache["value"]
    _ua_file_cache["mtime"] = mtime
    _ua_file_cache["value"] = value
    return value


def effective_cli_version() -> str:
    """实际对上游声明的 CLI 版本：注入 > 版本文件 > 环境变量 > 常量默认值。"""
    if _ua_version_override:
        return _ua_version_override
    file_val = _read_version_file()
    if file_val:
        return file_val
    env_val = (os.environ.get(UA_ENV_KEY) or "").strip()
    if env_val:
        return env_val
    return CLI_UA_VERSION

#: 服务自身构建号。**与 CLI 版本是两回事**：
#:   - CLI_UA_VERSION 只在改「对齐上游 CLI」的契约时才动（UA 必须与 CLI 一致，不能乱升）；
#:     且它只是**默认值** —— 运行时可用 --ua-version / CODEBUDDY2OPENAI_UA_VERSION
#:     覆盖（上游不校验版本号，CLI 升级改配置即可，不必改代码）。
#:   - SERVICE_BUILD 每次服务代码有实质变更就 +1，用于镜像 tag / 包名，
#:     让「旧镜像」与「新镜像」天然可区分。
#: 为什么要拆开：image tag 若一直等于 2.155.0，`docker compose up -d` 会复用
#: 旧镜像，改了代码不生效（踩过两次）；而 --no-cache 靠人记，迟早忘。
#: tag 变了 Docker 自然新建镜像 —— 这才是版本号的本来目的。
SERVICE_BUILD = 13

#: 对外版本串（包名 / 镜像 tag / 启动日志统一用这个）：
#: `2.155.0-b2` = CLI 版本 + 服务构建号。单一真源仍是本文件，别处不得硬编码。
SERVICE_VERSION = f"{CLI_UA_VERSION}-b{SERVICE_BUILD}"

#: 判定 UA 是否合法的正则（上游真实规则：前缀 CLI/ 后必须再出现 CodeBuddy/，大小写敏感）。
CLI_UA_RE = re.compile(r"^CLI/\S+\s+CodeBuddy/\S+")


def cli_user_agent(version: str | None = None) -> str:
    """构造 `/v3/config` 能认的 User-Agent。版本取 effective_cli_version()。"""
    v = version or effective_cli_version()
    return f"CLI/{v} CodeBuddy/{v}"


def is_cli_user_agent(ua: str | None) -> bool:
    """判断一个 UA 是否能通过 `/v3/config` 的门槛（只用于测试与自检）。"""
    return bool(ua) and bool(CLI_UA_RE.match(str(ua)))


# ---------------------------------------------------------------------------
# 分环境内置回退表
# ---------------------------------------------------------------------------
#
# 为什么必须分环境：两个环境的模型集**不是包含关系**。
# 国内版有 glm-5.3-flash / hy3-x / minimax-m3 / deepseek-v4-pro，国际版没有；
# 国际版有 gpt-6-astra / gemini-3.5-flash / 一整套 gpt-5.6-* 与别名 default-model 等，
# 国内版没有。用一张表覆盖两个环境，在任一侧都会给出「调不通的模型」。
#
# 数据来源：2026-09-19 用正式凭据直接打 `{chat_base}/v3/config`，取
# `data.agents[name=="cli"].models` 白名单 ∩ `data.models` 的结果，逐条落库。

#: 国内版（copilot.tencent.com）实测：`data.models[]` 31 个，但 **cli agent 白名单 16 个**
#: —— 对外真正可用的就是这 16 个，与旧表完全一致（旧表本来就是对的）。
#: 2026-09-25 用 UA=CLI/2.158.0 复核：白名单仍为这 16 个，无增减。
#: 这张表按**白名单口径**收，与服务 `/v1/models` 的输出一致。
FALLBACK_MODELS_CN = [
    "hy4-preview",
    "hy3",
    "hy3-x",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "minimax-m2.7",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]

#: 国内版 `models[]` 里存在、但不在 cli 白名单的模型（诊断用：对比 31 vs 16 的差）。
CN_NON_CLI_MODELS = [
    "default",
    "deepseek-v4-flash",
    "deepseek-v3-2-volc",
    "minimax-m2.5",
    "glm-5.0",
    "glm-5.0-turbo",
    "glm-4.7",
    "glm-4.6",
    "glm-4.6v",
    "kimi-k2.5",
    "kimi-k2-thinking",
    "hy4-preview-x",
    "hunyuan-chat",
    "hunyuan-image-alpha",
    "hunyuan-image-alpha-edit",
]

#: 国际版（www.codebuddy.ai）实测（2026-09-25，UA=CLI/2.158.0）：`data.models[]` 22 个，
#: **cli agent 白名单 21 个** —— `deepseek-v4.1-flash-sg` 仍只在 models[] 里（服务端不给 CLI 用）。
#: 相对 2.155.0 旧表（20 个）新增了 `glm-5.3-flash`（排在 gemini-3.5-flash 之后，按白名单原序）。
#: 这张表按**实际对外可用**（即白名单口径）收 21 个，与服务 `/v1/models` 的输出一致。
FALLBACK_MODELS_INTL = [
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
    "hy4-preview",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]

#: 国际版 `models[]` 里存在、但不在 cli 白名单的模型（诊断用：对比 22 vs 21 的差）。
INTL_NON_CLI_MODELS = ["deepseek-v4.1-flash-sg"]

#: 环境 → 内置回退表。仅在上游拉取失败且无任何快照时使用，正常路径不会命中。
FALLBACK_MODELS_BY_ENV: dict[str, list[str]] = {
    environments.CN: FALLBACK_MODELS_CN,
    environments.INTL: FALLBACK_MODELS_INTL,
}

#: 兼容旧调用点的默认值（国内版表）。**不要**用它去探国际版端点。
FALLBACK_MODELS = FALLBACK_MODELS_CN

SNAPSHOT_NAME = "models-snapshot.json"

#: 探针的判别码：`code 11102 == model [x] service info not found`（上游明确语义）。
PROBE_MODEL_CODE = 11102


def fallback_models(env: str | None = None) -> list[str]:
    """取某环境的内置回退表（env 为 None / 未知时按 resolve_env 推断，兜底国内版）。"""
    e = environments.resolve_env(env)
    return list(FALLBACK_MODELS_BY_ENV.get(e) or FALLBACK_MODELS_CN)


def probe_candidates(env: str | None = None) -> list[str]:
    """探针候选池 —— **按环境取**，与内置回退表同源。

    D6 修复：原实现是 `list(FALLBACK_MODELS) + ["kimi-k2.5", "kimi-k3"]`，
    而 FALLBACK_MODELS 是国内版表。拿国内版名单去探国际版端点，只能捞出
    两个集合的**交集**（14 个），既漏掉国际版独有的 gpt/gemini 系，也把
    国内版独有的 glm-5.3-flash / hy3-x / deepseek-v4-pro 这类「不存在」的
    噪声混进了探测流程。探针池必须与本环境回退表同源。
    """
    return fallback_models(env)


#: 默认候选池（国内版）。保留此名只为兼容旧调用点/旧测试；
#: 新代码请用 `probe_candidates(env)` —— 跨环境使用会重现 D6。
PROBE_CANDIDATES: list[str] = probe_candidates(environments.CN)


class ModelFetchError(RuntimeError):
    """上游模型接口不可用（HTTP 非 200 / code != 0 / 白名单为空 / 解析失败）。"""


# ---------------------------------------------------------------------------
# 上下文窗口解析
# ---------------------------------------------------------------------------

def normalize_supported_context_windows(
    lengths, max_input_tokens: int | None = None
) -> list[int]:
    """归一 `contextWindow.supportedLengths`。

    对齐上游 `normalizeSupportedContextWindows`：
      过滤非正安全整数 → 过滤 > maxInputTokens 的项 → 去重 → 升序。

    为什么要过滤 > maxInputTokens：上游自己就这么做（窗口不可能比输入上限还大），
    照抄可以避免把脏数据带进模型表。
    """
    out: list[int] = []
    for v in lengths or []:
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if v <= 0:
            continue
        if max_input_tokens is not None and v > max_input_tokens:
            continue
        if v not in out:
            out.append(v)
    return sorted(out)


def resolve_effective_context_window(
    *,
    override: int | None = None,
    context_window: dict | None = None,
    max_input_tokens: int | None = None,
) -> int:
    """解析**真正生效**的上下文预算。

    严格对齐上游 `resolveEffectiveContextBudget` 的优先级：
      ① override（会话覆盖，且必须恰好落在支持列表里）
      ② contextWindow.defaultLength（且必须在支持列表里）
      ③ supportedLengths 的最小档（仅当支持列表 ≥ 2 档）
      ④ maxInputTokens

    为什么不能用 maxInputTokens 了事：部分模型（如 gpt-6-astra）声明
    `maxInputTokens=1000000` 但 `supportedLengths=[400000, 1000000]`、
    `defaultLength=400000` —— 默认跑的是 400K。填 1M 会让客户端迟迟不压缩，
    最后撞上游 max_tokens 报错。这个坑在 Codex 的 models.json 上真实踩过。
    """
    mip = max_input_tokens if isinstance(max_input_tokens, int) and max_input_tokens > 0 else None
    cw = context_window if isinstance(context_window, dict) else {}
    supported = normalize_supported_context_windows(cw.get("supportedLengths"), mip)

    def _is_budget(v) -> bool:
        return (
            isinstance(v, int) and not isinstance(v, bool) and v > 0
            and len(supported) >= 2 and v in supported
        )

    if _is_budget(override):
        return int(override)
    if _is_budget(cw.get("defaultLength")):
        return int(cw["defaultLength"])
    if len(supported) >= 2:
        return supported[0]
    if mip:
        return mip
    return 0


def model_info_from_upstream(m: dict) -> ModelInfo:
    """把上游 `/v3/config` 里的一个模型对象裁剪成 `ModelInfo`。

    抽成独立函数是为了让 `sync_codex_models.py` 能复用同一套解析 ——
    否则两处各写一份窗口解析逻辑，必然漂移（这个项目已经因为
    「两份环境表」踩过一次 D6 了）。
    """
    m = m or {}
    mip = m.get("maxInputTokens")
    mip_int = int(mip) if isinstance(mip, int) and mip > 0 else 0
    reasoning = m.get("reasoning") or {}
    efforts = tuple(
        str(e) for e in (reasoning.get("supportedEfforts") or []) if e
    )
    default_effort = reasoning.get("defaultEffort") or reasoning.get("effort") or ""
    return ModelInfo(
        id=str(m.get("id") or ""),
        name=str(m.get("name") or ""),
        context_window=resolve_effective_context_window(
            context_window=m.get("contextWindow"),
            max_input_tokens=mip_int or None,
        ),
        max_tokens=int(m.get("maxOutputTokens") or 0),
        efforts=efforts,
        max_allowed_size=int(m.get("maxAllowedSize") or 0),
        context_tiers=tuple(
            normalize_supported_context_windows(
                (m.get("contextWindow") or {}).get("supportedLengths"), mip_int or None
            )
        ),
        supports_reasoning=bool(m.get("supportsReasoning")),
        only_reasoning=bool(m.get("onlyReasoning")),
        default_effort=str(default_effort),
        credits=str(m.get("credits") or ""),
        supports_images=bool(m.get("supportsImages")),
        supports_tool_call=bool(m.get("supportsToolCall")),
    )


@dataclass(frozen=True)
class ModelInfo:
    """裁剪后的模型条目。

    窗口字段不是简单照抄上游的 `maxInputTokens` —— 见 `resolve_effective_context_window`：
    上游允许一个模型声明**多档上下文窗口**（`contextWindow.supportedLengths`），
    此时真正生效的是 `defaultLength`（或最小档），而不是 `maxInputTokens`。
    照抄 `maxInputTokens` 会让客户端以为有 1M 上下文，实际 400K 就触发压缩。

    字段来源（报告《CodeBuddy积分与思考强度机制报告》第三部分）：
      maxInputTokens / maxOutputTokens / maxAllowedSize / contextWindow / reasoning / credits
    """

    id: str
    name: str = ""
    context_window: int = 0
    max_tokens: int = 0
    efforts: tuple[str, ...] = field(default_factory=tuple)
    # 以下是新增的能力字段（2026-09-20）。默认值保证旧快照/旧测试仍能构造。
    max_allowed_size: int = 0
    context_tiers: tuple[int, ...] = field(default_factory=tuple)
    supports_reasoning: bool = False
    only_reasoning: bool = False
    default_effort: str = ""
    credits: str = ""
    supports_images: bool = False
    supports_tool_call: bool = False

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "object": "model", "owned_by": "codebuddy"}
        if self.name:
            d["name"] = self.name
        if self.context_window > 0:
            d["context_length"] = self.context_window
        if self.max_tokens > 0:
            d["max_tokens"] = self.max_tokens
        if self.efforts:
            d["reasoning_efforts"] = list(self.efforts)
        # 扩展能力字段：OpenAI /v1/models 规范不认这些键，但多带不违规，
        # 而且让 Codex 同步工具与状态快照能直接复用同一份数据。
        if self.max_allowed_size > 0:
            d["max_allowed_size"] = self.max_allowed_size
        if self.context_tiers:
            d["context_tiers"] = list(self.context_tiers)
        if self.supports_reasoning:
            d["supports_reasoning"] = True
        if self.only_reasoning:
            d["only_reasoning"] = True
        if self.default_effort:
            d["default_effort"] = self.default_effort
        if self.credits:
            d["credits"] = self.credits
        if self.supports_images:
            d["supports_images"] = True
        if self.supports_tool_call:
            d["supports_tool_call"] = True
        return d

    @staticmethod
    def from_dict(d: dict) -> "ModelInfo":
        return ModelInfo(
            id=d["id"],
            name=d.get("name") or "",
            context_window=int(d.get("context_length") or 0),
            max_tokens=int(d.get("max_tokens") or 0),
            efforts=tuple(d.get("reasoning_efforts") or ()),
            max_allowed_size=int(d.get("max_allowed_size") or 0),
            context_tiers=tuple(int(v) for v in (d.get("context_tiers") or ())),
            supports_reasoning=bool(d.get("supports_reasoning")),
            only_reasoning=bool(d.get("only_reasoning")),
            default_effort=d.get("default_effort") or "",
            credits=d.get("credits") or "",
            supports_images=bool(d.get("supports_images")),
            supports_tool_call=bool(d.get("supports_tool_call")),
        )


def parse_models_payload(env: dict) -> list[ModelInfo]:
    """按上游信封裁剪出 CLI 可用模型集。

    入参是 `GET {chat_base}/v3/config` 的整个响应体（`/console/enterprises/
    personal/models` 的信封同构，也走这里）。

    规则（对照 Go 版 FetchModels）：
      1. code != 0 → ModelFetchError
      2. data.agents[name=="cli"].models 为空 → ModelFetchError
         ⚠️ UA 不合法时 UA 门槛未过，`data.models` 会是 null —— 此时
         `agents` 往往也拿不到，第 2 条就会报错，这正是我们要的信号：
         宁可报错走回退，也不要静默返回空/错的列表。
      3. 只保留白名单内、且在 data.models 索引中存在、且 disabled != true 的条目
      4. 结果为空 → ModelFetchError
    """
    if not isinstance(env, dict):
        raise ModelFetchError("models payload is not an object")
    if env.get("code") not in (0, None):
        raise ModelFetchError(f"models api code={env.get('code')} msg={env.get('msg')}")

    data = env.get("data") or {}
    if not isinstance(data, dict):
        raise ModelFetchError("models payload missing data")

    whitelist: list[str] = []
    for agent in data.get("agents") or []:
        if isinstance(agent, dict) and agent.get("name") == CLI_AGENT:
            whitelist = list(agent.get("models") or [])
            break
    if not whitelist:
        raise ModelFetchError("no cli agent models found")

    index: dict[str, dict] = {}
    for m in data.get("models") or []:
        if isinstance(m, dict) and m.get("id"):
            index[m["id"]] = m

    out: list[ModelInfo] = []
    for mid in whitelist:
        m = index.get(mid)
        if not m or m.get("disabled") is True:
            continue
        info = model_info_from_upstream({**m, "id": mid})
        # id 以白名单为准（上游索引里的 id 可能与白名单写法不同）
        out.append(info)

    if not out:
        raise ModelFetchError("models api returned empty list")
    return out


def _default_cache_dir() -> Path:
    env = os.environ.get("CODEBUDDY2OPENAI_CACHE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "codebuddy2openai"
    xdg = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return xdg / "codebuddy2openai"


class ModelRegistry:
    """动态模型注册表：上游拉取 + 内存缓存 + 落盘快照 + 多级回退。"""

    def __init__(
        self,
        *,
        base_url: str,
        cache_dir: Path | str | None = None,
        ttl: float = DEFAULT_TTL,
        fail_cooldown: float = DEFAULT_FAIL_COOLDOWN,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool = True,
        log: Callable[[str], None] | None = None,
        env: str | None = None,
        proxy: str | None = None,
        on_resolve: Callable[[list["ModelInfo"], str], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        self.ttl = float(ttl)
        self.fail_cooldown = float(fail_cooldown)
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        self.env = env
        self.proxy = proxy
        self._log = log or (lambda _msg: None)
        # on_resolve：每次 resolve 产出模型集（含回退/探针路径）时回调。
        # 为什么需要：converter 的 _remember_models（能力表落盘 + 快照刷新）此前只挂在
        # converter.resolve_models 里，而 warm / status 等直接调 reg.resolve 的路径会
        # 绕过它 → 首次拉取在后台线程完成时能力表永远不写（实测踩过）。
        self.on_resolve = on_resolve

        self._lock = threading.Lock()
        self._models: list[ModelInfo] = []
        self._fetched_at: float = 0.0
        self._last_fail: float = 0.0
        self._last_error: str = ""
        self._source: str = "uninitialized"
        self._last_fetch_ms: int = 0

    # ---------------------------------------------------------------- 快照

    @property
    def snapshot_path(self) -> Path:
        return self.cache_dir / SNAPSHOT_NAME

    def _save_snapshot(self, models: Iterable[ModelInfo], fetched_at: float) -> None:
        payload = {
            "fetched_at": fetched_at,
            "base_url": self.base_url,
            "models": [m.to_dict() for m in models],
        }
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.snapshot_path)
        except OSError as e:
            self._log(f"[models] WARN 快照落盘失败：{e}")

    def _load_snapshot(self) -> tuple[list[ModelInfo], float] | None:
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        items = raw.get("models") or []
        try:
            models = [ModelInfo.from_dict(d) for d in items if isinstance(d, dict) and d.get("id")]
        except (KeyError, TypeError, ValueError):
            return None
        if not models:
            return None
        return models, float(raw.get("fetched_at") or 0.0)

    # ---------------------------------------------------------------- 拉取

    def _fetch(self, headers: dict) -> list[ModelInfo]:
        """打 `/v3/config` 取权威模型清单。

        两个必须做对的地方：
          1. **URL 是 `/v3/config`**（CLI 的 CloudProductProvider 走的那条），
             不是 `/console/enterprises/personal/models` —— 后者对个人号恒返回
             `data: []`（国际版甚至 500），是旧实现的错误来源。
          2. **User-Agent 必须是 `CLI/<ver> CodeBuddy/<ver>`**。否则接口 200
             但 `data.models` 为 null，会被误读成「该环境没有权威清单」。
        """
        url = f"{self.base_url}/v3/config"
        h = dict(headers)
        h["Accept"] = "application/json, text/plain, */*"
        # 覆盖调用方带来的 UA（cn 的 billing UA 是 `CLI/x CodeBuddy/x` 也是合法的，
        # 但 chat 路径的 `codebuddy2openai/2.0` 不是，必须在这里统一校正）。
        h["User-Agent"] = cli_user_agent()
        with environments.make_client(
            self.env, proxy=self.proxy, timeout=self.timeout
        ) as c:
            r = c.get(url, headers=h)
        if r.status_code != 200:
            raise ModelFetchError(
                f"config api status {r.status_code}: {r.text[:120]}"
            )
        try:
            env = r.json()
        except Exception as e:  # noqa: BLE001 — 上游可能返回非 JSON
            raise ModelFetchError(f"config parse: {e}") from e
        return parse_models_payload(env)

    # ------------------------------------------------------------ 探针兜底

    def _probe_one(self, model_id: str, headers: dict) -> bool:
        """探一个模型 id 是否在国际版后端注册。返回 True 表示可用。

        判定口径（上游明确语义，非文本猜测）：
          - HTTP 200                → 存在
          - HTTP 400 + code 11102   → 不存在（`model [x] service info not found`）
          - 其他                    → 保守视为不可用（网络抖动不该污染列表）
        """
        url = f"{self.base_url}/v2/chat/completions"
        body = {
            "model": model_id,
            # 国际版强制要求首条是 system prompt，否则 400 + code 11128，
            # 会把「模型不存在」和「请求不合法」混在一起，探针就失效了。
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hi"},
            ],
            "stream": True,
            "max_tokens": 1,
        }
        try:
            with environments.make_client(
                self.env, proxy=self.proxy, timeout=self.timeout
            ) as c:
                with c.stream("POST", url, headers=headers, json=body) as r:
                    status = r.status_code
                    # 只读极少量字节就断开，别真把整个流拉完
                    for _ in r.iter_bytes():
                        break
            if status == 200:
                return True
            if status == 400:
                return False
        except Exception:  # noqa: BLE001 — 单点失败不放大
            return False
        return False

    def _fetch_by_probe(self, headers: dict) -> list[ModelInfo]:
        """探针法兜底：并发探**本环境**候选池，筛出真实可用的模型集。

        为什么还留着：`/v3/config` 已经能给出权威清单，探针只在它失败时兜底
        （代理抖动 / 凭据过期 / 上游改版）。此时探针给出的仍是**当前真实可用**
        的集合，比旧快照和内置静态表都准。

        为什么并发：串行探 30 个候选 × 单次 3~4s（走代理）≈ 100s，首个请求要等
        一分多钟不可接受。这里用线程池并发，耗时收敛到「最慢的那一个」。

        D6：候选池**必须**按 self.env 取。用国内版名单探国际版端点只会捞出交集。
        """
        candidates = probe_candidates(self.env)
        found: list[ModelInfo] = []
        lock = threading.Lock()

        def work(mid: str) -> None:
            if self._probe_one(mid, headers):
                with lock:
                    found.append(ModelInfo(id=mid, name=mid))

        # 上限 8：再高对上游不礼貌，而且受限于本机到代理的带宽，收益递减。
        workers = min(8, len(candidates)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, candidates))

        if not found:
            raise ModelFetchError("probe found no models")
        # 并发收集顺序不确定 → 按候选池原顺序重排，保证输出稳定（便于快照对比）
        order = {mid: i for i, mid in enumerate(candidates)}
        found.sort(key=lambda m: order.get(m.id, len(order)))
        return found

    # ---------------------------------------------------------------- 对外

    def _notify(self, models: list[ModelInfo], source: str) -> None:
        """成功产出模型集后回调注册方。回调异常绝不能影响 resolve 本身。"""
        cb = getattr(self, "on_resolve", None)
        if cb is None:
            return
        try:
            cb(list(models), source)
        except Exception as e:  # noqa: BLE001
            self._log(f"[models] WARN on_resolve 回调失败：{_truncate(str(e), 120)}")

    def resolve(
        self, headers: dict | None, *, force: bool = False
    ) -> tuple[list[ModelInfo], str]:
        """返回 (模型列表, 来源标记)。

        来源标记：cache / upstream / probe / stale-memory / stale-snapshot / fallback-static。
        无法动态获取且无任何快照时抛 ModelFetchError，由调用方决定是否用内置表。
        """
        if not self.enabled:
            raise ModelFetchError("dynamic models disabled")

        now = time.time()
        with self._lock:
            if not force and self._models and (now - self._fetched_at) < self.ttl:
                self._source = "cache"
                return list(self._models), "cache"
            cooling = (
                not force
                and self._last_fail
                and (now - self._last_fail) < self.fail_cooldown
            )
            stale = list(self._models)
            stale_at = self._fetched_at
            last_error = self._last_error

        if cooling:
            hit = self._fallback(stale, stale_at)
            if hit:
                models, src = hit
                self._log(f"[models] 失败冷却中（{last_error or '上次拉取失败'}），回退 {src}")
                self._notify(models, src)
                return models, src
            raise ModelFetchError(f"models fetch cooling down: {last_error}")

        if headers is None:
            hit = self._fallback(stale, stale_at)
            if hit:
                self._notify(hit[0], hit[1])
                return hit
            raise ModelFetchError("no credential available for models fetch")

        t0 = time.perf_counter()
        try:
            models = self._fetch(headers)
        except Exception as e:  # noqa: BLE001 — 任何失败都要走回退，不能冒泡打断 /v1/models
            elapsed = int((time.perf_counter() - t0) * 1000)
            # 归一化错误文本：上游可能返回多行 HTML，日志必须保持单行
            err = _truncate(str(e), 200)
            self._log(f"[models] ERR 拉取失败（{elapsed}ms）：{err}")

            # 权威接口不可用时才试探针法，再谈旧快照/内置表。
            # 顺序很重要：探针给的是**当前真实可用**的集合，比旧快照和内置静态表都准。
            p0 = time.perf_counter()
            try:
                probed = self._fetch_by_probe(headers)
            except Exception as pe:  # noqa: BLE001
                self._log(f"[models] 探针兜底也未命中：{_truncate(str(pe), 120)}")
            else:
                pms = int((time.perf_counter() - p0) * 1000)
                with self._lock:
                    self._models = probed
                    self._fetched_at = time.time()
                    self._last_fail = 0.0
                    self._last_error = ""
                    self._last_fetch_ms = pms
                    self._source = "probe"
                self._save_snapshot(probed, self._fetched_at)
                self._log(
                    f"[models] OK 探针法筛出 {len(probed)} 个可用模型（{pms}ms）"
                )
                self._notify(probed, "probe")
                return list(probed), "probe"

            with self._lock:
                self._last_fail = time.time()
                self._last_error = err
            hit = self._fallback(stale, stale_at)
            if hit:
                models, src = hit
                self._log(f"[models] 回退 {src}（{len(models)} 个模型）")
                self._notify(models, src)
                return models, src
            raise ModelFetchError(str(e)) from e

        elapsed = int((time.perf_counter() - t0) * 1000)
        with self._lock:
            self._models = models
            self._fetched_at = time.time()
            self._last_fail = 0.0
            self._last_error = ""
            self._last_fetch_ms = elapsed
            self._source = "upstream"
        self._save_snapshot(models, self._fetched_at)
        self._log(f"[models] OK 上游拉取 {len(models)} 个模型（{elapsed}ms）")
        self._notify(models, "upstream")
        return list(models), "upstream"

    def _fallback(
        self, mem: list[ModelInfo], mem_at: float
    ) -> tuple[list[ModelInfo], str] | None:
        """失败回退：优先进程内旧快照（更新鲜），其次落盘快照（跨重启）。"""
        if mem:
            self._source = "stale-memory"
            return list(mem), "stale-memory"
        snap = self._load_snapshot()
        if snap:
            models, at = snap
            with self._lock:
                self._models = models
                self._fetched_at = at
            self._source = "stale-snapshot"
            return models, "stale-snapshot"
        return None

    def warm(self, headers: dict | None) -> None:
        """启动预热：忽略失败，仅为把快照提前装进内存。"""
        if not self.enabled:
            return
        try:
            self.resolve(headers)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._fetched_at if self._fetched_at else None
            return {
                "enabled": self.enabled,
                "source": self._source,
                "models": len(self._models),
                "ids": [m.id for m in self._models],
                "fetched_at": int(self._fetched_at) if self._fetched_at else None,
                "age_seconds": int(age) if age is not None else None,
                "ttl_seconds": int(self.ttl),
                "fresh": bool(age is not None and age < self.ttl),
                "last_fail_at": int(self._last_fail) if self._last_fail else None,
                "last_error": self._last_error or None,
                "last_fetch_ms": self._last_fetch_ms or None,
                "snapshot_file": str(self.snapshot_path),
                "snapshot_exists": self.snapshot_path.exists(),
            }


def _truncate(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")
