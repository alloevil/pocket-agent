"""CodexBackend 端到端 — mock WebSocket JSON-RPC server，验证包装后行为"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets

from pocket_agent.events import AgentSession, EventKind
from pocket_agent.backends.codex import CodexBackend


def test_codex_backend_turn_flow():
    async def run():
        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                mid, method = msg.get("id"), msg.get("method")
                if method == "initialize":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}))
                elif method == "thread/start":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid,
                                              "result": {"threadId": "th1"}}))
                elif method == "turn/start":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}))
                    # 推送通知序列：turn started, text delta, completed
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "turn/started",
                        "params": {"threadId": "th1", "turn": {"id": "tn1"}}}))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                        "params": {"threadId": "th1", "delta": "Hello "}}))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                        "params": {"threadId": "th1", "delta": "world"}}))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "turn/completed",
                        "params": {"threadId": "th1"}}))

        server = await websockets.serve(handler, "127.0.0.1", 5197)
        backend = CodexBackend("ws://127.0.0.1:5197")
        await backend.start()

        s = AgentSession(user_id="u", chat_id="c")
        await backend.send(s, "hi")
        assert s.native_id == "th1"

        # 收集事件
        collected = []
        try:
            while True:
                ev = await asyncio.wait_for(backend._events.get(), timeout=3)
                collected.append(ev)
                if ev.kind == EventKind.TURN_DONE:
                    break
        except asyncio.TimeoutError:
            raise AssertionError("codex turn timed out")

        kinds = [e.kind for e in collected]
        assert EventKind.TURN_STARTED in kinds
        assert EventKind.TURN_DONE in kinds
        text = "".join(e.text for e in collected if e.kind == EventKind.TEXT_DELTA)
        assert text == "Hello world", text

        await backend.close()
        server.close()
        await server.wait_closed()

    asyncio.run(run())


def test_codex_approval_translation():
    async def run():
        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                mid, method = msg.get("id"), msg.get("method")
                if method == "initialize":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}))
                elif method == "thread/start":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid,
                                              "result": {"threadId": "th1"}}))
                    # 服务端请求审批（带 id）
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 999,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "th1", "command": "rm -rf /", "cwd": "/tmp"}}))
                elif method == "turn/start":
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {}}))

        server = await websockets.serve(handler, "127.0.0.1", 5196)
        backend = CodexBackend("ws://127.0.0.1:5196")
        await backend.start()
        s = AgentSession(user_id="u", chat_id="c")
        await backend.send(s, "do something")

        ev = None
        for _ in range(10):
            e = await asyncio.wait_for(backend._events.get(), timeout=3)
            if e.kind == EventKind.APPROVAL_REQUEST:
                ev = e
                break
        assert ev is not None, "no approval request"
        assert ev.approval_kind == "command"
        assert "rm -rf" in ev.approval_text
        assert ev.approval_detail == "/tmp"
        assert ev.raw["request_msg_id"] == 999

        await backend.close()
        server.close()
        await server.wait_closed()

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
