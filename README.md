# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

适用场景：

- 用 **Codex CLI** 走 `/v1/responses`
- 用 **Claude Code / CC Switch** 走 `/v1/messages`
- 用 **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** 走 `/v1/chat/completions`

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

`workbuddy2api` 是一个本地协议转换器。它会读取你已经登录好的 WorkBuddy / CodeBuddy 桌面端凭据，转发到腾讯后端 `copilot.tencent.com`，然后在本地暴露这些接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

它不负责登录，不模拟桌面端，也不替你执行工具。它只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> 命名说明：项目对外名称现在叫 `workbuddy2api`。代码里仍保留部分历史命名，比如 `codebuddy2openai`、`CODEBUDDY2OPENAI_*`，目的是兼容旧配置和环境变量。
> 另外，GitHub 仓库路径当前也可能仍沿用 `codebuddy2openai`，这是仓库路径与项目展示名尚未完全统一，不影响使用。

### 你能用它做什么

- 把 WorkBuddy 订阅复用到 OpenAI 兼容客户端
- 让 Codex CLI 直接接腾讯后端，而不是只接 OpenAI 官方
- 让 Claude Code 通过 CC Switch 复用 WorkBuddy 支持的模型
- 保留原生 `tools` / `tool_calls` / 流式 SSE / 多轮工具调用
- 顺带每天自动跑完六类积分任务：签到 / 活跃上报 / 猫猫旅行 / token 保活 / 开学季 / 夜猫子

### 当前支持

