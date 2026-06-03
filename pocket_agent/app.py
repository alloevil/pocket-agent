"""应用入口"""

import asyncio
import logging
import os
import shutil
import sys

from .config import Config
from .bridge import Bridge
from .feishu_client import FeishuEventClient

logger = logging.getLogger(__name__)


def prepare_config(config_path: str = "config.json") -> Config:
    """准备并校验配置；返回可用的 Config，否则抛 SystemExit 附友好提示。

    这是用户的第一道关，单独抽出便于测试，且确保任何失败都给「下一步怎么做」
    而不是裸 traceback。
    """
    # 1) 配置文件不存在：尝试从模板生成，并提示用户去填，然后退出
    if not os.path.isfile(config_path):
        example = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "config.example.json")
        if os.path.isfile(example) and not os.path.exists(config_path):
            try:
                shutil.copy(example, config_path)
                raise SystemExit(
                    f"✅ 已为你生成配置文件：{config_path}\n"
                    f"请填入飞书凭证（feishu_app_id / feishu_app_secret）并选择 agent，\n"
                    f"然后重新运行 `python main.py`；\n"
                    f"或运行 `python main.py setup` 用交互式向导填写。")
            except OSError as e:
                raise SystemExit(f"配置文件 {config_path} 不存在，且自动生成失败：{e}")
        raise SystemExit(
            f"配置文件不存在：{config_path}\n"
            f"请先 `cp config.example.json config.json` 并填写，"
            f"或运行 `python main.py setup`。")

    # 2) 解析（JSON 语法错误也给友好提示）
    try:
        config = Config.from_file(config_path)
    except ValueError as e:    # JSONDecodeError 是 ValueError 子类
        raise SystemExit(f"配置文件 {config_path} 不是合法 JSON：{e}")

    # 3) 字段校验：缺凭证 / agent 无效 / CLI 缺失 等
    problems = config.validate()
    if problems:
        raise SystemExit(
            "配置有误，请修正后重试：\n  - " + "\n  - ".join(problems)
            + "\n（或运行 `python main.py setup` 重新配置）")

    return config


def run(config_path: str = "config.json"):
    """启动桥接服务"""
    config = prepare_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bridge = Bridge(config)
    event_client = FeishuEventClient(config.feishu_app_id, config.feishu_app_secret)

    # 注册事件处理器
    event_client.on_message(bridge.on_feishu_message)
    event_client.on_card_action(bridge.on_card_action)

    logger.info("🤖 飞书 Agent 遥控器 — agent=%s, workdir=%s",
                config.agent, config.workdir)

    # lark-oapi SDK 的 WebSocket 客户端是同步阻塞的，且在自己的线程上回调；
    # bridge 的异步部分跑在单独的事件循环线程里。两者通过 run_coroutine_threadsafe
    # 跨线程通信，因此这里必须把 bridge 的 loop 引用交给 event_client。
    import threading

    loop_ready = threading.Event()
    start_error: list = []

    async def _start():
        event_client.set_loop(asyncio.get_running_loop())
        try:
            await bridge.start()
        except Exception as e:
            start_error.append(e)
            loop_ready.set()
            return
        loop_ready.set()
        logger.info("Bridge ready. Waiting for Feishu events...")
        # 保持运行
        await asyncio.Event().wait()

    def run_async():
        asyncio.run(_start())

    # 先启动 bridge（连接 agent 后端），等其 loop 就绪后再接入飞书事件
    bridge_thread = threading.Thread(target=run_async, daemon=True)
    bridge_thread.start()
    loop_ready.wait()

    if start_error:
        logger.error("后端启动失败: %s", start_error[0])
        raise start_error[0]

    # 启动飞书 WebSocket（阻塞主线程）
    logger.info("Starting Feishu WebSocket...")
    event_client.start()


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    run(config_file)
