English | [简体中文](README.zh-CN.md)

# Pocket Agent 🤖

> Remote-control the AI coding agents on your computer from Feishu (Lark) — while walking, commuting, or between meetings, a single message gets Claude Code / opencode / Codex working on your machine.

<p align="center">
  <a href="https://github.com/alloevil/pocket-agent/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/alloevil/pocket-agent/actions/workflows/tests.yml/badge.svg"></a>
  <a href="https://github.com/alloevil/pocket-agent/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/alloevil/pocket-agent?logo=github&color=blue"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Agents" src="https://img.shields.io/badge/agents-Claude%20Code%20%7C%20opencode%20%7C%20Codex-orange">
</p>

---

## ✨ Features

- 🔌 **Three agents, one interface** — pick any of Claude Code, opencode, or Codex; a unified normalized event model means adding a new agent only requires implementing one backend subclass.
- 💬 **Rich Feishu card rendering** — streaming Markdown (code blocks / lists / tables), tool-call progress, collapsible thinking, colored diff approvals.
- 🗂 **Multi-session management** — `/new` opens multiple conversations in the same chat, `/switch` to switch, `/resume` to restore history; different groups / DMs are automatically isolated.
- 🔐 **Remote approvals** — when the agent runs a sensitive command or edits a file, an approval card is pushed; approve or reject with one tap on your phone, with "allow all for this turn" support.
- 📱 **Designed for phone-based remote control** — heartbeat progress while running, proactive completion notifications, command auto-correction, first-run onboarding, folding for very long content.
- 🛡 **Robust and reliable** — automatic Feishu API retries, streaming dedup and degradation, subprocess crash self-healing, reconnect on disconnect, self-service first-run onboarding.
- 🌐 **Zero public IP** — the Feishu side uses an outbound WebSocket long connection, the agent side uses local subprocesses/ports; no port forwarding needed.

## 📑 Table of Contents