| 客户端 / 协议 | 接口 | 当前状态 |
|------|------|------|
| OpenAI Chat Completions | `/v1/chat/completions` | 已支持 |
| OpenAI Responses | `/v1/responses` | 已支持，适配 Codex CLI |
| Anthropic Messages | `/v1/messages` | 已支持，适配 Claude Code / CC Switch |
| OpenAI Models | `/v1/models` | 已支持，**动态拉取上游模型列表**（带缓存与快照回退） |
| 定时积分任务（六类） | 后台调度 + `/admin/tasks` | 已支持，见[定时积分任务](#定时积分任务六类) |
| 每日自动签到 | 后台调度 + `/admin/checkin` | 已支持，默认每天 `09:00` / `21:00` |
| 模型缓存状态 | `/admin/models` | 已支持 |
| Health Check | `/health` | 已支持 |

---

## 3 分钟上手

### 1. 前置条件

你需要先满足这 3 个条件：

1. 本机已经安装并登录 **WorkBuddy / CodeBuddy** 桌面端
2. 本机有 **Python 3.8+**
3. 已安装依赖 `fastapi`、`uvicorn`、`httpx`

默认会在这些位置寻找登录态：

- macOS: `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- Windows: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
- Linux: `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`

### 2. 安装依赖

推荐用 `uv`：

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 注意：无论是启动服务，还是执行 `python3 converter.py --help`，都必须先装依赖。

### 3. 启动

最常用的启动方式：

```bash
uv run converter.py --desensitize --log converter.log
```

或：

```bash
python3 converter.py --desensitize --log converter.log
```

看到监听 `http://127.0.0.1:8787` 就说明已经起来了。

### 4. 快速自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
curl http://127.0.0.1:8787/admin/models     # 模型来源/缓存状态
curl http://127.0.0.1:8787/admin/checkin    # 签到排程与最近结果
curl http://127.0.0.1:8787/admin/tasks      # 六类定时任务排程与最近结果
```

如果这几条能通，说明本地服务、登录态、基本路由、动态模型与六类定时任务排程都没问题。
`/v1/models` 返回的模型应该与你订阅里 CLI 可用的模型一致（而不是内置的那张老表）。

---

## 客户端接入

### Codex CLI

这是当前最推荐的接法。Codex CLI 走的是 `/v1/responses`，而不是 `/v1/chat/completions`。

推荐启动命令：

```bash
uv run converter.py --desensitize --log converter.log
```

把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

设置一个占位环境变量：

```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

启动：

```bash
codex --profile workbuddy "你的任务描述"
```

补充说明：

- 推荐保留 `--desensitize`
- 当前 `/v1/responses` 默认已经会做投影压缩
- 如果你想尽量保留原始 system prompt，可试 `--desensitize --no-compact`
- `--desensitize --no-compact` 下若仍命中审核，当前实现会自动退回紧凑模式重试一次

### Claude Code / CC Switch

Claude Code 不走 OpenAI 协议，而是走 Anthropic Messages。

推荐启动命令：

```bash
uv run converter.py --desensitize --log converter.log
```

在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

注意：

- 模型名必须填写腾讯后端支持的真实模型名
- 不做 Anthropic 模型名到腾讯模型名的自动映射
- Claude Code 场景强烈建议开启 `--desensitize`

### 其他 OpenAI 兼容客户端

适用于：

- Cherry Studio
- ZCode
- LobeChat
- NextChat
- Open WebUI
- 自己写的 OpenAI SDK 客户端

配置方式：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 留空，或填你启动时设置的 `--api-key`
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

---

## 常用命令

### 基本启动

```bash
python3 converter.py
python3 converter.py --desensitize
python3 converter.py --desensitize --log converter.log
python3 converter.py --api-key mysecret
python3 converter.py --port 9000
```

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize` 使用，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |
| `--models-ttl` | `3600` | 动态模型列表的内存缓存时长（秒） |
| `--no-dynamic-models` | 否 | 关闭上游动态模型拉取，只用本机 `product.json` / 内置表 |
| `--cache-dir` | 见下 | 模型快照 + 签到留档 + 六类任务留档的落盘目录 |
| `--checkin-hours` | `9,21` | 每日自动签到的整点时刻，逗号分隔；传空串仍回落 `9,21` |
| `--no-checkin` | 否 | 关闭每日自动签到线程 |
| `--checkin-on-start` | 否 | 启动时先签到一次，之后按 `--checkin-hours` 排程 |
| `--checkin-timeout`（别名 `--task-timeout`） | `60` | 签到 / 余额 / 六类任务的单次请求超时（秒） |
| `--travel-hours` | `9,21` | 猫猫旅行巡检的整点时刻 |
| `--activity-hours` | `10` | 活跃上报的整点时刻 |
| `--activity-report-count` | `5` | 每次活跃上报的条数；`0` / 负数按 1 条处理 |
| `--keepalive-hours` | `22` | token 保活的整点时刻 |
| `--school-hours` | `12` | 开学季任务的整点时刻 |
| `--cat-hours` | `1` | 夜猫子任务的整点时刻 |
| `--no-travel` / `--no-activity` / `--no-keepalive` / `--no-school` / `--no-cat` | 否 | 分别关闭对应任务 |
| `--no-tasks` | 否 | 关闭全部六类定时任务（含签到） |

`--cache-dir` 默认位置：Windows 为 `%LOCALAPPDATA%\codebuddy2openai`，其他平台为 `~/.cache/codebuddy2openai`。

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/models
curl "http://127.0.0.1:8787/v1/models?refresh=1"   # 强制重拉上游模型列表
curl http://127.0.0.1:8787/admin/models            # 模型缓存状态
curl http://127.0.0.1:8787/admin/checkin           # 签到调度状态 + 最近结果
curl -X POST http://127.0.0.1:8787/admin/checkin   # 立即签到一次
curl http://127.0.0.1:8787/admin/tasks             # 六类定时任务状态 + 最近结果
curl -X POST "http://127.0.0.1:8787/admin/tasks?task=all"   # 六类任务各跑一次

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"你好"}]}'

curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 日志与排障

### 推荐启动方式

```bash
uv run converter.py --desensitize --log converter.log
```

### 日志里能看到什么

**每个 HTTP 请求都会留下一行访问日志**（中间件统一记，不依赖各端点自觉）：

```
[192.168.1.20] POST /v1/chat/completions → 200 | 3421ms | model=gemini-3.5-flash | ua=CherryStudio/1.2
[192.168.1.20] GET /v1/models → 200 | 2ms | ua=NewAPI/0.6
[10.0.0.5] GET /v1/models → 401 | 1ms | ua=curl/8.5.0
```

四个字段分别回答「谁连的 / 连了什么 / 结果如何 / 花了多久」：

- 开头的 IP：优先取 `X-Forwarded-For` 首个非 `unknown` 项 —— 经过 New API
  之类的中转面板时，`remote_addr` 是面板自己，只有 XFF 才能看出真实来源。
- `model=`：仅聊天类端点有（模型名在请求体里，端点解析后顺手登记）。
- 这一行**覆盖了以前的三处盲区**：`/v1/models` 过去完全没日志；401 在进端点前
  就返回、端点内记不到；也没有任何地方记录客户端 IP。
  `/health` 会被容器 healthcheck 每 30 秒打一次，**故意跳过**不记。

然后才是每个聊天请求带唯一 ID 的详细日志：

- `REQUEST BODY`
- `RESPONSES → CHAT BODY`
- `RESPONSES PROJECTION`
- `RESPONSE BODY`
- `RESPONSE RAW SSE`
- `⚠️内容审核拦截`

其中 `RESPONSES PROJECTION` 会告诉你：

- 投影前后消息数
- 投影前后字符数
- tool schema 压缩量
- 是否丢掉了 harness 消息
- 是否保留了 anchor user

### 状态快照（`state.json`）

不想翻流水账时，直接看这一份文件：

```
logs/state.json
```

它把「当前可用模型 / 积分余额 / 任务状态」聚合成 JSON，路径默认**跟 `--log` 同目录**，
所以在群晖上 File Station 点开 `logs/` 就能看到，不需要新增挂载。

```bash
# 默认：logs/converter.log 旁边生成 logs/state.json
uv run converter.py --log /logs/converter.log

# 显式指定位置与刷新间隔
uv run converter.py --log /logs/converter.log --state-file /logs/state.json --state-interval 1800
```

内容长这样：

```json
{
  "schema": 1,
  "updated_at": "2026-09-20 09:00:12",
  "env": "intl",
  "models": { "source": "upstream", "count": 20, "ids": ["gpt-6-astra", "..."], "fetched_at": "..." },
  "credential": { "uid": "...", "nickname": "...", "token_expired": false },
  "points": {
    "remain": 379,
    "accounts": [{ "package": "Bonus Pack", "remain": 250, "size": 250 }],
    "updated_at": "...", "source": "refresh"
  },
  "checkin": { "enabled": true, "hours": [9, 21], "next_fire_at": "...", "last": { "ok": true } },
  "tasks": { "alive": true, "next_wake_at": "...", "last": { "checkin": "今日已签到" } }
}
```

刷新时机（事件驱动为主，周期兜底）：

| 时机 | 说明 |
|---|---|
| 启动时 | 写一次，保证文件立刻存在 |
| 模型清单刷新后 | 每次成功解析模型都更新 |
| 每类定时任务跑完后 | 积分正是在这些时点变化的 |
| 周期兜底 | 默认 1 小时（`--state-interval` 调整，`0` = 只在事件时刷新） |

几个要点：

- **`points` 是纯查询来的**，走 `refresh_points()` 而不是签到 —— 刷新观测数据不该
  触发写操作。`source` 字段标明是 `refresh`（即时查询）还是 `checkin`（签到附带）。
- **原子写**：先写 `state.json.tmp` 再 `os.replace`，所以读到的一定是完整 JSON。
- **写失败只记日志**，不影响服务；`/health` 的 `snapshot` 字段能看到写入次数与上次错误。
- 快照路径也会在 `/health` 里回显（`snapshot.file`），启动横幅里也会打印一行。

### 最常见问题

#### 找不到登录文件

说明桌面端没登录，或者登录目录不在默认路径。先确认桌面端已经真正完成登录。

#### 401

分两种：

- 本地 401：你启用了 `--api-key`，但客户端没带同一个 key
- 后端 401：腾讯 token 失效，尝试重新打开桌面端登录

#### 响应慢

先换快一点的模型，比如 `deepseek-v4-flash`。

#### 被“敏感内容”拦截

这是腾讯后端的内容审核，不一定是用户问题本身敏感，很多时候是 agent runtime 文本触发的，比如：

- `DoS`
- `exploit`
- `credential`
- `sandbox`
- `escalation`
- 竞争品牌词
- tool description 中的安全术语

建议排查顺序：

1. 开 `--log`
2. 看同一请求 ID 下的 `REQUEST BODY` 或 `RESPONSES → CHAT BODY`
3. 如果是 Codex CLI，再看 `RESPONSES PROJECTION`
4. 开 `--desensitize`
5. 如果还不稳，再尝试 `--desensitize --no-compact`

---

## Docker 部署

如果你更习惯用 Docker，可以直接用。

前提是把宿主机登录态目录挂进去，因为容器里拿不到桌面端 auth 文件。

> ⚠️ **auth 目录必须可写**（不要加 `:ro`）。
> `converter.py` 在 token 临近过期时会用 `os.replace()` 原子回写 auth 文件；
> 只读挂载会导致刷新失败 → 后端一路 401。
> 同理，要挂**目录**而不是单个 `.info` 文件，否则容器内 rename 会报 busy。

### 群晖（Synology）

群晖用 Container Manager（旧名 Docker 套件）的「项目」功能即可。推荐目录布局：

```text
/volume1/docker/workbuddy2api/          <- 代码（compose.yaml 所在处）
/volume1/docker/workbuddy2api/auth/     <- 凭据
/volume1/docker/workbuddy2api/logs/     <- 日志
```

> ⚠️ **这两个挂载目录必须先手工建好**。群晖的 Docker **不会**自动创建 bind mount 的
> 宿主机目录（Linux 上会，群晖不会），缺目录时项目启动直接报
> `Bind mount failed`。所以先去 File Station 在 `/volume1/docker/workbuddy2api/`
> 下建出 `auth/` 和 `logs/`，再创建项目。

凭据**按环境分开放**（两个环境是两份不同账号的 token，混放会随机取错 → 401）：

```text
auth/cn/workbuddy-desktop.info                 <- 国内版
auth/intl/Tencent-Cloud.coding-copilot.info    <- 国际版
```

只用一个环境时，把 `.info` 直接丢 `auth/` 根目录也行 —— 容器会按凭据里的
`auth.domain` 自动认领（`CODEBUDDY2OPENAI_ENV=auto`）。

凭据从电脑获取的位置：

| 系统 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/` |
| Linux | `~/.local/share/CodeBuddyExtension/Data/Public/auth/` |

群晖上 `compose.yaml` 里请用**绝对路径**（`./` 相对路径在 Container Manager 里容易解析错），
仓库里给的默认值已经是 `/volume1/docker/workbuddy2api/...`，按自己的共享文件夹改。

**路线 A —— Container Manager 图形界面**

1. 把整个仓库传到 `/volume1/docker/workbuddy2api/`（File Station 上传或 `rsync`）。
2. File Station 里建好 `auth/`、`logs/`、`auth/cn/`、`auth/intl/`，把 `.info` 放进去。
3. Container Manager →「项目」→ 创建 → 路径选 `/volume1/docker/workbuddy2api`，
   来源选「使用现有的 docker-compose.yml」。
4. 编辑 YAML，改掉挂载路径与 `CODEBUDDY2OPENAI_KEY`，再创建。
5. 若启动时报镜像不存在，到「项目」→ 动作 →「构建」（等价 `up --build --no-start`）先建镜像。

**路线 B —— SSH 命令行**（推荐，出问题时日志更好读）

```bash
cd /volume1/docker/workbuddy2api
sudo docker compose up -d --build
```

验证：

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models -H "Authorization: Bearer <你的KEY>"
```

> `/health` **不参与鉴权**（公开端点，供 healthcheck 用），且会回显 uid、昵称、
> 企业名和凭据文件路径。只想本机访问就把端口改成 `127.0.0.1:8787:8787`，
> 要暴露到局域网则务必设置 `CODEBUDDY2OPENAI_KEY`。

#### 只往 NAS 传一个包（带版本号，tar 包 → 解成目录再构建）

不想在 NAS 上同步整个仓库时，可以本机打好包，只传一个
`wb2api-<版本>.tar.gz`。

> **包名带版本，且版本与 CodeBuddy CLI 对齐。** 版本号只有一个真源 ——
> `model_registry.CLI_UA_VERSION`（也就是本服务对上游声明
> `User-Agent: CLI/<ver> CodeBuddy/<ver>` 时用的那个版本）。包名、
> 包内 `VERSION`、`compose.yaml` 的 image tag 全部由它派生，不会各说各话。
> 要换版本只改 `model_registry.CLI_UA_VERSION` 一处，其余由测试守着。

> 🚫 **群晖 Container Manager 不支持 `.tar.gz` 作为 build context。**
> 写 `context: ./wb2api-2.155.0.tar.gz` 会直接报：
>
> ```text
> unable to prepare context: context must be a directory:
> ```
>
> （实测。群晖的构建器只认目录。普通 Linux 上 `docker build` 是支持 tar context 的，
> 所以这条限制**只在群晖上暴露**，本地测不出来。）
>
> 因此 `test_deployment.py::test_compose_context_is_a_directory` 会守着这条。

**本机打包 + 同时解出源码目录**（脚本已处理中文路径、反斜杠、凭据排除等坑）：

```bash
cd <仓库的 workbuddy2api 目录>
python3 pack_for_nas.py --extract ../wb2api-src
```

输出会带版本号与 SHA256，方便传输后核对：

```text
✅ 打包完成：D:\mycode\myworkbuddy2api\wb2api-2.155.0.tar.gz
   版本  ：2.155.0（与 CodeBuddy CLI 对齐）
   文件数：44
   大小  ：172.9 KB
   SHA256：ae66f5fa...

✅ 已解压到目录：D:\mycode\myworkbuddy2api\wb2api-src
   文件数：44
```

> SHA256 是**可复现**的：打包时已归一元信息（mtime/uid/gid），
> 同样内容连打两次哈希完全一致，可以当真用来对账。
> 包名也可以固定成别的版本：`pack_for_nas.py -o wb2api-9.9.9.tar.gz`。

**把解出来的目录拷到 NAS**：

```bash
scp -r wb2api-src <NAS用户>@<NAS_IP>:/volume1/docker/workbuddy2api/src
```

**compose 指向该目录，并把 image tag 改成包版本**：

```yaml
build:
  context: ./src          # ← 目录，不是 tar 包
  dockerfile: Dockerfile
image: workbuddy2api:2.155.0    # ← 换包时同步改，见下方说明
```

然后照常 `sudo docker compose up -d --build`。

> 不想在本地解压也行 —— 把 tar 包传上去后在 NAS 上解：
> `mkdir -p src && tar -xzf wb2api-2.155.0.tar.gz -C src`，再 `context: ./src`。

**包的硬约束**（`context: ./x.tar.gz` 场景下才需要，但解压同样受益）：

1. **包根就是项目根** —— 不能有 `workbuddy2api/` 这层前缀，
   否则 `COPY requirements.txt .` 找不到文件；
2. 必须含 `Dockerfile`。

`pack_for_nas.py` 已按此约定生成（扁平结构 + 正斜杠路径 + 附 `VERSION`）。
`test_pack_script_produces_flat_archive` 与 `test_pack_script_can_extract_to_directory`
分别守着「打包」和「解压」这两步。

> 无论用哪种方式，`auth/` 和 `logs/` 仍要在 `/volume1/docker/workbuddy2api/` 下
> 手工建好 —— 包只管代码，不管数据。
>
> **image tag 带版本**（如 `workbuddy2api:2.155.0`）是刻意的：
> 固定 tag 会让 `docker compose up -d` 直接复用旧镜像、**掩盖重建**，
> 表现为「改了 environment 没生效」。带版本号后换包就得改 tag，
> 改了必然重建，`docker images` 里也能一眼看出跑的是哪版。
> 改完仍建议走完整套：`down` → `build --no-cache` → `up -d`。
>
> NAS 上核对版本：`cat src/VERSION`，或 `docker inspect` 看 image tag。

### docker compose

先改 `compose.yaml` 里的 auth 挂载路径，再执行：

```bash
docker compose up -d --build
```

### docker run

```bash
docker build -t workbuddy2api .

# 注意：/data/auth 不要加 :ro —— token 刷新需要回写
docker run -d --name workbuddy2api -p 8787:8787 \
  -v /volume1/docker/workbuddy2api/auth:/data/auth \
  -v /volume1/docker/workbuddy2api/logs:/logs \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  -e CODEBUDDY2OPENAI_ENV=auto \
  -e TZ=Asia/Shanghai \
  -e CODEBUDDY2OPENAI_KEY=换成你自己的随机串 \
  --restart unless-stopped \
  workbuddy2api
```

### 相关环境变量

| 变量 | 说明 |
|------|------|
| `CODEBUDDY_AUTH_DIR` | 指定登录态目录 |
| `CODEBUDDY2OPENAI_ENV` | 环境选择：`cn`（国内版）/ `intl`（国际版）/ `auto`（按凭据的 `auth.domain` 自动判定，推荐） |
| `CODEBUDDY2OPENAI_PROXY_CN` | 仅国内版走的代理，如 `http://192.168.1.100:7890` 或 `socks5://...` |
| `CODEBUDDY2OPENAI_PROXY_INTL` | 仅国际版走的代理 |
| `CODEBUDDY2OPENAI_PROXY` | 两个环境都用同一个代理时的兜底（优先级低于上面两个） |
| `CODEBUDDY2OPENAI_AUTH_CN` | 显式指定国内版凭据文件/目录（优先级高于 `CODEBUDDY_AUTH_DIR`） |
| `CODEBUDDY2OPENAI_AUTH_INTL` | 显式指定国际版凭据文件/目录 |
| `CODEBUDDY2OPENAI_KEY` | 本地 API Key |
| `CODEBUDDY2OPENAI_LOG` | 日志路径 |
| `CODEBUDDY2OPENAI_STATE_FILE` | 状态快照路径（默认与 `CODEBUDDY2OPENAI_LOG` 同目录的 `state.json`） |
| `CODEBUDDY2OPENAI_STATE_INTERVAL` | 状态快照的周期刷新秒数，默认 3600；`0` = 只在事件时刷新 |
| `CODEBUDDY2OPENAI_CACHE_DIR` | 模型快照 / 签到留档 / 任务留档的落盘目录 |
| `CODEBUDDY2OPENAI_MODELS_TTL` | 模型列表缓存时长（秒） |
| `CODEBUDDY2OPENAI_CHECKIN_HOURS` | 每日签到的整点时刻，如 `9,21` |
| `CODEBUDDY2OPENAI_TRAVEL_HOURS` | 猫猫旅行的整点时刻，如 `9,21` |
| `CODEBUDDY2OPENAI_ACTIVITY_HOURS` | 活跃上报的整点时刻，如 `10` |
| `CODEBUDDY2OPENAI_KEEPALIVE_HOURS` | token 保活的整点时刻，如 `22` |
| `CODEBUDDY2OPENAI_SCHOOL_HOURS` | 开学季任务的整点时刻，如 `12` |
| `CODEBUDDY2OPENAI_CAT_HOURS` | 夜猫子任务的整点时刻，如 `1` |
| `CODEBUDDY2OPENAI_ACTIVITY_REPORT_COUNT` | 每次活跃上报的条数，默认 `5` |
| `CODEBUDDY2OPENAI_BILLING_UA` | billing 域 User-Agent（留空用内置 CLI UA） |

### 分环境代理怎么选

| 场景 | 配置 |
|---|---|
| 国内版直连、国际版要代理（最常见） | 只填 `CODEBUDDY2OPENAI_PROXY_INTL` |
| 两个环境都走同一个代理 | 只填 `CODEBUDDY2OPENAI_PROXY` |
| 都直连（如海外 VPS 跑国际版） | 都不填 |

代理是**按环境**选的，不是按目标域选的 —— 同一个环境的所有请求都走它自己的代理。
实测走代理比直连慢 3~10 倍，所以别为了省事给直连得了的环境也配代理。

`socks5://` 需要镜像里的 `httpx[socks]`，`requirements.txt` 已含。


---

## 模型列表（动态拉取）

`/v1/models` 不再依赖硬编码表：直接从上游拿，再按 CLI 白名单裁剪。

```text
GET https://copilot.tencent.com/console/enterprises/personal/models
  → 取 data.agents[name=="cli"].models 作为白名单
  → 用 data.models 建索引，只保留「白名单内 且 disabled != true」的条目
  → maxInputTokens → context_length，maxOutputTokens → max_tokens
```

解析顺序（前者可用即返回，逐级降级），可用 `/admin/models` 查看当前生效的来源：

| 顺序 | 来源标记 | 说明 |
|------|------|------|
| 1 | `upstream` / `cache` | 上游拉取 / 1 小时内的内存缓存 |
| 2 | `stale-memory` / `stale-snapshot` | 上游失败时回退到**上一份真实列表**（跨重启也能用落盘快照） |
| 3 | `local-product` | 本机 WorkBuddy 安装目录的 `product.json` |
| 4 | `static` | 内置回退表（最后兜底） |

缓存语义是**惰性判定、非定时刷新**：只有请求进来时才看时间戳。上游拉取失败后的 5 分钟内不会反复重试。
非新鲜来源时，`/v1/models` 响应会多一个 `x_models_source` 字段，便于客户端判断列表是否可能过期。

具体能拉到哪些模型，取决于你的 WorkBuddy / CodeBuddy 订阅与上游当前配置。

---

## 定时积分任务（六类）

转换器内置一个后台调度线程，六类任务**各自独立时点、独立开关**，互不影响：

| 任务 | 默认时点 | 做什么 | 关闭参数 |
|------|------|------|------|
| 签到 | 09 / 21 点 | 每日签到 + 积分余额查询 | `--no-checkin` |
| 活跃上报 | 10 点 | 对话活跃上报（默认 5 条同会话多轮），点亮连登天数、解锁领养前置，回读 streak 自检 | `--no-activity` |
| 猫猫旅行 | 09 / 21 点 | 独立排程：领养 / 派出 / 领奖闭环推进 | `--no-travel` |
| token 保活 | 22 点 | 主动刷新 token；连续 3 次刷新失败才判凭据失效 | `--no-keepalive` |
| 开学季任务 | 12 点 | 任务点亮 + claim + 自动抽空抽奖余额，活动下线时自动跳过 | `--no-school` |
| 夜猫子任务 | 1 点 | 夜猫窗口（23:00–08:00 CST）内补一次 black_cat 任务 | `--no-cat` |

`--no-tasks` 一次关掉全部六类（含签到）。时点用 `--xxx-hours` 改（如 `--activity-hours 10,15`）；
留空 / 非法值一律回落到该类默认时点——**回落不等于禁用**，禁用请用 `--no-xxx`。

### 通用语义

- **到点执行 + 迟到补跑**：休眠 / 挂起后唤醒，会补跑 6 小时内错过的班次；超过 6 小时不补，
  避免重启时翻出昨天的班次；
- **同一整点多类任务并行派发**：签到与旅行都配 09 点时一起触发，慢任务不阻塞其他任务族；
- 每类结果统一收敛成一行结论并落盘留档（`tasks-log.json`），`GET /admin/tasks` 可查最近 5 次；
- 六类任务都按**宿主 / 容器本地时区**的整点触发（Docker 部署务必保留 `TZ=Asia/Shanghai`）；
  只有「夜猫窗口」和「上游自然日」这类上游口径按 CST 固定 +8 计算，不受本机时区影响；
- `/admin/*` 与 `/v1/*` 共用同一套鉴权，公网部署务必设置 `--api-key`。

### 各任务的实现要点

**签到**（`POST www.codebuddy.cn/v2/billing/meter/daily-checkin` + `get-user-resource`）

- 签到前按 2 小时窗口预刷新 token，避免到点时 token 已过期导致当天签不上；
- 签到是幂等的：重复签到返回 `HTTP 400 / code 10001「今天已签到」`，记为「已签到」而不是失败；
- 结果写日志并落盘留档（`checkin-log.json`），`GET /admin/checkin` 可查最近 5 次；
- billing 域对 UA 有校验，不显式设置 UA 会被拒（`403 / code 10085`）。已内置 CLI UA，
  可用 `CODEBUDDY2OPENAI_BILLING_UA` 覆盖。

**活跃上报**（`POST www.codebuddy.cn/v2/report`）

- 默认连发 5 条 `chat_request_send`，**共用同一个 conversationId**（同一会话多轮）、requestId 各自独立；
  条间间隔 1.5s，避免秒发触发风控。条数用 `--activity-report-count` 改（0 / 负数按 1 条）；
- 事件必须带 `userId`：缺了它服务端照样 200 但**静默丢弃**，连登不涨。所以上报完会回读
  `GET /activity/growth/streak` 对账，`days=0` 时打 WARN（`report OK but streak.days=0 (silent drop?)`）；
- 上报把对话量补满后，会立刻重试一次领养猫（豁免旅行侧的当日防抖），就地闭环；
- 任一条上报失败即停止续发（streak 自检与领养一并跳过），单条失败如实记日志。

**猫猫旅行**（`/activity/growth/buddy/*`）

- 每趟只推进一个动作：无猫 → 同意协议 + 领养；`arrived` → 领奖（必须带 `record_id`）；
  `idle` → 派出（地点固定 4，四个地点收益与时长区间完全相同）；`traveling` → 本趟跳过；
- 领养门槛未达（`HTTP 400 first_buddy task not completed yet`）是预期行为，记「当日已试」后当天不再重试；
- 本任务**不刷 token**：查询失败只跳过本轮，刷新交给 22 点的 token 保活。

**token 保活**

- 主动刷新一次 access token，把续期从「下一个请求撞上过期」提前到低峰时点；
- 刷新失败会累计，**连续 3 次**才判「凭据失效」（一次失败很可能只是抖动，直接判死会误伤），
  刷新成功立即清零；判失效时打 ERR 日志并提示重新登录桌面端。

**开学季任务**（`/portal/activity/school/*` + `/v2/report`）

- 只做可自动完成的动作：`share_invite` 走 share-complete；`chat_3_times` / `desktop_chat_1_time` / `expert_use`
  走事件上报（必须带 `activityId=school_open_day_2026`，桌面链路整体上报 6 连事件）；
- `task_student_verify`（微信学生认证）是人工环节，一律跳过、不伪造；服务端新增的未知 task_code 保守跳过；
- 活动下线（`in_period=false`）时任务与抽奖全部跳过、正常返回，不算失败；
- 领完奖会把大转盘抽奖次数**抽到空**（写动作间隔 ≥1s；`409 / code 40900 no chance` 是正常收尾边界）；
  余额不降（服务端异常）连续 3 次即提前停，防死循环。

**夜猫子任务**

- 夜猫窗口 = CST 23:00–08:00；窗口外**一律不发写请求**（上游窗口外上报不计进度，白打还吃风控）；
- 窗口内每次最多补 **1 条** `chat_request_send`（GLM-5.2，`mode=night`），补完回读进度，满了才领奖；
- 已领（`claimed`）跳过；已完成未领直接领奖；服务端 `already_claimed` 按幂等命中处理。

### 手动触发

```bash
curl -X POST "http://127.0.0.1:8787/admin/tasks?task=all"      # 立即跑全部已启用任务
curl -X POST "http://127.0.0.1:8787/admin/tasks?task=travel"   # 只跑猫猫旅行
curl -X POST "http://127.0.0.1:8787/admin/tasks?task=cat"      # 只跑夜猫子
curl -X POST "http://127.0.0.1:8787/admin/checkin"             # 立即签到（沿用旧端点）
curl http://127.0.0.1:8787/admin/tasks                         # 六类排程状态 + 最近结果
```

---

## 项目结构

```text
workbuddy2api/
├── converter.py
├── model_registry.py
├── task_scheduler.py
├── task_result.py
├── growth_api.py
├── checkin.py
├── activity.py
├── travel.py
├── keepalive.py
├── school.py
├── blackcat.py
├── responses_adapter.py
├── responses_projection.py
├── anthropic_adapter.py
├── desensitize.py
├── codex-codebuddy.example.toml
├── test_model_registry.py
├── test_checkin.py
├── test_task_scheduler.py
├── test_growth_api.py
├── test_activity.py
├── test_travel.py
├── test_keepalive.py
├── test_school.py
├── test_blackcat.py
├── test_responses_adapter.py
├── test_anthropic_adapter.py
├── README.md
└── LICENSE
```

各文件作用：

- `converter.py`: 主入口，FastAPI 服务
- `model_registry.py`: 上游动态模型发现 + 内存缓存 + 落盘快照 + 多级回退
- `task_scheduler.py`: 六类定时任务的统一排程（独立时点 / 独立开关 / 并行派发 / 补跑 / 留档）
- `task_result.py`: 任务执行结果的统一结构（任务模块与调度器共用，保持依赖单向）
- `growth_api.py`: 上游 growth / billing 域接口层（账号视图 + 请求头 + 信封与错误语义）
- `checkin.py`: 每日签到、积分余额查询
- `activity.py`: 对话活跃上报 + streak 自检
- `travel.py`: 猫猫旅行巡检状态机（领养 / 派出 / 领奖）
- `keepalive.py`: token 保活（主动刷新 + 连续失败判定）
- `school.py`: 开学季任务点亮 / 领奖 / 抽奖
- `blackcat.py`: 夜猫子任务（夜猫窗口内补一次）
- `responses_adapter.py`: OpenAI Responses ↔ Chat 适配
- `responses_projection.py`: Codex / agent 请求投影压缩
- `anthropic_adapter.py`: Anthropic Messages ↔ Chat 适配
- `desensitize.py`: 运行时文本压缩与零宽脱敏

各任务模块之间**互不 import**：共享的 HTTP 层下沉到 `growth_api.py`，
结果结构下沉到 `task_result.py`，跨任务的回调（活跃上报 → 领养）由调度器在装配处注入。

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
## English

`workbuddy2api` exposes your already logged-in **WorkBuddy / CodeBuddy** desktop session as local **OpenAI- and Anthropic-compatible APIs**.

Supported endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

Recommended use cases:

- **Codex CLI** via `/v1/responses`
- **Claude Code / CC Switch** via `/v1/messages`
- **Cherry Studio / ZCode / LobeChat / Open WebUI** via `/v1/chat/completions`

### Quick Start

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
uv run converter.py --desensitize --log converter.log
```

Then verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### Codex CLI

Use `/v1/responses` and keep `--desensitize` enabled.

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

Run:

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

Use `/v1/messages`:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### Notes

- `--desensitize` is recommended for both Codex CLI and Claude Code
- `/v1/responses` already applies backend-facing projection by default
- `--desensitize --no-compact` preserves more of the original system prompt
- if that still gets review-blocked, `/v1/responses` will retry once in compact mode

### CLI Options

```bash
python3 converter.py [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--skip-check]
                     [--models-ttl SECONDS] [--no-dynamic-models] [--cache-dir PATH]
                     [--checkin-hours H,H] [--no-checkin] [--checkin-on-start] [--checkin-timeout SECONDS]
                     [--travel-hours H,H] [--activity-hours H,H] [--activity-report-count N]
                     [--keepalive-hours H,H] [--school-hours H,H] [--cat-hours H,H]
                     [--no-travel] [--no-activity] [--no-keepalive] [--no-school] [--no-cat] [--no-tasks]
```

`/v1/models` pulls the live model list from the upstream API (CLI-whitelisted, 1h in-memory cache plus an
on-disk snapshot for cross-restart fallback). A background thread runs six independent scheduled tasks —
check-in, activity report, cat travel, token keepalive, school-season tasks, and the night-owl task —
each with its own hours and on/off switch. See the Chinese section above for details.

### Disclaimer

For personal learning and research only. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic.

---

<sub>
Keywords: codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
