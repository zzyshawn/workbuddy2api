#!/usr/bin/env python3
"""sync_codex_models.py — 把上游可用模型同步进 Codex 的模型注册表。

为什么要单独做这个工具
----------------------
Codex 的模型下拉不是从我们的 `/v1/models` 读的，而是读它自己的注册表
`~/.codex/models.json`（结构 `{"models": [{slug, display_name, ...}]}`）。
光在 `config.toml` 里写 `[profiles.x] model = "..."` 不够 —— 那样模型**不会出现在
下拉列表里**，用户会以为"模型没加进去"。

而注册表每条记录的 schema 很长（`base_instructions` / `model_messages` 各有 ~18K 字符，
是 Codex 的 agent 提示词），不适合手写。所以这里**以本机已有的一条为模板克隆**，
只覆盖与具体模型相关的字段。

元数据来源是上游的 `GET /v3/config`（**不是**猜的）：它给了每个模型的
`maxAllowedSize` / `maxInputTokens` / `maxOutputTokens` / `supportsImages` /
`supportsToolCall` / `name` / `descriptionZh`。比照抄模板里的上下文窗口准得多 ——
上下文窗口填大了会让 Codex 迟迟不触发自动压缩，最后撞上游的 max_tokens 报错。

用法
----
    # 先看要改什么（不落盘）
    python3 sync_codex_models.py --dry-run

    # 落盘（自动备份 models.json）
    python3 sync_codex_models.py

    # 指定环境 / 文件位置 / 离线用现成 JSON
    python3 sync_codex_models.py --env intl --codex-models ~/.codex/models.json
    python3 sync_codex_models.py --from-json v3_models.json

注意
----
- 只**新增/更新**，**不删除**已有条目 —— 手加的模型不该被工具抹掉。
- 只同步 `agents[name=="cli"].models` 白名单里的模型（`models[]` 里有一部分
  拿不到 cli 入口，同步进 Codex 也没用）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# 元数据获取
# ---------------------------------------------------------------------------

def _config_url(env: str | None) -> str:
    import environments

    return environments.base_for("chat", environments.resolve_env(env)) + "/v3/config"


def fetch_config(env: str | None = None, auth: str | None = None) -> dict:
    """打上游 `/v3/config`（带 CLI UA），返回 `data` 字段。

    UA 是唯一门槛：形如 `CLI/<ver> CodeBuddy/<ver>`，否则 `data.models` 恒为 null。

    auth 显式给凭据路径（文件或目录）时会盖过自动查找 —— 本机的国际版凭据
    是复制到工作区的，不在桌面端的默认目录里，不加这个就会**误取到国内版**凭据，
    结果是同步了 16 个国内版模型（实测踩过）。
    """
    import httpx
    from converter import CredentialManager, find_auth_file
    from model_registry import cli_user_agent

    from pathlib import Path as _Path

    if auth:
        p = _Path(auth).expanduser()
        if p.is_dir():
            cands = sorted(p.glob("*.info"))
            if not cands:
                raise SystemExit(f"❌ 目录里没有 .info 凭据：{p}")
            p = cands[0]
        auth_file = p
    else:
        auth_file = find_auth_file(env)
    if auth_file is None:
        raise SystemExit("❌ 找不到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy")
    cred = CredentialManager(auth_file, env=env)
    headers = dict(cred.get_headers())
    headers["User-Agent"] = cli_user_agent()
    headers["Accept"] = "application/json, text/plain, */*"
    r = httpx.get(_config_url(env), headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json().get("data") or {}
    # 回显实际用的凭据与域，避免"以为在同步国际版、其实取了国内版"这种误判
    print(f"凭据：{auth_file}")
    print(f"域  ：{_config_url(env)}")
    return data


def parse_metas(data: dict) -> tuple[list[dict], list[str]]:
    """从 `/v3/config` 的 data 里取出 (逐模型元数据, cli 白名单)。

    为什么要白名单：`models[]` 里有些模型**没有 cli 入口**（如 intl 的
    `deepseek-v4.1-flash-sg`），同步进 Codex 也是白搭。
    """
    keep = (
        "id", "name", "descriptionZh", "descriptionEn", "maxAllowedSize",
        "maxInputTokens", "maxOutputTokens", "supportsImages", "supportsToolCall",
        "isDefault", "temperature", "vendor",
        # 2026-09-20 补：窗口分档与思考能力。没有这几项就算不出「有效上下文预算」，
        # 也拿不到 reasoning effort 的档位集合。
        "contextWindow", "reasoning", "supportsReasoning", "onlyReasoning", "credits",
    )
    models = [m for m in (data.get("models") or []) if isinstance(m, dict)]
    whitelist: list[str] = []
    for a in (data.get("agents") or []):
        if isinstance(a, dict) and a.get("name") == "cli":
            whitelist = [str(x) for x in (a.get("models") or [])]
            break
    if whitelist:
        models = [m for m in models if m.get("id") in whitelist]
    return [{k: m.get(k) for k in keep} for m in models], whitelist


# ---------------------------------------------------------------------------
# 条目构造
# ---------------------------------------------------------------------------

def pick_template(existing: list[dict], metas: list[dict]) -> dict:
    """挑一条已有条目当模板。

    优先用「本次要同步的模型」里已有的那条 —— 它的 base_instructions / model_messages
    是用户环境里实际在跑的版本，比另找一条更贴近。
    """
    want = {m.get("id") for m in metas}
    for m in existing:
        if m.get("slug") in want:
            return m
    if existing:
        return existing[0]
    raise SystemExit(
        "❌ models.json 里没有任何条目可作模板。\n"
        "   这个文件的 base_instructions / model_messages 是 Codex 的 agent 提示词（各约 18K 字符），\n"
        "   没法凭空生成 —— 请先用官方客户端产生至少一条记录再跑本工具。"
    )


def build_entry(template: dict, meta: dict, priority: int) -> dict:
    """以模板为底，覆盖与具体模型相关的字段。"""
    e = json.loads(json.dumps(template))  # 深拷贝，避免污染模板
    mid = str(meta.get("id") or "").strip()
    if not mid:
        raise ValueError("meta 缺少 id")

    e["slug"] = mid
    e["display_name"] = meta.get("name") or mid
    # Codex 的 description 是给用户看的，优先中文
    e["description"] = meta.get("descriptionZh") or meta.get("descriptionEn") or ""

    # 上下文窗口：用**有效预算**，不是 maxAllowedSize。
    #
    # 为什么改（2026-09-20）：`maxAllowedSize` 是上游的**校验上限**（用于夹取
    # max_tokens），不是模型实际跑的窗口。部分模型声明 maxInputTokens=1000000
    # 但 `contextWindow.supportedLengths=[400000, 1000000]`、`defaultLength=400000`
    # —— 默认只跑 400K。填 1M 会让 Codex 迟迟不触发自动压缩，最后撞上游 max_tokens 报错。
    #
    # 解析优先级严格对齐上游 `resolveEffectiveContextBudget`（报告 §13）：
    #   contextWindow.defaultLength > min(supportedLengths) > maxInputTokens
    from model_registry import resolve_effective_context_window

    win = resolve_effective_context_window(
        context_window=meta.get("contextWindow"),
        max_input_tokens=meta.get("maxInputTokens"),
    )
    if win > 0:
        e["context_window"] = win
        e["max_context_window"] = win

    # 多模态
    images = bool(meta.get("supportsImages"))
    e["input_modalities"] = ["text", "image"] if images else ["text"]
    if not images:
        e["supports_image_detail_original"] = False

    e["visibility"] = "list"
    e["supported_in_api"] = True
    e["priority"] = priority
    return e


def merge_models(
    existing: list[dict], metas: list[dict], template: dict
) -> tuple[list[dict], list[str], list[str]]:
    """把 metas 合并进 existing：同 slug 更新，新 slug 追加。

    返回 (新列表, 新增的 slug, 更新的 slug)。**不删除任何已有条目。**
    """
    by_slug = {m.get("slug"): i for i, m in enumerate(existing) if m.get("slug")}
    out = [dict(m) for m in existing]
    added, updated = [], []
    # 优先级从现有最大值之后开始排，不打乱用户既有的排序
    prio = max([m.get("priority") or 0 for m in existing] or [0])
    for meta in metas:
        mid = meta.get("id")
        if not mid:
            continue
        if mid in by_slug:
            prio += 1
            out[by_slug[mid]] = build_entry(template, meta, prio)
            updated.append(mid)
        else:
            prio += 1
            out.append(build_entry(template, meta, prio))
            added.append(mid)
    return out, added, updated


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def write_models(path: Path, models: list[dict]) -> Path:
    """写回 models.json，先备份。

    原子写（临时文件 + os.replace）：这个文件被正在运行的 Codex 读，
    写一半会让它解析失败。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    if path.exists():
        shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"models": models}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return bak


