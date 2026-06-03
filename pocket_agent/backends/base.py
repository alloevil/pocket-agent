"""Agent 后端抽象基类

每个后端把自己的原生协议翻译成 AgentEvent，通过共享的 asyncio.Queue
推给 bridge。bridge 不关心后端是 WebSocket、子进程还是 HTTP server。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..events import AgentEvent, AgentSession

logger = logging.getLogger(__name__)


class AgentBackend(ABC):
    """所有 agent 后端的统一接口"""

    #: 后端名（"codex" | "claude" | "opencode"），由子类设置
    name: str = "agent"

    def __init__(self):
        # 后端把翻译好的 AgentEvent 投到这里；bridge 通过 events() 消费
        self._events: asyncio.Queue[AgentEvent] = asyncio.Queue()

    # ── 生命周期 ──

    @abstractmethod
    async def start(self) -> None:
        """启动/连接后端（连 WebSocket、起子进程、起 server 等）"""

    @abstractmethod
    async def close(self) -> None:
        """关闭后端，释放资源"""

    # ── 会话交互 ──

    @abstractmethod
    async def send(self, session: AgentSession, text: str) -> None:
        """在 session 上发起/追加一轮对话。

        首轮时后端负责创建原生会话并把 native_id 写回 session
        （可直接赋值，或通过 SESSION_ID 事件异步回填）。
        """

    @abstractmethod
    async def interrupt(self, session: AgentSession) -> None:
        """中断当前正在执行的 turn"""

    async def approve(self, session: AgentSession, approval_id: str,
                      approved: bool) -> None:
        """响应审批请求。默认不支持（子类按需覆盖）。"""
        logger.warning("%s backend does not support approvals", self.name)

    async def new_session(self, session: AgentSession) -> None:
        """开启全新会话（/new）。默认仅清空 native_id。"""
        session.native_id = ""
        session.current_turn_id = ""

    # ── 事件流 ──

    async def emit(self, event: AgentEvent) -> None:
        """后端内部调用：推送一个归一化事件"""
        await self._events.put(event)

    async def events(self) -> AsyncIterator[AgentEvent]:
        """bridge 调用：异步迭代后端产生的事件"""
        while True:
            yield await self._events.get()
