"""claude_sessions 模块测试 — 造临时 ~/.claude/projects 结构，验证列举/摘要/容错"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pocket_agent import claude_sessions as cs


def _write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")


def _user_line(text, cwd="/work/proj"):
    return {"type": "user", "cwd": cwd, "timestamp": "2026-06-04T00:00:00Z",
            "message": {"content": text}}


def test_project_dir_for_encoding():
    d = cs.project_dir_for("/home/gaoruilin/pocket-agent")
    assert d.endswith("-home-gaoruilin-pocket-agent")


def test_list_sessions_sorted_and_summary():
    with tempfile.TemporaryDirectory() as root:
        proj = cs.project_dir_for("/work/proj")
        # 把 PROJECTS_ROOT 指到临时目录
        cs.PROJECTS_ROOT = root
        proj = os.path.join(root, "-work-proj")
        os.makedirs(proj)
        _write_jsonl(os.path.join(proj, "aaaa1111.jsonl"), [_user_line("第一个任务")])
        _write_jsonl(os.path.join(proj, "bbbb2222.jsonl"), [_user_line("第二个任务")])
        # 让 bbbb 更新（更晚 mtime）
        os.utime(os.path.join(proj, "bbbb2222.jsonl"), (9e9, 9e9))
        sessions = cs.list_sessions("/work/proj")
        assert len(sessions) == 2
        assert sessions[0]["id"] == "bbbb2222"   # 最新在前
        assert "第二个任务" in sessions[0]["summary"]


def test_list_sessions_missing_dir():
    cs.PROJECTS_ROOT = "/no/such/root/xyz"
    assert cs.list_sessions("/whatever") == []


def test_summary_tolerates_bad_lines():
    with tempfile.TemporaryDirectory() as root:
        cs.PROJECTS_ROOT = root
        proj = os.path.join(root, "-work-proj")
        os.makedirs(proj)
        p = os.path.join(proj, "cccc3333.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("not json at all\n")
            f.write(json.dumps({"type": "queue-operation"}) + "\n")  # 非消息
            f.write(json.dumps(_user_line("真正的指令")) + "\n")
        sessions = cs.list_sessions("/work/proj")
        assert sessions[0]["summary"] == "真正的指令"


def test_summary_no_user_message():
    with tempfile.TemporaryDirectory() as root:
        cs.PROJECTS_ROOT = root
        proj = os.path.join(root, "-work-proj")
        os.makedirs(proj)
        _write_jsonl(os.path.join(proj, "dddd4444.jsonl"),
                     [{"type": "queue-operation"}])
        sessions = cs.list_sessions("/work/proj")
        assert sessions[0]["summary"] == "(无摘要)"


def test_session_exists():
    with tempfile.TemporaryDirectory() as root:
        cs.PROJECTS_ROOT = root
        proj = os.path.join(root, "-work-proj")
        os.makedirs(proj)
        _write_jsonl(os.path.join(proj, "eeee5555.jsonl"),
                     [_user_line("x", cwd="/work/proj")])
        hit = cs.session_exists("/work/proj", "eeee5555")
        assert hit and hit["id"] == "eeee5555" and hit["cwd"] == "/work/proj"
        assert cs.session_exists("/work/proj", "nonexist") is None


def test_list_all_grouped():
    with tempfile.TemporaryDirectory() as root:
        cs.PROJECTS_ROOT = root
        for proj_name in ("-work-a", "-work-b"):
            p = os.path.join(root, proj_name)
            os.makedirs(p)
            for i in range(3):
                _write_jsonl(os.path.join(p, f"s{i}.jsonl"), [_user_line(f"任务{i}")])
        groups = cs.list_all_grouped(limit_per=2, max_projects=5)
        assert len(groups) == 2
        assert all(len(g["sessions"]) == 2 for g in groups)   # 每组限 2


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
