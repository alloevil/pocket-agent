"""飞书富渲染 — 把 AgentSession 的累积状态渲染成交互卡片

一个 turn 用一张（或多张，长输出分条）卡片，随流式输出原地更新（PATCH）。卡片含：
- 标题栏：agent 名 + 运行状态（+ 多卡片时的分页标记）
- 思考过程：可折叠面板（show_thinking 控制是否渲染）
- 工具调用：列表，每项工具名 + 关键参数 + 状态图标
- 正文：完整 Markdown（用飞书 markdown 组件，支持列表/代码块/标题/分割线）
- 完成页脚：耗时 + 成本（后端提供时）

⚠️ 用飞书 **markdown 组件**（tag="markdown"）而非 lark_md：后者只认加粗/斜体/
链接，列表、代码块、标题都不渲染——对 AI 编码 agent 的输出（大量代码块/列表）
是硬伤。markdown 组件支持完整 Markdown。
"""

import json
import os
import re
from typing import Optional

from .events import AgentSession
from .renderer_base import Renderer

# 连续的 markdown 表格块（表头 + 分隔行 + 数据行）。用于按表格边界切分长内容。
_MD_TABLE_RE = re.compile(
    r"(?:^|\n)[ \t]*\|.*\|[ \t]*\n[ \t]*\|[ \t]*[-:| \t]+\|[ \t]*(?:\n[ \t]*\|.*\|[ \t]*)*"
)

# 代码围栏行：``` 或 ~~~ 开头（可带语言标识）
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")


def _fence_lang(line: str) -> str:
    """从围栏开行提取语言标识（用于跨卡片重开围栏时保留高亮）"""
    s = line.strip().lstrip("`~").strip()
    return s


def split_at_codefence_boundary(text: str, limit: int) -> tuple[str, str]:
    """把 text 切成 (head, tail)，head 不超过 limit，且不切断代码围栏。

    逐行累积到接近 limit 时切分；
    若切点落在未闭合的 ``` 围栏内，则在 head 末尾补 ``` 闭合、在 tail 头部用
    同语言重开围栏，保证两段都能正确渲染代码块。
    """
    if len(text) <= limit:
        return text, ""

    lines = text.split("\n")
    head_lines: list[str] = []
    size = 0
    in_fence = False
    fence_lang = ""
    fence_mark = "```"
    cut_idx = len(lines)

    for i, line in enumerate(lines):
        add = len(line) + 1  # +1 换行
        # 超限且不在围栏内 → 这里切（至少留一行，避免空 head）
        if size + add > limit and head_lines and not in_fence:
            cut_idx = i
            break
        m = _FENCE_RE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                fence_mark = m.group(1)
                fence_lang = _fence_lang(line)
            else:
                in_fence = False
                fence_lang = ""
        head_lines.append(line)
        size += add
        # 在围栏内但已严重超限（单个代码块比 limit 还长）→ 强制在此切并补围栏
        if size >= limit and in_fence:
            cut_idx = i + 1
            break

    if cut_idx >= len(lines):
        return text, ""

    head = "\n".join(head_lines)
    tail = "\n".join(lines[cut_idx:]).lstrip("\n")

    # 切点在未闭合围栏内：补全 head、用同语言重开 tail
    if in_fence:
        head = head + "\n" + fence_mark
        reopen = fence_mark + (fence_lang or "")
        tail = reopen + "\n" + tail

    return head, tail


def split_markdown_by_tables(md_text: str, max_tables: int = 3) -> list[str]:
    """表格过多时按表格边界把内容切成多条。

    表格 <= max_tables：原样返回单条。否则前 max_tables 个表格连同其前文为第一条，
    其余每个表格各成一条。避免单条飞书消息塞太多表格导致渲染异常。
    """
    if max_tables <= 0:
        return [md_text]
    matches = list(_MD_TABLE_RE.finditer(md_text))
    if len(matches) <= max_tables:
        return [md_text]

    parts: list[str] = []
    first_end = matches[max_tables].start()
    first = md_text[:first_end].strip()
    if first:
        parts.append(first)
    for m in matches[max_tables:]:
        block = md_text[m.start():m.end()].strip()
        if block:
            parts.append(block)
    return parts

# 工具状态 → 图标
_TOOL_ICON = {
    "running": "⏳",
    "completed": "✅",
    "error": "❌",
    "": "🔧",
}

# 常见工具的关键参数字段（用于在卡片里展示简洁摘要）
_TOOL_SUMMARY_KEYS = (
    "command", "cmd", "file_path", "path", "filePath",
    "pattern", "query", "url", "description",
)


def md(content: str) -> dict:
    """飞书 markdown 独立组件（支持完整 Markdown）"""
    return {"tag": "markdown", "content": content}


