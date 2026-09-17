"""shopfast_agent_gui.py -- the ShopFast support agent as a live demo window.

WHAT TO SHOW, IN ORDER

  1. TICKET 1. Sheet reads, verify, lookup, refund. HOOK BLOCKS stays 0.

  2. TICKET 2 -- a $650 refund against a $500 limit. HOOK BLOCKS **still 0**.
     The model reads the rule and escalates without ever calling process_refund.
     This is the interesting part: a well-guided agent makes the hooks look
     pointless, because nothing ever reaches them.

  3. GUIDANCE: OFF, DESCRIPTIONS: vague, then TICKET 2 again. Now the model has
     never been told the rule. It calls process_refund -- and HOOK BLOCKS goes
     to 1, the HOOK DECISIONS pane turns red, the refund is recorded BLOCKED.

  4. ENFORCE: OFF as well, then TICKET 2 once more. REFUNDED reads $650.00.
     "Good news, the refund has already been processed!"

     Steps 2 -> 3 -> 4 are the whole lesson. Toggling ENFORCE alone shows you
     nothing, because with guidance on the hook never fires either way.

  5. TICKET 3, and watch the CASE FACTS pane fill. The last turn asks for a total
     nobody restated.

  6. TICKET 4. RETRIES counts two 503s on order 4321, and none at all on 99999.

  Then read the TERMINAL tile rather than the prose. A reply that sounds finished
  and a terminal_reason of "max_turns" are the same run; only one is telling the
  truth.

THIS WINDOW IS A VIEWER
  No loop, no tool dispatch, no sheet access. It renders what the SDK's loop yields.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import scrolledtext, ttk

import shopfast_agent as agent
import shopfast_backend as backend
import shopfast_policy as policy
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import (
    HookEventMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from shopfast_policy import CASE, HOOK_LOG, SESSION

HERE = Path(__file__).resolve().parent
SKILL_PATH = HERE / ".claude" / "skills" / "shopfast-support" / "SKILL.md"

# --- palette -------------------------------------------------------------
BG = "#F7F6F3"
PANEL = "#FFFFFF"
TINT = "#FBFBFA"
INK = "#1F2328"
MUTED = "#6B7280"
BLUE = "#1D4ED8"
GREEN = "#15803D"
AMBER = "#B45309"
RED = "#B91C1C"
LINE = "#E2E0DB"

root = tk.Tk()
root.title("ShopFast support -- hooks enforce, prompts request")
root.configure(bg=BG)
root.minsize(940, 660)

w = min(1250, root.winfo_screenwidth() - 80)
h = min(1020, root.winfo_screenheight() - 80)
root.geometry(f"{w}x{h}+30+16")

# Panes get a SHARE of the window we actually got. Windows display scaling inflates
# every fixed-height widget, and a hardcoded layout leaves the conversation with three
# visible lines on a scaled screen.
_avail = max(260, h - 300)
FACTS_H = max(110, min(240, int(_avail * 0.26)))
CONVO_H = max(160, int(_avail * 0.46))
HOOKS_H = max(90, _avail - FACTS_H - CONVO_H)

BASE_BODY, BASE_MONO, BASE_LABEL, BASE_BIG, BASE_TINY = 11, 10, 9, 12, 8
BODY = tkfont.Font(family="Segoe UI", size=BASE_BODY)
MONO = tkfont.Font(family="Consolas", size=BASE_MONO)
LABEL = tkfont.Font(family="Segoe UI", size=BASE_LABEL, weight="bold")
BIG = tkfont.Font(family="Segoe UI", size=BASE_BIG, weight="bold")
TINY = tkfont.Font(family="Segoe UI", size=BASE_TINY)
MONO_BOLD = tkfont.Font(family="Consolas", size=BASE_MONO, weight="bold")

ZOOMABLE = [(BODY, BASE_BODY), (MONO, BASE_MONO), (LABEL, BASE_LABEL),
            (BIG, BASE_BIG), (TINY, BASE_TINY), (MONO_BOLD, BASE_MONO)]
zoom_offset = 0

root.columnconfigure(0, weight=1)
# Five rows, each with its own. An earlier version put the header and the button strip
# both in row 0 and pushed the buttons down with padding -- they simply covered the
# heading. Overlapping grid cells do not fight for space, they stack.
root.rowconfigure(0, weight=0)   # heading + zoom
root.rowconfigure(1, weight=0)   # ticket buttons + switches
root.rowconfigure(2, weight=1)   # panes  <- all spare height lands here
root.rowconfigure(3, weight=0)   # stats
root.rowconfigure(4, weight=0)   # footer


def card(parent, title, note=""):
    wrap = tk.Frame(parent, bg=BG)
    head = tk.Frame(wrap, bg=BG)
    head.pack(fill=tk.X, padx=2, pady=(0, 3))
    tk.Label(head, text=title, font=LABEL, fg=MUTED, bg=BG).pack(side=tk.LEFT)
    if note:
        tk.Label(head, text="  " + note, font=TINY, fg=MUTED, bg=BG).pack(side=tk.LEFT)
    inner = tk.Frame(wrap, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
    inner.pack(fill=tk.BOTH, expand=True)
    return wrap, inner


def write(widget, text, tag=None):
    widget.configure(state=tk.NORMAL)
    widget.insert(tk.END, text, tag or ())
    widget.configure(state=tk.DISABLED)
    widget.see(tk.END)


# --- row 0: controls -----------------------------------------------------
bar = tk.Frame(root, bg=BG)
bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))

tk.Label(bar, text="A HOOK IS A GATE . A PROMPT IS A REQUEST",
         font=LABEL, fg=MUTED, bg=BG).pack(side=tk.LEFT)
tk.Label(bar, text="  ticket 2 three ways: as-is  .  GUIDANCE off  .  GUIDANCE + ENFORCE off",
         font=TINY, fg=MUTED, bg=BG).pack(side=tk.LEFT)

zoom_text = tk.StringVar(value="100%")
zbox = tk.Frame(bar, bg=BG)
zbox.pack(side=tk.RIGHT)
ttk.Button(zbox, text="-", width=3, command=lambda: set_zoom(-1)).pack(side=tk.LEFT)
tk.Label(zbox, textvariable=zoom_text, font=LABEL, fg=MUTED, bg=BG, width=6).pack(side=tk.LEFT)
ttk.Button(zbox, text="+", width=3, command=lambda: set_zoom(+1)).pack(side=tk.LEFT)


def set_zoom(delta):
    global zoom_offset
    zoom_offset = 0 if delta == 0 else max(-3, min(12, zoom_offset + delta))
    for font, base in ZOOMABLE:
        font.configure(size=base + zoom_offset)
    zoom_text.set(f"{round((BASE_BODY + zoom_offset) / BASE_BODY * 100)}%")


ticket_btns: dict[str, ttk.Button] = {}
row_btns = tk.Frame(root, bg=BG)
row_btns.grid(row=1, column=0, sticky="w", padx=14, pady=(8, 0))

for key, ticket in agent.TICKETS.items():
    b = ttk.Button(row_btns, text=f"TICKET {key}", width=10,
                   command=lambda k=key: start_ticket(k))
    b.pack(side=tk.LEFT, padx=(0, 6))
    ticket_btns[key] = b

# TWO switches, not one. With guidance on, a good model reads the $500 rule and
# polices itself -- the hook never fires and HOOK BLOCKS sits at 0 whether enforcement
# is on or off. Toggling ENFORCE alone therefore shows you nothing. Turn GUIDANCE off
# first; then the model actually calls process_refund and you can watch what catches it.
guidance_var = tk.StringVar(value="GUIDANCE: ON")
ttk.Button(row_btns, textvariable=guidance_var, width=18,
           command=lambda: toggle_guidance()).pack(side=tk.LEFT, padx=(14, 6))

enforce_var = tk.StringVar(value="ENFORCE: ON (hooks)")
enforce_btn = ttk.Button(row_btns, textvariable=enforce_var, width=22,
                         command=lambda: toggle_enforce())
enforce_btn.pack(side=tk.LEFT, padx=(0, 6))

desc_var = tk.StringVar(value="DESCRIPTIONS: good")
ttk.Button(row_btns, textvariable=desc_var, width=20,
           command=lambda: toggle_descriptions()).pack(side=tk.LEFT, padx=(0, 6))

ttk.Button(row_btns, text="Clear", width=8, command=lambda: clear_all()).pack(side=tk.LEFT)

# --- row 1: panes --------------------------------------------------------
panes = tk.PanedWindow(root, orient=tk.VERTICAL, bg=BG, bd=0,
                       sashwidth=7, sashrelief=tk.RAISED, sashpad=2, showhandle=False)
panes.grid(row=2, column=0, sticky="nsew", padx=14, pady=(10, 6))

facts_wrap, facts_card = card(
    panes, "CASE FACTS",
    "rebuilt from tool RESULTS and re-injected on every turn -- never from the model's prose")
panes.add(facts_wrap, minsize=60, height=FACTS_H, stretch="never", pady=3)
facts_box = tk.Text(facts_card, wrap=tk.NONE, font=MONO, fg=INK, bg=TINT,
                    relief=tk.FLAT, padx=10, pady=8, state=tk.DISABLED)
facts_box.pack(fill=tk.BOTH, expand=True)

convo_wrap, convo_card = card(
    panes, "CONVERSATION",
    "the prose can say 'all sorted' -- the TERMINAL tile cannot   .   drag dividers to resize")
panes.add(convo_wrap, minsize=100, height=CONVO_H, stretch="always", pady=3)
convo = scrolledtext.ScrolledText(convo_card, wrap=tk.WORD, font=BODY, fg=INK, bg=PANEL,
                                  relief=tk.FLAT, padx=12, pady=10, state=tk.DISABLED)
convo.pack(fill=tk.BOTH, expand=True)
convo.tag_config("who", foreground=MUTED, font=LABEL, spacing1=10, spacing3=2)
convo.tag_config("you", foreground=BLUE, spacing3=6)
convo.tag_config("claude", foreground=INK, spacing3=10)
convo.tag_config("sdk", foreground=GREEN, font=LABEL, spacing1=8, spacing3=2)
convo.tag_config("tool", foreground=AMBER, font=MONO, spacing3=2)
convo.tag_config("dim", foreground=MUTED, font=TINY, spacing3=4)
convo.tag_config("err", foreground=RED)
convo.tag_config("banner", foreground=INK, font=LABEL, spacing1=12, spacing3=4)

hooks_wrap, hooks_card = card(
    panes, "HOOK DECISIONS",
    "every allow/deny, in order -- a denial is an ordinary tool result, it does NOT raise")
panes.add(hooks_wrap, minsize=70, height=HOOKS_H, stretch="never", pady=3)
hooks_box = scrolledtext.ScrolledText(hooks_card, wrap=tk.WORD, font=MONO, fg=INK, bg=TINT,
                                      relief=tk.FLAT, padx=10, pady=8, state=tk.DISABLED)
hooks_box.pack(fill=tk.BOTH, expand=True)
hooks_box.tag_config("deny", foreground=RED, font=MONO_BOLD)
hooks_box.tag_config("allow", foreground=GREEN)
hooks_box.tag_config("obs", foreground=MUTED)

# --- row 2: stats --------------------------------------------------------
stats = tk.Frame(root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
stats.grid(row=3, column=0, sticky="ew", padx=14, pady=(6, 10))

v_verified = tk.StringVar(value="no")
v_blocks = tk.StringVar(value="0")
v_retries = tk.StringVar(value="0")
v_tools = tk.StringVar(value="--")
v_turns = tk.StringVar(value="0")
v_fresh = tk.StringVar(value="0")
v_out = tk.StringVar(value="0")
v_cread = tk.StringVar(value="0")
v_cwrite = tk.StringVar(value="0")
v_cost = tk.StringVar(value="$0.0000")
v_refunded = tk.StringVar(value="$0.00")
v_terminal = tk.StringVar(value="--")

TILES = [
    ("VERIFIED", v_verified),
    ("HOOK BLOCKS", v_blocks),
    ("RETRIES", v_retries),
    ("TOOLS PICKED", v_tools),
    ("TURNS", v_turns),
    ("REFUNDED", v_refunded),
    # Input arrives in THREE counters. TOKENS IN is FRESH only -- read it alone and you
    # will see 40 tokens next to a $0.05 bill.
    ("TOKENS IN", v_fresh),
    ("TOKENS OUT", v_out),
    ("CACHE READ", v_cread),
    ("CACHE WRITE", v_cwrite),
    ("COST", v_cost),
    ("TERMINAL", v_terminal),
]

tile_labels: dict[str, tk.Label] = {}
for i, (title, var) in enumerate(TILES):
    r, c = divmod(i, 4)
    stats.columnconfigure(c, weight=1)
    tile = tk.Frame(stats, bg=PANEL)
    tile.grid(row=r, column=c, sticky="w", padx=12, pady=5)
    tk.Label(tile, text=title, font=TINY, fg=MUTED, bg=PANEL).pack(side=tk.LEFT)
    lab = tk.Label(tile, textvariable=var, font=BIG, fg=INK, bg=PANEL)
    lab.pack(side=tk.LEFT, padx=(6, 0))
    tile_labels[title] = lab

# --- row 3: footer -------------------------------------------------------
foot = tk.Frame(root, bg=BG)
foot.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 10))
status = tk.StringVar(value="Ready. Press TICKET 2 to watch a $650 refund get blocked.")
tk.Label(foot, textvariable=status, font=LABEL, fg=MUTED, bg=BG, anchor="w").pack(
    side=tk.LEFT, fill=tk.X, expand=True)

raw_win = tk.Toplevel(root)
raw_win.title("Raw message stream -- what the SDK's loop actually yields")
raw_win.configure(bg=BG)
raw_win.geometry(f"{min(1000, root.winfo_screenwidth() - 140)}x"
                 f"{min(720, root.winfo_screenheight() - 180)}")
raw_win.withdraw()
raw_win.protocol("WM_DELETE_WINDOW", raw_win.withdraw)
_rb = tk.Frame(raw_win, bg=BG)
_rb.pack(fill=tk.X, padx=12, pady=(10, 4))
tk.Label(_rb, text="One entry per message the SDK yielded, in order",
         font=LABEL, fg=MUTED, bg=BG).pack(side=tk.LEFT)
ttk.Button(_rb, text="Close", command=raw_win.withdraw).pack(side=tk.RIGHT)
_rc = tk.Frame(raw_win, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
_rc.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
raw = scrolledtext.ScrolledText(_rc, wrap=tk.WORD, font=MONO, fg="#374151", bg=TINT,
                                relief=tk.FLAT, padx=12, pady=10, state=tk.DISABLED)
raw.pack(fill=tk.BOTH, expand=True)
raw.tag_config("hdr", foreground=GREEN, font=MONO_BOLD)

ttk.Button(foot, text="Show raw messages", width=20,
           command=lambda: (raw_win.deiconify(), raw_win.lift(), raw.see(tk.END))
           ).pack(side=tk.RIGHT)


# --- live state ----------------------------------------------------------
seen = {"offered": [], "chosen": {}, "running": False}
totals = {"fresh": 0, "out": 0, "cread": 0, "cwrite": 0, "cost": 0.0, "turns": 0}
events: queue.Queue = queue.Queue()


def redraw_facts():
    facts_box.configure(state=tk.NORMAL)
    facts_box.delete("1.0", tk.END)
    facts_box.insert("1.0", CASE.render(SESSION))
    facts_box.configure(state=tk.DISABLED)
    v_verified.set("YES" if SESSION.customer_verified else "no")
    tile_labels["VERIFIED"].configure(fg=GREEN if SESSION.customer_verified else MUTED)
    v_refunded.set(f"${CASE.total_refunded():,.2f}")


_hooks_drawn = 0


def redraw_hooks():
    """Append only the new decisions -- the log is the source of truth, not the widget."""
    global _hooks_drawn
    fresh = HOOK_LOG[_hooks_drawn:]
    _hooks_drawn = len(HOOK_LOG)
    for d in fresh:
        if d.decision == "deny":
            write(hooks_box, f"  DENY   {d.hook:<13} {d.tool}\n         {d.reason}\n", "deny")
        elif d.decision == "allow" and d.reason:
            write(hooks_box, f"  allow  {d.hook:<13} {d.tool}  ({d.reason})\n", "allow")
        elif d.decision == "observe" and d.reason:
            write(hooks_box, f"  .      {d.hook:<13} {d.reason}\n", "obs")
    blocks = len(policy.denials())
    v_blocks.set(str(blocks))
    tile_labels["HOOK BLOCKS"].configure(fg=RED if blocks else INK)
    v_retries.set(str(len(backend.RETRY_LOG)))
    tile_labels["RETRIES"].configure(fg=AMBER if backend.RETRY_LOG else INK)


def toggle_guidance():
    if seen["running"]:
        return
    policy.set_guidance(not policy.guidance_on())
    on = policy.guidance_on()
    guidance_var.set(f"GUIDANCE: {'ON' if on else 'OFF'}")
    write(convo,
          "\nGUIDANCE is now " + ("ON -- SKILL.md, the Policy tab, the eligibility "
                                 "verdict and the case-facts policy line are all back.\n"
                                 if on else
                                 "OFF -- the model has never been told the $500 rule. "
                                 "Now it will actually try.\n"),
          "sdk" if on else "err")
    status.set("Guidance " + ("ON. The model knows the rules and will police itself."
                              if on else
                              "OFF. Run ticket 2 -- the model will attempt the refund."))


def toggle_enforce():
    if seen["running"]:
        return
    policy.set_enforcing(not policy.enforcing())
    on = policy.enforcing()
    enforce_var.set(f"ENFORCE: {'ON (hooks)' if on else 'OFF (prompt only)'}")
    write(convo, f"\nENFORCE is now {'ON -- rules are hooks' if on else 'OFF -- rules are only prose in the system prompt'}\n",
          "sdk" if on else "err")
    status.set("Enforcement " + ("ON. Hooks gate the tools." if on else
                                 "OFF. Re-run ticket 2 and watch what gets through."))


def toggle_descriptions():
    if seen["running"]:
        return
    now = backend.description_mode()
    os.environ["SHOPFAST_DESCRIPTIONS"] = "vague" if now == "good" else "good"
    desc_var.set(f"DESCRIPTIONS: {backend.description_mode()}")
    write(convo, f"\nTool descriptions switched to '{backend.description_mode()}'. "
                 f"Re-run a ticket and count the wrong tool calls.\n", "dim")


def clear_all():
    if seen["running"]:
        return
    global _hooks_drawn
    policy.reset_all()
    backend.reset_faults()
    _hooks_drawn = 0
    seen["chosen"] = {}
    totals.update({"fresh": 0, "out": 0, "cread": 0, "cwrite": 0, "cost": 0.0, "turns": 0})
    for widget in (convo, hooks_box, raw):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.configure(state=tk.DISABLED)
    for var in (v_blocks, v_retries, v_turns, v_fresh, v_out, v_cread, v_cwrite):
        var.set("0")
    v_cost.set("$0.0000")
    v_terminal.set("--")
    v_tools.set("--")
    tile_labels["TERMINAL"].configure(fg=INK)
    redraw_facts()
    status.set("Cleared.")


# --- driving the agent ---------------------------------------------------
async def drive(key: str):
    ticket = agent.TICKETS[key]
    options = agent.build_options()
    async with ClaudeSDKClient(options=options) as client:
        for i, turn in enumerate(ticket.turns, 1):
            events.put(("turn", (i, len(ticket.turns), turn)))
            await client.query(turn)
            async for message in client.receive_response():
                events.put(("msg", message))


def start_ticket(key: str):
    if seen["running"]:
        return
    global _hooks_drawn
    policy.reset_all()
    backend.reset_faults()
    _hooks_drawn = 0
    seen["chosen"] = {}
    redraw_facts()
    redraw_hooks()

    ticket = agent.TICKETS[key]
    write(convo, f"\nTICKET {key} -- {ticket.title}\n", "banner")
    write(convo, f"teaches: {ticket.teaches}\n", "dim")
    write(convo, f"expect : {ticket.expect}\n", "dim")
    write(convo, f"mode   : enforcement {'ON' if policy.enforcing() else 'OFF'}, "
                 f"descriptions {backend.description_mode()}\n", "dim")

    seen["running"] = True
    for b in ticket_btns.values():
        b.configure(state=tk.DISABLED)
    v_terminal.set("running")
    tile_labels["TERMINAL"].configure(fg=MUTED)
    status.set(f"Running ticket {key} on {agent.MODEL}...")

    def worker():
        try:
            asyncio.run(drive(key))
        except Exception as exc:   # surfaced in the pane, not swallowed
            events.put(("error", exc))
        events.put(("done", None))

    threading.Thread(target=worker, daemon=True).start()


def finish(text: str):
    seen["running"] = False
    for b in ticket_btns.values():
        b.configure(state=tk.NORMAL)
    status.set(text)


def show_blocks(content):
    if isinstance(content, str):
        return
    for block in content or []:
        if isinstance(block, TextBlock):
            if block.text.strip():
                write(convo, "AGENT\n", "who")
                write(convo, block.text.strip() + "\n", "claude")
        elif isinstance(block, ToolUseBlock):
            if not block.name.startswith("mcp__"):
                write(convo, f"    sdk internal: {block.name}\n", "dim")
                continue
            seen["chosen"][block.name] = seen["chosen"].get(block.name, 0) + 1
            offered = seen["offered"]
            where = (f"#{offered.index(block.name) + 1} of {len(offered)}"
                     if block.name in offered else "server list not in yet")
            write(convo, f"    TOOL  {block.name}   [{where}]\n", "tool")
            write(convo, f"          args {block.input}\n", "tool")
            v_tools.set(f"{len(seen['chosen'])}/{len(offered) or '?'}")
        elif isinstance(block, ToolResultBlock):
            text = str(block.content)
            if len(text) > 420:
                text = text[:420] + f" ... (+{len(text) - 420} chars, see raw)"
            write(convo, f"          -> {text}{'  [is_error]' if block.is_error else ''}\n",
                  "err" if block.is_error else "tool")


def handle(item):
    kind = item[0]

    if kind == "turn":
        i, total, text = item[1]
        write(convo, f"\nCUSTOMER ({i}/{total})\n", "who")
        write(convo, text + "\n", "you")
        return

    if kind == "error":
        write(convo, "ERROR\n", "who")
        write(convo, f"{item[1]}\n", "err")
        v_terminal.set("error")
        tile_labels["TERMINAL"].configure(fg=RED)
        finish("Run failed -- see the conversation pane.")
        return

    if kind == "done":
        # The ONLY place the run is declared over. A ticket is several turns and each
        # one ends with its own ResultMessage; calling finish() there re-enabled the
        # buttons between turns, so a second ticket could be started on top of a
        # running one. "done" is emitted once, after the last turn.
        redraw_facts()
        redraw_hooks()
        finish(f"Ticket complete. terminal_reason: {v_terminal.get()}")
        return

    message = item[1]
    write(raw, f"\n----- {type(message).__name__} -----\n", "hdr")
    write(raw, f"{message!r}\n")

    # HookEventMessage subclasses SystemMessage -- test it FIRST.
    if isinstance(message, HookEventMessage):
        pass   # the HOOK DECISIONS pane is fed from HOOK_LOG, which is richer

    elif isinstance(message, SystemMessage):
        if message.subtype == "init":
            data = message.data or {}
            seen["offered"] = sorted(t for t in data.get("tools", [])
                                     if t.startswith("mcp__"))
            ok = "shopfast-support" in (data.get("skills") or [])
            write(convo, f"SDK: session started. Skill loaded: {ok}. "
                         f"{len(seen['offered'])} mcp tool(s) offered.\n", "sdk")
            if not ok:
                write(convo, "SKILL.md did not load -- the behaviour rules are missing.\n", "err")
            for s in data.get("mcp_servers", []):
                state = str(s.get("status"))
                write(convo, f"    mcp server '{s.get('name')}': {state}"
                             + ("  (pending is normal -- it connects in the background)"
                                if state == "pending" else "") + "\n", "dim")

    elif isinstance(message, (AssistantMessage, UserMessage)):
        show_blocks(message.content)

    elif isinstance(message, ResultMessage):
        reason = message.terminal_reason or message.stop_reason or "?"
        usage = message.usage or {}
        totals["fresh"] += usage.get("input_tokens", 0)
        totals["out"] += usage.get("output_tokens", 0)
        totals["cread"] += usage.get("cache_read_input_tokens", 0)
        totals["cwrite"] += usage.get("cache_creation_input_tokens", 0)
        totals["turns"] += message.num_turns
        totals["cost"] += message.total_cost_usd or 0.0

        v_fresh.set(f"{totals['fresh']:,}")
        v_out.set(f"{totals['out']:,}")
        v_cread.set(f"{totals['cread']:,}")
        v_cwrite.set(f"{totals['cwrite']:,}")
        v_turns.set(str(totals["turns"]))
        v_cost.set(f"${totals['cost']:.4f}")
        v_terminal.set(str(reason))

        write(convo, f"SDK: turn finished. terminal_reason: {reason} "
                     f"({message.num_turns} turn(s))\n", "sdk")

        if reason == "max_turns":
            tile_labels["TERMINAL"].configure(fg=RED)
            write(convo, f"WARNING: hit the {agent.MAX_TURNS}-turn cap. This turn did NOT "
                         f"succeed, however finished the reply sounds.\n", "err")
        elif reason == "error_max_budget_usd":
            tile_labels["TERMINAL"].configure(fg=RED)
            write(convo, "WARNING: hit the spend cap. This turn did NOT succeed.\n", "err")
        elif "max_tokens" in (reason, message.stop_reason or ""):
            tile_labels["TERMINAL"].configure(fg=RED)
            write(convo, f"WARNING: the reply hit the {agent.MAX_OUTPUT_TOKENS}-token cap "
                         f"and was CUT OFF mid-sentence.\n", "err")
        else:
            tile_labels["TERMINAL"].configure(
                fg=GREEN if reason in ("completed", "end_turn") else AMBER)

        if message.errors:
            write(convo, f"errors: {message.errors}\n", "err")

        redraw_facts()
        redraw_hooks()
        # One turn ended, not the ticket. finish() waits for the "done" event.
        status.set(f"turn done -- terminal_reason: {reason} "
                   f"({message.num_turns} turn(s)); continuing...")


def drain():
    try:
        while True:
            handle(events.get_nowait())
    except queue.Empty:
        pass
    root.after(80, drain)


# --- boot ----------------------------------------------------------------
root.bind_all("<Control-minus>", lambda e: set_zoom(-1))
root.bind_all("<Control-equal>", lambda e: set_zoom(+1))
root.bind_all("<Control-0>", lambda e: set_zoom(0))

write(convo, "ShopFast support agent -- Claude Agent SDK with real hooks.\n", "who")
write(convo, "\nTwo tool surfaces, on purpose:\n", "dim")
write(convo, "  mcp__airtable__*  external server, READ tools only -- the data\n", "dim")
write(convo, "  mcp__shopfast__*  in-process, five actions -- what the hooks gate\n", "dim")
write(convo, "\nYou cannot hook a refund cap onto list_records; there is no refund in\n", "dim")
write(convo, "it. That is why the action tools exist as first-class tools.\n", "dim")
write(convo, "\nPress TICKET 2, then flip ENFORCE to OFF and press it again. Same model,\n", "dim")
write(convo, "same prompt, same ticket. Only the enforcement layer changes.\n", "dim")
write(convo, f"\nCaps: {agent.MAX_TURNS} turns, ${agent.MAX_BUDGET_USD} spend, "
             f"{agent.MAX_OUTPUT_TOKENS} tokens per reply.\n", "dim")

if not agent.AIRTABLE_PAT:
    write(convo, "\nAIRTABLE_PAT is not set in .env -- every Airtable read will "
                 "fail.\n", "err")
if not agent.AIRTABLE_BASE_ID:
    write(convo, "\nAIRTABLE_BASE_ID is not set in .env.\n", "err")

redraw_facts()
root.after(80, drain)


# --- self-test -----------------------------------------------------------
# SHOPFAST_GUI_SELFTEST=4 runs ticket 4 as if its button had been clicked, prints the
# resulting tiles, and quits. A GUI that merely opens proves nothing: the interesting
# code is all on the click path -- the worker thread, the queue drain, and the three
# panes. This exercises that without a human at the keyboard.
_selftest = os.getenv("SHOPFAST_GUI_SELFTEST", "").strip()
if _selftest:
    def _report_and_quit():
        if seen["running"]:
            root.after(500, _report_and_quit)
            return
        print("=== GUI SELF-TEST RESULT ===")
        for _title, _var in TILES:
            print(f"  {_title:<14} {_var.get()}")
        print(f"  hook log       {len(HOOK_LOG)} entries, "
              f"{len(policy.denials())} denial(s)")
        print(f"  retries        {len(backend.RETRY_LOG)}")
        print(f"  convo pane     {len(convo.get('1.0', tk.END))} chars")
        print(f"  facts pane     {len(facts_box.get('1.0', tk.END))} chars")
        print(f"  hooks pane     {len(hooks_box.get('1.0', tk.END))} chars")
        root.quit()

    def _kick():
        start_ticket(_selftest)
        root.after(2000, _report_and_quit)

    root.after(700, _kick)

root.mainloop()
