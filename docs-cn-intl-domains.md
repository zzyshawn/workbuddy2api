# 国内版 / 国际版域名对比研究

> 调研日期：2026-09-19
> 样本：Oracle 东京 VPS（<已脱敏>）上的 CodeBuddy 国际版安装
> 目标：为「国内版 / 国际版 配置开关 + 分别代理」提供改造依据

## 一、核心结论

**两个版本的 API 路径结构完全同源，差异只在域名。** 这意味着改造工作量很小 ——
本质是把硬编码的域名常量换成可配置的环境映射，不需要做协议适配。

## 二、域名对照表

| 用途 | 国内版 | 国际版 | 验证方式 |
|---|---|---|---|
| **chat / 模型列表** | `https://copilot.tencent.com` | `https://www.codebuddy.ai` | 两者 `/v2/chat/completions` 均返回 **401**（端点存在，需鉴权） |
| billing / 签到 / 上报 | `https://www.codebuddy.cn` | `https://www.codebuddy.ai` | 两者 `/v2/billing/meter/get-user-resource` **路径同形** |
| web 降级领奖 | `https://www.workbuddy.cn` | 待确认 | 国际版样本里未出现 workbuddy 域 |
| SSO 登录 | — | `https://tencent.sso.codebuddy.cn` | 国际版日志中 108 次 |
| 下载 | `https://download.codebuddy.cn` | 同 | 13 次 |

**注意：国际版的 chat 与 billing 收敛到同一个域 `www.codebuddy.ai`**，
而国内版是 chat 走 `copilot.tencent.com`、billing 走 `www.codebuddy.cn` 两个域分离。
这是两者最大的结构差异。

### 国际版域名出现频次（VPS 实测，作为权重参考）

```
508  https://www.codebuddy.ai          ← 主力
237  https://mirrors.tencent.com       ← 包镜像
108  https://tencent.sso.codebuddy.cn  ← SSO
 31  https://tcb.cloud.tencent.com
 13  https://download.codebuddy.cn
  9  https://copilot.tencent.com       ← 残留，非主力
  2  https://www.codebuddy.cn          ← 几乎不用
```

## 三、凭据结构对照

两个版本的凭据结构**完全相同**，唯一区别是 `auth.domain` 字段：

```json
{
  "account": {
    "uid": "<intl-uid>",
    "nickname": "<intl-nickname>",
    "uin": "<uin>",
    "type": "personal"
  },
  "auth": {
    "accessToken": "<JWT，已截断>",
    "refreshToken": "<JWT，已截断>",
    "tokenType": "Bearer",
    "scope": "openid profile offline_access email",
    "domain": "www.codebuddy.ai",                     // ← 环境标识，就靠它区分
    "expiresAt": 1821291633059,                       // 2027-09-18
    "refreshExpiresAt": 1821291633059,
    "sessionState": "<session-state>"
  },
  "accounts": [ /* 与 account 同构 */ ]
}
```

| 项 | 国内版（本机） | 国际版（VPS） |
|---|---|---|
| uid | `<cn-uid>` | `<intl-uid>` |
| nickname | `<cn-nickname>` | `<intl-nickname>` |
| uin | `<uin>` | `<uin>` |
| `auth.domain` | `www.codebuddy.cn`（推定） | **`www.codebuddy.ai`** |
| 文件名 | `workbuddy-desktop.info` | `Tencent-Cloud.coding-copilot.info` |

**关键：`auth.domain` 是天然的环境标识。** 现有代码已经利用了它：

```python
# converter.py:192
domain = auth.get("domain") or DEFAULT_DOMAIN
```

所以 `X-Domain` 请求头**已经会自动跟随凭据走**，这一点不需要改。

## 四、现有代码的硬编码点

| 文件 | 行 | 常量 | 值 | 国际版应为 |
|---|---|---|---|---|
| `converter.py` | 73 | `BACKEND` | `https://copilot.tencent.com` | `https://www.codebuddy.ai` |
| `converter.py` | 74 | `DEFAULT_DOMAIN` | `www.codebuddy.cn` | `www.codebuddy.ai` |
| `checkin.py` | 42 | `BILLING_BASE` | `https://www.codebuddy.cn` | `https://www.codebuddy.ai` |
| `growth_api.py` | 58 | `GROWTH_BASE` | `https://copilot.tencent.com` | `https://www.codebuddy.ai` |
| `growth_api.py` | 59 | `BILLING_BASE` | `https://www.codebuddy.cn` | `https://www.codebuddy.ai` |
| `growth_api.py` | 60 | `WEB_BASE` | `https://www.workbuddy.cn` | 待确认 |
| `school.py` | 45 | `SCHOOL_BASE` | `https://www.codebuddy.cn` | 待确认 |