def _tool_summary(tool_input: Optional[dict]) -> str:
    """从工具参数里挑一个有代表性的值做单行摘要"""
    if not tool_input:
        return ""
    for k in _TOOL_SUMMARY_KEYS:
        if k in tool_input and tool_input[k]:
            val = str(tool_input[k]).replace("\n", " ")
            return val[:80] + ("…" if len(val) > 80 else "")
    try:
        s = json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        s = str(tool_input)
    return s[:80] + ("…" if len(s) > 80 else "")


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s}s"


# 会修改文件的工具 → 从其 input 提取文件路径，用于完成态「改动摘要」
_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit"}


def _changed_files(tool_calls: list) -> list:
    """从本轮工具调用提取被修改的文件路径（去重，保序）"""
    seen, out = set(), []
    for tc in tool_calls:
        if tc.get("name") in _FILE_WRITE_TOOLS:
            fp = (tc.get("input") or {}).get("file_path") or (tc.get("input") or {}).get("path")
            if fp and fp not in seen:
                seen.add(fp)
                out.append(fp)
    return out


def _render_diff(tool_input: Optional[dict], max_lines: int = 30) -> str:
    """Edit 工具的 old_string/new_string → ```diff 代码块（飞书按 diff 高亮）。

    返回空串表示无 diff 可渲染。
    """
    if not tool_input:
        return ""
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if old is None or new is None:
        return ""
    lines = ["```diff"]
    for line in str(old).split("\n")[:max_lines]:
        lines.append(f"- {line}")
    for line in str(new).split("\n")[:max_lines]:
        lines.append(f"+ {line}")
    lines.append("```")
    return "\n".join(lines)


