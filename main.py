#!/usr/bin/env python3
"""Pocket Agent — 入口

用法：
  python main.py          # 启动桥接
  python main.py setup    # 交互式配置向导
"""

import sys
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

_AGENTS = ("claude", "opencode", "codex")

# 桥接核心实际用到的权限（运行最小集）——用于自检与「至少要开这些」校验。
_CORE_SCOPES = [
    "im:message.p2p_msg:readonly",   # 读私聊消息
    "im:message:send_as_bot",        # 发消息
]
_OPTIONAL_SCOPES = [
    "application:application:self_manage",  # 自动识别主人，免手动绑定
]
_REQUIRED_EVENTS = ["im.message.receive_v1"]   # 接收消息
_REQUIRED_CALLBACKS = ["card.action.trigger"]   # 卡片按钮回调（审批等）

# 推荐的「完整权限集」——一次批量导入开齐 im / docs / sheets / base /
# wiki / task / board 等全部能力，省去日后用到新功能再回来加权限。
# 飞书「权限管理 → 批量导入」格式：{"scopes": {"tenant": [...], "user": [...]}}。
_FULL_IMPORT_SCOPES = {
    "tenant": [
        "application:application:self_manage",
        "base:app:copy", "base:app:create", "base:app:read", "base:app:update",
        "base:dashboard:create", "base:dashboard:delete", "base:dashboard:read",
        "base:dashboard:update",
        "base:field:create", "base:field:delete", "base:field:read", "base:field:update",
        "base:form:create", "base:form:delete", "base:form:read", "base:form:update",
        "base:record:create", "base:record:delete", "base:record:read", "base:record:update",
        "base:role:create", "base:role:delete", "base:role:read", "base:role:update",
        "base:table:create", "base:table:delete", "base:table:read", "base:table:update",
        "base:view:read", "base:view:write_only",
        "base:workflow:create", "base:workflow:delete", "base:workflow:read",
        "base:workflow:update",
        "base:workspace:list",
        "board:whiteboard:node:create", "board:whiteboard:node:delete",
        "board:whiteboard:node:read",
        "cardkit:card:read", "cardkit:card:write",
        "contact:contact.base:readonly", "contact:user.base:readonly",
        "docs:document.comment:create", "docs:document.comment:delete",
        "docs:document.comment:read", "docs:document.comment:update",
        "docs:document.comment:write_only",
        "docs:document.media:download", "docs:document.media:upload",
        "docs:document:copy", "docs:document:export", "docs:event:subscribe",
        "docs:permission.member:auth", "docs:permission.member:create",
        "docs:permission.member:transfer",
        "docx:document:create", "docx:document:readonly", "docx:document:write_only",
        "drive:drive.metadata:readonly", "drive:file:download", "drive:file:upload",
        "im:chat.members:read", "im:chat.members:write_only",
        "im:chat:create", "im:chat:read", "im:chat:update",
        "im:message", "im:message.group_at_msg:readonly", "im:message.group_msg",
        "im:message.p2p_msg:readonly", "im:message.pins:read", "im:message.pins:write_only",
        "im:message.reactions:read", "im:message.reactions:write_only",
        "im:message:readonly", "im:message:send_as_bot", "im:resource",
        "sheets:spreadsheet.meta:read", "sheets:spreadsheet.meta:write_only",
        "sheets:spreadsheet:create", "sheets:spreadsheet:read", "sheets:spreadsheet:write_only",
        "task:comment:write", "task:task:read", "task:task:write",
        "task:tasklist:read", "task:tasklist:write",
        "wiki:node:copy", "wiki:node:create", "wiki:node:read", "wiki:node:retrieve",
        "wiki:node:update", "wiki:space:read", "wiki:space:retrieve", "wiki:wiki:readonly",
    ],
    "user": [
        "base:app:copy", "base:app:create", "base:app:read", "base:app:update",
        "base:dashboard:create", "base:dashboard:delete", "base:dashboard:read",
        "base:dashboard:update",
        "base:field:create", "base:field:delete", "base:field:read", "base:field:update",
        "base:form:create", "base:form:delete", "base:form:read", "base:form:update",
        "base:record:create", "base:record:delete", "base:record:read", "base:record:update",
        "base:role:create", "base:role:delete", "base:role:read", "base:role:update",
        "base:table:create", "base:table:delete", "base:table:read", "base:table:update",
        "base:view:read", "base:view:write_only",
        "base:workflow:create", "base:workflow:delete", "base:workflow:read",
        "base:workflow:update",
        "base:workspace:list",
        "board:whiteboard:node:create", "board:whiteboard:node:delete",
        "board:whiteboard:node:read",
        "contact:user.base:readonly", "contact:user.basic_profile:readonly",
        "contact:user:search",
        "docs:document.comment:create", "docs:document.comment:delete",
        "docs:document.comment:read", "docs:document.comment:update",
        "docs:document.comment:write_only",
        "docs:document.media:download", "docs:document.media:upload",
        "docs:document:copy", "docs:document:export", "docs:event:subscribe",
        "docs:permission.member:auth", "docs:permission.member:create",
        "docs:permission.member:transfer",
        "docx:document:create", "docx:document:readonly", "docx:document:write_only",
        "drive:drive.metadata:readonly", "drive:file:download", "drive:file:upload",
        "im:chat.members:read", "im:chat.members:write_only",
        "im:chat:read", "im:chat:update",
        "im:message.group_msg:get_as_user", "im:message.p2p_msg:get_as_user",
        "im:message.pins:read", "im:message.pins:write_only",
        "im:message.reactions:read", "im:message.reactions:write_only",
        "im:message:readonly",
        "mail:user_mailbox:readonly", "offline_access",
        "sheets:spreadsheet.meta:read", "sheets:spreadsheet.meta:write_only",
        "sheets:spreadsheet:create", "sheets:spreadsheet:read", "sheets:spreadsheet:write_only",
        "task:comment:write", "task:task:read", "task:task:write",
        "task:tasklist:read", "task:tasklist:write",
        "vc:meeting.meetingevent:read", "vc:meeting.search:read", "vc:note:read",
        "wiki:node:copy", "wiki:node:create", "wiki:node:read", "wiki:node:retrieve",
        "wiki:node:update", "wiki:space:read", "wiki:space:retrieve",
        "wiki:space:write_only", "wiki:wiki:readonly",
    ],
}


