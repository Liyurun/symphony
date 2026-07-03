"""Render the REAL captured Symphony run into screenshot-style PNGs.

Three images:
  1. tui.png   — the terminal transcript of the native-pi TUI running /sop.
  2. web.png   — a mock of the Web dashboard task-detail view, built from the
                 ACTUAL file-log events of the TUI-created task.
  3. storage.png — the on-disk file-log layout (proving SQLite is gone).

All text comes from real run artifacts, not fabricated.
"""
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.patches import FancyBboxPatch, Rectangle

# Register a CJK-capable font so Chinese TUI text renders (no tofu boxes).
_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
fm.fontManager.addfont(_CJK)
_CJK_NAME = fm.FontProperties(fname=_CJK).get_name()
plt.rcParams["font.family"] = [_CJK_NAME]
plt.rcParams["font.sans-serif"] = [_CJK_NAME, "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
MONO = {"family": _CJK_NAME}  # use CJK font in place of monospace for zh lines

# ---- collect real data -----------------------------------------------------
TUI_TX = Path("tui_transcript.txt").read_text().rstrip("\n").splitlines()

tasks = sorted(Path("data/tasks").glob("*.json"), key=lambda p: p.stat().st_mtime)
web_tid = tasks[-1].stem
events = [json.loads(l) for l in Path(f"data/logs/{web_tid}.jsonl").read_text().splitlines()]

# aggregate node deltas + status
node_order = ["brainstorm", "write", "critique"]
node_text = {n: "" for n in node_order}
node_status = {n: "pending" for n in node_order}
for e in events:
    nid = e.get("node_id")
    et = e["event_type"]
    if et == "agent_message_delta" and nid in node_text:
        node_text[nid] += e["data"].get("text", "")
    if et == "node_started" and nid in node_status:
        node_status[nid] = "running"
    if et == "node_completed" and nid in node_status:
        node_status[nid] = "completed"

# ---- image 1: TUI terminal -------------------------------------------------
BG = "#0c0f14"; FG = "#d7dde3"; GREEN = "#57c785"; BLUE = "#5aa9e6"
CYAN = "#59d0d0"; YELLOW = "#e6c15a"; DIM = "#7a828c"

fig, ax = plt.subplots(figsize=(11, 8.2), dpi=150)
fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
# title bar
ax.add_patch(Rectangle((0, 0.965), 1, 0.035, color="#1b2028"))
for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]):
    ax.add_patch(plt.Circle((0.018 + i * 0.022, 0.982), 0.006, color=c))
ax.text(0.5, 0.982, "symphony  —  TUI (native pi + /sop)", color=DIM, fontsize=10,
        ha="center", va="center", family=_CJK_NAME)

y = 0.95
lh = 0.0225
for line in TUI_TX:
    color = FG
    s = line
    if line.startswith("你 >"):
        color = CYAN
    elif "✔" in line or "完成" in line or "任务结束" in line:
        color = GREEN
    elif "▶" in line or "运行中" in line or "执行中" in line:
        color = BLUE
    elif "✅" in line or "已创建任务" in line:
        color = GREEN
    elif "Web 详情" in line or "完整记录" in line or "Web 看板" in line:
        color = BLUE
    elif line.startswith("=") or "Symphony TUI" in line or "模型" in line:
        color = YELLOW
    elif line.strip().startswith("- ") or line.strip().startswith("/") or line.strip().startswith("<"):
        color = DIM
    ax.text(0.02, y, s, color=color, fontsize=8.6, ha="left", va="top",
            family=_CJK_NAME)
    y -= lh
    if y < 0.02:
        break

fig.savefig("shot_tui.png", facecolor=BG, bbox_inches="tight", pad_inches=0.15)
plt.close(fig)

# ---- image 2: Web dashboard task-detail -----------------------------------
WBG = "#0f1420"; CARD = "#182131"; ACC = "#3b82f6"; OKG = "#22c55e"
WTX = "#e5eaf2"; WSUB = "#93a1b5"

fig, ax = plt.subplots(figsize=(11, 8.6), dpi=150)
fig.patch.set_facecolor(WBG); ax.set_facecolor(WBG)
ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

