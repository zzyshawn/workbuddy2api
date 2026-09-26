# 双环境（国内版 / 国际版）+ 分环境代理 —— 需求与概要设计

> 状态图例：**未分析** → **已实现** → **修定** → **已实现**
>
> | 需求 | 状态 |
> |---|---|
> | R1 环境开关（国内版 / 国际版） | 已实现 |
> | R2 分环境代理配置 | 已实现 |
> | R3 环境自适应（凭据自动推断） | 已实现 |
> | R4 六类定时任务的国际版兼容 | 部分实测（见 §2 R4） |
>
> 实现细节见 §5；实测证据见 `docs-cn-intl-domains.md`。

---

## 1. 背景与目标

本机的 CodeBuddy / WorkBuddy 是**国内版**（登录态走 `copilot.tencent.com` +
`www.codebuddy.cn`）。但同一个产品还有**国际版**（`www.codebuddy.ai`），
两者 API 路径结构同源、**仅域名不同**，凭据结构也完全一致。

用户场景：**在 NAS 上部署一套中转服务**，同时服务国内版与国际版账号；
且因为 NAS 在国内，国际版流量必须走代理，国内版则应直连（走代理会慢 3~10 倍，实测）。

因此需要：

1. **R1** 一个环境开关，决定打到哪套域名；
2. **R2** 代理按环境分别配置 —— 国内版可直连、国际版走 SOCKS5/HTTP 代理；
3. **R3** 最好能自动识别，避免用户配错。

---

## 2. 需求分析

### R1 环境开关

- 取值 `cn` / `intl` / `auto`，缺省 `auto`。
- `auto` 时从凭据 `auth.domain` 推断（`www.codebuddy.ai` → intl，否则 cn）。
- 显式指定时以显式值为准（允许「用国内版凭据打国际版域」这类对照排查）。

**为什么默认 auto**：凭据的 `domain` 字段是天然的环境标识，且现有代码
（`converter.py:192`）已经在用它填 `X-Domain` 头。跟着凭据走**不会配错**。

### R2 分环境代理

- 两个独立配置项：`CODEBUDDY2OPENAI_PROXY_CN` / `CODEBUDDY2OPENAI_PROXY_INTL`。
- 生效规则：**按「当前生效的环境」取对应代理**，不是按目标域名。
  这样 `--env intl` 时所有出站流量统一走国际代理，语义简单可预测。
- 值为标准代理 URL，支持 `http://` / `https://` / `socks5://`（带或不带用户名密码）。
- 留空 = 直连。
- 兜底：另设 `CODEBUDDY2OPENAI_PROXY` 作为「两个环境都不单独配」时的统一代理。

### R3 环境自适应

见 R1 的 `auto`。

### R4 六类定时任务的国际版兼容（部分实测）

国际版是否有全部六类任务**尚未逐项实测**，但**国际版凭据本身已实测可调通**
（`_e2e_intl.py` 走 SOCKS5 全链路 10/10 通过，模型列表、流式、工具调用、
`/v1/messages`、`/v1/responses` 全部可用）。

| 任务 | 国际版可用性 |
|---|---|
| 签到 checkin | 待验证 |
| 活跃上报 activity | 待验证 |
| 猫猫旅行 travel | 待验证 |
| token 保活 keepalive | 待验证 |
| 开学季 school | **很可能没有**（打的是 `/portal/activity/school`，国内活动） |
| 夜猫子 cat | 待验证 |

**旁证**：国际版个人号在权威模型接口上也拿不到数据（`/v3/config` 返回
`models: null`，官方 CLI 自己都降级去读本地 `~/.codebuddy/models.json`），
说明国际版账号体系的「活动类」接口整体更薄 —— 任务类接口大概率同样受限。
但这只是推断，**逐项实测仍未做**。

**处理策略**：环境本身不做任务开关 —— 任务该不该跑由既有的
`--no-xxx` 决定。国际版里不存在的活动，其接口会返回业务错误，
任务如实记一条失败（开学季有 `in_period` 下线保护，会自动跳过而不报错）。

---

## 3. 域映射（基于实测）

| 用途 | `cn` | `intl` |
|---|---|---|
| chat / 模型列表 / token 刷新 | `https://copilot.tencent.com` | `https://www.codebuddy.ai` |
| billing / 签到 / 余额 / 上报 | `https://www.codebuddy.cn` | `https://www.codebuddy.ai` |
| web 降级领奖 | `https://www.workbuddy.cn` | `https://www.codebuddy.ai` |
| `X-Domain` 头 | `www.codebuddy.cn` | `www.codebuddy.ai` |

**关键结构差异**：国内版 chat 与 billing **分属两个域**；国际版**收敛到同一个域**。
这是 `environments.py` 存在的核心理由 —— 不是简单换一个字符串，而是「一个环境 → 一组域」。

`intl` 的 web 域填 `www.codebuddy.ai` 是**降级兜底**：国际版不存在 `workbuddy.cn`，
领奖降级路径大概率用不上；真出问题时该路径会 404，与「不降级」等效，不会更糟。

---

## 4. 概要设计

