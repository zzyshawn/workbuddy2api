#!/usr/bin/env python3
"""account_pool.py — 多账号池：账号级代理绑定、账号级故障切号、首选持久化。

对应上游 Go 实现 `internal/pool/` + `internal/server/handler.go` 的
`applyErrorPolicy()`。按任务书 R2/R3/R5 裁剪移植：

* 选号 = 首选（default_uid，若可用）→ 其余按 priority 升序。
  上游是四因子加权随机（credits/EMA/防惊群），本需求明确不要 ——
  两个实例的账号本来就没几个，加权是过度设计。
* 故障处置只做「账号级故障」两种（见 classify_account_fault）：
  不要 429 软冷却 / 402 硬冷却到次日 04:00 / 404 固定冷却 / 熔断器 /
  模型级负缓存 —— 那些超出本次范围，加了反而让行为不可预测。
* 状态（禁用 / 禁用原因 / 首选 / 计数）原子落盘到**现有** state 文件
  （state-intl.json / state-cn.json）的 `account_pool` 段里，
  与 state_snapshot.SnapshotWriter 共用一把文件锁，避免两个写者互相覆盖。

依赖方向：本模块**不 import converter**（避免循环导入）。
凭据管理器（CredentialManager）由装配处（converter.main）以 `cred_factory`
回调注入；日志与状态变更通知同样回调注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: 账号配置文件的环境变量覆盖（CLI 参数 --accounts-file 同名）。
ACCOUNTS_FILE_ENV_KEY = "CODEBUDDY2OPENAI_ACCOUNTS_FILE"

#: 配置文件名（建议名）。实际按环境派生：accounts-intl.json / accounts-cn.json，
#: 找不到派生名时回落到 accounts.json（向后兼容）。
ACCOUNTS_FILE_BASENAME = "accounts.json"

#: state 文件里账号池状态段的键名。
STATE_SECTION = "account_pool"


# ---------------------------------------------------------------------------
# 账号级故障分类（照抄上游 internal/upstream/client.go 的语义，务必精确）
# ---------------------------------------------------------------------------
#
# 为什么必须按 msg 关键词判、不能按 code 判：
#   code 11140 同时承载两种文案 —— "request illegal"（账号级授权封禁，
#   需重新 OAuth 登录才能恢复）和 "The model provider is rate-limiting
#   requests."（模型级限流，必须保持软冷却/换号重试，不能当封号处理）。
#   按 code 一刀切会把限流误判成封号，把好号永久禁用（上游实测注释原话）。
#
# 上游词表与匹配模式（client.go:133-194）：
#   sessionDeadRule  (matchExact，大小写敏感): "Offline user session not found", "12153"
#   accountFaultRule (matchFold，大小写不敏感): "request illegal",
#       "trial not activated", "trial version is not yet activated"
#   判定顺序：session_dead 先于 account_fault 先于 soft_rate。
#
# 本移植的取舍：
#   "trial not activated"（14017）在上游走软冷却、不禁用 —— 超出本次范围，
#   这里不把它归为账号级故障（保持原有错误透传行为），注释里记一笔，
#   免得后人以为漏了词。

#: session 失效（大小写敏感，照抄上游 matchExact）。
_SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")

#: 账号授权封禁（大小写不敏感，照抄上游 matchFold）。
_ACCOUNT_BANNED_MARKERS = ("request illegal",)

#: 11140 的模型级限流变体 —— 命中它说明「不是账号故障」，
#: classify 必须返回 None（保持原有行为：错误透传 / 网络重试，不禁用不换号）。
_RATE_LIMIT_TEXT_MARKER = "rate-limiting requests"


def classify_account_fault(http_status: int, body: bytes | str | None) -> str | None:
    """判定一次上游失败是否为「账号级故障」。

    返回禁用原因文案；返回 None 表示不是账号级故障（调用方保持原有行为）。

    规则（照抄上游，顺序也有语义）：
      1. body 含 "Offline user session not found" / "12153"（大小写敏感）→ session 失效
      2. body 含 "request illegal"（大小写不敏感）→ 账号授权封禁（11140）
    注意：只看 msg 文案，不看 code / HTTP 状态 —— 11140 的 code 也承载模型级限流文案，
    那种必须保持原有处理，不能当账号故障。
    """
    if body is None:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if not text:
        return None
    # 大小写敏感（上游 matchExact）：session 失效的文案是上游固定写法。
    for marker in _SESSION_DEAD_MARKERS:
        if marker in text:
            return f"session 失效（{marker}），需重新登录"
    # 大小写不敏感（上游 matchFold）。
    lowered = text.lower()
    for marker in _ACCOUNT_BANNED_MARKERS:
        if marker in lowered:
            return "账号被上游授权封禁（request illegal，需重新 OAuth 登录才能恢复）"
    return None


class AccountFaultError(Exception):
    """同一请求内向上游试了某个账号，结果撞上账号级故障。

    由各协议路径的单次尝试函数抛出，外层的换号循环捕获后
    调 pool.mark_account_fault() 禁用并换下一个账号。
    """

    def __init__(self, uid: str, reason: str, http_status: int | None = None,
                 body: bytes | None = None):
        super().__init__(f"account {uid}: {reason}")
        self.uid = uid
        self.reason = reason
        self.http_status = http_status
        self.body = body


class RateLimitedError(Exception):
    """429 限流（**非**账号级故障）：不禁号，但本次请求应换下一个账号。

    与 AccountFaultError 的本质区别：11140 是账号被上游封禁（永久禁用，
    只有重新登录才能恢复），而 429 是「这个号暂时打太快」—— 号本身是好的，
    换别的号继续、原号保留。池里一圈全 429 时由调用方短退避后重试
    （CONFIG["rate_limit_retry"] 轮），用尽才把 429 透传给客户端。
    """

    def __init__(self, uid: str, http_status: int | None = None,
                 body: bytes | None = None):
        super().__init__(f"account {uid}: rate limited (429)")
        self.uid = uid
        self.http_status = http_status
        self.body = body


# ---------------------------------------------------------------------------
# state 文件并发写保护
# ---------------------------------------------------------------------------
#
# 为什么需要：state-intl.json 有两个写者 —— 本池的 save_state()（禁用/换首选时
# 同步写）和 state_snapshot.SnapshotWriter.tick()（周期/事件写全量快照）。
# 两边都是 read-modify-write + 原子 replace；不加锁的话，A 读→B 写→A 写
# 会把 B 的变更吞掉（比如禁用刚落盘就被快照覆盖回去，重启后复活）。
# 锁按文件路径区分（intl/cn 各一把），只保护「读→改→写」临界区，
# 不保护网络等慢操作。

_state_file_locks: dict[str, threading.RLock] = {}
_state_file_locks_guard = threading.Lock()


def state_file_lock(path: str | os.PathLike) -> threading.RLock:
    """取某个 state 文件的写锁（进程内按路径单例）。"""
    key = str(path)
    with _state_file_locks_guard:
        lock = _state_file_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _state_file_locks[key] = lock
        return lock


def merge_state_section(path: str | os.PathLike, key: str, section: dict) -> bool:
    """把 `section` 合并进 state 文件的顶层 `key` 段，原子写回。

    read-modify-write 全程持有 state_file_lock：与 SnapshotWriter.tick()
    互斥，任一写者都不会吞掉另一方的段。失败返回 False（不抛异常，
    状态落盘不能反过来影响主流程）。
    """
    p = Path(path)
    lock = state_file_lock(p)
    with lock:
        data: dict = {}
        try:
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except (OSError, ValueError):
            data = {}
        data[key] = section
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return True
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return False


def read_state_section(path: str | os.PathLike, key: str) -> dict | None:
    """读 state 文件里某个顶层段；文件不存在/解析失败返回 None。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    section = raw.get(key)
    return section if isinstance(section, dict) else None