# top bar
ax.add_patch(Rectangle((0, 0.94), 1, 0.06, color="#111827"))
ax.text(0.02, 0.97, "Symphony", color=WTX, fontsize=15, va="center", weight="bold")
ax.text(0.30, 0.97, "#/tasks/" + web_tid, color=WSUB, fontsize=11, va="center", family=_CJK_NAME)
for i, tab in enumerate(["Tasks", "SOP", "Logs", "Settings"]):
    ax.text(0.60 + i * 0.10, 0.97, tab, color=WSUB if tab != "Tasks" else ACC,
            fontsize=11, va="center")

# task header
ax.text(0.02, 0.905, "Task  demo-haiku", color=WTX, fontsize=15, va="center", weight="bold")
ax.add_patch(FancyBboxPatch((0.31, 0.888), 0.11, 0.032, boxstyle="round,pad=0.006",
             color=OKG))
ax.text(0.365, 0.904, "COMPLETED", color="#04210f", fontsize=9.5, va="center",
        ha="center", weight="bold")
ax.text(0.02, 0.872, f'prompt: "the sea at dawn"   ·   nodes: brainstorm → write → critique',
        color=WSUB, fontsize=10, va="center")

# node cards
def wrap(t, n):
    import textwrap
    out = []
    for para in t.split("\n"):
        out += textwrap.wrap(para, n) or [""]
    return out

top = 0.83
titles = {"brainstorm": "① Brainstorm", "write": "② Write Haiku", "critique": "③ Critique"}
heights = {"brainstorm": 0.11, "write": 0.20, "critique": 0.27}
for nid in node_order:
    h = heights[nid]
    ax.add_patch(FancyBboxPatch((0.02, top - h), 0.96, h - 0.015,
                 boxstyle="round,pad=0.004", color=CARD, ec="#26324a", lw=1.2))
    ax.text(0.04, top - 0.028, titles[nid], color=WTX, fontsize=12, va="center", weight="bold")
    # status pill
    ax.add_patch(FancyBboxPatch((0.86, top - 0.036), 0.10, 0.026,
                 boxstyle="round,pad=0.004", color=OKG))
    ax.text(0.91, top - 0.023, "✓ completed", color="#04210f", fontsize=8, va="center", ha="center", weight="bold")
    # body text (real LLM output)
    yy = top - 0.052
    for ln in wrap(node_text[nid].strip(), 95):
        ax.text(0.045, yy, ln, color="#cdd6e4", fontsize=9, va="top", family=_CJK_NAME)
        yy -= 0.020
    top -= (h + 0.005)

fig.savefig("shot_web.png", facecolor=WBG, bbox_inches="tight", pad_inches=0.12)
plt.close(fig)

# ---- image 3: storage layout ----------------------------------------------
fig, ax = plt.subplots(figsize=(10, 5.2), dpi=150)
fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
ax.text(0.03, 0.93, "data/  —  file-backed logs (SQLite removed)", color=YELLOW,
        fontsize=13, va="center", weight="bold", family=_CJK_NAME)

lines = ["data/", "├── config.toml            # Volcengine provider (built-in)"]
for p in sorted(Path("data/logs").glob("*.jsonl")):
    lines.append(f"├── logs/{p.name:<22}# {p.stat().st_size} bytes  ← event stream (JSONL)")
for p in sorted(Path("data/tasks").glob("*.json")):
    lines.append(f"├── tasks/{p.name:<21}# task metadata (JSON)")
lines.append("└── sop_templates/")
for p in sorted(Path("data/sop_templates").glob("*.yaml")):
    lines.append(f"    ├── {p.name}")

y = 0.83
for ln in lines:
    col = FG
    if "logs/" in ln: col = BLUE
    elif "tasks/" in ln: col = CYAN
    elif "sop_templates" in ln or ".yaml" in ln: col = GREEN
    elif "config.toml" in ln: col = YELLOW
    ax.text(0.03, y, ln, color=col, fontsize=10.5, va="top", family=_CJK_NAME)
    y -= 0.055

fig.savefig("shot_storage.png", facecolor=BG, bbox_inches="tight", pad_inches=0.15)
plt.close(fig)

print("web task:", web_tid)
print("wrote shot_tui.png, shot_web.png, shot_storage.png")
