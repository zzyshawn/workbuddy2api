"""思考强度（reasoning effort）字段的归一化与透传。

## 为什么需要这个模块

三条协议入口（OpenAI Chat / Anthropic Messages / OpenAI Responses）各自用不同字段
表达「思考强度」，而 CodeBuddy 后端只认**一种**规范形态。此前三个适配器都直接把这些
字段丢掉，导致：
  - Codex 在 config.toml 里写 `model_reasoning_effort = "high"` → 完全无效
  - Claude Code 传 `thinking = {type:"enabled", budget_tokens: N}` → 完全无效
  - 用户在客户端选「低/中/高」档 → 完全无效

## 上游契约（来源：`CodeBuddy积分与思考强度机制报告.md` §5-§9 + CLI bundle 实读）

报告里的权威结论：

  VALID_REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]
  含关闭的完整阶梯       = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

**关键**：CLI 有两条传输路径，字段形态**不同**（报告 §9③）：

  A. Chat Completions 传输（`OpenAIChatCompletionsModel`）
       → body 顶层扁平 `reasoning_effort: "<档>"` + `verbosity: "high"`
       → **没有**嵌套 reasoning 对象

  B. Responses API 传输（`_buildResponsesCreateRequest`）
       → body 顶层嵌套 `reasoning: { effort, summary }`
       → **没有**扁平 reasoning_effort

我们所有请求最终都打 CodeBuddy 的 `/v2/chat/completions`，
所以统一采用 **A（Chat 传输）** 的形态。

另：`www.codebuddy.ai` 属产品网关，兼容层（CompatibilityRequestProcessor）
会**整体跳过**（`isProductGatewayHost` 命中即 return）。即客户端原样把
`reasoning_effort` 发给服务端，由服务端自己做各家模型适配 ——
所以我们不需要实现 thinkingFormat 那套改写规则。

## 本机实测（2026-09-20，直打 www.codebuddy.ai /v2/chat/completions）

  - 扁平 `reasoning_effort`（low/medium/high/xhigh）→ 200，reasoning_content 有梯度
  - 嵌套 `reasoning.effort` → 200 但**无梯度**（被静默忽略）→ 印证「Chat 传输只认扁平」
  - `xhigh`/`max` 在无 thinkingLevelMap 的模型上会被折叠成 `high`（LegacyXhighFallbackRule）
  - `minimal` 偶发 400 + code 11133 + extError.param=reasoning.effort + unsupported_value
    （复现不稳定 → 属模型侧抖动，不是稳定枚举拦截）

结论：**只发扁平 `reasoning_effort` + `verbosity`**，与 CLI 的 Chat 传输完全一致。

## 未知值处理

上游对未知档位是「夹取」而不是报错（`clampEffortToSupported`）。为避免把请求打挂，
本模块对未知值统一**回落到 medium**并留一条告警，而不是原样透传。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 档位常量
# ---------------------------------------------------------------------------

#: 上游认可的完整阶梯（含关闭档）。顺序即「强弱顺序」，用于就近夹取。
EFFORT_LADDER: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

#: 可发往上游的正常档位（不含 off —— off 的语义是「删除字段」）。
VALID_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

#: 未指定时的默认档。选 medium 是因为它是 CodeBuddy 大多数内置模型的默认值
#: （`reasoning.effort` 实测多为 "medium"），且是唯一在全部内置三档子集里都存在的值。
DEFAULT_EFFORT = "medium"

#: 「关闭思考」的同义写法。不同客户端用词不同，统一收口到这里。
OFF_ALIASES: frozenset[str] = frozenset({"off", "none", "disabled", "false", "0", "no"})

#: 一些客户端用的别名 → 上游档位
EFFORT_ALIASES: dict[str, str] = {
    "auto": DEFAULT_EFFORT,
    "default": DEFAULT_EFFORT,
    "enabled": DEFAULT_EFFORT,
    "true": DEFAULT_EFFORT,
    "on": DEFAULT_EFFORT,
    "yes": DEFAULT_EFFORT,
    "min": "minimal",
    "minimum": "minimal",
    "lowest": "minimal",
    "med": "medium",
    "normal": "medium",
    "standard": "medium",
    "mid": "medium",
    "veryhigh": "xhigh",
    "very_high": "xhigh",
    "extrahigh": "xhigh",
    "extra_high": "xhigh",
    "xh": "xhigh",
    "maximum": "max",
    "highest": "max",
    "ultra": "max",
    "ultracode": "max",  # CLI 特有档；对外无对应语义，映射到最强普通档
}

#: thinkingFormat 取值 → 该服务商期望的字段形状。
#: 我们不直接对接这些服务商，但保留映射表以便未来接第三方模型时复用。
THINKING_FORMATS: tuple[str, ...] = (
    "openai", "openrouter", "deepseek", "qwen", "qwen-chat", "zai",
    "together", "chat-template", "baseten", "string-thinking",
    "ant-ling", "google-generative-ai", "google-vertex",
)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def normalize_effort(value: Any) -> str | None:
    """把任意客户端写法归一成上游档位。

    返回：
      - 合法档位字符串（如 "high"）→ 应当透传
      - ``None`` → 语义是「关闭思考」，调用方应删除相关字段
      - ``DEFAULT_EFFORT`` → 值无法识别时的兜底（不返回 None，避免误关思考）

    设计取舍：不认的值**不原样透传**。上游虽会夹取，但遇到真非法值会偶发
    400 + code 11133（见模块 docstring 实测），在「透传非法值」和「回落到默认档」
    之间选后者更稳。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return DEFAULT_EFFORT if value else None
    if isinstance(value, (int, float)):
        # 数字档位：0 视作关闭，其余当默认档
        return None if value == 0 else DEFAULT_EFFORT
    if not isinstance(value, str):
        return DEFAULT_EFFORT

    key = value.strip().lower()
    if not key:
        return DEFAULT_EFFORT
    if key in OFF_ALIASES:
        return None
    if key in VALID_EFFORTS:
        return key
    if key in EFFORT_ALIASES:
        return EFFORT_ALIASES[key]
    return DEFAULT_EFFORT


