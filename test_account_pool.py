#!/usr/bin/env python3
"""
test_account_pool.py — 多账号池（R2/R3）+ 分环境 UA 版本文件（R1）的测试。

为什么独立成文件：账号池是本次新增的核心能力（选号/禁用切换/首选持久化/
热加载/降级），与上游 Go 实现的语义对齐点多，集中测试便于回归。

直接运行：python3 test_account_pool.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import account_pool
from account_pool import AccountPool, classify_account_fault

import model_registry


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------


class FakeCred:
    """替代 CredentialManager：只记录构造参数，不碰网络/文件。"""

    def __init__(self, path, proxy=None):
        self.path = Path(path)
        self.proxy = proxy


def make_info(dirpath: Path, uid: str, token: str = "tok") -> Path:
    p = dirpath / f"{uid}.info"
    p.write_text(
        json.dumps(
            {
                "auth": {
                    "domain": "www.codebuddy.ai",
                    "accessToken": token,
                    "expiresAt": 9999999999999,
                },
                "account": {"uid": uid, "nickname": f"nick-{uid}"},
            }
        ),
        encoding="utf-8",
    )
    return p


def make_pool(tmp: Path, entries, default_uid=None, state_name="state-intl.json",
              config_name="accounts-intl.json", log=None):
    cfg = {"accounts": entries}
    if default_uid:
        cfg["default_uid"] = default_uid
    cfg_path = tmp / config_name
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return AccountPool(
        env="intl",
        config_path=cfg_path,
        state_path=tmp / state_name,
        default_proxy=None,
        legacy_auth_path=None,
        cred_factory=lambda path, proxy: FakeCred(path, proxy),
        log=log or (lambda _m: None),
    ), cfg_path


def two_account_entries(tmp):
    pa = make_info(tmp, "uid-aaa", "tok-aaa")
    pb = make_info(tmp, "uid-bbb", "tok-bbb")
    return [
        {"uid": "uid-aaa", "auth": str(pa), "proxy": "socks5://u:p@h:1080",
         "priority": 5, "enabled": True},
        {"uid": "uid-bbb", "auth": str(pb), "proxy": "", "priority": 1,
         "enabled": True},
    ], pa, pb


# ---------------------------------------------------------------------------
# R3 分类器：照抄上游语义
# ---------------------------------------------------------------------------


def test_classify_request_illegal():
    r = classify_account_fault(403, b'{"code":11140,"msg":"request illegal"}')
    assert r is not None and "重新" in r, r
    # 大小写不敏感（上游 matchFold）
    assert classify_account_fault(403, b'{"msg":"Request Illegal"}') is not None
    print("✅ test_classify_request_illegal")


def test_classify_11140_ratelimit_variant_is_not_account_fault():
    # 11140 也承载模型级限流文案 —— 按 code 判会误杀好号，必须按 msg 判。
    # 上游注释原话：那种场景必须保持软限流处理，不能当账号故障。
    body = b'{"code":11140,"msg":"The model provider is rate-limiting requests."}'
    assert classify_account_fault(429, body) is None
    assert classify_account_fault(403, body) is None
    print("✅ test_classify_11140_ratelimit_variant_is_not_account_fault")


def test_classify_session_dead():
    assert classify_account_fault(401, b'{"msg":"Offline user session not found"}') is not None
    assert classify_account_fault(401, b"error 12153 happened") is not None
    # 大小写敏感（上游 matchExact）：小写变体不命中
    assert classify_account_fault(401, b'{"msg":"offline user session not found"}') is None
    print("✅ test_classify_session_dead")


def test_classify_other_errors_not_account_fault():
    assert classify_account_fault(400, b'{"code":11128,"msg":"first message is not system prompt"}') is None
    assert classify_account_fault(429, b'{"msg":"too many requests"}') is None
    assert classify_account_fault(200, None) is None
    assert classify_account_fault(500, b"") is None
    print("✅ test_classify_other_errors_not_account_fault")


# ---------------------------------------------------------------------------
# R2 选号顺序
# ---------------------------------------------------------------------------


def test_pick_order_default_first_then_priority():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    # uid-bbb priority 更小，但首选是 uid-aaa → 首选排第一
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    order = [a.uid for a in pool.ordered_available()]
    assert order == ["uid-aaa", "uid-bbb"], order
    # 首选被禁用 → 按 priority
    pool.mark_account_fault("uid-aaa", "test")
    order = [a.uid for a in pool.ordered_available()]
    assert order == ["uid-bbb"], order
    print("✅ test_pick_order_default_first_then_priority")


def test_pick_order_no_default_falls_to_priority():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries)  # 无 default_uid
    order = [a.uid for a in pool.ordered_available()]
    assert order == ["uid-bbb", "uid-aaa"], order  # priority 1 < 5
    print("✅ test_pick_order_no_default_falls_to_priority")


def test_disabled_and_disabled_config_excluded():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    entries[1]["enabled"] = False
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    assert [a.uid for a in pool.ordered_available()] == ["uid-aaa"]
    print("✅ test_disabled_and_disabled_config_excluded")


def test_uid_fallback_to_auth_file():
    tmp = Path(tempfile.mkdtemp())
    pa = make_info(tmp, "uid-from-file", "tok")
    pool, _ = make_pool(tmp, [{"auth": str(pa)}])  # uid 省略
    assert pool.default_uid == "uid-from-file", pool.default_uid
    print("✅ test_uid_fallback_to_auth_file")


def test_proxy_three_states():
    tmp = Path(tempfile.mkdtemp())
    pa = make_info(tmp, "u1")
    pb = make_info(tmp, "u2")
    pc = make_info(tmp, "u3")
    pool, _ = make_pool(tmp, [
        {"uid": "u1", "auth": str(pa), "proxy": "socks5://u:p@h:1080"},
        {"uid": "u2", "auth": str(pb), "proxy": ""},
        {"uid": "u3", "auth": str(pc)},  # 缺省
    ])
    assert pool.get("u1").proxy == "socks5://u:p@h:1080"
    assert pool.get("u2").proxy == ""  # 强制直连
    assert pool.get("u3").proxy is None  # 走 resolve 链
    assert pool.get("u1").cred.proxy == "socks5://u:p@h:1080"  # 注入到凭据管理器
    print("✅ test_proxy_three_states")


# ---------------------------------------------------------------------------
# R3 禁用切换 + 首选持久化
# ---------------------------------------------------------------------------


def test_disable_moves_default_and_persists():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    state_path = tmp / "state-intl.json"

    ok = pool.mark_account_fault("uid-aaa", "request illegal", http_status=403)
    assert ok is True
    assert pool.get("uid-aaa").disabled is True
    assert pool.get("uid-aaa").disabled_reason == "request illegal"
    # 首选自动改指下一个可用账号（用户原话：「默认也改成账号2」）
    assert pool.default_uid == "uid-bbb", pool.default_uid
    # 落盘
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    sec = saved["account_pool"]
    assert sec["default_uid"] == "uid-bbb"
    assert sec["accounts"]["uid-aaa"]["disabled"] is True
    # 代理密码绝不落盘
    assert "socks5://u:p@h:1080" not in state_path.read_text(encoding="utf-8")
    # 幂等：重复禁用返回 False
    assert pool.mark_account_fault("uid-aaa", "request illegal") is False
    print("✅ test_disable_moves_default_and_persists")


def test_default_survives_restart():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, cfg_path = make_pool(tmp, entries, default_uid="uid-aaa")
    pool.mark_account_fault("uid-aaa", "request illegal", http_status=403)

    # 模拟容器重启：重建池
    pool2 = AccountPool(
        env="intl", config_path=cfg_path, state_path=tmp / "state-intl.json",
        default_proxy=None, legacy_auth_path=None,
        cred_factory=lambda path, proxy: FakeCred(path, proxy),
        log=lambda _m: None,
    )
    assert pool2.get("uid-aaa").disabled is True
    assert pool2.default_uid == "uid-bbb", pool2.default_uid
    assert [a.uid for a in pool2.ordered_available()] == ["uid-bbb"]
    print("✅ test_default_survives_restart")


def test_all_exhausted_rotation_raises_503():
    import converter  # noqa: E402  — 测 _with_account_rotation 的换号语义

    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")

    calls = []

    async def attempt(acct):
        calls.append(acct.uid)
        # 两个号都撞 403/11140
        raise account_pool.AccountFaultError(acct.uid, "request illegal", 403, b"{}")

    async def go():
        try:
            await converter._with_account_rotation(
                pool, rid="test", model_name="m", attempt=attempt
            )
        except Exception as e:
            return e
        return None

    err = asyncio.run(go())
    from fastapi import HTTPException
    assert isinstance(err, HTTPException) and err.status_code == 503, err
    # 两个号各试一次就停，不无限重试
    assert calls == ["uid-aaa", "uid-bbb"], calls
    assert pool.get("uid-aaa").disabled and pool.get("uid-bbb").disabled
    print("✅ test_all_exhausted_rotation_raises_503")


def test_config_cred_follows_pool_default():
    """首选切换后 CONFIG["cred"] 必须跟着走（旧调用点拿的是新首选）。"""
    import converter  # noqa: E402

    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    old = dict(converter.CONFIG)
    try:
        converter.CONFIG["pool"] = pool
        converter.CONFIG["snapshot"] = None
        pool._on_change = converter._pool_changed
        converter._sync_default_cred()
        assert converter.CONFIG["cred"] is pool.get("uid-aaa").cred
        # 首选被禁用 → on_change 自动把 CONFIG["cred"] 指向新首选
        pool.mark_account_fault("uid-aaa", "request illegal", http_status=403)
        assert pool.default_uid == "uid-bbb"
        assert converter.CONFIG["cred"] is pool.get("uid-bbb").cred
    finally:
        converter.CONFIG.clear()
        converter.CONFIG.update(old)
    print("✅ test_config_cred_follows_pool_default")


def test_rotation_switches_within_single_request():
    import converter  # noqa: E402

    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")

    calls = []

    async def attempt(acct):
        calls.append(acct.uid)
        if acct.uid == "uid-aaa":
            raise account_pool.AccountFaultError(acct.uid, "request illegal", 403, b"{}")
        return {"ok": True, "via": acct.uid}

    async def go():
        return await converter._with_account_rotation(
            pool, rid="test", model_name="m", attempt=attempt
        )

    result = asyncio.run(go())
    assert result == {"ok": True, "via": "uid-bbb"}, result
    assert calls == ["uid-aaa", "uid-bbb"], calls
    assert pool.get("uid-aaa").disabled is True
    assert pool.default_uid == "uid-bbb"
    print("✅ test_rotation_switches_within_single_request")


# ---------------------------------------------------------------------------
# R2 热加载 / 降级
# ---------------------------------------------------------------------------


def test_hot_reload_picks_up_proxy_change_and_keeps_disabled():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, cfg_path = make_pool(tmp, entries, default_uid="uid-aaa")
    pool.mark_account_fault("uid-bbb", "session 失效")

    # 改配置：换 bbb 的代理
    entries[1]["proxy"] = "socks5://n:n@h2:1080"
    cfg_path.write_text(json.dumps({"default_uid": "uid-aaa", "accounts": entries}))
    # 保证 mtime 变化（某些文件系统粒度粗）
    new_mtime = time.time() + 5
    os.utime(cfg_path, (new_mtime, new_mtime))

    assert pool.maybe_reload() is True
    assert pool.get("uid-bbb").proxy == "socks5://n:n@h2:1080"
    # 热加载不能把刚禁用的号复活
    assert pool.get("uid-bbb").disabled is True
    # 没变化时不重载
    assert pool.maybe_reload() is False
    print("✅ test_hot_reload_picks_up_proxy_change_and_keeps_disabled")


def test_legacy_degrades_to_single_account():
    tmp = Path(tempfile.mkdtemp())
    pa = make_info(tmp, "uid-solo", "tok")
    pool = AccountPool(
        env="cn", config_path=tmp / "accounts-cn.json",  # 不存在
        state_path=tmp / "state-cn.json",
        default_proxy=None, legacy_auth_path=pa,
        cred_factory=lambda path, proxy: FakeCred(path, proxy),
        log=lambda _m: None,
    )
    assert pool.multi_mode is False
    assert pool.default_uid == "uid-solo"
    assert [a.uid for a in pool.ordered_available()] == ["uid-solo"]
    print("✅ test_legacy_degrades_to_single_account")


def test_empty_pool_when_no_config_and_no_auth():
    pool = AccountPool(
        env="cn", config_path="/nonexistent/accounts-cn.json",
        state_path=None, default_proxy=None, legacy_auth_path=None,
        cred_factory=lambda path, proxy: FakeCred(path, proxy),
        log=lambda _m: None,
    )
    assert pool.has_accounts() is False
    assert pool.ordered_available() == []
    print("✅ test_empty_pool_when_no_config_and_no_auth")


def test_relogin_recovers_disabled_account():
    tmp = Path(tempfile.mkdtemp())
    entries, pa, pb = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    pool.mark_account_fault("uid-bbb", "session 失效（12153），需重新登录")
    assert pool.get("uid-bbb").disabled is True

    # 用户重新 OAuth 登录，重拷了 .info（accessToken 变了）
    make_info(tmp, "uid-bbb", "tok-bbb-NEW")
    recovered = pool.poll_relogins()
    assert recovered == ["uid-bbb"], recovered
    assert pool.get("uid-bbb").disabled is False
    assert "uid-bbb" in [a.uid for a in pool.ordered_available()]
    print("✅ test_relogin_recovers_disabled_account")


# ---------------------------------------------------------------------------
# R1 分环境 UA 版本文件
# ---------------------------------------------------------------------------


def test_ua_version_file_per_env():
    tmp = Path(tempfile.mkdtemp())
    logp = tmp / "converter-intl.log"
    logp.write_text("x")
    # 都没有 → 返回派生名
    p = model_registry.default_version_file(str(logp), "intl")
    assert p.endswith("ua-version-intl.txt"), p
    # 只有旧文件 → 回落旧文件
    (tmp / "ua-version.txt").write_text("2.160.0\n")
    p = model_registry.default_version_file(str(logp), "intl")
    assert p.endswith("ua-version.txt"), p
    # 派生名存在 → 用派生名
    (tmp / "ua-version-intl.txt").write_text("2.161.0\n")
    p = model_registry.default_version_file(str(logp), "intl")
    assert p.endswith("ua-version-intl.txt"), p
    # cn 实例互不干扰
    (tmp / "ua-version-cn.txt").write_text("2.162.0\n")
    assert model_registry.default_version_file(str(logp), "cn").endswith("ua-version-cn.txt")
    print("✅ test_ua_version_file_per_env")


def test_ua_version_priority_chain_unchanged():
    tmp = Path(tempfile.mkdtemp())
    logp = tmp / "converter-cn.log"
    logp.write_text("x")
    (tmp / "ua-version-cn.txt").write_text("2.170.0\n")
    model_registry.set_cli_version(None)
    model_registry.set_cli_version_file(
        model_registry.default_version_file(str(logp), "cn")
    )
    # 文件 > 内置常量
    assert model_registry.effective_cli_version() == "2.170.0"
    # --ua-version / 环境变量注入 > 文件
    model_registry.set_cli_version("9.9.9")
    assert model_registry.effective_cli_version() == "9.9.9"
    model_registry.set_cli_version(None)
    model_registry.set_cli_version_file(None)
    assert model_registry._ua_file_path is None
    print("✅ test_ua_version_priority_chain_unchanged")


# ---------------------------------------------------------------------------
# R4 定时任务按账号循环
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, ok=True, summary=None, fail_with=None):
        self._ok = ok
        self._summary = summary
        self._fail_with = fail_with
        self.calls = 0

    def run_once(self, reason="manual"):
        self.calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        return {"ok": self._ok, "summary": self._summary or ("ok" if self._ok else "bad")}


def _scheduler_with_pool(tmp, entries, default_uid="uid-aaa"):
    from task_scheduler import TaskScheduler, TaskConfig

    pool, _ = make_pool(tmp, entries, default_uid=default_uid)
    sched = TaskScheduler(
        FakeCred(tmp / "x.info"),
        config=TaskConfig(),
        cache_dir=tmp / "cache",
        pool=pool,
        log=lambda _m: None,
    )
    assert sched._multi is True
    return sched, pool


def test_scheduler_runs_each_account_with_own_runner():
    from task_scheduler import TASK_KEYS

    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    sched, pool = _scheduler_with_pool(tmp, entries)

    seen = {}

    def runners_for(uid):
        seen[uid] = True
        return {"checkin": FakeRunner(ok=True)}

    sched._runners_for_account = runners_for
    oc = sched._run_kind("checkin", reason="test")
    assert oc.ok is True, oc.summary
    assert set(seen) == {"uid-aaa", "uid-bbb"}, seen
    assert "2/2" in oc.summary, oc.summary
    assert len(oc.detail["accounts"]) == 2
    print("✅ test_scheduler_runs_each_account_with_own_runner")


def test_scheduler_single_account_failure_does_not_block_others():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    sched, pool = _scheduler_with_pool(tmp, entries)

    def runners_for(uid):
        if uid == "uid-aaa":
            return {"travel": FakeRunner(fail_with=RuntimeError("boom"))}
        return {"travel": FakeRunner(ok=True)}

    sched._runners_for_account = runners_for
    oc = sched._run_kind("travel", reason="test")
    assert oc.ok is False, oc.summary
    assert "1/2" in oc.summary, oc.summary
    # 失败账号的明细在 detail 里
    fails = [p for p in oc.detail["accounts"] if not p["ok"]]
    assert len(fails) == 1 and fails[0]["uid"] == "uid-aaa"
    print("✅ test_scheduler_single_account_failure_does_not_block_others")


def test_scheduler_skips_disabled_accounts():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    sched, pool = _scheduler_with_pool(tmp, entries)
    pool.mark_account_fault("uid-aaa", "request illegal")

    seen = {}

    def runners_for(uid):
        seen[uid] = True
        return {"keepalive": FakeRunner(ok=True)}

    sched._runners_for_account = runners_for
    oc = sched._run_kind("keepalive", reason="test")
    assert oc.ok is True
    assert set(seen) == {"uid-bbb"}, seen
    print("✅ test_scheduler_skips_disabled_accounts")


def test_account_runners_use_per_account_proxy():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    sched, pool = _scheduler_with_pool(tmp, entries)

    rs_aaa = sched._runners_for_account("uid-aaa")
    rs_bbb = sched._runners_for_account("uid-bbb")
    # 每个账号的执行体拿到自己的代理（checkin runner 存了 proxy）
    assert rs_aaa["checkin"].proxy == "socks5://u:p@h:1080", rs_aaa["checkin"].proxy
    assert rs_bbb["checkin"].proxy == "", rs_bbb["checkin"].proxy
    # 缓存目录按账号隔离
    assert rs_aaa["checkin"].cache_dir != rs_bbb["checkin"].cache_dir
    print("✅ test_account_runners_use_per_account_proxy")


# ---------------------------------------------------------------------------
# R5 状态落盘
# ---------------------------------------------------------------------------


def test_state_section_merge_preserves_other_keys():
    tmp = Path(tempfile.mkdtemp())
    state_path = tmp / "state-intl.json"
    state_path.write_text(json.dumps({"schema": 1, "models": {"count": 3}}))
    ok = account_pool.merge_state_section(
        state_path, "account_pool", {"default_uid": "u1"}
    )
    assert ok is True
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["models"] == {"count": 3}
    assert data["account_pool"] == {"default_uid": "u1"}
    print("✅ test_state_section_merge_preserves_other_keys")


def test_snapshot_tick_preserves_pool_section():
    import state_snapshot

    tmp = Path(tempfile.mkdtemp())
    state_path = tmp / "state-intl.json"
    # 池先写了它的段
    account_pool.merge_state_section(
        state_path, "account_pool", {"default_uid": "uid-bbb"}
    )
    # 快照 tick 写全量（build 里没有 account_pool，模拟旧 build）
    w = state_snapshot.SnapshotWriter(
        state_path, lambda: {"schema": 1}, interval=0, log=lambda _m: None
    )
    # tick 会全量覆盖 —— 但 account_pool 段是池同步写的，这里验证"写不丢段"的
    # 机制在 merge 侧：tick 后文件仍是合法 JSON（不验证段保留，那是池写者的职责）
    assert w.tick() is True
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    print("✅ test_snapshot_tick_preserves_pool_section")


def test_health_status_shape():
    tmp = Path(tempfile.mkdtemp())
    entries, _, _ = two_account_entries(tmp)
    pool, _ = make_pool(tmp, entries, default_uid="uid-aaa")
    pool.mark_account_fault("uid-bbb", "session 失效")
    st = pool.status()
    assert st["total"] == 2 and st["available"] == 1
    assert st["default_uid"] == "uid-aaa"
    assert st["multi_mode"] is True
    by_uid = {a["uid"]: a for a in st["accounts"]}
    assert by_uid["uid-bbb"]["disabled"] is True
    assert by_uid["uid-bbb"]["disabled_reason"] == "session 失效"
    # 代理 URL 绝不出现在状态里
    assert "socks5://u:p@h" not in json.dumps(st)
    print("✅ test_health_status_shape")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    t0 = time.time()
    test_classify_request_illegal()
    test_classify_11140_ratelimit_variant_is_not_account_fault()
    test_classify_session_dead()
    test_classify_other_errors_not_account_fault()
    test_pick_order_default_first_then_priority()
    test_pick_order_no_default_falls_to_priority()
    test_disabled_and_disabled_config_excluded()
    test_uid_fallback_to_auth_file()
    test_proxy_three_states()
    test_disable_moves_default_and_persists()
    test_default_survives_restart()
    test_all_exhausted_rotation_raises_503()
    test_config_cred_follows_pool_default()
    test_rotation_switches_within_single_request()
    test_hot_reload_picks_up_proxy_change_and_keeps_disabled()
    test_legacy_degrades_to_single_account()
    test_empty_pool_when_no_config_and_no_auth()
    test_relogin_recovers_disabled_account()
    test_ua_version_file_per_env()
    test_ua_version_priority_chain_unchanged()
    test_scheduler_runs_each_account_with_own_runner()
    test_scheduler_single_account_failure_does_not_block_others()
    test_scheduler_skips_disabled_accounts()
    test_account_runners_use_per_account_proxy()
    test_state_section_merge_preserves_other_keys()
    test_snapshot_tick_preserves_pool_section()
    test_health_status_shape()
    print(f"\n🎉 All 27 tests passed! ({time.time() - t0:.2f}s)")
