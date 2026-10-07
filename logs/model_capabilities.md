# 模型能力表（上下文空间 + 思考强度）

> 生成时间：2026-10-07 10:16:10　环境：cn　来源：service resolve_models (upstream)

## 契约说明

- **生效窗口**：生效窗口 = contextWindow.defaultLength > min(contextWindow.supportedLengths) > maxInputTokens（对齐上游 resolveEffectiveContextBudget）。maxAllowedSize 是校验上限，不是窗口。
- **思考档位**：off / minimal / low / medium / high / xhigh / max

**各协议入口 → 上游字段**：

| 协议入口 | 发往上游的字段 |
|---|---|
| `chat_completions` | reasoning_effort (扁平) + verbosity |
| `responses` | reasoning_effort (扁平) + verbosity  ← 由 responses_adapter 归一 |
| `anthropic_messages` | reasoning_effort (扁平) + verbosity  ← 由 anthropic_adapter 归一 |

**`thinking.budget_tokens` → 档位**：

| 预算上限 | 映射档位 |
|---|---|
| 0 ~ 2048 | `minimal` |
| 2048 ~ 8192 | `low` |
| 8192 ~ 32768 | `medium` |
| 32768 ~ 65536 | `high` |
| ≥ 65536 | `xhigh` |

**上游注意事项**：

- 上游 Chat 传输只认扁平 reasoning_effort + verbosity，嵌套 reasoning 对象会被忽略
- www.codebuddy.ai 属产品网关，兼容改写层被跳过，字段原样送达服务端
- xhigh/max 在无 thinkingLevelMap 的模型上会被上游折叠为 high

## 模型清单（17 个）

| 模型 | 名称 | 生效窗口 | 窗口档位 | 输入上限 | 输出上限 | 思考 | 默认档 | 可用档位 | 倍率 |
|---|---|---|---|---|---|---|---|---|---|
| `hy4-preview` | Hy4 preview | 960K | - | - | 64K | ✅(强制) | `high` | high | x0.29 credits |
| `hy3` | Hy3 | 192K | - | - | 64K | ✅(强制) | `high` | (全部) | x0.00 credits |
| `hy3-x` | Hy3 | 192K | - | - | 64K | ✅(强制) | `high` | (全部) | x0.05 credits |
| `space-bunny` | Space-Bunny | 1M | - | - | 128K | ✅(强制) | `max` | low / medium / high / xhigh / max | x0.03 credits |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | 1M | - | - | 128K | ✅(强制) | `high` | low / high / max | x0.11 credits |
| `glm-5.3` | GLM-5.3 | 1M | - | - | 64K | ✅(强制) | `medium` | (全部) | x0.79 credits |
| `glm-5.3-flash` | GLM-5.3-Flash | 1M | - | - | 131072 | ✅(强制) | `high` | low / high / max | x0.06 credits |
| `glm-5.2` | GLM-5.2 | 1M | - | - | 64K | ✅(强制) | `medium` | (全部) | x0.79 credits |
| `glm-5.1` | GLM-5.1 | 200K | - | - | 48K | ✅(强制) | `medium` | (全部) | x0.79 credits |
| `glm-5v-turbo` | GLM-5v-Turbo | 200K | - | - | 64K | ✅(强制) | `medium` | (全部) | x0.71 credits |
| `minimax-m3` | MiniMax-M3 | 512K | - | - | 64K | ✅(强制) | `medium` | (全部) | x0.25 credits |
| `minimax-m2.7` | MiniMax-M2.7 | 200K | - | - | 48K | ✅(强制) | `medium` | (全部) | x0.19 credits |
| `kimi-k3-1` | Kimi-K3 | 1M | - | - | 32K | ✅(强制) | `medium` | (全部) | x1.62 credits |
| `kimi-k2.8-preview` | Kimi-K2.8-Preview | 1M | - | - | 64K | ✅(强制) | `high` | low / high / max | x0.77 credits |
| `kimi-k2.7` | Kimi-K2.7-Code | 256K | - | - | 32K | ✅(强制) | `medium` | (全部) | x0.57 credits |
| `kimi-k2.6` | Kimi-K2.6 | 256K | - | - | 32K | ✅(强制) | `medium` | (全部) | x0.52 credits |
| `deepseek-v4-pro` | Deepseek-V4-Pro | 1M | - | - | 128K | ✅(强制) | `high` | (全部) | x0.51 credits |
