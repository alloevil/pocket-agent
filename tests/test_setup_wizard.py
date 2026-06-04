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
    def request(self, *a, **k): return _Resp(self._d)
    @property
    def closed(self): return False
    async def close(self): pass


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


def test_feishu_config_constants():
    # 桥接核心实际用到的权限/事件不能漏
    assert "im:message.p2p_msg:readonly" in main._CORE_SCOPES
    assert "im:message:send_as_bot" in main._CORE_SCOPES
    assert "application:application:self_manage" in main._OPTIONAL_SCOPES
    assert main._REQUIRED_EVENTS == ["im.message.receive_v1"]
    assert main._REQUIRED_CALLBACKS == ["card.action.trigger"]


def test_scopes_import_json():
    import json as _json
    obj = _json.loads(main._scopes_import_json())
    # 结构符合飞书批量导入格式
    assert set(obj.keys()) == {"scopes"}
    assert set(obj["scopes"].keys()) == {"tenant", "user"}
    tenant = obj["scopes"]["tenant"]
    # 完整集足够大（不是只有那几个核心权限）
    assert len(tenant) > 50
    # 防漂移：核心 + 可选权限必须都在 tenant 完整集里
    for s in main._CORE_SCOPES + main._OPTIONAL_SCOPES:
        assert s in tenant, f"核心权限 {s} 不在完整导入清单中"


# ── 自动解析应用所有者（零配置绑定）──
# 注：_request 会先 post 取 token，再 request 业务接口；共享 mock 对两次返回同一 data，
# 故测试 payload 同时含 tenant_access_token 与 owner 字段。

def test_resolve_owner_success():
    data = {
        "tenant_access_token": "t-xxx", "expire": 7200,
        "code": 0,
        "data": {"app": {"owner": {"owner_id": "ou_owner123"}}},
    }
    with _patch_aiohttp(data):
        oid = main._resolve_owner("cli_x", "secret")
    assert oid == "ou_owner123"


def test_resolve_owner_no_permission():
    # 未开 self_manage：飞书返回非 0 code，应安静返回 ""
    data = {"tenant_access_token": "t-xxx", "expire": 7200,
            "code": 99991672, "msg": "permission denied"}
    with _patch_aiohttp(data):
        oid = main._resolve_owner("cli_x", "secret")
    assert oid == ""


def test_resolve_owner_missing_field():
    # code=0 但响应里没有 owner_id → ""
    data = {"tenant_access_token": "t-xxx", "expire": 7200,
            "code": 0, "data": {"app": {}}}
    with _patch_aiohttp(data):
        oid = main._resolve_owner("cli_x", "secret")
    assert oid == ""


# ── 粘贴凭证自动解析 ──

def test_parse_credentials_labeled():
    text = "App ID  cli_a1b2c3d4e5f6\nApp Secret  Xy9zAbCdEfGh1234 Klm"
    pid, psec = main._parse_credentials(text)
    assert pid == "cli_a1b2c3d4e5f6"
    assert psec == "Xy9zAbCdEfGh1234"


def test_parse_credentials_chinese_label():
    text = "应用 ID：cli_zzz111aaa222\n密钥: SECRETvalue9876543210"
    pid, psec = main._parse_credentials(text)
    assert pid == "cli_zzz111aaa222"
    assert psec == "SECRETvalue9876543210"


def test_parse_credentials_bare_two_tokens():
    # 无标签，裸两段：cli_ 锁 id，另一长串当 secret
    text = "cli_abc123def456  qwerASDF1234zxcv7890"
    pid, psec = main._parse_credentials(text)
    assert pid == "cli_abc123def456"
    assert psec == "qwerASDF1234zxcv7890"


def test_parse_credentials_id_only():
    pid, psec = main._parse_credentials("cli_onlyid12345678")
    assert pid == "cli_onlyid12345678"
    assert psec == ""


def test_parse_credentials_secret_not_mistaken_as_id():
    # secret 标签后若是 app_id 形态，不能误当 secret；真正的 id 仍要锁对
    text = "App Secret: cli_should_not_win\nApp ID cli_realid9999"
    pid, psec = main._parse_credentials(text)
    assert pid == "cli_realid9999"
    assert psec != pid


# ── 已有配置的快捷路径（跳过建应用/填凭证）──

def _run_setup_with(inputs, cfg, verify_ok=True):
    """驱动 cmd_setup：mock 掉文件/验证/绑定，喂入 inputs，返回调用统计。"""
    import builtins
    calls = {"step12": 0, "verify": 0, "run": 0}

    def fake_step12(c): calls["step12"] += 1
    def fake_verify(a, b):
        calls["verify"] += 1
        return (verify_ok, "凭证有效，飞书连接成功" if verify_ok else "凭证无效：code=10003")
    def fake_run(p): calls["run"] += 1

    it = iter(inputs)
    with mock.patch.object(main, "CONFIG_PATH") as P, \
         mock.patch.object(main, "_step_app_and_credentials", fake_step12), \
         mock.patch.object(main, "_verify_feishu", fake_verify), \
         mock.patch.object(main, "_resolve_owner", lambda a, b: ""), \
         mock.patch.object(builtins, "input", lambda *a, **k: next(it)), \
         mock.patch("json.dump"), \
         mock.patch("json.load", return_value=cfg), \
         mock.patch("builtins.open", mock.mock_open(read_data="{}")):
        P.exists.return_value = True
        # cmd_setup 内部 `from pocket_agent.app import run`
        with mock.patch("pocket_agent.app.run", fake_run):
            try:
                main.cmd_setup()
            except StopIteration:
                pass
    return calls


def _has_creds_cfg():
    return {"feishu_app_id": "cli_existing", "feishu_app_secret": "sec",
            "agent": "claude", "workdir": "."}


def test_setup_existing_creds_quick_start():
    # 路径1：已配且验证通过 → 直接启动，既不重走①②也不进③
    calls = _run_setup_with(["1"], _has_creds_cfg())
    assert calls["verify"] == 1
    assert calls["step12"] == 0
    assert calls["run"] == 1, "选1 应直接启动"


def test_setup_existing_creds_skip_to_agent():
    # 路径2：跳过建应用/填凭证，只调 agent 与绑定
    calls = _run_setup_with(
        ["2", "claude", ".", "n", "n"], _has_creds_cfg())
    assert calls["verify"] == 1
    assert calls["step12"] == 0, "选2 必须跳过①②建应用/填凭证"
    assert calls["run"] == 0, "末尾选 n 不启动"


def test_setup_existing_creds_full_reconfig():
    # 路径3：重新完整配置 → 仍会走①②
    calls = _run_setup_with(
        ["3", "claude", ".", "n", "n"], _has_creds_cfg())
    assert calls["step12"] == 1, "选3 应重走①②"


def test_setup_invalid_existing_creds_forces_reconfig():
    # 已有凭证但验证失败 → 不给快捷菜单，强制走①②重配
    calls = _run_setup_with(
        ["claude", ".", "n", "n"], _has_creds_cfg(), verify_ok=False)
    assert calls["verify"] == 1
    assert calls["step12"] == 1, "凭证失效应强制重配①②"


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
