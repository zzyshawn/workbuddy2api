#!/usr/bin/env python3
"""state_snapshot.py — 把「当前可用模型 + 积分余额 + 任务状态」落成一份可读文件。

为什么需要它
------------
`/health` 已经聚合了这些信息，但它是**只读接口**：在群晖上想看一眼得开 SSH 敲 curl；
而 `converter.log` 是流水账，滚起来之后想回答「现在到底有哪些模型、还剩多少积分」
得往上翻。这里把聚合结果落盘成 `logs/state.json`，在 File Station 里点开就能看，
也方便监控脚本直接读。

设计要点
--------
1. **原子写**（临时文件 + `os.replace`）：与 `CredentialManager._refresh` 回写凭据同一套路。
   不这么做的话，读的一瞬间可能拿到写了一半的 JSON —— 而这份文件存在的意义就是「能读」。
2. **纯标准库**（json / os / threading / time）：容器里不想为一个观测功能多装依赖。
3. **写失败只记日志不抛**：快照是观测手段，不能反过来影响主流程。
4. **单写线程 + 事件合并**：`request()` 只是置一个 Event，真正的采集与写盘在
   后台线程里做。这样调用方（请求线程 / 排程线程）永远不会因为磁盘或网络而卡住；
   短时间内的多次 request 会合并成一次写，不会把日志目录刷爆。
5. `build_snapshot()` 是**纯函数**（不碰网络、不碰磁盘），方便单测。

文件路径的推导
--------------
默认放在**日志文件所在目录**（`--log /logs/converter.log` → `/logs/state.json`）。
这样不必新增配置项：compose 里已经挂了 `logs/`，快照自然跟着一起可见、一起持久化。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

#: 快照文件名。放日志目录下，跟着 logs/ 一起挂载。
SNAPSHOT_NAME = "state.json"

#: 快照结构版本。将来字段有增删时递增，读取方可据此判断兼容性。
SCHEMA_VERSION = 1

#: 周期兜底刷新间隔（秒）。事件驱动已经覆盖「模型刷新 / 任务跑完」这些关键时点，
#: 但两类情况仍需兜底：①进程空转、没有任何事件的时段；②外部改了上游配置而我们没触发。
#: 默认 1 小时。设 0 关闭周期刷新（只保留事件驱动）。
DEFAULT_INTERVAL = 3600.0

#: 积分明细最多留几条（套餐数量有限，纯粹防脏数据把文件撑大）
MAX_ACCOUNTS = 20


def _fmt_ts(ts: float | None) -> str | None:
    """时间戳 → 本地时区字符串。None / 0 一律回 None，避免出现 1970 年。"""
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return None


def snapshot_path(log_path: str | os.PathLike | None, name: str = SNAPSHOT_NAME) -> Path | None:
    """从日志路径推导快照路径；没有日志路径时返回 None（= 不落盘）。

    判定顺序（**不能只看 is_dir**：开机第一次跑时日志文件还不存在，
    路径长得像文件却探不到，会把它误当目录，得到 `converter.log/state.json`）：

      1. 已存在且是目录          → 目录 / state.json
      2. 以 .json 结尾           → 原样（用户直接指定了快照文件名）
      3. 带其它扩展名（如 .log） → 当文件，取其父目录
      4. 没有扩展名              → 当目录（`--state-file /logs` 这种用法）
    """
    if not log_path:
        return None
    p = Path(log_path)
    try:
        if p.is_dir():
            return p / name
    except OSError:
        pass
    if p.suffix.lower() == ".json":
        return p
    if p.suffix:
        return p.parent / name
    return p / name


def write_atomic(path: Path, data: dict) -> bool:
    """原子写入 JSON（先写同目录临时文件，再 os.replace 顶替）。

    返回是否成功。**不抛异常** —— 快照写失败不该影响主流程。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        # 清掉可能残留的半截临时文件；清不掉也不影响（下次会覆盖）
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def build_snapshot(
    *,
    env: str | None = None,
    app: dict | None = None,
    models: dict | None = None,
    credential: dict | None = None,
    checkin: dict | None = None,
    tasks: dict | None = None,
    now: float | None = None,
) -> dict:
    """把各来源状态拼成快照 dict。

    纯函数：不访问网络、不访问磁盘，入参就是各模块 `status()` / `summary()` 的结果。
    这样测试可以直接喂构造数据，不用起服务、不用真凭据。

    入参约定（都允许缺省，缺省的部分在输出里表现为 null / 空列表）：
      - models     : {"ids": [...], "source": "upstream", "at": <ts>, "error": <str>}
      - credential : CredentialManager.summary() 的结果
      - checkin    : CheckinScheduler.status() 的结果（读其中的 points / last / enabled …）
      - tasks      : TaskScheduler.status() 的结果
    """
    ts = time.time() if now is None else float(now)
    models = models or {}
    ids = [str(x) for x in (models.get("ids") or [])]

    snap: dict = {
        "schema": SCHEMA_VERSION,
        "updated_at": _fmt_ts(ts),
        "updated_ts": int(ts),
        "env": env,
    }
    if app:
        snap["app"] = app

    snap["models"] = {
        "source": models.get("source"),
        "count": len(ids),
        "ids": ids,
        "fetched_at": _fmt_ts(models.get("at")),
        "error": models.get("error"),
    }

    if credential is not None:
        snap["credential"] = credential

    # ---- 积分 ----
    # 两个来源，优先级：refresh_points() 的即时查询 > 最近一次签到的附带查询。
    # 为什么分两种：签到（09/21 点）会顺带查余额，但旅行/活跃上报也会发积分，
    # 那些时点不查余额的话，快照上的数字会明显滞后。
    points: dict = {}
    if checkin is not None:
        fresh = checkin.get("points") or {}
        last = checkin.get("last") or {}
        if fresh:
            points = {
                "remain": fresh.get("remain"),
                "ok": fresh.get("ok"),
                "msg": fresh.get("msg"),
                "accounts": (fresh.get("accounts") or [])[:MAX_ACCOUNTS],
                "updated_at": _fmt_ts(fresh.get("at")),
                "source": "refresh",
            }
        elif last:
            points = {
                "remain": last.get("balance"),
                "ok": last.get("balance_ok"),
                "msg": last.get("balance_msg"),
                "accounts": (last.get("accounts") or [])[:MAX_ACCOUNTS],
                "updated_at": _fmt_ts(last.get("at")),
                "source": "checkin",
            }
    snap["points"] = points or None

    if checkin is not None:
        last = checkin.get("last") or {}
        snap["checkin"] = {
            "enabled": checkin.get("enabled"),
            "hours": checkin.get("hours"),
            "next_fire_at": checkin.get("next_fire_at"),
            "last": {
                "at": _fmt_ts(last.get("at")),
                "ok": last.get("ok"),
                "already": last.get("already"),
                "code": last.get("code"),
                "msg": last.get("msg"),
                "error": last.get("error"),
            }
            if last
            else None,
        }

    if tasks is not None:
        tlast = tasks.get("tasks") or {}
        # 只留**跑过**的任务。TaskScheduler.status() 会把六类全都列出来（没跑过的
        # summary 是 None），原样照搬会在快照里出现六个 null，读的人得自己过滤。
        ran: dict = {}
        for k, v in tlast.items():
            summary = (v or {}).get("summary")
            if summary:
                ran[k] = summary
        snap["tasks"] = {
            "alive": tasks.get("alive"),
            "next_wake_at": tasks.get("next_wake_at"),
            "next_wake_tasks": tasks.get("next_wake_tasks"),
            "last": ran or None,
        }

    return snap


