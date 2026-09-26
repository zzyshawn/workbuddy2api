#!/usr/bin/env python3
"""test_chat_system_prompt.py — 三条协议入口都必须保证「首条是 system prompt」。

背景（为什么值得单独一个测试文件）：
    国际版（`www.codebuddy.ai`）**硬性要求** chat 请求的 messages 首条是 system，
    否则直接：

        400 + code 11128 "first message is not system prompt"

    这个错误最坑的地方是**报错文案完全误导**。响应体里的 displayMsg 是：

        "The request was blocked by security policy."
        "请求被安全策略拦截，请稍后重试或联系支持。"

    看起来像敏感词 / 内容审核问题，于是会去查脱敏、查 system 内容、查工具描述，
    全都白费 —— 真因只是「首条不是 system」。实测最朴素的
    `[{"role":"user","content":"只回两个字：正常"}]` 就必然触发。

三条入口曾经覆盖不全：
    /v1/responses  → responses_projection._ensure_leading_system   ✅
    /v1/messages   → anthropic_adapter._ensure_leading_system      ✅
    /v1/chat/completions → （无兜底）                              ❌
    OpenAI SDK 和多数桌面客户端走的正是最后这条，所以它才最该被测。

这三份实现刻意不互相 import（协议入口各自独立，避免耦合），
因此本文件逐个直接测，防止哪一处后来又漏掉。

直接运行：python3 test_chat_system_prompt.py
"""

import sys

sys.path.insert(0, ".")

from anthropic_adapter import (  # noqa: E402
    FALLBACK_SYSTEM_PROMPT as ANTHROPIC_FALLBACK,
    anthropic_request_to_chat,
)
from converter import (  # noqa: E402
    FALLBACK_SYSTEM_PROMPT,
    _ensure_leading_system,
)
from responses_projection import project_responses_chat_body  # noqa: E402
from responses_adapter import responses_request_to_chat  # noqa: E402


def test_converter_ensures_leading_system():
    """直连 /v1/chat/completions 的核心逻辑：缺 system 时补，已有则不动。"""
    body = {"model": "hy3", "messages": [{"role": "user", "content": "你好"}]}
    changed = _ensure_leading_system(body)
    assert changed is True, "首条不是 system 时必须返回 True（表示补过）"
    assert body["messages"][0]["role"] == "system", "首条未变成 system"
    assert body["messages"][0]["content"] == FALLBACK_SYSTEM_PROMPT
    assert body["messages"][1] == {"role": "user", "content": "你好"}, "原消息被改动了"
    print("✅ test_converter_ensures_leading_system")


def test_converter_ensure_is_idempotent():
    """幂等：已经满足时不能再插一条（否则每轮请求都会堆 system）。"""
    body = {
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "你好"},
        ]
    }
    assert _ensure_leading_system(body) is False, "已满足时应返回 False"
    assert len(body["messages"]) == 2, f"消息数被改动：{len(body['messages'])}"
    assert body["messages"][0]["content"] == "Be concise.", "原有 system 被顶掉了"
    print("✅ test_converter_ensure_is_idempotent")


def test_converter_ignores_malformed_messages():
    """畸形输入不能崩 —— messages 缺失/非 list/空 时静默返回 False。"""
    assert _ensure_leading_system({}) is False
    assert _ensure_leading_system({"messages": None}) is False
    assert _ensure_leading_system({"messages": "not-a-list"}) is False
    assert _ensure_leading_system({"messages": []}) is False
    print("✅ test_converter_ignores_malformed_messages")


def test_responses_entry_ensures_leading_system():
    """Responses 入口：投影后首条必须是 system。"""
    req = {"model": "hy3", "input": [{"role": "user", "content": "你好"}]}
    chat = responses_request_to_chat(req)
    projected, stats = project_responses_chat_body(chat)
    assert projected["messages"][0]["role"] == "system", "Responses 分支首条不是 system"
    assert stats.get("system_prompt_ensured") is True, "统计里没有标记补过"
    print("✅ test_responses_entry_ensures_leading_system")


def test_anthropic_entry_ensures_leading_system():
    """Anthropic 入口：客户端完全不传 system 字段时也要补上。"""
    body = {
        "model": "hy3",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "你好"}],
    }
    chat = anthropic_request_to_chat(body)
    assert chat["messages"][0]["role"] == "system", "Anthropic 分支首条不是 system"
    assert chat["messages"][0]["content"] == ANTHROPIC_FALLBACK
    print("✅ test_anthropic_entry_ensures_leading_system")


def test_all_three_entrypoints_agree_on_contract():
    """三处兜底必须是同一份**契约**：首条都是 system。

    注意只比「角色」不比「内容」—— responses 投影有自己的 BASE_SYSTEM_PROMPT
    （面向 CLI/agent 场景，比通用兜底长得多），内容不同是设计使然，
    真正的契约是「首条为 system」这一条硬约束。
    """
    prompt = "你好"

    # 1) converter（直连 /v1/chat/completions）
    b1 = {"messages": [{"role": "user", "content": prompt}]}
    _ensure_leading_system(b1)
    head1 = b1["messages"][0]

    # 2) responses
    chat = responses_request_to_chat(
        {"model": "hy3", "input": [{"role": "user", "content": prompt}]}
    )
    proj, _ = project_responses_chat_body(chat)
    head2 = proj["messages"][0]

    # 3) anthropic
    chat3 = anthropic_request_to_chat(
        {"model": "hy3", "max_tokens": 32, "messages": [{"role": "user", "content": prompt}]}
    )
    head3 = chat3["messages"][0]

    for label, head in (("converter", head1), ("responses", head2), ("anthropic", head3)):
        assert head.get("role") == "system", f"{label} 分支首条不是 system：{head}"
        assert head.get("content"), f"{label} 分支补出的 system 内容为空"
    print("✅ test_all_three_entrypoints_agree_on_contract")


if __name__ == "__main__":
    test_converter_ensures_leading_system()
    test_converter_ensure_is_idempotent()
    test_converter_ignores_malformed_messages()
    test_responses_entry_ensures_leading_system()
    test_anthropic_entry_ensures_leading_system()
    test_all_three_entrypoints_agree_on_contract()
    print("\n🎉 All 6 tests passed!")
