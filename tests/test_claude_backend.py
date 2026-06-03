"""ClaudeBackend NDJSON 解析测试 + 真实 claude CLI 端到端（若已安装）"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.events import AgentSession, EventKind
from pocket_agent.backends.claude import ClaudeBackend


async def _drain(backend, n, timeout=2.0):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(backend._events.get(), timeout))
    return out


def test_parse_recorded_stream():
    """喂录制的 NDJSON 事件，断言翻译出的 AgentEvent 序列"""
    async def run():
        b = ClaudeBackend()
        s = AgentSession(user_id="u", chat_id="c")

        class FakeStdout:
            def __init__(self, lines):
                self._lines = [l.encode() for l in lines]
            def __aiter__(self):
                self._i = 0
                return self
            async def __anext__(self):
                if self._i >= len(self._lines):
                    raise StopAsyncIteration
                line = self._lines[self._i]
                self._i += 1
                return line

        class FakeProc:
            def __init__(self, lines):
                self.stdout = FakeStdout(lines)

        import json as _json
        lines = [
            _json.dumps({"type": "system", "subtype": "init",
                         "session_id": "sess-abc"}) + "\n",
            _json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "Hello "}}}) + "\n",
            _json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "hmm"}}}) + "\n",
            # 工具调用：start + json delta + stop
            _json.dumps({"type": "stream_event", "event": {
                "type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "Bash"}}}) + "\n",
            _json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}}}) + "\n",
            _json.dumps({"type": "stream_event", "event": {
                "type": "content_block_stop", "index": 1}}) + "\n",
            _json.dumps({"type": "result", "subtype": "success",
                         "session_id": "sess-abc", "total_cost_usd": 0.01}) + "\n",
        ]
        await b._read_stream(s, FakeProc(lines), "claude-pending-1")

        kinds = []
        while not b._events.empty():
            kinds.append((await b._events.get()))

        ks = [e.kind for e in kinds]
        assert EventKind.SESSION_ID in ks
        assert EventKind.TURN_STARTED in ks
        assert EventKind.TEXT_DELTA in ks
        assert EventKind.THINKING_DELTA in ks
        assert EventKind.TOOL_START in ks
        assert EventKind.TURN_DONE in ks
        # session_id 已回填
        assert s.native_id == "sess-abc"
        # 工具参数解析正确
        tool = [e for e in kinds if e.kind == EventKind.TOOL_START][0]
        assert tool.tool_name == "Bash"
        assert tool.tool_input == {"command": "ls"}
        # 文本增量
        text = "".join(e.text for e in kinds if e.kind == EventKind.TEXT_DELTA)
        assert text == "Hello "

    asyncio.run(run())


def test_missing_binary_emits_error():
    async def run():
        b = ClaudeBackend(command="claude-does-not-exist-xyz")
        s = AgentSession(user_id="u", chat_id="c")
        await b._run_turn(s, "hi")
        ev = await asyncio.wait_for(b._events.get(), 2.0)
        assert ev.kind == EventKind.ERROR
        assert "找不到" in ev.error
    asyncio.run(run())


def test_real_claude_cli():
    """真实跑一次 claude（若安装）。验证解析器对真实输出有效。"""
    if not shutil.which("claude"):
        print("  (skip: claude 未安装)")
        return

    async def run():
        b = ClaudeBackend(workdir=str(Path(__file__).resolve().parent.parent))
        s = AgentSession(user_id="u", chat_id="c")
        await b.send(s, "Reply with exactly the word: PONG. Do not use any tools.")
        # 收集事件直到 turn_done 或超时
        kinds = []
        try:
            while True:
                ev = await asyncio.wait_for(b._events.get(), timeout=60)
                kinds.append(ev)
                if ev.kind == EventKind.TURN_DONE:
                    break
        except asyncio.TimeoutError:
            raise AssertionError("real claude turn timed out")
        body = "".join(e.text for e in kinds if e.kind == EventKind.TEXT_DELTA)
        assert "PONG" in body.upper(), f"unexpected body: {body!r}"
        assert s.native_id and not s.native_id.startswith("claude-pending"), s.native_id
        print(f"  real claude session_id={s.native_id[:12]}… body={body.strip()[:40]!r}")

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
