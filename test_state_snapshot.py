#!/usr/bin/env python3
"""test_state_snapshot.py — 状态快照（logs/state.json）与访问日志。

背景（为什么要有这两样）：
    用户反馈「日志里只有启动信息，看不到谁连了、连了什么、上游怎么回的」。
    查下来是三个盲区：
      1. `/v1/models` 完全没日志 —— 客户端报「获取模型失败」时无法确认它到底有没有打进来；
      2. 401 在进端点前就返回了，端点内的日志天然记不到；
      3. 没有任何地方记录客户端 IP，容器里分不清是谁在用。
    于是加了两层：
      - **访问日志中间件**：每个请求一行（IP / 方法 / 路径 / 状态码 / 耗时 / 模型 / UA）；
      - **状态快照文件**：把「可用模型 + 积分余额 + 任务状态」落成 logs/state.json，
        省得为了看一眼而翻几万行流水账。

覆盖：
  1. build_snapshot 纯函数：字段齐全、模型清单、积分优先级、任务摘要
  2. snapshot_path：日志文件 / 目录 / 显式 .json / 无路径
  3. write_atomic：内容正确、不残留 .tmp、失败返回 False 不抛
  4. SnapshotWriter：request 触发写、before_build 被调、失败不影响写盘
  5. 访问日志：记 IP 与状态码、跳过 /health、401 也记、模型名进日志
  6. CheckinScheduler.refresh_points：不签到、只查余额、异常收敛
  7. TaskScheduler.on_result：每跑完一类任务回调一次，回调抛异常不带崩排程

直接运行：python3 test_state_snapshot.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import state_snapshot as ss  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import checkin as checkin_mod  # noqa: E402
import converter  # noqa: E402
import task_scheduler as ts_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 1. build_snapshot（纯函数）
# ---------------------------------------------------------------------------

def test_build_snapshot_shape():
    """基本字段：schema / 时间 / 环境 / 五大区块。"""
    snap = ss.build_snapshot(now=1700000000)
    assert snap["schema"] == ss.SCHEMA_VERSION
    assert snap["updated_ts"] == 1700000000
    assert snap["updated_at"], "updated_at 不该为空"
    assert "models" in snap and "points" in snap
    assert snap["models"]["count"] == 0 and snap["models"]["ids"] == []
    assert snap["points"] is None, "没有任何来源时 points 应为 None"
    print("✅ test_build_snapshot_shape")


def test_build_snapshot_models():
    """模型清单：条数按 ids 长度算，来源与错误原样带出。"""
    snap = ss.build_snapshot(
        env="intl",
        models={"ids": ["a", "b", "c"], "source": "upstream", "at": 1700000000},
    )
    assert snap["env"] == "intl"
    m = snap["models"]
    assert m["count"] == 3 and m["ids"] == ["a", "b", "c"]
    assert m["source"] == "upstream"
    assert m["fetched_at"], "有 at 时 fetched_at 应被格式化"
    assert m["error"] is None

    # ids 缺失也不该炸
    snap2 = ss.build_snapshot(models={"source": "static"})
    assert snap2["models"]["count"] == 0
    print("✅ test_build_snapshot_models")


def test_points_prefers_fresh_refresh_over_checkin_record():
    """积分优先级：refresh_points 的即时结果 > 最近一次签到附带的余额。

    为什么要分两种：签到只在 09/21 点跑，而旅行/活跃上报也会发积分 ——
    那些时点不刷新的话，快照上的数字会明显滞后。
    """
    checkin_status = {
        "enabled": True,
        "hours": [9, 21],
        "next_fire_at": "2026-09-20 09:00:00",
        # 签到时的旧余额
        "last": {"at": 1700000000, "balance": 100, "balance_ok": True, "accounts": [{"p": "old"}]},
        # 之后 refresh_points 拿到的新余额
        "points": {"at": 1700009000, "ok": True, "remain": 379, "accounts": [{"p": "new"}]},
    }
    snap = ss.build_snapshot(checkin=checkin_status)
    p = snap["points"]
    assert p["remain"] == 379, f"应取 refresh 的值，实为 {p['remain']}"
    assert p["source"] == "refresh"
    assert p["accounts"] == [{"p": "new"}]

    # 没有 refresh 结果时退回签到记录
    checkin_status.pop("points")
    snap2 = ss.build_snapshot(checkin=checkin_status)
    p2 = snap2["points"]
    assert p2["remain"] == 100
    assert p2["source"] == "checkin"
    print("✅ test_points_prefers_fresh_refresh_over_checkin_record")


def test_build_snapshot_tasks_and_checkin_blocks():
    """任务摘要只保留「跑过且有 summary」的；签到块保留开关与下次时点。"""
    tasks = {
        "alive": True,
        "next_wake_at": "2026-09-20 09:00:00",
        "next_wake_tasks": ["checkin", "travel"],
        "tasks": {
            "checkin": {"summary": "今日已签到", "ok": True},
            "travel": None,                                    # 没跑过
            "cat": {"summary": None, "ok": True},              # 跑过但无摘要
        },
    }
    checkin_status = {
        "enabled": True,
        "hours": [9, 21],
        "next_fire_at": "2026-09-20 09:00:00",
        "last": {"at": 1700000000, "ok": True, "already": True, "code": 10001},
    }
    snap = ss.build_snapshot(checkin=checkin_status, tasks=tasks)
    assert snap["tasks"]["last"] == {"checkin": "今日已签到"}, (
        f"只该留跑出摘要的任务，实为 {snap['tasks']['last']}"
    )
    assert snap["tasks"]["next_wake_tasks"] == ["checkin", "travel"]
    assert snap["checkin"]["enabled"] is True
    assert snap["checkin"]["last"]["already"] is True

    # 任务全没跑过 → last 为 None 而不是一堆 null
    snap2 = ss.build_snapshot(tasks={"tasks": {"checkin": None, "travel": None}})
    assert snap2["tasks"]["last"] is None
    print("✅ test_build_snapshot_tasks_and_checkin_blocks")


# ---------------------------------------------------------------------------
# 2. 路径推导与原子写
# ---------------------------------------------------------------------------

def test_snapshot_path_derivation():
    """日志文件 → 同目录 state.json；目录 → 目录下；显式 .json → 原样；None → None。

    重点覆盖「路径还不存在」的情况：开机第一次跑时日志文件尚未创建，
    只靠 is_dir 判断会把它当目录，得出 `converter.log/state.json` 这种错路径。
    """
    assert ss.snapshot_path("/logs/converter.log") == Path("/logs/state.json")
    assert ss.snapshot_path("converter.log") == Path("state.json")
    assert ss.snapshot_path("/logs") == Path("/logs/state.json")
    assert ss.snapshot_path("/volume1/my.docker/data") == Path(
        "/volume1/my.docker/data/state.json"
    )
    assert ss.snapshot_path("/logs/my.json") == Path("/logs/my.json")
    assert ss.snapshot_path(None) is None
    assert ss.snapshot_path("") is None

    # 真实存在的目录优先按目录处理
    with tempfile.TemporaryDirectory() as d:
        assert ss.snapshot_path(d) == Path(d) / "state.json"
    print("✅ test_snapshot_path_derivation")


def test_write_atomic_roundtrip_and_no_residue():
    """写入内容正确、可被 json 读回，且不残留 .tmp。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "state.json"
        assert ss.write_atomic(p, {"a": 1, "中文": "值"}) is True
        assert json.loads(p.read_text(encoding="utf-8"))["中文"] == "值"
        assert not (p.parent / "state.json.tmp").exists(), "不该残留临时文件"
        # 覆盖写也要成功（os.replace 顶替）
        assert ss.write_atomic(p, {"a": 2}) is True
        assert json.loads(p.read_text(encoding="utf-8"))["a"] == 2
    print("✅ test_write_atomic_roundtrip_and_no_residue")


