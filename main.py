#!/usr/bin/env python3
"""Pocket Agent — 入口

用法：
  python main.py          # 启动桥接
  python main.py setup    # 交互式配置向导
"""

import sys
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

_AGENTS = ("claude", "opencode", "codex")


def _open_link(url: str):
    """打印一个（多数终端可点击的）链接"""
    print(f"  🔗 {url}")


def cmd_setup():
    """交互式配置向导：建应用引导 → 填凭证(当场验证) → 选 agent → 扫脸绑定 → 保存启动"""
    print("\n🤖 Pocket Agent — 配置向导\n")

    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        print(f"  找到已有配置：{CONFIG_PATH}（回车保留原值）\n")

    # ── 第 1 步：创建飞书应用（平台要求人工创建，无法跳过，这里讲到最细）──
    print("━" * 54)
    print("① 创建飞书应用（约 2 分钟，只需一次）")
    print("━" * 54)
    print("  在飞书开放平台按下面 6 步操作：")
    print()
    print("  1) 打开开放平台 → 创建企业自建应用：")
    _open_link("https://open.feishu.cn/app")
    print("  2) 应用能力 → 添加「机器人」")
    print("  3) 权限管理 → 开通这两个权限：")
    print("       im:message.p2p_msg:readonly   （读私聊消息）")
    print("       im:message:send_as_bot        （发消息）")
    print("  4) 事件与回调 → 订阅方式 → 选「使用长连接接收事件」")
    print("  5) 事件与回调 → 添加事件 im.message.receive_v1；")
    print("                 添加回调 card.action.trigger")
    print("  6) 版本管理与发布 → 创建版本并发布（企业内可用即可）")
    print()
    print("  完成后在「凭证与基础信息」页可看到 App ID / App Secret。")
    print()

    # ── 第 2 步：填凭证 + 当场验证 ──
    print("━" * 54)
    print("② 填入凭证（当场验证有效性）")
    print("━" * 54)
    while True:
        app_id = input(f"  App ID [{config.get('feishu_app_id', '')}]: ").strip()
        if app_id:
            config['feishu_app_id'] = app_id
        if not config.get('feishu_app_id'):
            print("  ❌ App ID 不能为空"); continue

        secret_shown = (config.get('feishu_app_secret', '')[:6] + "…") if config.get('feishu_app_secret') else ""
        app_secret = input(f"  App Secret [{secret_shown}]: ").strip()
        if app_secret:
            config['feishu_app_secret'] = app_secret
        if not config.get('feishu_app_secret'):
            print("  ❌ App Secret 不能为空"); continue

        print("\n  正在验证凭证…")
        ok, msg = _verify_feishu(config['feishu_app_id'], config['feishu_app_secret'])
        print(f"  {'✅' if ok else '❌'} {msg}")
        if ok:
            break
        retry = input("  凭证有误，重新输入？(Y/n): ").strip().lower()
        if retry == 'n':
            break

    # ── 第 3 步：选 agent ──
    print()
    print("━" * 54)
    print("③ 选择 agent")
    print("━" * 54)
    cur = config.get('agent', 'claude')
    print(f"  可选：{' / '.join(_AGENTS)}")
    agent = input(f"  使用哪个 agent？[{cur}]: ").strip().lower()
    config['agent'] = agent if agent in _AGENTS else cur
    print(f"  → {config['agent']}")

    if config['agent'] in ('claude', 'opencode'):
        wd = input(f"  工作目录 workdir [{config.get('workdir', '.')}]: ").strip()
        config['workdir'] = wd or config.get('workdir', '.')
        if config['agent'] == 'opencode':
            m = input(f"  opencode 模型 provider/model [{config.get('opencode_model', '')}]: ").strip()
            if m:
                config['opencode_model'] = m
    elif config['agent'] == 'codex':
        ws = input(f"  Codex WebSocket URL [{config.get('codex_ws_url', 'ws://127.0.0.1:5123')}]: ").strip()
        config['codex_ws_url'] = ws or config.get('codex_ws_url', 'ws://127.0.0.1:5123')

    # ── 第 4 步：绑定使用者（免查 open_id）──
    print()
    print("━" * 54)
    print("④ 绑定使用者（可选，限制只有你能用）")
    print("━" * 54)
    if config.get('feishu_app_id') and config.get('feishu_app_secret'):
        print("  无需手动查 open_id：启动后在飞书给机器人发任意一条消息，")
        print("  程序会自动把你识别为主人并写入白名单。")
        do_bind = input("  现在就用「发消息绑定」？(Y/n): ").strip().lower()
        if do_bind != 'n':
            oid = _capture_open_id(config['feishu_app_id'], config['feishu_app_secret'])
            if oid:
                config['allowed_users'] = oid
                print(f"  ✅ 已绑定：{oid}")
            else:
                print("  ⏭ 跳过绑定（allowed_users 留空 = 不限制，谁都能用）")
    else:
        print("  （凭证未就绪，跳过）")

    # ── 保存 ──
    print()
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print(f"  💾 配置已保存到 {CONFIG_PATH}")

    # ── 启动 ──
    print()
    start = input("  现在启动？(Y/n): ").strip().lower()
    if start != 'n':
        from pocket_agent.app import run
        run(str(CONFIG_PATH))


def _verify_feishu(app_id: str, app_secret: str) -> tuple[bool, str]:
    """用 app_id/secret 换 tenant_access_token，验证凭证是否有效。"""
    import asyncio

    async def _check():
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                ) as resp:
                    data = await resp.json()
        except Exception as e:
            return False, f"网络错误：{e}"
        if data.get("tenant_access_token"):
            return True, "凭证有效，飞书连接成功"
        # 飞书会返回具体错误码，原样透出便于排查
        return False, f"凭证无效：code={data.get('code')} {data.get('msg', '')}"

    return asyncio.run(_check())


def _capture_open_id(app_id: str, app_secret: str, timeout: int = 120) -> str:
    """启动飞书长连接，等用户发来第一条消息，抓取其 open_id 后返回。

    这样用户无需自己去查 open_id —— 在飞书里发条消息即完成绑定。
    """
    print("\n  ⏳ 正在连接飞书…请在飞书里给机器人发任意一条消息（最多等 2 分钟）")
    try:
        import lark_oapi as lark
    except ImportError:
        print("  ⚠️  未安装 lark-oapi，无法自动绑定。请先 `uv sync`。")
        return ""

    import threading
    captured = {"open_id": ""}
    done = threading.Event()

    def on_msg(data):
        try:
            oid = data.event.sender.sender_id.open_id
            if oid:
                captured["open_id"] = oid
                done.set()
        except Exception:
            pass

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_msg)
               .build())
    cli = lark.ws.Client(app_id, app_secret, event_handler=handler,
                         log_level=lark.LogLevel.ERROR)

    t = threading.Thread(target=cli.start, daemon=True)
    t.start()
    done.wait(timeout=timeout)
    return captured["open_id"]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        cmd_setup()
    else:
        config_path = sys.argv[1] if len(sys.argv) > 1 else str(CONFIG_PATH)
        from pocket_agent.app import run
        run(config_path)


if __name__ == "__main__":
    main()
