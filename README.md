# Pocket Agent 🤖

> 用飞书远程遥控本地电脑上的 AI 编码 Agent —— 走路、通勤、开会间隙，一条消息就能让 Claude Code / opencode / Codex 在你的电脑上干活。

<p align="center">
  <a href="https://github.com/alloevil/pocket-agent/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/alloevil/pocket-agent/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Agents" src="https://img.shields.io/badge/agents-Claude%20Code%20%7C%20opencode%20%7C%20Codex-orange">
</p>

---

## ✨ 特性

- 🔌 **三种 Agent，一套接口** —— Claude Code、opencode、Codex 任选，统一的归一化事件模型，加新 agent 只需实现一个后端子类。
- 💬 **飞书富卡片渲染** —— 流式 Markdown（代码块/列表/表格）、工具调用过程、可折叠思考、彩色 diff 审批。
- 🗂 **多会话管理** —— 同一聊天里 `/new` 开多个对话、`/switch` 切换、`/resume` 恢复历史；不同群/私聊自动隔离。
- 🔐 **远程审批** —— Agent 执行敏感命令/改文件时推审批卡片，手机一点批准或拒绝，支持「本轮全部允许」。
- 📱 **为手机遥控而设计** —— 运行中心跳进度、完成主动提醒、命令纠错、首次引导、超长内容折叠。
- 🛡 **健壮可靠** —— 飞书 API 自动重试、流式去重降级、子进程崩溃自愈、断线重连、首次启动自助引导。
- 🌐 **零公网 IP** —— 飞书侧 WebSocket 长连接主动外连，Agent 侧本地子进程/端口，无需端口映射。

## 📑 目录

