"""飞书客户端 — API 调用 + WebSocket 事件接收

使用 lark-oapi SDK 的 WebSocket 长连接模式，无需公网 IP。
"""

import asyncio
import json
import logging
import time
from typing import Optional, Callable

import aiohttp

logger = logging.getLogger(__name__)


class FeishuAPI:
    """飞书 Open API HTTP 客户端"""

    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._token_expires: float = 0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _ensure_token(self, force: bool = False):
        if not force and self._token and time.time() < self._token_expires - 60:
            return
        session = await self._get_session()
        async with session.post(
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        ) as resp:
            data = await resp.json()
            self._token = data["tenant_access_token"]
            self._token_expires = time.time() + data.get("expire", 7200)
            logger.info("Feishu token refreshed")

    # 飞书业务错误码：token 失效（需刷新重试）vs 限流/参数（不重试）
    _TOKEN_INVALID_CODES = {99991663, 99991661, 99991664}  # tenant access token 相关

    async def _request(self, method: str, path: str, _retries: int = 3, **kwargs) -> dict:
        """带瞬时错误指数退避重试 + token 失效自动刷新重试。

        两层重试：网络瞬时错误（超时/连接重置）退避重试；
        token 失效码刷新后重试一次；限流等业务码不重试，直接返回交上层处理。
        """
        delay = 0.5
        last_exc: Optional[Exception] = None
        refreshed = False

        for attempt in range(_retries):
            try:
                await self._ensure_token()
                session = await self._get_session()
                headers = {"Authorization": f"Bearer {self._token}"}
                async with session.request(
                    method, f"{self.BASE}{path}", headers=headers, **kwargs
                ) as resp:
                    data = await resp.json()

                # token 失效：刷新一次再重试（只刷一次，避免死循环）
                if data.get("code") in self._TOKEN_INVALID_CODES and not refreshed:
                    logger.warning("Feishu token invalid (code=%s), refreshing", data.get("code"))
                    await self._ensure_token(force=True)
                    refreshed = True
                    continue
                return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # 瞬时网络错误：指数退避后重试
                last_exc = e
                if attempt < _retries - 1:
                    logger.warning("Feishu %s %s transient error: %s; retry in %.1fs",
                                   method, path, e, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
                    continue
                raise

        if last_exc:
            raise last_exc
        return {}

    async def send_text(self, receive_id: str, text: str,
                        receive_id_type: str = "chat_id") -> dict:
        # 默认 chat_id：bridge 一律按会话(chat_id)发送，含 P2P 私聊（其 chat_id
        # 形如 oc_…）。用 open_id 类型发 oc_ 值会因 id 类型不符而失败。
        return await self._request("POST", "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            })

    async def reply_text(self, message_id: str, text: str) -> dict:
        return await self._request("POST", f"/im/v1/messages/{message_id}/reply",
            json={"msg_type": "text", "content": json.dumps({"text": text})})

    async def reply_card(self, message_id: str, card: dict) -> dict:
        """以交互卡片回复一条消息（挂在原消息下，与 reply_text 同形态）。"""
        return await self._request("POST", f"/im/v1/messages/{message_id}/reply",
            json={"msg_type": "interactive", "content": json.dumps(card)})

    async def update_message(self, message_id: str, text: str) -> dict:
        return await self._request("PATCH", f"/im/v1/messages/{message_id}",
            json={"msg_type": "text", "content": json.dumps({"text": text})})

    async def update_card(self, message_id: str, card: dict) -> dict:
        """原地更新一条交互卡片消息（流式渲染用）"""
        return await self._request("PATCH", f"/im/v1/messages/{message_id}",
            json={"msg_type": "interactive", "content": json.dumps(card)})

    async def send_interactive(self, receive_id: str, card: dict,
                               receive_id_type: str = "chat_id") -> dict:
        # 默认 chat_id：理由同 send_text。卡片几乎都发到某个会话。
        return await self._request("POST", "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card),
            })

    async def get_app_owner(self) -> str:
        """查询本应用所有者的 open_id（最佳努力）。

        用于「零配置绑定」：填完凭证即可自动识别主人，无需用户发消息。
        需应用开通 application:application:self_manage 权限；未开通时飞书
        返回非 0 code，这里安静返回 "" 让调用方回退到「发消息绑定」。
        """
        try:
            data = await self._request(
                "GET", "/application/v6/applications/me",
                params={"user_id_type": "open_id", "lang": "zh_cn"})
        except Exception as e:
            logger.warning("get_app_owner failed: %s", e)
            return ""
        if data.get("code") == 0:
            owner = data.get("data", {}).get("app", {}).get("owner", {})
            return owner.get("owner_id", "") or ""
        logger.info("get_app_owner non-zero code=%s msg=%s (缺 self_manage 权限?)",
                    data.get("code"), data.get("msg"))
        return ""

    async def add_reaction(self, message_id: str, emoji_type: str) -> dict:
        return await self._request("POST", f"/im/v1/messages/{message_id}/reactions",
            json={"reaction_type": {"emoji_type": emoji_type}})

    # ── 附件：上传 + 发送（agent 产物回传给用户）──

    async def upload_image(self, data: bytes) -> str:
        """上传图片，返回 image_key。"""
        await self._ensure_token()
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("image_type", "message")
        form.add_field("image", data, filename="image.png",
                       content_type="application/octet-stream")
        async with session.post(
            f"{self.BASE}/im/v1/images",
            headers={"Authorization": f"Bearer {self._token}"},
            data=form,
        ) as resp:
            d = await resp.json()
            return d.get("data", {}).get("image_key", "")

    async def upload_file(self, data: bytes, file_name: str,
                          file_type: str = "stream") -> str:
        """上传文件，返回 file_key。file_type: opus/mp4/pdf/doc/xls/ppt/stream。"""
        await self._ensure_token()
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("file_type", file_type)
        form.add_field("file_name", file_name)
        form.add_field("file", data, filename=file_name,
                       content_type="application/octet-stream")
        async with session.post(
            f"{self.BASE}/im/v1/files",
            headers={"Authorization": f"Bearer {self._token}"},
            data=form,
        ) as resp:
            d = await resp.json()
            return d.get("data", {}).get("file_key", "")

    async def send_image(self, receive_id: str, image_key: str,
                         receive_id_type: str = "open_id") -> dict:
        return await self._request("POST", "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            })

    async def send_file(self, receive_id: str, file_key: str,
                        receive_id_type: str = "open_id") -> dict:
        return await self._request("POST", "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
            })

    async def close(self):
        if self._session:
            await self._session.close()


class FeishuEventClient:
    """
    飞书 WebSocket 事件客户端。

    使用 lark-oapi SDK 的长连接模式接收事件，无需公网 IP。

    在飞书开放平台配置：
    1. 事件与回调 → 订阅方式 → 「使用长连接接收事件」
    2. 添加事件：im.message.receive_v1
    3. 添加回调：card.action.trigger
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._on_message: Optional[Callable] = None
        self._on_card_action: Optional[Callable] = None
        # bridge 的事件循环（运行在另一个线程）。SDK 回调通过它跨线程调度协程。
        self._loop: Optional["asyncio.AbstractEventLoop"] = None

    def set_loop(self, loop):
        """注入 bridge 所在线程的事件循环（跨线程调度协程时需要）"""
        self._loop = loop

    def on_message(self, handler: Callable):
        """注册消息事件处理器"""
        self._on_message = handler

    def on_card_action(self, handler: Callable):
        """注册卡片回调处理器"""
        self._on_card_action = handler

    def start(self):
        """启动 WebSocket 长连接（同步阻塞，自带自动重连）。

        注意：内部全是 lark SDK 的同步阻塞调用（cli.start()），没有 await，
        因此是普通同步方法。app.py 在主线程同步调用它阻塞运行。
        """
        try:
            import lark_oapi as lark
        except ImportError:
            raise ImportError(
                "需要安装 lark-oapi: pip install lark-oapi\n"
                "飞书 SDK 提供 WebSocket 长连接支持"
            )

        # 构建事件处理器（消息 + 卡片回调都走 EventDispatcher）
        builder = lark.EventDispatcherHandler.builder(
            "", ""  # encrypt_key, verification_token（长连接模式不需要）
        ).register_p2_im_message_receive_v1(self._handle_message)

        # 卡片回调也注册到 EventDispatcher
        # WebSocket 模式下不支持单独的 CardActionHandler
        if self._on_card_action:
            builder = builder.register_p2_card_action_trigger(self._handle_card_action)

        event_handler = builder.build()

        # 创建 WebSocket 客户端
        cli = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("Starting Feishu WebSocket connection...")
        # 阻塞运行（自动重连）
        cli.start()

    def _handle_message(self, data):
        """处理飞书消息事件（SDK 回调，运行在 SDK 线程）"""
        if not self._on_message:
            return
        if self._loop is None:
            logger.error("Event loop not set; dropping message event")
            return
        # 跨线程把协程投递到 bridge 的事件循环；消息处理 fire-and-forget
        future = asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

        def _log_exc(fut):
            exc = fut.exception()
            if exc:
                logger.error("Message handler error: %s", exc, exc_info=exc)

        future.add_done_callback(_log_exc)

    def _handle_card_action(self, data):
        """处理卡片回调（SDK 线程；SDK 需要同步拿到返回值）"""
        if not self._on_card_action:
            return
        if self._loop is None:
            logger.error("Event loop not set; dropping card action")
            return
        future = asyncio.run_coroutine_threadsafe(self._on_card_action(data), self._loop)
        try:
            # 阻塞等待协程结果，把响应（toast）同步交回 SDK
            return future.result(timeout=10)
        except Exception as e:
            logger.error("Card action handler error: %s", e, exc_info=True)
            return None
