"""Codex app-server WebSocket 客户端

协议基于 Codex app-server JSON-RPC v2。
支持：线程管理、turn 控制、审批响应、流式输出。
"""

import asyncio
import json
import logging
from typing import Any, Optional

import websockets

logger = logging.getLogger(__name__)


class CodexClient:
    """Codex app-server WebSocket JSON-RPC 客户端"""

    def __init__(self, url: str, auth_token: str = ""):
        self.url = url
        self.auth_token = auth_token
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._req_id = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._notifications: asyncio.Queue = asyncio.Queue()
        self._connected = asyncio.Event()
        self._receive_task: Optional[asyncio.Task] = None
        self._closing = False

    # ── 连接管理 ──

    async def _open(self):
        """打开 WebSocket 并完成 initialize 握手

        握手必须内联完成：此时 receive_loop 要么尚未启动（首次连接），
        要么正阻塞在 _reconnect 上（重连），都无法替我们消费 initialize 的响应。
        """
        headers = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        self.ws = await websockets.connect(
            self.url,
            additional_headers=headers,
            max_size=50 * 1024 * 1024,
        )

        # 内联 initialize 握手
        req_id = self._next_id()
        await self.ws.send(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {"clientInfo": {"name": "pocket-agent", "version": "0.1.0"}},
        }))
        resp = await self._read_until_response(req_id)
        logger.info("Codex connected: %s", json.dumps(resp, ensure_ascii=False)[:200])

        self._connected.set()

    async def _read_until_response(self, req_id: str, timeout: float = 30) -> dict:
        """读取消息直到拿到匹配 req_id 的响应；其间的通知投递到队列"""
        deadline = timeout
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=deadline)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(msg.get("id", "")) == req_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise Exception(f"Codex error: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                await self._notifications.put(msg)

    async def connect(self):
        self._closing = False
        await self._open()
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _reconnect(self):
        """断线后以指数退避重连，恢复后通知循环继续工作"""
        backoff = 1.0
        while not self._closing:
            await asyncio.sleep(backoff)
            try:
                logger.info("Reconnecting to Codex (backoff=%.1fs) ...", backoff)
                await self._open()
                logger.info("Codex reconnected")
                return
            except Exception as e:
                logger.warning("Reconnect failed: %s", e)
                backoff = min(backoff * 2, 30.0)

    async def close(self):
        self._closing = True
        if self._receive_task:
            self._receive_task.cancel()
        if self.ws:
            await self.ws.close()
        self._connected.clear()
        self._fail_pending(Exception("connection closed"))

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _fail_pending(self, exc: Exception):
        """让所有等待中的请求立即失败，避免悬挂到超时"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ── JSON-RPC 基础 ──

    def _next_id(self) -> str:
        self._req_id += 1
        return str(self._req_id)

    async def _send_request(self, method: str, params: dict, timeout: float = 300) -> dict:
        req_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _send_notification(self, method: str, params: dict):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        await self.ws.send(json.dumps(msg))

    async def send_response(self, req_id: Any, result: dict):
        """回复服务端请求（如审批）"""
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        await self.ws.send(json.dumps(msg))

    async def _receive_loop(self):
        while not self._closing:
            try:
                async for raw in self.ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if "id" in msg and ("result" in msg or "error" in msg):
                        req_id = str(msg["id"])
                        fut = self._pending.get(req_id)
                        if fut and not fut.done():
                            if "error" in msg:
                                fut.set_exception(
                                    Exception(f"Codex error: {msg['error']}")
                                )
                            else:
                                fut.set_result(msg.get("result", {}))

                    elif "method" in msg:
                        await self._notifications.put(msg)

            except websockets.ConnectionClosed:
                logger.warning("Codex WebSocket disconnected")
            except Exception as e:
                logger.error("Codex receive loop error: %s", e, exc_info=True)

            if self._closing:
                break

            # 连接断了：让等待中的请求失败，然后重连
            self._connected.clear()
            self._fail_pending(Exception("Codex connection lost"))
            await self._reconnect()

    async def get_notification(self) -> dict:
        return await self._notifications.get()

    # ── 线程管理 ──

    async def start_thread(self, prompt: str) -> str:
        """创建新线程并发送初始 prompt，返回 threadId"""
        result = await self._send_request("thread/start", {})
        thread_id = (
            result.get("threadId")
            or result.get("id")
            or result.get("thread", {}).get("id", "")
        )
        logger.info("Thread started: %s", thread_id)

        await self._send_request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        })
        logger.info("Turn started in thread %s", thread_id)
        return thread_id

    async def send_turn(self, thread_id: str, message: str) -> dict:
        """在已有线程中发送新 turn"""
        return await self._send_request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": message}],
        })

    async def steer_turn(self, thread_id: str, turn_id: str, message: str) -> dict:
        """转向/追加指令"""
        return await self._send_request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": message}],
        })

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> dict:
        """中断当前 turn"""
        return await self._send_request("turn/interrupt", {
            "threadId": thread_id,
            "turnId": turn_id,
        })

    async def archive_thread(self, thread_id: str) -> dict:
        """归档线程"""
        return await self._send_request("thread/archive", {"threadId": thread_id})

    async def list_threads(self) -> dict:
        return await self._send_request("thread/list", {})

    async def read_thread(self, thread_id: str) -> dict:
        return await self._send_request("thread/read", {"threadId": thread_id})

    # ── 审批 ──

    async def approve_command(self, req_id: Any, approved: bool):
        """审批命令执行"""
        decision = "approved" if approved else "denied"
        await self.send_response(req_id, {"decision": decision})

    async def approve_file_change(self, req_id: Any, approved: bool):
        """审批文件修改"""
        decision = "accept" if approved else "decline"
        await self.send_response(req_id, {"decision": decision})