def _scopes_import_json() -> str:
    """生成飞书「权限管理 → 批量导入」可直接粘贴的完整权限 JSON。"""
    return json.dumps({"scopes": _FULL_IMPORT_SCOPES},
                      indent=2, ensure_ascii=False)


def _open_link(url: str):
    """打印一个（多数终端可点击的）链接"""
    print(f"  🔗 {C.link(url)}")


# ── 终端配色（无 tty / NO_COLOR 时自动降级为无色）──
class _Palette:
    def __init__(self):
        import os
        enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self._on = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self._on else s

    def bold(self, s):   return self._w("1", s)
    def dim(self, s):    return self._w("2", s)
    def red(self, s):    return self._w("31", s)
    def green(self, s):  return self._w("32", s)
    def yellow(self, s): return self._w("33", s)
    def blue(self, s):   return self._w("34", s)
    def cyan(self, s):   return self._w("36", s)
    def link(self, s):   return self._w("4;36", s)        # 下划线青
    def title(self, s):  return self._w("1;36", s)        # 粗体青
    def key(self, s):    return self._w("1;33", s)         # 粗体黄：重点项

C = _Palette()


def _h1(num: str, text: str):
    """带分隔线的步骤大标题"""
    bar = "━" * 54
    print(C.cyan(bar))
    print(f"{C.title(num)} {C.bold(text)}")
    print(C.cyan(bar))


def _ok(msg: str):    print(f"  {C.green('✅ ' + msg)}")
def _warn(msg: str):  print(f"  {C.yellow('⚠️  ' + msg)}")
def _info(msg: str):  print(f"  {C.blue('ℹ️  ' + msg)}")
def _hint(msg: str):  print(f"  {C.dim('💡 ' + msg)}")