def test_write_atomic_failure_is_silent():
    """写不进去时返回 False，**不抛异常** —— 观测失败不能带崩主流程。"""
    with tempfile.TemporaryDirectory() as d:
        afile = Path(d) / "notadir"
        afile.write_text("x", encoding="utf-8")
        # 把文件当目录用 → 必然失败
        assert ss.write_atomic(afile / "state.json", {"a": 1}) is False
    print("✅ test_write_atomic_failure_is_silent")


# ---------------------------------------------------------------------------
# 3. SnapshotWriter
# ---------------------------------------------------------------------------

def test_writer_tick_writes_and_counts():
    """tick 写一次文件并累加统计；before_build 必须被调用。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        calls = []
        w = ss.SnapshotWriter(
            p,
            lambda: {"n": len(calls)},
            interval=0,
            before_build=lambda: calls.append(1),
        )
        assert w.tick() is True
        assert json.loads(p.read_text(encoding="utf-8"))["n"] == 1
        assert len(calls) == 1, "before_build 应被调用"
        assert w.writes == 1 and w.failures == 0
        assert w.status()["last_write_at"]
        print("✅ test_writer_tick_writes_and_counts")


def test_writer_survives_before_build_and_build_failure():
    """刷新失败、采集失败都不该中断 —— 前者照旧写盘，后者只计数。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"

        def boom():
            raise RuntimeError("refresh down")

        w = ss.SnapshotWriter(p, lambda: {"ok": True}, interval=0, before_build=boom)
        assert w.tick() is True, "刷新失败仍应写盘（用旧值也比没有强）"
        assert json.loads(p.read_text(encoding="utf-8"))["ok"] is True

        def build_boom():
            raise RuntimeError("collect down")

        w2 = ss.SnapshotWriter(p, build_boom, interval=0)
        assert w2.tick() is False
        assert w2.failures == 1 and w2.last_error
        print("✅ test_writer_survives_before_build_and_build_failure")