```
                    ┌──────────────────────────┐
                    │   environments.py        │
                    │  ENVIRONMENTS (域映射表)  │
                    │  resolve_env()  环境解析  │
                    │  resolve_proxy() 代理解析 │
                    │  make_client()  httpx 工厂│
                    └────────────┬─────────────┘
                                 │ 只被能力模块单向依赖
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
  converter.py            growth_api.py            checkin.py / school.py
  （BACKEND/默认域）        （GROWTH/BILLING/WEB）    （BILLING_BASE/SCHOOL_BASE）
```

### 4.1 分层与依赖方向

- `environments.py` 是**最底层**，不 import 任何业务模块（避免循环导入）。
- 各业务模块单向 import `environments`，且**模块级常量仍需存在**
  （测试与外部代码在引用它们），改为「由 environments 计算出的值」。
- `env` / `proxy` 通过**参数注入**传递，不引入全局可变状态
  —— 与项目既有风格一致（`transport`、`log` 都是这么传的）。

### 4.2 环境解析优先级

```
1. 显式参数（CLI --env / 函数入参）
2. 环境变量 CODEBUDDY2OPENAI_ENV
3. 凭据 auth.domain 自动推断        ← auto 的落点
4. 兜底 "cn"（保持既有行为）
```

### 4.3 代理解析优先级

```
1. 显式参数（CLI --proxy-cn / --proxy-intl，或函数入参 proxy）
2. 按当前环境取对应环境变量
     cn   → CODEBUDDY2OPENAI_PROXY_CN
     intl → CODEBUDDY2OPENAI_PROXY_INTL
3. 通用兜底 CODEBUDDY2OPENAI_PROXY
4. 无 → 直连（proxy=None）
```

### 4.4 NAS 中转拓扑

```
客户端 ──► NAS (workbuddy2api) ──┬─ 国内版: 直连 copilot.tencent.com / www.codebuddy.cn
                                 └─ 国际版: 经 SOCKS5 代理 → www.codebuddy.ai
```

---

## 5. 实现要点

### 5.1 新增 `environments.py`

- `ENVIRONMENTS: dict[str, dict[str, str]]` —— 两套域的完整映射
- `resolve_env(explicit=None, domain=None) -> str`
- `resolve_proxy(env, explicit=None) -> str | None`
- `make_client(env=None, *, proxy=None, timeout=..., transport=None) -> httpx.Client`
- `bases_for(env) -> dict[str, str]` —— 取某环境的域映射
- `env_of_domain(domain) -> str` —— 由 `X-Domain` 值反推环境

### 5.2 改动清单（7 个常量 / 4 个文件）

| 文件 | 常量 | 改动 |
|---|---|---|
| `converter.py` | `BACKEND` | `environments.BASE_CHAT(env)` |
| `converter.py` | `DEFAULT_DOMAIN` | `environments.DEFAULT_DOMAIN(env)` |
| `growth_api.py` | `GROWTH_BASE` | 同上（chat 域） |
| `growth_api.py` | `BILLING_BASE` | 同上（billing 域） |
| `growth_api.py` | `WEB_BASE` | 同上（web 域） |
| `checkin.py` | `BILLING_BASE` | 同上（billing 域） |
| `school.py` | `SCHOOL_BASE` | 同上（billing 域） |

**兼容性**：这些模块级常量保留原名与语义，只是取值来自 environments
（模块导入时按「环境变量 + 兜底 cn」算一次）。函数签名**全部不变**，
`base_url=` 显式传参的路径继续优先。

### 5.3 依赖约束（实测踩坑）

- `httpx 0.28.1` **只支持 `proxy=`**，`proxies=` 已移除。
- SOCKS5 需要额外的 `socksio` 包；`requirements.txt` 必须写 `httpx[socks]`。
- `requirements.txt` 原本是**裸 `httpx` 没锁版本** → 必须锁 `httpx>=0.28,<0.29`，
  否则解析到旧版会直接 `TypeError`。
- `socksio` 在清华源上**不存在**，走官方 PyPI 才能装到 —— 部署文档要写明。

---

## 6. 未确认项（影响 R4）

**已关闭的项**（实测证据见 `docs-cn-intl-domains.md`）：

- ~~国际版凭据能否真正调通业务接口~~ —— **已实测可调通**。
  `_e2e_intl.py` 经 `socks5://` 代理跑通 10/10（`/health`、模型列表、
  非流式、流式、`stream=false` 降级、工具调用、`/v1/messages`、`/v1/responses`）。
- ~~国际版凭据文件名与国内版不同~~ —— **已实测**。国际版文件名是
  `Tencent-Cloud.coding-copilot.info`（VPS 上实际观察到），国内版是
  `workbuddy-desktop.info`。当前**不靠文件名**识别，而是**按 `auth.domain` 认领**
  （`cn` / `intl` 分目录存），比通配更稳，已由 `test_auth_layout.py` 覆盖。

**仍开放**：

1. 国际版是否支持全部六类任务（尤其开学季）—— 见 §2 R4
2. 国际版 web 降级领奖域是否存在（`www.codebuddy.ai` 是推断的兜底值）
3. 国际版 billing 端点（`/v2/billing/meter/daily-checkin`、
   `/v2/billing/meter/get-user-resource`）是否存在 —— **未实测**

---

## 7. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-19 | 初版：R1/R2/R3 设计并实现；R4 标记未分析 |
| 2026-09-19 | 实测补录：关闭 §6 第 1、4 项；R4 改为「部分实测」并补旁证；新增 §6 第 3 项（billing 端点未测） |
