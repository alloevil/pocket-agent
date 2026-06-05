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


def test_read_approval_not_labeled_as_modify():
    # Read 类审批应显示「读取」而非「文件修改」，避免误导
    r = CardRenderer("claude")
    s = AgentSession(user_id="u", chat_id="c")
    card = r.render_approval(s, "a1", "read", "/some/file.jsonl", "Read")
    title = card["header"]["title"]["content"]
    body = card["elements"][0]["content"]
    assert "读取" in title and "修改" not in title
    assert "读取" in body


# ── bridge 命令 ──

class _FakeFeishu:
    def __init__(self): self.replies = []; self.cards = []; self._n = 0
    async def send_interactive(self, c, card, **k):
        self._n += 1; self.cards.append(card)
        return {"data": {"message_id": f"m{self._n}"}}
    async def update_card(self, *a): pass
    async def send_text(self, *a, **k): return {"data": {"message_id": "mt"}}
    async def reply_text(self, mid, t): self.replies.append(t)
    async def reply_card(self, mid, card):
        parts = [card.get("header", {}).get("title", {}).get("content", "")]
        parts += [e.get("content", "") for e in card.get("elements", []) if e.get("tag") == "markdown"]
        self.replies.append("\n".join(parts))
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
        assert "已更新" in b.feishu.replies[-1]
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


# ── store rename / delete ──

def test_store_rename():
    from pocket_agent.session_store import SessionStore
    st = SessionStore()
    s = st.create("u", "c", title="旧")
    assert st.rename("u", "c", s.session_id, "新标题")
    assert st.get("u", "c", s.session_id).title == "新标题"
    assert not st.rename("u", "c", "nope", "x")


def test_store_delete():
    from pocket_agent.session_store import SessionStore
    st = SessionStore()
    s1 = st.create("u", "c")
    s2 = st.create("u", "c")   # s2 成为 active
    assert st.active("u", "c").session_id == s2.session_id
    # 删 active → active 落到剩余的 s1
    assert st.delete("u", "c", s2.session_id)
    assert st.active("u", "c").session_id == s1.session_id
    # 删不存在 → False
    assert not st.delete("u", "c", "nope")
    # 删最后一个 → active 为空
    assert st.delete("u", "c", s1.session_id)
    assert st.active("u", "c") is None


# ── 新命令分支 ──

def test_cmd_clear_resets_native_id():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "hi")
        s = b.store.active("u", "c")
        assert s.native_id   # 首轮后已有
        await b._handle_command("u", "c", "m", "/clear")
        assert s.native_id == ""   # 清空，下一轮无上下文
    asyncio.run(run())


def test_cmd_pwd_and_status():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "hi")
        await b._handle_command("u", "c", "m", "/pwd")
        assert any("工作目录" in r for r in b.feishu.replies)
        await b._handle_command("u", "c", "m", "/status")
        assert any("Agent" in r and "状态" in r for r in b.feishu.replies)
    asyncio.run(run())


def test_cmd_rename_and_delete():
    async def run():
        b = _bridge()
        await b._forward("u", "c", "m", "hi")
        s = b.store.active("u", "c")
        await b._handle_command("u", "c", "m", "/rename 我的任务")
        assert s.title == "我的任务"
        assert any("已重命名" in r for r in b.feishu.replies)
        # 删除
        await b._handle_command("u", "c", "m", f"/delete {s.session_id}")
        assert any("已删除" in r for r in b.feishu.replies)
        assert b.store.get("u", "c", s.session_id) is None
        # 删不存在
        await b._handle_command("u", "c", "m", "/delete nope")
        assert any("未找到" in r for r in b.feishu.replies)
    asyncio.run(run())


def test_cmd_retry_no_prompt():
    async def run():
        b = _bridge()
        b.store.create("u", "c")   # 空会话，无 last_prompt
        await b._handle_command("u", "c", "m", "/retry")
        assert any("无可重试" in r for r in b.feishu.replies)
    asyncio.run(run())


# ── /history + /resume 接 claude 本地历史 ──

def test_cmd_history_lists_sessions():
    async def run():
        from pocket_agent import claude_sessions
        b = _bridge()
        b.agent_name = "claude"
        orig = claude_sessions.list_sessions
        claude_sessions.list_sessions = lambda wd, **k: [
            {"id": "abcd1234efgh", "summary": "改个 bug", "rel": "2小时前", "cwd": "/x"}]
        try:
            await b._handle_command("u", "c", "m", "/history")
        finally:
            claude_sessions.list_sessions = orig
        assert any("abcd1234" in r and "改个 bug" in r for r in b.feishu.replies)
    asyncio.run(run())


def test_cmd_history_non_claude():
    async def run():
        b = _bridge()           # codex 后端
        await b._handle_command("u", "c", "m", "/history")
        assert any("仅 claude" in r for r in b.feishu.replies)
    asyncio.run(run())


def test_resume_external_uuid():
    async def run():
        from pocket_agent import claude_sessions
        b = _bridge()
        b.agent_name = "claude"
        orig = claude_sessions.session_exists
        claude_sessions.session_exists = lambda wd, sid: (
            {"id": "uuid-1234", "cwd": "/proj/x"} if sid == "uuid-1234" else None)
        try:
            await b._handle_command("u", "c", "m", "/resume uuid-1234")
        finally:
            claude_sessions.session_exists = orig
        s = b.store.active("u", "c")
        assert s.native_id == "uuid-1234"           # 下一轮 --resume
        assert s.workdir_override == "/proj/x"        # 绑回原 cwd
        assert any("已恢复 claude 历史" in r for r in b.feishu.replies)
    asyncio.run(run())


def test_resume_unknown_id():
    async def run():
        from pocket_agent import claude_sessions
        b = _bridge()
        b.agent_name = "claude"
        orig_e, orig_l = claude_sessions.session_exists, claude_sessions.list_sessions
        claude_sessions.session_exists = lambda wd, sid: None
        claude_sessions.list_sessions = lambda wd, **k: []
        try:
            await b._handle_command("u", "c", "m", "/resume nope999")
        finally:
            claude_sessions.session_exists, claude_sessions.list_sessions = orig_e, orig_l
        assert any("未找到" in r for r in b.feishu.replies)
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
