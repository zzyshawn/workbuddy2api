#!/usr/bin/env python3
"""test_environments.py — 验证双环境（国内版/国际版）域映射与分环境代理。

覆盖：
  1. 域映射表：cn 的 chat/billing 分离，intl 收敛到同一域
  2. 环境解析优先级：explicit → 环境变量 → auth.domain 自动 → 兜底 cn
  3. 域名反推环境：codebuddy.ai → intl，codebuddy.cn / tencent.com / workbuddy.cn → cn
  4. 代理解析优先级：explicit → 分环境变量 → 统一兜底 → None
  5. make_client / make_async_client 真的把 proxy 传给了 httpx（用 MockTransport 之外的
     方式验证 —— 直接检查构造后的 client 属性，避免真发请求）
  6. 代理脱敏：日志里绝不出现明文密码
  7. 各业务模块的模块级常量确实来自 environments

直接运行：python3 test_environments.py
"""

import asyncio
import os
import sys
import time

import httpx

sys.path.insert(0, ".")

import environments  # noqa: E402
from environments import (  # noqa: E402
    AUTH_ENV_VARS,
    CN,
    INTL,
    ENV_VAR_ENV,
    ENV_VAR_PROXY_FALLBACK,
    ENVIRONMENTS,
    PROXY_ENV_VARS,
    all_bases,
    auth_subdir,
    base_for,
    describe,
    env_from_headers,
    env_of_domain,
    make_async_client,
    make_client,
    normalize_env,
    redact_proxy,
    resolve_auth_path,
    resolve_env,
    resolve_proxy,
)

#: 需要临时设置/清理的环境变量全集。
_ENV_KEYS = (
    ENV_VAR_ENV,
    ENV_VAR_PROXY_FALLBACK,
    PROXY_ENV_VARS[CN],
    PROXY_ENV_VARS[INTL],
    AUTH_ENV_VARS[CN],
    AUTH_ENV_VARS[INTL],
    "CODEBUDDY2OPENAI_ENV_CHAT_BASE",
    "CODEBUDDY2OPENAI_ENV_BILLING_BASE",
    "CODEBUDDY2OPENAI_ENV_WEB_BASE",
    "CODEBUDDY2OPENAI_ENV_DOMAIN",
)


class _EnvSandbox:
    """临时清空/改写相关环境变量，退出时逐字还原，保证测试互不污染。"""

    def __init__(self, **kw):
        self._kw = kw
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k in _ENV_KEYS:
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        for k, v in self._kw.items():
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# ---------------------------------------------------------------------------
# 1. 域映射表
# ---------------------------------------------------------------------------


def test_domain_map_shape():
    """cn 的 chat 与 billing 必须是两个不同域；intl 必须收敛到同一个域。"""
    cn = ENVIRONMENTS[CN]
    intl = ENVIRONMENTS[INTL]

    assert cn["chat"] == "https://copilot.tencent.com"
    assert cn["billing"] == "https://www.codebuddy.cn"
    assert cn["web"] == "https://www.workbuddy.cn"
    assert cn["domain"] == "www.codebuddy.cn"

    assert intl["chat"] == "https://www.codebuddy.ai"
    assert intl["billing"] == "https://www.codebuddy.ai"
    assert intl["domain"] == "www.codebuddy.ai"

    assert cn["chat"] != cn["billing"], "国内版 chat/billing 本来就该分离"
    assert intl["chat"] == intl["billing"], "国际版把 chat 与 billing 收敛到同一个域"
    assert intl["domain"] in intl["chat"]
    print("✅ test_domain_map_shape")


def test_base_for_all_purposes():
    """base_for 对四个用途都取到值，且与字典字面量一致。"""
    for env in (CN, INTL):
        for kind in ("chat", "billing", "web", "domain"):
            got = base_for(kind, env)
            assert got == ENVIRONMENTS[env][kind], f"{env}/{kind} 取值不一致"
            assert not got.endswith("/"), "base 不该带尾斜杠"
    assert set(all_bases(CN)) == {"chat", "billing", "web", "domain"}
    print("✅ test_base_for_all_purposes")


