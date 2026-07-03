#!/usr/bin/env bash
#
# Symphony 一键安装脚本
# ---------------------------------------------------------------------------
# 用法（在解压后的 symphony-final 目录里）：
#
#     bash install.sh
#
# 完成后即可在任意位置直接运行：
#
#     symphony
#
# 脚本会依次完成：
#   1. 定位项目根目录
#   2. 检测 / 安装 uv
#   3. 安装 / 修复 bytedcli，并在 Symphony 启动前完成必要登录预热
#   4. 构建 pi（产出 dist/cli.js，rpc 模式所需）
#   5. 用 `uv tool install --editable .` 把 symphony 命令装进 PATH
#   6. 确保 ~/.local/bin 在 PATH 中
# ---------------------------------------------------------------------------

set -euo pipefail

# ── 小工具 ────────────────────────────────────────────────────────────────
info()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

append_path_once() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  if ! printf '%s' ":$PATH:" | grep -q ":$dir:"; then
    export PATH="$dir:$PATH"
  fi
}

persist_path_once() {
  local dir="$1"
  [ -d "$dir" ] || return 0
  local shell_rc="$HOME/.bashrc"
  case "${SHELL:-}" in
    */zsh) shell_rc="$HOME/.zshrc" ;;
  esac
  local line="export PATH=\"$dir:\$PATH\""
  if ! grep -qsF "$line" "$shell_rc" 2>/dev/null; then
    printf '\n# Added by Symphony install.sh\n%s\n' "$line" >> "$shell_rc"
    warn "已把 ${dir} 加入 PATH（写入 ${shell_rc}）。请执行 'source ${shell_rc}' 或重开终端。"
  fi
}

# ── 0. 定位项目根目录（脚本所在目录） ──────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
[ -f pyproject.toml ] || die "未找到 pyproject.toml，请把本脚本放在 symphony-final 根目录再运行。"
info "项目根目录：$SCRIPT_DIR"

# ── 1. 检测 / 安装 uv ──────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  warn "未检测到 uv，正在安装…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv 默认装到 ~/.local/bin
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv 安装失败，请手动安装：https://docs.astral.sh/uv/"
fi
ok "uv：$(uv --version)"

# ── 2. 安装 / 修复 bytedcli（在运行 symphony 之前完成） ───────────────────────
#     后续 pi 的 bash 工具会直接调用 bytedcli；因此这里提前安装、修复 PATH，
#     并尽量把登录流程前置，避免用户在 TUI 里才看到 command not found 或
#     AUTH_REQUIRED。登录需要本人扫码/浏览器确认，脚本只能自动发起并打开二维码。
BNPM_REGISTRY="${BNPM_REGISTRY:-http://bnpm.byted.org}"
BYTEDCLI_PACKAGE="${BYTEDCLI_PACKAGE:-@bytedance-dev/bytedcli@latest}"

if command -v npm >/dev/null 2>&1; then
  NPM_PREFIX="$(npm config get prefix 2>/dev/null || true)"
  NPM_BIN=""
  if [ -n "$NPM_PREFIX" ] && [ "$NPM_PREFIX" != "undefined" ]; then
    NPM_BIN="$NPM_PREFIX/bin"
    append_path_once "$NPM_BIN"
  fi

  if command -v bytedcli >/dev/null 2>&1; then
    ok "bytedcli 已安装：$(command -v bytedcli) ($(bytedcli --version 2>/dev/null || true))"
  else
    info "安装 bytedcli（$BYTEDCLI_PACKAGE）…"
    NPM_CONFIG_REGISTRY="$BNPM_REGISTRY" npm install -g "$BYTEDCLI_PACKAGE"
    append_path_once "$NPM_BIN"

    # npm 全局 bin 在某些环境中不在启动 shell 的 PATH 里。macOS/Homebrew 下
    # 如果 /opt/homebrew/bin 可写，则创建一个稳定软链，保证 symphony/pi 子进程
    # 继承 PATH 后也能找到 bytedcli。
    if ! command -v bytedcli >/dev/null 2>&1 \
       && [ -n "$NPM_BIN" ] \
       && [ -x "$NPM_BIN/bytedcli" ] \
       && [ -d /opt/homebrew/bin ] \
       && [ -w /opt/homebrew/bin ]; then
      ln -sf "$NPM_BIN/bytedcli" /opt/homebrew/bin/bytedcli
      append_path_once /opt/homebrew/bin
    fi

    command -v bytedcli >/dev/null 2>&1 \
      && ok "bytedcli 安装完成：$(command -v bytedcli) ($(bytedcli --version 2>/dev/null || true))" \
      || warn "bytedcli 已尝试安装但当前 shell 仍找不到；请检查 npm prefix/bin 是否在 PATH 中：$NPM_BIN"
  fi

  [ -n "$NPM_BIN" ] && persist_path_once "$NPM_BIN"

  if command -v bytedcli >/dev/null 2>&1; then
    if [ "${SYMPHONY_SKIP_BYTEDCLI_LOGIN:-0}" = "1" ]; then
      warn "已跳过 bytedcli 登录预热（SYMPHONY_SKIP_BYTEDCLI_LOGIN=1）。"
    else
      info "预热 bytedcli Feishu 登录；如弹出二维码/浏览器，请完成授权…"
      # Feishu 登录是幂等的：已有 token 时会复用；未登录时会输出二维码/链接并等待用户授权。
      # BYTEDCLI_OPEN_QR_IMAGE=1 让 macOS 非 TTY 场景自动打开二维码图片。
      BYTEDCLI_OPEN_QR_IMAGE=1 bytedcli --json feishu login --no-terminal-qr \
        && ok "bytedcli Feishu 登录已完成或已有可用授权。" \
        || warn "bytedcli Feishu 登录未完成；之后可手动执行：bytedcli --json feishu login --no-terminal-qr"
    fi
  fi