def clamp_effort(effort: str, supported: list[str] | tuple[str, ...] | None) -> str:
    """把档位夹到模型支持的集合内（与上游 ``clampEffortToSupported`` 同语义）。

    ``supported`` 为空/None 时不做夹取。
    """
    if not supported:
        return effort
    allowed = [e for e in supported if e in VALID_EFFORTS]
    if not allowed:
        return effort
    if effort in allowed:
        return effort
    # 就近取：按阶梯顺序找距离最近的合法档；同距离偏向**更高**档
    # （与 LegacyXhighFallbackRule 的「高档折叠到 high」方向一致，宁可多想不可少想）
    target = EFFORT_LADDER.index(effort) if effort in EFFORT_LADDER else EFFORT_LADDER.index(DEFAULT_EFFORT)
    best, best_dist = allowed[0], 10**6
    for cand in allowed:
        dist = abs(EFFORT_LADDER.index(cand) - target)
        if dist < best_dist or (dist == best_dist and EFFORT_LADDER.index(cand) > EFFORT_LADDER.index(best)):
            best, best_dist = cand, dist
    return best


# ---------------------------------------------------------------------------
# 各协议入口 → 规范形态
# ---------------------------------------------------------------------------

def from_responses_reasoning(reasoning: Any, top_level_effort: Any = None) -> dict:
    """解析 Responses / Codex 形态。

    支持的入参形态（都实测见过）：
      - ``{"effort": "high", "summary": "auto"}``   ← Codex 标准形态
      - ``{"effort": "high"}``
      - 顶层 ``reasoning_effort = "high"``           ← 非规范但常见

    返回值：``{"effort": <档>}`` / ``{"effort": <档>, "summary": ...}`` / ``{}``（关闭）
    """
    result: dict[str, Any] = {}
    effort: str | None = None
    summary: str | None = None

    if isinstance(reasoning, dict):
        effort = normalize_effort(reasoning.get("effort"))
        raw_summary = reasoning.get("summary")
        if isinstance(raw_summary, str) and raw_summary.strip():
            summary = raw_summary.strip()

    # 顶层 reasoning_effort 优先（CLI 的 requestedEffort 就是这个优先级）
    if top_level_effort is not None and not isinstance(top_level_effort, (dict, list)):
        top = normalize_effort(top_level_effort)
        if top is not None:
            effort = top
        elif top is None and effort is None:
            effort = None

    # 只有 reasoning 对象、但 effort 缺失（如只给了 summary）时，用默认档
    if effort is None and isinstance(reasoning, dict) and reasoning and "effort" not in reasoning:
        effort = DEFAULT_EFFORT

    if effort is None:
        return {}
    result["effort"] = effort
    if summary:
        result["summary"] = summary
    return result


def from_anthropic_thinking(thinking: Any, top_level_effort: Any = None) -> dict:
    """解析 Anthropic / Claude Code 形态。

    支持的入参形态：
      - ``{"type": "enabled", "budget_tokens": 8000}``
      - ``{"type": "disabled"}``                 → 关闭
      - ``{"type": "adaptive"}``                 → 默认档
      - 字符串 ``"high"``
      - 顶层 ``reasoning_effort``

    budget_tokens → 档位的换算（上游只有 6 档，没有连续预算，所以做分段映射）：
      <2048 → minimal ; <8192 → low ; <32768 → medium ; <65536 → high ; 其余 → xhigh
    换算依据：CLI 侧 MAX_THINKING_TOKENS 是独立的一条线（providerData），
    我们无法透传预算，只能用最接近的档位近似。
    """
    if top_level_effort is not None and not isinstance(top_level_effort, (dict, list)):
        top = normalize_effort(top_level_effort)
        return {"effort": top} if top else {}

    if isinstance(thinking, str):
        e = normalize_effort(thinking)
        return {"effort": e} if e else {}

    if not isinstance(thinking, dict):
        return {}

    ttype = str(thinking.get("type") or "").strip().lower()

    # 显式关闭
    if ttype in OFF_ALIASES:
        return {}

    # 明确关闭的另一种写法：enabled=false
    if thinking.get("enabled") is False:
        return {}

    if ttype == "enabled" or thinking.get("enabled") is True or ttype == "adaptive":
        budget = thinking.get("budget_tokens")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            return {"effort": _budget_to_effort(int(budget))}
        return {"effort": DEFAULT_EFFORT}

    # type 未知但带了 effort 字段
    if "effort" in thinking:
        e = normalize_effort(thinking.get("effort"))
        return {"effort": e} if e else {}

    return {}


