"""极简 in-process MCP server（Streamable HTTP）— 给 Claude Code 做审批中继

Claude Code 子进程通过 `--permission-prompt-tool mcp__feishu__approve_permission`
在需要审批时调用本 server 的工具。本 server 跑在 bridge 自己的事件循环里
（不是被 Claude 当子进程起的），因此工具 handler 能直接把审批请求 emit 到
归一化事件队列、await 用户在飞书点击的结果——与 opencode/codex 的 in-process
审批模型一致。

路由：每个 Claude 会话用一个稳定 token，URL 为 /mcp/{token}。Claude 每轮是
独立子进程、审批请求里没有会话标识，靠 URL 里的 token 定位是哪个会话。

只实现协议必需的最小子集：
  initialize / notifications/initialized / tools/list / tools/call
请求用 application/json 单条响应，通知回 202。
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "approve_permission"

# handler(token, tool_name, tool_input) -> {"behavior": "allow"/"deny", ...}
PermissionHandler = Callable[[str, str, dict], Awaitable[dict]]


class MCPApprovalServer:
    def __init__(self, handler: PermissionHandler, host: str = "127.0.0.1",
                 port: int = 0):
        self._handler = handler
        self.host = host
        self.port = port              # 0 = 随机端口，start() 后回填真实端口
        self._runner: Optional[web.AppRunner] = None

    @property
    def server_name(self) -> str:
        return "feishu"

    def url_for(self, token: str) -> str:
        return f"http://{self.host}:{self.port}/mcp/{token}"

    async def start(self):
        app = web.Application()
        app.router.add_post("/mcp/{token}", self._handle_post)
        # GET 用于 SSE 监听；我们不主动推 server→client，返回 405 即可
        app.router.add_get("/mcp/{token}", self._handle_get)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        # 回填实际监听端口（port=0 时由 OS 分配）
        for sock in self._runner.addresses:
            self.port = sock[1]
            break
        logger.info("MCP approval server at http://%s:%s/mcp/{token}", self.host, self.port)

    async def close(self):
        if self._runner:
            await self._runner.cleanup()

    async def _handle_get(self, request: web.Request) -> web.Response:
        # 不提供 server→client SSE 流
        return web.Response(status=405)

    async def _handle_post(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        try:
            msg = await request.json()
        except Exception:
            return web.json_response(
                self._err(None, -32700, "Parse error"), status=400)

        method = msg.get("method", "")
        msg_id = msg.get("id")

        # 通知（无 id）→ 202
        if msg_id is None:
            return web.Response(status=202)

        if method == "initialize":
            return web.json_response(self._result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.server_name, "version": "0.1.0"},
            }))

        if method == "tools/list":
            return web.json_response(self._result(msg_id, {
                "tools": [{
                    "name": TOOL_NAME,
                    "description": "审批一次工具调用（远程人工批准/拒绝）",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "input": {"type": "object"},
                        },
                    },
                }],
            }))

        if method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") != TOOL_NAME:
                return web.json_response(
                    self._err(msg_id, -32602, f"未知工具: {params.get('name')}"))
            args = params.get("arguments", {})
            tool_name = args.get("tool_name", "")
            tool_input = args.get("input", {}) or {}
            try:
                decision = await self._handler(token, tool_name, tool_input)
            except Exception as e:
                logger.error("Approval handler error: %s", e, exc_info=True)
                decision = {"behavior": "deny", "message": f"审批出错: {e}"}
            # 工具返回必须是文本块，内容为 JSON 字符串化的决定
            return web.json_response(self._result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}],
            }))

        return web.json_response(self._err(msg_id, -32601, f"方法未实现: {method}"))

    @staticmethod
    def _result(msg_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
