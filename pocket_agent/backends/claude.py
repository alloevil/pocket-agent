"""Claude Code 后端 — 驱动 `claude -p` 子进程，解析 stream-json

每一轮起一个 `claude -p` 进程：
  claude -p <prompt> --output-format stream-json --verbose
         --include-partial-messages [--resume <session_id>] [--model ...]

stdout 是 NDJSON，逐行解析翻译成 AgentEvent：
  stream_event/content_block_delta → text_delta / thinking_delta（逐 token）
  assistant.content[].tool_use      → tool_start
  system/init & result.session_id   → session_id（用于下一轮 --resume）
  result                            → turn_done

会话续接靠 --resume <session_id>，session_id 从首轮 init/result 事件获取。
"""

import asyncio
import itertools
import json
import logging
import shlex

from ..events import AgentEvent, AgentSession, EventKind
from ..mcp_approval import MCPApprovalServer
from .base import AgentBackend

logger = logging.getLogger(__name__)


class ClaudeBackend(AgentBackend):
    name = "claude"

    def __init__(self, workdir: str = ".", model: str = "",
                 extra_args: list | None = None, command: str = "claude",
                 approvals: bool = True):
        super().__init__()
        self.workdir = workdir
        self.model = model
        self.extra_args = list(extra_args or [])
        self.command = command
        self.approvals = approvals
        # native_id(session_id) -> 当前运行的进程，用于中断
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # 把「本轮 session」与正在累积的 tool_use 关联
        self._counter = 0
        # tool_use_id -> 该工具写出的图片路径（待工具成功后回传）
        self._pending_files: dict[str, str] = {}
        # 审批：in-process MCP server + token→session + 待决 future
        self._mcp: MCPApprovalServer | None = None
        self._token_sessions: dict[str, AgentSession] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}
        self._approval_seq = itertools.count(1)

    async def start(self) -> None:
        if self.approvals:
            self._mcp = MCPApprovalServer(self._on_permission)
            await self._mcp.start()
        logger.info("ClaudeBackend ready (command=%s, workdir=%s, approvals=%s)",
                    self.command, self.workdir, self.approvals)

    async def close(self) -> None:
        for proc in list(self._procs.values()):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if self._mcp:
            await self._mcp.close()

    async def send(self, session: AgentSession, text: str) -> None:
        # 首轮没有 native_id；给 session 一个临时路由 id，便于事件回流
        if not session.native_id:
            self._counter += 1
            session.native_id = f"claude-pending-{self._counter}"
        # 分配稳定的审批 token 并注册（同一 session 复用）
        if self.approvals and not session.mcp_token:
            session.mcp_token = f"t{self._counter}-{id(session) & 0xffffff:06x}"
            self._token_sessions[session.mcp_token] = session
        asyncio.create_task(self._run_turn(session, text))

    async def interrupt(self, session: AgentSession) -> None:
        proc = self._procs.get(session.native_id)
        if proc:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def approve(self, session: AgentSession, approval_id: str,
                      approved: bool) -> None:
        info = session.pending_approvals.pop(approval_id, None)
        fut = self._approval_futures.pop(approval_id, None)
        if fut and not fut.done():
            tool_input = (info or {}).get("tool_input", {})
            if approved:
                fut.set_result({"behavior": "allow", "updatedInput": tool_input})
            else:
                fut.set_result({"behavior": "deny", "message": "用户在飞书拒绝了此操作"})

    async def new_session(self, session: AgentSession) -> None:
        # Claude 无需归档；丢弃 session_id 即开新会话
        self._token_sessions.pop(session.mcp_token, None)
        session.native_id = ""
        session.current_turn_id = ""
        session.mcp_token = ""

    # ── 审批回调（由 MCP server 在工具被调用时触发）──

    async def _on_permission(self, token: str, tool_name: str,
                             tool_input: dict) -> dict:
        """Claude 子进程请求审批 → emit 事件 → await 用户点击 → 返回决定"""
        session = self._token_sessions.get(token)
        if session is None:
            return {"behavior": "deny", "message": "会话已失效"}

        approval_id = f"claude:{next(self._approval_seq)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._approval_futures[approval_id] = fut

        # 按工具类型分类，决定审批卡片的标题/措辞
        if tool_name in ("Bash", "BashOutput", "KillShell"):
            kind = "command"
        elif tool_name in ("Read", "Glob", "Grep", "NotebookRead", "LS"):
            kind = "read"       # 只读类：如实显示「读取」，不误称「修改」
        else:
            kind = "file"       # Write/Edit/NotebookEdit 等写类
        summary = ""
        for k in ("command", "file_path", "path", "url", "pattern", "description"):
            if tool_input.get(k):
                summary = str(tool_input[k])
                break
        await self.emit(AgentEvent(
            EventKind.APPROVAL_REQUEST, route_id=session.native_id,
            approval_id=approval_id,
            approval_kind=kind,
            approval_text=summary or tool_name,
            approval_detail=tool_name,
            raw={"tool_input": tool_input},
        ))

        try:
            return await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            self._approval_futures.pop(approval_id, None)
            return {"behavior": "deny", "message": "审批超时（10 分钟未响应）"}

    # ── 运行一轮 ──

    def _build_args(self, session: AgentSession, prompt: str) -> list[str]:
        args = [
            self.command, "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        # 已有真实 session_id（非 pending 占位）→ 续接
        if session.native_id and not session.native_id.startswith("claude-pending-"):
            args += ["--resume", session.native_id]
        # 会话级模型覆盖优先于全局
        model = session.model_override or self.model
        if model:
            args += ["--model", model]
        # 审批中继：把权限提示路由到 in-process MCP server 的 approve_permission 工具
        if self.approvals and self._mcp and session.mcp_token:
            url = self._mcp.url_for(session.mcp_token)
            mcp_config = {"mcpServers": {self._mcp.server_name: {"type": "http", "url": url}}}
            tool = f"mcp__{self._mcp.server_name}__approve_permission"
            args += [
                "--permission-mode", "default",
                "--mcp-config", json.dumps(mcp_config),
                "--permission-prompt-tool", tool,
            ]
        args += self.extra_args
        return args

    async def _run_turn(self, session: AgentSession, prompt: str):
        args = self._build_args(session, prompt)
        route = session.native_id
        logger.info("claude turn: %s", " ".join(shlex.quote(a) for a in args[:6]) + " …")

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=session.workdir_override or self.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            await self.emit(AgentEvent(
                EventKind.ERROR, route_id=route,
                error=f"找不到 `{self.command}` 命令，请确认 Claude Code 已安装",
            ))
            return

        self._procs[route] = proc
        try:
            emitted_done = await self._read_stream(session, proc, route)
            stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
            rc = await proc.wait()
            route = session.native_id or route
            if not emitted_done:
                # 没收到 result：按退出码区分崩溃 vs 正常无结果
                if rc != 0:
                    await self.emit(AgentEvent(
                        EventKind.ERROR, route_id=route,
                        error=f"claude 异常退出（code={rc}）：{stderr[:300] or '无 stderr'}",
                    ))
                else:
                    await self.emit(AgentEvent(EventKind.TURN_DONE, route_id=route))
        except Exception as e:
            await self.emit(AgentEvent(
                EventKind.ERROR, route_id=session.native_id or route,
                error=f"claude 进程读取出错: {e}"))
        finally:
            self._procs.pop(route, None)

    async def _read_stream(self, session: AgentSession,
                           proc: asyncio.subprocess.Process, route: str):
        # 累积每个 tool_use block 的流式参数 JSON
        tool_buffers: dict[int, dict] = {}  # content index -> {id, name, json}
        emitted_done = False

        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            if mtype == "system" and msg.get("subtype") == "init":
                sid = msg.get("session_id", "")
                if sid:
                    session.native_id = sid
                    route = sid
                    await self.emit(AgentEvent(
                        EventKind.SESSION_ID, route_id=sid, session_id=sid))
                    await self.emit(AgentEvent(EventKind.TURN_STARTED, route_id=sid))

            elif mtype == "stream_event":
                await self._handle_stream_event(route, msg.get("event", {}), tool_buffers)

            elif mtype == "assistant":
                # 完整 assistant 消息：补齐 tool_use（防止 delta 丢失）
                for block in msg.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        await self.emit(AgentEvent(
                            EventKind.TOOL_START, route_id=route,
                            tool_call_id=block.get("id", ""),
                            tool_name=block.get("name", ""),
                            tool_input=block.get("input") or {},
                        ))
                        # 探测：Write 生成了图片文件 → 记下，待工具完成后回传
                        self._note_file_output(block)

            elif mtype == "user":
                # tool_result 回流：标记对应工具完成
                for block in msg.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        out = block.get("content", "")
                        if isinstance(out, list):
                            out = " ".join(
                                b.get("text", "") for b in out if isinstance(b, dict))
                        tool_id = block.get("tool_use_id", "")
                        is_err = block.get("is_error")
                        await self.emit(AgentEvent(
                            EventKind.TOOL_END, route_id=route,
                            tool_call_id=tool_id,
                            tool_status="error" if is_err else "completed",
                            tool_output=str(out)[:500],
                        ))
                        # 工具成功完成且之前记到了图片产物 → 回传
                        if not is_err:
                            path = self._pending_files.pop(tool_id, None)
                            if path:
                                await self.emit(AgentEvent(
                                    EventKind.FILE_OUTPUT, route_id=route,
                                    file_path=path, is_image=True))

            elif mtype == "result":
                sid = msg.get("session_id", "")
                if sid:
                    session.native_id = sid
                await self.emit(AgentEvent(
                    EventKind.TURN_DONE, route_id=session.native_id or route,
                    cost_usd=msg.get("total_cost_usd", 0.0),
                    error=("; ".join(msg.get("errors", [])) if msg.get("is_error") else ""),
                ))
                emitted_done = True

        # 注意：没有 result 时不在这里兜底 TURN_DONE——交给 _run_turn 按退出码
        # 区分「正常结束」与「崩溃」，避免把崩溃渲染成 ✅完成
        return emitted_done

    async def _handle_stream_event(self, route: str, event: dict,
                                   tool_buffers: dict):
        etype = event.get("type")

        if etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_buffers[event.get("index", -1)] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "json": "",
                }

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                await self.emit(AgentEvent(
                    EventKind.TEXT_DELTA, route_id=route, text=delta.get("text", "")))
            elif dtype == "thinking_delta":
                await self.emit(AgentEvent(
                    EventKind.THINKING_DELTA, route_id=route,
                    text=delta.get("thinking", "")))
            elif dtype == "input_json_delta":
                buf = tool_buffers.get(event.get("index", -1))
                if buf is not None:
                    buf["json"] += delta.get("partial_json", "")

        elif etype == "content_block_stop":
            buf = tool_buffers.pop(event.get("index", -1), None)
            if buf:
                try:
                    tool_input = json.loads(buf["json"]) if buf["json"] else {}
                except json.JSONDecodeError:
                    tool_input = {"_raw": buf["json"]}
                await self.emit(AgentEvent(
                    EventKind.TOOL_START, route_id=route,
                    tool_call_id=buf["id"], tool_name=buf["name"],
                    tool_input=tool_input,
                ))
                self._note_file_output({"id": buf["id"], "name": buf["name"],
                                        "input": tool_input})

    # 图片扩展名（Write 出这些后缀时，工具完成后回传给用户）
    _IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")

    def _note_file_output(self, block: dict):
        """记录 Write 工具写出的图片路径，待工具成功完成后回传给用户。

        保守策略：只认 Write 工具 + 图片扩展名，避免误传一堆代码文件。
        路径相对时按 workdir 解析。
        """
        import os
        if block.get("name") != "Write":
            return
        fp = (block.get("input") or {}).get("file_path", "")
        if not fp or not fp.lower().endswith(self._IMG_EXTS):
            return
        if not os.path.isabs(fp):
            fp = os.path.join(self.workdir, fp)
        self._pending_files[block.get("id", "")] = fp
