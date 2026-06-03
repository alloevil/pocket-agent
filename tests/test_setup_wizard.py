"""配置向导测试 — 凭证验证的解析与分支（不依赖真实网络）"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


class _Resp:
    def __init__(self, data):
        self._d = data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def json(self): return self._d


class _Session:
    def __init__(self, data):
        self._d = data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    def post(self, *a, **k): return _Resp(self._d)


def _patch_aiohttp(data):
    """让 aiohttp.ClientSession() 返回固定响应"""
    return mock.patch("aiohttp.ClientSession", lambda *a, **k: _Session(data))


def test_verify_feishu_valid():
    with _patch_aiohttp({"tenant_access_token": "t-xxx", "expire": 7200}):
        ok, msg = main._verify_feishu("cli_real", "secret")
    assert ok is True
    assert "成功" in msg


def test_verify_feishu_invalid_credentials():
    with _patch_aiohttp({"code": 10003, "msg": "invalid param"}):
        ok, msg = main._verify_feishu("cli_bad", "bad")
    assert ok is False
    assert "10003" in msg or "无效" in msg


def test_verify_feishu_network_error():
    def boom(*a, **k):
        raise OSError("connection refused")
    with mock.patch("aiohttp.ClientSession", boom):
        ok, msg = main._verify_feishu("cli", "s")
    assert ok is False
    assert "网络" in msg or "错误" in msg


def test_agents_constant():
    # 向导支持的 agent 与后端一致
    assert set(main._AGENTS) == {"claude", "opencode", "codex"}


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