def default_codex_models() -> Path:
    env = os.environ.get("CODEX_HOME")
    base = Path(env) if env else Path.home() / ".codex"
    return base / "models.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="把上游模型同步进 Codex 的 models.json")
    ap.add_argument("--env", default=None, choices=["cn", "intl", "auto"],
                    help="凭据环境，默认按凭据 auth.domain 自动判定")
    ap.add_argument("--auth", default=None, metavar="PATH",
                    help="显式指定凭据文件或目录（盖过自动查找）。"
                         "本机的国际版凭据常是复制出来的，不指就会误取国内版。")
    ap.add_argument("--codex-models", default=None, metavar="PATH",
                    help="Codex 的 models.json 路径（默认 $CODEX_HOME/models.json 或 ~/.codex/models.json）")
    ap.add_argument("--from-json", default=None, metavar="FILE",
                    help="离线模式：读一个已保存的 /v3/config data JSON，不打上游")
    ap.add_argument("--dump-json", default=None, metavar="FILE",
                    help="把拉到的原始 config 存成 JSON（配合 --from-json 离线复用）")
    ap.add_argument("--capabilities", default=None, metavar="FILE",
                    help="同时导出模型能力表（上下文空间 + 思考强度）到 FILE，"
                         "并写出同名 .md。默认不导出。")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做的改动，不落盘")
    args = ap.parse_args()

    target = Path(args.codex_models).expanduser() if args.codex_models else default_codex_models()
    if not target.exists():
        print(f"❌ 找不到 {target}")
        return 1
    doc = json.loads(target.read_text(encoding="utf-8"))
    existing = doc.get("models") or []
    if not isinstance(existing, list):
        print("❌ models.json 结构不对（顶层应为 {'models': [...]}）")
        return 1

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        data = data.get("data", data)
    else:
        data = fetch_config(args.env, args.auth)
        if args.dump_json:
            dump = Path(args.dump_json).expanduser()
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(
                json.dumps({"data": data}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"原始 config 已存：{dump}")

    metas, whitelist = parse_metas(data)
    if not metas:
        print("❌ 上游没返回可用模型（UA 不对时 data.models 会是 null）")
        return 1

    template = pick_template(existing, metas)
    print(f"模板条目：{template.get('slug')}")
    print(f"白名单  ：{len(whitelist)} 个，本次处理 {len(metas)} 个")

    merged, added, updated = merge_models(existing, metas, template)

    for m in metas:
        mid = m.get("id")
        tag = "新增" if mid in added else "更新"
        from model_registry import resolve_effective_context_window

        win = resolve_effective_context_window(
            context_window=m.get("contextWindow"),
            max_input_tokens=m.get("maxInputTokens"),
        )
        img = "图" if m.get("supportsImages") else "文"
        eff = (m.get("reasoning") or {}).get("supportedEfforts") or []
        eff_s = f" 档位={len(eff)}" if eff else ""
        print(f"  [{tag}] {mid:22s} {m.get('name') or '':16s} 窗口={win} 模态={img}{eff_s}")

    # 能力表导出（上下文空间 + 思考强度），供人查 / 供其它工具消费
    if args.capabilities:
        import model_capabilities

        table = model_capabilities.build_table(data, env=args.env or "")
        out = model_capabilities.write_table(table, args.capabilities)
        print(f"\n📋 能力表已导出：{out}")
        print(f"   可读版：{out.with_suffix('.md')}")

    if args.dry_run:
        print(f"\n（dry-run）将新增 {len(added)} 条、更新 {len(updated)} 条，未落盘。")
        return 0

    bak = write_models(target, merged)
    print(f"\n✅ 已写入 {target}")
    print(f"   新增 {len(added)} 条，更新 {len(updated)} 条，总计 {len(merged)} 条")
    print(f"   备份：{bak}")
    print("   ⚠️ 重启 Codex（或重开窗口）才会读到新列表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
