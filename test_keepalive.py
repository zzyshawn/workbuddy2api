#!/usr/bin/env python3
"""test_keepalive.py — 验证 token 保活：强制刷新窗口、连续失败阈值与恢复。

直接运行：python3 test_keepalive.py
"""

import sys

sys.path.insert(0, ".")

from keepalive import (  # noqa: E402
    FORCE_REFRESH_WINDOW_MS,
    SESSION_DEAD_THRESHOLD,
    KeepaliveRunner,
    looks_session_dead,
)


class _Cred:
    """假凭据管理器：可切换成功/失败，并记录被要求的预刷新窗口。"""

    def __init__(self, fail_msg=None, expires_at=1789491600000):
        self.fail_msg = fail_msg
        self.expires_at = expires_at
        self.windows: list[int] = []
        self.refreshes = 0

    def get_headers(self, within_ms=60_000):
        self.windows.append(within_ms)
        if self.fail_msg:
            raise RuntimeError(self.fail_msg)
        self.refreshes += 1
        return {"Authorization": "Bearer t", "X-User-Id": "u-1"}

    def summary(self):
        return {"token_expires_at": self.expires_at}


def test_force_refresh_window_is_larger_than_any_token_lifetime():
    """保活靠「窗口大到必然命中」实现强制刷新，不需要给 CredentialManager 加方法。"""
    cred = _Cred()
    r = KeepaliveRunner(cred)
    oc = r.run_once(reason="22:00")
    assert oc.ok and cred.refreshes == 1
    assert cred.windows == [FORCE_REFRESH_WINDOW_MS], cred.windows
    assert FORCE_REFRESH_WINDOW_MS > 400 * 24 * 3600 * 1000 - 1
    assert oc.detail["token_expires_at"] == 1789491600000
    assert "新有效期至" in oc.summary, oc.summary
    print("✅ test_force_refresh_window_is_larger_than_any_token_lifetime")


def test_consecutive_failures_need_threshold_before_session_dead():
    """一次失败不判死（可能是网络抖动），连续 3 次才判 session 失效。"""
    cred = _Cred(fail_msg="刷新 token 网络失败：connect timeout")
    r = KeepaliveRunner(cred)

    oc1 = r.run_once()
    assert not oc1.ok and r.consecutive_failures == 1 and r.session_dead is False
    r.run_once()
    assert r.consecutive_failures == 2 and r.session_dead is False

    oc3 = r.run_once()
    assert not oc3.ok and r.consecutive_failures == 3 and r.session_dead is True
    assert "已连续 3 次失败" in oc3.summary and "重新登录桌面端" in oc3.summary, oc3.summary
    assert r.status()["session_dead"] is True

    # 恢复正常：计数清零 + 从失效状态恢复
    cred.fail_msg = None
    oc4 = r.run_once()
    assert oc4.ok and r.consecutive_failures == 0 and r.session_dead is False
    assert "已从 session 失效状态恢复" in oc4.summary
    print("✅ test_consecutive_failures_need_threshold_before_session_dead")


def test_session_dead_marker_short_circuits_threshold():
    """上游明确报 session 失效（12153）时不必等满 3 次，直接提示重新登录。"""
    cred = _Cred(fail_msg="刷新 token 失败：{'code': 12153, 'msg': 'session dead'}")
    r = KeepaliveRunner(cred)
    oc = r.run_once()
    assert not oc.ok and r.session_dead is True and r.consecutive_failures == 1
    assert "session 失效" in oc.summary, oc.summary
    assert oc.detail["session_dead_hint"] is True
    print("✅ test_session_dead_marker_short_circuits_threshold")


def test_session_dead_keyword_detection():
    assert looks_session_dead("code=12153")
    assert looks_session_dead("Session Dead")
    assert looks_session_dead("invalid_grant")
    assert not looks_session_dead("connect timeout")
    assert not looks_session_dead("")
    assert SESSION_DEAD_THRESHOLD == 3
    print("✅ test_session_dead_keyword_detection")


def test_missing_credential_and_expiry_fallback():
    r = KeepaliveRunner(None)
    oc = r.run_once()
    assert not oc.ok and "登录凭据" in oc.summary

    class _NoSummary(_Cred):
        def summary(self):
            raise RuntimeError("读不到")

    cred = _NoSummary()
    oc2 = KeepaliveRunner(cred).run_once()
    assert oc2.ok and oc2.detail["token_expires_at"] == 0
    print("✅ test_missing_credential_and_expiry_fallback")


def test_default_hours_are_22():
    import keepalive

    assert keepalive.DEFAULT_KEEPALIVE_HOURS == (22,)
    print("✅ test_default_hours_are_22")


if __name__ == "__main__":
    import time

    t0 = time.time()
    test_force_refresh_window_is_larger_than_any_token_lifetime()
    test_consecutive_failures_need_threshold_before_session_dead()
    test_session_dead_marker_short_circuits_threshold()
    test_session_dead_keyword_detection()
    test_missing_credential_and_expiry_fallback()
    test_default_hours_are_22()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
