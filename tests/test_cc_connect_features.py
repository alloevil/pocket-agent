"""借鉴 cc-connect 的三项增强测试：用量累计/usage、表格切分、渲染抽象"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentEvent, AgentSession, EventKind
from pocket_agent.renderer import CardRenderer, split_markdown_by_tables
from pocket_agent.renderer_base import Renderer


class FakeFeishu:
    def __init__(self):
        self.cards = []
        self.replies = []
        self.updates = 0
        self._n = 0

    async def send_interactive(self, chat_id, card, **k):
        self._n += 1
        self.cards.append(card)
        return {"data": {"message_id": f"m{self._n}"}}

    async def update_card(self, mid, card):
        self.updates += 1

    async def send_text(self, *a, **k):
        return {"data": {"message_id": "mt"}}

    async def reply_text(self, mid, t):
        self.replies.append(t)

    async def close(self):
        pass


class FakeBackend:
    name = "codex"
    async def start(self): pass
    async def close(self): pass
    async def send(self, s, t):
        if not s.native_id:
            s.native_id = "x"
    async def interrupt(self, s): pass
    async def new_session(self, s): s.native_id = ""


def _bridge():
    b = Bridge(Config(agent="codex", throttle_seconds=0.0))
    b.feishu = FakeFeishu()
    b.backend = FakeBackend()
    return b


# ── 渲染抽象 ──

def test_card_renderer_implements_renderer():
    assert issubclass(CardRenderer, Renderer)
    r = CardRenderer("claude")
    s = AgentSession(user_id="u", chat_id="c")
    # 抽象方法都可调用
    assert isinstance(r.render(s, done=True), dict)
    assert isinstance(r.render_approval(s, "a1", "command", "ls", "/tmp"), dict)
    assert isinstance(r.split_long("x"), list)


# ── 表格切分 ──

def test_split_markdown_by_tables():
    tbl = "| a | b |\n| - | - |\n| 1 | 2 |"
    md = "intro\n\n" + "\n\n".join([tbl] * 4)
    assert len(split_markdown_by_tables(md, max_tables=2)) == 3  # 前2张+其余2张各一条
    assert len(split_markdown_by_tables(md, max_tables=10)) == 1  # 未超额不切
    assert len(split_markdown_by_tables("no tables here", max_tables=3)) == 1


# ── /usage 与累计 ──

def test_usage_command():
    async def run():
        b = _bridge()
        s = AgentSession(user_id="u", chat_id="c", native_id="x")
        s.total_turns = 3
        s.total_cost_usd = 0.0321
        s.total_tool_calls = 7
        b.store.add(s)
        await b._handle_command("u", "c", "m", "/usage")
        txt = b.feishu.replies[-1]
        assert "3" in txt and "0.0321" in txt and "7" in txt
    asyncio.run(run())


def test_usage_empty():
    async def run():
        b = _bridge()
        b.store.add(AgentSession(user_id="u", chat_id="c"))
        await b._handle_command("u", "c", "m", "/usage")
        assert "还没有" in b.feishu.replies[-1]
    asyncio.run(run())


def test_turn_done_accumulates_usage():
    async def run():
        b = _bridge()
        s = AgentSession(user_id="u", chat_id="c", native_id="x", turn_active=True)
        s.card_message_id = "m1"
        s.tool_calls = [{"id": "1", "name": "Bash", "status": "completed"}]
        b.store.add(s)
        await b._handle_event(AgentEvent(EventKind.TURN_DONE, route_id="x", cost_usd=0.01))
        assert s.total_turns == 1
        assert abs(s.total_cost_usd - 0.01) < 1e-9
        assert s.total_tool_calls == 1
        # 第二轮继续累计
        s.turn_active = True
        s.tool_calls = [{"id": "2", "name": "Read", "status": "completed"}]
        await b._handle_event(AgentEvent(EventKind.TURN_DONE, route_id="x", cost_usd=0.02))
        assert s.total_turns == 2
        assert abs(s.total_cost_usd - 0.03) < 1e-9
        assert s.total_tool_calls == 2
    asyncio.run(run())


def test_finish_turn_splits_many_tables():
    async def run():
        b = _bridge()
        s = AgentSession(user_id="u", chat_id="c", native_id="x", turn_active=True)
        s.card_message_id = "m1"
        tbl = "| a | b |\n| - | - |\n| 1 | 2 |"
        s.body_buffer = "intro\n\n" + "\n\n".join([tbl] * 5)
        b.store.add(s)
        n_before = len(b.feishu.cards)
        await b._finish_turn(s)
        assert len(b.feishu.cards) > n_before, "表格过多应额外发卡片"
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
