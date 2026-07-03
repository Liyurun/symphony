"""Native-pi TUI (route A).

Design
------
Symphony's TUI is intentionally thin: it is a plain interactive terminal on top
of pi's native agent loop, plus ONE Symphony-specific slash command, ``/sop``.

- **Normal chat** goes straight to pi (``PiBridge.run_prompt_to_completion``),
  i.e. the full native pi agent experience. When ``log_normal_chat`` is enabled
  (default), each turn is also recorded as a one-node task in the shared file
  log so the Web UI can display and later analyze it.

- **``/sop <name> [key=value ...] [free text]``** runs a real SOP through the
  backend ``TaskManager`` / ``SOPExecutor``. Its events are written to the same
  shared log, so the Web UI shows the full node-by-node record.

There is NO WebSocket sync between TUI and Web anymore — both simply read/write
the same local log directory (the single source of truth).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import shlex
import shutil
import struct
import sys
import unicodedata
import zlib
from typing import Any, Optional

from symphony.core.event_bus import SymphonyEvent


HELP_TEXT = """\
Symphony TUI — 命令：
  <直接输入>                  对话（记录为 Web 可追踪任务）

  Pi 会话能力：
  /status                     查看当前 pi session / model / context 文件
  /model [provider/model]     查看或切换模型；也支持 /model <keyword> 模糊匹配
  /models [keyword]           列出可用模型
  /thinking <level>           设置 thinking: off/minimal/low/medium/high/xhigh
  /compact [instructions]     手动压缩 pi 上下文
  /new                        开启新的 pi session
  /commands                   列出 pi 侧 slash/prompt/extension/skill 命令
  /skills                     列出 pi skills
  /bash <command>             通过 pi 执行 bash，并进入上下文

  Symphony 编排能力：
  /sop <name> [k=v ...]       运行 SOP（Web 可看到节点级记录）
  /sops                       列出 SOP 模板
  /tasks                      列出最近任务
  /task <task_id>             打印任务详情链接

  /help                       显示本帮助
  Ctrl+C                      运行中：中断当前回答；输入中：退出
  /quit                       退出
"""


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_GRAY = "\033[90m"
ANSI_BLACK = "\033[30m"
ANSI_WHITE_BG = "\033[47m"
ANSI_BLACK_BG = "\033[40m"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _ansi(code: str) -> str:
    return code if sys.stdout.isatty() else ""


def _style(text: str, *codes: str) -> str:
    prefix = "".join(_ansi(c) for c in codes)
    return f"{prefix}{text}{_ansi(ANSI_RESET)}" if prefix else text


def _inline_markdown(text: str) -> str:
    """Render a small, terminal-safe subset of inline Markdown."""
    code_spans: list[str] = []

    def keep_code(match: re.Match) -> str:
        code_spans.append(_style(match.group(1), ANSI_CYAN))
        return f"\u0000CODE{len(code_spans) - 1}\u0000"

    text = re.sub(r"`([^`]+)`", keep_code, text)
    text = re.sub(r"\*\*([^*]+)\*\*", lambda m: _style(m.group(1), ANSI_BOLD), text)
    text = re.sub(r"__([^_]+)__", lambda m: _style(m.group(1), ANSI_BOLD), text)
    for i, rendered in enumerate(code_spans):
        text = text.replace(f"\u0000CODE{i}\u0000", rendered)
    return text


def _normalize_markdown(text: str) -> str:
    """Normalize common LLM Markdown glitches before terminal rendering."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        # Models often emit "###1. title"; Markdown requires a space.
        line = re.sub(r"^(\s*#{1,6})(?=\S)", r"\1 ", line)
        # And sometimes "-列表项" / "-议题" without the required space.
        line = re.sub(r"^(\s*)[-*+](?=\S)", r"\1- ", line)
        # Normalize numbered lists like "1.读取文件".
        line = re.sub(r"^(\s*\d+[.)])(?=\S)", r"\1 ", line)
        lines.append(line)
    return "\n".join(lines)


