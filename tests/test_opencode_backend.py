"""OpenCodeBackend 测试 — part 解析（离线）+ 真实 serve 端到端（若安装）"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.events import AgentSession, EventKind
from pocket_agent.backends.opencode import OpenCodeBackend


def test_text_part_incremental():
    """text part 是全量快照，应转成增量 TEXT_DELTA"""
    async def run():
        b = OpenCodeBackend()
        # 第一次：Hello
        await b._handle_part({"type": "text", "id": "p1", "sessionID": "s1", "text": "Hello"})
        # 第二次：Hello world（全量）→ 只发增量 " world"
        await b._handle_part({"type": "text", "id": "p1", "sessionID": "s1", "text": "Hello world"})
        evs = []
        while not b._events.empty():
            evs.append(await b._events.get())
        assert [e.kind for e in evs] == [EventKind.TEXT_DELTA, EventKind.TEXT_DELTA]
        assert evs[0].text == "Hello"
        assert evs[1].text == " world"
    asyncio.run(run())


def test_tool_state_machine():
    async def run():
        b = OpenCodeBackend()
        base = {"type": "tool", "id": "tp", "sessionID": "s1",
                "callID": "c1", "tool": "bash"}
        await b._handle_part({**base, "state": {"status": "running", "input": {"command": "ls"}}})
        await b._handle_part({**base, "state": {"status": "running", "input": {"command": "ls"}}})  # dup
        await b._handle_part({**base, "state": {"status": "completed", "input": {"command": "ls"},
                                                "title": "listed", "output": "a b c"}})
        evs = []
        while not b._events.empty():
            evs.append(await b._events.get())
        kinds = [e.kind for e in evs]
        # 去重：只 1 个 tool_start，然后 1 个 tool_end
        assert kinds == [EventKind.TOOL_START, EventKind.TOOL_END], kinds
        assert evs[0].tool_name == "bash"
        assert evs[0].tool_input == {"command": "ls"}
        assert evs[1].tool_status == "completed"
    asyncio.run(run())


def test_reasoning_and_permission():
    async def run():
        b = OpenCodeBackend()
        await b._handle_part({"type": "reasoning", "id": "r1", "sessionID": "s1", "text": "thinking"})
        await b._translate({"type": "permission.updated",
                            "properties": {"sessionID": "s1", "id": "perm9", "title": "run rm"}})
        await b._translate({"type": "session.idle", "properties": {"sessionID": "s1"}})
        evs = []
        while not b._events.empty():
            evs.append(await b._events.get())
        kinds = [e.kind for e in evs]
        assert EventKind.THINKING_DELTA in kinds
        assert EventKind.APPROVAL_REQUEST in kinds
        assert EventKind.TURN_DONE in kinds
        perm = [e for e in evs if e.kind == EventKind.APPROVAL_REQUEST][0]
        assert perm.raw["permission_id"] == "perm9"
    asyncio.run(run())


def test_real_opencode_serve():
    """真实启动 opencode serve，验证健康检查 + 建会话 + SSE 连接。"""
    if not shutil.which("opencode"):
        print("  (skip: opencode 未安装)")
        return

    async def run():
        b = OpenCodeBackend(workdir=str(Path(__file__).resolve().parent.parent), port=5198)
        try:
            await b.start()  # 含 _wait_ready，超时会抛
            print("  ✅ opencode serve 启动 + 健康检查通过")
            # 建会话
            s = AgentSession(user_id="u", chat_id="c")
            async with b._session.post(f"{b.base}/session", json={}) as r:
                data = await r.json()
                assert data.get("id", "").startswith("ses"), data
            print(f"  ✅ 创建会话 {data['id'][:16]}…")
            # SSE 任务在跑
            assert b._sse_task and not b._sse_task.done()
            print("  ✅ SSE 事件循环已连接")
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