# ---------------------------------------------------------------------------
# 账号条目
# ---------------------------------------------------------------------------


def _token_fp(auth_path: Path | None) -> str | None:
    """读 auth 文件里 accessToken 的指纹（sha256 前 16 字符）。

    用途：禁用时记录「当时是哪个登录态」；之后文件里的 token 变了，
    说明用户重新做过 OAuth 登录 → 可以解禁。直接读文件原文，
    不走 CredentialManager（不能触发自动刷新 —— 刷新会改 token，
    那样每次自动续期都会把封禁中的号"复活"，就全乱了）。
    token 值本身绝不记录，只记指纹。
    """
    if auth_path is None:
        return None
    try:
        raw = json.loads(Path(auth_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = ((raw.get("auth") or {}).get("accessToken") or "")
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _read_uid(auth_path: Path | None) -> str | None:
    """从 auth 文件读 account.uid（accounts.json 里 uid 省略时用）。"""
    if auth_path is None:
        return None
    try:
        raw = json.loads(Path(auth_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ((raw.get("account") or {}).get("uid") or None) or None


@dataclass
class PooledAccount:
    """池中的一个账号。

    proxy 语义（与 environments._proxy_kwargs 一致）：
      None  → 走现有的 resolve_proxy 链（环境变量 → 兜底）
      ""    → 强制直连（忽略环境变量）
      URL   → 用这个代理
    """

    uid: str
    auth_path: Path
    proxy: str | None
    priority: int = 0
    enabled: bool = True
    cred: object = None  # CredentialManager（由 cred_factory 构造）
    disabled: bool = False
    disabled_reason: str | None = None
    disabled_at: float | None = None
    disabled_token_fp: str | None = field(default=None, repr=False)
    requests: int = 0
    account_faults: int = 0

    @property
    def available(self) -> bool:
        """能否参与选号：配置启用 且 未被禁用。"""
        return self.enabled and not self.disabled

    def status(self) -> dict:
        d: dict = {
            "uid": self.uid,
            "auth": str(self.auth_path),
            "priority": self.priority,
            "enabled": self.enabled,
            "disabled": self.disabled,
            "proxy_configured": self.proxy not in (None,),
            "proxy_forced_direct": self.proxy == "",
            "requests": self.requests,
            "account_faults": self.account_faults,
        }
        if self.disabled:
            d["disabled_reason"] = self.disabled_reason
            d["disabled_at"] = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.disabled_at))
                if self.disabled_at else None
            )
        # 代理 URL 含密码，绝不进状态/日志。
        return d


# ---------------------------------------------------------------------------
# 账号池
# ---------------------------------------------------------------------------


class AccountPool:
    """多账号池。

    配置（accounts.json，热加载）：
        {
          "default_uid": "<首选账号 uid>",
          "accounts": [
            {"uid": "...", "auth": "auth/intl/a.info",
             "proxy": "socks5://user:pass@host:port", "priority": 1, "enabled": true}
          ]
        }
      - auth：相对路径按**配置文件所在目录**解析（/data/accounts-intl.json 里写
        "auth/intl/a.info" → /data/auth/intl/a.info）。
      - uid 可省略 → 从 auth 文件的 account.uid 读。
      - proxy 缺省 → 走 environments.resolve_proxy 链；"" → 强制直连。
      - priority 缺省 0；enabled 缺省 true。

    accounts.json 缺失 → 优雅降级为单账号（legacy_auth_path + default_proxy），
    老部署升级不崩（验收自查第 3 条）。
    """

    def __init__(
        self,
        *,
        env: str | None,
        config_path: str | os.PathLike | None,
        state_path: str | os.PathLike | None,
        default_proxy: str | None,
        legacy_auth_path: str | os.PathLike | None,
        cred_factory: Callable[[Path, str | None], object],
        log: Callable[[str], None] | None = None,
        on_change: Callable[[], None] | None = None,
    ):
        self.env = env
        self.config_path = Path(config_path) if config_path else None
        self.state_path = Path(state_path) if state_path else None
        self.default_proxy = default_proxy
        self.legacy_auth_path = Path(legacy_auth_path) if legacy_auth_path else None
        self._cred_factory = cred_factory
        self._log = log or (lambda _m: None)
        #: 状态变化回调（禁用/换首选/配置重载后触发）。装配处用它请求一次
        #: 状态快照刷新 —— 只做观测用途，回调异常不能影响池本身。
        self._on_change = on_change
        self._lock = threading.RLock()
        self.accounts: dict[str, PooledAccount] = {}
        self.default_uid: str | None = None
        self.multi_mode: bool = False
        self._config_mtime: float | None = None
        self._config_default_uid: str | None = None
        with self._guarded():
            # 顺序不能错：先加载配置，再从 state 文件恢复运行时状态（禁用/首选），
            # 最后才落盘 —— 否则初始加载的 save 会把要恢复的旧状态覆盖掉
            # （禁用重启后"复活"，验收自查第 4 条的反例）。
            self._load_config_locked(initial=True)
            self._restore_state_locked()
            self._save_state_locked()

    def _guarded(self):
        """写状态的临界区：先文件锁、后池锁。

        锁序必须全局一致（文件锁 → 池锁）：SnapshotWriter.tick() 持文件锁
        写盘时 build() 会调 pool.status() 拿池锁；若池这边反过来（池锁 → 文件锁），
        两个线程各持一把互相等 —— 经典死锁。state_file_lock 是 RLock，
        同线程在 _save_state_locked → merge_state_section 里重入无碍。
        state_path 为 None（纯内存测试）时退化为只拿池锁。
        """
        from contextlib import ExitStack, nullcontext

        stack = ExitStack()
        if self.state_path is not None:
            stack.enter_context(state_file_lock(self.state_path))
        else:
            stack.enter_context(nullcontext())
        stack.enter_context(self._lock)
        return stack

    # ---------------------------------------------------------- 配置加载

    def _read_config_file(self) -> tuple[dict | None, float | None]:
        """读配置文件；返回 (内容, mtime)。文件不存在/解析失败返回 (None, mtime|None)。"""
        if self.config_path is None:
            return None, None
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return None, None
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._log(f"[pool] WARN 账号配置文件解析失败（{self.config_path}）：{e}，保持现有配置")
            return None, mtime
        if not isinstance(raw, dict):
            self._log(f"[pool] WARN 账号配置文件顶层不是对象：{self.config_path}，保持现有配置")
            return None, mtime
        return raw, mtime

    def _resolve_auth(self, auth: str) -> Path:
        p = Path(auth)
        if not p.is_absolute() and self.config_path is not None:
            p = self.config_path.parent / p
        return p

    def _build_account(self, entry: dict, fallback_uid: str | None = None) -> PooledAccount | None:
        """由一条配置构造 PooledAccount（含 CredentialManager）。失败返回 None 并记日志。"""
        auth_raw = (entry.get("auth") or "").strip()
        if not auth_raw:
            self._log(f"[pool] WARN 账号条目缺 auth，已跳过：{entry}")
            return None
        auth_path = self._resolve_auth(auth_raw)
        uid = (entry.get("uid") or "").strip() or _read_uid(auth_path) or fallback_uid
        if not uid:
            self._log(f"[pool] WARN 读不到 uid 且未配置 uid，已跳过：{auth_raw}")
            return None
        try:
            priority = int(entry.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        # proxy 三态：缺省 None（走 resolve 链）/ "" 强制直连 / URL 原样用。
        proxy = entry.get("proxy", None)
        if proxy is not None:
            proxy = str(proxy)
        try:
            cred = self._cred_factory(auth_path, proxy)
        except Exception as e:  # noqa: BLE001 — 单个账号构造失败不能带崩整个池
            self._log(f"[pool] WARN 账号 {uid} 凭据管理器构造失败：{e}，已跳过")
            return None
        return PooledAccount(
            uid=uid,
            auth_path=auth_path,
            proxy=proxy,
            priority=priority,
            enabled=bool(entry.get("enabled", True)),
            cred=cred,
        )

    def _load_config_locked(self, initial: bool = False) -> bool:
        """（持锁）加载/重载配置。返回配置是否发生变化。"""
        raw, mtime = self._read_config_file()
        if raw is None:
            if initial:
                self._enter_legacy_locked()
            return False
        if not initial and mtime == self._config_mtime:
            return False
        entries = raw.get("accounts") or []
        if not isinstance(entries, list):
            entries = []
        new_accounts: dict[str, PooledAccount] = {}
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            acct = self._build_account(e, fallback_uid=f"account-{i}")
            if acct is None:
                continue
            if acct.uid in new_accounts:
                self._log(f"[pool] WARN 配置里 uid 重复（{acct.uid}），只保留第一条")
                continue
            new_accounts[acct.uid] = acct
        if not new_accounts:
            self._log(f"[pool] WARN 配置文件里没有可用账号条目：{self.config_path}，保持现有配置")
            return False
        # 合并运行时状态：已存在的 uid 保留禁用/计数（热加载不能把"刚禁用的号"复活）。
        for uid, acct in new_accounts.items():
            old = self.accounts.get(uid)
            if old is not None:
                acct.disabled = old.disabled
                acct.disabled_reason = old.disabled_reason
                acct.disabled_at = old.disabled_at
                acct.disabled_token_fp = old.disabled_token_fp
                acct.requests = old.requests
                acct.account_faults = old.account_faults
        dropped = [u for u in self.accounts if u not in new_accounts]
        self.accounts = new_accounts
        self.multi_mode = True
        self._config_mtime = mtime
        # default_uid：文件里显式改了才跟进（用户手工编辑是明确意图）；
        # 否则保持运行时已有的（可能是故障切换后的新首选）。
        cfg_default = (raw.get("default_uid") or "").strip() or None
        if cfg_default != self._config_default_uid:
            self._config_default_uid = cfg_default
            if cfg_default and cfg_default in self.accounts and self.accounts[cfg_default].available:
                if self.default_uid != cfg_default:
                    self._log(f"[pool] 配置变更：首选账号 → {short_uid(cfg_default)}")
                self.default_uid = cfg_default
            elif cfg_default:
                self._log(f"[pool] WARN 配置的 default_uid={cfg_default} 不可用（不存在/已禁用），保持当前首选")
        if self.default_uid not in self.accounts:
            # 首选指向了已删除的账号 → 回落到优先级最高的可用账号
            nxt = self._pick_first_locked()
            self.default_uid = nxt.uid if nxt else None
        if dropped:
            self._log(f"[pool] 配置重载：移除账号 {', '.join(short_uid(u) for u in dropped)}")
        self._log(
            f"[pool] 已加载 {len(self.accounts)} 个账号"
            f"（{self.config_path.name}），首选={short_uid(self.default_uid)}"
        )
        if not initial:
            # 热重载后落盘（合并了旧运行时状态）；初始加载不落盘 ——
            # __init__ 里恢复旧状态之后会统一落一次。
            self._save_state_locked()
        return True

    def _enter_legacy_locked(self) -> None:
        """降级为单账号模式：沿用装配处已解析出的凭据文件与代理（老部署行为）。"""
        self.multi_mode = False
        self._config_mtime = None
        if self.legacy_auth_path is None:
            self._log("[pool] 未找到账号配置文件，也没有 legacy 凭据：池为空（服务将 503）")
            return
        uid = _read_uid(self.legacy_auth_path) or "default"
        try:
            cred = self._cred_factory(self.legacy_auth_path, self.default_proxy)
        except Exception as e:  # noqa: BLE001
            self._log(f"[pool] ERR legacy 凭据管理器构造失败：{e}")
            return
        self.accounts = {
            uid: PooledAccount(
                uid=uid,
                auth_path=self.legacy_auth_path,
                proxy=self.default_proxy,
                priority=0,
                enabled=True,
                cred=cred,
            )
        }
        self.default_uid = uid
        self._log(
            f"[pool] 单账号模式（未找到 {self.config_path.name if self.config_path else 'accounts.json'}），"
            f"沿用 {self.legacy_auth_path.name}（uid={short_uid(uid)}）"
        )

    def maybe_reload(self) -> bool:
        """配置文件 mtime 变了就重读。热加载：改配置不用重启容器。"""
        with self._guarded():
            changed = self._load_config_locked(initial=False)
        if changed:
            self._notify_change()
        return changed

    # ---------------------------------------------------------- 状态持久化

    def _state_dict_locked(self) -> dict:
        return {
            "default_uid": self.default_uid,
            "config_file": str(self.config_path) if self.config_path else None,
            "multi_mode": self.multi_mode,
            "accounts": {
                uid: {
                    "disabled": a.disabled,
                    "disabled_reason": a.disabled_reason,
                    "disabled_at": a.disabled_at,
                    "disabled_at_text": (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.disabled_at))
                        if a.disabled_at else None
                    ),
                    "priority": a.priority,
                    "enabled": a.enabled,
                    "auth": str(a.auth_path),
                    # 代理 URL 含密码，绝不落盘；只记"是否配了账号级代理"。
                    "proxy_configured": a.proxy not in (None,),
                    "requests": a.requests,
                    "account_faults": a.account_faults,
                }
                for uid, a in self.accounts.items()
            },
        }

    def _save_state_locked(self) -> None:
        """（持锁）把账号状态合并进 state 文件。无 state 路径时静默跳过。"""
        if self.state_path is None:
            return
        ok = merge_state_section(self.state_path, STATE_SECTION, self._state_dict_locked())
        if not ok:
            self._log(f"[pool] WARN 账号状态落盘失败：{self.state_path}")

    def save_state(self) -> None:
        with self._guarded():
            self._save_state_locked()

    def _restore_state_locked(self) -> None:
        """（持锁）从 state 文件恢复禁用状态与首选。文件缺失/损坏 → 零状态启动。"""
        if self.state_path is None:
            return
        section = read_state_section(self.state_path, STATE_SECTION)
        if not section:
            return
        saved_accounts = section.get("accounts")
        # 防御（2026-10-14 启动崩溃回归修复）：accounts 必须是 dict（uid → 状态）。
        # 2026-10-14 实测：state_snapshot 曾把 status() 的 **list** 形态写进同一段，
        # 快照后写覆盖池的 dict → 重启恢复时 list.items() AttributeError → 容器
        # 启动循环崩溃。恢复失败绝不能带崩服务 —— 跳过恢复，零状态启动，
        # save_state() 会用正确格式重写该段。键名冲突已在 state_snapshot 侧修复
        # （快照观测副本改用 account_pool_status 键）。
        if not isinstance(saved_accounts, dict):
            self._log(
                "[pool] WARN state 里的账号池段格式异常"
                f"（accounts 为 {type(saved_accounts).__name__}），跳过恢复（零状态启动）"
            )
            return
        restored = 0
        for uid, st in saved_accounts.items():
            acct = self.accounts.get(uid)
            if acct is None or not isinstance(st, dict):
                continue
            if st.get("disabled"):
                acct.disabled = True
                acct.disabled_reason = st.get("disabled_reason")
                acct.disabled_at = st.get("disabled_at")
                acct.requests = int(st.get("requests") or 0)
                acct.account_faults = int(st.get("account_faults") or 0)
                restored += 1
        saved_default = (section.get("default_uid") or "").strip() or None
        if saved_default and saved_default in self.accounts and self.accounts[saved_default].available:
            self.default_uid = saved_default
        elif saved_default and saved_default in self.accounts:
            # 首选在上次运行结束时是被禁用的 → 保持禁用，下面的回落会另选可用账号
            pass
        if restored or saved_default:
            self._log(
                f"[pool] 从状态文件恢复：{restored} 个禁用账号，首选={short_uid(self.default_uid)}"
            )
        # 回落：首选不可用（被禁用/已删除）→ 选优先级最高的可用账号
        if self.default_uid not in self.accounts or not self.accounts[self.default_uid].available:
            nxt = self._pick_first_locked()
            if nxt and self.default_uid != nxt.uid:
                self._log(f"[pool] 保存的首选不可用，回落首选 → {short_uid(nxt.uid)}")
                self.default_uid = nxt.uid

    # ---------------------------------------------------------- 选号

    def _pick_first_locked(self) -> PooledAccount | None:
        """（持锁）按 priority 升序取第一个可用账号（uid 二级排序保证确定性）。"""
        cands = [a for a in self.accounts.values() if a.available]
        if not cands:
            return None
        cands.sort(key=lambda a: (a.priority, a.uid))
        return cands[0]

    def _ordered_locked(self) -> list[PooledAccount]:
        """（持锁）选号顺序：首选（若可用）→ 其余按 priority 升序。

        首选语义优先于 priority：即使首选的 priority 不是最小，它也排第一。
        """
        cands = [a for a in self.accounts.values() if a.available]
        cands.sort(key=lambda a: (a.priority, a.uid))
        default_acct = self.accounts.get(self.default_uid or "")
        if default_acct is not None and default_acct.available:
            return [default_acct] + [a for a in cands if a.uid != default_acct.uid]
        return cands

    def ordered_available(self) -> list[PooledAccount]:
        """当前可用账号的选号顺序快照。每次调用前先检查配置热加载。"""
        self.maybe_reload()
        with self._lock:
            return self._ordered_locked()

    def has_accounts(self) -> bool:
        with self._lock:
            return bool(self.accounts)

    def default_account(self) -> PooledAccount | None:
        """首选账号（可用才返回；/health 与 legacy CONFIG['cred'] 用）。"""
        with self._lock:
            acct = self.accounts.get(self.default_uid or "")
            if acct is not None and acct.available:
                return acct
            return self._pick_first_locked()

    def get(self, uid: str) -> PooledAccount | None:
        with self._lock:
            return self.accounts.get(uid)

    # ---------------------------------------------------------- 故障处置

    def mark_account_fault(self, uid: str, reason: str, http_status: int | None = None) -> bool:
        """禁用一个账号（账号级故障）。

        上游语义（handler.go:1002-1015）：11140 request illegal 是**永久禁用**
        （软冷却到期也不会自动恢复，到期后重新选号只会再撞 403 浪费一次轮换），
        需重新 OAuth 登录才能恢复。12153 session 失效同理（session 已死，
        冷却救不活）。
        若被禁用的是首选，自动把 default_uid 改指下一个可用账号并落盘
        （用户原话：「默认也改成账号2」）。
        幂等：并发请求同时撞到同一故障只记一次。
        """
        with self._guarded():
            acct = self.accounts.get(uid)
            if acct is None or acct.disabled:
                return False
            acct.disabled = True
            acct.disabled_reason = reason
            acct.disabled_at = time.time()
            acct.disabled_token_fp = _token_fp(acct.auth_path)
            acct.account_faults += 1
            moved_to = None
            if self.default_uid == uid:
                nxt = self._pick_first_locked()
                self.default_uid = nxt.uid if nxt else None
                moved_to = self.default_uid
            self._save_state_locked()
        status_txt = f"HTTP {http_status} " if http_status else ""
        self._log(
            f"[pool] ⛔ 账号 {short_uid(uid)} 已禁用（{status_txt}{reason}）"
            + (f"；首选已切换为 {short_uid(moved_to)}" if moved_to else "；池中已无可用账号" if self.default_uid is None else "")
        )
        self._notify_change()
        return True

    def note_attempt(self, uid: str) -> None:
        """记一次"用该账号向上游发请求"（计数用，不触发任何处置）。

        为什么只计数：account_faults 在 mark_account_fault 里记；
        成功/失败明细看日志就够了，状态文件里放太多计数只是噪音。
        """
        with self._lock:
            acct = self.accounts.get(uid)
            if acct is not None:
                acct.requests += 1

    # ---------------------------------------------------------- 重新登录检测

    def poll_relogins(self) -> list[str]:
        """检查被禁用的账号是否重新登录过（auth 文件的 accessToken 变了）。

        只在签到循环里调用：签到是唯一"低成本探测"的入口。
        恢复的账号自动解禁并记日志；调用方随后用 task_accounts() 拿到它。
        注意：CredentialManager 的自动刷新不会触发这里 —— 被禁用的账号
        不参与任何请求，也就不会被刷新；指纹变化只可能来自用户手工重拷
        .info（= 重新 OAuth 登录），这正是解禁的合法条件。
        """
        recovered: list[str] = []
        with self._guarded():
            for uid, acct in self.accounts.items():
                if not acct.disabled or not acct.enabled:
                    continue
                if not acct.disabled_token_fp:
                    continue
                cur = _token_fp(acct.auth_path)
                if cur and cur != acct.disabled_token_fp:
                    acct.disabled = False
                    acct.disabled_reason = None
                    acct.disabled_at = None
                    acct.disabled_token_fp = None
                    recovered.append(uid)
            if recovered:
                self._save_state_locked()
        for uid in recovered:
            self._log(f"[pool] ✅ 账号 {short_uid(uid)} 检测到重新登录，自动解禁恢复可用")
        if recovered:
            self._notify_change()
        return recovered

    def task_accounts(self) -> list[PooledAccount]:
        """定时任务用的账号列表：enabled 且未被禁用，按选号顺序。"""
        self.maybe_reload()
        with self._lock:
            return self._ordered_locked()

    # ---------------------------------------------------------- 观测

    def status(self) -> dict:
        """账号池状态（进 state 快照与 /health）。代理 URL 绝不出现在这里。"""
        with self._lock:
            accounts = [a.status() for a in self._ordered_locked()]
            # 把禁用的也附上（排在后面），排查时能看到"为什么没被选"。
            seen = {a["uid"] for a in accounts}
            for uid in sorted(self.accounts):
                if uid not in seen:
                    accounts.append(self.accounts[uid].status())
            return {
                "multi_mode": self.multi_mode,
                "config_file": str(self.config_path) if self.config_path else None,
                "total": len(self.accounts),
                "available": sum(1 for a in self.accounts.values() if a.available),
                "default_uid": self.default_uid,
                "accounts": accounts,
            }

    def _notify_change(self) -> None:
        cb = self._on_change
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001 — 观测回调绝不能影响池
            pass