def _render_markdown(text: str, *, prefix: str = "") -> str:
    """Pretty-print common Markdown blocks for the plain TUI.

    This intentionally avoids heavyweight dependencies: it handles headings,
    bullets, numbered lists, quotes, fenced code blocks, bold, and inline code.
    """
    rendered: list[str] = []
    in_code = False
    code_lang = ""

    for raw_line in _normalize_markdown(text).split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        fence = re.match(r"^```\s*([^`]*)$", stripped)
        if fence:
            if not in_code:
                in_code = True
                code_lang = fence.group(1).strip()
                label = f" code: {code_lang} " if code_lang else " code "
                rendered.append(prefix + _style("─" * 2 + label + "─" * 24, ANSI_GRAY))
            else:
                in_code = False
                code_lang = ""
                rendered.append(prefix + _style("─" * 34, ANSI_GRAY))
            continue

        if in_code:
            rendered.append(prefix + "  " + _style(line, ANSI_CYAN))
            continue

        if not stripped:
            rendered.append("")
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            title = _inline_markdown(heading.group(2))
            marker = "━" if level <= 2 else "─"
            rendered.append(prefix + _style(f"{marker} {title}", ANSI_BOLD, ANSI_CYAN))
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            rendered.append(prefix + _style("│ ", ANSI_GRAY) + _inline_markdown(quote.group(1)))
            continue

        bullet = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if bullet:
            indent = " " * (len(bullet.group(1)) // 2 * 2)
            rendered.append(prefix + indent + _style("• ", ANSI_GREEN) + _inline_markdown(bullet.group(2)))
            continue

        numbered = re.match(r"^(\s*)(\d+)[.)]\s+(.+)$", line)
        if numbered:
            indent = " " * (len(numbered.group(1)) // 2 * 2)
            marker = _style(f"{numbered.group(2)}. ", ANSI_YELLOW)
            rendered.append(prefix + indent + marker + _inline_markdown(numbered.group(3)))
            continue

        rendered.append(prefix + _inline_markdown(line))

    return "\n".join(rendered)


def _assistant_block(text: str) -> str:
    rendered = _render_markdown(text).split("\n")
    if not rendered:
        rendered = [""]
    first = _style("pi > ", ANSI_BOLD, ANSI_CYAN) + rendered[0]
    rest = [("     " + line) if line else "" for line in rendered[1:]]
    return "\n".join([first, *rest])


def _line_count(text: str) -> int:
    """Return how many terminal rows a rendered block occupies.

    The TUI redraws the current assistant block in-place for every streamed
    full-text snapshot. Counting only ``\n`` is not enough: a long single line
    wraps across many terminal rows, so clearing just one logical line leaves
    stale wrapped fragments behind. That stale text is exactly what made output
    look like repeated ``pi > ... pi > ...`` prefixes for long answers.
    """
    width = max(20, shutil.get_terminal_size(fallback=(100, 24)).columns)
    total = 0
    for line in text.split("\n") or [""]:
        visible_width = _terminal_cell_width(ANSI_RE.sub("", line))
        total += max(1, (visible_width + width - 1) // width)
    return max(1, total)


def _terminal_cell_width(text: str) -> int:
    """Approximate display-cell width, treating CJK/fullwidth chars as 2."""
    cells = 0
    for ch in text.expandtabs(4):
        if unicodedata.combining(ch):
            continue
        cells += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return cells


def _stream_suffix(previous: str, current: str) -> str:
    """Best-effort suffix for non-interactive stdout where redraw is impossible."""
    if current.startswith(previous):
        return current[len(previous):]
    return current


def _stream_update(previous_full: str, incoming: str, *, replace: bool = False) -> tuple[str, str, bool]:
    """Normalize mixed snapshot/delta stream events.

    pi/SOP events may carry either a full assistant snapshot (``replace=True``)
    or a true incremental delta. The TUI must keep a *turn-global* full text so
    that after a tool card is printed, the next full snapshot only contributes
    its new suffix instead of replaying the whole answer again.

    Returns ``(next_full_text, visible_delta, is_rewrite)``. ``is_rewrite`` is
    true when the provider sent a replacement that is not an extension of the
    previous full text; callers may choose to redraw the current block.
    """
    if not incoming:
        return previous_full, "", False
    if replace or incoming.startswith(previous_full):
        if incoming == previous_full:
            return previous_full, "", False
        if incoming.startswith(previous_full):
            return incoming, incoming[len(previous_full):], False
        return incoming, incoming, True
    return previous_full + incoming, incoming, False


def _terminal_width() -> int:
    return max(20, shutil.get_terminal_size(fallback=(100, 24)).columns)


def _clear_rendered_lines(line_count: int) -> None:
    if line_count <= 0 or not sys.stdout.isatty():
        return
    print("\r\033[K", end="")
    for _ in range(line_count - 1):
        print("\033[F\033[K", end="")


def _one_line(value, limit: int = 140) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            text = str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _collect_text(value) -> str:
    """Extract readable text from pi tool results."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_collect_text(v) for v in value)))
    if isinstance(value, dict):
        parts: list[str] = []
        # pi/bash results are usually {content:[{type:'text', text:'...'}], details:{}}
        if isinstance(value.get("text"), str):
            parts.append(value["text"])
        if value.get("content") is not None:
            parts.append(_collect_text(value.get("content")))
        for key in ("stdout", "stderr", "output", "message", "error"):
            if isinstance(value.get(key), str):
                parts.append(value[key])
        if parts:
            return "\n".join(p for p in parts if p)
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(value)
    return str(value)


def _iter_json_lines(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _tool_result_summary(data: dict, limit: int = 180) -> str:
    text = _collect_text(data.get("result"))
    if not text:
        return ""

    # Prefer actionable auth hints from bytedcli JSON lines.
    for obj in _iter_json_lines(text):
        err = obj.get("error") if isinstance(obj, dict) else None
        data_obj = obj.get("data") if isinstance(obj, dict) else None
        code = obj.get("code") or (data_obj.get("code") if isinstance(data_obj, dict) else None)
        if code == "AUTH_REQUIRED":
            return _one_line("bytedcli 可执行，但 Feishu 未登录；请完成 Feishu 登录", limit)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or "调用失败"
            hint = err.get("hint") or err.get("auth_command")
            if err.get("code") == "AUTH_REQUIRED" or (hint and "feishu login" in hint):
                return _one_line("bytedcli 可执行，但 Feishu 未登录；请完成 Feishu 登录", limit)
            return _one_line(f"{msg}；{hint}" if hint else msg, limit)
        if isinstance(data_obj, dict):
            # Login JSON can contain URL / QR image fields depending on CLI version.
            for key in ("url", "login_url", "auth_url", "qr_url", "qr_image", "path"):
                if data_obj.get(key):
                    return _one_line(f"{key}: {data_obj[key]}", limit)
            if data_obj.get("message"):
                return _one_line(data_obj["message"], limit)

    url = re.search(r"https?://\S+", text)
    if url:
        return _one_line(url.group(0), limit)

    meaningful = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("Command exited with code")
    ]
    return _one_line(meaningful[0] if meaningful else text, limit)


def _extract_action_hint(data: dict) -> dict[str, str] | None:
    """Turn verbose tool logs into one concise user action, if any."""
    text = _collect_text(data.get("result")) or _collect_text(data.get("partial_result"))
    if not text:
        return None

    # bytedcli/Feishu login can stream a QR path while the command is still
    # waiting for the user to scan. Surface that immediately instead of waiting
    # for tool_call_end, which only arrives after the login command exits.
    qr_path = re.search(r"QR code saved to:\s*(\S+\.png)", text, re.IGNORECASE)
    if qr_path:
        return {"kind": "file", "text": f"打开/扫码二维码图片：{qr_path.group(1)}"}

    png_path = re.search(r"(/[^\s]+(?:qr|login)[^\s]*\.png)", text, re.IGNORECASE)
    if png_path and ("scan" in text.lower() or "扫码" in text or "二维码" in text or "qr" in text.lower()):
        return {"kind": "file", "text": f"打开/扫码二维码图片：{png_path.group(1)}"}

    for obj in _iter_json_lines(text):
        if not isinstance(obj, dict):
            continue
        data_obj = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        code = obj.get("code") or data_obj.get("code") or err.get("code")
        event = str(obj.get("event") or "")
        auth_cmd = err.get("auth_command") or data_obj.get("auth_command") or data_obj.get("authCommand")
        auth_context = (
            code == "AUTH_REQUIRED"
            or bool(auth_cmd)
            or event in {"action_required", "auth_required", "qr_image_ready", "login_url_ready"}
        )

        for key in ("login_url", "auth_url", "qr_url", "url"):
            if auth_context and data_obj.get(key):
                return {"kind": "url", "text": f"打开登录链接：{data_obj[key]}"}
        for key in ("qr_image", "qr_image_path", "path"):
            if auth_context and data_obj.get(key):
                return {"kind": "file", "text": f"打开/扫码二维码图片：{data_obj[key]}"}

        if auth_context:
            return {
                "kind": "command",
                "text": "请在另一个终端完成 Feishu 登录：bytedcli --json feishu login --no-terminal-qr",
            }

    # Only treat a raw URL as a login URL when the surrounding text is clearly
    # about authentication. Skill docs often contain unrelated URLs (for
    # example bnpm registry links), and those must not be surfaced as login URLs.
    authish = (
        "AUTH_REQUIRED" in text
        or "Not authenticated" in text
        or "feishu login" in text
        or "Open this URL in Feishu/Lark" in text
        or "Please scan" in text
        or "User Code:" in text
        or "oauth/v1/device/verify" in text
        or "QR code saved to:" in text
    )
    if authish:
        url = re.search(r"https?://\S+", text)
        if url:
            return {"kind": "url", "text": f"打开登录链接：{url.group(0)}"}
        return {
            "kind": "command",
            "text": "请在另一个终端完成 Feishu 登录：bytedcli --json feishu login --no-terminal-qr",
        }
    return None


def _extract_action_target(action: dict[str, str]) -> str:
    text = action.get("text", "")
    if action.get("kind") == "file":
        prefix = "打开/扫码二维码图片："
        return text.split(prefix, 1)[1].strip() if prefix in text else text.strip()
    if action.get("kind") == "url":
        prefix = "打开登录链接："
        return text.split(prefix, 1)[1].strip() if prefix in text else text.strip()
    return text.strip()


def _action_key(action: dict[str, str]) -> str:
    return f"{action.get('kind', '')}:{_extract_action_target(action)}"


def _print_action_hint(print_fn, action: dict[str, str], rendered_actions: set[str]) -> None:
    key = _action_key(action)
    if key in rendered_actions:
        return
    rendered_actions.add(key)
    print_fn("      " + _style("↳", ANSI_GRAY) + " " + action["text"])
    if action["kind"] == "file":
        rendered_image = _render_image_for_tui(_extract_action_target(action))
        if rendered_image:
            print_fn(_style("      ↳ 可直接在 TUI 扫描下方二维码：", ANSI_GRAY))
            for line in rendered_image.splitlines():
                print_fn("      " + line)
    if action["kind"] == "command":
        print_fn("      " + _style("↳", ANSI_GRAY) + " 完成后，重新发起刚才的请求即可。")


def _read_png_pixels(path: str) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """Read a simple non-interlaced PNG into RGBA pixels using stdlib only.

    The Feishu/bytedcli QR image is a tiny PNG. Pulling in Pillow just for TUI
    display would make installation heavier, so this decoder intentionally
    supports the common 8-bit, non-interlaced PNG color types we need here.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a png")

    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette: list[tuple[int, int, int, int]] = []
    idat: list[bytes] = []

    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos: pos + 4])[0]
        ctype = raw[pos + 4: pos + 8]
        chunk = raw[pos + 8: pos + 8 + length]
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
    rows: list[bytes] = []
    prev = bytearray(stride)
    idx = 0
    bpp = channels

    for _ in range(height):
        ftype = data[idx]
        idx += 1
        scan = bytearray(data[idx: idx + stride])
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
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                val = x + pred
            else:
                raise ValueError("unsupported png filter")
            recon[i] = val & 0xFF
        rows.append(bytes(recon))
        prev = recon

    pixels: list[tuple[int, int, int, int]] = []
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


def _qr_pixel_is_dark(pixel: tuple[int, int, int, int]) -> bool:
    r, g, b, a = pixel
    if a < 32:
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 180


def _estimate_qr_module_size(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> int | None:
    """Estimate the original PNG pixels-per-QR-module.

    Login QR PNGs are normally generated as a crisp bitmap where every QR module
    occupies N x N source pixels. Resampling the image to an arbitrary terminal
    size can merge neighbouring modules and makes the terminal rendering
    unscannable even though the original PNG is valid.  We therefore infer N
    from horizontal/vertical run lengths, then render one terminal cell per QR
    module instead of one terminal cell per resampled image pixel.
    """
    if width <= 0 or height <= 0 or not pixels:
        return None

    binary = [_qr_pixel_is_dark(p) for p in pixels]
    runs: list[int] = []

    row_step = max(1, height // 96)
    for y in range(0, height, row_step):
        row = binary[y * width: (y + 1) * width]
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


def _png_to_terminal_qr(path: str, *, max_width: int = 56) -> str:
    width, height, pixels = _read_png_pixels(path)
    if width <= 0 or height <= 0:
        return ""

    # Render by QR modules whenever possible.  The previous implementation
    # resampled the source image to fit the terminal width.  That is dangerous
    # for QR codes: if the source is 410px wide (41 modules * 10px), rendering it
    # as 34 terminal samples destroys module boundaries and phones cannot scan
    # it, although the original PNG is perfectly valid.
    term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    module_size = _estimate_qr_module_size(width, height, pixels)

    if module_size:
        target_w = max(1, round(width / module_size))
        target_h = max(1, round(height / module_size))
        # Two spaces make each QR module close to square on common terminals.
        # If the terminal is too narrow, prefer one exact cell per module over
        # downsampling, because preserving every module is more important than
        # perfect aspect ratio.
        cell_width = 2 if target_w * 2 <= max(20, term_width - 2) else 1
        quiet = 0

        def is_dark(tx: int, ty: int) -> bool:
            sx = min(width - 1, max(0, int((tx + 0.5) * module_size)))
            sy = min(height - 1, max(0, int((ty + 0.5) * module_size)))
            return _qr_pixel_is_dark(pixels[sy * width + sx])
    else:
        target_w = max(16, min(width, max_width, max(16, (term_width - 12) // 2)))
        target_h = max(16, round(height * target_w / width))
        cell_width = 2
        quiet = 2

        def is_dark(tx: int, ty: int) -> bool:
            sx = min(width - 1, int((tx + 0.5) * width / target_w))
            sy = min(height - 1, int((ty + 0.5) * height / target_h))
            return _qr_pixel_is_dark(pixels[sy * width + sx])

    if sys.stdout.isatty():
        dark_cell = _ansi(ANSI_BLACK_BG) + (" " * cell_width)
        light_cell = _ansi(ANSI_WHITE_BG) + (" " * cell_width)
        reset = _ansi(ANSI_RESET)
    else:
        dark_cell = "█" * cell_width
        light_cell = " " * cell_width
        reset = ""

    lines: list[str] = []
    for y in range(-quiet, target_h + quiet):
        chars: list[str] = []
        for x in range(-quiet, target_w + quiet):
            dark = 0 <= x < target_w and 0 <= y < target_h and is_dark(x, y)
            chars.append(dark_cell if dark else light_cell)
        lines.append("".join(chars) + reset)
    return "\n".join(lines).rstrip("\n")


def _iterm_inline_image(path: str) -> str:
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    name = base64.b64encode(os.path.basename(path).encode()).decode("ascii")
    return f"\033]1337;File=name={name};inline=1;preserveAspectRatio=1:{encoded}\a"


def _render_image_for_tui(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    lower = path.lower()
    if lower.endswith(".png"):
        try:
            art = _png_to_terminal_qr(path)
            if art:
                return art
        except Exception:
            pass
    if os.environ.get("SYMPHONY_TUI_INLINE_IMAGES") == "1" and sys.stdout.isatty():
        try:
            return _iterm_inline_image(path)
        except Exception:
            return ""
    return ""


def _is_plain_feishu_login(command: str) -> bool:
    normalized = " ".join((command or "").split())
    return normalized in {"bytedcli feishu login", "bytedcli feishu login --no-terminal-qr"}


def _tool_summary(tool: str, data: dict, *, is_end: bool = False) -> str:
    args = data.get("arguments") or data.get("args") or data.get("input") or {}
    tool_l = (tool or "").lower()
    if is_end:
        result_summary = _tool_result_summary(data)
        if result_summary:
            return result_summary
    if not isinstance(args, dict):
        return _one_line(args)

    if tool_l in {"read", "write", "edit"}:
        path = args.get("file_path") or args.get("path") or args.get("filename")
        detail = f"文件 {_one_line(path, 90)}" if path else _one_line(args)
        if tool_l == "edit" and args.get("old_string"):
            detail += f"；替换 {_one_line(args.get('old_string'), 45)}"
        return detail
    if tool_l in {"bash", "shell"}:
        return _one_line(args.get("command") or args.get("cmd") or args, 150)
    if tool_l in {"grep", "glob", "ls"}:
        return _one_line(args, 150)

    return _one_line(args, 150)


def _parse_sop_args(rest: str) -> tuple[str, dict, str]:
    """Parse '/sop' arguments into (name, kv_inputs, free_text).

    Example: 'code-review repo_path=/x depth=2 please be strict'
      -> ('code-review', {'repo_path': '/x', 'depth': '2'}, 'please be strict')
    """
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    if not tokens:
        return "", {}, ""
    name = tokens[0]
    kv: dict[str, str] = {}
    free: list[str] = []
    for tok in tokens[1:]:
        if "=" in tok and not tok.startswith("="):
            k, v = tok.split("=", 1)
            kv[k] = v
        else:
            free.append(tok)
    return name, kv, " ".join(free)


def _model_label(model: dict[str, Any] | None) -> str:
    if not isinstance(model, dict):
        return "unknown"
    provider = model.get("provider") or model.get("api") or "unknown"
    model_id = model.get("id") or model.get("model") or model.get("name") or "unknown"
    thinking = model.get("thinkingLevel") or model.get("thinking")
    suffix = f":{thinking}" if thinking else ""
    return f"{provider}/{model_id}{suffix}"


def _one_line_json(value: Any, limit: int = 240) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NativeTUI:
    """Minimal interactive loop: native pi chat + /sop dispatch."""

    def __init__(
        self,
        *,
        pi_bridge,
        task_manager,
        sop_registry,
        event_bus,
        event_log,
        web_port: int,
        log_normal_chat: bool = True,
        model: str = "",
    ):
        self.pi = pi_bridge
        self.tm = task_manager
        self.registry = sop_registry
        self.bus = event_bus
        self.log = event_log
        self.web_port = web_port
        self.log_normal_chat = log_normal_chat
        self.model = model
        self._chat_task_id: Optional[str] = None
        self._active_task_id: Optional[str] = None

    # ── output helpers ─────────────────────────────────────────

    def _print(self, *a):
        print(*a, flush=True)

    def _web_url(self, task_id: str = "") -> str:
        base = f"http://localhost:{self.web_port}"
        return f"{base}/#/tasks/{task_id}" if task_id else f"{base}/#/tasks"

    # ── main loop ──────────────────────────────────────────────

    async def run(self) -> None:
        self._print("=" * 60)
        self._print(" Symphony TUI  (native pi + /sop)")
        self._print(f" Web 看板: {self._web_url()}")
        if self.model:
            self._print(f" 模型: {self.model}")
        self._print("=" * 60)
        self._print(HELP_TEXT)

        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, self._read_line)
            except (EOFError, KeyboardInterrupt):
                self._print("\n再见。")
                return
            if line is None:
                return
            line = line.strip()
            if not line:
                continue

            if line.startswith("/"):
                should_continue = await self._handle_command(line)
                if not should_continue:
                    return
            else:
                await self._handle_chat(line)

    def _read_line(self) -> Optional[str]:
        try:
            return input("\n你 > ")
        except EOFError:
            return None

    # ── command dispatch ───────────────────────────────────────

    async def _handle_command(self, line: str) -> bool:
        parts = line[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            self._print("再见。")
            return False
        if cmd in ("help", "h", "?"):
            self._print(HELP_TEXT)
            return True
        if cmd in ("status", "session", "info"):
            await self._cmd_status()
            return True
        if cmd == "model":
            await self._cmd_model(rest)
            return True
        if cmd == "models":
            await self._cmd_models(rest)
            return True
        if cmd == "thinking":
            await self._cmd_thinking(rest)
            return True
        if cmd == "compact":
            await self._cmd_compact(rest)
            return True
        if cmd in ("commands", "cmds"):
            await self._cmd_pi_commands()
            return True
        if cmd == "skills":
            await self._cmd_pi_skills()
            return True
        if cmd in ("new", "new-session"):
            await self._cmd_new_session()
            return True
        if cmd == "bash":
            await self._cmd_bash(rest)
            return True
        if cmd == "sops":
            await self._cmd_list_sops()
            return True
        if cmd == "tasks":
            await self._cmd_list_tasks()
            return True
        if cmd == "task":
            await self._cmd_task(rest)
            return True
        if cmd == "sop":
            await self._cmd_run_sop(rest)
            return True

        self._print(f"未知命令: /{cmd}（输入 /help 查看）")
        return True

    async def _cmd_status(self) -> None:
        try:
            state = await self.pi.get_state()
        except Exception as e:
            self._print(f"无法读取 pi 状态: {e}")
            return
        self._print("当前状态：")
        self._print(f"  model: {_model_label(state.get('model'))}")
        self._print(f"  thinking: {state.get('thinkingLevel', 'unknown')}")
        self._print(f"  streaming: {state.get('isStreaming', False)}")
        self._print(f"  compacting: {state.get('isCompacting', False)}")
        self._print(f"  auto_compaction: {state.get('autoCompactionEnabled', False)}")
        self._print(f"  session: {state.get('sessionName') or state.get('sessionId') or 'unknown'}")
        if state.get("sessionFile"):
            self._print(f"  session_file: {state.get('sessionFile')}")
        cfg = getattr(self.pi, "config", None)
        if cfg is not None:
            self._print(f"  pi_cwd: {getattr(cfg, 'cwd', None) or os.getcwd()}")
            context_file_infos = getattr(cfg, "context_file_infos", None)
            if callable(context_file_infos):
                infos = context_file_infos()
                if infos:
                    self._print("  context_files:")
                    for info in infos:
                        if info.get("error"):
                            self._print(f"    - {info.get('path')} error={info.get('error')}")
                        else:
                            self._print(
                                f"    - {info.get('path')} sha256={info.get('sha256_short')} bytes={info.get('bytes')}"
                            )
                else:
                    self._print("  context_files: none")
        if self._active_task_id:
            self._print(f"  active_task: {self._active_task_id} {self._web_url(self._active_task_id)}")

    async def _cmd_model(self, rest: str) -> None:
        query = rest.strip()
        if not query:
            await self._cmd_status()
            self._print("用法: /model <provider/model> 或 /model <keyword>")
            return
        try:
            provider, model_id = await self._resolve_model_query(query)
            data = await self.pi.set_model(provider, model_id)
        except Exception as e:
            self._print(f"切换模型失败: {e}")
            return
        self._print(f"已切换模型: {_model_label(data if data else {'provider': provider, 'id': model_id})}")

    async def _resolve_model_query(self, query: str) -> tuple[str, str]:
        if "/" in query:
            provider, model_id = query.split("/", 1)
            if provider and model_id:
                return provider, model_id
        parts = query.split()
        if len(parts) == 2:
            return parts[0], parts[1]

        models = await self.pi.get_available_models()
        needle = query.lower()
        matches = [
            m for m in models
            if needle in str(m.get("id", "")).lower()
            or needle in str(m.get("provider", "")).lower()
            or needle in _model_label(m).lower()
        ]
        exact = [m for m in matches if needle == _model_label(m).lower() or needle == str(m.get("id", "")).lower()]
        if len(exact) == 1:
            m = exact[0]
            return str(m.get("provider")), str(m.get("id"))
        if len(matches) == 1:
            m = matches[0]
            return str(m.get("provider")), str(m.get("id"))
        if not matches:
            raise ValueError(f"找不到匹配模型: {query}")
        preview = ", ".join(_model_label(m) for m in matches[:10])
        more = "" if len(matches) <= 10 else f" ... 还有 {len(matches) - 10} 个"
        raise ValueError(f"模型匹配不唯一，请使用 provider/model。候选: {preview}{more}")

    async def _cmd_models(self, rest: str) -> None:
        query = rest.strip().lower()
        try:
            models = await self.pi.get_available_models()
        except Exception as e:
            self._print(f"读取模型列表失败: {e}")
            return
        if query:
            models = [m for m in models if query in _model_label(m).lower()]
        self._print(f"可用模型（{len(models)}）：")
        for m in models[:40]:
            ctx = m.get("contextWindow") or m.get("context")
            reasoning = " reasoning" if m.get("reasoning") else ""
            extra = f"  ctx={ctx}{reasoning}" if ctx or reasoning else ""
            self._print(f"  - {_model_label(m)}{extra}")
        if len(models) > 40:
            self._print(f"  ... 还有 {len(models) - 40} 个，使用 /models <keyword> 过滤")

    async def _cmd_thinking(self, rest: str) -> None:
        level = rest.strip()
        allowed = {"off", "minimal", "low", "medium", "high", "xhigh"}
        if level not in allowed:
            self._print("用法: /thinking off|minimal|low|medium|high|xhigh")
            return
        try:
            await self.pi.set_thinking_level(level)
        except Exception as e:
            self._print(f"设置 thinking 失败: {e}")
            return
        self._print(f"已设置 thinking: {level}")

    async def _cmd_compact(self, rest: str) -> None:
        try:
            data = await self.pi.compact(rest.strip() or None)
        except Exception as e:
            self._print(f"压缩上下文失败: {e}")
            return
        self._print("上下文压缩完成。")
        if data:
            self._print(f"  {_one_line_json(data)}")

    async def _cmd_pi_commands(self) -> None:
        try:
            commands = await self.pi.get_commands()
        except Exception as e:
            self._print(f"读取 pi 命令失败: {e}")
            return
        if not commands:
            self._print("（pi 未返回可用命令）")
            return
        self._print(f"pi 命令（{len(commands)}）：")
        for c in commands[:80]:
            desc = f" — {c.get('description')}" if c.get("description") else ""
            self._print(f"  /{c.get('name')} [{c.get('source', 'unknown')}]{desc}")
        if len(commands) > 80:
            self._print(f"  ... 还有 {len(commands) - 80} 个")

    async def _cmd_pi_skills(self) -> None:
        try:
            skills = await self.pi.list_skills()
        except Exception as e:
            self._print(f"读取 pi skills 失败: {e}")
            return
        if not skills:
            self._print("（暂无 pi skills）")
            return
        self._print(f"pi skills（{len(skills)}）：")
        for s in skills:
            desc = f" — {s.get('description')}" if s.get("description") else ""
            self._print(f"  /skill:{s.get('name')}{desc}")

    async def _cmd_new_session(self) -> None:
        try:
            data = await self.pi.new_session()
        except Exception as e:
            self._print(f"创建 pi session 失败: {e}")
            return
        if data.get("cancelled"):
            self._print("已取消创建新 session。")
        else:
            self._print("已创建新的 pi session。")

    async def _cmd_bash(self, rest: str) -> None:
        command = rest.strip()
        if not command:
            self._print("用法: /bash <command>")
            return
        try:
            result = await self.pi.bash(command)
        except Exception as e:
            self._print(f"bash 执行失败: {e}")
            return
        if result.get("exitCode") not in (None, 0):
            self._print(f"退出码: {result.get('exitCode')}")
        stdout = result.get("stdout") or result.get("output") or result.get("result")
        stderr = result.get("stderr")
        if stdout:
            self._print(str(stdout).rstrip())
        if stderr:
            self._print(_style(str(stderr).rstrip(), ANSI_RED))

    async def _cmd_task(self, rest: str) -> None:
        task_id = rest.strip()
        if not task_id:
            self._print("用法: /task <task_id>")
            return
        self._print(f"任务详情: {self._web_url(task_id)}")

    async def _cmd_list_sops(self) -> None:
        names = await self.registry.list_names()
        if not names:
            self._print("（暂无 SOP 模板，可在 Web 的 #/sop 页面添加）")
            return
        self._print("可用 SOP：")
        for n in names:
            self._print(f"  - {n}")

    async def _cmd_list_tasks(self) -> None:
        tasks = await self.tm.list_tasks()
        if not tasks:
            self._print("（暂无任务）")
            return
        self._print("最近任务：")
        for t in tasks[:15]:
            self._print(f"  {t.task_id[:8]}  {t.status:<10}  {t.sop_name}")

    async def _cmd_run_sop(self, rest: str) -> None:
        name, kv, free = _parse_sop_args(rest)
        if not name:
            self._print("用法: /sop <name> [key=value ...] [自然语言补充]")
            return

        sop = await self.registry.get(name)
        if sop is None:
            self._print(f"找不到 SOP: {name}（用 /sops 查看可用模板）")
            return

        # Build the initial input the root node receives.
        initial_input: dict = dict(kv)
        if free:
            initial_input.setdefault("prompt", free)

        metadata = {"source": "tui", "inputs": initial_input}
        if "prompt" in initial_input:
            metadata["prompt"] = initial_input["prompt"]

        task = await self.tm.create_task(sop, metadata=metadata)
        self._print(f"✅ 已创建任务 {task.task_id}（SOP: {name}）")
        node_ids = " → ".join(n.id for n in sop.nodes)
        self._print(f"   节点: {node_ids}")
        self._print(f"   📊 Web 详情: {self._web_url(task.task_id)}")
        self._print("   ▶ 执行中…（详细进度见 Web）")

        try:
            await self.tm.start_task(task.task_id, sop)
        except Exception as e:
            self._print(f"   ✖ 执行出错: {e}")
            return

        # Wait for the task to reach a terminal state, printing node transitions.
        await self._run_with_ctrl_c_cancel(task.task_id, self._await_task(task.task_id))

    async def _await_task(self, task_id: str, poll: float = 0.4) -> None:
        seen = 0
        handled: set[str] = set()  # request_ids we've already prompted for
        while True:
            events = await self.log.get_events(task_id, after_seq=seen)
            for e in events:
                seen = max(seen, e["seq"])
                et = e["event_type"]
                nid = e.get("node_id") or ""
                data = e.get("data") or {}
                if et == "node_started":
                    self._print(f"   ▶ {nid} 运行中…")
                elif et == "node_completed":
                    self._print(f"   ✔ {nid} 完成")
                elif et == "node_failed":
                    self._print(f"   ✖ {nid} 失败")
                elif et == "user_question_required":
                    await self._tui_answer_question(task_id, nid, data, handled)
                elif et == "human_intervention_required":
                    await self._tui_review(task_id, nid, data, handled)
            task = await self.tm.get_task(task_id)
            if task and task.status in ("completed", "failed", "cancelled"):
                self._print(f"   ● 任务结束: {task.status}")
                self._print(f"     完整记录: {self._web_url(task_id)}")
                return
            await asyncio.sleep(poll)

    async def _tui_answer_question(
        self, task_id: str, node_id: str, data: dict, handled: set[str]
    ) -> None:
        """Prompt the operator to answer a node's needs_user_input in the TUI."""
        req_id = data.get("request_id") or f"{task_id}:{node_id}"
        if req_id in handled:
            return
        # Only prompt if the executor is actually still waiting for this answer.
        pending = getattr(self.tm.human_manager, "_pending_questions", {})
        if f"{task_id}:{node_id}" not in pending:
            return
        handled.add(req_id)
        self._print(f"\n   ❓ 节点 {node_id} 需要你的输入 —— {data.get('reason', '')}")
        answers = []
        for q in data.get("questions", []) or []:
            question = q.get("question") or q.get("key") or ""
            ans = await self._ainput(f"     · {question}\n     你的回答 > ")
            if ans is None:
                ans = ""
            answers.append(f"{question}：{ans.strip()}")
        try:
            await self.tm.answer_question(task_id, node_id, "\n".join(answers))
            self._print("   ↩ 已提交回答，节点将据此继续。")
        except Exception as ex:
            self._print(f"   （提交回答失败: {ex}；可到 Web 操作: {self._web_url(task_id)}）")

    async def _tui_review(
        self, task_id: str, node_id: str, data: dict, handled: set[str]
    ) -> None:
        """Prompt the operator to approve/reject a human-intervention node."""
        req_id = data.get("request_id") or f"{task_id}:{node_id}"
        if req_id in handled:
            return
        pending = getattr(self.tm.human_manager, "_pending", {})
        if f"{task_id}:{node_id}" not in pending:
            return
        handled.add(req_id)
        preview = data.get("result_preview") or {}
        art = preview.get("artifact") if isinstance(preview, dict) else None
        self._print(f"\n   ⏸ 节点 {node_id}（{data.get('node_name', '')}）需要人工确认。")
        if art and art.get("value"):
            self._print(f"     产物[{art.get('type')}]: {art.get('value')}")
        elif isinstance(preview, dict) and preview.get("output"):
            self._print(f"     产出预览: {str(preview.get('output'))[:300]}")
        choice = await self._ainput("     通过？[y]通过 / [n]驳回 > ")
        approved = (choice or "").strip().lower() in ("y", "yes", "是", "")
        feedback = ""
        if not approved:
            feedback = (await self._ainput("     驳回原因（会作为重跑指令）> ")) or ""
        try:
            await self.tm.respond_human(task_id, node_id, approved, feedback.strip())
            self._print("   ↩ 已" + ("通过。" if approved else "驳回，节点将据反馈重跑。"))
        except Exception as ex:
            self._print(f"   （提交失败: {ex}；可到 Web 操作: {self._web_url(task_id)}）")

    async def _ainput(self, prompt: str) -> Optional[str]:
        """Read a line without blocking the event loop."""
        try:
            return await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    # ── normal chat (native pi) ────────────────────────────────

    async def _handle_chat(self, text: str) -> None:
        if not self.log_normal_chat:
            # Pure native pi turn, not recorded.
            try:
                result = await self.pi.run_prompt_to_completion(text)
                self._print("\n" + _style("pi > ", ANSI_BOLD, ANSI_CYAN) + _render_markdown(getattr(result, "text", "")))
            except Exception as e:
                self._print(f"（pi 调用出错: {e}）")
            return

        # Record the chat as a one-node ad-hoc task so the Web UI can show it.
        try:
            task = await self.tm.create_and_start_qa(text)
        except Exception as e:
            self._print(f"（无法记录会话，改用纯 pi: {e}）")
            try:
                result = await self.pi.run_prompt_to_completion(text)
                self._print("\n" + _style("pi > ", ANSI_BOLD, ANSI_CYAN) + _render_markdown(getattr(result, "text", "")))
            except Exception as e2:
                self._print(f"（pi 调用出错: {e2}）")
            return

        self._chat_task_id = task.task_id
        # Stream the answer by tailing the task's events.
        try:
            await self._run_with_ctrl_c_cancel(task.task_id, self._await_chat(task.task_id))
        finally:
            self._chat_task_id = None

    async def _run_with_ctrl_c_cancel(self, task_id: str, awaitable) -> None:
        """Run a task watcher where Ctrl+C cancels only this task.

        ``input()`` mode still uses the normal terminal behavior: Ctrl+C exits
        the TUI. During an active answer/SOP run, however, we temporarily own
        SIGINT so it maps to ``TaskManager.cancel_task`` instead of bubbling up
        and killing the whole process.
        """
        loop = asyncio.get_running_loop()
        waiter = asyncio.ensure_future(awaitable)
        cancel_task: asyncio.Task | None = None
        signal_installed = False
        self._active_task_id = task_id

        def request_cancel() -> None:
            nonlocal cancel_task
            if cancel_task is not None and not cancel_task.done():
                return
            cancel_task = asyncio.create_task(self._cancel_current_task(task_id))
            waiter.cancel()

        try:
            try:
                loop.add_signal_handler(signal.SIGINT, request_cancel)
                signal_installed = True
            except (NotImplementedError, RuntimeError):
                # Fallback for environments where asyncio signal handlers are
                # unavailable; KeyboardInterrupt below will still cancel.
                pass

            try:
                await waiter
            except asyncio.CancelledError:
                if cancel_task is not None:
                    await cancel_task
                else:
                    raise
            except KeyboardInterrupt:
                await self._cancel_current_task(task_id)
        finally:
            self._active_task_id = None
            if signal_installed:
                loop.remove_signal_handler(signal.SIGINT)

    async def _cancel_current_task(self, task_id: str) -> None:
        self._print("\n   ⏹ 正在中断当前任务…")
        try:
            await self.tm.cancel_task(task_id)
        except Exception as e:
            self._print(f"   ⚠️ 中断失败: {e}")
            return
        self._print(f"   ⊘ 已中断。完整记录: {self._web_url(task_id)}")

    async def _await_chat(self, task_id: str, poll: float = 0.25) -> None:
        seen = 0
        printed_prefix = False
        stream_full_text = ""
        rendered_text = ""
        output_open = False
        prefix_after_tool = False
        rendered_block_lines = 0
        rendered_width = _terminal_width()
        rendered_actions: set[str] = set()
        while True:
            events = await self.log.get_events(task_id, after_seq=seen)
            for e in events:
                seen = max(seen, e["seq"])
                et = e["event_type"]
                if et == "agent_message_delta":
                    data = e.get("data") or {}
                    text = data.get("text", "")
                    if not text:
                        continue
                    replace = bool(data.get("replace"))
                    next_full, visible_delta, is_rewrite = _stream_update(
                        stream_full_text, text, replace=replace
                    )
                    stream_full_text = next_full
                    if not visible_delta and not is_rewrite:
                        continue

                    if not printed_prefix:
                        print("", flush=True)
                        printed_prefix = True
                        output_open = True
                        prefix_after_tool = False

                    current_width = _terminal_width()
                    if rendered_block_lines and current_width != rendered_width:
                        # Terminal resize reflows already-printed wrapped lines.
                        # Cursor-up based clearing is no longer reliable, so do
                        # not attempt to redraw the old block. Finish the visual
                        # block and continue with future suffixes only.
                        if output_open:
                            print("", flush=True)
                        rendered_text = ""
                        rendered_block_lines = 0
                        output_open = False
                        prefix_after_tool = True
                        rendered_width = current_width

                    if prefix_after_tool:
                        output_open = True
                        prefix_after_tool = False
                        rendered_block_lines = 0
                        rendered_text = ""

                    if is_rewrite and rendered_text:
                        rendered_text = visible_delta
                    else:
                        rendered_text += visible_delta

                    if rendered_block_lines:
                        _clear_rendered_lines(rendered_block_lines)

                    # Redraw only the current visual assistant segment. The
                    # turn-global ``stream_full_text`` is kept separately so a
                    # cumulative snapshot after a tool call contributes only the
                    # suffix after the previously printed assistant text.
                    block = _assistant_block(rendered_text)
                    print(block, end="", flush=True)
                    rendered_block_lines = _line_count(block)
                    rendered_width = current_width
                    output_open = not rendered_text.endswith("\n")
                elif et == "tool_call_start":
                    data = e.get("data") or {}
                    tool = data.get("tool_name") or data.get("name") or "tool"
                    summary = _tool_summary(tool, data)
                    if output_open:
                        print("", flush=True)
                    line = f"   {_style('🔧 工具', ANSI_YELLOW)} {_style(tool, ANSI_BOLD)}"
                    if summary:
                        line += f"  {_style('›', ANSI_GRAY)} {summary}"
                    self._print(line)
                    if tool.lower() in {"bash", "shell"} and _is_plain_feishu_login(summary):
                        self._print(
                            "      "
                            + _style("↳", ANSI_GRAY)
                            + " bytedcli 可以执行；当前可能在等待 Feishu 登录/扫码。若这里没有二维码/URL，请在另一个终端执行: "
                            + _style("bytedcli --json feishu login --no-terminal-qr", ANSI_CYAN)
                        )
                    output_open = False
                    prefix_after_tool = True
                    rendered_text = ""
                    rendered_block_lines = 0
                    rendered_width = _terminal_width()
                elif et == "tool_call_update":
                    data = e.get("data") or {}
                    action = _extract_action_hint(data)
                    if action:
                        if output_open:
                            print("", flush=True)
                            output_open = False
                        _print_action_hint(self._print, action, rendered_actions)
                        prefix_after_tool = True
                        rendered_text = ""
                        rendered_block_lines = 0
                        rendered_width = _terminal_width()
                elif et == "tool_call_end":
                    data = e.get("data") or {}
                    tool = data.get("tool_name") or data.get("name") or "tool"
                    summary = _tool_summary(tool, data, is_end=True)
                    if data.get("is_error"):
                        line = f"   {_style('⚠️ 失败', ANSI_RED)} {_style(tool, ANSI_BOLD)}"
                    else:
                        line = f"   {_style('✅ 完成', ANSI_GREEN)} {_style(tool, ANSI_BOLD)}"
                    if summary and data.get("is_error"):
                        line += f"  {_style('›', ANSI_GRAY)} {summary}"
                    self._print(line)
                    action = _extract_action_hint(data)
                    if action:
                        _print_action_hint(self._print, action, rendered_actions)
                    output_open = False
                    prefix_after_tool = True
                    rendered_text = ""
                    rendered_block_lines = 0
                    rendered_width = _terminal_width()
            task = await self.tm.get_task(task_id)
            if task and task.status in ("completed", "failed", "cancelled"):
                if printed_prefix and output_open:
                    print("", flush=True)
                elif task.status == "failed":
                    self._print("（本轮失败，详见 Web）")
                return
            await asyncio.sleep(poll)
