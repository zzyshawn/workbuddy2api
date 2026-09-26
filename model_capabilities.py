"""model_capabilities.py — 模型能力表（上下文空间 + 思考强度）。

## 为什么单独做这个模块

「上下文窗口」和「思考强度」这两件事，上游 `/v3/config` 给了权威数据，但
**不能照抄字段名直接用**：

  - 窗口：`maxInputTokens` / `maxAllowedSize` 都不是模型实际跑的窗口。
    真正生效的是 `contextWindow.defaultLength`（多档模型）或 `maxInputTokens`。
    照抄会让客户端以为有 1M 上下文，实际 400K 就触发压缩。
    解析优先级见 `model_registry.resolve_effective_context_window`（对齐上游
    `resolveEffectiveContextBudget`，报告 §13）。

  - 思考强度：上游只有 6 档（minimal/low/medium/high/xhigh/max），且**没有**连续预算。
    Claude Code 的 `budget_tokens`、Codex 的 `reasoning.effort` 都要映射到这 6 档。
    档位与传输形态的完整契约见 `reasoning.py`。

本模块把两者合成**一张可直接消费的表**，供三处复用：

  1. 运行中的服务 → 落盘 `logs/model_capabilities.json`（随模型刷新更新）
  2. `sync_codex_models.py` → 生成 Codex 的 `models.json` 时取窗口与档位
  3. 人 → `render_markdown()` 输出可读表格

**零依赖**：只用标准库，能在 NAS 容器里直接跑（与 `state_snapshot.py` 同策略）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

#: 上游认可的思考档位阶梯（含关闭档），顺序即强弱顺序。
#: 与 reasoning.EFFORT_LADDER 同源，这里重复声明是为了让本模块能独立 import
#: （NAS 上按单文件分发时不必拖 reasoning.py 的依赖链）。
EFFORT_LADDER: tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max",
)

#: 各协议入口 → 上游扁平字段的对应关系（写进文件，供人查）
EFFORT_FIELD_MAP: dict[str, str] = {
    "chat_completions": "reasoning_effort (扁平) + verbosity",
    "responses": "reasoning_effort (扁平) + verbosity  ← 由 responses_adapter 归一",
    "anthropic_messages": "reasoning_effort (扁平) + verbosity  ← 由 anthropic_adapter 归一",
}

#: Anthropic thinking.budget_tokens → 档位的分段映射（写进文件，供人查）
BUDGET_TO_EFFORT: tuple[tuple[int, str], ...] = (
    (2048, "minimal"),
    (8192, "low"),
    (32768, "medium"),
    (65536, "high"),
    (2**63, "xhigh"),
)


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def _model_row(m: dict) -> dict:
    """把上游一个模型对象整理成能力表的一行。"""
    from model_registry import (
        normalize_supported_context_windows,
        resolve_effective_context_window,
    )

    m = m or {}
    mid = str(m.get("id") or "")
    mip = m.get("maxInputTokens")
    mip_int = int(mip) if isinstance(mip, int) and mip > 0 else 0
    cw = m.get("contextWindow") if isinstance(m.get("contextWindow"), dict) else {}
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else {}

    tiers = normalize_supported_context_windows(cw.get("supportedLengths"), mip_int or None)
    effective = resolve_effective_context_window(
        context_window=cw, max_input_tokens=mip_int or None
    )

    efforts = [str(e) for e in (reasoning.get("supportedEfforts") or []) if e]
    default_effort = reasoning.get("defaultEffort") or reasoning.get("effort") or ""

    return {
        "id": mid,
        "name": str(m.get("name") or ""),
        "credits": str(m.get("credits") or ""),
        # ---- 上下文空间 ----
        "context_window": effective,          # ← 真正生效的预算
        "context_tiers": tiers,               # 多档窗口（空 = 单档）
        "default_length": int(cw.get("defaultLength") or 0),
        "max_input_tokens": mip_int,
        "max_output_tokens": int(m.get("maxOutputTokens") or 0),
        "max_allowed_size": int(m.get("maxAllowedSize") or 0),
        # ---- 思考强度 ----
        "supports_reasoning": bool(m.get("supportsReasoning")),
        "only_reasoning": bool(m.get("onlyReasoning")),
        "default_effort": str(default_effort),
        "supported_efforts": efforts,
        # ---- 其它 ----
        "supports_images": bool(m.get("supportsImages")),
        "supports_tool_call": bool(m.get("supportsToolCall")),
    }


def _registry_row(m: dict) -> dict:
    """把 `ModelInfo.to_dict()` 的一行整理成能力表的一行。

    与 `_model_row` 的区别：入参已经是**归一后**的字段名（`context_length` /
    `context_tiers` / `reasoning_efforts` …），不需要再解析上游原始结构。

    为什么运行中的服务走这条而不是 `_model_row`：`resolve_models()` 的结果
    本身就是 `ModelInfo.to_dict()`，再回头去调一次 `/v3/config` 只为拿原始字段，
    既慢又会在上游抖动时把能力表写坏（还会和模型缓存互相触发）。
    两种入口共用同一套窗口/档位解析（都来自 `model_registry`），不会漂移。
    """
    m = m or {}
    tiers = [int(v) for v in (m.get("context_tiers") or []) if isinstance(v, int)]
    efforts = [str(e) for e in (m.get("reasoning_efforts") or []) if e]
    return {
        "id": str(m.get("id") or ""),
        "name": str(m.get("name") or ""),
        "credits": str(m.get("credits") or ""),
        # ---- 上下文空间 ----
        "context_window": int(m.get("context_length") or 0),
        "context_tiers": tiers,
        "default_length": 0,  # 归一后不保留；多档时生效档已体现在 context_window
        "max_input_tokens": 0,
        "max_output_tokens": int(m.get("max_tokens") or 0),
        "max_allowed_size": int(m.get("max_allowed_size") or 0),
        # ---- 思考强度 ----
        "supports_reasoning": bool(m.get("supports_reasoning")),
        "only_reasoning": bool(m.get("only_reasoning")),
        "default_effort": str(m.get("default_effort") or ""),
        "supported_efforts": efforts,
        # ---- 其它 ----
        "supports_images": bool(m.get("supports_images")),
        "supports_tool_call": bool(m.get("supports_tool_call")),
    }


def build_from_registry(
    items: list[dict], *, env: str | None = None, source: str = ""
) -> dict:
    """从 `resolve_models()` 的结果（`ModelInfo.to_dict()` 列表）构造能力表。

    运行中的服务走这条：**零额外上游请求**，任何来源（upstream / cache /
    stale-snapshot / static）都能生成，回退时也有表可看。
    """
    rows = [_registry_row(m) for m in (items or []) if isinstance(m, dict)]
    return {
        "generated_at": int(time.time()),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": env or "",
        "source": f"service resolve_models ({source})" if source else "service resolve_models",
        "whitelist_count": len(rows),
        "model_count": len(rows),
        "contract": _contract(),
        "models": rows,
    }


def _contract() -> dict:
    """把「契约说明」抽出来 —— 两张表（上游原始 / 服务归一）共用同一份说明。"""
    return {
        "context_window_rule": (
            "生效窗口 = contextWindow.defaultLength > min(contextWindow.supportedLengths) "
            "> maxInputTokens（对齐上游 resolveEffectiveContextBudget）。"
            "maxAllowedSize 是校验上限，不是窗口。"
        ),
        "effort_ladder": list(EFFORT_LADDER),
        "effort_field_map": EFFORT_FIELD_MAP,
        "budget_to_effort": [
            {"budget_tokens_lt": b if b < 2**63 else None, "effort": e}
            for b, e in BUDGET_TO_EFFORT
        ],
        "upstream_notes": [
            "上游 Chat 传输只认扁平 reasoning_effort + verbosity，嵌套 reasoning 对象会被忽略",
            "www.codebuddy.ai 属产品网关，兼容改写层被跳过，字段原样送达服务端",
            "xhigh/max 在无 thinkingLevelMap 的模型上会被上游折叠为 high",
        ],
    }


def build_table(data: dict, *, env: str | None = None) -> dict:
    """从 `/v3/config` 的 `data` 构造能力表。

    只收 `agents[name=="cli"]` 白名单里的模型 —— 与对外可用清单同口径
    （`models[]` 里有些模型没有 cli 入口，列进来会误导）。
    """
    models = [m for m in (data.get("models") or []) if isinstance(m, dict)]
    index = {m.get("id"): m for m in models}

    whitelist: list[str] = []
    for a in (data.get("agents") or []):
        if isinstance(a, dict) and a.get("name") == "cli":
            whitelist = [str(x) for x in (a.get("models") or [])]
            break

    rows: list[dict] = []
    for mid in (whitelist or [m.get("id") for m in models]):
        m = index.get(mid)
        if not m or m.get("disabled") is True:
            continue
        rows.append(_model_row({**m, "id": mid}))

    return {
        "generated_at": int(time.time()),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": env or "",
        "source": "upstream /v3/config",
        "whitelist_count": len(whitelist),
        "model_count": len(rows),
        # 把「契约」也写进文件 —— 这份表的价值一半在数据、一半在说明，
        # 脱离说明的裸数字（如 context_window=400000）会被误读成上游字段值。
        "contract": _contract(),
        "models": rows,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    """token 数格式化为人类可读（对齐上游 formatContextWindowOption）。"""
    if n <= 0:
        return "-"
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1_000 and n % 1_000 == 0:
        return f"{n // 1_000}K"
    return str(n)


def render_markdown(table: dict) -> str:
    """把能力表渲染成可读的 Markdown。"""
    lines: list[str] = []
    lines.append("# 模型能力表（上下文空间 + 思考强度）")
    lines.append("")
    lines.append(f"> 生成时间：{table.get('generated_at_str')}　环境：{table.get('env') or '-'}"
                 f"　来源：{table.get('source')}")
    lines.append("")

    c = table.get("contract") or {}
    lines.append("## 契约说明")
    lines.append("")
    lines.append(f"- **生效窗口**：{c.get('context_window_rule', '')}")
    lines.append(f"- **思考档位**：{' / '.join(c.get('effort_ladder') or [])}")
    lines.append("")
    lines.append("**各协议入口 → 上游字段**：")
    lines.append("")
    lines.append("| 协议入口 | 发往上游的字段 |")
    lines.append("|---|---|")
    for k, v in (c.get("effort_field_map") or {}).items():
        lines.append(f"| `{k}` | {v} |")
    lines.append("")
    lines.append("**`thinking.budget_tokens` → 档位**：")
    lines.append("")
    lines.append("| 预算上限 | 映射档位 |")
    lines.append("|---|---|")
    prev = 0
    for item in (c.get("budget_to_effort") or []):
        hi = item.get("budget_tokens_lt")
        rng = f"≥ {prev}" if hi is None else f"{prev} ~ {hi}"
        lines.append(f"| {rng} | `{item.get('effort')}` |")
        prev = hi if hi is not None else prev
    lines.append("")
    lines.append("**上游注意事项**：")
    lines.append("")
    for n in (c.get("upstream_notes") or []):
        lines.append(f"- {n}")
    lines.append("")

    lines.append(f"## 模型清单（{table.get('model_count')} 个）")
    lines.append("")
    lines.append("| 模型 | 名称 | 生效窗口 | 窗口档位 | 输入上限 | 输出上限 | 思考 | 默认档 | 可用档位 | 倍率 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for m in table.get("models") or []:
        tiers = m.get("context_tiers") or []
        tier_s = " / ".join(_fmt_tokens(t) for t in tiers) if len(tiers) > 1 else "-"
        efforts = m.get("supported_efforts") or []
        eff_s = " / ".join(efforts) if efforts else ("(全部)" if m.get("supports_reasoning") else "-")
        think = "✅" if m.get("supports_reasoning") else "-"
        if m.get("only_reasoning"):
            think += "(强制)"
        lines.append(
            f"| `{m.get('id')}` | {m.get('name') or '-'} | "
            f"{_fmt_tokens(m.get('context_window') or 0)} | {tier_s} | "
            f"{_fmt_tokens(m.get('max_input_tokens') or 0)} | "
            f"{_fmt_tokens(m.get('max_output_tokens') or 0)} | {think} | "
            f"`{m.get('default_effort') or '-'}` | {eff_s} | {m.get('credits') or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def default_path(log_path: str | Path | None = None) -> Path:
    """能力表默认路径：跟日志同目录（`logs/` 已挂载，不必新增 volume）。

    与 `state_snapshot.snapshot_path` 同一策略 —— 推导不能只看 `is_dir()`，
    首次启动时日志文件还不存在会被误判成目录。
    """
    if log_path:
        p = Path(log_path)
        base = p.parent if p.suffix else p
        return base / "model_capabilities.json"
    return Path("model_capabilities.json")


def write_table(table: dict, path: str | Path) -> Path:
    """原子写 JSON + 同名 Markdown。

    原子写（临时文件 + os.replace）：这份文件可能被正在运行的进程读，
    写一半会解析失败。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)

    md = p.with_suffix(".md")
    tmp_md = md.with_name(md.name + ".tmp")
    tmp_md.write_text(render_markdown(table), encoding="utf-8")
    os.replace(tmp_md, md)
    return p


__all__ = [
    "EFFORT_LADDER",
    "EFFORT_FIELD_MAP",
    "BUDGET_TO_EFFORT",
    "build_table",
    "build_from_registry",
    "render_markdown",
    "default_path",
    "write_table",
]