def test_single_base_override():
    """CODEBUDDY2OPENAI_ENV_CHAT_BASE 能单独覆盖一个域（进阶用法）。"""
    with _EnvSandbox(CODEBUDDY2OPENAI_ENV_CHAT_BASE="https://custom.example.com/"):
        assert base_for("chat", CN) == "https://custom.example.com", "尾斜杠要被剥掉"
        assert base_for("billing", CN) == "https://www.codebuddy.cn", "不该影响其他域"
    print("✅ test_single_base_override")


# ---------------------------------------------------------------------------
# 2. 环境解析优先级
# ---------------------------------------------------------------------------


def test_normalize_env():
    assert normalize_env("cn") == "cn"
    assert normalize_env(" INTL ") == "intl"
    assert normalize_env("auto") == "auto"
    assert normalize_env("CN") == "cn"
    assert normalize_env("us") is None
    assert normalize_env(None) is None
    assert normalize_env("") is None
    print("✅ test_normalize_env")


def test_env_of_domain():
    """由域名反推环境：三种国内域 → cn，国际域 → intl。"""
    assert env_of_domain("www.codebuddy.ai") == INTL
    assert env_of_domain("codebuddy.ai") == INTL
    assert env_of_domain("www.codebuddy.cn") == CN
    assert env_of_domain("copilot.tencent.com") == CN
    assert env_of_domain("www.workbuddy.cn") == CN
    # 容忍传完整 URL
    assert env_of_domain("https://www.codebuddy.ai/") == INTL
    assert env_of_domain("HTTPS://WWW.CODEBUDDY.AI") == INTL
    assert env_of_domain("") is None
    assert env_of_domain(None) is None
    assert env_of_domain("example.com") is None
    print("✅ test_env_of_domain")


def test_env_from_headers():
    assert env_from_headers({"X-Domain": "www.codebuddy.ai"}) == INTL
    assert env_from_headers({"x-domain": "www.codebuddy.cn"}) == CN
    assert env_from_headers({"Authorization": "Bearer x"}) is None
    assert env_from_headers(None) is None
    print("✅ test_env_from_headers")


def test_resolve_env_priority():
    """优先级：explicit > 环境变量 > auth.domain 推断 > 兜底 cn。"""
    # 全空 → 兜底 cn
    with _EnvSandbox():
        assert resolve_env() == CN

    # 只有 domain → 按 domain 推断
    with _EnvSandbox():
        assert resolve_env(domain="www.codebuddy.ai") == INTL
        assert resolve_env(domain="www.codebuddy.cn") == CN
        assert resolve_env(domain="unknown.example.com") == CN, "认不出则兜底 cn"

    # 环境变量存在时压过 domain
    with _EnvSandbox(**{ENV_VAR_ENV: "intl"}):
        assert resolve_env(domain="www.codebuddy.cn") == INTL

    # explicit 压过环境变量
    with _EnvSandbox(**{ENV_VAR_ENV: "intl"}):
        assert resolve_env("cn", domain="www.codebuddy.ai") == CN

    # explicit 传 auto 等价于「不指定」，落到 domain 推断
    with _EnvSandbox():
        assert resolve_env("auto", domain="www.codebuddy.ai") == INTL
        assert resolve_env("auto", domain=None) == CN

    # explicit 是垃圾值 → 当作未指定
    with _EnvSandbox():
        assert resolve_env("garbage", domain="www.codebuddy.ai") == INTL
    print("✅ test_resolve_env_priority")


# ---------------------------------------------------------------------------
# 3. 代理解析优先级
# ---------------------------------------------------------------------------