def cmd_setup():
    """交互式配置向导：建应用引导 → 填凭证(当场验证) → 选 agent → 扫脸绑定 → 保存启动"""
    print(f"\n{C.title('🤖 Pocket Agent — 配置向导')}\n")

    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        print(C.dim(f"  找到已有配置：{CONFIG_PATH}（回车保留原值）\n"))

    # ── 第 1 步：创建飞书应用（平台要求人工创建，无法跳过，这里讲到最细）──
    _h1("①", "创建飞书应用（约 2 分钟，只需一次）")
    print(f"  在飞书开放平台按下面几步操作（{C.bold('蓝色')}为入口，{C.key('黄色')}为要复制/勾选的重点）：")
    print()
    print(f"  {C.bold('1)')} 打开开放平台 → 创建{C.bold('企业自建应用')}：")
    _open_link("https://open.feishu.cn/app")
    print(f"  {C.bold('2)')} 应用能力 → 添加 {C.key('「机器人」')}")
    print()
    print(f"  {C.bold('3)')} 权限管理 → {C.key('「批量导入」')} → 把下面整段 JSON 粘进去（{C.bold('一次开齐全部能力')}）：")
    print(C.dim("  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ 复制以下 JSON ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"))
    print(C.green(_scopes_import_json()))
    print(C.dim("  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"))
    print(C.dim("     含 im / docs / sheets / base / wiki / task 等全部权限；"))
    print(C.dim("     本程序核心只需 im 那几项，多开的为日后扩展功能预留，可按需删减。"))
    print()
    print(f"  {C.bold('4)')} 事件与回调 → 订阅方式 → 选 {C.key('「使用长连接接收事件」')}")
    print(f"  {C.bold('5)')} 事件与回调 → 添加事件：{C.key('  '.join(_REQUIRED_EVENTS))}")
    print(f"                 添加回调：{C.key('  '.join(_REQUIRED_CALLBACKS))}")
    print(f"  {C.bold('6)')} 版本管理与发布 → 创建版本并{C.bold('发布')}（企业内可用即可）")
    print()
    print(f"  完成后在 {C.key('「凭证与基础信息」')} 页可看到 {C.key('App ID')} / {C.key('App Secret')}。")
    print()

    # ── 第 2 步：填凭证 + 当场验证 ──
    _h1("②", "填入凭证（当场验证有效性）")
    # 快捷方式：从飞书后台整段复制粘贴，程序自动拆出 App ID / Secret
    _hint(f"可直接从 {C.key('「凭证与基础信息」')} 页整段复制粘贴，自动识别；或留空回车，逐栏手动输入。")
    pasted = input(f"  {C.bold('粘贴在此')}（含 App ID 和 Secret 的任意文本）: ").strip()
    if pasted:
        pid, psec = _parse_credentials(pasted)
        if pid:
            config['feishu_app_id'] = pid
            print(f"  {C.green('✓')} 识别到 App ID：{C.cyan(pid)}")
        if psec:
            config['feishu_app_secret'] = psec
            print(f"  {C.green('✓')} 识别到 App Secret：{C.cyan(psec[:6] + '…')}")
        if not (pid and psec):
            _warn("未能全部识别，下面补齐缺的部分")

    while True:
        app_id = input(f"  App ID [{C.dim(config.get('feishu_app_id', ''))}]: ").strip()
        if app_id:
            config['feishu_app_id'] = app_id
        if not config.get('feishu_app_id'):
            print(f"  {C.red('❌ App ID 不能为空')}"); continue

        secret_shown = (config.get('feishu_app_secret', '')[:6] + "…") if config.get('feishu_app_secret') else ""
        app_secret = input(f"  App Secret [{C.dim(secret_shown)}]: ").strip()
        if app_secret:
            config['feishu_app_secret'] = app_secret
        if not config.get('feishu_app_secret'):
            print(f"  {C.red('❌ App Secret 不能为空')}"); continue

        print(C.dim("\n  正在验证凭证…"))
        ok, msg = _verify_feishu(config['feishu_app_id'], config['feishu_app_secret'])
        if ok:
            _ok(msg); break
        print(f"  {C.red('❌ ' + msg)}")
        retry = input("  凭证有误，重新输入？(Y/n): ").strip().lower()
        if retry == 'n':
            break

    # ── 第 3 步：选 agent ──
    print()
    _h1("③", "选择 agent")
    cur = config.get('agent', 'claude')
    print(f"  可选：{C.cyan(' / '.join(_AGENTS))}")
    agent = input(f"  使用哪个 agent？[{C.dim(cur)}]: ").strip().lower()
    config['agent'] = agent if agent in _AGENTS else cur
    print(f"  → {C.green(config['agent'])}")

    if config['agent'] in ('claude', 'opencode'):
        wd = input(f"  工作目录 workdir [{config.get('workdir', '.')}]: ").strip()
        config['workdir'] = wd or config.get('workdir', '.')
        if config['agent'] == 'opencode':
            m = input(f"  opencode 模型 provider/model [{config.get('opencode_model', '')}]: ").strip()
            if m:
                config['opencode_model'] = m
    elif config['agent'] == 'codex':
        ws = input(f"  Codex WebSocket URL [{config.get('codex_ws_url', 'ws://127.0.0.1:5123')}]: ").strip()
        config['codex_ws_url'] = ws or config.get('codex_ws_url', 'ws://127.0.0.1:5123')

    # ── 第 4 步：绑定使用者（自动识别主人，失败回退发消息）──
    print()
    _h1("④", "绑定使用者（开箱只有你能用，更安全）")
    if config.get('feishu_app_id') and config.get('feishu_app_secret'):
        print(C.dim("  正在尝试自动识别主人（需已开通 self_manage 权限）…"))
        oid = _resolve_owner(config['feishu_app_id'], config['feishu_app_secret'])
        if oid:
            config['bot_owner'] = oid
            config['allowed_users'] = oid
            _ok(f"已自动识别主人：{C.cyan(oid)}（无需发消息）")
        else:
            _info("未能自动识别（多半是没开 self_manage 权限）。")
            do_bind = input("  改用「发消息绑定」？启动后给机器人发条消息即可 (Y/n): ").strip().lower()
            if do_bind != 'n':
                oid = _capture_open_id(config['feishu_app_id'], config['feishu_app_secret'])
                if oid:
                    config['bot_owner'] = oid
                    config['allowed_users'] = oid
                    _ok(f"已绑定：{C.cyan(oid)}")
                else:
                    _warn("跳过绑定（allowed_users 留空 = 不限制，谁都能用）")
    else:
        print(C.dim("  （凭证未就绪，跳过）"))

    # ── 保存 ──
    print()
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    _ok(f"配置已保存到 {C.cyan(str(CONFIG_PATH))}")

    # ── 启动 ──
    print()
    start = input(f"  {C.bold('现在启动？')} (Y/n): ").strip().lower()
    if start != 'n':
        from pocket_agent.app import run
        run(str(CONFIG_PATH))


