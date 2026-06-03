"""Codex 后端 — 包装 CodexClient（WebSocket JSON-RPC）

把 Codex app-server 的原生 notification 翻译成归一化 AgentEvent。
原来散落在 bridge._handle_codex_notifications 里的逻辑搬到这里，行为等价。
"""

import asyncio
import logging

from ..codex_client import CodexClient
from ..events import AgentEvent, AgentSession, EventKind
from .base import AgentBackend

logger = logging.getLogger(__name__)


class CodexBackend(AgentBackend):
    name = "codex"

    def __init__(self, ws_url: str, auth_token: str = ""):
        super().__init__()
        self.client = CodexClient(ws_url, auth_token)
        self._notif_task = None
        # 记录「刚发起但还没拿到 threadId」的 session，用于回填 native_id
        self._pending_start: AgentSession | None = None

    async def start(self) -> None:
        await self.client.connect()
        self._notif_task = asyncio.create_task(self._pump_notifications())

    async def close(self) -> None:
        if self._notif_task:
            self._notif_task.cancel()
        await self.client.close()

    async def send(self, session: AgentSession, text: str) -> None:
        if session.native_id and session.turn_active and session.current_turn_id:
            # 当前轮进行中 → steer 追加
            await self.client.steer_turn(session.native_id, session.current_turn_id, text)
        elif session.native_id:
            await self.client.send_turn(session.native_id, text)
        else:
            # 首轮：建线程。threadId 既由 start_thread 返回，也会通过
            # thread/started 通知到达；这里直接用返回值回填。
            self._pending_start = session
            thread_id = await self.client.start_thread(text)
            session.native_id = thread_id
            self._pending_start = None

    async def interrupt(self, session: AgentSession) -> None:
        if session.native_id and session.current_turn_id:
            await self.client.interrupt_turn(session.native_id, session.current_turn_id)

    async def approve(self, session: AgentSession, approval_id: str,
                      approved: bool) -> None:
        info = session.pending_approvals.pop(approval_id, None)
        if not info:
            return
        req_msg_id = info["request_msg_id"]
        if info["kind"] == "command":
            await self.client.approve_command(req_msg_id, approved)
        else:
            await self.client.approve_file_change(req_msg_id, approved)

    async def new_session(self, session: AgentSession) -> None:
        if session.native_id:
            try:
                await self.client.archive_thread(session.native_id)
            except Exception:
                pass
        session.native_id = ""
        session.current_turn_id = ""

    # ── 通知翻译 ──

    async def _pump_notifications(self):
        while True:
            try:
                msg = await self.client.get_notification()
                await self._translate(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Codex notification error: %s", e, exc_info=True)
                await asyncio.sleep(1)

    async def _translate(self, msg: dict):
        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")  # 服务端请求才有 id
        tid = params.get("threadId", "")

        if method == "thread/started":
            # threadId 已由 send() 的返回值回填；这里仅记录
            thread_obj = params.get("thread", {})
            logger.info("Codex thread started: %s", thread_obj.get("id", ""))

        elif method == "turn/started":
            turn = params.get("turn", {})
            await self.emit(AgentEvent(
                EventKind.TURN_STARTED, route_id=tid,
                tool_call_id=turn.get("id", ""),
            ))

        elif method == "item/agentMessage/delta":
            await self.emit(AgentEvent(
                EventKind.TEXT_DELTA, route_id=tid,
                text=params.get("delta", ""),
            ))

        elif method == "item/reasoning/delta":
            await self.emit(AgentEvent(
                EventKind.THINKING_DELTA, route_id=tid,
                text=params.get("delta", ""),
            ))

        elif method == "turn/completed":
            await self.emit(AgentEvent(EventKind.TURN_DONE, route_id=tid))

        elif method == "error":
            await self.emit(AgentEvent(
                EventKind.ERROR, route_id=tid,
                error=params.get("message", "未知错误"),
            ))

        elif method == "item/commandExecution/requestApproval":
            command = params.get("command") or ""
            cwd = params.get("cwd") or ""
            actions = params.get("commandActions") or []
            if actions and not command:
                command = " ".join(
                    a.get("command", "") for a in actions if isinstance(a, dict)
                )
            await self.emit(AgentEvent(
                EventKind.APPROVAL_REQUEST, route_id=tid,
                approval_id=f"cmd:{msg_id}", approval_kind="command",
                approval_text=command, approval_detail=cwd,
                raw={"request_msg_id": msg_id},
            ))

        elif method == "item/fileChange/requestApproval":
            await self.emit(AgentEvent(
                EventKind.APPROVAL_REQUEST, route_id=tid,
                approval_id=f"file:{msg_id}", approval_kind="file",
                approval_text=params.get("reason") or "请求修改文件",
                raw={"request_msg_id": msg_id},
            ))

        elif method == "item/tool/requestUserInput":
            await self.emit(AgentEvent(
                EventKind.TEXT_DELTA, route_id=tid,
                text=f"\n\n💬 需要输入：{params.get('prompt', '')}\n直接回复即可。",
            ))