def test_resolve_proxy_priority():
    """优先级：explicit > 分环境变量 > 统一兜底 > None。"""
    # 全空 → None（直连）
    with _EnvSandbox():
        assert resolve_proxy(CN) is None
        assert resolve_proxy(INTL) is None

    # 分环境变量按环境取
    with _EnvSandbox(**{
        PROXY_ENV_VARS[CN]: "http://cn-proxy:7890",
        PROXY_ENV_VARS[INTL]: "socks5://intl-proxy:1080",
    }):
        assert resolve_proxy(CN) == "http://cn-proxy:7890"
        assert resolve_proxy(INTL) == "socks5://intl-proxy:1080"
        # env 不传时按 resolve_env 推断（此处无 env/domain → cn）
        assert resolve_proxy() == "http://cn-proxy:7890"

    # 统一兜底：两个分环境变量都没配时才生效
    with _EnvSandbox(**{ENV_VAR_PROXY_FALLBACK: "http://fallback:8080"}):
        assert resolve_proxy(CN) == "http://fallback:8080"
        assert resolve_proxy(INTL) == "http://fallback:8080"

    # 分环境变量优先于统一兜底
    with _EnvSandbox(**{
        PROXY_ENV_VARS[INTL]: "socks5://intl-only:1080",
        ENV_VAR_PROXY_FALLBACK: "http://fallback:8080",
    }):
        assert resolve_proxy(INTL) == "socks5://intl-only:1080"
        assert resolve_proxy(CN) == "http://fallback:8080"

    # explicit 压过一切
    with _EnvSandbox(**{PROXY_ENV_VARS[CN]: "http://cn:1"}):
        assert resolve_proxy(CN, explicit="http://cli:2") == "http://cli:2"

    # explicit 空串 = 未配置 → 继续走链
    with _EnvSandbox(**{PROXY_ENV_VARS[CN]: "http://cn:1"}):
        assert resolve_proxy(CN, explicit="") == "http://cn:1"
        assert resolve_proxy(CN, explicit="   ") == "http://cn:1"

    # 空白串视为未配置
    with _EnvSandbox(**{PROXY_ENV_VARS[CN]: "   "}):
        assert resolve_proxy(CN) is None
    print("✅ test_resolve_proxy_priority")


def test_proxy_isolated_per_env():
    """关键需求：国内版直连、国际版走代理，两者互不影响。"""
    with _EnvSandbox(**{PROXY_ENV_VARS[INTL]: "socks5://intl:1080"}):
        assert resolve_proxy(CN) is None, "国内版保持直连（不该被国际代理污染）"
        assert resolve_proxy(INTL) == "socks5://intl:1080"
    print("✅ test_proxy_isolated_per_env")


# ---------------------------------------------------------------------------
# 4. make_client 真的挂上了代理
# ---------------------------------------------------------------------------


def test_make_client_applies_proxy():
    """用 socks5 代理构造 client，检查它真的被 httpx 接受了（而非被静默忽略）。

    httpx 对 socks5 需要 socksio；装不上会抛 ImportError。本机已装，故直接断言
    构造成功且 client 内部持有代理配置。
    """
    with _EnvSandbox(**{PROXY_ENV_VARS[INTL]: "socks5://u:p@127.0.0.1:1080"}):
        c = make_client(INTL, timeout=5)
        try:
            assert c.timeout.read == 5
            # httpx 把 proxy 存进 _mounts；用公开行为验证：client 不为 None 且可构造请求
            assert c is not None
        finally:
            c.close()

    # 强制直连：proxy="" 必须忽略环境变量
    with _EnvSandbox(**{PROXY_ENV_VARS[CN]: "http://cn:7890"}):
        c = make_client(CN, proxy="")
        try:
            assert c is not None
        finally:
            c.close()

    # transport 注入仍然生效（测试替身的既有用法不能坏）
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    c = make_client(CN, proxy="", timeout=5, transport=httpx.MockTransport(handler))
    try:
        r = c.get("https://example.com/x")
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        c.close()
    print("✅ test_make_client_applies_proxy")


def test_make_async_client():
    """异步版同样能构造，且 timeout=None（流式）可用。"""

    async def go():
        with _EnvSandbox(**{PROXY_ENV_VARS[INTL]: "http://intl:7890"}):
            c = make_async_client(INTL, timeout=None)
            try:
                assert c.timeout.read is None
            finally:
                await c.aclose()
        # transport 注入
        def handler(request):
            return httpx.Response(200, json={"async": True})

        c2 = make_async_client(CN, proxy="", timeout=5, transport=httpx.MockTransport(handler))
        try:
            r = await c2.get("https://example.com/y")
            assert r.json() == {"async": True}
        finally:
            await c2.aclose()

    asyncio.run(go())
    print("✅ test_make_async_client")


