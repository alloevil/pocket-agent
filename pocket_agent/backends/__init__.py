"""Agent 后端工厂"""

from .base import AgentBackend


def create_backend(name: str, config) -> AgentBackend:
    """按名字创建后端实例。

    name: "codex" | "claude" | "opencode"
    config: Config 对象
    """
    name = (name or "codex").lower()

    if name == "codex":
        from .codex import CodexBackend
        return CodexBackend(config.codex_ws_url, config.codex_auth_token)
    elif name == "claude":
        from .claude import ClaudeBackend
        return ClaudeBackend(
            workdir=config.workdir,
            model=config.claude_model,
            extra_args=config.claude_extra_args,
        )
    elif name == "opencode":
        from .opencode import OpenCodeBackend
        return OpenCodeBackend(
            workdir=config.workdir,
            model=config.opencode_model,
            port=config.opencode_port,
        )
    raise ValueError(f"未知 agent 后端: {name!r}（可选: codex / claude / opencode）")


SUPPORTED_BACKENDS = ("codex", "claude", "opencode")
