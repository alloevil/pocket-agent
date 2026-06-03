#!/usr/bin/env python3
"""飞书 Codex 遥控器 — 入口

用法：
  python main.py          # 启动桥接
  python main.py setup    # 交互式配置
"""

import sys
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def cmd_setup():
    """交互式配置向导"""
    print("\n🤖 飞书 Codex 遥控器 — 配置向导\n")

    # 加载已有配置
    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        print(f"  找到已有配置: {CONFIG_PATH}\n")

    # ── 飞书应用配置 ──
    print("━" * 50)
    print("📱 飞书应用配置")
    print("━" * 50)
    print()
    print("  请在飞书开放平台创建应用：")
    print("  1. 打开 https://open.feishu.cn/app")
    print("  2. 点击「创建企业自建应用」")
    print("  3. 启用机器人能力")
    print("  4. 添加权限：im:message.p2p_msg:readonly, im:message:send_as_bot")
    print("  5. 事件与回调 → 使用长连接接收事件")
    print("  6. 添加事件：im.message.receive_v1")
    print("  7. 添加回调：card.action.trigger")
    print("  8. 发布应用")
    print()

    # 尝试生成二维码
    try:
        import qrcode
        url = "https://open.feishu.cn/app"
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print("  扫码打开飞书开放平台：")
        qr.print_ascii(invert=True)
        print()
    except ImportError:
        print("  （安装 qrcode 可在终端显示二维码：pip install qrcode）")
        print(f"  或直接访问：https://open.feishu.cn/app")
        print()

    app_id = input(f"  App ID [{config.get('feishu_app_id', '')}]: ").strip()
    if app_id:
        config['feishu_app_id'] = app_id
    elif not config.get('feishu_app_id'):
        print("  ❌ App ID 不能为空")
        return

    app_secret = input(f"  App Secret [{config.get('feishu_app_secret', '')[:8]}...]: ").strip()
    if app_secret:
        config['feishu_app_secret'] = app_secret
    elif not config.get('feishu_app_secret'):
        print("  ❌ App Secret 不能为空")
        return

    # ── Codex 配置 ──
    print()
    print("━" * 50)
    print("⚙️  Codex 配置")
    print("━" * 50)
    print()
    print("  确保 Codex remote-control 已启动：")
    print("  $ codex remote-control")
    print()

    ws_url = input(f"  Codex WebSocket URL [{config.get('codex_ws_url', 'ws://127.0.0.1:5123')}]: ").strip()
    if ws_url:
        config['codex_ws_url'] = ws_url
    elif not config.get('codex_ws_url'):
        config['codex_ws_url'] = 'ws://127.0.0.1:5123'

    # ── 可选配置 ──
    print()
    allowed = input(f"  限制用户（open_id，逗号分隔，留空=不限）[{config.get('allowed_users', '')}]: ").strip()
    if allowed:
        config['allowed_users'] = allowed

    # ── 保存 ──
    print()
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print(f"  ✅ 配置已保存到 {CONFIG_PATH}")

    # ── 测试连接 ──
    print()
    test = input("  测试连接？(Y/n): ").strip().lower()
    if test != 'n':
        _test_connection(config)

    # ── 启动 ──
    print()
    start = input("  现在启动？(Y/n): ").strip().lower()
    if start != 'n':
        from pocket_agent.app import run
        run(str(CONFIG_PATH))


def _test_connection(config: dict):
    """测试飞书和 Codex 连接"""
    import asyncio

    # 测试飞书
    print("\n  测试飞书连接...")
    try:
        import aiohttp

        async def test_feishu():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": config['feishu_app_id'],
                        "app_secret": config['feishu_app_secret'],
                    },
                ) as resp:
                    data = await resp.json()
                    if data.get("tenant_access_token"):
                        print(f"  ✅ 飞书连接成功 (token: {data['tenant_access_token'][:16]}...)")
                    else:
                        print(f"  ❌ 飞书连接失败: {data}")

        asyncio.run(test_feishu())
    except Exception as e:
        print(f"  ❌ 飞书连接失败: {e}")

    # 测试 Codex
    print("\n  测试 Codex 连接...")
    try:
        import websockets

        async def test_codex():
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(config['codex_ws_url']),
                    timeout=3
                )
                await ws.close()
                print(f"  ✅ Codex 连接成功 ({config['codex_ws_url']})")
            except asyncio.TimeoutError:
                print(f"  ⚠️  Codex 连接超时 — 请确认 codex remote-control 已启动")
            except ConnectionRefusedError:
                print(f"  ⚠️  Codex 连接被拒 — 请确认 codex remote-control 已启动")
            except Exception as e:
                print(f"  ⚠️  Codex 连接失败: {e}")

        asyncio.run(test_codex())
    except Exception as e:
        print(f"  ❌ Codex 连接测试失败: {e}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        cmd_setup()
    else:
        config_path = sys.argv[1] if len(sys.argv) > 1 else str(CONFIG_PATH)
        from pocket_agent.app import run
        run(config_path)


if __name__ == "__main__":
    main()