else
  warn "未检测到 npm，跳过 bytedcli 安装。安装 Node.js/npm 后重新运行 install.sh。"
fi

# ── 3. 构建 pi（最关键的一步：rpc 模式依赖 dist/cli.js） ────────────────────
#     pi 是 npm workspaces monorepo，必须在“仓库根目录”安装依赖并按顺序构建
#     （tui → ai → agent → coding-agent → orchestrator）。单独进 coding-agent
#     构建会因缺少工作区依赖而失败。
#     安装时用 --ignore-scripts 跳过 monorepo 根的 `prepare: husky` 钩子——
#     husky 只在开发（git 仓库）场景需要，终端用户机器上没有它会报
#     `sh: husky: command not found`（npm error code 127）。
PI_ROOT="pi-agent"
PI_DIR="$PI_ROOT/packages/coding-agent"
PI_CLI="$PI_DIR/dist/cli.js"
if [ -f "$PI_CLI" ]; then
  ok "pi 已构建：$PI_CLI"
else
  if command -v npm >/dev/null 2>&1; then
    info "构建 pi（在 monorepo 根安装依赖并整体构建，跳过 husky 钩子）…"
    ( cd "$PI_ROOT" && npm install --ignore-scripts && npm run build )
    [ -f "$PI_CLI" ] && ok "pi 构建完成：$PI_CLI" \
      || warn "pi 构建未产出 dist/cli.js，节点将退化为单次 LLM 调用（可稍后手动构建）。"
  else
    warn "未检测到 npm，跳过 pi 构建。安装 Node.js(>=22) 后在 $PI_ROOT 执行：npm install --ignore-scripts && npm run build。"
  fi
fi

# ── 4. 安装 symphony 命令到 PATH ───────────────────────────────────────────
#     （TUI 现在直接用 pi 原生 + /sop，无需单独的 TypeScript TUI 依赖）
info "安装 symphony 命令（uv tool install --editable .）…"
uv tool install --force --editable .
ok "symphony 命令已安装。"

# ── 5. 确保 ~/.local/bin 在 PATH 中 ────────────────────────────────────────
UV_BIN="$HOME/.local/bin"
if ! printf '%s' ":$PATH:" | grep -q ":$UV_BIN:"; then
  SHELL_RC="$HOME/.bashrc"
  case "${SHELL:-}" in
    */zsh) SHELL_RC="$HOME/.zshrc" ;;
  esac
  LINE='export PATH="$HOME/.local/bin:$PATH"'
  if ! grep -qsF "$LINE" "$SHELL_RC" 2>/dev/null; then
    printf '\n# Added by Symphony install.sh\n%s\n' "$LINE" >> "$SHELL_RC"
    warn "已把 $UV_BIN 加入 PATH（写入 $SHELL_RC）。请执行 'source $SHELL_RC' 或重开终端。"
  fi
fi

# ── 完成提示 ───────────────────────────────────────────────────────────────
echo
ok "安装完成！"
echo
echo "  下一步："
echo "    1) 确认 data/config.toml 里已填好 provider 的 api_key（已填则无需改）"
echo "    2) 在本目录运行： symphony"
echo "       （在其它目录运行需带： symphony --data-dir \"$SCRIPT_DIR/data\"）"
echo