def _parse_credentials(text: str) -> tuple[str, str]:
    """从粘贴的任意文本里抽出 (app_id, app_secret)；抽不到的位置返回 ""。

    兼容飞书后台常见的几种复制形态：
      "App ID  cli_abc123\nApp Secret  Xy9..."   带标签
      "cli_abc123  Xy9z..."                       裸两段
      多行、中英文标签、冒号/空格分隔都尽量识别。
    策略：App ID 用 `cli_` 前缀强特征锁定；Secret 优先取标签后的串，
    否则取「不是 app_id 的那段较长字母数字」。
    """
    import re

    app_id = ""
    app_secret = ""

    # 1) App ID：飞书自建应用 ID 形如 cli_xxxxxxxx
    m = re.search(r"\bcli_[A-Za-z0-9]+\b", text)
    if m:
        app_id = m.group(0)

    # 2) App Secret：先找显式标签（secret / 密钥）后面的串
    m = re.search(r"(?:app\s*secret|secret|密钥)\W*([A-Za-z0-9]{16,})",
                  text, re.IGNORECASE)
    if m and m.group(1) != app_id:
        app_secret = m.group(1)

    # 3) 兜底：取所有「长字母数字串」里第一个不等于 app_id 的
    if not app_secret:
        for tok in re.findall(r"\b[A-Za-z0-9]{16,}\b", text):
            if tok != app_id and not tok.startswith("cli_"):
                app_secret = tok
                break

    return app_id, app_secret


def _verify_feishu(app_id: str, app_secret: str) -> tuple[bool, str]:
    """用 app_id/secret 换 tenant_access_token，验证凭证是否有效。"""
    import asyncio

    async def _check():
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                ) as resp:
                    data = await resp.json()
        except Exception as e:
            return False, f"网络错误：{e}"
        if data.get("tenant_access_token"):
            return True, "凭证有效，飞书连接成功"
        # 飞书会返回具体错误码，原样透出便于排查
        return False, f"凭证无效：code={data.get('code')} {data.get('msg', '')}"

    return asyncio.run(_check())


def _resolve_owner(app_id: str, app_secret: str) -> str:
    """自动解析应用所有者的 open_id（同步封装）；失败返回 ""。

    复用 FeishuAPI.get_app_owner（最佳努力，未开 self_manage 权限时返回 ""）。
    """
    import asyncio

    async def _go():
        from pocket_agent.feishu_client import FeishuAPI
        api = FeishuAPI(app_id, app_secret)
        try:
            return await api.get_app_owner()
        finally:
            await api.close()

    try:
        return asyncio.run(_go())
    except Exception:
        return ""


def _capture_open_id(app_id: str, app_secret: str, timeout: int = 120) -> str:
    """启动飞书长连接，等用户发来第一条消息，抓取其 open_id 后返回。

    这样用户无需自己去查 open_id —— 在飞书里发条消息即完成绑定。
    """
    print("\n  ⏳ 正在连接飞书…请在飞书里给机器人发任意一条消息（最多等 2 分钟）")
    try:
        import lark_oapi as lark
    except ImportError:
        print("  ⚠️  未安装 lark-oapi，无法自动绑定。请先 `uv sync`。")
        return ""

    import threading
    captured = {"open_id": ""}
    done = threading.Event()

    def on_msg(data):
        try:
            oid = data.event.sender.sender_id.open_id
            if oid:
                captured["open_id"] = oid
                done.set()
        except Exception:
            pass

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_msg)
               .build())
    cli = lark.ws.Client(app_id, app_secret, event_handler=handler,
                         log_level=lark.LogLevel.ERROR)

    t = threading.Thread(target=cli.start, daemon=True)
    t.start()
    done.wait(timeout=timeout)
    return captured["open_id"]


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        cmd_setup()
    else:
        config_path = sys.argv[1] if len(sys.argv) > 1 else str(CONFIG_PATH)
        from pocket_agent.app import run
        run(config_path)


if __name__ == "__main__":
    main()
