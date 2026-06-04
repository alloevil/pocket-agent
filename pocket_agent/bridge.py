"""桥接核心 — 飞书 ↔ Agent 后端消息路由

与具体 agent 无关：通过 AgentBackend 抽象驱动 Codex/Claude/opencode，
消费统一的 AgentEvent 流，用 CardRenderer 渲染成飞书交互卡片。
"""

import asyncio
import json
import logging
import os
import time

from .config import Config
from .events import AgentEvent, AgentSession, EventKind
from .feishu_client import FeishuAPI
from .backends import create_backend, SUPPORTED_BACKENDS
from .renderer_base import Renderer
from .renderer import info_card
from .renderer import CardRenderer
from .session_store import SessionStore

logger = logging.getLogger(__name__)


class Bridge:
    """飞书 ↔ Agent 桥接器"""

    def __init__(self, config: Config):
        self.config = config
        self.feishu = FeishuAPI(config.feishu_app_id, config.feishu_app_secret)
        self.agent_name = config.agent
        self.backend = create_backend(self.agent_name, config)
        # 依赖抽象 Renderer 接口，不绑死飞书实现（与具体平台解耦）
        self.renderer: Renderer = CardRenderer(
            self.agent_name,
            show_thinking=config.show_thinking,
            max_body_len=config.max_message_length,
        )
        # 会话存储：按 (user,chat) 分组 + 组内多会话 + 持久化
        self.store = SessionStore(persist_path=config.persist_path)
        self.store.load()
        self._welcomed: set = set()    # 已发过欢迎引导的 (user,chat)
        self._heartbeats: dict = {}    # session_id -> 心跳 asyncio.Task
        self._event_task = None
        self._owner_task = None
        # /loop：session_id -> turn 完成事件（TURN_DONE/ERROR 时 set）
        self._turn_done_events: dict = {}

    # 所有者刷新间隔（秒）：飞书后台转移应用所有权后 bot 自动跟随
    OWNER_REFRESH_SECONDS = 30 * 60

    async def start(self):
        logger.info("Starting backend: %s", self.agent_name)
        await self.backend.start()
        logger.info("Backend %s ready", self.agent_name)
        self._event_task = asyncio.create_task(self._consume_events())
        self._owner_task = asyncio.create_task(self._refresh_owner_loop())

    async def _refresh_owner_loop(self):
        """周期性解析应用所有者写入 config.bot_owner（最佳努力，失败只 warn）。

        启动即刷新一次：向导未绑定成功也能在运行时补上；之后每 30 分钟一次，
        使飞书后台转移所有权后 bot 自动跟随主人（永远锁不死自己）。
        """
        while True:
            try:
                oid = await self.feishu.get_app_owner()
                if oid and oid != self.config.bot_owner:
                    self.config.bot_owner = oid
                    logger.info("Bot owner resolved: %s", oid)
            except Exception as e:
                logger.warning("owner refresh failed: %s", e)
            await asyncio.sleep(self.OWNER_REFRESH_SECONDS)

    async def shutdown(self):
        if self._event_task:
            self._event_task.cancel()
        if self._owner_task:
            self._owner_task.cancel()
        for task in list(self._heartbeats.values()):
            task.cancel()
        self.store.save()
        await self.backend.close()
        await self.feishu.close()

    # ── session 查找 ──

    def _session_by_native(self, native_id: str) -> AgentSession | None:
        return self.store.by_native(native_id)

    def _get_session(self, user_id: str, chat_id: str = "c") -> AgentSession | None:
        """便捷查找：返回该用户某 chat 的活跃会话（测试/单 chat 场景用）。

        默认 chat_id="c" 仅为兼容单聊/测试；多 chat 场景请直接用 store.active。
        """
        s = self.store.active(user_id, chat_id)
        if s:
            return s
        # 兜底：该用户任意 chat 的活跃会话
        for sess in self.store.all_sessions():
            if sess.user_id == user_id:
                return sess
        return None

    # ── 飞书消息入口 ──

    async def on_feishu_message(self, data):
        try:
            event = data.event
            message = event.message
            sender = event.sender

            chat_id = message.chat_id
            message_id = message.message_id
            sender_id = sender.sender_id.open_id
            logger.info("收到消息 sender=%s chat=%s type=%s",
                        sender_id, chat_id, message.message_type)

            if not self.config.is_allowed(sender_id):
                # 私有默认：对非授权用户静默忽略，不回复——
                # 回「无权限」只会向陌生人确认 bot 的存在，反而暴露。
                logger.info("ignored message from non-allowed user %s", sender_id)
                return

            # 非文本消息不再静默：明确告知只支持文字，避免用户以为机器人坏了
            if message.message_type != "text":
                hint = {
                    "audio": "🎤 暂不支持语音，请发文字指令",
                    "image": "🖼 暂不支持图片，请发文字指令",
                    "file": "📎 暂不支持文件，请发文字指令",
                    "post": "暂不支持富文本，请发纯文字指令",
                }.get(message.message_type, "暂只支持文字消息，请发文字指令")
                await self.feishu.reply_text(message_id, hint)
                return

            content = json.loads(message.content)
            text = content.get("text", "").strip()
            text = self._strip_mentions(text, getattr(message, "mentions", None))
            if not text:
                return

            # 首次：该 (用户,chat) 从未交互过 → 先发欢迎引导（仅一次）
            wkey = (sender_id, chat_id)
            if wkey not in self._welcomed and text != "/help":
                self._welcomed.add(wkey)
                if not self.store.list_sessions(sender_id, chat_id):
                    await self._send_welcome(chat_id)

            if text.startswith("/"):
                await self._handle_command(sender_id, chat_id, message_id, text)
            else:
                await self._forward(sender_id, chat_id, message_id, text)

        except Exception as e:
            logger.error("Message handler error: %s", e, exc_info=True)

    @staticmethod
    def _strip_mentions(text: str, mentions) -> str:
        """剥离群聊 @ 占位符（如 @_user_1），只把干净正文交给 agent。

        飞书群里 @机器人时，text 含 `@_user_1` 之类占位符，mentions 数组给出
        占位符 key → 名字的映射。若不剥离，agent 会收到 "@_user_1 帮我看下" 这种噪声。
        """
        if mentions:
            for m in mentions:
                key = getattr(m, "key", None)
                if key:
                    text = text.replace(key, "")
        # 兜底：清掉残留的 @_user_N / @_all 占位符
        import re as _re
        text = _re.sub(r"@_(user_\d+|all)\b", "", text)
        return text.strip()

    async def on_card_action(self, data) -> dict:
        """处理飞书审批卡片按钮回调"""
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
                CallBackToast,
            )

            def _toast(toast_type: str, content: str):
                resp = P2CardActionTriggerResponse()
                resp.toast = CallBackToast({"type": toast_type, "content": content})
                return resp
        except ImportError:
            # lark 未安装（如测试环境）：toast 退化为普通 dict，不影响动作逻辑
            def _toast(toast_type: str, content: str):
                return {"toast": {"type": toast_type, "content": content}}

        try:
            value = data.event.action.value or {}
            act = value.get("action", "")
            approval_id = value.get("key", "")
            user_id = value.get("uid", "")

            # 重试按钮：用上一条 prompt 在该会话重发
            if act == "retry":
                session = self.store.get(user_id, value.get("cid", ""), value.get("sid", ""))
                if session and session.last_prompt and not session.turn_active:
                    self.store.switch(user_id, session.chat_id, session.session_id)
                    await self._forward(user_id, session.chat_id, "", session.last_prompt)
                    return _toast("success", "已重试")
                return _toast("error", "无法重试（任务进行中或无记录）")

            session = self.store.active(user_id, value.get("cid", ""))
            if not session or approval_id not in session.pending_approvals:
                return _toast("error", "审批已过期")

            # 本轮全部允许：开关打开 + 批准当前这个
            if act == "approve_all":
                session.auto_approve_turn = True
                await self.backend.approve(session, approval_id, True)
                session.pending_approvals.pop(approval_id, None)
                return _toast("success", "本轮后续将自动允许")

            approved = act == "approve"
            await self.backend.approve(session, approval_id, approved)
            kind = session.pending_approvals.get(approval_id, {}).get("kind", "")
            session.pending_approvals.pop(approval_id, None)
            text = ("已批准" if approved else "已拒绝") + ("修改" if kind == "file" else "")
            return _toast("success", text)

        except Exception as e:
            logger.error("Card action error: %s", e, exc_info=True)
            return _toast("error", f"失败: {e}")

    # ── 命令 ──

    async def _handle_command(self, user_id, chat_id, message_id, cmd):
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/new":
            session = self.store.active(user_id, chat_id)
            # 中断进行中的 turn，但保留旧会话在列表里（/list 可见、/switch 可回）
            if session and session.turn_active:
                try:
                    await self.backend.interrupt(session)
                    session.turn_active = False
                except Exception:
                    pass
            # 新建一个空会话并设为活跃；旧会话保留
            self.store.create(user_id, chat_id)
            await self.feishu.reply_card(message_id, info_card(
                "✅ 新对话已创建", "旧会话仍保留，可用 `/list` 查看、`/switch` 切回。",
                template="green"))

        elif command == "/list":
            sessions = self.store.list_sessions(user_id, chat_id)
            if not sessions:
                await self.feishu.reply_card(message_id, info_card(
                    "🗂 会话列表", "暂无会话，直接发消息即可开始。", template="blue"))
                return
            active = self.store.active(user_id, chat_id)
            lines = []
            for s in sessions:
                mark = "▶" if s is active else "　"
                title = s.title or "(未命名)"
                meta = f"{s.total_turns}轮" if s.total_turns else "新建"
                lines.append(f"{mark} `{s.session_id}`  **{title}**  ·  {meta}")
            await self.feishu.reply_card(message_id, info_card(
                "🗂 会话列表", "\n".join(lines), template="blue",
                footer="/switch <id> 切换  ·  /resume <id> 恢复历史会话"))

        elif command == "/switch":
            if not arg:
                await self.feishu.reply_card(message_id, info_card(
                    "❓ 用法", "`/switch <会话id>`，如 `/switch s2`", template="red"))
            elif self.store.switch(user_id, chat_id, arg):
                s = self.store.active(user_id, chat_id)
                await self.feishu.reply_card(message_id, info_card(
                    "✅ 已切换", f"当前会话：`{arg}`  {s.title or ''}", template="green"))
            else:
                await self.feishu.reply_card(message_id, info_card(
                    "❓ 未找到", f"没有会话 `{arg}`，用 `/list` 查看。", template="red"))

        elif command == "/resume":
            await self._cmd_resume(user_id, chat_id, message_id, arg)

        elif command == "/cd":
            session = self.store.active(user_id, chat_id)
            if not arg:
                cur = (session.workdir_override if session else "") or self.config.workdir
                await self.feishu.reply_card(message_id, info_card(
                    "ℹ️ 工作目录", f"当前：`{cur}`\n\n用法：`/cd <路径>`", template="blue"))
            elif not os.path.isdir(os.path.expanduser(arg)):
                await self.feishu.reply_card(message_id, info_card(
                    "❌ 目录不存在", f"`{arg}`", template="red"))
            else:
                if session is None:
                    session = self.store.create(user_id, chat_id)
                session.workdir_override = os.path.expanduser(arg)
                await self.feishu.reply_card(message_id, info_card(
                    "✅ 工作目录已更新",
                    f"`{session.workdir_override}`（下一轮生效）", template="green"))

        elif command == "/model":
            session = self.store.active(user_id, chat_id)
            if not arg:
                cur = (session.model_override if session else "") or "（默认）"
                await self.feishu.reply_card(message_id, info_card(
                    "ℹ️ 模型", f"当前：{cur}\n\n用法：`/model <模型名>`", template="blue"))
            else:
                if session is None:
                    session = self.store.create(user_id, chat_id)
                session.model_override = arg
                await self.feishu.reply_card(message_id, info_card(
                    "✅ 模型已更新",
                    f"`{arg}`（仅本会话，下一轮生效）", template="green"))

        elif command == "/stop":
            session = self.store.active(user_id, chat_id)
            if session and session.turn_active:
                try:
                    session.loop_cancelled = True   # 若在 /loop 中，令其退出
                    await self.backend.interrupt(session)
                    session.turn_active = False
                    self._stop_heartbeat(session)
                    self._freeze_card(session)
                    await self.feishu.reply_card(message_id, info_card(
                        "⏹ 已中断", "当前任务已停止。", template="green"))
                except Exception as e:
                    await self.feishu.reply_card(message_id, info_card(
                        "❌ 中断失败", f"`{e}`", template="red"))
            else:
                await self.feishu.reply_card(message_id, info_card(
                    "ℹ️ 无运行任务", "当前没有正在执行的任务。", template="blue"))

        elif command == "/clear":
            session = self.store.active(user_id, chat_id)
            if session is None:
                await self.feishu.reply_card(message_id, info_card(
                    "ℹ️ 无会话", "当前没有会话，直接发消息即可开始。", template="blue"))
            else:
                await self.backend.new_session(session)
                await self.feishu.reply_card(message_id, info_card(
                    "🧹 已清空上下文",
                    f"会话 `{session.session_id}` 已清空记忆，下一轮从零开始（id 与标题保留）。",
                    template="green"))

        elif command == "/retry":
            session = self.store.active(user_id, chat_id)
            if not session or not session.last_prompt:
                await self.feishu.reply_card(message_id, info_card(
                    "ℹ️ 无可重试", "本会话还没有可重发的消息。", template="blue"))
            elif session.turn_active:
                await self.feishu.reply_card(message_id, info_card(
                    "⏳ 任务进行中", "请先 `/stop` 再重试。", template="orange"))
            else:
                prompt = session.last_prompt
                await self.feishu.reply_card(message_id, info_card(
                    "🔁 重新发送", f"`{prompt[:60]}`", template="blue"))
                await self._forward(user_id, chat_id, message_id, prompt)

        elif command == "/pwd":
            session = self.store.active(user_id, chat_id)
            cur = (session.workdir_override if session else "") or self.config.workdir
            await self.feishu.reply_card(message_id, info_card(
                "📂 工作目录", f"`{cur}`", template="blue"))

        elif command == "/status":
            session = self.store.active(user_id, chat_id)
            n = len(self.store.list_sessions(user_id, chat_id))
            if session:
                wd = session.workdir_override or self.config.workdir
                running = "运行中 ⏳" if session.turn_active else "空闲"
                body = (f"**Agent**：{self.agent_name}\n"
                        f"**工作目录**：`{wd}`\n"
                        f"**当前会话**：`{session.session_id}` {session.title or '(未命名)'}\n"
                        f"**状态**：{running}\n"
                        f"**本聊天会话数**：{n}")
            else:
                body = (f"**Agent**：{self.agent_name}\n"
                        f"**工作目录**：`{self.config.workdir}`\n"
                        f"**当前会话**：无（发消息即开始）")
            await self.feishu.reply_card(message_id, info_card(
                "📋 状态", body, template="blue"))

        elif command == "/rename":
            if not arg:
                await self.feishu.reply_card(message_id, info_card(
                    "❓ 用法", "`/rename <新标题>`（重命名当前会话）", template="red"))
            else:
                session = self.store.active(user_id, chat_id)
                if not session:
                    await self.feishu.reply_card(message_id, info_card(
                        "ℹ️ 无会话", "当前没有会话可重命名。", template="blue"))
                else:
                    self.store.rename(user_id, chat_id, session.session_id, arg)
                    await self.feishu.reply_card(message_id, info_card(
                        "✅ 已重命名", f"`{session.session_id}` → **{arg}**", template="green"))

        elif command == "/delete":
            if not arg:
                await self.feishu.reply_card(message_id, info_card(
                    "❓ 用法", "`/delete <会话id>`，先用 `/list` 查看", template="red"))
            elif self.store.delete(user_id, chat_id, arg):
                await self.feishu.reply_card(message_id, info_card(
                    "🗑 已删除", f"会话 `{arg}` 已删除。", template="green"))
            else:
                await self.feishu.reply_card(message_id, info_card(
                    "❓ 未找到", f"没有会话 `{arg}`，用 `/list` 查看。", template="red"))

        elif command == "/loop":
            await self._cmd_loop(user_id, chat_id, message_id, arg)

        elif command == "/agent":
            await self.feishu.reply_card(message_id, info_card(
                "🤖 当前 Agent",
                f"当前：**{self.agent_name}**\n\n"
                f"可选：{', '.join(f'`{a}`' for a in SUPPORTED_BACKENDS)}",
                template="blue",
                footer="切换：修改 config.json 的 agent 字段后重启"))

        elif command == "/usage":
            session = self.store.active(user_id, chat_id)
            if session and session.total_turns > 0:
                body = (f"**轮数**：{session.total_turns}\n"
                        f"**工具调用**：{session.total_tool_calls} 次")
                if session.total_cost_usd > 0:
                    body += f"\n**累计成本**：${session.total_cost_usd:.4f}"
                await self.feishu.reply_card(message_id, info_card(
                    f"📊 本会话用量", body, template="blue",
                    footer=f"agent: {self.agent_name}"))
            else:
                await self.feishu.reply_card(message_id, info_card(
                    "📊 本会话用量", "本会话还没有用量记录。", template="blue"))

        elif command == "/help":
            await self.feishu.reply_card(message_id, info_card(
                "🤖 Pocket Agent",
                "直接发消息 → Agent 处理\n\n"
                "**会话**\n"
                "`/new` 新对话　`/list` 列出会话\n"
                "`/switch <id>` 切换　`/resume <id>` 恢复历史\n"
                "`/rename <名>` 改标题　`/delete <id>` 删除\n"
                "`/clear` 清空上下文\n\n"
                "**设置**\n"
                "`/cd <路径>` 切目录　`/pwd` 看目录　`/model <名>` 切模型\n\n"
                "**运行**\n"
                "`/retry` 重发上条　`/loop <任务>` 反复迭代到完成\n"
                "`/stop` 中断　`/status` 状态　`/usage` 用量　`/agent` 当前 agent",
                template="blue",
                footer=f"当前 agent: {self.agent_name}"))

        else:
            # 命令纠错：找最接近的已知命令
            import difflib
            known = ["/new", "/list", "/switch", "/resume", "/cd", "/model",
                     "/stop", "/usage", "/agent", "/help",
                     "/clear", "/retry", "/pwd", "/status", "/rename",
                     "/delete", "/loop"]
            near = difflib.get_close_matches(command, known, n=1, cutoff=0.5)
            tip = f"你是说 `{near[0]}` 吗？" if near else "发送 `/help` 查看可用命令。"
            await self.feishu.reply_card(message_id, info_card(
                "❓ 未知命令", f"`{command}`\n\n{tip}", template="red"))

    async def _cmd_resume(self, user_id, chat_id, message_id, arg):
        """恢复一个历史会话为活跃（后端续接靠 native_id，下一轮自然带上）。"""
        if not arg:
            await self.feishu.reply_card(message_id, info_card(
                "❓ 用法", "`/resume <会话id>`，先用 `/list` 查看", template="red"))
            return
        s = self.store.get(user_id, chat_id, arg)
        if not s:
            await self.feishu.reply_card(message_id, info_card(
                "❓ 未找到", f"没有会话 `{arg}`", template="red"))
            return
        self.store.switch(user_id, chat_id, arg)
        tip = "将续接之前的上下文" if s.native_id else "无后端记录，将作为新会话"
        await self.feishu.reply_card(message_id, info_card(
            "✅ 已恢复会话", f"`{arg}`  {s.title or ''}\n\n{tip}", template="green"))

    # ── /loop 自循环 ──

    LOOP_MAX_DEFAULT = 5
    LOOP_MAX_HARD = 15
    LOOP_DONE_MARKERS = ("已完成", "全部完成", "任务完成", "done", "completed", "完成。")
    LOOP_CONTINUE_PROMPT = (
        "请检查上面的工作是否已经完全达成任务目标。"
        "如果已全部完成，请在回复的开头明确写「已完成」并简述结果；"
        "如果还没有，请继续改进，不要重复已做的部分。")

    def _signal_turn_done(self, session, errored: bool):
        """turn 结束（TURN_DONE/ERROR）时记录结果并唤醒等待该 session 的 /loop。"""
        session._loop_last_errored = errored
        evt = self._turn_done_events.get(session.session_id)
        if evt:
            evt.set()

    async def _wait_turn_done(self, session, timeout: float = 1800.0) -> bool:
        """等待 session 当前 turn 结束。返回 True=正常结束，False=出错/超时。"""
        if not session.turn_active:
            return not getattr(session, "_loop_last_errored", False)
        evt = asyncio.Event()
        self._turn_done_events[session.session_id] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._turn_done_events.pop(session.session_id, None)
        return not getattr(session, "_loop_last_errored", False)

    async def _cmd_loop(self, user_id, chat_id, message_id, arg):
        """让 agent 反复迭代直到自评完成（含完成标记）或达到最大轮数。

        用法：/loop <任务>  或  /loop <轮数> <任务>（轮数 1~LOOP_MAX_HARD）。
        停止条件：agent 回复含完成标记 / 达到上限 / 出错 / 被 /stop 取消。
        """
        # 解析可选的轮数前缀
        max_rounds = self.LOOP_MAX_DEFAULT
        parts = arg.split(maxsplit=1)
        if parts and parts[0].isdigit():
            max_rounds = max(1, min(self.LOOP_MAX_HARD, int(parts[0])))
            task = parts[1].strip() if len(parts) > 1 else ""
        else:
            task = arg.strip()
        if not task:
            await self.feishu.reply_card(message_id, info_card(
                "❓ 用法", "`/loop <任务>` 或 `/loop <轮数> <任务>`，"
                "让 agent 反复迭代到自评完成。", template="red"))
            return

        session = self.store.active(user_id, chat_id)
        if session and session.turn_active:
            await self.feishu.reply_card(message_id, info_card(
                "⏳ 任务进行中", "请先 `/stop` 再开始循环。", template="orange"))
            return

        await self.feishu.reply_card(message_id, info_card(
            "🔄 开始循环", f"任务：{task}\n\n最多 {max_rounds} 轮，自评完成即停。",
            template="blue"))

        # 首轮
        await self._forward(user_id, chat_id, message_id, task)
        session = self.store.active(user_id, chat_id)
        if session is None:
            return
        session.loop_cancelled = False

        for i in range(1, max_rounds + 1):
            ok = await self._wait_turn_done(session)
            if session.loop_cancelled:
                await self.feishu.send_interactive(chat_id, info_card(
                    "⏹ 循环已停止", f"在第 {i} 轮被中断。", template="blue"))
                return
            if not ok:
                await self.feishu.send_interactive(chat_id, info_card(
                    "⚠️ 循环中止", f"第 {i} 轮出错或超时，已停止。", template="red"))
                return
            # 读最近一轮回复，判断是否自评完成
            reply = (session.history[-1]["reply"] if session.history else "") or ""
            if any(m in reply.lower() for m in
                   (mk.lower() for mk in self.LOOP_DONE_MARKERS)):
                await self.feishu.send_interactive(chat_id, info_card(
                    "✅ 循环完成", f"agent 在第 {i} 轮自评完成。", template="green"))
                return
            if i >= max_rounds:
                break
            # 继续下一轮
            await self.feishu.send_interactive(chat_id, info_card(
                "🔄 继续迭代", f"第 {i + 1}/{max_rounds} 轮…", template="blue"))
            await self._forward(user_id, chat_id, message_id,
                                self.LOOP_CONTINUE_PROMPT)

        await self.feishu.send_interactive(chat_id, info_card(
            "⏹ 已达上限", f"已迭代 {max_rounds} 轮仍未自评完成，停止。"
            "可 `/retry` 或继续手动指挥。", template="orange"))

    # ── 转发 ──

    async def _forward(self, user_id, chat_id, message_id, text):
        session = self.store.active(user_id, chat_id)

        # 空闲轮换：超过 idle_minutes 没有真实用户消息 → 开新会话防上下文漂移
        if session and not session.turn_active and self.config.idle_minutes > 0:
            idle = time.time() - session.last_user_msg_time
            if session.last_user_msg_time and idle > self.config.idle_minutes * 60:
                try:
                    await self.backend.new_session(session)
                except Exception:
                    pass
                self.store.remove_active(user_id, chat_id)
                session = None
                logger.info("Session for %s rotated after %.0fs idle", user_id, idle)

        if session is None:
            # 用首条消息前 20 字做标题
            title = text[:20] + ("…" if len(text) > 20 else "")
            session = self.store.create(user_id, chat_id, title=title)

        # 上一轮还在跑：Codex 支持把新消息 steer 进当前 turn，子进程后端则
        # 不支持并发——提示用户先 /stop，避免卡片错乱、进程打架。
        if session.turn_active:
            if self.backend.name == "codex":
                try:
                    await self.backend.send(session, text)
                    return
                except Exception as e:
                    logger.error("Steer failed: %s", e)
            await self.feishu.send_text(
                chat_id, "⏳ 上一条还在处理中。发送 /stop 可中断，然后再发新消息。")
            return

        session.last_user_msg_time = time.time()
        session.last_prompt = text       # 错误重试用
        # 起一张新卡片
        session.reset_turn()
        session.turn_start_time = time.time()
        card = self.renderer.render(session, done=False)
        resp = await self.feishu.send_interactive(chat_id, card)
        session.card_message_id = resp.get("data", {}).get("message_id", "")

        # 启动心跳：静默期也刷新「已运行 Xs」，让用户知道没卡死
        self._start_heartbeat(session)

        try:
            await self.backend.send(session, text)
        except Exception as e:
            logger.error("Forward failed: %s", e, exc_info=True)
            session.turn_active = False
            await self._update_card(session, error=str(e))

    # ── 事件消费 ──

    async def _consume_events(self):
        async for event in self.backend.events():
            try:
                await self._handle_event(event)
            except Exception as e:
                logger.error("Event handling error: %s", e, exc_info=True)

    async def _handle_event(self, event: AgentEvent):
        session = self._session_by_native(event.route_id)
        if session is None:
            return

        kind = event.kind

        if kind == EventKind.SESSION_ID:
            if event.session_id:
                session.native_id = event.session_id

        elif kind == EventKind.TURN_STARTED:
            session.current_turn_id = event.tool_call_id
            session.turn_active = True

        elif kind == EventKind.TEXT_DELTA:
            session.body_buffer += event.text
            await self._maybe_split(session)
            await self._maybe_update(session)

        elif kind == EventKind.THINKING_DELTA:
            session.thinking_buffer += event.text
            await self._maybe_update(session)

        elif kind == EventKind.TOOL_START:
            existing = self._find_tool(session, event.tool_call_id)
            if existing:
                # 去重：补全可能缺失的参数
                if not existing.get("input") and event.tool_input:
                    existing["input"] = event.tool_input
            else:
                session.tool_calls.append({
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "input": event.tool_input,
                    "status": "running",
                })
            await self._maybe_update(session, force=True)

        elif kind == EventKind.TOOL_END:
            tc = self._find_tool(session, event.tool_call_id)
            if tc:
                tc["status"] = event.tool_status or "completed"
                tc["output"] = event.tool_output
            else:
                session.tool_calls.append({
                    "id": event.tool_call_id, "name": event.tool_name,
                    "input": event.tool_input,
                    "status": event.tool_status or "completed",
                    "output": event.tool_output,
                })
            await self._maybe_update(session, force=True)

        elif kind == EventKind.APPROVAL_REQUEST:
            session.pending_approvals[event.approval_id] = {
                "kind": event.approval_kind,
                **(event.raw or {}),
            }
            session.approval_count += 1
            # 用户本轮已点「全部允许」→ 直接批准，不再弹卡片
            if session.auto_approve_turn:
                await self.backend.approve(session, event.approval_id, True)
                session.pending_approvals.pop(event.approval_id, None)
            else:
                await self._send_approval_card(session, event)

        elif kind == EventKind.FILE_OUTPUT:
            await self._send_file_output(session, event.file_path, event.is_image)

        elif kind == EventKind.TURN_DONE:
            session.turn_active = False
            session.current_turn_id = ""
            self._stop_heartbeat(session)
            if event.cost_usd:
                session.last_cost_usd = event.cost_usd
            # 累计会话用量（/usage 展示）
            session.total_turns += 1
            session.total_cost_usd += event.cost_usd or 0.0
            session.total_tool_calls += len(session.tool_calls)
            # 记一条历史（持久化用）：用户 prompt + agent 回复摘要 + 工具
            session.history.append({
                "prompt": session.last_prompt,
                "reply": session.body_buffer[:2000],
                "tools": [t.get("name", "") for t in session.tool_calls],
                "cost": event.cost_usd or 0.0,
            })
            elapsed = (time.time() - session.turn_start_time
                       if session.turn_start_time else 0.0)
            await self._finish_turn(session)
            await self._maybe_notify_done(session, elapsed)
            self.store.save()      # 每轮结束落盘，防进程异常退出丢失
            self._signal_turn_done(session, errored=False)

        elif kind == EventKind.ERROR:
            session.turn_active = False
            self._stop_heartbeat(session)
            await self._update_card(session, error=event.error)
            # 出错且有上轮 prompt → 发一张带「重试」按钮的卡片
            if session.last_prompt:
                await self._send_retry_card(session)
            self._signal_turn_done(session, errored=True)

    @staticmethod
    def _find_tool(session: AgentSession, tool_call_id: str):
        for tc in session.tool_calls:
            if tc.get("id") == tool_call_id:
                return tc
        return None

    # ── 卡片更新（节流）──

    async def _maybe_split(self, session: AgentSession):
        """正文超长 → 封板当前卡片，另起续接卡片，避免截断丢内容。

        用代码围栏感知切分：不会把 ``` 代码块从中切断（否则两段都渲染失效）。
        """
        if len(session.body_buffer) <= self.config.max_message_length:
            return
        from .renderer import split_at_codefence_boundary
        head, tail = split_at_codefence_boundary(
            session.body_buffer, self.config.max_message_length)
        if not tail:        # 没找到合适切点（极端情况），不切
            return

        # 把当前卡片定格在 head（标“续下页”），然后开新卡片
        session.body_buffer = head
        await self._update_card(session, done=True, continued=True)

        session.card_index += 1
        session.body_buffer = tail
        session.thinking_buffer = ""     # 思考只在首卡展示，避免重复
        session.tool_calls = []
        session.last_update_time = 0.0
        session.last_sent_len = 0
        session.last_sent_card = ""      # 新卡片重置去重/降级状态
        session.card_degraded = False
        session.card_fail_count = 0
        card = self.renderer.render(session, done=False)
        resp = await self.feishu.send_interactive(session.chat_id, card)
        session.card_message_id = resp.get("data", {}).get("message_id", "")

    async def _maybe_update(self, session: AgentSession, force: bool = False):
        # 已降级：流式期间不再 PATCH（终态更新走 _update_card 自己的逻辑）
        if session.card_degraded:
            return
        now = time.time()
        if not force:
            # 时间节流 + 最小增量字符门槛（减少无谓 PATCH）
            if now - session.last_update_time < self.config.throttle_seconds:
                return
            grown = len(session.body_buffer) + len(session.thinking_buffer) - session.last_sent_len
            if grown < self.config.min_delta_chars:
                return
        session.last_update_time = now
        session.last_sent_len = len(session.body_buffer) + len(session.thinking_buffer)
        await self._update_card(session, done=False)

    async def _update_card(self, session: AgentSession, done: bool = False,
                           error: str = "", continued: bool = False):
        if not session.card_message_id:
            return
        # 流式中途且已降级：跳过；但终态（done/error）必须尝试落地
        terminal = done or bool(error)
        if session.card_degraded and not terminal:
            return

        elapsed = (time.time() - session.turn_start_time
                   if session.turn_start_time else 0.0)
        card = self.renderer.render(session, done=done, error=error, elapsed=elapsed)
        if continued:
            card["elements"].append({
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "↓ 内容较长，接下一条"}],
            })

        # 相同内容跳过：飞书对相同内容的 PATCH 会失败/报错
        import json as _json
        serialized = _json.dumps(card, ensure_ascii=False, sort_keys=True)
        if serialized == session.last_sent_card and not terminal:
            return

        try:
            await self.feishu.update_card(session.card_message_id, card)
            session.last_sent_card = serialized
            session.card_fail_count = 0
        except Exception as e:
            session.card_fail_count += 1
            logger.error("Card update failed (%d): %s", session.card_fail_count, e)
            # 连续失败 → 降级，停止流式 PATCH，避免持续撞墙刷日志
            if session.card_fail_count >= 3 and not terminal:
                session.card_degraded = True
                logger.warning("Card streaming degraded for session %s", session.native_id)

    def _freeze_card(self, session: AgentSession):
        """定格流式卡片：审批弹出 / 中断时调用，停止后续流式 PATCH，避免错乱。

        审批与流式更新不应同时改同一张卡片，故定格。
        """
        session.card_degraded = True

    # ── 运行中心跳：静默期也刷新「已运行 Xs」，缓解「看不见终端」焦虑 ──

    def _start_heartbeat(self, session: AgentSession):
        if self.config.heartbeat_seconds <= 0:
            return
        self._stop_heartbeat(session)
        self._heartbeats[session.session_id] = asyncio.create_task(
            self._heartbeat_loop(session))

    def _stop_heartbeat(self, session: AgentSession):
        task = self._heartbeats.pop(session.session_id, None)
        if task:
            task.cancel()

    async def _heartbeat_loop(self, session: AgentSession):
        interval = self.config.heartbeat_seconds
        try:
            while session.turn_active:
                await asyncio.sleep(interval)
                if not session.turn_active or session.card_degraded:
                    break
                # 仅在「这段时间没有别的更新」时才心跳刷新，避免和流式 PATCH 抢
                quiet = time.time() - session.last_update_time
                if quiet >= interval:
                    await self._update_card(session, done=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("heartbeat error: %s", e)

    async def _send_welcome(self, chat_id: str):
        """首次交互的欢迎引导卡片。"""
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "👋 欢迎使用 Pocket Agent"},
                       "template": "blue"},
            "elements": [
                {"tag": "markdown", "content":
                    f"用飞书远程遥控本地 **{self.agent_name}** 编码 agent。\n\n"
                    "**直接发文字**就是给 agent 的指令，比如：\n"
                    "- `看看当前目录有哪些文件`\n"
                    "- `修复登录接口的报错`\n"
                    "- `跑一下测试`\n\n"
                    "**常用命令：**\n"
                    "- `/new` 新对话 · `/list` 列出 · `/switch` 切换\n"
                    "- `/cd` 切目录 · `/stop` 中断 · `/usage` 用量\n"
                    "- `/help` 查看全部命令"},
            ],
        }
        try:
            await self.feishu.send_interactive(chat_id, card)
        except Exception as e:
            logger.error("Send welcome failed: %s", e)

    async def _finish_turn(self, session: AgentSession):
        """一轮结束：定格主卡片；若正文表格过多，溢出部分另发消息。

        单条飞书消息塞太多表格会渲染异常，故按表格边界拆分。
        """
        # 表格过多 → 拆分：主卡片只留前半，其余表格各发一条 markdown 卡片
        parts = self.renderer.split_long(session.body_buffer, max_tables=3)
        if len(parts) > 1:
            session.body_buffer = parts[0]
        await self._update_card(session, done=True)

        for extra in parts[1:]:
            card = {
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": extra}],
            }
            try:
                await self.feishu.send_interactive(session.chat_id, card)
            except Exception as e:
                logger.error("Send split table card failed: %s", e)

    # ── 审批卡片 ──

    async def _send_approval_card(self, session: AgentSession, event: AgentEvent):
        tool_input = (event.raw or {}).get("tool_input")
        card = self.renderer.render_approval(
            session, event.approval_id, event.approval_kind,
            event.approval_text, event.approval_detail, tool_input=tool_input)
        await self.feishu.send_interactive(session.chat_id, card)

    async def _send_retry_card(self, session: AgentSession):
        """出错后给一张「重试」卡片，点击用同一 prompt 重发。"""
        preview = session.last_prompt[:40] + ("…" if len(session.last_prompt) > 40 else "")
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "↻ 重试上一条？"},
                       "template": "grey"},
            "elements": [
                {"tag": "markdown", "content": f"上一条：{preview}"},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "↻ 重试"},
                     "type": "primary",
                     "value": {"action": "retry", "uid": session.user_id,
                               "cid": session.chat_id, "sid": session.session_id}},
                ]},
            ],
        }
        await self.feishu.send_interactive(session.chat_id, card)

    async def _maybe_notify_done(self, session: AgentSession, elapsed: float):
        """长任务完成主动提醒：用户可能已切走，发一条独立消息拉回注意力。"""
        threshold = self.config.notify_done_seconds
        if threshold <= 0 or elapsed < threshold:
            return
        from .renderer import _fmt_duration, _changed_files
        changed = _changed_files(session.tool_calls)
        files = ("，改动 " + str(len(changed)) + " 个文件") if changed else ""
        try:
            await self.feishu.send_text(
                session.chat_id,
                f"✅ 任务完成（耗时 {_fmt_duration(elapsed)}{files}）")
        except Exception as e:
            logger.error("notify done failed: %s", e)

    # ── 文件/图片回传 ──

    async def _send_file_output(self, session: AgentSession, path: str, is_image: bool):
        """把 agent 产出的图片/文件上传飞书并发给用户。"""
        import os
        if not path or not os.path.isfile(path):
            logger.warning("File output not found: %s", path)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            # 飞书图片/文件单条上限约 30MB；过大降级为提示
            if len(data) > 30 * 1024 * 1024:
                await self.feishu.send_text(
                    session.chat_id, f"📎 产物过大未发送：{os.path.basename(path)}")
                return
            if is_image:
                key = await self.feishu.upload_image(data)
                if key:
                    await self.feishu.send_image(session.chat_id, key)
            else:
                key = await self.feishu.upload_file(data, os.path.basename(path))
                if key:
                    await self.feishu.send_file(session.chat_id, key)
        except Exception as e:
            logger.error("Send file output failed: %s", e)
