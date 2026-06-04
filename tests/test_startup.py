"""启动流程测试 — prepare_config（用户第一道关）+ config 解析/校验

覆盖之前 0% 的 app.py 启动前置逻辑和 config 入口。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.app import prepare_config
from pocket_agent.config import Config


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ── prepare_config：缺文件 ──

def test_missing_config_autogenerates_and_exits():
    """无 config 但有 example → 自动生成 + SystemExit 引导，不裸 traceback"""
    with tempfile.TemporaryDirectory() as d:
        # 在临时目录放一个 example，并把 cwd 切过去
        cfg = os.path.join(d, "config.json")
        # prepare_config 找的是包目录的 example，这里直接验证「缺文件时抛 SystemExit」
        old = os.getcwd()
        os.chdir(d)
        try:
            raised = False
            try:
                prepare_config(cfg)
            except SystemExit as e:
                raised = True
                assert "config" in str(e).lower() or "配置" in str(e)
            assert raised, "缺 config 应抛 SystemExit 而非 traceback"
        finally:
            os.chdir(old)


def test_invalid_json_friendly_exit():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as f:
            f.write("{ not valid json ")
        try:
            prepare_config(cfg)
            assert False, "应抛 SystemExit"
        except SystemExit as e:
            assert "JSON" in str(e) or "json" in str(e)


def test_placeholder_config_fails_validation():
    """占位符配置（example 原样）→ validate 失败，SystemExit 列出问题"""
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.json")
        _write(cfg, {
            "agent": "codex",
            "feishu_app_id": "cli_axxxxxxxxxxxx",   # 占位符
            "feishu_app_secret": "xxxxxxxxxxxxxxxxxxxxxxxx",
        })
        try:
            prepare_config(cfg)
            assert False, "占位符应校验失败"
        except SystemExit as e:
            assert "feishu_app_id" in str(e) or "凭证" in str(e) or "feishu" in str(e)


def test_valid_codex_config_passes():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.json")
        _write(cfg, {
            "agent": "codex",
            "feishu_app_id": "cli_real12345",
            "feishu_app_secret": "real_secret_value",
            "codex_ws_url": "ws://127.0.0.1:5123",
        })
        config = prepare_config(cfg)
        assert isinstance(config, Config)
        assert config.agent == "codex"


def test_invalid_agent_name_rejected():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.json")
        _write(cfg, {
            "agent": "gemini",
            "feishu_app_id": "cli_real12345",
            "feishu_app_secret": "real_secret_value",
        })
        try:
            prepare_config(cfg)
            assert False
        except SystemExit as e:
            assert "agent" in str(e) or "无效" in str(e)


# ── Config 本身 ──

def test_config_from_file_ignores_unknown_keys():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config.json")
        _write(cfg, {"agent": "claude", "unknown_future_key": 123})
        c = Config.from_file(cfg)
        assert c.agent == "claude"
        assert not hasattr(c, "unknown_future_key")


def test_config_validate_clean():
    c = Config(agent="codex", feishu_app_id="cli_x", feishu_app_secret="s")
    assert c.validate() == []


def test_config_allowed_users():
    c = Config(allowed_users="a, b ,")
    assert c.allowed_user_set == {"a", "b"}
    assert c.is_allowed("a") and not c.is_allowed("z")
    assert Config(allowed_users="").is_allowed("anyone")


def test_is_allowed_owner_branches():
    # 1) 所有者永远放行——即使白名单写错也锁不死自己
    c = Config(bot_owner="owner", allowed_users="someone_else")
    assert c.is_allowed("owner")
    assert not c.is_allowed("someone_else_random")
    # 2) 白名单优先：配了白名单按白名单（非所有者按名单判定）
    assert Config(bot_owner="owner", allowed_users="x").is_allowed("x")
    # 3) 私有默认 + 已知所有者 + 空白名单 → 仅所有者，其他人拒
    c = Config(bot_owner="owner")
    assert c.is_allowed("owner")
    assert not c.is_allowed("stranger")
    # 4) 兼容老配置：无所有者、空白名单 → 放行所有人
    assert Config().is_allowed("anyone")
    # private_by_default=False 时，即使有所有者、空白名单也放行
    assert Config(bot_owner="owner", private_by_default=False).is_allowed("anyone")


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
