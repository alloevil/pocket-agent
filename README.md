# 飞书 Agent 遥控器 🤖

通过飞书消息远程控制本地电脑上的 AI 编码 Agent —— 支持 **Claude Code / opencode / Codex**，统一的富消息展示（Markdown + 工具调用 + 折叠思考）。

> 对标并超越 [cc-connect](https://github.com/chenhg5/cc-connect)：同样把各 agent 当后端统一驱动，但用「归一化事件模型 + 可插拔后端」架构，三种不同并发模型（持久 WebSocket、流式子进程、HTTP server）藏在同一个抽象后，飞书侧做统一富渲染。

## 支持的 Agent

| Agent | 连接方式 | 流式文本 | 思考过程 | 工具调用 | 会话续接 | 审批 |
|-------|----------|:---:|:---:|:---:|:---:|:---:|
| **Claude Code** | `claude -p` 子进程（stream-json） | ✅ | ✅ thinking | ✅ | `--resume` | ✅ MCP 中继 |
| **opencode** | `opencode serve` + REST/SSE | ✅ | ✅ reasoning | ✅ | session 持久 | ✅ permission |
| **Codex** | WebSocket JSON-RPC | ✅ | ✅ | ✅ | thread 持久 | ✅ 卡片审批 |

## 架构

```
手机飞书 ──消息──► 飞书服务器
                      │ WebSocket 长连接（SDK 主动连出）
                      ▼
                 Bridge（后端无关）
                   ├─ FeishuEventClient   飞书事件接收（lark-oapi）
                   ├─ AgentBackend（抽象）─► emit(AgentEvent) ─► CardRenderer ─► 飞书卡片
                   │    ├─ CodexBackend    WebSocket JSON-RPC
                   │    ├─ ClaudeBackend   claude -p 子进程
                   │    └─ OpenCodeBackend opencode serve + SSE
                   └─ SessionManager       用户 ↔ 会话映射
```

**零公网 IP** — 飞书侧是 SDK 往外连的长连接；agent 侧是本地子进程/本地端口。

**归一化事件** (`pocket_agent/events.py`)：所有后端把原生协议翻译成统一的
`AgentEvent`（text_delta / thinking_delta / tool_start / tool_end /
approval_request / turn_done / error），bridge 只消费这一种事件，renderer 统一渲染。
新增第 4 个 agent 只需实现一个 `AgentBackend` 子类，无需改 bridge。

## 富消息展示

每一轮对话用一张**交互卡片**，随流式输出原地更新（PATCH，按 `throttle_seconds` 节流）：

```
┌─ 🤖 claude · ⏳运行中 ───────────────────────┐
│ 🧠 思考过程  ▾  (collapsible_panel，可开关折叠) │
│ 🔧 工具调用                                     │
│   ✅ `Bash` — ls -la                            │
│   ⏳ `Read` — /tmp/foo.py                       │
│ ───────────────────────────────────────────── │
│ <正文 Markdown，lark_md：**加粗** `代码` 列表>   │
└────────────────────────────────────────────────┘
```

- **Markdown**：正文用飞书 **markdown 组件**（非 lark_md），完整支持列表、代码块、
  标题、分割线——对 AI 编码 agent 的输出（大量代码/列表）是刚需。需飞书客户端 7.6+。
- **工具调用**：工具名 + 关键参数摘要 + 状态图标（⏳运行 / ✅完成 / ❌出错）。
- **思考过程**：飞书可折叠面板，`show_thinking: false` 可整体关闭。
- **完成页脚**：耗时 ⏱ + 成本 💰（后端提供时）+ 工具调用次数。
- **长输出分条**：超过 `max_message_length` 自动封板当前卡片、另起续接卡片（标 `#2`），
  不截断丢内容。

## 体验细节（手机遥控友好）

为"看不见终端 / 怕失控 / 碎片化阅读"三个手机遥控痛点做的优化：

- **运行中心跳**：静默期卡片刷新「⏳ 已运行 45s · 3 个工具」，让你知道没卡死。
- **非文本回执**：发语音/图片/文件时明确回「暂只支持文字」，不再石沉大海。
- **完成主动提醒**：长任务（`notify_done_seconds`）完成时单独发一条，避免错过。
- **审批带上下文**：审批卡片显示会话/目录/本轮第几次，并提供「⏭ 本轮全部允许」。
- **完成变更摘要**：完成卡片列出「📝 改动文件：foo.py、bar.py」。
- **命令纠错**：`/lst` → 提示「你是说 /list 吗？」。
- **首次欢迎**：新会话首条消息先给引导卡片（仅一次）。
- **超长正文折叠**：完成态长回复默认折叠摘要，点击展开全文。

## 审批流程

三个后端的审批最终都归一为 `APPROVAL_REQUEST` 事件 → 飞书审批卡片（允许/拒绝按钮）
→ 用户点击 → 异步回传决定。各自的传输不同：

- **Codex**：WS 上服务端发 `requestApproval` 请求，同一条 WS 回响应。
- **opencode**：SSE 推 `permission.updated`，`POST /permissions/{id}` 回 once/reject。
- **Claude Code**：子进程模式用 `--permission-prompt-tool` 把权限提示路由到一个
  **in-process MCP server**（`pocket_agent/mcp_approval.py`，Streamable-HTTP transport，
  跑在 bridge 自己的事件循环里，URL 按会话带 token）。工具 await 用户决定后返回
  `{"behavior":"allow","updatedInput":...}` 或 `{"behavior":"deny","message":...}`。

> Claude `--permission-mode default` 只对真正有副作用的工具（Write/Edit/危险 Bash）
> 发审批，只读命令（如 `ls`）自动放行——避免审批卡片刷屏。可用 `approvals=False`
> 关闭。

## 快速开始

### 1. 安装依赖

```bash
cd pocket-agent
pip install -r requirements.txt
```

确保你要用的 agent CLI 已安装：
- Claude Code：`claude --version`
- opencode：`opencode --version`
- Codex：另起 `codex remote-control`（默认 `ws://localhost:5123`）

### 2. 配置飞书应用

1. [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用
2. 启用机器人能力
3. 添加权限：`im:message.p2p_msg:readonly`、`im:message:send_as_bot`
4. 事件与回调 → 订阅方式 → **使用长连接接收事件**
5. 添加事件：`im.message.receive_v1`；添加回调：`card.action.trigger`
6. 发布应用

### 3. 配置并启动

```bash
python main.py            # 首次运行会自动生成 config.json 并提示去填
# 编辑 config.json：填飞书凭证，选 agent，设 workdir
python main.py            # 再次运行启动
# 或：python main.py setup —— 交互式配置向导（含连接测试）
```

> 首次直接 `python main.py` 不会报错崩溃：若没有 config.json，会自动从模板生成并
> 引导你填写；配置缺凭证 / agent 无效 / CLI 未安装时，启动前会给出清晰的中文提示。

### 4. 使用

飞书中给机器人发消息：
- 直接发文字 → 当前 agent 处理（流式回复）
- `/new` — 新对话（旧会话保留，可 /list 查看）
- `/list` — 列出本会话所有对话
- `/switch <id>` — 切换对话
- `/resume <id>` — 恢复历史对话（续接上下文）
- `/cd <路径>` — 切换工作目录（本会话）
- `/model <名>` — 切换模型（本会话，下一轮生效）
- `/stop` — 中断任务
- `/usage` — 本会话用量统计（轮数 / 工具调用 / 累计成本）
- `/agent` — 查看当前 agent
- `/help` — 帮助

> 多会话：同一飞书会话里可用 `/new` 开多个对话、`/switch` 切换；不同飞书群/私聊
> 自动隔离。出错时弹「↻ 重试」按钮。文件修改审批展示彩色 diff。

## 配置项

| 字段 | 说明 | 默认 |
|------|------|------|
| `agent` | 默认后端：`codex` / `claude` / `opencode` | `codex` |
| `workdir` | claude/opencode 子进程的工作目录 | `.` |
| `codex_ws_url` | Codex remote-control 地址 | `ws://127.0.0.1:5123` |
| `claude_model` | Claude 模型（空=默认） | `""` |
| `claude_extra_args` | 透传给 `claude` 的额外参数数组 | `[]` |
| `opencode_model` | `provider/model`（空=默认） | `""` |
| `opencode_port` | server 端口（0=4096） | `0` |
| `show_thinking` | 是否展示折叠思考面板 | `true` |
| `throttle_seconds` | 卡片流式更新节流间隔 | `3.0` |
| `min_delta_chars` | 流式更新最小增量字符（不够不 PATCH，省 API） | `30` |
| `idle_minutes` | 空闲超此分钟自动开新会话防上下文漂移；0=关闭 | `0` |
| `persist_path` | 会话持久化文件路径（空=不持久化），如 `~/.pocket-agent/sessions.json` | `""` |
| `heartbeat_seconds` | 运行中心跳刷新间隔，静默期显示「已运行 Xs」；0=关闭 | `15` |
| `notify_done_seconds` | 长任务（超此秒数）完成时主动发提醒；0=关闭 | `0` |
| `allowed_users` | 用户白名单（逗号分隔 open_id，空=不限） | `""` |

## 测试

```bash
python tests/run_all.py
```

包含离线解析测试（喂录制的 NDJSON/SSE/JSON-RPC 样本断言事件序列）和真实
CLI 端到端测试（claude / opencode / mock Codex server）；未安装的 CLI 自动跳过。

## 项目结构

```
pocket-agent/
├── main.py                       # 入口 / 配置向导
├── config.example.json
├── requirements.txt
├── pocket_agent/
│   ├── config.py                 # 配置
│   ├── events.py                 # 归一化事件模型 AgentEvent / AgentSession
│   ├── session_store.py          # 会话存储：按(用户,chat)分组+多会话+持久化
│   ├── bridge.py                 # 后端无关桥接核心
│   ├── renderer_base.py          # 平台无关的 Renderer 抽象接口
│   ├── renderer.py               # CardRenderer：AgentEvent → 飞书富卡片
│   ├── feishu_client.py          # 飞书 API（含重试）+ WebSocket 事件
│   ├── codex_client.py           # Codex WebSocket JSON-RPC 客户端
│   ├── mcp_approval.py           # in-process MCP server（Claude 审批中继）
│   ├── app.py                    # 应用启动
│   └── backends/
│       ├── base.py               # AgentBackend 抽象基类
│       ├── codex.py              # Codex 后端
│       ├── claude.py             # Claude Code 后端
│       └── opencode.py           # opencode 后端
└── tests/
    ├── run_all.py
    ├── test_renderer.py
    ├── test_sessions.py
    ├── test_ux.py
    ├── test_startup.py
    ├── test_bridge_integration.py
    ├── test_streaming_robustness.py
    ├── test_cc_connect_features.py
    ├── test_cc_connect_features2.py
    ├── test_codex_backend.py
    ├── test_claude_backend.py
    ├── test_claude_approval.py
    └── test_opencode_backend.py
```

## 借鉴自 cc-connect

读了 [cc-connect](https://github.com/chenhg5/cc-connect)（成熟的 Go 多平台实现）源码后，移植了几个它趟过坑的健壮性机制：

- **飞书 API 重试**（`feishu_client._request`）：瞬时网络错误指数退避重试（0.5→5s）；token 失效码自动刷新重试一次；限流码不重试。
- **流式更新去重 + 降级**：相同内容跳过 PATCH（飞书对相同内容 PATCH 会失败）；连续失败 3 次降级，停止流式只发终态。
- **最小增量门槛** `min_delta_chars`：新增字符不够不 PATCH，省 API 调用。
- **`/stop` 定格卡片**：中断后到达的在途事件不再改卡片（仿 cc-connect `freeze()`）。
- **表格切分**（`split_markdown_by_tables`）：正文表格过多时按表格边界拆成多条。
- **会话用量** `/usage`：累计轮数 / 工具调用 / 成本。
- **渲染抽象** `renderer_base.Renderer`：bridge 依赖抽象接口，加新平台只需实现一个 Renderer 子类。
- **代码围栏感知分块**：长输出切分时不切断 ``` 代码块（否则两段都渲染失效）。
- **@提及剥离**：群聊 @机器人时剥离 `@_user_1` 占位符，只把干净正文给 agent。
- **工具列表紧凑**：工具调用 >10 时折叠中间，只显示前 6 后 3。
- **空闲会话轮换**：`idle_minutes` 超时自动开新会话防上下文漂移。
- **图片/文件回传**：agent 生成的图片（Write 出 .png 等）自动上传飞书发给用户。

## 参考

- [Claude Code Headless 模式](https://code.claude.com/docs/en/headless.md) — `claude -p --output-format stream-json`
- [opencode Server API](https://opencode.ai/docs/server/) — REST + SSE
- [Codex app-server 协议](https://developers.openai.com/codex/) — JSON-RPC over WebSocket
- [cc-connect](https://github.com/chenhg5/cc-connect) — Go 版参考实现
- [飞书开放平台](https://open.feishu.cn/) — API 文档