class SnapshotWriter(threading.Thread):
    """后台写快照的线程：事件触发（request）为主，周期刷新兜底。

    调用方只需 `request()`，它立刻返回；采集与写盘都在本线程完成。
    """

    def __init__(
        self,
        path: Path | str,
        build: Callable[[], dict],
        *,
        interval: float = DEFAULT_INTERVAL,
        before_build: Callable[[], None] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        super().__init__(name="state-snapshot", daemon=True)
        self.path = Path(path)
        self.build = build
        #: 写盘前先跑一次（用来刷新积分这类需要联网的字段）。None 表示不刷新。
        self.before_build = before_build
        self.interval = float(interval or 0)
        self._log = log or (lambda _m: None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        #: 统计信息，便于 /health 观察（写失败率、上次写入时间）
        self.writes = 0
        self.failures = 0
        self.last_write_ts: float | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ 控制

    def request(self) -> None:
        """请求写一次快照。非阻塞、可合并，任意线程可调。"""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------------ 主循环

    def run(self) -> None:
        while not self._stop.is_set():
            # interval <= 0 时只等事件（Event.wait(None) 会永久阻塞至被 set）
            self._wake.wait(self.interval if self.interval > 0 else None)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.tick()

    def tick(self) -> bool:
        """采集一次并写盘。返回是否写成功。"""
        if self.before_build is not None:
            try:
                self.before_build()
            except Exception as e:  # noqa: BLE001 — 刷新失败不阻断写盘（用旧值也比没有强）
                self._log(f"[state] WARN 刷新积分失败：{e}")
        try:
            data = self.build()
        except Exception as e:  # noqa: BLE001
            self.failures += 1
            self.last_error = f"采集失败：{e}"
            self._log(f"[state] ERR 采集失败：{e}")
            return False
        if write_atomic(self.path, data):
            self.writes += 1
            self.last_write_ts = time.time()
            self.last_error = None
            return True
        self.failures += 1
        self.last_error = f"写入失败：{self.path}"
        self._log(f"[state] ERR 写入失败：{self.path}")
        return False

    def status(self) -> dict:
        return {
            "file": str(self.path),
            "interval_seconds": self.interval,
            "writes": self.writes,
            "failures": self.failures,
            "last_write_at": _fmt_ts(self.last_write_ts),
            "last_error": self.last_error,
        }


__all__ = [
    "DEFAULT_INTERVAL",
    "MAX_ACCOUNTS",
    "SCHEMA_VERSION",
    "SNAPSHOT_NAME",
    "SnapshotWriter",
    "build_snapshot",
    "snapshot_path",
    "write_atomic",
]
