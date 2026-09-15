#!/usr/bin/env python3
"""
model_registry.py — 上游动态模型发现（模型列表不再依赖本地 product.json 或硬编码表）。

链路（与 Go 参考实现 internal/upstream.FetchModels + handler 缓存状态机同构）：

    GET {chat_base}/console/enterprises/personal/models
      → 解 {code,msg,data} 信封
      → 取 data.agents[name=="cli"].models 作为白名单
      → 以 data.models 建索引，只输出「白名单内 且 disabled != true」的模型
      → maxInputTokens → context_length，maxOutputTokens → max_tokens

相对 Go 参考实现修掉的三个缺陷：

  D1 静态回退表过期        → FALLBACK_MODELS 按上游实测重写
  D2 失败期回退到过期内置表 → 失败时先回退「上一份真实快照」（带 source=stale-*），拿不到才谈内置表
  D5 快照仅进程内存、重启冷启动 → 快照落盘，新进程启动即可用（首个请求若命中快照则同步返回）

缓存语义与参考实现一致，仍是**惰性判定、非定时刷新**：
  - 命中新鲜快照（age < ttl，默认 1h）→ 0 次上游调用；
  - 拉取失败后 fail_cooldown（默认 5min）内不再打上游，直接走回退；
  - 快照进程内全局一份，不按账号分片。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import httpx

# 上游 models 接口的 CLI 白名单 agent 名
CLI_AGENT = "cli"

DEFAULT_TTL = 3600.0
DEFAULT_FAIL_COOLDOWN = 300.0
DEFAULT_TIMEOUT = 30.0

#: D1 修复：按 2026-09-12 上游实测重写的内置回退表（原表含已下线模型）。
#: 仅在上游拉取失败且无任何快照时使用，正常路径不会命中。
FALLBACK_MODELS = [
    "auto",
    "hy4-preview",
    "hy3",
    "hy3-x",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "minimax-m3",
    "deepseek-v4-pro",
]

SNAPSHOT_NAME = "models-snapshot.json"


class ModelFetchError(RuntimeError):
    """上游模型接口不可用（HTTP 非 200 / code != 0 / 白名单为空 / 解析失败）。"""


@dataclass(frozen=True)
class ModelInfo:
    """裁剪后的模型条目。context_window / max_tokens 来自上游 tokens 字段。"""

    id: str
    name: str = ""
    context_window: int = 0
    max_tokens: int = 0
    efforts: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "object": "model", "owned_by": "codebuddy"}
        if self.name:
            d["name"] = self.name
        if self.context_window > 0:
            d["context_length"] = self.context_window
        if self.max_tokens > 0:
            d["max_tokens"] = self.max_tokens
        if self.efforts:
            d["reasoning_efforts"] = list(self.efforts)
        return d

    @staticmethod
    def from_dict(d: dict) -> "ModelInfo":
        return ModelInfo(
            id=d["id"],
            name=d.get("name") or "",
            context_window=int(d.get("context_length") or 0),
            max_tokens=int(d.get("max_tokens") or 0),
            efforts=tuple(d.get("reasoning_efforts") or ()),
        )


def parse_models_payload(env: dict) -> list[ModelInfo]:
    """按上游信封裁剪出 CLI 可用模型集。

    规则（对照 Go 版 FetchModels）：
      1. code != 0 → ModelFetchError
      2. data.agents[name=="cli"].models 为空 → ModelFetchError
      3. 只保留白名单内、且在 data.models 索引中存在、且 disabled != true 的条目
      4. 结果为空 → ModelFetchError（宁可报错走回退，也不返回空列表）
    """
    if not isinstance(env, dict):
        raise ModelFetchError("models payload is not an object")
    if env.get("code") not in (0, None):
        raise ModelFetchError(f"models api code={env.get('code')} msg={env.get('msg')}")

    data = env.get("data") or {}
    if not isinstance(data, dict):
        raise ModelFetchError("models payload missing data")

    whitelist: list[str] = []
    for agent in data.get("agents") or []:
        if isinstance(agent, dict) and agent.get("name") == CLI_AGENT:
            whitelist = list(agent.get("models") or [])
            break
    if not whitelist:
        raise ModelFetchError("no cli agent models found")

    index: dict[str, dict] = {}
    for m in data.get("models") or []:
        if isinstance(m, dict) and m.get("id"):
            index[m["id"]] = m

    out: list[ModelInfo] = []
    for mid in whitelist:
        m = index.get(mid)
        if not m or m.get("disabled") is True:
            continue
        reasoning = m.get("reasoning") or {}
        efforts = reasoning.get("supportedEfforts") or []
        out.append(
            ModelInfo(
                id=mid,
                name=m.get("name") or "",
                context_window=int(m.get("maxInputTokens") or 0),
                max_tokens=int(m.get("maxOutputTokens") or 0),
                efforts=tuple(str(e) for e in efforts if e),
            )
        )

    if not out:
        raise ModelFetchError("models api returned empty list")
    return out


def _default_cache_dir() -> Path:
    env = os.environ.get("CODEBUDDY2OPENAI_CACHE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "codebuddy2openai"
    xdg = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return xdg / "codebuddy2openai"


class ModelRegistry:
    """动态模型注册表：上游拉取 + 内存缓存 + 落盘快照 + 多级回退。"""

    def __init__(
        self,
        *,
        base_url: str,
        cache_dir: Path | str | None = None,
        ttl: float = DEFAULT_TTL,
        fail_cooldown: float = DEFAULT_FAIL_COOLDOWN,
        timeout: float = DEFAULT_TIMEOUT,
        enabled: bool = True,
        log: Callable[[str], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        self.ttl = float(ttl)
        self.fail_cooldown = float(fail_cooldown)
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        self._log = log or (lambda _msg: None)

        self._lock = threading.Lock()
        self._models: list[ModelInfo] = []
        self._fetched_at: float = 0.0
        self._last_fail: float = 0.0
        self._last_error: str = ""
        self._source: str = "uninitialized"
        self._last_fetch_ms: int = 0

    # ---------------------------------------------------------------- 快照

    @property
    def snapshot_path(self) -> Path:
        return self.cache_dir / SNAPSHOT_NAME

    def _save_snapshot(self, models: Iterable[ModelInfo], fetched_at: float) -> None:
        payload = {
            "fetched_at": fetched_at,
            "base_url": self.base_url,
            "models": [m.to_dict() for m in models],
        }
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.snapshot_path)
        except OSError as e:
            self._log(f"[models] WARN 快照落盘失败：{e}")

    def _load_snapshot(self) -> tuple[list[ModelInfo], float] | None:
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        items = raw.get("models") or []
        try:
            models = [ModelInfo.from_dict(d) for d in items if isinstance(d, dict) and d.get("id")]
        except (KeyError, TypeError, ValueError):
            return None
        if not models:
            return None
        return models, float(raw.get("fetched_at") or 0.0)

    # ---------------------------------------------------------------- 拉取

    def _fetch(self, headers: dict) -> list[ModelInfo]:
        url = f"{self.base_url}/console/enterprises/personal/models"
        h = dict(headers)
        h["Accept"] = "application/json, text/plain, */*"
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(url, headers=h)
        if r.status_code != 200:
            raise ModelFetchError(
                f"models api status {r.status_code}: {r.text[:120]}"
            )
        try:
            env = r.json()
        except Exception as e:  # noqa: BLE001 — 上游可能返回非 JSON
            raise ModelFetchError(f"models parse: {e}") from e
        return parse_models_payload(env)

    # ---------------------------------------------------------------- 对外

    def resolve(
        self, headers: dict | None, *, force: bool = False
    ) -> tuple[list[ModelInfo], str]:
        """返回 (模型列表, 来源标记)。

        来源标记：cache / upstream / stale-memory / stale-snapshot / fallback-static。
        无法动态获取且无任何快照时抛 ModelFetchError，由调用方决定是否用内置表。
        """
        if not self.enabled:
            raise ModelFetchError("dynamic models disabled")

        now = time.time()
        with self._lock:
            if not force and self._models and (now - self._fetched_at) < self.ttl:
                self._source = "cache"
                return list(self._models), "cache"
            cooling = (
                not force
                and self._last_fail
                and (now - self._last_fail) < self.fail_cooldown
            )
            stale = list(self._models)
            stale_at = self._fetched_at
            last_error = self._last_error

        if cooling:
            hit = self._fallback(stale, stale_at)
            if hit:
                models, src = hit
                self._log(f"[models] 失败冷却中（{last_error or '上次拉取失败'}），回退 {src}")
                return models, src
            raise ModelFetchError(f"models fetch cooling down: {last_error}")

        if headers is None:
            hit = self._fallback(stale, stale_at)
            if hit:
                return hit
            raise ModelFetchError("no credential available for models fetch")

        t0 = time.perf_counter()
        try:
            models = self._fetch(headers)
        except Exception as e:  # noqa: BLE001 — 任何失败都要走回退，不能冒泡打断 /v1/models
            elapsed = int((time.perf_counter() - t0) * 1000)
            # 归一化错误文本：上游可能返回多行 HTML，日志必须保持单行
            err = _truncate(str(e), 200)
            with self._lock:
                self._last_fail = time.time()
                self._last_error = err
            self._log(f"[models] ERR 拉取失败（{elapsed}ms）：{err}")
            hit = self._fallback(stale, stale_at)
            if hit:
                models, src = hit
                self._log(f"[models] 回退 {src}（{len(models)} 个模型）")
                return models, src
            raise ModelFetchError(str(e)) from e

        elapsed = int((time.perf_counter() - t0) * 1000)
        with self._lock:
            self._models = models
            self._fetched_at = time.time()
            self._last_fail = 0.0
            self._last_error = ""
            self._last_fetch_ms = elapsed
            self._source = "upstream"
        self._save_snapshot(models, self._fetched_at)
        self._log(f"[models] OK 上游拉取 {len(models)} 个模型（{elapsed}ms）")
        return list(models), "upstream"

    def _fallback(
        self, mem: list[ModelInfo], mem_at: float
    ) -> tuple[list[ModelInfo], str] | None:
        """失败回退：优先进程内旧快照（更新鲜），其次落盘快照（跨重启）。"""
        if mem:
            self._source = "stale-memory"
            return list(mem), "stale-memory"
        snap = self._load_snapshot()
        if snap:
            models, at = snap
            with self._lock:
                self._models = models
                self._fetched_at = at
            self._source = "stale-snapshot"
            return models, "stale-snapshot"
        return None

    def warm(self, headers: dict | None) -> None:
        """启动预热：忽略失败，仅为把快照提前装进内存。"""
        if not self.enabled:
            return
        try:
            self.resolve(headers)
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        with self._lock:
            age = time.time() - self._fetched_at if self._fetched_at else None
            return {
                "enabled": self.enabled,
                "source": self._source,
                "models": len(self._models),
                "ids": [m.id for m in self._models],
                "fetched_at": int(self._fetched_at) if self._fetched_at else None,
                "age_seconds": int(age) if age is not None else None,
                "ttl_seconds": int(self.ttl),
                "fresh": bool(age is not None and age < self.ttl),
                "last_fail_at": int(self._last_fail) if self._last_fail else None,
                "last_error": self._last_error or None,
                "last_fetch_ms": self._last_fetch_ms or None,
                "snapshot_file": str(self.snapshot_path),
                "snapshot_exists": self.snapshot_path.exists(),
            }


def _truncate(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")