def test_writer_request_triggers_tick_in_thread():
    """request() 能唤醒后台线程写盘（事件驱动路径真的通）。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "state.json"
        w = ss.SnapshotWriter(p, lambda: {"v": 1}, interval=3600)
        w.start()
        try:
            w.request()
            for _ in range(60):  # 最多等 ~3s
                if p.exists():
                    break
                time.sleep(0.05)
            assert p.exists(), "request() 后文件应被写出"
        finally:
            w.stop()
            w.join(timeout=2)
        print("✅ test_writer_request_triggers_tick_in_thread")


# ---------------------------------------------------------------------------
# 4. 访问日志中间件
# ---------------------------------------------------------------------------

def _with_log(fn):
    """在临时日志文件下跑 fn，返回日志文本。用完恢复 CONFIG。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "access.log"
        saved = converter.CONFIG.get("log_path")
        converter.CONFIG["log_path"] = str(p)
        try:
            fn()
            return p.read_text(encoding="utf-8") if p.exists() else ""
        finally:
            converter.CONFIG["log_path"] = saved


def test_access_log_records_client_and_status():
    """开启 --log-requests 后：每个请求一行 IP / 方法 / 路径 / 状态码 / 耗时。"""
    saved_key = converter.CONFIG["api_key"]
    saved_env = converter.CONFIG["env"]
    saved_lr = converter.CONFIG["log_requests"]
    converter.CONFIG["api_key"] = ""
    converter.CONFIG["env"] = "intl"
    converter.CONFIG["log_requests"] = True

    def run():
        c = TestClient(converter.app)
        assert c.get("/v1/models").status_code == 200

    try:
        txt = _with_log(run)
    finally:
        converter.CONFIG["api_key"] = saved_key
        converter.CONFIG["env"] = saved_env
        converter.CONFIG["log_requests"] = saved_lr

    assert "GET /v1/models → 200" in txt, f"访问日志缺少请求行：\n{txt[-800:]}"
    assert "testclient" in txt.lower(), "应记录客户端 IP（TestClient 固定为 testclient）"
    assert "ms |" in txt, "应记录耗时"
    print("✅ test_access_log_records_client_and_status")


def test_access_log_default_is_quiet():
    """默认（不开 --log-requests）：成功请求不记行，只有异常请求（401 等）留下痕迹。

    「默认日志简单」是明确需求 —— 成功流量不打日志，排障线索（非 2xx）不能丢。
    """
    saved_key = converter.CONFIG["api_key"]
    saved_lr = converter.CONFIG["log_requests"]
    converter.CONFIG["api_key"] = "secret123"  # 有 key 才会有 401
    converter.CONFIG["log_requests"] = False

    def run():
        c = TestClient(converter.app)
        assert c.get("/v1/models").status_code == 401  # 未带 key → 401

    try:
        txt = _with_log(run)
    finally:
        converter.CONFIG["api_key"] = saved_key
        converter.CONFIG["log_requests"] = saved_lr

    assert "→ 200" not in txt, f"默认模式成功请求不应记录：\n{txt[-800:]}"
    assert "→ 401" in txt, f"401 是排障第一现场，默认必须记录：\n{txt[-800:]}"
    print("✅ test_access_log_default_is_quiet")


def test_access_log_skips_health():
    """/health 被 healthcheck 每 30s 打一次，必须跳过，否则把日志淹掉。"""
    saved_key = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = ""

    def run():
        c = TestClient(converter.app)
        assert c.get("/health").status_code == 200

    try:
        txt = _with_log(run)
    finally:
        converter.CONFIG["api_key"] = saved_key

    assert "/health" not in txt, f"/health 不该进访问日志：\n{txt}"
    print("✅ test_access_log_skips_health")


