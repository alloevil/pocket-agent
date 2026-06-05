"""Claude 审批流测试 — MCP server 单元 + 真实 claude 端到端

真实测试会让 claude 执行一个需要审批的 Bash 命令，断言：
  1. 我们的 MCP approve_permission 工具被调用（→ 触发 APPROVAL_REQUEST 事件）
  2. 我们回 allow 后命令真的执行、turn 正常完成
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.events import AgentSession, EventKind
from pocket_agent.backends.claude import ClaudeBackend
from pocket_agent.mcp_approval import MCPApprovalServer


def test_mcp_server_handshake_and_call():
    """MCP server 的 initialize/tools.list/tools.call 契约"""
    async def run():
        import aiohttp, json
        decided = {}

        async def handler(token, tool_name, tool_input):
            decided["token"] = token
            decided["tool"] = tool_name
            return {"behavior": "allow", "updatedInput": tool_input}

        srv = MCPApprovalServer(handler)
        await srv.start()
        try:
            async with aiohttp.ClientSession() as s:
                url = srv.url_for("abc")
                async with s.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": "initialize", "params": {}}) as r:
                    init = await r.json()
                    assert init["result"]["protocolVersion"]
                async with s.post(url, json={"jsonrpc": "2.0", "id": 2,
                                             "method": "tools/call",
                                             "params": {"name": "approve_permission",
                                                        "arguments": {"tool_name": "Bash",
                                                                      "input": {"command": "ls"}}}}) as r:
                    tc = await r.json()
                    payload = json.loads(tc["result"]["content"][0]["text"])
                    assert payload["behavior"] == "allow"
            assert decided["token"] == "abc"
            assert decided["tool"] == "Bash"
        finally:
            await srv.close()
    asyncio.run(run())


def test_approval_future_resolution():
    """_on_permission 应 emit APPROVAL_REQUEST 并阻塞，直到 approve() 解锁"""
    async def run():
        b = ClaudeBackend(approvals=False)  # 不真正起 server
        s = AgentSession(user_id="u", chat_id="c", native_id="sess-x", mcp_token="tok")
        b._token_sessions["tok"] = s

        # 并发：一边 _on_permission 等待，一边模拟用户审批
        task = asyncio.create_task(b._on_permission("tok", "Bash", {"command": "rm x"}))
        ev = await asyncio.wait_for(b._events.get(), 2)
        assert ev.kind == EventKind.APPROVAL_REQUEST
        assert ev.approval_kind == "command"
        assert "rm x" in ev.approval_text
        # bridge 会把 raw 存入 pending_approvals；这里手动模拟
        s.pending_approvals[ev.approval_id] = {"tool_input": ev.raw["tool_input"], **ev.raw}
        await b.approve(s, ev.approval_id, approved=True)
        decision = await asyncio.wait_for(task, 2)
        assert decision["behavior"] == "allow"
        assert decision["updatedInput"] == {"command": "rm x"}
    asyncio.run(run())


def test_approval_deny():
    async def run():
        b = ClaudeBackend(approvals=False)
        s = AgentSession(user_id="u", chat_id="c", native_id="sx", mcp_token="t2")
        b._token_sessions["t2"] = s
        task = asyncio.create_task(b._on_permission("t2", "Write", {"file_path": "/etc/x"}))
        ev = await asyncio.wait_for(b._events.get(), 2)
        s.pending_approvals[ev.approval_id] = {"tool_input": ev.raw["tool_input"]}
        await b.approve(s, ev.approval_id, approved=False)
        decision = await asyncio.wait_for(task, 2)
        assert decision["behavior"] == "deny"
        assert "拒绝" in decision["message"]
    asyncio.run(run())


def test_approval_kind_classification():
    """工具按类型正确分类：Bash→command、Read→read、Write/Edit→file。"""
    async def run():
        b = ClaudeBackend(approvals=False)
        cases = [("Bash", "command"), ("Read", "read"), ("Glob", "read"),
                 ("Write", "file"), ("Edit", "file")]
        for i, (tool, expect) in enumerate(cases):
            s = AgentSession(user_id="u", chat_id="c", native_id=f"s{i}",
                             mcp_token=f"tk{i}")
            b._token_sessions[f"tk{i}"] = s
            task = asyncio.create_task(b._on_permission(f"tk{i}", tool, {"file_path": "/x"}))
            ev = await asyncio.wait_for(b._events.get(), 2)
            assert ev.approval_kind == expect, f"{tool} 应为 {expect}，实际 {ev.approval_kind}"
            s.pending_approvals[ev.approval_id] = {"tool_input": ev.raw["tool_input"]}
            await b.approve(s, ev.approval_id, approved=True)
            await asyncio.wait_for(task, 2)
    asyncio.run(run())


def test_real_claude_approval():
    """真实 claude：触发一个 Bash 审批，自动批准，断言被调用 + 执行成功。"""
    if not shutil.which("claude"):
        print("  (skip: claude 未安装)")
        return

    async def run():
        b = ClaudeBackend(workdir="/tmp", approvals=True)
        await b.start()
        try:
            s = AgentSession(user_id="u", chat_id="c")

            # 后台任务：自动批准收到的审批请求
            approved_tools = []

            async def auto_approver():
                while True:
                    ev = await b._events.get()
                    if ev.kind == EventKind.APPROVAL_REQUEST:
                        approved_tools.append(ev.approval_detail)
                        s.pending_approvals[ev.approval_id] = {
                            "tool_input": ev.raw.get("tool_input", {})}
                        await b.approve(s, ev.approval_id, approved=True)
                    elif ev.kind in (EventKind.TURN_DONE, EventKind.ERROR):
                        return ev

            approver = asyncio.create_task(auto_approver())
            await b.send(s, "Create a file /tmp/feishu_approval_probe.txt containing the "
                            "text APPROVED_OK using the Write tool.")
            final = await asyncio.wait_for(approver, timeout=120)

            assert final.kind == EventKind.TURN_DONE, f"ended with {final.kind}: {final.error}"
            assert approved_tools, "审批工具从未被调用 —— MCP 中继没生效"
            print(f"  ✅ 审批被调用 {len(approved_tools)} 次，工具={approved_tools}")
            # 确认 allow 后操作真的执行
            probe = Path("/tmp/feishu_approval_probe.txt")
            assert probe.exists(), "批准后文件未创建 —— allow 决定没生效"
            probe.unlink()
        finally:
            await b.close()

    asyncio.run(run())


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"✅ {fn.__name__}")
        except Exception:
            failed += 1
            print(f"❌ {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
