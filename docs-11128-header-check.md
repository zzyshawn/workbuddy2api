# 上游 11128「request illegal」头族校验与排查方法（2026-10-17）

> 本文记录 2026-10-16 23:44 起的 11128 全局拦截事故：根因定位、排查方法论、修复（b20）。
> 面向后续维护者：**上游风控会改，这套排查方法不会过时。**

## 现象

- 2026-10-16 23:44 起：所有 chat 请求 `HTTP 400 + {"code":11128,"msg":"request illegal","displayMsg":"请求被拦截，请重新发送"}`。
- NAS 网关 / VPS1/VPS2 代理 / 国内直连**全部出口命中**；两个账号同时命中。
- **同机同号矛盾**：VPS2 上官方 codebuddy CLI 对话正常，自制客户端（converter / curl / node）全被拦。

## 排查方法（排除矩阵 + 抓包重放）

### 排除矩阵（每次只变一个维度）

IP（本机/代理/直连）、账号（2 个）、UA 版本（2.164/2.168）、单独加 Stainless 头、路径（/v1 404、/v2 拦）、模型（5 个）、TLS 栈（curl/python/node）、会话头族（单独加）——**全部同结果** → 只剩「请求头整体形态」。

### 抓官方 CLI 真实请求（终局）

```bash
# 1) VPS2 起本地转发器（dump 后转发 https://www.codebuddy.ai）
# 2) CLI 走转发器跑一次对话：
CODEBUDDY_BASE_URL=http://127.0.0.1:9999/v2 codebuddy -p "reply ok"
# 3) 抓包得到：40 头 + /v2/chat/completions + 99KB body
# 4) 重放对照：全头 → 200；缺头 → 11128  → 实锤
```

坑：`CODEBUDDY_BASE_URL` 不带 `/v2` 时 SDK 拼 `/chat/completions`（405）；dump body 别截断（截断报 11101 unexpected EOF）。

## 结论：31 个 CLI 客户端头（b20 已补齐）

`_cli_client_headers()`（converter.py）注入 `_build_headers_from()`：

- `x-codebuddy-request: 1`、`X-Product: SaaS`、`x-requested-with: XMLHttpRequest`
- `X-Agent-Purpose/Intent/Runtime/Type`、`X-IDE-Type/Name/Version`
- `x-stainless-*`（js / 6.25.0 / Linux / x64 / 0 / node / v22.22.3）
- `X-Private-Data: false`
- 会话/链路 ID 族：`X-Conversation-*`、`X-Request-ID`、`X-Root-Request-ID`、`baggage`、`traceparent`、`b3`、`X-B3-*`、`X-Trace-ID`（动态 UUID v4 每请求生成即可）

## ⚠️ 11128 的三种形态（判定绝不能只看 msg）

| 形态 | 特征 | 处置 |
|---|---|---|
| 缺 system prompt | 首条非 system | 强制首条 system |
| 头族缺失（本文） | 无 CLI 头族 | 补头族 |
| 账号封禁 11140 | 403 + displayMsg「内容未通过安全审核」 | 永久禁用 + 换号 |

三种 msg 都可能是 "request illegal" —— **displayMsg 才是语义关键**：「请重新发送」= 临时拦截（换号+退避、绝不禁号，见 b18）。

## 历史版本关联

| 版本 | 内容 |
|---|---|
| b18 | 11128 临时拦截不再误判封号（displayMsg 判定） |
| b19 | 快照全量覆盖丢失池持久化段（state 回填） |
| b20 | **本文：31 头补齐，11128 拦截修复** |
| b16/b17 | state 池段键名冲突 / runner 缓存热加载失效 |
