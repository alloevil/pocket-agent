"""渲染层抽象 — 平台无关的 Renderer 接口

把"如何把一轮对话状态渲染成平台消息"
抽象成接口，bridge 只依赖这个接口而非具体的飞书实现。将来加 Telegram /
Slack 时，只需实现一个新的 Renderer 子类，bridge 一行不改。

约定：render 返回的对象是「平台原生消息载荷」——飞书是卡片 dict，Telegram
可能是 (text, parse_mode) 元组。bridge 不解释它，只把它透传给对应的 platform
client 的 send/update 方法。因此 Renderer 和 platform client 必须配套。
"""

from abc import ABC, abstractmethod
from typing import Any

from .events import AgentSession


class Renderer(ABC):
    """把 AgentSession 状态渲染成平台原生消息载荷的抽象接口"""

    @abstractmethod
    def render(self, session: AgentSession, done: bool = False,
               error: str = "", elapsed: float = 0.0) -> Any:
        """渲染一轮对话的当前状态。

        done: 是否已完成（终态）
        error: 非空表示出错
        elapsed: 本轮耗时（秒），0 表示不显示
        返回：平台原生消息载荷（飞书=卡片 dict）
        """

    @abstractmethod
    def render_approval(self, session: AgentSession, approval_id: str,
                        kind: str, text: str, detail: str = "",
                        tool_input: dict = None) -> Any:
        """渲染审批请求消息（含批准/拒绝按钮）。

        kind: "command" | "file"
        text: 要审批的内容（命令/原因）
        detail: 附加信息（cwd / 工具名）
        返回：平台原生消息载荷
        """

    def split_long(self, content: str, max_tables: int = 3) -> list[str]:
        """把过长/过多表格的内容切分成多条（平台可选实现）。

        默认不切分。需要的平台覆盖此方法。
        """
        return [content]
