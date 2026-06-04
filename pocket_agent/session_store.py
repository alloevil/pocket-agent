"""会话存储 — 按 (user_id, chat_id) 分组管理多会话 + 当前活跃指针 + 持久化

设计：
- 一个 (user_id, chat_id) 是一个「会话组」，组内可有多个 AgentSession。
- 每组有一个「当前活跃会话」，用户发消息默认走它；/switch 可切换。
- 后端事件靠 native_id 路由回正确会话（by_native）。
- 可选持久化：元数据 + 对话历史 落地本地 json，重启后 /resume 接回。
"""

import json
import logging
import os
import tempfile
from typing import Optional

from .events import AgentSession

logger = logging.getLogger(__name__)


def _short_id(n: int) -> str:
    """生成短会话 id：s1, s2, …（够用且好在手机上输入）"""
    return f"s{n}"


class SessionStore:
    def __init__(self, persist_path: str = ""):
        # (user_id, chat_id) -> list[AgentSession]
        self._groups: dict[tuple, list[AgentSession]] = {}
        # (user_id, chat_id) -> 当前活跃会话的 session_id
        self._active: dict[tuple, str] = {}
        self._seq = 0
        self.persist_path = persist_path

    # ── 基础 ──

    @staticmethod
    def _key(user_id: str, chat_id: str) -> tuple:
        return (user_id, chat_id)

    def _next_session_id(self) -> str:
        self._seq += 1
        return _short_id(self._seq)

    def active(self, user_id: str, chat_id: str) -> Optional[AgentSession]:
        """返回该 (用户,chat) 的当前活跃会话，没有则 None"""
        key = self._key(user_id, chat_id)
        sid = self._active.get(key)
        if not sid:
            return None
        for s in self._groups.get(key, []):
            if s.session_id == sid:
                return s
        return None

    def create(self, user_id: str, chat_id: str, title: str = "",
               **kwargs) -> AgentSession:
        """在该 (用户,chat) 组内新建一个会话并设为活跃"""
        key = self._key(user_id, chat_id)
        s = AgentSession(user_id=user_id, chat_id=chat_id,
                         session_id=self._next_session_id(),
                         title=title, **kwargs)
        self._groups.setdefault(key, []).append(s)
        self._active[key] = s.session_id
        return s

    def add(self, session: AgentSession) -> AgentSession:
        """把一个已构造好的 AgentSession 纳入管理并设为活跃（持久化恢复/测试用）"""
        if not session.session_id:
            session.session_id = self._next_session_id()
        key = self._key(session.user_id, session.chat_id)
        self._groups.setdefault(key, []).append(session)
        self._active[key] = session.session_id
        return session

    def list_sessions(self, user_id: str, chat_id: str) -> list:
        return list(self._groups.get(self._key(user_id, chat_id), []))

    def switch(self, user_id: str, chat_id: str, session_id: str) -> bool:
        """切换活跃会话；成功返回 True"""
        key = self._key(user_id, chat_id)
        for s in self._groups.get(key, []):
            if s.session_id == session_id:
                self._active[key] = session_id
                return True
        return False

    def get(self, user_id: str, chat_id: str, session_id: str) -> Optional[AgentSession]:
        for s in self._groups.get(self._key(user_id, chat_id), []):
            if s.session_id == session_id:
                return s
        return None

    def remove_active(self, user_id: str, chat_id: str):
        """移除当前活跃会话（/new 丢弃旧会话时用）"""
        key = self._key(user_id, chat_id)
        sid = self._active.get(key)
        if not sid:
            return
        self._groups[key] = [s for s in self._groups.get(key, [])
                             if s.session_id != sid]
        self._active.pop(key, None)
        # 若组内还有其它会话，活跃指针落到最后一个
        remaining = self._groups.get(key, [])
        if remaining:
            self._active[key] = remaining[-1].session_id

    def rename(self, user_id: str, chat_id: str, session_id: str, title: str) -> bool:
        """给指定会话改标题；成功返回 True。"""
        s = self.get(user_id, chat_id, session_id)
        if not s:
            return False
        s.title = title
        self.save()
        return True

    def delete(self, user_id: str, chat_id: str, session_id: str) -> bool:
        """删除指定会话；若删的是活跃会话，活跃指针落到剩余最后一个。成功返回 True。"""
        key = self._key(user_id, chat_id)
        group = self._groups.get(key, [])
        if not any(s.session_id == session_id for s in group):
            return False
        self._groups[key] = [s for s in group if s.session_id != session_id]
        if self._active.get(key) == session_id:
            remaining = self._groups.get(key, [])
            if remaining:
                self._active[key] = remaining[-1].session_id
            else:
                self._active.pop(key, None)
        self.save()
        return True

    def by_native(self, native_id: str) -> Optional[AgentSession]:
        """按后端 native_id 路由回会话（事件回流用）"""
        if not native_id:
            # 兜底：全局唯一活跃 turn
            active = [s for g in self._groups.values() for s in g if s.turn_active]
            return active[0] if len(active) == 1 else None
        for g in self._groups.values():
            for s in g:
                if s.native_id == native_id:
                    return s
        return None

    def all_sessions(self) -> list:
        return [s for g in self._groups.values() for s in g]

    # ── 持久化 ──

    def load(self):
        """从磁盘加载会话元数据（重启恢复）。失败不致命。"""
        if not self.persist_path or not os.path.isfile(self.persist_path):
            return
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("加载会话持久化失败: %s", e)
            return
        self._seq = data.get("seq", 0)
        for item in data.get("sessions", []):
            try:
                s = AgentSession.from_dict(item)
            except Exception:
                continue
            key = self._key(s.user_id, s.chat_id)
            self._groups.setdefault(key, []).append(s)
        for item in data.get("active", []):
            self._active[(item["user_id"], item["chat_id"])] = item["session_id"]
        logger.info("已加载 %d 个会话", len(self.all_sessions()))

    def save(self):
        """原子写入磁盘（先写临时文件再 rename，避免半截损坏）。"""
        if not self.persist_path:
            return
        data = {
            "seq": self._seq,
            "sessions": [s.to_dict() for s in self.all_sessions()],
            "active": [
                {"user_id": k[0], "chat_id": k[1], "session_id": v}
                for k, v in self._active.items()
            ],
        }
        try:
            os.makedirs(os.path.dirname(self.persist_path) or ".", exist_ok=True)
            d = os.path.dirname(self.persist_path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.persist_path)
        except Exception as e:
            logger.warning("保存会话持久化失败: %s", e)
