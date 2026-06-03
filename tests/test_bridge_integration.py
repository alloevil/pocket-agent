"""Bridge 胶水层集成测试 — fake 后端 + fake 飞书，验证整条事件→渲染链

这是之前缺失的关键测试：backend 事件 → bridge 路由 → renderer → 飞书卡片。
用 fake 后端注入任意事件序列，断言飞书侧收到的卡片正确。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentEvent, AgentSession, EventKind


class FakeFeishu:
    def __init__(self):
        self.cards = []      # send_interactive 发出的卡片
        self.updates = []    # update_card 的 (mid, card)
        self._n = 0

    async def send_interactive(self, chat_id, card, **k):
        self._n += 1
        self.cards.append((f"m{self._n}", card))
        return {"data": {"message_id": f"m{self._n}"}}

    async def update_card(self, mid, card):
        self.updates.append((mid, card))

    async def send_text(self, *a, **k):
        return {"data": {"message_id": "mt"}}

    async def reply_text(self, *a, **k):
        return {}

    async def update_message(self, *a, **k):
        return {}

    async def close(self):
        pass


class FakeBackend:
    """不连任何真实 agent；send() 时把预设事件灌进 bridge 的队列。"""
    name = "fake"

    def __init__(self, script):
        self._script = script   # list[AgentEvent]
        self.bridge = None

    async def start(self): pass
    async def close(self): pass
    async def interrupt(self, s): pass
    async def approve(self, s, aid, ok): pass
    async def new_session(self, s): s.native_id = ""

    async def send(self, session, text):
        if not session.native_id:
            session.native_id = "fake-1"
        for ev in self._script:
            ev.route_id = "fake-1"
            await self.bridge._handle_event(ev)


def _bridge_with(script, **cfg_kw):
    cfg = Config(agent="codex", throttle_seconds=0.0, **cfg_kw)
    b = Bridge(cfg)
    b.feishu = FakeFeishu()
    b.backend = FakeBackend(script)
    b.backend.bridge = b
    return b


def test_full_turn_text_tools_done():
    async def run():
        script = [
            AgentEvent(EventKind.TURN_STARTED, tool_call_id="t1"),
            AgentEvent(EventKind.TEXT_DELTA, text="正在处理"),
            AgentEvent(EventKind.TOOL_START, tool_call_id="c1", tool_name="Bash",
                       tool_input={"command": "ls"}),
            AgentEvent(EventKind.TOOL_END, tool_call_id="c1", tool_status="completed",
                       tool_output="a b c"),
            AgentEvent(EventKind.TEXT_DELTA, text="，完成了。"),
            AgentEvent(EventKind.TURN_DONE, cost_usd=0.005),
        ]
        b = _bridge_with(script)
        await b._forward("u", "c", "m", "do it")
        s = b._get_session("u")
        assert not s.turn_active
        assert s.body_buffer == "正在处理，完成了。"
        assert s.last_cost_usd == 0.005
        assert [(t["name"], t["status"]) for t in s.tool_calls] == [("Bash", "completed")]
        # 最终卡片：绿色 + 含成本页脚
        final = b.feishu.updates[-1][1]
        assert final["header"]["template"] == "green"
        notes = [e for e in final["elements"] if e.get("tag") == "note"]
        assert any("$0.005" in n["elements"][0]["content"] for n in notes)
    asyncio.run(run())


def test_long_output_splits_into_multiple_cards():
    async def run():
        # 制造超过 max_message_length 的正文
        big = "x" * 50 + "\n"
        script = [AgentEvent(EventKind.TEXT_DELTA, text=big) for _ in range(10)]
        script.append(AgentEvent(EventKind.TURN_DONE))
        b = _bridge_with(script, max_message_length=120)
        await b._forward("u", "c", "m", "long")
        # 应发出不止一张卡片（首卡 + 至少一张续接卡）
        assert len(b.feishu.cards) >= 2, f"只发了 {len(b.feishu.cards)} 张卡片"
        s = b._get_session("u")
        assert s.card_index >= 1
        # 某张卡片标题带分页标记
        titles = [c["header"]["title"]["content"] for _, c in b.feishu.cards]
        assert any("(#2)" in t for t in titles)
    asyncio.run(run())


def test_error_event_renders_red():
    async def run():
        script = [
            AgentEvent(EventKind.TEXT_DELTA, text="partial"),
            AgentEvent(EventKind.ERROR, error="boom"),
        ]
        b = _bridge_with(script)
        await b._forward("u", "c", "m", "x")
        final = b.feishu.updates[-1][1]
        assert final["header"]["template"] == "red"
        assert any("boom" in e.get("content", "") for e in final["elements"]
                   if e.get("tag") == "markdown")
    asyncio.run(run())


def test_approval_card_uses_markdown_and_buttons():
    async def run():
        script = [
            AgentEvent(EventKind.APPROVAL_REQUEST, approval_id="cmd:1",
                       approval_kind="command", approval_text="rm -rf /tmp/x",
                       approval_detail="/tmp", raw={"request_msg_id": 1}),
        ]
        b = _bridge_with(script)
        await b._forward("u", "c", "m", "x")
        # 审批卡片是 send_interactive 的最后一张
        approval = b.feishu.cards[-1][1]
        assert approval["header"]["template"] == "orange"
        tags = [e.get("tag") for e in approval["elements"]]
        assert "markdown" in tags and "action" in tags
        # pending 已记录
        s = b._get_session("u")
        assert "cmd:1" in s.pending_approvals
    asyncio.run(run())


def test_stop_with_no_active_turn():
    async def run():
        b = _bridge_with([])
        replies = []

        async def fake_reply(mid, t):
            replies.append(t)
        b.feishu.reply_text = fake_reply
        await b._handle_command("u", "c", "m", "/stop")
        assert any("没有正在执行" in r for r in replies)
    asyncio.run(run())


def test_unknown_command_hints_help():
    async def run():
        b = _bridge_with([])
        replies = []

        async def fake_reply(mid, t):
            replies.append(t)
        b.feishu.reply_text = fake_reply
        await b._handle_command("u", "c", "m", "/foobar")
        assert any("/help" in r for r in replies)
    asyncio.run(run())


def test_message_while_turn_active_rejected_for_subprocess():
    async def run():
        b = _bridge_with([])
        b.backend.name = "claude"      # 模拟子进程后端（不支持并发）
        sent = []

        async def fake_send_text(cid, t, **k):
            sent.append(t)
            return {"data": {"message_id": "x"}}
        b.feishu.send_text = fake_send_text
        s = AgentSession(user_id="u", chat_id="c", native_id="x")
        s.turn_active = True
        b.store.add(s)
        await b._forward("u", "c", "m", "second message")
        assert any("处理中" in t and "/stop" in t for t in sent)
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
