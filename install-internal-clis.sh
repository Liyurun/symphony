#!/usr/bin/env bash
#
# Symphony 内部 CLI + Skills 初始化脚本
# -----------------------------------------------------------------------------
# 用法：
#   bash install-internal-clis.sh
#
# 作用：
#   1. 安装常用公司内部 CLI：
#      ncmdcli / dleap-cli / LeapCLI / bytedcli / dataleap-cli / feishu-cli
#   2. 使用 bytedcli 把内部通用 skills 安装到 ~/.agents/skills/。
#      Pi/Symphony 会自动扫描该目录，无需手工复制到项目内。
#   3. 尽量预热登录。若 CLI 输出二维码/图片路径，本脚本会在普通终端里直接
#      渲染二维码；在 Symphony TUI 内也会由 TUI 渲染二维码。
#
# 可选环境变量：
#   BNPM_REGISTRY=https://bnpm.byted.org
#   SYMPHONY_SKIP_INTERNAL_LOGIN=1      # 跳过登录预热
#   SYMPHONY_INSTALL_ALL_BYTED_SKILLS=1 # 安装 bytedcli 打包的全部内部 skills
#
# 如果某个 CLI 的内部 npm 包名发生变化，可以通过下面变量覆盖候选包名：
#   NCMDCLI_UV_SPEC="git+ssh://git@code.byted.org/ad/smb_cli.git"
#   DLEAP_CLI_PACKAGES="pkg-a pkg-b"
#   LEAPCLI_PACKAGES="pkg-a pkg-b"
#   DATALEAP_CLI_PACKAGES="pkg-a pkg-b"
#   FEISHU_CLI_PACKAGES="pkg-a pkg-b"
# -----------------------------------------------------------------------------

set -u -o pipefail

info()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; }

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
    printf '\n# Added by Symphony internal CLI installer\n%s\n' "$line" >> "$shell_rc"
    warn "已把 $dir 写入 PATH（$shell_rc）。当前终端已临时生效；新终端请 source 或重开。"
  fi
}

command_exists_any() {
  local bin
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 && return 0
  done
  return 1
}

first_existing_command() {
  local bin
  for bin in "$@"; do
    if command -v "$bin" >/dev/null 2>&1; then
      command -v "$bin"
      return 0
    fi
  done
  return 1
}