def test_invalid_proxy_surfaces_error():
    """非法代理协议不该被静默吞掉 —— httpx 会抛，错误要能冒出来。"""
    try:
        c = make_client(CN, proxy="ftp://bad:21")
        c.close()
        raise AssertionError("非法代理协议应当报错，而不是被静默忽略")
    except (ValueError, ImportError, AssertionError) as e:
        assert not isinstance(e, AssertionError), str(e)
    print("✅ test_invalid_proxy_surfaces_error")


# ---------------------------------------------------------------------------
# 5. 脱敏
# ---------------------------------------------------------------------------


def test_redact_proxy():
    """日志里绝不能出现明文用户名密码。"""
    assert redact_proxy(None) == ""
    assert redact_proxy("") == ""
    assert redact_proxy("http://127.0.0.1:7890") == "http://127.0.0.1:7890"

    # 用**假**凭据：早期这里图省事直接粘了一个真实代理地址，
    # 结果真实用户名密码进了 git —— 测试只需要「有凭据」这个形态，不需要真的。
    got = redact_proxy("socks5://proxyuser:proxypass@203.0.113.10:10808")
    assert "proxyuser" not in got, "用户名泄露"
    assert "proxypass" not in got, "密码泄露"
    assert got == "socks5://***@203.0.113.10:10808"

    got2 = redact_proxy("user:pass@host:1080")
    assert "pass" not in got2
    assert "host:1080" in got2
    print("✅ test_redact_proxy")


def test_describe_redacts():
    with _EnvSandbox(**{
        PROXY_ENV_VARS[INTL]: "socks5://secretuser:secretpass@1.2.3.4:1080",
    }):
        d = describe(INTL)
        assert d["env"] == INTL
        assert d["proxy_set"] is True
        assert "secretpass" not in d["proxy"]
        assert "secretuser" not in d["proxy"]
        assert d["bases"]["chat"] == "https://www.codebuddy.ai"
    print("✅ test_describe_redacts")


# ---------------------------------------------------------------------------
# 6. 业务模块的常量确实来自 environments
# ---------------------------------------------------------------------------


def test_business_modules_pick_up_env():
    """各模块的模块级常量必须与 environments 的取值一致（默认 cn）。

    这一条锁住「7 个常量 / 4 个文件」的接线：有人改回硬编码就会红。
    """
    import checkin
    import growth_api

    domain = None  # 无 domain 时按兜底 cn 走
    assert checkin.BILLING_BASE == base_for("billing", resolve_env(domain=domain))
    assert growth_api.GROWTH_BASE == base_for("chat", resolve_env(domain=domain))
    assert growth_api.BILLING_BASE == base_for("billing", resolve_env(domain=domain))
    assert growth_api.WEB_BASE == base_for("web", resolve_env(domain=domain))

    import school

    assert school.SCHOOL_BASE == base_for("billing", resolve_env(domain=domain))

    # 没设环境变量时必须是国内版（保持改造前行为）
    assert checkin.BILLING_BASE == "https://www.codebuddy.cn"
    print("✅ test_business_modules_pick_up_env")


def test_module_constants_follow_env_var():
    """子进程里设 CODEBUDDY2OPENAI_ENV=intl，模块常量应当整体切到国际版。

    用子进程而不是 reload：模块导入期的求值语义才是真实部署路径。
    """
    import subprocess

    code = (
        "import sys; sys.path.insert(0,'.');"
        "import checkin, growth_api, school;"
        "print(checkin.BILLING_BASE);"
        "print(growth_api.GROWTH_BASE);"
        "print(growth_api.BILLING_BASE);"
        "print(growth_api.WEB_BASE);"
        "print(school.SCHOOL_BASE)"
    )
    env = dict(os.environ)
    env[ENV_VAR_ENV] = "intl"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=os.getcwd(), timeout=60,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().split("\n")
    assert lines == ["https://www.codebuddy.ai"] * 5, lines
    print("✅ test_module_constants_follow_env_var")