- [How It Works](#-how-it-works)
- [Supported Agents](#-supported-agents)
- [Quick Start](#-quick-start)
- [Usage](#-usage)
- [Configuration](#-configuration)
- [Project Structure](#-project-structure)
- [Testing](#-testing)
- [License](#-license)

## 🧭 How It Works

```
Feishu on phone ──message──► Feishu servers
                                │  WebSocket long connection (SDK dials out, zero public IP)
                                ▼
                           Bridge (backend-agnostic)
                             ├─ FeishuEventClient   receives Feishu events
                             ├─ AgentBackend (abstract) ──► AgentEvent ──► Renderer ──► Feishu cards
                             │    ├─ CodexBackend      WebSocket JSON-RPC
                             │    ├─ ClaudeBackend     claude -p subprocess (stream-json)
                             │    └─ OpenCodeBackend   opencode serve + REST/SSE
                             └─ SessionStore        session grouping / multi-session / persistence
```

The core is a **normalized event model**: each backend translates its own native protocol (WebSocket, subprocess NDJSON, HTTP SSE) into a unified `AgentEvent` (text delta / thinking / tool call / approval / done / error). The Bridge only consumes this one kind of event, and the Renderer uniformly renders them into Feishu cards. Three completely different concurrency models hide behind the same abstraction.

## 🤝 Supported Agents

| Agent | Connection | Streaming text | Thinking | Tool calls | Session resume | Approvals |
|-------|----------|:---:|:---:|:---:|:---:|:---:|
| **Claude Code** | `claude -p` subprocess (stream-json) | ✅ | ✅ | ✅ | `--resume` | ✅ MCP relay |
| **opencode** | `opencode serve` + REST/SSE | ✅ | ✅ | ✅ | persistent session | ✅ permission |
| **Codex** | WebSocket JSON-RPC | ✅ | ✅ | ✅ | persistent thread | ✅ card approval |

## 🚀 Quick Start

### Prerequisites

- Python **3.10+**
- One of the agent CLIs you want to use:
  - Claude Code — `claude --version`
  - opencode — `opencode --version` (run `opencode auth login` to configure a model)
  - Codex — start `codex remote-control` separately (default `ws://localhost:5123`)

### 1. One-Command Install (Recommended)

One command does it all: installs uv → clones the repo → installs dependencies → enters the setup wizard.

```bash
curl -LsSf https://raw.githubusercontent.com/alloevil/pocket-agent/main/install.sh | sh
```

> Wary of `curl | sh`? Download it and take a look first:
> ```bash
> curl -LO https://raw.githubusercontent.com/alloevil/pocket-agent/main/install.sh
> less install.sh && sh install.sh
> ```

The script installs to `~/pocket-agent` by default (customize with `POCKET_AGENT_DIR=/your/path`) and is safe to re-run (updates if already present).

<details>
<summary>Manual install (with uv or a traditional venv, click to expand)</summary>

```bash
git clone https://github.com/alloevil/pocket-agent.git
cd pocket-agent

# Option A: uv (recommended — one to two orders of magnitude faster than pip, manages the virtualenv automatically)
# Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                          # install exact dependencies from uv.lock
uv run python main.py setup      # enter the setup wizard

# Option B: standard venv + pip
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
python main.py setup
```
</details>

> The one-command script only prepares the **environment** up to "ready to configure" — you still need to obtain your Feishu credentials from the open platform (see the next step); the wizard walks you through it.

### 2. Create a Feishu App

> The setup wizard guides you through this step-by-step with direct links; the list below is for reference.

1. Create a self-built enterprise app on the [Feishu Open Platform](https://open.feishu.cn/) and enable the bot capability
2. Permissions → **Batch import**, paste a permissions JSON (format `{"scopes":{"tenant":[...],"user":[...]}}`):
   - Run `uv run python main.py setup`; step ① of the wizard prints a **ready-to-copy complete permissions JSON** (opens up im / docs / sheets / base / wiki / task and all other capabilities at once, reserved for future expansion)
   - The **core only needs** `im:message.p2p_msg:readonly` and `im:message:send_as_bot`; also recommended is `application:application:self_manage` (auto-detects the owner, no manual binding)
3. Events & Callbacks → Subscription method → **Receive events over a long connection** (⚠️ choosing Webhook means the long connection connects but receives no messages)
4. Add the event `im.message.receive_v1` and the callback `card.action.trigger` (⚠️ **missing the event = messages get zero response with no error**, the most common pitfall)
5. Publish the app (status must be "Published"), then grab the **App ID** and **App Secret** from "Credentials & Basic Info"

### 3. Configure

The one-command install automatically enters the **setup wizard**; for manual installs, run:

```bash
uv run python main.py setup        # with a standard venv, use: python main.py setup
```

The wizard guides you all the way: ① walks you through creating the Feishu app step by step → ② enter credentials, **validated immediately** (supports **pasting the whole block** from the console with auto-detection of App ID / Secret) → ③ pick an agent →
④ **bind the user** (with the `self_manage` permission enabled, the owner is **auto-detected — zero action needed**; otherwise it falls back to "send the bot a message after startup" auto-binding) → save and start.

> **Private by default** out of the box: after binding, only you can use it; messages from anyone else are silently ignored. And you can **never lock yourself out** —
> while running, the program periodically re-confirms the app owner from Feishu.

<details>
<summary>Or edit config.json manually (click to expand)</summary>

```bash
uv run python main.py     # first run auto-generates config.json and prompts you
# Edit config.json: fill in Feishu credentials, pick an agent, set workdir
uv run python main.py     # run again to start
```

> The first direct run won't crash with an error: a missing `config.json` is auto-generated from the template with next-step hints;
> missing credentials / invalid agent / CLI not installed all produce clear messages before startup.
</details>

## 📲 Usage

**Sending plain text** to the bot in Feishu is the instruction to the agent, e.g. "list the files in the current directory", "fix the error in the login endpoint", "run the tests".

| Command | Description |
|------|------|
| `/new` | New conversation (old ones are kept; view with `/list`) |
| `/list` | List all conversations in the current chat |
| `/switch <id>` | Switch conversation |
| `/resume <id>` | Resume a historical conversation (continue its context) |
| `/history` | List local claude history sessions (`/history all` shows all projects); reattach with `/resume <id>` |
| `/rename <name>` | Rename the current conversation |
| `/delete <id>` | Delete a conversation |
| `/clear` | Clear the current conversation's context (keep the id; next turn starts fresh) |
| `/cd <path>` | Change working directory (this session) |
| `/pwd` | Show the current working directory |
| `/model <name>` | Switch model (this session, takes effect next turn) |
| `/retry` | Resend the last instruction |
| `/loop <task>` | Have the agent iterate repeatedly until it self-assesses completion (`/loop <rounds> <task>` sets a cap) |
| `/stop` | Interrupt the current task |
| `/status` | Overview of current agent / working directory / active sessions / running state |
| `/usage` | Usage stats for this session (turns / tool calls / cost) |
| `/agent` | Show the current agent |
| `/help` | Help |

> Command responses are all **colored cards** (green for success / blue for info / red for errors) — recognizable at a glance.

The **message card** refreshes in real time with the streaming output: run status, collapsible thinking, tool-call list, Markdown body, and a footer with elapsed time and cost. On errors it includes a "Retry" button, and file-edit approvals show a colored diff.

## ⚙️ Configuration

`config.json` fields (full template in [`config.example.json`](config.example.json)):

| Field | Description | Default |
|------|------|------|
| `agent` | Default backend: `codex` / `claude` / `opencode` | `codex` |
| `workdir` | Working directory for the claude/opencode subprocess | `.` |
| `feishu_app_id` / `feishu_app_secret` | Feishu app credentials | — |
| `codex_ws_url` | Codex remote-control address | `ws://127.0.0.1:5123` |
| `claude_model` / `opencode_model` | Model (empty = each backend's default) | `""` |
| `show_thinking` | Show the collapsible thinking panel | `true` |
| `throttle_seconds` | Throttle interval for streaming card updates | `3.0` |
| `min_delta_chars` | Minimum delta characters per streaming update (saves API calls) | `30` |
| `idle_minutes` | Auto-start a new session after idle timeout to prevent context drift; 0 = off | `0` |
| `persist_path` | Session persistence file path; empty = no persistence | `""` |
| `heartbeat_seconds` | Heartbeat refresh interval while running; 0 = off | `15` |
| `notify_done_seconds` | Threshold for proactive completion notification on long tasks; 0 = off | `0` |
| `allowed_users` | User allowlist (comma-separated open_ids, empty = unrestricted) | `""` |
| `bot_owner` | App owner open_id (auto-written by the wizard/runtime, always allowed, can never lock yourself out) | `""` |
| `private_by_default` | Private out of the box: with no allowlist and a known owner, only the owner may use it | `true` |

## 🗂 Project Structure

```
pocket-agent/
├── main.py                       # Entry point / setup wizard
├── install.sh                    # One-command install script
├── config.example.json           # Config template
├── pyproject.toml                # Dependencies and project metadata
├── uv.lock                       # Locked exact dependencies (reproducible installs)
├── pocket_agent/
│   ├── events.py                 # Normalized event model AgentEvent / AgentSession
│   ├── config.py                 # Config loading and validation
│   ├── bridge.py                 # Backend-agnostic bridge core
│   ├── session_store.py          # Session grouping / multi-session / persistence
│   ├── renderer_base.py          # Platform-agnostic Renderer abstract interface
│   ├── renderer.py               # Rich Feishu card rendering
│   ├── feishu_client.py          # Feishu API (with retries) + WebSocket events
│   ├── codex_client.py           # Codex WebSocket JSON-RPC client
│   ├── mcp_approval.py           # In-process MCP server (Claude approval relay)
│   ├── app.py                    # Application startup
│   └── backends/                 # codex / claude / opencode backends
└── tests/                        # 80 tests across 12 files
```

## 🧪 Testing

```bash
uv run python tests/run_all.py
```

Includes offline parsing tests (feeding recorded NDJSON / SSE / JSON-RPC samples and asserting event sequences) and real
CLI end-to-end tests (claude / opencode / mock WebSocket server); CLIs that aren't installed are skipped automatically.

## 📄 License

[MIT](LICENSE)