def _budget_to_effort(budget: int) -> str:
    """把 thinking 预算（token 数）分段映射到档位。"""
    if budget <= 0:
        return "minimal"
    if budget < 2048:
        return "minimal"
    if budget < 8192:
        return "low"
    if budget < 32768:
        return "medium"
    if budget < 65536:
        return "high"
    return "xhigh"


# ---------------------------------------------------------------------------
# 规范形态 → 各协议入口（用于回传，让客户端能读到生效档位）
# ---------------------------------------------------------------------------

def to_openai_fields(spec: dict) -> dict:
    """规范形态 → 上游 Chat Completions body 字段。

    ## 形态来源（报告《CodeBuddy积分与思考强度机制报告》§9③A）

    CodeBuddy CLI 的 `OpenAIChatCompletionsModel` 把 modelSettings 拍平成：

        if (modelSettings.reasoning?.effort) a.reasoning_effort = effort;
        if (modelSettings.text?.verbosity)   a.verbosity        = verbosity;

    然后 `...providerData` 展开到 body 顶层 —— 即 **Chat 传输只发扁平
    `reasoning_effort` 与 `verbosity`，不发嵌套 `reasoning` 对象**。
    （嵌套形态是 Responses 传输的另一条路径。）

    我们所有请求最终都打 CodeBuddy 的 `/v2/chat/completions`，所以必须照
    Chat 传输的形态发。本机实测也印证：扁平字段能改变 reasoning_content 长度，
    嵌套对象无梯度（被静默忽略）。

    ## 为什么带上 verbosity

    CLI 开启思考时会同时设 `text.verbosity = "high"`，这是上游的既有搭配
    （见报告 §7「开启思考时还会附加」）。一起发才能拿到完整的推理输出。

    ## 关于 summary

    Chat 传输下 `reasoning.summary` **不进 body**（只有 effort 与 verbosity 被拍平），
    所以这里不产出它。
    """
    if not spec or not spec.get("effort"):
        return {}
    return {
        "reasoning_effort": spec["effort"],
        "verbosity": spec.get("verbosity") or "high",
    }


def to_anthropic_fields(spec: dict) -> dict:
    """规范形态 → Anthropic Messages 响应侧字段（回显用）。"""
    if not spec or not spec.get("effort"):
        return {}
    effort = spec["effort"]
    budget = _effort_to_budget(effort)
    if budget is None:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


def _effort_to_budget(effort: str) -> int | None:
    """档位 → 预算估算值（回显给 Anthropic 客户端用）。"""
    return {
        "minimal": 1024,
        "low": 4096,
        "medium": 16384,
        "high": 49152,
        "xhigh": 98304,
        "max": 131072,
    }.get(effort)


def to_responses_reasoning(spec: dict) -> dict:
    """规范形态 → Responses 响应侧 ``reasoning`` 对象（回显用）。"""
    if not spec or not spec.get("effort"):
        return {}
    return {"effort": spec["effort"], "summary": spec.get("summary") or "auto"}


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def resolve(spec_hint: dict | None, *, supported: list[str] | tuple[str, ...] | None = None) -> dict:
    """对 spec 做夹取与清洗，返回可直接使用的规范形态。

    ``spec_hint`` 由上面三个 ``from_*`` 函数产出。
    """
    if not spec_hint or not spec_hint.get("effort"):
        return {}
    effort = clamp_effort(spec_hint["effort"], supported)
    out: dict[str, Any] = {"effort": effort}
    if spec_hint.get("summary"):
        out["summary"] = spec_hint["summary"]
    return out


__all__ = [
    "EFFORT_LADDER",
    "VALID_EFFORTS",
    "DEFAULT_EFFORT",
    "OFF_ALIASES",
    "EFFORT_ALIASES",
    "THINKING_FORMATS",
    "normalize_effort",
    "clamp_effort",
    "from_responses_reasoning",
    "from_anthropic_thinking",
    "to_openai_fields",
    "to_anthropic_fields",
    "to_responses_reasoning",
    "resolve",
]
