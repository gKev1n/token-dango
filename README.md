<p align="center"><img src="plugins/token-dango/assets/dango.svg" width="96" alt="Token 团子"></p>

# Token 团子

ChatGPT 桌面版（Codex）的会话面板插件，在会话右侧的标签页里显示这条会话用了多少 Token。

团子肚子里的液面就是这条会话的上下文占用：60% 以下是抹茶绿，60–80% 是蜂蜜黄，80% 以上是山楂红。表情跟着状态变：生成中眨眼，空闲打盹，快满了冒汗，没有数据时打问号。

> 这是个人做的非官方插件，与 OpenAI 无关。

## 显示什么

- **上下文**：百分比、已用 / 窗口大小、剩余
- **会话累计**：Token 总量、缓存命中率、按 API 价格折算的花费
- **本轮或上一轮**：用时、输出量、约多少 tok/s、折算花费
- **账号额度**：本周或 5 小时窗口的已用百分比和重置时间，取所有会话里最新的一条记录
- **明细**：输入（其中缓存）、输出（其中推理）、最近一次请求的大小、首字时间、模型时间和工具时间、花费拆分、价格来源

面板会套用 ChatGPT 传入的主题和样式变量，支持深色模式。每 2.5 秒刷新一次，标签页不可见时暂停。

## 隐私

只读本机的 `~/.codex/state_5.sqlite`、`~/.codex/config.toml` 和会话日志（rollout JSONL）。会话日志里有聊天内容，插件只取其中的用量和计时字段，聊天内容不会出现在面板里。不读 `auth.json`，不写任何 Codex 文件，不联网。

只用 macOS 自带的 `/usr/bin/python3`，没有任何第三方依赖。

## 要求

- macOS，装有带 Codex 的 ChatGPT 桌面版
- 在 ChatGPT 26.1027.51404（内置 codex-cli 0.166.0-alpha.5.1）上测试过

## 安装

```bash
git clone https://github.com/gKev1n/token-dango.git && cd token-dango
```

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin marketplace add "$PWD"
```

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin add token-dango@token-dango
```

装好后**重启一次 ChatGPT**。安装前就开着的会话不会加载新插件，会显示“未能显示 Token 团子”。

## 打开

在任意会话右侧按 ⌘T 新开标签页，点「更多工具」，选「Token 团子」。打开过的标签页会留在那条会话里。

## 更新与卸载

改了代码或 `git pull` 之后，先 remove 再 add，让 Codex 刷新插件缓存：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin remove token-dango@token-dango
```

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin add token-dango@token-dango
```

彻底卸载时，remove 之后再执行：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin marketplace remove token-dango
```

## 两个估算值的口径

**tok/s**：本轮输出 token ÷（本轮总时长 − 工具执行时间）。工具执行时间来自日志里每次跑命令、调用工具、子代理的起止时间，重叠部分只扣一次。结果包含首字等待、网络延迟和推理 token，所以比模型真正吐字的速度略低。本轮输出少于 200 token 或模型时间不到 1 秒时不显示。

**折算花费**：按 OpenAI API 价格计算“同样的用量走 API 要多少钱”，**不是订阅的实际账单**。

- 价格在 [`prices.json`](plugins/token-dango/prices.json)，单位是美元 / 每百万 token，2026-11-01 从 [官方价格页](https://developers.openai.com/api/docs/pricing) 抄录。价格变了改这个文件即可。
- 每次请求按各自的输入大小分档：≤272K 用短上下文价，>272K 用长上下文价。
- 档位读 `~/.codex/config.toml` 顶层的 `service_tier`：`priority` 按 Fast 档算，其他按标准档算。
- 公式：（输入 − 缓存）× 输入价 + 缓存 × 缓存价 + 输出 × 输出价 + 缓存写入 × 写入价。推理 token 已包含在输出里，不重复计算。
- 价格表里没有的模型显示“—”。
- 没有逐次请求记录的老会话，按累计数和短上下文价粗估，结果偏低，面板会注明。
- 价格按会话当前的模型算。如果一条会话中途换过模型，折算会有偏差。

## 测试

```bash
cd plugins/token-dango && /usr/bin/python3 -m unittest discover -s tests -v
```

## 文件

| 文件 | 作用 |
| --- | --- |
| `plugins/token-dango/server.py` | MCP stdio 服务：声明会话面板入口，增量读取日志，返回用量快照 |
| `plugins/token-dango/panel.html` | 面板界面：团子、指标、明细 |
| `plugins/token-dango/prices.json` | 各模型的 API 价格（标准 / Fast × 短 / 长上下文） |
| `plugins/token-dango/tests/test_server.py` | 19 个测试：累计数、半行、文件替换、跨会话取额度、进行中的轮次、工具时间去重、按请求分档计价、Fast 档、未知模型、老会话粗估等 |

## 许可

[MIT](LICENSE)
