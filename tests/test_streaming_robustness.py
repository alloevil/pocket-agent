"""流式稳健性 + 飞书重试层测试（从 cc-connect 借鉴的机制）

覆盖：最小增量门槛、相同内容跳过、连续失败降级、freeze 定格、
飞书请求层瞬时错误重试 / token 失效刷新 / 限流码不重试。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from pocket_agent.config import Config
from pocket_agent.bridge import Bridge
from pocket_agent.events import AgentSession
from pocket_agent.feishu_client import FeishuAPI


class FakeFeishu:
    def __init__(self, fail=False):
        self.cards = []
        self.update_calls = 0
        self._n = 0
        self.fail = fail

    async def send_interactive(self, chat_id, card, **k):
        self._n += 1
        self.cards.append(card)
        return {"data": {"message_id": f"m{self._n}"}}

    async def update_card(self, mid, card):
        self.update_calls += 1
        if self.fail:
            raise RuntimeError("feishu PATCH fail")

    async def send_text(self, *a, **k):
        return {"data": {"message_id": "mt"}}

    async def reply_text(self, *a, **k):
        return {}

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


def _bridge(fail=False, **cfg):
    c = Config(agent="codex", throttle_seconds=0.0, min_delta_chars=30, **cfg)
    b = Bridge(c)
    b.feishu = FakeFeishu(fail=fail)
    b.backend = FakeBackend()
    return b


def _session(b):
    s = AgentSession(user_id="u", chat_id="c", native_id="x", turn_active=True)
    s.card_message_id = "m1"
    b.store.add(s)
    return s


def test_min_delta_chars_gate():
    async def run():
        b = _bridge()
        s = _session(b)
        s.body_buffer = "短"          # < 30 字
        await b._maybe_update(s)
        assert b.feishu.update_calls == 0, "小增量不应触发 PATCH"
        s.body_buffer = "x" * 40       # 跨过门槛
        await b._maybe_update(s)
        assert b.feishu.update_calls == 1
    asyncio.run(run())


def test_skip_identical_content():
    async def run():
        b = _bridge()
        s = _session(b)
        s.body_buffer = "x" * 40
        await b._maybe_update(s, force=True)
        n = b.feishu.update_calls
        await b._maybe_update(s, force=True)   # 内容没变
        await b._maybe_update(s, force=True)
        assert b.feishu.update_calls == n, "相同内容应跳过 PATCH"
    asyncio.run(run())


def test_degrade_after_repeated_failures():
    async def run():
        b = _bridge(fail=True)
        s = _session(b)
        for i in range(5):
            s.body_buffer = "x" * (50 * (i + 1))   # 每次都变，绕过去重
            await b._maybe_update(s, force=True)
        assert s.card_degraded, "连续失败应降级"
        n = b.feishu.update_calls
        s.body_buffer = "x" * 9999
        await b._maybe_update(s, force=True)
        assert b.feishu.update_calls == n, "降级后不应再 PATCH"
    asyncio.run(run())


def test_freeze_stops_streaming():
    async def run():
        b = _bridge()
        s = _session(b)
        b._freeze_card(s)
        assert s.card_degraded
        n = b.feishu.update_calls
        s.body_buffer = "x" * 9999
        await b._maybe_update(s, force=True)
        assert b.feishu.update_calls == n
    asyncio.run(run())


# ── 飞书请求层重试 ──

class _Resp:
    def __init__(self, data): self._d = data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def json(self): return self._d


def test_transient_error_retries():
    async def run():
        api = FeishuAPI("id", "sec")
        api._token = "t"; api._token_expires = 9e18
        calls = {"n": 0}

        class S:
            def request(self, m, u, **k):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise aiohttp.ClientError("connection reset")
                return _Resp({"code": 0, "msg": "ok"})
            def post(self, *a, **k): return _Resp({"tenant_access_token": "t", "expire": 7200})
            @property
            def closed(self): return False
        api._session = S()
        r = await api._request("POST", "/x")
        assert r["code"] == 0 and calls["n"] == 3
    asyncio.run(run())


def test_token_invalid_refreshes_once():
    async def run():
        api = FeishuAPI("id", "sec")
        api._token = "old"; api._token_expires = 9e18
        calls = {"req": 0, "tok": 0}

        class S:
            def request(self, m, u, **k):
                calls["req"] += 1
                if calls["req"] == 1:
                    return _Resp({"code": 99991663, "msg": "token invalid"})
                return _Resp({"code": 0})
            def post(self, *a, **k):
                calls["tok"] += 1
                return _Resp({"tenant_access_token": "new", "expire": 7200})
            @property
            def closed(self): return False
        api._session = S()
        r = await api._request("POST", "/y")
        assert r["code"] == 0 and calls["req"] == 2 and calls["tok"] >= 1
    asyncio.run(run())


def test_rate_limit_code_not_retried():
    async def run():
        api = FeishuAPI("id", "sec")
        api._token = "t"; api._token_expires = 9e18
        calls = {"n": 0}

        class S:
            def request(self, m, u, **k):
                calls["n"] += 1
                return _Resp({"code": 230001, "msg": "rate limited"})
            def post(self, *a, **k): return _Resp({"tenant_access_token": "t", "expire": 7200})
            @property
            def closed(self): return False
        api._session = S()
        r = await api._request("POST", "/z")
        assert r["code"] == 230001 and calls["n"] == 1
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
