"""会话管理与新命令测试：SessionStore、多会话命令、/cd /model、diff、重试、持久化"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentEvent, AgentSession, EventKind
from pocket_agent.session_store import SessionStore
from pocket_agent.renderer import CardRenderer, _render_diff


# ── SessionStore ──

def test_store_create_switch_list():
    st = SessionStore()
    a = st.create("u", "c", title="第一个")
    b = st.create("u", "c", title="第二个")
    assert st.active("u", "c") is b           # 新建即活跃
    assert len(st.list_sessions("u", "c")) == 2
    assert st.switch("u", "c", a.session_id)
    assert st.active("u", "c") is a
    assert not st.switch("u", "c", "nope")


def test_store_groups_by_chat():
    st = SessionStore()
    s1 = st.create("u", "chatA")
    s2 = st.create("u", "chatB")
    assert st.active("u", "chatA") is s1
    assert st.active("u", "chatB") is s2       # 不同 chat 互不干扰


def test_store_by_native_routing():
    st = SessionStore()
    s = st.create("u", "c")
    s.native_id = "thread-9"
    assert st.by_native("thread-9") is s
    assert st.by_native("nope") is None


def test_store_remove_active_falls_back():
    st = SessionStore()
    a = st.create("u", "c")
    b = st.create("u", "c")
    st.remove_active("u", "c")                 # 删 b（当前活跃）
    assert st.active("u", "c") is a            # 回落到 a


def test_store_persist_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sessions.json")
        st = SessionStore(persist_path=path)
        s = st.create("u", "c", title="持久化测试")
        s.native_id = "claude-sess-1"
        s.total_turns = 5
        s.total_cost_usd = 0.12
        s.history.append({"prompt": "hi", "reply": "yo", "tools": [], "cost": 0.01})
        st.save()
        # 新 store 从磁盘加载
        st2 = SessionStore(persist_path=path)
        st2.load()
        r = st2.active("u", "c")
        assert r is not None
        assert r.title == "持久化测试"
        assert r.native_id == "claude-sess-1"
        assert r.total_turns == 5
        assert abs(r.total_cost_usd - 0.12) < 1e-9
        assert r.history and r.history[0]["prompt"] == "hi"


# ── diff 渲染 ──

def test_render_diff():
    out = _render_diff({"old_string": "a=1\nb=2", "new_string": "a=1\nb=3"})
    assert "```diff" in out
    assert "- a=1" in out and "- b=2" in out
    assert "+ a=1" in out and "+ b=3" in out
    assert _render_diff({"command": "ls"}) == ""    # 无 old/new → 空
    assert _render_diff(None) == ""


def test_approval_card_includes_diff():
    r = CardRenderer("claude")
    s = AgentSession(user_id="u", chat_id="c")
    card = r.render_approval(s, "a1", "file", "编辑 foo.py", "Edit",
                             tool_input={"old_string": "x", "new_string": "y"})
    body = card["elements"][0]["content"]
    assert "```diff" in body and "- x" in body and "+ y" in body


# ── bridge 命令 ──

class _FakeFeishu:
    def __init__(self): self.replies = []; self.cards = []; self._n = 0
    async def send_interactive(self, c, card, **k):
        self._n += 1; self.cards.append(card)
        return {"data": {"message_id": f"m{self._n}"}}
    async def update_card(self, *a): pass
    async def send_text(self, *a, **k): return {"data": {"message_id": "mt"}}
    async def reply_text(self, mid, t): self.replies.append(t)
    async def close(self): pass


class _FakeBackend:
    name = "codex"
    def __init__(self): self.sent = []
    async def start(self): pass
    async def close(self): pass
    async def send(self, s, t):
        if not s.native_id:
            s.native_id = "th"
        self.sent.append(t)
    async def interrupt(self, s): pass
    async def new_session(self, s): s.native_id = ""


def _bridge(**cfg):
    b = Bridge(Config(agent="codex", throttle_seconds=0.0, **cfg))
    b.feishu = _FakeFeishu()
    b.backend = _FakeBackend()
    return b


def test_cmd_list_and_switch():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "任务一")
        await b._handle_command("u", "c", "m", "/new")
        await b._forward("u", "c", "m", "任务二")
        await b._handle_command("u", "c", "m", "/list")
        listing = b.feishu.replies[-1]
        assert "s1" in listing and "s2" in listing
        # 切回 s1
        await b._handle_command("u", "c", "m", "/switch s1")
        assert b.store.active("u", "c").session_id == "s1"
    asyncio.run(run())


def test_cmd_cd_validates_dir():
    async def run():
        b = _bridge()
        await b._handle_command("u", "c", "m", "/cd /no/such/dir/xyz")
        assert "不存在" in b.feishu.replies[-1]
        await b._handle_command("u", "c", "m", "/cd /tmp")
        assert "已设为" in b.feishu.replies[-1]
        assert b.store.active("u", "c").workdir_override == "/tmp"
    asyncio.run(run())


def test_cmd_model_session_scoped():
    async def run():
        b = _bridge()
        await b._handle_command("u", "c", "m", "/model opus")
        s = b.store.active("u", "c")
        assert s.model_override == "opus"
    asyncio.run(run())


def test_retry_reuses_last_prompt():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "原始指令")
        s = b.store.active("u", "c")
        s.turn_active = False                  # 模拟已结束
        assert s.last_prompt == "原始指令"
        n_before = len(b.backend.sent)
        # 模拟点击重试按钮
        class _Ev:
            class event:
                class action:
                    value = {"action": "retry", "uid": "u", "cid": "c",
                             "sid": s.session_id}
        await b.on_card_action(_Ev())
        assert len(b.backend.sent) == n_before + 1
        assert b.backend.sent[-1] == "原始指令"
    asyncio.run(run())


def test_turn_done_records_history():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "做点事")
        s = b.store.active("u", "c")
        s.body_buffer = "做完了"
        await b._handle_event(AgentEvent(EventKind.TURN_DONE, route_id="th", cost_usd=0.02))
        assert s.history and s.history[-1]["prompt"] == "做点事"
        assert s.history[-1]["reply"] == "做完了"
        assert s.total_turns == 1
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
