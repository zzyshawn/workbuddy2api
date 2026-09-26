#!/usr/bin/env python3
"""test_sync_codex_models.py — Codex 模型注册表同步工具的纯函数测试。

背景（为什么要有这个工具）：
    Codex 的模型下拉**不是**从我们的 `/v1/models` 读的，而是读它自己的
    `~/.codex/models.json`。光在 `config.toml` 里写 `[profiles.x] model=...` 不够 ——
    模型不会出现在列表里，用户会以为"模型没加进去"（真实反馈）。
    而那条记录的 schema 有 39 个键（`base_instructions` / `model_messages` 各约 18K 字符，
    是 Codex 的 agent 提示词），没法手写，所以工具**以本机已有条目为模板克隆**。

    元数据取自上游 `GET /v3/config`（权威），**不照抄模板** —— 上下文窗口填大了
    会让 Codex 迟迟不触发自动压缩，最后撞上游 max_tokens 报错。

覆盖：
  1. parse_metas：按 cli 白名单过滤、字段裁剪、无 agents 时不过滤
  2. build_entry：覆盖哪些字段、不污染模板、多模态/窗口缺省的处理
  3. merge_models：新增 / 更新 / **不删除**已有条目 / 优先级续排
  4. pick_template：优先选本次要同步的条目；空列表时报错而不是乱造
  5. write_models：备份 + 原子写（无 .tmp 残留）+ 可被 json 读回

直接运行：python3 test_sync_codex_models.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import sync_codex_models as scm  # noqa: E402


def _tmpl() -> dict:
    """一份最小的"已有条目"替身（真实条目有 39 个键，这里只留会被用到的）。"""
    return {
        "slug": "old-model",
        "display_name": "Old",
        "description": "old desc",
        "context_window": 1048576,
        "max_context_window": 1048576,
        "effective_context_window_percent": 95,
        "input_modalities": ["text"],
        "supports_image_detail_original": True,
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "base_instructions": "X" * 200,
        "model_messages": {"instructions_template": "Y" * 200},
    }


# ---------------------------------------------------------------------------
# 1. parse_metas
# ---------------------------------------------------------------------------

def test_parse_metas_filters_by_cli_whitelist():
    """只留 cli 白名单里的模型 —— models[] 里有一部分拿不到 cli 入口，同步了也没用。"""
    data = {
        "models": [
            {"id": "a", "name": "A", "maxAllowedSize": 100},
            {"id": "b", "name": "B", "maxAllowedSize": 200},
            {"id": "sg-only", "name": "SG", "maxAllowedSize": 300},
        ],
        "agents": [{"name": "cli", "models": ["a", "b"]}],
    }
    metas, wl = scm.parse_metas(data)
    assert wl == ["a", "b"]
    assert [m["id"] for m in metas] == ["a", "b"], "应被白名单过滤掉 sg-only"
    print("✅ test_parse_metas_filters_by_cli_whitelist")


def test_parse_metas_keeps_fields_and_tolerates_missing_agents():
    """字段被裁到白名单集；没有 agents 段时不过滤（而非清空）。"""
    data = {
        "models": [{
            "id": "x", "name": "X", "descriptionZh": "中文", "maxAllowedSize": 1,
            "maxInputTokens": 2, "maxOutputTokens": 3, "supportsImages": True,
            "supportsToolCall": True, "isDefault": None, "temperature": 1,
            "vendor": "e", "someUnknownField": "应被丢掉",
        }],
    }
    metas, wl = scm.parse_metas(data)
    assert wl == []
    assert len(metas) == 1
    m = metas[0]
    assert "someUnknownField" not in m, "只保留关注字段"
    for k in ("id", "name", "descriptionZh", "maxAllowedSize", "supportsImages"):
        assert k in m, f"缺字段 {k}"
    print("✅ test_parse_metas_keeps_fields_and_tolerates_missing_agents")


# ---------------------------------------------------------------------------
# 2. build_entry
# ---------------------------------------------------------------------------

def test_build_entry_overrides_model_specific_fields():
    """覆盖 slug / 名称 / 描述 / 窗口 / 模态 / 优先级，其余继承模板。

    窗口用**有效预算**（contextWindow.defaultLength 优先），不是 maxAllowedSize
    —— 后者是上游校验上限，填它会让 Codex 以为有 1M 上下文、实际 400K 就压缩。
    """
    e = scm.build_entry(
        _tmpl(),
        {"id": "gpt-5.5", "name": "GPT-5.5", "descriptionZh": "旗舰编码模型",
         "maxInputTokens": 1000000, "maxAllowedSize": 1000000,
         "contextWindow": {"supportedLengths": [1000000, 400000],
                           "defaultLength": 400000},
         "supportsImages": True},
        7,
    )
    assert e["slug"] == "gpt-5.5"
    assert e["display_name"] == "GPT-5.5"
    assert e["description"] == "旗舰编码模型"
    assert e["context_window"] == 400000, "应取 defaultLength 而非 maxAllowedSize"
    assert e["max_context_window"] == 400000
    assert e["input_modalities"] == ["text", "image"]
    assert e["priority"] == 7
    assert e["visibility"] == "list" and e["supported_in_api"] is True
    # 模板里的长文本必须原样带过来（否则 Codex 拿不到 agent 提示词）
    assert e["base_instructions"] == "X" * 200
    assert e["model_messages"]["instructions_template"] == "Y" * 200
    print("✅ test_build_entry_overrides_model_specific_fields")


def test_build_entry_window_fallbacks():
    """窗口回退链：无 contextWindow 时退回 maxInputTokens；两者都无则不设窗口。"""
    e = scm.build_entry(_tmpl(), {"id": "m1", "maxInputTokens": 256000}, 8)
    assert e["context_window"] == 256000

    # 只有 maxAllowedSize（旧夹具形态）→ 不再据此填窗口（那是校验上限）
    e = scm.build_entry(_tmpl(), {"id": "m2", "maxAllowedSize": 1000000}, 9)
    assert "context_window" not in e or e["context_window"] != 1000000
    print("✅ test_build_entry_window_fallbacks")


def test_build_entry_does_not_mutate_template():
    """深拷贝 —— 否则第二条模型会继承第一条的字段。"""
    t = _tmpl()
    scm.build_entry(t, {"id": "a", "maxAllowedSize": 111}, 1)
    assert t["slug"] == "old-model" and t["context_window"] == 1048576, "模板被污染了"
    print("✅ test_build_entry_does_not_mutate_template")


def test_build_entry_text_only_model():
    """不支持图片的模型：模态只留 text，并关掉原图细节开关。"""
    e = scm.build_entry(_tmpl(), {"id": "t", "supportsImages": False}, 1)
    assert e["input_modalities"] == ["text"]
    assert e["supports_image_detail_original"] is False
    print("✅ test_build_entry_text_only_model")


def test_build_entry_window_fallback_and_missing():
    """窗口：优先 maxAllowedSize，退 maxInputTokens，都没有则保留模板值。"""
    assert scm.build_entry(_tmpl(), {"id": "a", "maxInputTokens": 555, "maxAllowedSize": None}, 1)[
        "context_window"
    ] == 555
    assert scm.build_entry(_tmpl(), {"id": "b"}, 1)["context_window"] == 1048576, (
        "上游没给窗口时应保留模板值，而不是写 None"
    )
    # 0 / 负数视为无效
    assert scm.build_entry(_tmpl(), {"id": "c", "maxAllowedSize": 0}, 1)[
        "context_window"
    ] == 1048576
    print("✅ test_build_entry_window_fallback_and_missing")


def test_build_entry_requires_id():
    try:
        scm.build_entry(_tmpl(), {"name": "no id"}, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("缺 id 应报错")
    print("✅ test_build_entry_requires_id")


# ---------------------------------------------------------------------------
# 3. merge_models
# ---------------------------------------------------------------------------

def test_merge_adds_updates_and_never_deletes():
    """同 slug 更新，新 slug 追加，**用户手加的无关条目必须保留**。"""
    existing = [
        {"slug": "deepseek-v4.1-flash", "priority": 1, "display_name": "旧名"},
        {"slug": "deepseek-v4-pro", "priority": 2, "display_name": "手加的 CN 模型"},
    ]
    metas = [
        {"id": "deepseek-v4.1-flash", "name": "新名", "maxAllowedSize": 1000000},
        {"id": "gpt-5.5", "name": "GPT-5.5", "maxAllowedSize": 1000000},
    ]
    merged, added, updated = scm.merge_models(existing, metas, _tmpl())
    slugs = [m["slug"] for m in merged]
    assert added == ["gpt-5.5"]
    assert updated == ["deepseek-v4.1-flash"]
    assert "deepseek-v4-pro" in slugs, "手加条目不该被工具抹掉"
    assert len(merged) == 3
    # 同 slug 的条目被替换而不是重复追加
    assert slugs.count("deepseek-v4.1-flash") == 1
    assert next(m for m in merged if m["slug"] == "deepseek-v4.1-flash")["display_name"] == "新名"
    print("✅ test_merge_adds_updates_and_never_deletes")


def test_merge_priority_continues_after_existing_max():
    """新条目优先级接在现有最大值之后，不打乱用户既有排序。"""
    existing = [{"slug": "a", "priority": 40}, {"slug": "b", "priority": 2}]
    merged, _, _ = scm.merge_models(
        existing, [{"id": "n1", "maxAllowedSize": 1}, {"id": "n2", "maxAllowedSize": 1}], _tmpl()
    )
    prios = {m["slug"]: m["priority"] for m in merged}
    assert prios["a"] == 40 and prios["b"] == 2, "已有条目的优先级不该被动"
    assert prios["n1"] == 41 and prios["n2"] == 42
    print("✅ test_merge_priority_continues_after_existing_max")


def test_merge_skips_metas_without_id():
    merged, added, updated = scm.merge_models([], [{"name": "no id"}], _tmpl())
    assert merged == [] and added == [] and updated == []
    print("✅ test_merge_skips_metas_without_id")


# ---------------------------------------------------------------------------
# 4. pick_template
# ---------------------------------------------------------------------------

def test_pick_template_prefers_a_model_being_synced():
    existing = [{"slug": "unrelated"}, {"slug": "gpt-5.5"}]
    t = scm.pick_template(existing, [{"id": "gpt-5.5"}])
    assert t["slug"] == "gpt-5.5", "应优先选本次要同步的那条（更贴近用户实际在跑的版本）"
    # 没有交集时退第一条
    assert scm.pick_template(existing, [{"id": "zzz"}])["slug"] == "unrelated"
    print("✅ test_pick_template_prefers_a_model_being_synced")


def test_pick_template_errors_when_nothing_to_clone():
    """空列表要明确报错并解释原因 —— 提示词没法凭空生成。"""
    try:
        scm.pick_template([], [{"id": "a"}])
    except SystemExit as e:
        assert "模板" in str(e), f"报错信息该说清原因，实为 {e}"
    else:
        raise AssertionError("空列表应报错")
    print("✅ test_pick_template_errors_when_nothing_to_clone")


# ---------------------------------------------------------------------------
# 5. write_models
# ---------------------------------------------------------------------------

def test_write_models_backs_up_and_writes_atomically():
    """写回后：内容可读、有备份、无 .tmp 残留。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "models.json"
        p.write_text(json.dumps({"models": [{"slug": "old"}]}, ensure_ascii=False), encoding="utf-8")
        bak = scm.write_models(p, [{"slug": "new"}])
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert doc["models"] == [{"slug": "new"}]
        assert bak.exists(), "应生成备份"
        assert json.loads(bak.read_text(encoding="utf-8"))["models"] == [{"slug": "old"}]
        assert not (p.parent / "models.json.tmp").exists(), "不该残留临时文件"
        assert doc["models"][0]["slug"] == "new"
    print("✅ test_write_models_backs_up_and_writes_atomically")


if __name__ == "__main__":
    t0 = time.time()
    test_parse_metas_filters_by_cli_whitelist()
    test_parse_metas_keeps_fields_and_tolerates_missing_agents()
    test_build_entry_overrides_model_specific_fields()
    test_build_entry_does_not_mutate_template()
    test_build_entry_text_only_model()
    test_build_entry_window_fallback_and_missing()
    test_build_entry_window_fallbacks()
    test_build_entry_requires_id()
    test_merge_adds_updates_and_never_deletes()
    test_merge_priority_continues_after_existing_max()
    test_merge_skips_metas_without_id()
    test_pick_template_prefers_a_model_being_synced()
    test_pick_template_errors_when_nothing_to_clone()
    test_write_models_backs_up_and_writes_atomically()
    print(f"\n🎉 All 14 tests passed! ({time.time() - t0:.2f}s)")
