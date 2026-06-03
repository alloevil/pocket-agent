"""opencode 后端 — `opencode serve` + REST/SSE

启动一个 opencode HTTP server 子进程，然后：
  POST /session                      → 建会话
  POST /session/{id}/message         → 发一轮（parts: [{type:text,text}]）
  GET  /event (SSE)                  → 流式接收 message.part.updated 等事件
  POST /session/{id}/abort           → 中断
  POST /session/{id}/permissions/{permissionID}  → 审批回复（once/reject）

part.updated 里 part.type ∈ text|reasoning|tool，tool.state.status 驱动
tool_start/tool_end。session.idle → turn_done。
"""

import asyncio
import json
import logging

import aiohttp

from ..events import AgentEvent, AgentSession, EventKind
from .base import AgentBackend

logger = logging.getLogger(__name__)


class OpenCodeBackend(AgentBackend):
    name = "opencode"

    def __init__(self, workdir: str = ".", model: str = "", port: int = 0,
                 command: str = "opencode"):
        super().__init__()
        self.workdir = workdir
        self.model = model            # "provider/model"
        self.port = port or 4096
        self.command = command
        self._proc = None
        self._session: aiohttp.ClientSession | None = None
        self._sse_task = None
        self._watchdog_task = None
        self._closing = False
        # opencode 的 text part 是「全量快照」，记录上次长度做增量
        self._text_seen: dict[str, int] = {}      # partID -> emitted len
        self._reasoning_seen: dict[str, int] = {}
        self._tool_started: set[str] = set()       # callID 已发过 tool_start

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._closing = False
        self._session = aiohttp.ClientSession()
        await self._spawn_serve()
        self._sse_task = asyncio.create_task(self._sse_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.info("opencode server ready at %s", self.base)

    async def _spawn_serve(self):
        self._proc = await asyncio.create_subprocess_exec(
            self.command, "serve", "--port", str(self.port),
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await self._wait_ready()

    async def _watchdog(self):
        """监控 serve 进程；意外退出时重启，避免 SSE 空转、对话静默失败。"""
        backoff = 1.0
        while not self._closing:
            if self._proc is None:
                await asyncio.sleep(1)
                continue
            await self._proc.wait()
            if self._closing:
                return
            logger.warning("opencode serve 退出（code=%s），%.0fs 后重启",
                           self._proc.returncode, backoff)
            # 通知所有人：服务中断
            await self.emit(AgentEvent(
                EventKind.ERROR, route_id="",
                error=f"opencode 服务意外退出（code={self._proc.returncode}），正在重启…"))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            try:
                await self._spawn_serve()
                logger.info("opencode serve 已重启")
                backoff = 1.0
            except Exception as e:
                logger.error("opencode 重启失败: %s", e)

    async def _wait_ready(self, timeout: float = 30):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with self._session.get(f"{self.base}/global/health") as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError("opencode server 启动超时")

    async def close(self) -> None:
        self._closing = True
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._sse_task:
            self._sse_task.cancel()
        if self._session:
            await self._session.close()
        if self._proc:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass

    # ── 交互 ──

    async def send(self, session: AgentSession, text: str) -> None:
        if not session.native_id:
            async with self._session.post(f"{self.base}/session", json={}) as r:
                data = await r.json()
                session.native_id = data.get("id", "")
                logger.info("opencode session: %s", session.native_id)

        body = {"parts": [{"type": "text", "text": text}]}
        # 会话级模型覆盖优先；形如 "provider/model"
        model = session.model_override or self.model
        if model and "/" in model:
            provider, model_id = model.split("/", 1)
            body["model"] = {"providerID": provider, "modelID": model_id}

        # 用 prompt_async 异步发起，结果走 SSE
        async with self._session.post(
            f"{self.base}/session/{session.native_id}/prompt_async", json=body
        ) as r:
            if r.status >= 400:
                err = await r.text()
                await self.emit(AgentEvent(
                    EventKind.ERROR, route_id=session.native_id,
                    error=f"opencode HTTP {r.status}: {err[:200]}"))

    async def interrupt(self, session: AgentSession) -> None:
        if session.native_id:
            async with self._session.post(
                f"{self.base}/session/{session.native_id}/abort"
            ) as r:
                await r.read()

    async def approve(self, session: AgentSession, approval_id: str,
                      approved: bool) -> None:
        info = session.pending_approvals.pop(approval_id, None)
        if not info:
            return
        perm_id = info.get("permission_id", "")
        sid = info.get("session_id", session.native_id)
        response = "once" if approved else "reject"
        async with self._session.post(
            f"{self.base}/session/{sid}/permissions/{perm_id}",
            json={"response": response},
        ) as r:
            await r.read()

    # ── SSE 事件循环 ──

    async def _sse_loop(self):
        while True:
            try:
                async with self._session.get(f"{self.base}/event") as resp:
                    async for raw in resp.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if not payload:
                            continue
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        await self._translate(evt)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("opencode SSE error: %s; reconnecting", e)
                await asyncio.sleep(1)

    async def _translate(self, evt: dict):
        etype = evt.get("type", "")
        props = evt.get("properties", {})

        if etype == "message.part.updated":
            await self._handle_part(props.get("part", {}))

        elif etype == "session.idle":
            sid = props.get("sessionID", "")
            await self.emit(AgentEvent(EventKind.TURN_DONE, route_id=sid))

        elif etype == "session.error":
            err = props.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            await self.emit(AgentEvent(
                EventKind.ERROR, route_id=props.get("sessionID", ""),
                error=msg or "opencode error"))

        elif etype == "permission.updated":
            # permission.updated 的 properties 就是 Permission 本身
            sid = props.get("sessionID", "")
            perm_id = props.get("id", "")
            title = props.get("title") or props.get("type") or "请求授权"
            await self.emit(AgentEvent(
                EventKind.APPROVAL_REQUEST, route_id=sid,
                approval_id=f"perm:{perm_id}", approval_kind="command",
                approval_text=str(title),
                raw={"permission_id": perm_id, "session_id": sid}))

    async def _handle_part(self, part: dict):
        ptype = part.get("type")
        sid = part.get("sessionID", "")
        pid = part.get("id", "")

        if ptype == "text":
            full = part.get("text", "")
            seen = self._text_seen.get(pid, 0)
            if len(full) > seen:
                await self.emit(AgentEvent(
                    EventKind.TEXT_DELTA, route_id=sid, text=full[seen:]))
                self._text_seen[pid] = len(full)

        elif ptype == "reasoning":
            full = part.get("text", "")
            seen = self._reasoning_seen.get(pid, 0)
            if len(full) > seen:
                await self.emit(AgentEvent(
                    EventKind.THINKING_DELTA, route_id=sid, text=full[seen:]))
                self._reasoning_seen[pid] = len(full)

        elif ptype == "tool":
            call_id = part.get("callID", "") or pid
            tool_name = part.get("tool", "tool")
            state = part.get("state", {})
            status = state.get("status", "")

            if status in ("pending", "running"):
                if call_id not in self._tool_started:
                    self._tool_started.add(call_id)
                    await self.emit(AgentEvent(
                        EventKind.TOOL_START, route_id=sid,
                        tool_call_id=call_id, tool_name=tool_name,
                        tool_input=state.get("input") or {}))
            elif status == "completed":
                await self.emit(AgentEvent(
                    EventKind.TOOL_END, route_id=sid,
                    tool_call_id=call_id, tool_name=tool_name,
                    tool_input=state.get("input") or {},
                    tool_status="completed",
                    tool_output=str(state.get("title") or state.get("output") or "")[:500]))
            elif status == "error":
                await self.emit(AgentEvent(
                    EventKind.TOOL_END, route_id=sid,
                    tool_call_id=call_id, tool_name=tool_name,
                    tool_input=state.get("input") or {},
                    tool_status="error",
                    tool_output=str(state.get("error") or "")[:500]))
