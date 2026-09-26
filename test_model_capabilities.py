"""test_model_capabilities.py — 模型能力表与思考强度归一化的测试。

覆盖：
  1. resolve_effective_context_window 的多档窗口优先级（报告 §13 契约）
  2. build_table（上游原始形态）与 build_from_registry（服务归一形态）两张表
  3. render_markdown / write_table 落盘
  4. reasoning 模块：别名归一、档位夹取、budget_tokens 分段映射、
     Chat 传输只发扁平字段（嵌套对象会被上游忽略）
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

import model_capabilities as mc
import model_registry as mr
import reasoning as rs


def test_effective_context_window():
    # 多档窗口：defaultLength 命中支持列表 → 生效的是 400K 而不是 maxInput 1M
    cw = {"supportedLengths": [1000000, 400000], "defaultLength": 400000}
    got = mr.resolve_effective_context_window(
        context_window=cw, max_input_tokens=1000000
    )
    assert got == 400000, f"多档默认应取 defaultLength，got {got}"

    # 多档但没给 defaultLength → 取最小档
    got = mr.resolve_effective_context_window(
        context_window={"supportedLengths": [1000000, 400000]}, max_input_tokens=1000000
    )
    assert got == 400000

    # 单档窗口 → 退回 maxInputTokens
    got = mr.resolve_effective_context_window(
        context_window={"supportedLengths": [256000]}, max_input_tokens=256000
    )
    assert got == 256000

    # 支持列表里的项不允许超过 maxInputTokens（上游 normalizeSupportedContextWindows 语义）
    got = mr.resolve_effective_context_window(
        context_window={"supportedLengths": [2000000], "defaultLength": 2000000},
        max_input_tokens=1000000,
    )
    assert got == 1000000, f"超限档位应被过滤，退回 maxInputTokens，got {got}"
    print("✅ effective_context_window")


def _raw_config() -> dict:
    """构造一份 /v3/config 形态的最小数据。"""
    return {
        "code": 0,
        "data": {
            "models": [
                {
                    "id": "gpt-5.5",
                    "name": "GPT-5.5",
                    "maxInputTokens": 1000000,
                    "maxOutputTokens": 128000,
                    "maxAllowedSize": 1000000,
                    "supportsReasoning": True,
                    "supportsImages": True,
                    "supportsToolCall": True,
                    "credits": "1",
                    "reasoning": {
                        "supportedEfforts": ["low", "medium", "high"],
                        "defaultEffort": "medium",
                    },
                    "contextWindow": {
                        "supportedLengths": [1000000, 400000],
                        "defaultLength": 400000,
                    },
                },
                {
                    "id": "kimi-k2.6",
                    "name": "Kimi K2.6",
                    "maxInputTokens": 256000,
                    "maxOutputTokens": 32000,
                    "supportsReasoning": False,
                    "reasoning": {},
                    "contextWindow": {"supportedLengths": [256000]},
                },
                {"id": "not-for-cli", "name": "内部模型"},
            ],
            "agents": [{"name": "cli", "models": ["gpt-5.5", "kimi-k2.6"]}],
        },
    }


def test_build_table_from_raw():
    table = mc.build_table(_raw_config()["data"], env="intl")
    assert table["model_count"] == 2, "只收 cli 白名单内的模型"
    ids = [m["id"] for m in table["models"]]
    assert ids == ["gpt-5.5", "kimi-k2.6"]

    gpt = table["models"][0]
    assert gpt["context_window"] == 400000, "生效窗口应是 defaultLength 而非 maxInputTokens"
    assert gpt["context_tiers"] == [400000, 1000000]
    assert gpt["supported_efforts"] == ["low", "medium", "high"]
    assert gpt["default_effort"] == "medium"
    assert gpt["supports_reasoning"] is True
    assert gpt["supports_images"] is True
    kimi = table["models"][1]
    assert kimi["context_window"] == 256000
    assert kimi["supports_reasoning"] is False
    assert kimi["supported_efforts"] == []
    # 契约说明必须随表落盘
    assert table["contract"]["effort_ladder"][0] == "off"
    print("✅ build_table_from_raw")


def test_build_from_registry():
    """服务归一形态：ModelInfo.to_dict() → build_from_registry。"""
    infos = mr.parse_models_payload(_raw_config())
    items = [i.to_dict() for i in infos]
    table = mc.build_from_registry(items, env="intl", source="upstream")
    gpt = next(m for m in table["models"] if m["id"] == "gpt-5.5")
    assert gpt["context_window"] == 400000
    assert gpt["context_tiers"] == [400000, 1000000]
    assert gpt["supported_efforts"] == ["low", "medium", "high"]
    assert gpt["max_output_tokens"] == 128000
    assert gpt["supports_images"] is True
    kimi = next(m for m in table["models"] if m["id"] == "kimi-k2.6")
    assert kimi["context_window"] == 256000
    assert kimi["supports_reasoning"] is False
    print("✅ build_from_registry")


def test_render_and_write():
    table = mc.build_table(_raw_config()["data"], env="intl")
    md = mc.render_markdown(table)
    assert "模型能力表" in md and "400K" in md, "表格应包含格式化后的窗口值"
    assert "`gpt-5.5`" in md

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "logs" / "model_capabilities.json"
        out = mc.write_table(table, p)
        assert out == p and p.exists()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["model_count"] == 2
        md_file = p.with_suffix(".md")
        assert md_file.exists() and "gpt-5.5" in md_file.read_text(encoding="utf-8")
        # 路径推导：跟日志同目录；后缀判断要兼容「日志文件还不存在」的场景
        assert mc.default_path(Path(td) / "logs" / "converter.log") == Path(td) / "logs" / "model_capabilities.json"
        assert mc.default_path(Path(td) / "logs").name == "model_capabilities.json"
    print("✅ render_and_write")


def test_reasoning_normalize():
    assert rs.normalize_effort("high") == "high"
    assert rs.normalize_effort("High") == "high"
    assert rs.normalize_effort("min") == "minimal"
    assert rs.normalize_effort("very_high") == "xhigh"
    assert rs.normalize_effort("ultra") == "max"
    assert rs.normalize_effort("auto") == "medium"  # 别名 → 默认档
    assert rs.normalize_effort("unknown-garbage") == "medium"  # 不透传非法值
    assert rs.normalize_effort("off") is None
    assert rs.normalize_effort("none") is None
    assert rs.normalize_effort(0) is None
    assert rs.normalize_effort(False) is None
    assert rs.normalize_effort(True) == "medium"
    print("✅ reasoning_normalize")


def test_reasoning_clamp():
    # 模型只支持 low/medium/high：xhigh 就近夹到 high（同距离偏高档）
    assert rs.clamp_effort("xhigh", ["low", "medium", "high"]) == "high"
    assert rs.clamp_effort("minimal", ["low", "medium", "high"]) == "low"
    # 支持集合为空 → 不夹取
    assert rs.clamp_effort("xhigh", []) == "xhigh"
    assert rs.clamp_effort("xhigh", None) == "xhigh"
    # 已在支持集合内 → 原样
    assert rs.clamp_effort("low", ["low", "medium", "high"]) == "low"
    print("✅ reasoning_clamp")


def test_reasoning_from_anthropic():
    # budget_tokens 分段映射
    assert rs.from_anthropic_thinking({"type": "enabled", "budget_tokens": 1024}) == {"effort": "minimal"}
    assert rs.from_anthropic_thinking({"type": "enabled", "budget_tokens": 4096}) == {"effort": "low"}
    assert rs.from_anthropic_thinking({"type": "enabled", "budget_tokens": 16384}) == {"effort": "medium"}
    assert rs.from_anthropic_thinking({"type": "enabled", "budget_tokens": 49152}) == {"effort": "high"}
    assert rs.from_anthropic_thinking({"type": "enabled", "budget_tokens": 100000}) == {"effort": "xhigh"}
    # 显式关闭
    assert rs.from_anthropic_thinking({"type": "disabled"}) == {}
    assert rs.from_anthropic_thinking({"type": "enabled", "enabled": False}) == {}
    # 开启但没给预算 → 默认档
    assert rs.from_anthropic_thinking({"type": "enabled"}) == {"effort": "medium"}
    # 顶层 effort 优先
    assert rs.from_anthropic_thinking({"type": "disabled"}, "high") == {"effort": "high"}
    print("✅ reasoning_from_anthropic")


def test_reasoning_chat_transport_shape():
    """Chat 传输只发扁平 reasoning_effort + verbosity（嵌套对象会被上游忽略）。"""
    fields = rs.to_openai_fields({"effort": "high", "summary": "auto"})
    assert fields == {"reasoning_effort": "high", "verbosity": "high"}, fields
    assert "reasoning" not in fields, "Chat 传输禁止发嵌套 reasoning 对象"
    assert "text" not in fields, "verbosity 应是顶层扁平字段"

    # 关闭语义：空 spec → 不发任何字段
    assert rs.to_openai_fields({}) == {}
    assert rs.to_openai_fields({"effort": None}) == {}

    # Responses 回显形态保留嵌套（那是响应侧，不是上游请求）
    echo = rs.to_responses_reasoning({"effort": "low"})
    assert echo == {"effort": "low", "summary": "auto"}
    print("✅ reasoning_chat_transport_shape")


def test_reasoning_from_responses():
    assert rs.from_responses_reasoning({"effort": "high", "summary": "auto"}) == {
        "effort": "high", "summary": "auto",
    }
    # 顶层扁平优先
    assert rs.from_responses_reasoning({"effort": "low"}, "high") == {"effort": "high"}
    # 只给 reasoning 对象没给 effort → 默认档（summary 保留）
    assert rs.from_responses_reasoning({"summary": "auto"}) == {"effort": "medium", "summary": "auto"}
    # 关闭
    assert rs.from_responses_reasoning({"effort": "off"}) == {}
    assert rs.from_responses_reasoning(None, "off") == {}
    print("✅ reasoning_from_responses")


if __name__ == "__main__":
    test_effective_context_window()
    test_build_table_from_raw()
    test_build_from_registry()
    test_render_and_write()
    test_reasoning_normalize()
    test_reasoning_clamp()
    test_reasoning_from_anthropic()
    test_reasoning_chat_transport_shape()
    test_reasoning_from_responses()
    print("✅ test_model_capabilities")