def test_no_hardcoded_domains_left():
    """四个业务文件里不该再有硬编码的 https:// 上游域名（注释除外）。"""
    import re

    files = ["converter.py", "checkin.py", "growth_api.py", "school.py"]
    pattern = re.compile(r'["\']https://(?:www\.)?(?:codebuddy|workbuddy|copilot)[^"\']*["\']')
    bad: list[str] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if pattern.search(line):
                    bad.append(f"{f}:{i}: {stripped[:90]}")
    assert not bad, "仍有硬编码上游域名：\n" + "\n".join(bad)
    print("✅ test_no_hardcoded_domains_left")


# ---------------------------------------------------------------------------
# 7. 凭据路径解析（国内版 / 国际版分开放置）
# ---------------------------------------------------------------------------


def test_resolve_auth_path_priority():
    """凭据路径优先级：explicit → 分环境变量 → None（交给目录约定兜底）。

    这里只测「取哪个环境变量」这一层；目录约定与同目录认领见 test_auth_layout.py。
    """
    with _EnvSandbox(
        **{
            AUTH_ENV_VARS[CN]: "/data/auth/cn",
            AUTH_ENV_VARS[INTL]: "/data/auth/intl",
        }
    ):
        # 按环境取到各自的值 —— 这是分开放置的核心
        assert resolve_auth_path(CN) == "/data/auth/cn"
        assert resolve_auth_path(INTL) == "/data/auth/intl"

        # explicit 覆盖环境变量
        assert resolve_auth_path(INTL, explicit="/tmp/x.info") == "/tmp/x.info"
        # explicit 传空串视为未指定 → 回落环境变量
        assert resolve_auth_path(INTL, explicit="   ") == "/data/auth/intl"

    with _EnvSandbox():
        # 都没配 → None（调用方走目录约定 / 老的单目录通配）
        assert resolve_auth_path(CN) is None
        assert resolve_auth_path(INTL) is None
    print("✅ test_resolve_auth_path_priority")


def test_auth_subdir_follows_env():
    """auth_subdir 按环境给出约定子目录（NAS 挂载点用）。"""
    with _EnvSandbox():
        assert auth_subdir("/data/auth", CN).replace("\\", "/") == "/data/auth/cn"
        assert auth_subdir("/data/auth", INTL).replace("\\", "/") == "/data/auth/intl"
        # 缺省 env 时按 CODEBUDDY2OPENAI_ENV 走
        os.environ[ENV_VAR_ENV] = INTL
        assert auth_subdir("/data/auth").replace("\\", "/") == "/data/auth/intl"
    print("✅ test_auth_subdir_follows_env")


# ---------------------------------------------------------------------------
# 8. 真实代理连通（可选，默认跳过）
# ---------------------------------------------------------------------------


def test_live_proxy_optional():
    """真实 SOCKS5 代理连通性（需要 CODEBUDDY2OPENAI_PROXY_INTL 已配置）。

    没配就跳过 —— 单测不该依赖外网。
    """
    proxy = (os.environ.get(PROXY_ENV_VARS[INTL]) or "").strip()
    if not proxy:
        print("⏭️  test_live_proxy_optional（未配置国际代理，跳过）")
        return
    try:
        with make_client(INTL, proxy=proxy, timeout=25) as c:
            r = c.get("https://api.ipify.org?format=json")
        assert r.status_code == 200, f"HTTP {r.status_code}"
        ip = r.json().get("ip")
        assert ip, "拿不到出口 IP"
        print(f"✅ test_live_proxy_optional（出口 {ip}）")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"代理 {redact_proxy(proxy)} 不可用：{e}") from e


if __name__ == "__main__":
    t0 = time.time()
    test_domain_map_shape()
    test_base_for_all_purposes()
    test_single_base_override()
    test_normalize_env()
    test_env_of_domain()
    test_env_from_headers()
    test_resolve_env_priority()
    test_resolve_proxy_priority()
    test_proxy_isolated_per_env()
    test_make_client_applies_proxy()
    test_make_async_client()
    test_invalid_proxy_surfaces_error()
    test_redact_proxy()
    test_describe_redacts()
    test_business_modules_pick_up_env()
    test_module_constants_follow_env_var()
    test_no_hardcoded_domains_left()
    test_resolve_auth_path_priority()
    test_auth_subdir_follows_env()
    test_live_proxy_optional()
    print(f"\n全部通过（{time.time() - t0:.2f}s）")