def test_access_log_records_401():
    """鉴权失败在进端点前就返回了，端点内记不到 —— 中间件必须补上这一笔。"""
    saved_key = converter.CONFIG["api_key"]
    converter.CONFIG["api_key"] = "secret123"

    def run():
        c = TestClient(converter.app)
        assert c.get("/v1/models").status_code == 401

    try:
        txt = _with_log(run)
    finally:
        converter.CONFIG["api_key"] = saved_key

    assert "GET /v1/models → 401" in txt, f"401 请求没被记录：\n{txt}"
    print("✅ test_access_log_records_401")


def test_access_log_includes_model_and_models_count():
    """聊天请求带 model=；/v1/models 记返回条数（排查「获取模型失败」的第一现场）。"""
    saved_key = converter.CONFIG["api_key"]
    saved_env = converter.CONFIG["env"]
    converter.CONFIG["api_key"] = ""
    converter.CONFIG["env"] = "intl"

    def run():
        c = TestClient(converter.app)
        c.get("/v1/models")

    try:
        txt = _with_log(run)
    finally:
        converter.CONFIG["api_key"] = saved_key
        converter.CONFIG["env"] = saved_env

    assert "[models] 返回" in txt, f"/v1/models 应记录条数：\n{txt[-800:]}"
    print("✅ test_access_log_includes_model_and_models_count")


def test_mark_model_lands_in_request_state():
    """_mark_model 写进 request.state，中间件据此把模型名带进日志行。"""
    class FakeReq:
        class state:  # noqa: N801
            pass

    r = FakeReq()
    converter._mark_model(r, "gemini-3.5-flash")
    assert r.state.model == "gemini-3.5-flash"
    # 传 None 也不该炸（count_tokens 之类可能拿不到模型名）
    converter._mark_model(r, None)
    assert r.state.model is None
    print("✅ test_mark_model_lands_in_request_state")


# ---------------------------------------------------------------------------
# 5. refresh_points：只查余额，不签到
# ---------------------------------------------------------------------------

def test_refresh_points_without_cred():
    """没凭据时不抛异常，落一条 ok=False 的记录。"""
    sch = checkin_mod.CheckinScheduler(None, enabled=False)
    pts = sch.refresh_points()
    assert pts["ok"] is False and pts["remain"] is None
    assert "凭据" in pts["msg"]
    print("✅ test_refresh_points_without_cred")


def test_refresh_points_does_not_check_in(monkeypatch_note=""):
    """**关键**：刷新余额绝不能触发签到（观测动作不能变成写操作）。"""
    calls = {"checkin": 0, "balance": 0}

    class FakeCred:
        def get_headers(self, within_ms=None):
            return {"x": "y"}

    def fake_do_checkin(*a, **kw):
        calls["checkin"] += 1
        raise AssertionError("refresh_points 不该调用 do_checkin")

    class FakeBal:
        ok = True
        remain = 379
        accounts = [{"package": "Bonus Pack", "remain": 250, "size": 250}]
        msg = None

    def fake_fetch_balance(*a, **kw):
        calls["balance"] += 1
        return FakeBal()

    orig_do, orig_bal = checkin_mod.do_checkin, checkin_mod.fetch_balance
    checkin_mod.do_checkin = fake_do_checkin
    checkin_mod.fetch_balance = fake_fetch_balance
    try:
        sch = checkin_mod.CheckinScheduler(FakeCred(), enabled=True)
        pts = sch.refresh_points()
        assert calls["checkin"] == 0, "不该有签到调用"
        assert calls["balance"] == 1, "应恰好查一次余额"
        assert pts["ok"] is True and pts["remain"] == 379
        assert pts["accounts"][0]["package"] == "Bonus Pack"
        # 与 last 分离：不该污染签到记录
        assert sch.last is None, "refresh_points 不能改 last"
        # 但 status() 要把它带出来
        assert sch.status()["points"]["remain"] == 379
    finally:
        checkin_mod.do_checkin, checkin_mod.fetch_balance = orig_do, orig_bal
    print("✅ test_refresh_points_does_not_check_in")


def test_refresh_points_error_is_captured():
    """查询抛异常 → ok=False 并带原因，不向外抛。"""

    class BoomCred:
        def get_headers(self, within_ms=None):
            raise RuntimeError("token 坏了")

    sch = checkin_mod.CheckinScheduler(BoomCred(), enabled=True)
    pts = sch.refresh_points()
    assert pts["ok"] is False
    assert "token 坏了" in pts["msg"]
    print("✅ test_refresh_points_error_is_captured")