- [工作原理](#-工作原理)
- [支持的 Agent](#-支持的-agent)
- [快速开始](#-快速开始)
- [使用](#-使用)
- [配置](#-配置)
- [项目结构](#-项目结构)
- [测试](#-测试)
- [许可证](#-许可证)

## 🧭 工作原理

```
手机飞书 ──消息──► 飞书服务器
                      │  WebSocket 长连接（SDK 主动外连，零公网 IP）
                      ▼
                 Bridge（后端无关）
                   ├─ FeishuEventClient   接收飞书事件
                   ├─ AgentBackend（抽象）──► AgentEvent ──► Renderer ──► 飞书卡片
                   │    ├─ CodexBackend      WebSocket JSON-RPC
                   │    ├─ ClaudeBackend     claude -p 子进程（stream-json）
                   │    └─ OpenCodeBackend   opencode serve + REST/SSE
                   └─ SessionStore        会话分组 / 多会话 / 持久化
```

核心是一套**归一化事件模型**：每个后端把各自的原生协议（WebSocket、子进程 NDJSON、HTTP SSE）翻译成统一的 `AgentEvent`（文本增量 / 思考 / 工具调用 / 审批 / 完成 / 错误），Bridge 只消费这一种事件，Renderer 统一渲染成飞书卡片。三种完全不同的并发模型藏在同一个抽象之后。

## 🤝 支持的 Agent

| Agent | 连接方式 | 流式文本 | 思考过程 | 工具调用 | 会话续接 | 审批 |
|-------|----------|:---:|:---:|:---:|:---:|:---:|
| **Claude Code** | `claude -p` 子进程（stream-json） | ✅ | ✅ | ✅ | `--resume` | ✅ MCP 中继 |
| **opencode** | `opencode serve` + REST/SSE | ✅ | ✅ | ✅ | session 持久 | ✅ permission |
| **Codex** | WebSocket JSON-RPC | ✅ | ✅ | ✅ | thread 持久 | ✅ 卡片审批 |

## 🚀 快速开始

### 前置要求

- Python **3.10+**
- 你要使用的 Agent CLI 之一：
  - Claude Code — `claude --version`
  - opencode — `opencode --version`（需 `opencode auth login` 配好模型）
  - Codex — 另起 `codex remote-control`（默认 `ws://localhost:5123`）

### 1. 一键安装（推荐）

一条命令搞定：安装 uv → 克隆仓库 → 装依赖 → 进入配置向导。

```bash
curl -LsSf https://raw.githubusercontent.com/alloevil/pocket-agent/main/install.sh | sh
```

> 介意 `curl | sh`？先下载看一眼再运行：
> ```bash
> curl -LO https://raw.githubusercontent.com/alloevil/pocket-agent/main/install.sh
> less install.sh && sh install.sh
> ```

脚本默认装到 `~/pocket-agent`（可用 `POCKET_AGENT_DIR=/your/path` 自定义），可重复运行（已存在则更新）。

<details>
<summary>手动安装（用 uv 或传统 venv，点击展开）</summary>

```bash
git clone https://github.com/alloevil/pocket-agent.git
cd pocket-agent

# 方式 A：uv（推荐，比 pip 快一两个数量级，自动管理虚拟环境）
# 装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                          # 按 uv.lock 安装精确依赖
uv run python main.py setup      # 进入配置向导

# 方式 B：标准 venv + pip
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
python main.py setup
```
</details>

> 一键脚本只把**环境**准备到「可以开始配置」——飞书凭证仍需你到开放平台获取（见下一步），向导会引导你完成。

### 2. 创建飞书应用

> 配置向导会分步引导你完成这一步并附直达链接；这里列出供参考。

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用，启用机器人能力
2. 权限管理 → **批量导入**，粘贴一段权限 JSON（格式 `{"scopes":{"tenant":[...],"user":[...]}}`）：
   - 运行 `uv run python main.py setup`，向导第 ① 步会打印**可直接复制的完整权限 JSON**（一次开齐 im / docs / sheets / base / wiki / task 等全部能力，为日后扩展预留）
   - 本程序**核心只需** `im:message.p2p_msg:readonly`、`im:message:send_as_bot`；推荐再开 `application:application:self_manage`（自动识别主人、免手动绑定）
3. 事件与回调 → 订阅方式 → **使用长连接接收事件**
4. 添加事件 `im.message.receive_v1`，添加回调 `card.action.trigger`
5. 发布应用，在「凭证与基础信息」拿到 **App ID** 和 **App Secret**

### 3. 配置

一键安装会自动进入**配置向导**；手动安装则运行：

```bash
uv run python main.py setup        # 标准 venv 下用 python main.py setup
```

向导全程引导：① 分步教你建飞书应用 → ② 填凭证并**立即验证**有效性（支持从后台**整段粘贴**自动识别 App ID / Secret）→ ③ 选 agent →
④ **绑定使用者**（开了 `self_manage` 权限则**自动识别主人、零操作**；否则回退到「启动后给机器人发条消息」自动绑定）→ 保存并启动。

> 开箱**私有默认**：绑定后只有你能用，其他人发来的消息静默忽略；且**永远锁不死自己**——
> 程序运行时会周期性从飞书重新确认应用所有者。

<details>
<summary>或手动编辑 config.json（点击展开）</summary>

```bash
uv run python main.py     # 首次运行自动生成 config.json 并提示
# 编辑 config.json：填入飞书凭证、选择 agent、设置 workdir
uv run python main.py     # 再次运行启动
```

> 首次直接运行不会报错崩溃：缺 `config.json` 会自动从模板生成并提示下一步；
> 配置缺凭证 / agent 无效 / CLI 未安装时，启动前会给出清晰提示。
</details>

## 📲 使用

在飞书里给机器人**直接发文字**即是给 Agent 的指令，例如「看看当前目录有哪些文件」「修复登录接口的报错」「跑一下测试」。

| 命令 | 说明 |
|------|------|
| `/new` | 新对话（旧对话保留，可 `/list` 查看） |
| `/list` | 列出当前聊天的所有对话 |
| `/switch <id>` | 切换对话 |
| `/resume <id>` | 恢复历史对话（续接上下文） |
| `/cd <路径>` | 切换工作目录（本会话） |
| `/model <名>` | 切换模型（本会话，下一轮生效） |
| `/stop` | 中断当前任务 |
| `/usage` | 本会话用量统计（轮数 / 工具调用 / 成本） |
| `/agent` | 查看当前 agent |
| `/help` | 帮助 |

**消息卡片**随流式输出实时刷新，包含运行状态、可折叠思考、工具调用列表、Markdown 正文、耗时与成本页脚；出错时附带「重试」按钮，文件修改审批展示彩色 diff。

## ⚙️ 配置

`config.json` 字段（完整模板见 [`config.example.json`](config.example.json)）：

| 字段 | 说明 | 默认 |
|------|------|------|
| `agent` | 默认后端：`codex` / `claude` / `opencode` | `codex` |
| `workdir` | claude/opencode 子进程的工作目录 | `.` |
| `feishu_app_id` / `feishu_app_secret` | 飞书应用凭证 | — |
| `codex_ws_url` | Codex remote-control 地址 | `ws://127.0.0.1:5123` |
| `claude_model` / `opencode_model` | 模型（空=各自默认） | `""` |
| `show_thinking` | 是否展示折叠思考面板 | `true` |
| `throttle_seconds` | 卡片流式更新节流间隔 | `3.0` |
| `min_delta_chars` | 流式更新最小增量字符（省 API 调用） | `30` |
| `idle_minutes` | 空闲超时自动开新会话防上下文漂移；0=关闭 | `0` |
| `persist_path` | 会话持久化文件路径；空=不持久化 | `""` |
| `heartbeat_seconds` | 运行中心跳刷新间隔；0=关闭 | `15` |
| `notify_done_seconds` | 长任务完成主动提醒阈值；0=关闭 | `0` |
| `allowed_users` | 用户白名单（逗号分隔 open_id，空=不限） | `""` |
| `bot_owner` | 应用所有者 open_id（向导/运行时自动写入，永远放行，锁不死自己） | `""` |
| `private_by_default` | 开箱私有：未配白名单且已知所有者时仅所有者可用 | `true` |

## 🗂 项目结构

```
pocket-agent/
├── main.py                       # 入口 / 配置向导
├── install.sh                    # 一键安装脚本
├── config.example.json           # 配置模板
├── pyproject.toml                # 依赖与项目元数据
├── uv.lock                       # 锁定的精确依赖（可复现安装）
├── pocket_agent/
│   ├── events.py                 # 归一化事件模型 AgentEvent / AgentSession
│   ├── config.py                 # 配置加载与校验
│   ├── bridge.py                 # 后端无关桥接核心
│   ├── session_store.py          # 会话分组 / 多会话 / 持久化
│   ├── renderer_base.py          # 平台无关的 Renderer 抽象接口
│   ├── renderer.py               # 飞书富卡片渲染
│   ├── feishu_client.py          # 飞书 API（含重试）+ WebSocket 事件
│   ├── codex_client.py           # Codex WebSocket JSON-RPC 客户端
│   ├── mcp_approval.py           # in-process MCP server（Claude 审批中继）
│   ├── app.py                    # 应用启动
│   └── backends/                 # codex / claude / opencode 后端
└── tests/                        # 80 个测试，12 个文件
```

## 🧪 测试

```bash
uv run python tests/run_all.py
```

包含离线解析测试（喂录制的 NDJSON / SSE / JSON-RPC 样本断言事件序列）和真实
CLI 端到端测试（claude / opencode / mock WebSocket server）；未安装的 CLI 自动跳过。

## 📄 许可证

[MIT](LICENSE)
