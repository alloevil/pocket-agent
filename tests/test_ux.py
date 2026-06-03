"""用户体验增强测试：非文本回执、命令纠错、欢迎、批量审批、完成提醒、心跳"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentEvent, AgentSession, EventKind


class _Msg:
    def __init__(self, mtype, content="{}", mentions=None):
        self.message_type = mtype
        self.content = content
        self.chat_id = "c"
        self.message_id = "m"
        self.mentions = mentions


class _Data:
    def __init__(self, msg):
        class _Sender:
            class sender_id:
                open_id = "u"
        class _E:
            message = msg
            sender = _Sender()
        self.event = _E()


class _Action:
    def __init__(self, value):
        class _E:
            class action:
                pass
        self.event = _E()
        self.event.action.value = value


class FakeFeishu:
    def __init__(self):
        self.replies = []; self.cards = []; self.texts = []; self._n = 0
    async def send_interactive(self, c, card, **k):
        self._n += 1; self.cards.append(card)
        return {"data": {"message_id": f"m{self._n}"}}
    async def update_card(self, *a): pass
    async def send_text(self, c, t, **k):
        self.texts.append(t); return {"data": {"message_id": "mt"}}
    async def reply_text(self, mid, t): self.replies.append(t)
    async def close(self): pass


class FakeBackend:
    name = "codex"
    async def start(self): pass
    async def close(self): pass
    async def send(self, s, t):
        if not s.native_id:
            s.native_id = "th"
    async def interrupt(self, s): pass
    async def new_session(self, s): s.native_id = ""


def _bridge(**cfg):
    cfg.setdefault("heartbeat_seconds", 0)
    b = Bridge(Config(agent="codex", throttle_seconds=0.0, **cfg))
    b.feishu = FakeFeishu(); b.backend = FakeBackend()
    return b


def _has_welcome(cards):
    return sum("欢迎" in c.get("header", {}).get("title", {}).get("content", "")
               for c in cards)


def test_non_text_message_replies():
    async def run():
        b = _bridge()
        await b.on_feishu_message(_Data(_Msg("audio")))
        assert any("语音" in r for r in b.feishu.replies)
        b2 = _bridge()
        await b2.on_feishu_message(_Data(_Msg("image")))
        assert any("图片" in r for r in b2.feishu.replies)
    asyncio.run(run())


def test_command_typo_suggestion():
    async def run():
        b = _bridge()
        await b._handle_command("u", "c", "m", "/lst")
        assert any("/list" in r for r in b.feishu.replies)
        b2 = _bridge()
        await b2._handle_command("u", "c", "m", "/zzz999")  # 无相近
        assert any("/help" in r for r in b2.feishu.replies)
    asyncio.run(run())


def test_welcome_shown_once():
    async def run():
        b = _bridge()
        await b.on_feishu_message(_Data(_Msg("text", '{"text":"hi"}')))
        assert _has_welcome(b.feishu.cards) == 1
        await b.on_feishu_message(_Data(_Msg("text", '{"text":"again"}')))
        assert _has_welcome(b.feishu.cards) == 1   # 不重复
    asyncio.run(run())


def test_approve_all_this_turn():
    async def run():
        b = _bridge()
        s = b.store.create("u", "c"); s.native_id = "th"; s.turn_active = True
        approved = []
        async def fake_approve(sess, aid, ok):
            approved.append((aid, ok)); sess.pending_approvals.pop(aid, None)
        b.backend.approve = fake_approve

        await b._handle_event(AgentEvent(EventKind.APPROVAL_REQUEST, route_id="th",
            approval_id="a1", approval_kind="command", approval_text="ls",
            raw={"request_msg_id": 1}))
        await b.on_card_action(_Action({"action": "approve_all", "key": "a1",
                                        "uid": "u", "cid": "c"}))
        assert s.auto_approve_turn
        n = len(b.feishu.cards)
        # 后续审批自动批准，不再弹卡
        await b._handle_event(AgentEvent(EventKind.APPROVAL_REQUEST, route_id="th",
            approval_id="a2", approval_kind="command", approval_text="rm",
            raw={"request_msg_id": 2}))
        assert len(b.feishu.cards) == n
        assert ("a2", True) in approved
    asyncio.run(run())


def test_notify_done_long_task():
    async def run():
        b = _bridge(notify_done_seconds=1)
        s = b.store.create("u", "c"); s.native_id = "th"
        s.turn_active = True; s.card_message_id = "m1"
        s.turn_start_time = time.time() - 5
        await b._handle_event(AgentEvent(EventKind.TURN_DONE, route_id="th"))
        assert any("任务完成" in t for t in b.feishu.texts)
    asyncio.run(run())


def test_no_notify_short_task():
    async def run():
        b = _bridge(notify_done_seconds=60)
        s = b.store.create("u", "c"); s.native_id = "th"
        s.turn_active = True; s.card_message_id = "m1"
        s.turn_start_time = time.time() - 2
        await b._handle_event(AgentEvent(EventKind.TURN_DONE, route_id="th"))
        assert not any("任务完成" in t for t in b.feishu.texts)
    asyncio.run(run())


def test_heartbeat_refreshes_idle_card():
    async def run():
        # heartbeat_seconds 很短，验证静默期会刷新卡片
        b = _bridge(heartbeat_seconds=1)
        s = b.store.create("u", "c"); s.native_id = "th"
        s.turn_active = True; s.card_message_id = "m1"
        s.turn_start_time = time.time(); s.last_update_time = 0.0
        updates = {"n": 0}
        async def count_update(mid, card): updates["n"] += 1
        b.feishu.update_card = count_update
        b._start_heartbeat(s)
        await asyncio.sleep(2.3)            # 应触发 ~2 次心跳
        s.turn_active = False
        b._stop_heartbeat(s)
        assert updates["n"] >= 1, f"心跳应刷新卡片, got {updates['n']}"
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
