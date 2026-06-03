"""归一化事件模型 — 所有 agent 后端都翻译成这套统一事件

不同 agent（Codex/Claude Code/opencode）的原生协议差异很大，但对飞书侧的
展示需求是一致的：流式正文、思考过程、工具调用、审批、完成。这里定义一套
与后端无关的事件，后端负责翻译，renderer 负责展示。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EventKind(str, Enum):
    SESSION_ID = "session_id"          # 后端回传原生会话 id（用于续接）
    TURN_STARTED = "turn_started"      # 一轮开始（携带 turn id）
    TEXT_DELTA = "text_delta"          # 正文增量
    THINKING_DELTA = "thinking_delta"  # 思考/推理增量
    TOOL_START = "tool_start"          # 工具开始调用
    TOOL_END = "tool_end"              # 工具调用结束
    APPROVAL_REQUEST = "approval_request"  # 需要用户审批
    FILE_OUTPUT = "file_output"        # agent 产出了图片/文件，需回传用户
    TURN_DONE = "turn_done"            # 一轮结束
    ERROR = "error"                    # 出错


@dataclass
class AgentEvent:
    """后端 → bridge 的统一事件"""
    kind: EventKind

    # 路由：事件所属的后端原生会话 id。bridge 用它把事件投递到正确的
    # AgentSession（Codex/opencode 是共享后端，靠这个区分用户）。
    # 空字符串表示「当前唯一活跃会话」或后端无法提供（由 bridge 兜底）。
    route_id: str = ""

    # 文本类（text_delta / thinking_delta）
    text: str = ""

    # 工具类（tool_start / tool_end）
    tool_call_id: str = ""
    tool_name: str = ""
    tool_input: Optional[dict] = None
    tool_output: str = ""
    tool_status: str = ""              # "completed" | "error"

    # 审批类（approval_request）
    approval_id: str = ""              # 回传审批时用
    approval_kind: str = ""            # "command" | "file"
    approval_text: str = ""            # 展示给用户的内容（命令/原因）
    approval_detail: str = ""          # 附加信息（cwd 等）

    # 文件产出（file_output）
    file_path: str = ""                # 本地文件绝对路径
    is_image: bool = False             # 是否图片（决定用 image 还是 file 消息）

    # 会话/完成
    session_id: str = ""
    cost_usd: float = 0.0

    # 错误
    error: str = ""

    # 原始负载（调试用）
    raw: Optional[dict] = None


@dataclass
class AgentSession:
    """跨后端通用的会话状态

    一个飞书用户对应一个 AgentSession。后端原生的 thread/session id 存在
    native_id 里，由各后端自行解释。
    """
    user_id: str
    chat_id: str

    # 本地短会话 id（/list /switch /resume 用，区别于后端 native_id）
    session_id: str = ""
    # 会话标题（首条消息截取或自定义，/list 展示）
    title: str = ""

    # 后端原生会话标识（Codex threadId / Claude session_id / opencode sessionID）
    native_id: str = ""
    # 后端私有的稳定路由 token（如 Claude 审批 MCP 的 per-session token）
    mcp_token: str = ""
    # 当前 turn 标识（部分后端中断时需要）
    current_turn_id: str = ""
    turn_active: bool = False

    # ── 会话级覆盖（/cd /model，只影响本会话）──
    workdir_override: str = ""         # 空=用全局 config.workdir
    model_override: str = ""           # 空=用全局默认模型

    # 上一轮用户 prompt（错误重试用）
    last_prompt: str = ""

    # ── 飞书渲染侧状态 ──
    card_message_id: str = ""          # 当前流式卡片的 message_id
    body_buffer: str = ""              # 正文累积（当前卡片）
    thinking_buffer: str = ""          # 思考累积
    tool_calls: list = field(default_factory=list)  # [{name, input, status}]
    last_update_time: float = 0.0
    # 流式更新去重 / 降级
    last_sent_len: int = 0             # 上次 PATCH 时正文长度（最小增量门槛用）
    last_sent_card: str = ""           # 上次成功发送的卡片序列化（相同则跳过）
    card_degraded: bool = False        # PATCH 连续失败 → 停止流式，只发终态
    card_fail_count: int = 0           # 连续失败计数

    # ── 计时 / 成本 ──
    turn_start_time: float = 0.0       # 本轮开始时间戳
    last_cost_usd: float = 0.0         # 本轮成本（后端提供时）

    # ── 会话累计用量（/usage 命令展示）──
    total_cost_usd: float = 0.0        # 本会话累计成本
    total_turns: int = 0               # 本会话累计轮数
    total_tool_calls: int = 0          # 本会话累计工具调用次数
    last_user_msg_time: float = 0.0    # 上次真实用户消息时间（空闲轮换用）

    # ── 长输出分条 ──
    card_index: int = 0                # 当前是第几张卡片（0 起）

    # 审批：approval_id -> 后端特定的元信息
    pending_approvals: dict = field(default_factory=dict)
    # 本轮审批计数 + 「本轮全部允许」开关
    approval_count: int = 0
    auto_approve_turn: bool = False

    # 对话历史（持久化用）：[{role, text, tools, cost, time}]
    history: list = field(default_factory=list)

    def reset_turn(self):
        """开始新一轮时清空渲染缓冲"""
        self.body_buffer = ""
        self.thinking_buffer = ""
        self.tool_calls = []
        self.last_update_time = 0.0
        self.last_sent_len = 0
        self.last_sent_card = ""
        self.card_degraded = False
        self.card_fail_count = 0
        self.turn_start_time = 0.0
        self.last_cost_usd = 0.0
        self.card_index = 0
        self.approval_count = 0
        self.auto_approve_turn = False
        self.turn_active = True

    # ── 持久化 ──

    # 序列化时只存「重启后恢复需要」的字段，跳过纯运行时态（卡片缓冲等）
    _PERSIST_FIELDS = (
        "user_id", "chat_id", "session_id", "title", "native_id",
        "workdir_override", "model_override",
        "total_cost_usd", "total_turns", "total_tool_calls", "last_user_msg_time",
        "history",
    )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._PERSIST_FIELDS}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSession":
        known = {k for k in cls._PERSIST_FIELDS}
        return cls(**{k: v for k, v in d.items() if k in known})
