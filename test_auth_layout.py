#!/usr/bin/env python3
"""test_auth_layout.py — 验证国内版 / 国际版凭据「分开放置」的定位逻辑。

背景（为什么要专门的测试）：
    国内版与国际版是**两个账号的两份 token**，凭据文件名也不同
    （`workbuddy-desktop.info` vs `Tencent-Cloud.coding-copilot.info`）。
    改造前 `find_auth_file()` 是 `sorted(d.glob("*.info"))[0]` —— 同一个目录里放两份
    就是按文件名字典序碰运气，取错了必然全链路 401（网关层的 HTML 401，看不出真因）。
    所以「分开放置」不是整洁偏好，是正确性要求。

覆盖：
  1. 环境专属目录命中：<根>/cn 与 <根>/intl 各取各的
  2. 同目录多凭据：按 `auth.domain` 认领，而不是按文件名排序
  3. --auth-cn / --auth-intl（CODEBUDDY2OPENAI_AUTH_CN / _INTL）可直指文件
  4. 老部署兼容：目录里只有一份 .info 时行为与改造前一致
  5. `CODEBUDDY_AUTH_DIR` 仍然最高优先级
  6. 认不出域的凭据不冒充任何环境（退回兜底）

直接运行：python3 test_auth_layout.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

import environments  # noqa: E402
from environments import (  # noqa: E402
    AUTH_ENV_VARS,
    AUTH_SUBDIRS,
    CN,
    INTL,
    auth_subdir,
    resolve_auth_path,
)

# 引 converter 只为了拿 auth_dirs / find_auth_file；它 import fastapi 等重依赖，
# 环境不全时给一条明确的跳过说明而不是崩栈。
try:
    import converter  # noqa: E402
except Exception as e:  # noqa: BLE001
    print(f"⚠️ 跳过 test_auth_layout：无法 import converter（{e}）")
    sys.exit(0)


#: 需要临时改写的环境变量全集。
_ENV_KEYS = (
    "CODEBUDDY_AUTH_DIR",
    environments.ENV_VAR_ENV,
    AUTH_ENV_VARS[CN],
    AUTH_ENV_VARS[INTL],
    "LOCALAPPDATA",
    "XDG_DATA_HOME",
)


class _Sandbox:
    """临时目录 + 环境变量隔离；退出时清目录、还原环境变量。"""

    def __init__(self, **kw):
        self._kw = kw
        self._saved: dict[str, str | None] = {}
        self.root: Path | None = None

    def __enter__(self):
        self.root = Path(tempfile.mkdtemp(prefix="authtest-"))
        for k in _ENV_KEYS:
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        # 隔离平台默认目录，避免误读到本机真实凭据
        os.environ["LOCALAPPDATA"] = str(self.root / "no-such-localappdata")
        os.environ["XDG_DATA_HOME"] = str(self.root / "no-such-xdg")
        for k, v in self._kw.items():
            if v is None:
                continue  # 已在上面 pop 过，语义就是「保持未设置」
            os.environ[k] = str(v) if isinstance(v, Path) else v
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self.root and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        return False


def _write_auth(p: Path, domain: str, nickname: str = "tester") -> Path:
    """落一份最小可用的假凭据（结构对齐真实 auth 文件）。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "auth": {
                    "accessToken": "fake-token",
                    "refreshToken": "fake-refresh",
                    "domain": domain,
                    "expiresAt": 4102444800000,  # 2100 年，永不过期
                },
                "account": {"uid": "uid-" + nickname, "nickname": nickname},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# 1. 分环境子目录约定
# ---------------------------------------------------------------------------