def short_uid(uid: str | None) -> str:
    """uid 脱敏缩写（日志用）：取前 8 字符，避免全 uid 刷屏。"""
    if not uid:
        return "(无)"
    s = str(uid)
    return s if len(s) <= 12 else s[:8] + "…"


def default_accounts_path(auth_dir: str | os.PathLike | None, env: str | None) -> str | None:
    """默认账号配置文件路径：auth 目录旁的 accounts-<env>.json。

    双实例共用 /data/auth 卷，所以文件名必须按环境区分，否则 intl 容器会
    读到 cn 的账号（容器隔离了 realm，但文件是共享的）。
    派生名不存在时回落到 accounts.json（向后兼容）；都不存在也返回派生名
    （调用方据此打印"去哪里放文件"的提示，池本身优雅降级为单账号）。
    """
    if not auth_dir:
        return None
    parent = Path(auth_dir).parent
    e = (env or "").strip() or "default"
    per_env = parent / f"accounts-{e}.json"
    if per_env.exists():
        return str(per_env)
    legacy = parent / ACCOUNTS_FILE_BASENAME
    if legacy.exists():
        return str(legacy)
    return str(per_env)


__all__ = [
    "ACCOUNTS_FILE_ENV_KEY",
    "ACCOUNTS_FILE_BASENAME",
    "STATE_SECTION",
    "AccountFaultError",
    "AccountPool",
    "PooledAccount",
    "classify_account_fault",
    "default_accounts_path",
    "merge_state_section",
    "read_state_section",
    "short_uid",
    "state_file_lock",
]
