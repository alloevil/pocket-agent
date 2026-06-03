"""配置管理"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # ── Agent 选择 ──
    # 默认 agent 后端："codex" | "claude" | "opencode"
    agent: str = "codex"
    # agent 工作目录（claude/opencode 子进程在此目录运行）
    workdir: str = "."

    # ── Codex ──
    codex_ws_url: str = "ws://127.0.0.1:5123"
    codex_auth_token: str = ""

    # ── Claude Code ──
    claude_model: str = ""              # 空 = claude 默认模型
    claude_extra_args: list = field(default_factory=list)  # 透传给 claude 的额外 flag

    # ── opencode ──
    opencode_model: str = ""            # provider/model，空 = opencode 默认
    opencode_port: int = 0              # 0 = 随机端口

    # ── 飞书 ──
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # ── 桥接 ──
    # 用户白名单（逗号分隔 open_id），为空允许所有
    allowed_users: str = ""
    # 消息更新节流间隔（秒）
    throttle_seconds: float = 3.0
    # 流式更新最小增量字符：自上次发送以来新增 < 此值则不触发 PATCH
    min_delta_chars: int = 30
    # 空闲会话自动轮换：超过此分钟数无用户消息则下次开新会话防上下文漂移；0=关闭
    idle_minutes: int = 0
    # 会话持久化文件路径（空=不持久化）
    persist_path: str = ""
    # 运行中心跳刷新间隔（秒），静默期刷新「已运行 Xs」；0=关闭
    heartbeat_seconds: int = 15
    # 长任务（耗时超此秒数）完成时主动提醒；0=关闭
    notify_done_seconds: int = 0
    # 最大消息长度
    max_message_length: int = 4000
    # 是否展示思考过程（折叠面板）
    show_thinking: bool = True
    # 日志级别
    log_level: str = "info"

    @classmethod
    def from_file(cls, path: str | Path) -> "Config":
        with open(path) as f:
            data = json.load(f)
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def allowed_user_set(self) -> set[str]:
        if not self.allowed_users:
            return set()
        return {u.strip() for u in self.allowed_users.split(",") if u.strip()}

    def is_allowed(self, user_id: str) -> bool:
        allowed = self.allowed_user_set
        if not allowed:
            return True
        return user_id in allowed

    def validate(self) -> list[str]:
        """启动前自检：返回问题列表（空=通过）。快速失败，避免跑到一半才报错。"""
        import shutil

        problems = []

        # 飞书凭证
        if not self.feishu_app_id or self.feishu_app_id.startswith("cli_axxx"):
            problems.append("feishu_app_id 未填写（飞书开放平台应用 App ID）")
        if not self.feishu_app_secret or "xxxx" in self.feishu_app_secret:
            problems.append("feishu_app_secret 未填写")

        # agent 选择
        if self.agent not in ("codex", "claude", "opencode"):
            problems.append(
                f"agent={self.agent!r} 无效，应为 codex / claude / opencode")

        # 各后端的前置条件
        if self.agent == "claude":
            if not shutil.which("claude"):
                problems.append("未找到 `claude` 命令——请先安装 Claude Code CLI")
        elif self.agent == "opencode":
            if not shutil.which("opencode"):
                problems.append("未找到 `opencode` 命令——请先安装 opencode CLI")
            if not self.opencode_model:
                problems.append(
                    "opencode_model 未设置（形如 anthropic/claude-sonnet-4）；"
                    "且需确保 `opencode auth login` 已配置有效凭证，否则对话会返回 401")
        elif self.agent == "codex":
            if not self.codex_ws_url:
                problems.append("codex_ws_url 未设置")

        return problems

