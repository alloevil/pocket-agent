#!/usr/bin/env sh
# Pocket Agent 一键安装脚本
#
#   curl -LsSf https://raw.githubusercontent.com/alloevil/pocket-agent/main/install.sh | sh
#
# 做的事：检测/安装 uv → 克隆（或更新）仓库 → uv sync 装依赖 → 进入配置向导。
# 安全：本脚本简短可读，介意 curl|sh 的用户可先下载查看再运行。

set -eu

REPO_URL="https://github.com/alloevil/pocket-agent.git"
INSTALL_DIR="${POCKET_AGENT_DIR:-$HOME/pocket-agent}"

# ── 终端着色（无 tty 时降级为无色）──
if [ -t 1 ]; then
  B="$(printf '\033[1m')"; G="$(printf '\033[32m')"; Y="$(printf '\033[33m')"
  R="$(printf '\033[31m')"; N="$(printf '\033[0m')"
else
  B=""; G=""; Y=""; R=""; N=""
fi
info() { printf "%s▸%s %s\n" "$G" "$N" "$1"; }
warn() { printf "%s!%s %s\n" "$Y" "$N" "$1"; }
die()  { printf "%s✗%s %s\n" "$R" "$N" "$1" >&2; exit 1; }

printf "\n%s🤖 Pocket Agent 安装%s\n\n" "$B" "$N"

# ── 1. 前置检查 ──
command -v git >/dev/null 2>&1 || die "未找到 git，请先安装 git 后重试。"
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  die "需要 curl 或 wget 来下载 uv。"
fi

# ── 2. 安装 uv（若缺）──
if command -v uv >/dev/null 2>&1; then
  info "uv 已安装：$(uv --version)"
else
  info "安装 uv（Astral 官方脚本）…"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    wget -qO- https://astral.sh/uv/install.sh | sh
  fi
  # uv 默认装到 ~/.local/bin，当前 shell 的 PATH 可能还没包含它
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv 安装后仍未找到，请重开终端或把 ~/.local/bin 加入 PATH。"
  info "uv 安装完成：$(uv --version)"
fi

# ── 3. 克隆或更新仓库（幂等：已存在则 pull）──
if [ -d "$INSTALL_DIR/.git" ]; then
  info "仓库已存在，更新中：$INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only || warn "git pull 未成功（可能有本地改动），继续使用现有代码。"
elif [ -e "$INSTALL_DIR" ]; then
  die "$INSTALL_DIR 已存在但不是 git 仓库，请删除或设置 POCKET_AGENT_DIR 指向别处。"
else
  info "克隆仓库到：$INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 4. 安装依赖 ──
info "安装依赖（uv sync）…"
uv sync

# ── 5. 进入配置向导 ──
printf "\n%s✅ 环境就绪%s，进入配置向导…\n\n" "$G" "$N"
# 管道方式（curl|sh）下 stdin 不是终端，向导需要交互输入 → 重定向到 /dev/tty
if [ -t 0 ]; then
  exec uv run python main.py setup
elif [ -e /dev/tty ]; then
  exec uv run python main.py setup < /dev/tty
else
  warn "非交互环境，跳过向导。稍后手动运行："
  printf "    cd %s && uv run python main.py setup\n" "$INSTALL_DIR"
fi
