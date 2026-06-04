"""读取 Claude Code 本地历史会话。

Claude CLI 把每个项目的对话存成 jsonl：
  ~/.claude/projects/<cwd编码>/<session_id>.jsonl
其中文件名即 session_id，cwd 编码＝绝对路径的 `/` 和 `.` 替换为 `-`。

本模块只读文件系统、不依赖网络，便于让飞书 /history 列出这些历史会话、
/resume <UUID> 续接其中任意一个（claude 后端 --resume <session_id>）。
"""

import json
import os
import time

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")


def project_dir_for(workdir: str) -> str:
    """workdir 绝对路径 → 对应的 ~/.claude/projects/<编码> 目录路径。"""
    enc = os.path.abspath(workdir).replace("/", "-").replace(".", "-")
    return os.path.join(PROJECTS_ROOT, enc)


def _summary_of(jsonl_path: str, max_lines: int = 60) -> tuple[str, str]:
    """读 jsonl 前若干行，返回 (首条 user 文本摘要, cwd)；失败返回 ('', '')。

    只读前 max_lines 行避免大文件全读；容错跳过坏行/非消息行。
    """
    summary = ""
    cwd = ""
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines and summary:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if not summary and d.get("type") == "user":
                    msg = d.get("message", {})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
                    if isinstance(content, str) and content.strip():
                        summary = content.strip().replace("\n", " ")[:50]
    except OSError:
        return "", ""
    return summary, cwd


def _rel_time(epoch: float, now: float | None = None) -> str:
    """把时间戳格式化成相对时间（刚刚 / N分钟前 / N小时前 / N天前）。"""
    now = now if now is not None else time.time()
    diff = max(0, int(now - epoch))
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60}分钟前"
    if diff < 86400:
        return f"{diff // 3600}小时前"
    return f"{diff // 86400}天前"


def list_sessions(workdir: str, limit: int = 15, now: float | None = None) -> list[dict]:
    """列当前 workdir 的历史 session，按修改时间倒序。

    返回 [{id, mtime, rel, summary, cwd}]。目录不存在则返回 []。
    """
    d = project_dir_for(workdir)
    if not os.path.isdir(d):
        return []
    entries = []
    for name in os.listdir(d):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        entries.append((mtime, name[:-len(".jsonl")], path))
    entries.sort(key=lambda e: e[0], reverse=True)
    out = []
    for mtime, sid, path in entries[:limit]:
        summary, cwd = _summary_of(path)
        out.append({
            "id": sid,
            "mtime": mtime,
            "rel": _rel_time(mtime, now),
            "summary": summary or "(无摘要)",
            "cwd": cwd or workdir,
        })
    return out


def list_all_grouped(limit_per: int = 5, max_projects: int = 20,
                     now: float | None = None) -> list[dict]:
    """按项目分组列全部历史 session，项目按最近活跃倒序。

    返回 [{project, dir, sessions:[{id, rel, summary}...]}]。
    """
    if not os.path.isdir(PROJECTS_ROOT):
        return []
    projects = []
    for name in os.listdir(PROJECTS_ROOT):
        pdir = os.path.join(PROJECTS_ROOT, name)
        if not os.path.isdir(pdir):
            continue
        files = []
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(pdir, fn)
            try:
                files.append((os.path.getmtime(fp), fn[:-len(".jsonl")], fp))
            except OSError:
                continue
        if not files:
            continue
        files.sort(key=lambda e: e[0], reverse=True)
        latest = files[0][0]
        sessions = []
        for mtime, sid, fp in files[:limit_per]:
            summary, _ = _summary_of(fp)
            sessions.append({"id": sid, "rel": _rel_time(mtime, now),
                             "summary": summary or "(无摘要)"})
        projects.append({"project": name, "dir": pdir,
                         "latest": latest, "sessions": sessions})
    projects.sort(key=lambda p: p["latest"], reverse=True)
    return projects[:max_projects]


def session_exists(workdir: str, session_id: str) -> dict | None:
    """校验某 session_id 是否存在于当前 workdir 的历史里；返回 {id, cwd} 或 None。"""
    path = os.path.join(project_dir_for(workdir), f"{session_id}.jsonl")
    if not os.path.isfile(path):
        return None
    _, cwd = _summary_of(path)
    return {"id": session_id, "cwd": cwd or os.path.abspath(workdir)}
