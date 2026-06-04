"""renderer 快照测试 — 喂 AgentSession 状态，断言卡片结构正确"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent.events import AgentSession
from pocket_agent.renderer import CardRenderer, info_card


def _new_session():
    return AgentSession(user_id="u1", chat_id="c1")


def _markdown_texts(card):
    """收集卡片里所有 markdown 组件的 content（含折叠面板内层）"""
    out = []
    for e in card["elements"]:
        if e.get("tag") == "markdown":
            out.append(e["content"])
        elif e.get("tag") == "collapsible_panel":
            for inner in e.get("elements", []):
                if inner.get("tag") == "markdown":
                    out.append(inner["content"])
    return out


def test_uses_markdown_component_not_lark_md():
    """正文必须用 markdown 组件（支持列表/代码块），不能用 div+lark_md"""
    r = CardRenderer("codex")
    s = _new_session()
    s.body_buffer = "- item 1\n- item 2\n```py\nprint(1)\n```"
    card = r.render(s, done=True)
    # 不应再有 div+lark_md
    assert not any(e.get("tag") == "div" for e in card["elements"]), "仍在用 div"
    md = _markdown_texts(card)
    assert any("```py" in m for m in md), "代码块未进入 markdown 组件"


def test_running_card_status():
    r = CardRenderer("codex")
    s = _new_session()
    s.body_buffer = "Hello world"
    card = r.render(s, done=False)
    assert card["header"]["template"] == "blue"
    assert "运行中" in card["header"]["title"]["content"]
    md = _markdown_texts(card)
    assert any("Hello world" in m for m in md)
    assert any("输出中" in m for m in md)


def test_done_card_green():
    r = CardRenderer("claude")
    s = _new_session()
    s.body_buffer = "Final answer"
    card = r.render(s, done=True)
    assert card["header"]["template"] == "green"
    md = _markdown_texts(card)
    assert any(m == "Final answer" for m in md)
    assert not any("输出中" in m for m in md)


def test_tool_calls_rendered():
    r = CardRenderer("claude")
    s = _new_session()
    s.tool_calls = [
        {"id": "1", "name": "Bash", "input": {"command": "ls -la"}, "status": "completed"},
        {"id": "2", "name": "Read", "input": {"file_path": "/tmp/x.py"}, "status": "running"},
        {"id": "3", "name": "Edit", "input": {}, "status": "error", "output": "boom"},
    ]
    s.body_buffer = "done"
    card = r.render(s, done=True)
    joined = "\n".join(_markdown_texts(card))
    assert "`Bash` — ls -la" in joined
    assert "✅" in joined and "⏳" in joined and "❌" in joined
    assert "boom" in joined


def test_thinking_panel_toggle():
    s = _new_session()
    s.thinking_buffer = "let me think..."
    s.body_buffer = "answer"
    on = CardRenderer("codex", show_thinking=True).render(s, done=True)
    assert any(e.get("tag") == "collapsible_panel" for e in on["elements"])
    off = CardRenderer("codex", show_thinking=False).render(s, done=True)
    assert not any(e.get("tag") == "collapsible_panel" for e in off["elements"])


def test_error_card():
    r = CardRenderer("opencode")
    s = _new_session()
    card = r.render(s, error="connection lost")
    assert card["header"]["template"] == "red"
    assert any("connection lost" in m for m in _markdown_texts(card))


def test_done_footer_shows_duration_cost_tools():
    r = CardRenderer("claude")
    s = _new_session()
    s.body_buffer = "ok"
    s.last_cost_usd = 0.0123
    s.tool_calls = [{"id": "1", "name": "Bash", "input": {}, "status": "completed"}]
    card = r.render(s, done=True, elapsed=5.5)
    notes = [e for e in card["elements"] if e.get("tag") == "note"]
    assert notes, "完成卡片应有页脚 note"
    txt = notes[-1]["elements"][0]["content"]
    assert "5.5s" in txt and "$0.0123" in txt and "1 次工具调用" in txt


def test_pagination_marker_on_continuation():
    r = CardRenderer("codex")
    s = _new_session()
    s.card_index = 2
    s.body_buffer = "page 3 content"
    card = r.render(s, done=False)
    assert "(#3)" in card["header"]["title"]["content"]


# ── info_card：命令返回用的轻量卡片 ──

def test_info_card_structure():
    card = info_card("✅ 标题", "正文 **粗体**", template="green")
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "✅ 标题"
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == "正文 **粗体**"


def test_info_card_default_blue():
    card = info_card("标题", "正文")
    assert card["header"]["template"] == "blue"


def test_info_card_footer_adds_hr_and_note():
    card = info_card("T", "body", footer="提示文字")
    tags = [e["tag"] for e in card["elements"]]
    assert tags == ["markdown", "hr", "note"]
    note = card["elements"][-1]
    assert note["elements"][0]["content"] == "提示文字"


def test_info_card_no_footer_single_element():
    card = info_card("T", "body")
    assert len(card["elements"]) == 1


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
