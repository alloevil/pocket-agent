"""第二批 cc-connect 借鉴：代码围栏分块、@提及剥离、工具紧凑、空闲轮换、文件回传"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentEvent, AgentSession, EventKind
from pocket_agent.renderer import CardRenderer, split_at_codefence_boundary
from pocket_agent.backends.claude import ClaudeBackend


# ── 代码围栏感知分块 ──

def test_split_keeps_code_fence_intact():
    text = "before\n```python\n" + "\n".join(f"x{i}=1" for i in range(50)) + "\n```\nafter"
    head, tail = split_at_codefence_boundary(text, 120)
    assert head.rstrip().endswith("```"), "head 应补全闭合围栏"
    assert tail.lstrip().startswith("```python"), "tail 应用同语言重开"
    assert head.count("```") % 2 == 0 and tail.count("```") % 2 == 0


def test_split_plain_text_no_spurious_fence():
    plain = "\n".join(f"paragraph {i} words here" for i in range(20))
    head, tail = split_at_codefence_boundary(plain, 100)
    assert tail and "```" not in head and "```" not in tail


def test_split_no_op_when_short():
    h, t = split_at_codefence_boundary("short", 100)
    assert h == "short" and t == ""


# ── @提及剥离 ──

class _M:
    def __init__(self, key): self.key = key


def test_strip_mentions_with_map():
    assert Bridge._strip_mentions("@_user_1 看下代码", [_M("@_user_1")]) == "看下代码"
    assert Bridge._strip_mentions("@_user_1 @_user_2 hi", [_M("@_user_1"), _M("@_user_2")]) == "hi"


def test_strip_mentions_fallback():
    assert Bridge._strip_mentions("@_user_1 在吗", None) == "在吗"
    assert Bridge._strip_mentions("@_all 通知", None) == "通知"
    assert Bridge._strip_mentions("正常消息", None) == "正常消息"


# ── 工具列表紧凑 ──

def test_tool_list_compaction():
    r = CardRenderer("claude")
    s = AgentSession(user_id="u", chat_id="c")
    s.tool_calls = [{"id": str(i), "name": f"T{i}", "input": {}, "status": "completed"}
                    for i in range(15)]
    s.body_buffer = "done"
    txt = "\n".join(e["content"] for e in r.render(s, done=True)["elements"]
                     if e.get("tag") == "markdown")
    assert "中间省略 6" in txt  # 15 - 6 - 3 = 6


def test_tool_list_no_compaction_under_threshold():
    r = CardRenderer("claude")
    s = AgentSession(user_id="u", chat_id="c")
    s.tool_calls = [{"id": str(i), "name": f"T{i}", "input": {}, "status": "completed"}
                    for i in range(9)]
    s.body_buffer = "done"
    txt = "\n".join(e["content"] for e in r.render(s, done=True)["elements"]
                     if e.get("tag") == "markdown")
    assert "省略" not in txt


# ── 空闲轮换 ──

class _FakeFeishu:
    def __init__(self): self._n = 0
    async def send_interactive(self, c, card, **k):
        self._n += 1
        return {"data": {"message_id": f"m{self._n}"}}
    async def update_card(self, *a): pass
    async def send_text(self, *a, **k): return {"data": {"message_id": "mt"}}
    async def reply_text(self, *a, **k): return {}
    async def upload_image(self, data): return "imgkey"
    async def upload_file(self, data, name, ft="stream"): return "filekey"
    async def send_image(self, *a, **k): pass
    async def send_file(self, *a, **k): pass
    async def close(self): pass


class _FakeBackend:
    name = "codex"
    def __init__(self): self.new_session_calls = 0
    async def start(self): pass
    async def close(self): pass
    async def send(self, s, t):
        if not s.native_id:
            s.native_id = "thread"
    async def interrupt(self, s): pass
    async def new_session(self, s):
        self.new_session_calls += 1
        s.native_id = ""


def _bridge(**cfg):
    b = Bridge(Config(agent="codex", throttle_seconds=0.0, **cfg))
    b.feishu = _FakeFeishu()
    b.backend = _FakeBackend()
    return b


def test_idle_rotation_triggers():
    async def run():
        b = _bridge(idle_minutes=10)
        await b._forward("u", "c", "m1", "hi")
        s = b._get_session("u")
        s.turn_active = False
        s.last_user_msg_time = time.time() - 11 * 60
        await b._forward("u", "c", "m2", "hi")
        assert b.backend.new_session_calls == 1
    asyncio.run(run())


def test_idle_no_rotation_within_window():
    async def run():
        b = _bridge(idle_minutes=10)
        await b._forward("u", "c", "m1", "hi")
        s = b._get_session("u")
        s.turn_active = False
        s.last_user_msg_time = time.time() - 60
        await b._forward("u", "c", "m2", "hi")
        assert b.backend.new_session_calls == 0
    asyncio.run(run())


def test_idle_disabled_when_zero():
    async def run():
        b = _bridge(idle_minutes=0)
        await b._forward("u", "c", "m1", "hi")
        s = b._get_session("u")
        s.turn_active = False
        s.last_user_msg_time = time.time() - 9999 * 60
        await b._forward("u", "c", "m2", "hi")
        assert b.backend.new_session_calls == 0
    asyncio.run(run())


# ── 文件/图片回传 ──

def test_claude_notes_image_write_only():
    b = ClaudeBackend(workdir="/tmp", approvals=False)
    b._note_file_output({"id": "t1", "name": "Write", "input": {"file_path": "/tmp/c.png"}})
    assert b._pending_files.get("t1") == "/tmp/c.png"
    b._note_file_output({"id": "t2", "name": "Write", "input": {"file_path": "/tmp/x.py"}})
    assert "t2" not in b._pending_files       # 非图片
    b._note_file_output({"id": "t3", "name": "Bash", "input": {"command": "a.png"}})
    assert "t3" not in b._pending_files       # 非 Write
    b._note_file_output({"id": "t4", "name": "Write", "input": {"file_path": "rel.jpg"}})
    assert b._pending_files["t4"] == "/tmp/rel.jpg"   # 相对路径解析


def test_bridge_sends_image_output():
    async def run():
        b = _bridge()
        s = AgentSession(user_id="u", chat_id="c", native_id="x", turn_active=True)
        s.card_message_id = "m1"
        b.store.add(s)
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(b"\x89PNG fake")
        f.close()
        sent = {"n": 0}
        orig = b.feishu.send_image
        async def counting(*a, **k):
            sent["n"] += 1
            await orig(*a, **k)
        b.feishu.send_image = counting
        await b._handle_event(AgentEvent(EventKind.FILE_OUTPUT, route_id="x",
                                         file_path=f.name, is_image=True))
        assert sent["n"] == 1
        # 不存在的文件安全跳过
        await b._handle_event(AgentEvent(EventKind.FILE_OUTPUT, route_id="x",
                                         file_path="/no/such.png", is_image=True))
        assert sent["n"] == 1
        os.unlink(f.name)
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