**共 7 个常量，分布在 4 个文件。**

现有的好设计（可以复用）：
- `growth_call(..., base_url=GROWTH_BASE)` / `billing_call(..., base_url=BILLING_BASE)`
  —— **已经支持传入 base_url**，只需要把默认值改成从环境配置读取
- `Account.domain` 已经携带环境信息
- `api_call(base, path, ...)` 也是参数化的

## 五、改造建议（待定，未实施）

### 方案 A：环境配置文件（推荐）

新增 `environments.py`，集中定义两套域名：

```python
ENVIRONMENTS = {
    "cn": {
        "chat":    "https://copilot.tencent.com",
        "billing": "https://www.codebuddy.cn",
        "web":     "https://www.workbuddy.cn",
        "domain":  "www.codebuddy.cn",
    },
    "intl": {
        "chat":    "https://www.codebuddy.ai",
        "billing": "https://www.codebuddy.ai",
        "web":     "https://www.codebuddy.ai",
        "domain":  "www.codebuddy.ai",
    },
}
```

开关来源优先级：
1. CLI 参数 `--env cn|intl`
2. 环境变量 `CODEBUDDY2OPENAI_ENV`
3. **凭据里的 `auth.domain` 自动推断**（最省心，且不会配错）

### 方案 B：双份代理配置

你要的「国内版和国际版分别配置代理」，建议：

```yaml
# compose.yaml 示意
environment:
  - CODEBUDDY2OPENAI_ENV=cn          # cn | intl | auto
  # 国内版走直连或国内代理
  - CODEBUDDY2OPENAI_PROXY_CN=
  # 国际版走海外代理（NAS 在国内时必需）
  - CODEBUDDY2OPENAI_PROXY_INTL=http://192.168.1.x:7890
```

代理要按环境分别生效，实现上需要在 `api_call` 的 `httpx.Client(...)` 里传 `proxy=`。

**⚠️ 版本依赖（已实测）**：本机 `httpx 0.28.1`，**只支持 `proxy=` 参数**，
旧版的 `proxies=` 已被移除。但 `requirements.txt` 只写了裸 `httpx` 没锁版本 ——
如果部署时解析到 0.28 以下，`proxy=` 会直接抛 `TypeError`。

**改造时必须同时锁版本**：

```
httpx>=0.28,<0.29
```

### NAS 中转的拓扑设想

```
客户端 → NAS(workbuddy2api) →┬─ 国内版: 直连 copilot.tencent.com
                             └─ 国际版: 经代理 → www.codebuddy.ai
```

NAS 在国内的话，「国内版直连 + 国际版走代理」就是你要的形态。

## 六、待确认项

1. **国际版的 web 降级领奖域** —— 国内版有 `www.workbuddy.cn` 这条降级路径，
   国际版样本里完全没出现 workbuddy 域，可能不存在或换成了别处
2. **国际版的开学季活动** —— `school.py` 打的是 `www.codebuddy.cn/portal/activity/school`，
   国际版是否有这个活动未知（可能只有国内有）
3. **国际版的 six 任务是否都可用** —— 签到/活跃上报/旅行/保活/开学季/夜猫子，
   国际版未必全有，需要实际调用验证
4. **httpx 版本的 proxy 参数写法** —— 需查 `requirements.txt` 与实际安装版本

## 七、未完成的验证

**本次没能实测"国际版凭据能否调通业务接口"**（只读的
`POST /v2/billing/meter/get-user-resource` 探测被沙箱拒绝三次）。

因此以下仍是**推断**而非**实测**：
- 国际版 token 能否被 `www.codebuddy.ai` 接受
- 国际版是否真的支持全部六类任务
- WAF（`X-WAF-UUID` 响应头）对程序化请求的实际态度

**建议下一步**：在 VPS 上手动跑一次上述探测脚本（脚本内容见下方），确认凭据可用性，
再决定是否投入改造。

```python
# 只读探测，不影响任何任务状态
import json, urllib.request, urllib.error
F = "/home/ubuntu/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info"
d = json.load(open(F)); a = d["auth"]
host = "https://" + a["domain"]

def call(path, method="GET", body=None):
    req = urllib.request.Request(host + path,
        data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Authorization", f"Bearer {a['accessToken']}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "CodeBuddy/1.0.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[{r.status}] {path}\n    {r.read().decode('utf-8','ignore')[:400]}\n")
    except urllib.error.HTTPError as e:
        print(f"[{e.code}] {path}\n    {e.read().decode('utf-8','ignore')[:400]}\n")
    except Exception as e:
        print(f"[ERR] {path}: {e}\n")

call("/v2/billing/meter/get-user-resource", "POST", {})
call("/activity/growth/tasks")
call("/activity/growth/streak")
```