def test_per_env_subdirs_are_isolated():
    """<根>/cn 与 <根>/intl 各取各的，互不干扰（NAS 推荐的部署形态）。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        root = sb.root
        cn_file = _write_auth(root / "cn" / "workbuddy-desktop.info", "www.codebuddy.cn", "cn")
        intl_file = _write_auth(
            root / "intl" / "Tencent-Cloud.coding-copilot.info", "www.codebuddy.ai", "intl"
        )
        os.environ["CODEBUDDY_AUTH_DIR"] = str(root)

        # 注意：两个子目录都在 CODEBUDDY_AUTH_DIR 下，靠 env 选目录
        got_cn = converter.find_auth_file(CN)
        got_intl = converter.find_auth_file(INTL)

        # 目录名是 cn/intl，但 find_auth_file 会校验 auth.domain 归属；
        # 这里 cn 目录里放的是 cn 凭据 → 必须命中它。
        assert got_cn is not None and got_cn.name == cn_file.name, f"cn 取错：{got_cn}"
        # intl 的候选目录列表里 <根>/intl 优先，命中 intl 凭据。
        assert got_intl is not None and got_intl.name == intl_file.name, f"intl 取错：{got_intl}"
        assert got_cn != got_intl, "两个环境绝不能拿到同一份凭据"
    print("✅ test_per_env_subdirs_are_isolated")


def test_auth_subdir_helper():
    """auth_subdir 按环境拼出 <根>/cn 或 <根>/intl。"""
    with _Sandbox():
        assert auth_subdir("/data/auth", CN).replace("\\", "/") == "/data/auth/cn"
        assert auth_subdir("/data/auth", INTL).replace("\\", "/") == "/data/auth/intl"
        assert AUTH_SUBDIRS == {CN: "cn", INTL: "intl"}
    print("✅ test_auth_subdir_helper")


# ---------------------------------------------------------------------------
# 2. 同目录多凭据 —— 按 auth.domain 认领
# ---------------------------------------------------------------------------


def test_same_dir_claims_by_domain_not_by_filename():
    """关键用例：两份凭据同处一个目录时，按 auth.domain 认领而不是文件名字典序。

    刻意让国内版文件名**排在前面**（`a-cn.info` < `z-intl.info`），
    如果实现是 sorted()[0]，intl 就会拿到国内版那份 → 本用例会失败。
    """
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        root = sb.root / "shared"
        _write_auth(root / "a-cn.info", "www.codebuddy.cn", "cn-acc")
        _write_auth(root / "z-intl.info", "www.codebuddy.ai", "intl-acc")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(root)

        got_cn = converter.find_auth_file(CN)
        got_intl = converter.find_auth_file(INTL)

        assert got_cn is not None and got_cn.name == "a-cn.info", f"cn 取错：{got_cn}"
        assert got_intl is not None and got_intl.name == "z-intl.info", (
            f"intl 应命中 z-intl.info（按 auth.domain 认领），实际 {got_intl}"
        )
    print("✅ test_same_dir_claims_by_domain_not_by_filename")


def test_unknown_domain_does_not_impersonate():
    """认不出域的凭据不冒充任何环境：退回该目录第一份，由调用方自行承担。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        root = sb.root / "weird"
        _write_auth(root / "only.info", "example.internal", "mystery")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(root)

        # 域认不出 → 退到 first_any，但至少不能抛异常
        got = converter.find_auth_file(INTL)
        assert got is not None and got.name == "only.info"
        assert converter._info_env(got) is None, "这份凭据本就不该被认出环境"
    print("✅ test_unknown_domain_does_not_impersonate")


# ---------------------------------------------------------------------------
# 3. 显式路径（--auth-cn / --auth-intl）
# ---------------------------------------------------------------------------


def test_explicit_auth_file_path():
    """CODEBUDDY2OPENAI_AUTH_INTL 直指一个 .info 文件时直接采信。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        f = _write_auth(sb.root / "somewhere" / "intl-acc.info", "www.codebuddy.ai", "intl")
        os.environ[AUTH_ENV_VARS[INTL]] = str(f)

        assert resolve_auth_path(INTL) == str(f)
        got = converter.find_auth_file(INTL)
        assert got == f, f"应直接采信显式文件，实际 {got}"
    print("✅ test_explicit_auth_file_path")


def test_explicit_dir_and_explicit_file_priority():
    """CODEBUDDY_AUTH_DIR（老变量）优先于分环境子目录约定。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        legacy = sb.root / "legacy"
        legacy_file = _write_auth(legacy / "workbuddy-desktop.info", "www.codebuddy.cn", "cn")
        # 同时存在新约定的子目录，且里面是一份 intl 凭据
        _write_auth(sb.root / "intl" / "Tencent-Cloud.coding-copilot.info", "www.codebuddy.ai")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(legacy)

        got = converter.find_auth_file(CN)
        assert got == legacy_file, f"老的 CODEBUDDY_AUTH_DIR 应优先，实际 {got}"
    print("✅ test_explicit_dir_and_explicit_file_priority")


