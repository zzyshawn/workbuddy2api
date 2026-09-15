#!/usr/bin/env python3
"""
test_model_registry.py — 验证动态模型发现的裁剪规则与多级缓存/回退。

直接运行：python3 test_model_registry.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

from model_registry import (  # noqa: E402
    FALLBACK_MODELS,
    ModelFetchError,
    ModelInfo,
    ModelRegistry,
    parse_models_payload,
)


def _envelope(models, cli_ids):
    return {
        "code": 0,
        "msg": "OK",
        "data": {
            "models": models,
            "agents": [{"name": "cli", "models": cli_ids}],
        },
    }


UPSTREAM = _envelope(
    [
        {"id": "auto", "name": "Auto", "maxInputTokens": 168000, "maxOutputTokens": 32000},
        {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 1000000,
         "maxOutputTokens": 48000, "reasoning": {"supportedEfforts": ["low", "high", "max"]}},
        {"id": "hy3-x", "name": "Hy3", "maxInputTokens": 192000, "maxOutputTokens": 64000},
        {"id": "retired", "name": "Retired", "maxInputTokens": 1, "maxOutputTokens": 1,
         "disabled": True},
        {"id": "not-in-cli", "name": "Nope", "maxInputTokens": 1, "maxOutputTokens": 1},
    ],
    ["auto", "glm-5.3", "hy3-x", "retired", "never-existed"],
)


def test_parse_whitelist_and_fields():
    """白名单裁剪 + 字段映射（maxInputTokens→context_length、maxOutputTokens→max_tokens）。"""
    got = parse_models_payload(UPSTREAM)
    assert [m.id for m in got] == ["auto", "glm-5.3", "hy3-x"], [m.id for m in got]
    assert got[0].context_window == 168000 and got[0].max_tokens == 32000
    assert got[1].efforts == ("low", "high", "max")
    d = got[1].to_dict()
    assert d["context_length"] == 1000000 and d["max_tokens"] == 48000
    assert d["object"] == "model" and d["id"] == "glm-5.3"
    print("✅ test_parse_whitelist_and_fields")


def test_parse_filters_disabled_and_unknown():
    """disabled=true 与白名单里不存在于 models[] 的 id 都要被剔除。"""
    ids = [m.id for m in parse_models_payload(UPSTREAM)]
    assert "retired" not in ids and "never-existed" not in ids and "not-in-cli" not in ids
    print("✅ test_parse_filters_disabled_and_unknown")


def test_parse_error_paths():
    """code!=0 / 无 cli agent / 结果为空 都要报 ModelFetchError。"""
    for bad, why in [
        ({"code": 400, "msg": "nope", "data": {}}, "code!=0"),
        (_envelope([], []) | {"data": {"models": [], "agents": [{"name": "cli", "models": []}]}},
         "空白名单"),
        (_envelope([], []) | {"data": {"models": [], "agents": []}}, "无 cli agent"),
        ({}, "无 data"),
    ]:
        try:
            parse_models_payload(bad)
        except ModelFetchError:
            pass
        else:
            raise AssertionError(f"应当报错：{why}")
    print("✅ test_parse_error_paths")


def test_modelinfo_roundtrip():
    m = ModelInfo(id="x", name="X", context_window=10, max_tokens=2, efforts=("high",))
    assert ModelInfo.from_dict(m.to_dict()) == m
    bare = ModelInfo(id="y")
    assert "context_length" not in bare.to_dict() and "reasoning_efforts" not in bare.to_dict()
    print("✅ test_modelinfo_roundtrip")


class _Reg(ModelRegistry):
    """把上游拉取替换成可控结果。"""

    def __init__(self, *a, results=None, **kw):
        super().__init__(*a, **kw)
        self.calls = 0
        self.results = list(results or [])

    def _fetch(self, headers):
        self.calls += 1
        item = self.results.pop(0) if self.results else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise ModelFetchError("no scripted result")
        return item


def _models(*ids):
    return [ModelInfo(id=i, context_window=1000, max_tokens=100) for i in ids]


def test_cache_hit_and_refresh_cooldown():
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, ttl=3600, fail_cooldown=300,
                   results=[_models("a", "b"), _models("a", "b", "c")])
        m1, s1 = reg.resolve({"Authorization": "B"})
        assert s1 == "upstream" and [m.id for m in m1] == ["a", "b"]
        m2, s2 = reg.resolve({"Authorization": "B"})
        assert s2 == "cache" and reg.calls == 1, (s2, reg.calls)
        m3, s3 = reg.resolve({"Authorization": "B"}, force=True)
        assert s3 == "upstream" and reg.calls == 2 and [m.id for m in m3] == ["a", "b", "c"]
        print("✅ test_cache_hit_and_refresh_cooldown")


def test_snapshot_persist_and_cross_restart_fallback():
    """D5 修复：落盘快照让新进程冷启动也能拿到真实列表而不是内置表。"""
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, results=[_models("a", "b")])
        reg.resolve({"Authorization": "B"})
        assert (Path(td) / "models-snapshot.json").exists()

        # 新进程：上游不可用 → 命中落盘快照
        reg2 = _Reg(base_url="http://x", cache_dir=td, results=[ModelFetchError("boom")])
        m, s = reg2.resolve({"Authorization": "B"})
        assert s == "stale-snapshot" and [x.id for x in m] == ["a", "b"], (s, m)
        print("✅ test_snapshot_persist_and_cross_restart_fallback")


def test_stale_memory_beats_snapshot_and_failure_cooldown():
    """D2 修复：失败期回退「上一份真实列表」；且冷却窗口内不再打上游。"""
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, ttl=0.0, fail_cooldown=300,
                   results=[_models("a"), ModelFetchError("boom")])
        reg.resolve({"Authorization": "B"})          # upstream
        m, s = reg.resolve({"Authorization": "B"})   # ttl=0 → 过期，拉取失败 → 内存旧快照
        assert s == "stale-memory" and [x.id for x in m] == ["a"], (s, m)
        assert reg.calls == 2
        m2, s2 = reg.resolve({"Authorization": "B"})  # 冷却窗口内
        assert s2 == "stale-memory" and reg.calls == 2, (s2, reg.calls)
        print("✅ test_stale_memory_beats_snapshot_and_failure_cooldown")


def test_no_fallback_raises():
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, results=[ModelFetchError("boom")])
        try:
            reg.resolve({"Authorization": "B"})
        except ModelFetchError:
            pass
        else:
            raise AssertionError("无快照时应抛 ModelFetchError，由调用方落到内置表")
        print("✅ test_no_fallback_raises")


def test_disabled_registry_raises():
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, enabled=False)
        try:
            reg.resolve({"Authorization": "B"})
        except ModelFetchError as e:
            assert "disabled" in str(e)
        else:
            raise AssertionError("禁用时应抛 ModelFetchError")
        print("✅ test_disabled_registry_raises")


def test_status_shape():
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, ttl=60, results=[_models("a")])
        reg.resolve({"Authorization": "B"})
        st = reg.status()
        for k in ("enabled", "source", "models", "ids", "fetched_at", "age_seconds",
                  "ttl_seconds", "fresh", "snapshot_file", "snapshot_exists"):
            assert k in st, k
        assert st["ids"] == ["a"] and st["fresh"] is True and st["snapshot_exists"] is True
        assert json.dumps(st, ensure_ascii=False)  # 可序列化
        print("✅ test_status_shape")


def test_fallback_table_has_no_retired_models():
    """D1 修复：内置表里不能出现上游已下线的模型。"""
    retired = {"hy3-preview", "hy3-preview-agent", "deepseek-v4-flash",
               "kimi-k2.5", "minimax-m3-pay"}
    assert retired.isdisjoint(set(FALLBACK_MODELS)), retired & set(FALLBACK_MODELS)
    assert "glm-5.3" in FALLBACK_MODELS and "deepseek-v4.1-flash" in FALLBACK_MODELS
    print("✅ test_fallback_table_has_no_retired_models")


if __name__ == "__main__":
    t0 = time.time()
    test_parse_whitelist_and_fields()
    test_parse_filters_disabled_and_unknown()
    test_parse_error_paths()
    test_modelinfo_roundtrip()
    test_cache_hit_and_refresh_cooldown()
    test_snapshot_persist_and_cross_restart_fallback()
    test_stale_memory_beats_snapshot_and_failure_cooldown()
    test_no_fallback_raises()
    test_disabled_registry_raises()
    test_status_shape()
    test_fallback_table_has_no_retired_models()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
