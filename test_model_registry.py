#!/usr/bin/env python3
"""
test_model_registry.py — 验证动态模型发现的裁剪规则与多级缓存/回退。

直接运行：python3 test_model_registry.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, ".")

import model_registry as mr  # noqa: E402
from model_registry import (  # noqa: E402
    FALLBACK_MODELS,
    FALLBACK_MODELS_BY_ENV,
    CN_NON_CLI_MODELS,
    FALLBACK_MODELS_CN,
    FALLBACK_MODELS_INTL,
    INTL_NON_CLI_MODELS,
    PROBE_CANDIDATES,
    ModelFetchError,
    ModelInfo,
    ModelRegistry,
    cli_user_agent,
    fallback_models,
    is_cli_user_agent,
    parse_models_payload,
    probe_candidates,
)
import environments  # noqa: E402


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

    def __init__(self, *a, results=None, probe_hits=None, probe_raises=None, **kw):
        super().__init__(*a, **kw)
        self.calls = 0
        self.results = list(results or [])
        #: 探针脚本：命中的 id 集合；None 表示探针永远失败（模拟网络不可用）
        self.probe_hits = probe_hits
        self.probe_calls: list[str] = []
        self.probe_raises = probe_raises

    def _fetch(self, headers):
        self.calls += 1
        item = self.results.pop(0) if self.results else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise ModelFetchError("no scripted result")
        return item

    def _probe_one(self, model_id, headers):
        self.probe_calls.append(model_id)
        if self.probe_hits is None:
            return False
        return model_id in self.probe_hits

    def _fetch_by_probe(self, headers):
        """复刻真实实现：只保留探针命中的候选，全空则抛错。"""
        if self.probe_raises is not None:
            raise self.probe_raises
        return super()._fetch_by_probe(headers)


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


# ---------------------------------------------------------------------------
# 探针兜底（权威接口临时不可用时的兜底，不再是国际版的主路径）
# ---------------------------------------------------------------------------


def test_probe_used_when_upstream_fails():
    """上游失败 → 探针命中 → 来源标记为 probe，且结果落盘快照。

    这是**兜底**路径：权威接口 `/v3/config` 因代理抖动/凭据过期临时不可用，
    探针给出的仍是当前真实可用的集合，比旧快照和内置表都准。
    这里用国际版（env=intl），因为跨环境派生正是要防的缺陷。
    """
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(
            base_url="https://www.codebuddy.ai", env=environments.INTL,
            cache_dir=td, ttl=3600,
            results=[ModelFetchError("config api status 502: <html>APISIX")],
            probe_hits={"glm-5.2", "default-model", "kimi-k3"},
        )
        models, src = reg.resolve({"Authorization": "B"})
        assert src == "probe", f"应走探针兜底，实际 {src}"
        ids = [m.id for m in models]
        assert ids == ["default-model", "glm-5.2", "kimi-k3"], ids
        # 探针覆盖了候选池里每一个，且无重复。
        # 注意：真实实现是**并发**探（串行要 100s+，并发降到 ~15s），
        # 所以这里只能断言集合相等，不能断言调用顺序。
        pool = probe_candidates(environments.INTL)
        assert sorted(reg.probe_calls) == sorted(pool), "应把候选池整个探一遍"
        assert len(reg.probe_calls) == len(pool), "不应重复探同一个候选"
        # 结果必须落盘，避免每次重启都重新探一遍
        assert (Path(td) / "models-snapshot.json").exists()
        # 并发收集后应按候选池原顺序重排，保证输出稳定（便于快照对比）
        assert ids == [m for m in pool if m in set(ids)], "输出顺序应稳定"
        print("✅ test_probe_used_when_upstream_fails")


def test_probe_beats_stale_snapshot():
    """探针优先于旧快照 —— 旧快照可能含已下线的模型。"""
    with tempfile.TemporaryDirectory() as td:
        # 第一轮：上游正常，落一份含 "glm-5.3-flash"（国际版实际不存在）的快照
        reg1 = _Reg(base_url="https://www.codebuddy.ai", env=environments.INTL,
                    cache_dir=td, ttl=0.0,
                    results=[_models("default-model", "glm-5.3-flash")])
        reg1.resolve({"Authorization": "B"})

        # 第二轮：上游失败，探针命中 → 应给 probe 而不是 stale-snapshot
        reg2 = _Reg(base_url="https://www.codebuddy.ai", env=environments.INTL,
                    cache_dir=td, ttl=0.0,
                    results=[ModelFetchError("boom")],
                    probe_hits={"default-model", "glm-5.2"})
        models, src = reg2.resolve({"Authorization": "B"})
        assert src == "probe", f"探针应优先于旧快照，实际 {src}"
        assert [m.id for m in models] == ["default-model", "glm-5.2"], [m.id for m in models]
        print("✅ test_probe_beats_stale_snapshot")


def test_probe_failure_falls_back_to_snapshot():
    """探针也失败时，仍然回退旧快照（探针不能把回退链打断）。"""
    with tempfile.TemporaryDirectory() as td:
        reg1 = _Reg(base_url="http://x", cache_dir=td, ttl=0.0, results=[_models("a", "b")])
        reg1.resolve({"Authorization": "B"})

        reg2 = _Reg(base_url="http://x", cache_dir=td, ttl=0.0,
                    results=[ModelFetchError("boom")], probe_hits=set())
        models, src = reg2.resolve({"Authorization": "B"})
        assert src == "stale-snapshot" and [m.id for m in models] == ["a", "b"], (src, models)
        print("✅ test_probe_failure_falls_back_to_snapshot")


def test_probe_all_miss_raises():
    """候选池全不命中 → 探针自身抛错，交给调用方继续回退。"""
    with tempfile.TemporaryDirectory() as td:
        reg = _Reg(base_url="http://x", cache_dir=td, ttl=0.0,
                   results=[ModelFetchError("boom")], probe_hits=set())
        try:
            reg.resolve({"Authorization": "B"})
        except ModelFetchError:
            pass
        else:
            raise AssertionError("探针全空且无快照时应抛 ModelFetchError")
        print("✅ test_probe_all_miss_raises")


def test_probe_candidates_are_per_environment():
    """D6 回归：探针候选池必须与本环境回退表同源，**绝不跨环境派生**。

    原实现在这里翻了车：`PROBE_CANDIDATES = list(FALLBACK_MODELS) + [...]
    而 FALLBACK_MODELS 是国内版表` → 用国内版名单去探国际版端点，只能捞出
    两个集合的交集（14 个），把国际版独有的 gpt/gemini 系全漏了。
    """
    assert probe_candidates(environments.INTL) == FALLBACK_MODELS_INTL
    assert probe_candidates(environments.CN) == FALLBACK_MODELS_CN
    # 单参调用与不传参一致；未知环境按 resolve_env 兜底国内版。
    assert probe_candidates() == fallback_models()
    assert probe_candidates("nonsense") == FALLBACK_MODELS_CN
    # 返回值是副本，改它不该污染常量表。
    probe_candidates(environments.INTL).append("__junk__")
    assert "__junk__" not in FALLBACK_MODELS_INTL
    print("✅ test_probe_candidates_are_per_environment")


def test_cn_and_intl_tables_are_not_interchangeable():
    """两个环境的模型集**不是包含关系**，用一张表覆盖两个环境必然出错。"""
    cn, intl = set(FALLBACK_MODELS_CN), set(FALLBACK_MODELS_INTL)
    assert cn != intl
    # 国际版独有：gpt 系 + gemini + 五个别名档，国内版一个都没有。
    intl_only = {"gpt-6-astra", "gpt-5.6-sol", "gemini-3.5-flash", "default-model"}
    assert intl_only <= intl and not (intl_only & cn), intl_only & cn
    # 国内版独有（国际版实测不存在 / 不在白名单）：
    # 注：glm-5.3-flash 2026-09-25 起两边都有（国际版白名单新增），从 cn_only 移出。
    cn_only = {"hy3-x", "minimax-m3", "minimax-m2.7",
               "kimi-k3-1", "kimi-k2.7", "deepseek-v4-pro"}
    assert cn_only <= cn and not (cn_only & intl), cn_only & intl
    # 两边都有（真实交集）：
    both = {"hy4-preview", "hy3", "glm-5.3", "glm-5.2", "glm-5.3-flash",
            "kimi-k2.6", "kimi-k2.8-preview", "deepseek-v4.1-flash"}
    assert both <= (cn & intl), both - (cn & intl)
    # 交集就是「两个环境都真实存在」的部分，必须非空（否则说明表填错了）。
    assert 0 < len(cn & intl) < min(len(cn), len(intl))
    # 两张表都是 **cli 白名单口径**，不是 models[] 口径：
    #   cn   models[] 31 → cli 16（default/deepseek-v3-2-volc/hunyuan-* 等不给 CLI 用）
    #   intl models[] 22 → cli 21（deepseek-v4.1-flash-sg 不给 CLI 用；2026-09-25 复核）
    assert len(FALLBACK_MODELS_CN) == 16 and len(FALLBACK_MODELS_INTL) == 21
    assert "deepseek-v4.1-flash-sg" not in intl
    assert "default" not in cn, "`default` 只在 models[] 里，不在 cli 白名单"
    assert "hunyuan-chat" not in cn, "hunyuan-* 不在 cli 白名单"
    assert INTL_NON_CLI_MODELS == ["deepseek-v4.1-flash-sg"]
    assert len(CN_NON_CLI_MODELS) == 15, "31 - 16 = 15"
    print("✅ test_cn_and_intl_tables_are_not_interchangeable")


def test_fallback_models_by_env_lookup():
    assert fallback_models(environments.CN) == FALLBACK_MODELS_CN
    assert fallback_models(environments.INTL) == FALLBACK_MODELS_INTL
    assert fallback_models(None) == FALLBACK_MODELS_CN  # resolve_env 兜底 cn
    # 旧名仍指向国内版表，只是兼容用途。
    assert FALLBACK_MODELS is FALLBACK_MODELS_CN
    assert FALLBACK_MODELS_BY_ENV[environments.INTL] is FALLBACK_MODELS_INTL
    print("✅ test_fallback_models_by_env_lookup")


def test_cli_user_agent_gate():
    """`/v3/config` 的 UA 门槛：必须 `CLI/<ver> CodeBuddy/<ver>`，大小写敏感。

    这组断言直接对应 2026-09-19 的实测矩阵，任何一条被改动都意味着
    「国际版拿不到权威模型清单」这个坑可能被重新踩进来。
    """
    good = "CLI/2.158.0 CodeBuddy/2.158.0"
    assert cli_user_agent() == good
    assert is_cli_user_agent(good)
    assert is_cli_user_agent(good + " (darwin; arm64)")     # 后缀不影响
    assert is_cli_user_agent("CLI/9.9.9 CodeBuddy/9.9.9")   # 版本号不校验
    assert is_cli_user_agent(cli_user_agent("1.0.0"))       # 任意版本都行
    for bad in [
        "CodeBuddy/2.155.0",       # 缺 CLI/ 前缀 → models 为 null
        "codebuddy/2.155.0",       # 小写 → null
        "CLI/2.155.0",             # 只有 CLI/ → 400
        "codebuddy2openai/2.0",    # 本项目 chat 路径的 UA → 不合格
        "",
        None,
    ]:
        assert not is_cli_user_agent(bad), f"不该判为合格：{bad!r}"
    print("✅ test_cli_user_agent_gate")


def test_cli_version_runtime_override():
    """UA 版本可运行时覆盖（配置 > 环境变量 > 内置常量），CLI 升级不改代码。

    2026-09-25 需求：不是所有上游更新都值得重新打包 —— UA 版本号上游不校验，
    跟版本只是对齐语义，理应是纯配置。
    """
    saved_override = mr._ua_version_override
    saved_env = os.environ.get(mr.UA_ENV_KEY)
    try:
        # 1) 默认 = 内置常量
        assert mr.effective_cli_version() == mr.CLI_UA_VERSION

        # 2) 环境变量覆盖
        os.environ[mr.UA_ENV_KEY] = "2.159.0"
        assert mr.effective_cli_version() == "2.159.0"
        assert cli_user_agent() == "CLI/2.159.0 CodeBuddy/2.159.0"

        # 3) 程序化注入优先级最高（converter --ua-version 走这条）
        mr.set_cli_version("2.160.0")
        assert mr.effective_cli_version() == "2.160.0"

        # 4) 注入空串 = 清除注入，回落环境变量
        mr.set_cli_version("")
        assert mr.effective_cli_version() == "2.159.0"

        # 5) 环境变量清掉 → 回落常量
        del os.environ[mr.UA_ENV_KEY]
        assert mr.effective_cli_version() == mr.CLI_UA_VERSION
    finally:
        mr._ua_version_override = saved_override
        if saved_env is None:
            os.environ.pop(mr.UA_ENV_KEY, None)
        else:
            os.environ[mr.UA_ENV_KEY] = saved_env
    print("✅ test_cli_version_runtime_override")


def test_cli_version_file_hot_reload():
    """版本文件热读取：File Station 改完文件，下一个请求就生效，无需重启。

    这是「不要重建容器」需求的主路径 —— 文件在挂载目录里，改文件即改配置。
    """
    saved_override = mr._ua_version_override
    saved_file = mr._ua_file_path
    try:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "ua-version.txt"
            mr.set_cli_version_file(str(f))
            mr.set_cli_version(None)  # 清掉注入，让文件来源说话

            # 文件不存在 → 内置默认
            assert mr.effective_cli_version() == mr.CLI_UA_VERSION

            # 写入版本（带注释行）→ 生效
            f.write_text("# 上游 CLI 版本，改完即生效\n2.159.0\n", encoding="utf-8")
            assert mr.effective_cli_version() == "2.159.0"
            assert cli_user_agent() == "CLI/2.159.0 CodeBuddy/2.159.0"

            # 热更新：改内容即变（mtime 变化触发重读）
            f.write_text("2.160.0\n", encoding="utf-8")
            os.utime(f, (time.time() + 2, time.time() + 2))  # 确保 mtime 变化
            assert mr.effective_cli_version() == "2.160.0"

            # 清空文件 → 回落内置默认（不用删文件）
            f.write_text("\n", encoding="utf-8")
            os.utime(f, (time.time() + 4, time.time() + 4))
            assert mr.effective_cli_version() == mr.CLI_UA_VERSION

            # 关闭文件来源
            mr.set_cli_version_file(None)
            f.write_text("2.161.0\n", encoding="utf-8")
            os.utime(f, (time.time() + 6, time.time() + 6))
            assert mr.effective_cli_version() == mr.CLI_UA_VERSION
    finally:
        mr._ua_version_override = saved_override
        mr.set_cli_version_file(saved_file)
    print("✅ test_cli_version_file_hot_reload")


def test_fetch_hits_v3_config_with_cli_ua():
    """权威接口必须是 `/v3/config`，且请求头里的 UA 必须过门槛。

    旧实现打的是 `/console/enterprises/personal/models`（个人号恒空 / 国际版 500），
    这条断言把它钉死，防止回退。
    """
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, json=_envelope(
            [{"id": "gpt-5.6-sol", "name": "Sol", "maxInputTokens": 100, "maxOutputTokens": 10}],
            ["gpt-5.6-sol"],
        ))

    reg = ModelRegistry(base_url="https://www.codebuddy.ai", env=environments.INTL, proxy="")
    # `_fetch` 用 `with` 管客户端，所以工厂每次都得给一个**新**客户端。
    orig = environments.make_client
    environments.make_client = lambda *a, **kw: httpx.Client(
        transport=httpx.MockTransport(handler)
    )
    try:
        got = reg._fetch({"Authorization": "Bearer x", "User-Agent": "codebuddy2openai/2.0"})
    finally:
        environments.make_client = orig

    assert seen["url"] == "https://www.codebuddy.ai/v3/config", seen["url"]
    assert "personal/models" not in seen["url"], "不该再打旧的个人号端点"
    assert is_cli_user_agent(seen["ua"]), f"UA 未过门槛：{seen['ua']!r}"
    assert [m.id for m in got] == ["gpt-5.6-sol"]
    print("✅ test_fetch_hits_v3_config_with_cli_ua")


def test_probe_uses_environment_candidate_pool():
    """探针候选池按 env 取：国际版探的是国际版表，不是国内版表。"""
    tried: list[str] = []

    class _R(ModelRegistry):
        def _probe_one(self, mid, headers):
            tried.append(mid)
            return mid == "gpt-6-astra"

    reg = _R(base_url="https://www.codebuddy.ai", env=environments.INTL)
    got = reg._fetch_by_probe({})
    assert sorted(tried) == sorted(FALLBACK_MODELS_INTL), "应探整个国际版候选池"
    # glm-5.3-flash 自 2026-09-25 起两边白名单都有（不再是国内版独有）；
    # 仍需不在国际版探测池里的是「从未进国际版白名单」的模型：
    assert "hy3-x" not in tried and "minimax-m3" not in tried and "deepseek-v4-pro" not in tried, \
        "国内版独有模型不该出现在国际版探测里"
    assert [m.id for m in got] == ["gpt-6-astra"]
    print("✅ test_probe_uses_environment_candidate_pool")


def test_probe_candidate_pool_shape():
    """候选池：有序、无重复、非空；旧默认名仍等于国内版池（兼容）。"""
    for pool in (FALLBACK_MODELS_CN, FALLBACK_MODELS_INTL):
        assert pool, "回退表不能为空"
        assert len(pool) == len(set(pool)), f"回退表有重复：{pool}"
        assert all(isinstance(m, str) and m for m in pool)
    assert len(PROBE_CANDIDATES) == len(set(PROBE_CANDIDATES))
    assert PROBE_CANDIDATES == FALLBACK_MODELS_CN
    print("✅ test_probe_candidate_pool_shape")


def test_fallback_table_has_no_retired_models():
    """D1 修复：内置表里不能出现上游已下线的模型。

    注意 `kimi-k2.5` 在两个环境**都存在**（国内版实测在表内，国际版也有对应项），
    它不属于 retired 集合，别把「不在某环境表里」和「已下线」搞混。
    """
    retired = {"hy3-preview", "hy3-preview-agent", "deepseek-v4-flash",
               "minimax-m3-pay"}
    for env, table in FALLBACK_MODELS_BY_ENV.items():
        assert retired.isdisjoint(set(table)), (env, retired & set(table))
    assert "glm-5.3" in FALLBACK_MODELS_CN and "deepseek-v4.1-flash" in FALLBACK_MODELS_CN
    assert "glm-5.3" in FALLBACK_MODELS_INTL and "deepseek-v4.1-flash" in FALLBACK_MODELS_INTL
    print("✅ test_fallback_table_has_no_retired_models")


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
    """D1 修复：内置表里不能出现上游已下线的模型。

    注意 `kimi-k2.5` 在两个环境**都存在**（国内版实测在表内，国际版也有对应项），
    它不属于 retired 集合，别把「不在某环境表里」和「已下线」搞混。
    """
    retired = {"hy3-preview", "hy3-preview-agent", "minimax-m3-pay"}
    for env, table in FALLBACK_MODELS_BY_ENV.items():
        assert retired.isdisjoint(set(table)), (env, retired & set(table))
    assert "glm-5.3" in FALLBACK_MODELS_CN and "deepseek-v4.1-flash" in FALLBACK_MODELS_CN
    assert "glm-5.3" in FALLBACK_MODELS_INTL and "deepseek-v4.1-flash" in FALLBACK_MODELS_INTL
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
    test_fallback_models_by_env_lookup()
    test_cn_and_intl_tables_are_not_interchangeable()
    test_cli_user_agent_gate()
    test_cli_version_runtime_override()
    test_cli_version_file_hot_reload()
    test_fetch_hits_v3_config_with_cli_ua()
    test_fallback_table_has_no_retired_models()
    test_probe_candidates_are_per_environment()
    test_probe_uses_environment_candidate_pool()
    test_probe_candidate_pool_shape()
    test_probe_used_when_upstream_fails()
    test_probe_beats_stale_snapshot()
    test_probe_failure_falls_back_to_snapshot()
    test_probe_all_miss_raises()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