# ---------------------------------------------------------------------------
# 6. TaskScheduler.on_result 钩子
# ---------------------------------------------------------------------------

def test_task_scheduler_on_result_hook():
    """每跑完一类任务回调一次，参数是 (key, 结果字典)。"""
    got = []

    class FakeRunner:
        def run_once(self, reason="manual"):
            return {"ok": True, "summary": "假装成功"}

    cfg = ts_mod.TaskConfig()
    sched = ts_mod.TaskScheduler(
        cred=None,
        config=cfg,
        runners={"checkin": FakeRunner()},
        on_result=lambda k, r: got.append((k, r)),
    )
    sched._run_kind("checkin", "manual")
    assert len(got) == 1
    k, r = got[0]
    assert k == "checkin" and r.get("ok") is True
    print("✅ test_task_scheduler_on_result_hook")


def test_task_scheduler_on_result_failure_is_contained():
    """回调抛异常不能带崩排程线程 —— 它只是观测用途。"""
    logs = []

    class FakeRunner:
        def run_once(self, reason="manual"):
            return {"ok": True, "summary": "ok"}

    def boom(_k, _r):
        raise RuntimeError("snapshot 炸了")

    sched = ts_mod.TaskScheduler(
        cred=None,
        config=ts_mod.TaskConfig(),
        runners={"checkin": FakeRunner()},
        on_result=boom,
        log=logs.append,
    )
    oc = sched._run_kind("checkin", "manual")  # 不该抛
    assert oc.ok is True
    assert any("回调异常" in x for x in logs), "应记一条回调异常日志"
    print("✅ test_task_scheduler_on_result_failure_is_contained")


def test_brief_text_collapses_and_truncates():
    """`brief_text` 压成单行并截断 —— 防止 HTML 错误页灌进日志与快照。

    实测过：billing 域出错返回一整页 openresty 的 HTML 401，原样记进 msg 后
    一条日志被撑成十几行，状态快照的 points.msg 也彻底没法看。
    """
    html = (
        "<html>\r\n<head><title>401 Authorization Required</title></head>\r\n"
        "<body>\r\n<center><h1>401 Authorization Required</h1></center>\r\n"
        "<hr><center>openresty</center>\r\n</body>\r\n</html>"
    )
    out = checkin_mod.brief_text(html)
    assert "\n" not in out and "\r" not in out, "必须压成单行"
    assert "401 Authorization Required" in out, "判因关键信息要留住"
    assert "openresty" in out
    # 超长要截断并带省略号
    long = checkin_mod.brief_text("x" * 500)
    assert len(long) <= 161 and long.endswith("…")
    # 空值不炸
    assert checkin_mod.brief_text(None) == ""
    assert checkin_mod.brief_text("") == ""
    print("✅ test_brief_text_collapses_and_truncates")


def test_growth_api_envelope_briefs_html_error():
    """growth_api 解不出 JSON 信封时也要压成单行（travel / cat 的报错走这条）。"""
    import growth_api

    code, msg, data = growth_api._parse_envelope(401, "<html>\n<title>401</title>\n</html>")
    assert code is None and data is None
    assert "\n" not in msg and "401" in msg, f"应为单行摘要，实为 {msg!r}"
    print("✅ test_growth_api_envelope_briefs_html_error")


if __name__ == "__main__":
    t0 = time.time()
    test_build_snapshot_shape()
    test_build_snapshot_models()
    test_points_prefers_fresh_refresh_over_checkin_record()
    test_build_snapshot_tasks_and_checkin_blocks()
    test_snapshot_path_derivation()
    test_write_atomic_roundtrip_and_no_residue()
    test_write_atomic_failure_is_silent()
    test_writer_tick_writes_and_counts()
    test_writer_survives_before_build_and_build_failure()
    test_writer_request_triggers_tick_in_thread()
    test_access_log_records_client_and_status()
    test_access_log_default_is_quiet()
    test_access_log_skips_health()
    test_access_log_records_401()
    test_access_log_includes_model_and_models_count()
    test_mark_model_lands_in_request_state()
    test_refresh_points_without_cred()
    test_refresh_points_does_not_check_in()
    test_refresh_points_error_is_captured()
    test_task_scheduler_on_result_hook()
    test_task_scheduler_on_result_failure_is_contained()
    test_brief_text_collapses_and_truncates()
    test_growth_api_envelope_briefs_html_error()
    print(f"\n🎉 All 22 tests passed! ({time.time() - t0:.2f}s)")