# ---------------------------------------------------------------------------
# 4. 老部署兼容 & 5. 候选目录列表
# ---------------------------------------------------------------------------


def test_single_credential_legacy_behaviour():
    """老部署：目录里只有一份 .info → 行为与改造前完全一致（照取）。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        legacy = sb.root / "auth"
        f = _write_auth(legacy / "workbuddy-desktop.info", "www.codebuddy.cn", "cn")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(legacy)

        assert converter.find_auth_file(CN) == f
        # 即使声明成 intl 也仍然能拿到（唯一一份，没有更优选择）
        assert converter.find_auth_file(INTL) == f
    print("✅ test_single_credential_legacy_behaviour")


def test_auth_dirs_order_and_dedup():
    """候选目录顺序：显式路径 → 分环境子目录 → 平台默认；且不重复。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        root = sb.root / "auth"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["CODEBUDDY_AUTH_DIR"] = str(root)

        dirs = converter.auth_dirs(CN)
        as_str = [str(d) for d in dirs]
        assert len(as_str) == len(set(as_str)), f"候选目录有重复：{as_str}"
        assert as_str[0] == str(root), "显式 CODEBUDDY_AUTH_DIR 必须排第一"
        assert str(root / "cn") in as_str, "应包含分环境子目录约定"
        # 平台默认目录排在最后
        assert "CodeBuddyExtension" in as_str[-1]
    print("✅ test_auth_dirs_order_and_dedup")


def test_find_all_auth_files_lists_both():
    """find_all_auth_files 列出目录里全部凭据（供告警/auto 推断用）。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        root = sb.root / "auth"
        _write_auth(root / "a.info", "www.codebuddy.cn")
        _write_auth(root / "b.info", "www.codebuddy.ai")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(root)

        got = converter.find_all_auth_files(CN)
        names = {p.name for p in got}
        assert names == {"a.info", "b.info"}, f"应列出两份，实际 {names}"
    print("✅ test_find_all_auth_files_lists_both")


def test_read_auth_domain_is_readonly():
    """read_auth_domain 只读不写：调用后文件内容与 mtime 都不变。"""
    with _Sandbox() as sb:
        f = _write_auth(sb.root / "auth" / "x.info", "www.codebuddy.ai")
        before = f.read_text(encoding="utf-8")
        mtime = f.stat().st_mtime

        assert converter.read_auth_domain(f) == "www.codebuddy.ai"
        assert f.read_text(encoding="utf-8") == before, "只读探测不该改内容"
        assert f.stat().st_mtime == mtime, "只读探测不该改 mtime"
        assert converter.read_auth_domain(None) is None
    print("✅ test_read_auth_domain_is_readonly")


# ---------------------------------------------------------------------------
# 6. auto 场景：环境由凭据自己决定
# ---------------------------------------------------------------------------


def test_auto_infers_intl_from_intl_credential():
    """--env auto 时，凭据目录里的 intl 凭据应把环境定成 intl。"""
    with _Sandbox(CODEBUDDY_AUTH_DIR=None) as sb:
        f = _write_auth(sb.root / "auth" / "intl.info", "www.codebuddy.ai", "intl")
        os.environ["CODEBUDDY_AUTH_DIR"] = str(sb.root / "auth")

        # 模拟 main() 里 auto 的两轮解析逻辑
        probe = converter.find_auth_file(environments.resolve_env("auto"))
        assert probe is not None
        inferred = environments.env_of_domain(converter.read_auth_domain(probe))
        assert inferred == INTL, f"应从凭据域推出 intl，实际 {inferred}"

        # 环境定了之后再取凭据，仍然命中同一份
        assert converter.find_auth_file(inferred) == f
    print("✅ test_auto_infers_intl_from_intl_credential")


if __name__ == "__main__":
    test_per_env_subdirs_are_isolated()
    test_auth_subdir_helper()
    test_same_dir_claims_by_domain_not_by_filename()
    test_unknown_domain_does_not_impersonate()
    test_explicit_auth_file_path()
    test_explicit_dir_and_explicit_file_priority()
    test_single_credential_legacy_behaviour()
    test_auth_dirs_order_and_dedup()
    test_find_all_auth_files_lists_both()
    test_read_auth_domain_is_readonly()
    test_auto_infers_intl_from_intl_credential()
    print("\n全部通过 ✅")