render_qr_image() {
  local path="$1"
  [ -n "$path" ] && [ -f "$path" ] || return 0
  if ! command -v python3 >/dev/null 2>&1; then
    warn "检测到二维码图片，但当前没有 python3，无法在终端内渲染：$path"
    return 0
  fi

  python3 - "$path" <<'PY'
import os
import shutil
import struct
import sys
import zlib

path = sys.argv[1]

def read_png_pixels(p):
    raw = open(p, "rb").read()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a png")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette = []
    idat = []
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        chunk = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filter, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            palette = [(chunk[i], chunk[i + 1], chunk[i + 2], 255) for i in range(0, len(chunk), 3)]
        elif ctype == b"tRNS" and palette:
            for i, alpha in enumerate(chunk):
                if i < len(palette):
                    r, g, b, _ = palette[i]
                    palette[i] = (r, g, b, alpha)
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if not all(v is not None for v in (width, height, bit_depth, color_type, interlace)):
        raise ValueError("invalid png")
    if bit_depth != 8 or interlace != 0:
        raise ValueError("unsupported png")
    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type.get(color_type)
    if not channels:
        raise ValueError("unsupported png color type")
    data = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows = []
    prev = bytearray(stride)
    idx = 0
    bpp = channels
    for _ in range(height):
        ftype = data[idx]
        idx += 1
        scan = bytearray(data[idx:idx + stride])
        idx += stride
        recon = bytearray(stride)
        for i, x in enumerate(scan):
            left = recon[i - bpp] if i >= bpp else 0
            up = prev[i]
            up_left = prev[i - bpp] if i >= bpp else 0
            if ftype == 0:
                val = x
            elif ftype == 1:
                val = x + left
            elif ftype == 2:
                val = x + up
            elif ftype == 3:
                val = x + ((left + up) // 2)
            elif ftype == 4:
                pred_raw = left + up - up_left
                pa, pb, pc = abs(pred_raw - left), abs(pred_raw - up), abs(pred_raw - up_left)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                val = x + pred
            else:
                raise ValueError("unsupported png filter")
            recon[i] = val & 0xFF
        rows.append(bytes(recon))
        prev = recon
    pixels = []
    for row in rows:
        for x in range(width):
            off = x * channels
            if color_type == 0:
                g = row[off]
                pixels.append((g, g, g, 255))
            elif color_type == 2:
                pixels.append((row[off], row[off + 1], row[off + 2], 255))
            elif color_type == 3:
                idx_color = row[off]
                pixels.append(palette[idx_color] if idx_color < len(palette) else (0, 0, 0, 255))
            elif color_type == 4:
                g, a = row[off], row[off + 1]
                pixels.append((g, g, g, a))
            elif color_type == 6:
                pixels.append((row[off], row[off + 1], row[off + 2], row[off + 3]))
    return width, height, pixels

def main():
    width, height, pixels = read_png_pixels(path)
    term_width = shutil.get_terminal_size((100, 24)).columns

    def pixel_is_dark(pixel):
        r, g, b, a = pixel
        if a < 32:
            return False
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 180

    def estimate_module_size():
        # Login QR PNGs are normally crisp bitmaps: every QR module is N x N
        # source pixels.  If we resize to an arbitrary terminal width, adjacent
        # modules can be merged and the terminal QR becomes unscannable while the
        # original PNG remains valid.  Infer N from run lengths and render the
        # exact module grid instead.
        binary = [pixel_is_dark(p) for p in pixels]
        runs = []
        row_step = max(1, height // 96)
        for y in range(0, height, row_step):
            row = binary[y * width:(y + 1) * width]
            run = 1
            for x in range(1, width):
                if row[x] == row[x - 1]:
                    run += 1
                else:
                    if run >= 2:
                        runs.append(run)
                    run = 1
            if run >= 2:
                runs.append(run)
        col_step = max(1, width // 96)
        for x in range(0, width, col_step):
            run = 1
            prev = binary[x]
            for y in range(1, height):
                cur = binary[y * width + x]
                if cur == prev:
                    run += 1
                else:
                    if run >= 2:
                        runs.append(run)
                    run = 1
                    prev = cur
            if run >= 2:
                runs.append(run)
        if not runs:
            return None
        max_candidate = max(1, min(96, width // 16, height // 16))
        best_size = 0
        best_score = -1.0
        for size in range(2, max_candidate + 1):
            modules_x = round(width / size)
            modules_y = round(height / size)
            if not (16 <= modules_x <= 185 and 16 <= modules_y <= 185):
                continue
            ok = 0
            for run in runs:
                remainder = run % size
                distance = min(remainder, size - remainder)
                if distance <= max(1, int(size * 0.18)):
                    ok += 1
            score = ok / len(runs)
            if score > best_score or (abs(score - best_score) < 0.02 and size > best_size):
                best_score = score
                best_size = size
        if best_size >= 2 and best_score >= 0.55:
            return best_size
        return None

    module_size = estimate_module_size()
    if module_size:
        target_w = max(1, round(width / module_size))
        target_h = max(1, round(height / module_size))
        # Prefer preserving every QR module.  Use two spaces when the terminal is
        # wide enough for near-square modules; otherwise use one cell per module
        # rather than downsampling and corrupting the code.
        cell_width = 2 if target_w * 2 <= max(20, term_width - 2) else 1
        quiet = 0

        def is_dark(tx, ty):
            sx = min(width - 1, max(0, int((tx + 0.5) * module_size)))
            sy = min(height - 1, max(0, int((ty + 0.5) * module_size)))
            return pixel_is_dark(pixels[sy * width + sx])
    else:
        target_w = max(16, min(width, 56, max(16, (term_width - 12) // 2)))
        target_h = max(16, round(height * target_w / width))
        cell_width = 2
        quiet = 2

        def is_dark(tx, ty):
            sx = min(width - 1, int((tx + 0.5) * width / target_w))
            sy = min(height - 1, int((ty + 0.5) * height / target_h))
            return pixel_is_dark(pixels[sy * width + sx])

    if target_w * cell_width > term_width:
        print("\033[33m[!] 当前终端较窄，二维码可能换行；建议把窗口拉宽后再扫码。\033[0m", file=sys.stderr)

    def dark_cell():
        return "\033[40m" + (" " * cell_width)

    def light_cell():
        return "\033[47m" + (" " * cell_width)

    print("\033[90m↳ 可直接扫描下方二维码：\033[0m")
    for y in range(-quiet, target_h + quiet):
        chars = []
        for x in range(-quiet, target_w + quiet):
            dark = 0 <= x < target_w and 0 <= y < target_h and is_dark(x, y)
            chars.append(dark_cell() if dark else light_cell())
        print("".join(chars) + "\033[0m")

try:
    main()
except Exception as exc:
    print(f"\033[33m[!] 检测到二维码图片，但终端渲染失败：{exc}; 文件：{path}\033[0m", file=sys.stderr)
PY
}

run_with_qr_render() {
  local last_qr_path=""
  set +e
  "$@" 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"
    local qr_path=""
    if [[ "$line" =~ QR[[:space:]]code[[:space:]]saved[[:space:]]to:[[:space:]](.+\.png) ]]; then
      qr_path="${BASH_REMATCH[1]}"
    elif [[ "$line" =~ 二维码.*(:|：)[[:space:]]*(/.+\.png) ]]; then
      qr_path="${BASH_REMATCH[2]}"
    fi
    qr_path="${qr_path%$'\r'}"
    if [ -n "$qr_path" ] && [ "$qr_path" != "$last_qr_path" ]; then
      last_qr_path="$qr_path"
      render_qr_image "$qr_path"
    fi
  done
  local status=${PIPESTATUS[0]}
  return "$status"
}

install_npm_cli() {
  local label="$1"
  local bins_csv="$2"
  local packages="$3"
  local bins="${bins_csv//,/ }"

  # shellcheck disable=SC2086
  if command_exists_any $bins; then
    # shellcheck disable=SC2086
    ok "$label 已存在：$(first_existing_command $bins)"
    return 0
  fi

  info "安装 $label ..."
  local pkg
  for pkg in $packages; do
    if ! NPM_CONFIG_REGISTRY="$BNPM_REGISTRY" npm view "$pkg" version >/dev/null 2>&1; then
      warn "npm 源中未找到候选包：$pkg，跳过。"
      continue
    fi
    info "尝试 npm install -g $pkg"
    if NPM_CONFIG_REGISTRY="$BNPM_REGISTRY" npm install -g "$pkg"; then
      append_path_once "$NPM_BIN"
      # shellcheck disable=SC2086
      if command_exists_any $bins; then
        # shellcheck disable=SC2086
        ok "$label 安装完成：$(first_existing_command $bins)"
        return 0
      fi
      warn "$pkg 安装成功，但未在 PATH 中找到预期命令：$bins"
    else
      warn "$pkg 安装失败，继续尝试下一个候选包名。"
    fi
  done

  warn "$label 未能自动安装。可用对应环境变量覆盖包名后重试。"
  return 1
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  warn "未检测到 uv，正在安装 uv ..."
  if curl -LsSf https://astral.sh/uv/install.sh | sh; then
    append_path_once "$HOME/.local/bin"
  fi
  command -v uv >/dev/null 2>&1
}

install_ncmdcli() {
  if command -v ncmdcli >/dev/null 2>&1; then
    ok "ncmdcli 已存在：$(command -v ncmdcli)"
    return 0
  fi
  info "安装 ncmdcli（uv tool / git+ssh）..."
  if ! ensure_uv; then
    warn "uv 不可用，跳过 ncmdcli。安装 uv 后可手动执行：uv tool install --extra-index-url http://bytedpypi.byted.org/simple ${NCMDCLI_UV_SPEC:-git+ssh://git@code.byted.org/ad/smb_cli.git} --force"
    return 1
  fi
  if uv tool install --extra-index-url http://bytedpypi.byted.org/simple "${NCMDCLI_UV_SPEC:-git+ssh://git@code.byted.org/ad/smb_cli.git}" --force; then
    append_path_once "$HOME/.local/bin"
    command -v ncmdcli >/dev/null 2>&1 \
      && ok "ncmdcli 安装完成：$(command -v ncmdcli)" \
      || warn "ncmdcli 已安装但当前 shell 未找到，请确认 ~/.local/bin 在 PATH 中。"
  else
    warn "ncmdcli 安装失败：当前机器无法通过 SSH 读取 git@code.byted.org/ad/smb_cli.git。"
    warn "如确实需要 ncmdcli，请先配置 Codebase SSH key/仓库权限，再重跑本脚本；不影响其它 CLI 和 bytedcli skills 使用。"
    return 1
  fi
}

try_dataleap_login() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || return 0

  if "$cmd" whoami --provider dataleap >/dev/null 2>&1; then
    ok "$cmd 登录态已可用。"
    return 0
  fi
  if "$cmd" login --help >/dev/null 2>&1; then
    try_cmd "预热 $cmd login --provider dataleap --mode auto；如出现二维码/浏览器，请完成授权" \
      "$cmd" login --provider dataleap --mode auto
    return 0
  fi
  warn "$cmd 未发现 login 子命令，跳过登录预热。"
}

try_cmd() {
  local title="$1"
  shift
  info "$title"
  if run_with_qr_render "$@"; then
    ok "$title 完成。"
    return 0
  fi
  warn "$title 未完成，可稍后手动执行：$*"
  return 1
}

try_login_for_command() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || return 0

  if "$cmd" auth status >/dev/null 2>&1; then
    ok "$cmd auth 已可用。"
    return 0
  fi
  if "$cmd" auth login --help >/dev/null 2>&1; then
    try_cmd "预热 $cmd auth login；如出现二维码，请扫码授权" "$cmd" auth login
    return 0
  fi
  if "$cmd" login --help >/dev/null 2>&1; then
    try_cmd "预热 $cmd login；如出现二维码，请扫码授权" "$cmd" login
    return 0
  fi
  warn "$cmd 未发现标准 login/auth login 子命令，跳过登录预热。"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

BNPM_REGISTRY="${BNPM_REGISTRY:-https://bnpm.byted.org}"
GLOBAL_SKILL_DIR="$HOME/.agents/skills"

info "项目根目录：$SCRIPT_DIR"
info "npm registry：$BNPM_REGISTRY"

if ! command -v npm >/dev/null 2>&1; then
  err "未检测到 npm。请先安装 Node.js/npm 后重试。"
  exit 1
fi

NPM_PREFIX="$(npm config get prefix 2>/dev/null || true)"
NPM_BIN=""
if [ -n "$NPM_PREFIX" ] && [ "$NPM_PREFIX" != "undefined" ]; then
  NPM_BIN="$NPM_PREFIX/bin"
  append_path_once "$NPM_BIN"
  persist_path_once "$NPM_BIN"
fi

mkdir -p "$GLOBAL_SKILL_DIR"
ok "全局 skill 目录已就绪：$GLOBAL_SKILL_DIR"

# 1) 安装内部 CLI。候选包名先做 npm view 探测，避免无效包名刷出大段 E404。
install_npm_cli "bytedcli" \
  "bytedcli" \
  "${BYTEDCLI_PACKAGES:-@bytedance-dev/bytedcli@latest}"

install_ncmdcli

install_npm_cli "dleap-cli" \
  "dleap,dleap-cli" \
  "${DLEAP_CLI_PACKAGES:-@byted/dleap-cli@latest dleap-cli @bytedance/dleap-cli @bytedance-dev/dleap-cli}"

install_npm_cli "LeapCLI" \
  "leap,leapcli,LeapCLI" \
  "${LEAPCLI_PACKAGES:-leap-cli leapcli LeapCLI @bytedance/leap-cli @bytedance-dev/leap-cli}"

install_npm_cli "dataleap-cli" \
  "dataleap,dataleap-cli" \
  "${DATALEAP_CLI_PACKAGES:-@byted/dataleap-cli@latest dataleap-cli @bytedance/dataleap-cli @bytedance-dev/dataleap-cli}"

install_npm_cli "feishu-cli" \
  "feishu,feishu-cli,lark" \
  "${FEISHU_CLI_PACKAGES:-@lixiaolin94/feishu-cli@latest @larksuite/cli@latest feishu-cli @bytedance/feishu-cli @bytedance-dev/feishu-cli}"

# 2) 安装 bytedcli 官方内部 skills 到 ~/.agents/skills。Pi/Symphony 会自动扫描该目录。
if command -v bytedcli >/dev/null 2>&1; then
  info "注册 bytedcli 内部 skills 到 $GLOBAL_SKILL_DIR ..."
  if [ "${SYMPHONY_INSTALL_ALL_BYTED_SKILLS:-0}" = "1" ]; then
    bytedcli self skill install --all -g \
      && ok "已安装 bytedcli 打包的全部内部 skills。" \
      || warn "安装全部 bytedcli skills 失败，可稍后执行：bytedcli self skill install --all -g"
  else
    DEFAULT_SKILLS="bytedcli bytedance-auth bytedance-feishu bytedance-lark bytedance-codebase bytedance-insearch bytedance-meego bytedance-devflow bytedance-dorado bytedance-oceanus bytedance-hive bytedance-tqs bytedance-coral bytedance-dataq bytedance-log bytedance-tce bytedance-tcc"
    SKILLS_TO_INSTALL="${SYMPHONY_INTERNAL_SKILLS:-$DEFAULT_SKILLS}"
    for skill in $SKILLS_TO_INSTALL; do
      bytedcli self skill install -s "$skill" -g \
        && ok "skill 已注册：$skill" \
        || warn "skill 注册失败：$skill"
    done
  fi

  bytedcli self skill list --installed -g >/dev/null 2>&1 \
    && ok "bytedcli skill 列表可读取。" \
    || warn "无法读取 bytedcli 已安装 skill 列表。"
else
  warn "未找到 bytedcli，跳过 bytedcli skill 注册。"
fi

# 3) 登录预热。二维码会直接输出到终端；在 Symphony TUI 内执行时可由 TUI 渲染。
if [ "${SYMPHONY_SKIP_INTERNAL_LOGIN:-0}" = "1" ]; then
  warn "已跳过内部 CLI 登录预热（SYMPHONY_SKIP_INTERNAL_LOGIN=1）。"
else
  if command -v bytedcli >/dev/null 2>&1; then
    bytedcli --json auth status >/dev/null 2>&1 \
      && ok "bytedcli auth 已可用。" \
      || try_cmd "预热 bytedcli auth login；如出现二维码，请扫码授权" bytedcli auth login

    # Feishu 授权与普通 ByteCloud/SSO 授权不是完全等价的，单独预热一次。
    run_with_qr_render bytedcli feishu login --no-terminal-qr \
      && ok "bytedcli Feishu 授权已完成或已有可用授权。" \
      || warn "bytedcli Feishu 授权未完成；可稍后手动执行：bytedcli feishu login"
  fi

  try_login_for_command ncmdcli
  try_login_for_command ncmd
  try_login_for_command dleap
  try_login_for_command dleap-cli
  try_login_for_command leap
  try_login_for_command leapcli
  try_dataleap_login dataleap
  try_dataleap_login dataleap-cli
  try_login_for_command feishu
  try_login_for_command feishu-cli
fi

echo
ok "内部 CLI 与 skills 初始化完成。"
echo
echo "  Symphony/pi 会自动扫描：$GLOBAL_SKILL_DIR"
echo "  查看已安装 skills：bytedcli self skill list --installed -g"
echo "  安装全部 bytedcli skills：SYMPHONY_INSTALL_ALL_BYTED_SKILLS=1 bash install-internal-clis.sh"
echo "  启动 Symphony：symphony"
echo