class CardRenderer(Renderer):
    """AgentSession → 飞书交互卡片 JSON（Renderer 的飞书实现）"""

    def __init__(self, agent_name: str, show_thinking: bool = True,
                 max_body_len: int = 4000):
        self.agent_name = agent_name
        self.show_thinking = show_thinking
        self.max_body_len = max_body_len
        # 完成态正文超过此长度则折叠（手机小屏防刷屏）
        self._fold_threshold = 1500

    def render(self, session: AgentSession, done: bool = False,
               error: str = "", elapsed: float = 0.0) -> dict:
        """根据当前 session 状态生成完整卡片 dict"""
        if error:
            status, template = "❌ 出错", "red"
        elif done:
            status, template = "✅ 完成", "green"
        else:
            status, template = "⏳ 运行中", "blue"

        # 多卡片时标题带分页（第 2 张起显示）
        page = f" (#{session.card_index + 1})" if session.card_index > 0 else ""
        title = f"🤖 {self.agent_name} · {status}{page}"
        elements = []

        # ── 思考（折叠，置顶，默认收起）──
        if self.show_thinking and session.thinking_buffer.strip():
            elements.append(self._thinking_panel(session.thinking_buffer))

        # ── 工具调用 ──
        if session.tool_calls:
            elements.append(self._tools_block(session.tool_calls))
            elements.append({"tag": "hr"})

        # ── 正文 ──
        if error:
            elements.append(md(f"❌ **出错**：{error}"))
        else:
            body = session.body_buffer.strip()
            if body:
                # 完成态且正文超长 → 折叠：显示首段摘要 + 折叠面板放全文
                if done and len(body) > self._fold_threshold:
                    elements.append(self._folded_body(body))
                else:
                    elements.append(md(body))
                    if not done:
                        elements.append(md("*▌ 输出中…*"))
            elif done:
                elements.append(md("*(无输出)*"))
            else:
                # 还没有正文：根据已有信号给更贴切的提示 + 已运行时长，让用户知道没卡死
                if session.tool_calls:
                    hint = f"🔧 正在调用工具…（{len(session.tool_calls)} 个）"
                elif session.thinking_buffer.strip():
                    hint = "🧠 思考中…"
                else:
                    hint = "⏳ 处理中…"
                if elapsed >= 5:
                    hint += f"　已运行 {_fmt_duration(elapsed)}"
                elements.append(md(f"*{hint}*"))

        # ── 完成页脚：耗时 + 成本 + 文件变更 ──
        if done and not error:
            changed = _changed_files(session.tool_calls)
            if changed:
                shown = "、".join(f"`{os.path.basename(f)}`" for f in changed[:6])
                more = f" 等 {len(changed)} 个" if len(changed) > 6 else ""
                elements.append(md(f"📝 **改动文件：** {shown}{more}"))
            notes = []
            if elapsed > 0:
                notes.append(f"⏱ {_fmt_duration(elapsed)}")
            if session.last_cost_usd > 0:
                notes.append(f"💰 ${session.last_cost_usd:.4f}")
            if session.tool_calls:
                notes.append(f"🔧 {len(session.tool_calls)} 次工具调用")
            if notes:
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "  ·  ".join(notes)}],
                })

        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        }

    def _folded_body(self, body: str) -> dict:
        """超长正文折叠：摘要（前几行）+ 可展开全文。"""
        # 取前 3 行非空作为摘要预览
        preview_lines = []
        for line in body.split("\n"):
            if line.strip():
                preview_lines.append(line)
            if len(preview_lines) >= 3:
                break
        preview = "\n".join(preview_lines)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        full = body if len(body) <= self.max_body_len else body[:self.max_body_len] + "\n\n…(已截断)"
        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": f"📄 **回复较长，点击展开全文**\n\n{preview}"},
                "vertical_align": "top",
                "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                         "size": "16px 16px"},
                "icon_position": "right",
                "icon_expanded_angle": -180,
            },
            "border": {"color": "grey", "corner_radius": "5px"},
            "elements": [md(full)],
        }

    def _thinking_panel(self, thinking: str) -> dict:
        content = thinking.strip()
        if len(content) > self.max_body_len:
            content = content[:self.max_body_len] + " …"
        return {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": "🧠 **思考过程**"},
                "vertical_align": "center",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "16px 16px",
                },
                "icon_position": "right",
                "icon_expanded_angle": -180,
            },
            "border": {"color": "grey", "corner_radius": "5px"},
            "elements": [md(content)],
        }

    def _tools_block(self, tool_calls: list) -> dict:
        lines = ["**🔧 工具调用**", ""]
        # 工具过多时折叠中间，只显示前 head + 后 tail，避免长任务卡片爆长
        MAX_SHOWN = 10
        head_n, tail_n = 6, 3
        if len(tool_calls) > MAX_SHOWN:
            shown = (
                [(tc, False) for tc in tool_calls[:head_n]]
                + [(None, True)]
                + [(tc, False) for tc in tool_calls[-tail_n:]]
            )
            hidden = len(tool_calls) - head_n - tail_n
        else:
            shown = [(tc, False) for tc in tool_calls]
            hidden = 0

        for tc, is_gap in shown:
            if is_gap:
                lines.append(f"- … 中间省略 {hidden} 次工具调用 …")
                continue
            icon = _TOOL_ICON.get(tc.get("status", ""), "🔧")
            name = tc.get("name", "tool")
            summary = _tool_summary(tc.get("input"))
            if summary:
                lines.append(f"- {icon} `{name}` — {summary}")
            else:
                lines.append(f"- {icon} `{name}`")
            if tc.get("status") == "error" and tc.get("output"):
                err = str(tc["output"]).replace("\n", " ")[:120]
                lines.append(f"    ↳ {err}")
        return md("\n".join(lines))

    # ── 审批卡片 ──

    def render_approval(self, session: AgentSession, approval_id: str,
                        kind: str, text: str, detail: str = "",
                        tool_input: Optional[dict] = None) -> dict:
        # 上下文行：哪个会话 / 工作目录 / 本轮第几次审批——批准前看清楚
        ctx_parts = []
        if session.title:
            ctx_parts.append(f"会话 `{session.session_id}` {session.title}")
        wd = session.workdir_override
        if wd:
            ctx_parts.append(f"目录 `{wd}`")
        if session.approval_count > 1:
            ctx_parts.append(f"本轮第 {session.approval_count} 次")
        ctx = ("　·　".join(ctx_parts) + "\n\n") if ctx_parts else ""

        if kind == "command":
            title, template = "🔐 命令审批", "orange"
            head = f"**目录：** `{detail}`\n\n" if detail else ""
            body = f"{ctx}{head}**命令：**\n```\n{text}\n```"
            ok_text, no_text = "✅ 允许执行", "❌ 拒绝"
        else:
            title, template = "📝 文件修改审批", "blue"
            tool = f"`{detail}`  " if detail else ""
            body = f"{ctx}{tool}**操作：** {text}"
            # Edit 工具有 old_string/new_string → 渲染彩色 diff 预览
            diff = _render_diff(tool_input)
            if diff:
                body += "\n\n" + diff
            ok_text, no_text = "✅ 允许", "❌ 拒绝"

        common = {"key": approval_id, "uid": session.user_id, "cid": session.chat_id}
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": ok_text},
                     "type": "primary", "value": {"action": "approve", **common}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": no_text},
                     "type": "danger", "value": {"action": "reject", **common}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "⏭ 本轮全部允许"},
                     "type": "default", "value": {"action": "approve_all", **common}},
                ]},
            ],
        }

    # ── 长内容切分（表格过多时按表格边界切）──

    def split_long(self, content: str, max_tables: int = 3) -> list[str]:
        return split_markdown_by_tables(content, max_tables)
