"""shopfast_streamlit.py -- the agent as a support widget on a real storefront.

RUN IT
    uv run --with streamlit streamlit run shopfast_streamlit.py

`--with` installs Streamlit for this run only, so pyproject.toml stays untouched.

WHAT THIS IS
    A ShopFast landing page with a chat launcher in the bottom-right corner, exactly
    where a support widget lives on every e-commerce site. Click it and the panel
    opens over the page: customer messages right-aligned, agent replies left.

    The products on the page ARE the rows in the Orders tab -- the Wireless Headphones
    are order 12345, the 4K Monitor is the $650 one that trips the refund cap. So the
    thing a visitor browses and the thing the agent reasons about are the same thing.

WHY THE PANEL SHOWS ONLY PROSE
    shopfast_agent_gui.py is the ENGINEER'S view: every tool call, every hook decision,
    the raw message stream. This is the CUSTOMER'S view. No tool names, no denials, no
    sheet reads in the bubble -- a customer would never see those.

    All of it moves to the sidebar console. That split is the demo: the customer reads
    "your refund has been processed", and beside it you can see whether a $650 refund
    was quietly blocked or quietly went through.

HOW IT KEEPS THE CONVERSATION
    Streamlit re-runs this whole script on every interaction, so a long-lived
    ClaudeSDKClient is not an option. The first turn's session_id is captured and every
    later turn sets `options.resume` to it: one fresh client per message, one continuous
    conversation.

    This file only ever READS the other modules -- it builds options through
    agent.build_options() and sets attributes on the returned instance. Nothing in the
    working agent is modified.

ONE LIMITATION, STATED PLAINLY
    CASE, SESSION and HOOK_LOG are module-level globals in shopfast_policy, shared by
    every browser tab pointed at this server. Fine for a demo on one machine, wrong for
    anything else.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
import threading
import traceback
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="ShopFast — Electronics & Home",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

import shopfast_agent as agent          # noqa: E402
import shopfast_backend as backend      # noqa: E402
import shopfast_policy as policy        # noqa: E402
from claude_agent_sdk import (          # noqa: E402
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_agent_sdk.types import (    # noqa: E402
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from shopfast_policy import CASE, HOOK_LOG, SESSION   # noqa: E402

HERE = Path(__file__).resolve().parent

# =========================================================================
# Logging
# =========================================================================
# Two sinks, because the two failures this app can have look nothing alike:
#   the file  -- the full turn-by-turn trace, including worker-thread activity
#   stderr    -- the terminal you launched streamlit from, for the loud stuff
#
# The thread name is in the format ON PURPOSE. The first real bug here was reading
# st.session_state from the worker thread, and the error Streamlit raises for that
# ("has no attribute session_id") reads like a missing key. Seeing the thread name
# next to it turns a twenty-minute hunt into a glance.
LOG_PATH = HERE / "shopfast_streamlit.log"


def _build_logger() -> logging.Logger:
    log = logging.getLogger("shopfast.ui")
    # Streamlit re-executes this script on EVERY interaction. Without this guard the
    # handlers stack up and by the tenth click each line is written ten times.
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    log.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(threadName)-12s] %(message)s", "%H:%M:%S")
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass                      # a read-only checkout must not kill the app
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


LOG = _build_logger()


# =========================================================================
# Styling
# =========================================================================
CSS = """
<style>
  :root {
    --sf-ink:      #16202c;
    --sf-muted:    #6b7a8d;
    --sf-line:     #e3e8ef;
    --sf-brand:    #1668e3;
    --sf-brand-dk: #0f4fae;
    --sf-bg:       #f4f7fb;
    --sf-panel:    #ffffff;
    --sf-ok:       #0f8a4d;
  }

  .stApp { background: var(--sf-bg); }

  /* Header chrome. Checked against streamlit's own bundle rather than guessed: the
     sidebar's expand control (stExpandSidebarButton) lives INSIDE stToolbar, so
     hiding the toolbar traps the sidebar shut with no way back. Remove only the
     deploy button and the menu. */
  footer { visibility: hidden; }
  header[data-testid="stHeader"], [data-testid="stAppHeader"] { background: transparent; }
  [data-testid="stAppDeployButton"], [data-testid="stMainMenu"],
  #MainMenu, [data-testid="stStatusWidget"] { display: none !important; }
  [data-testid="stExpandSidebarButton"] {
    display: inline-flex !important; visibility: visible !important; opacity: 1 !important;
    background: var(--sf-panel) !important; border: 1px solid var(--sf-line) !important;
    border-radius: 9px !important; color: var(--sf-brand) !important;
    box-shadow: 0 2px 8px rgba(20,40,80,.14) !important; z-index: 1000;
  }
  [data-testid="stExpandSidebarButton"] * { color: var(--sf-brand) !important; }
  [data-testid="stSidebarCollapseButton"] * { color: #d7e0ea !important; }

  .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1120px; }

  /* ---------- storefront ---------- */
  .sf-nav {
    display: flex; align-items: center; gap: 14px; padding: 14px 22px;
    background: var(--sf-panel); border: 1px solid var(--sf-line);
    border-radius: 14px; margin-bottom: 18px;
  }
  .sf-logo { font-size: 1.15rem; font-weight: 700; color: var(--sf-ink); letter-spacing: -.3px; }
  .sf-logo span { color: var(--sf-brand); }
  .sf-navlinks { margin-left: auto; display: flex; gap: 22px; font-size: .86rem; color: var(--sf-muted); }

  .sf-hero {
    background: linear-gradient(135deg, var(--sf-brand) 0%, var(--sf-brand-dk) 100%);
    color: #fff; padding: 40px 44px; border-radius: 18px; margin-bottom: 26px;
    box-shadow: 0 10px 34px rgba(22,104,227,.22);
  }
  .sf-hero h1 { font-size: 2.05rem; margin: 0 0 8px; font-weight: 700; letter-spacing: -.6px; }
  .sf-hero p  { font-size: .98rem; opacity: .92; margin: 0 0 18px; max-width: 560px; }
  .sf-pill {
    display: inline-block; background: rgba(255,255,255,.16); padding: 6px 13px;
    border-radius: 999px; font-size: .78rem; margin-right: 8px;
  }

  .sf-sectitle { font-size: 1.02rem; font-weight: 650; color: var(--sf-ink); margin: 4px 0 12px; }

  .sf-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  @media (max-width: 900px) { .sf-grid { grid-template-columns: repeat(2, 1fr); } }
  .sf-card {
    background: var(--sf-panel); border: 1px solid var(--sf-line); border-radius: 14px;
    padding: 16px 18px;
  }
  .sf-card .emoji { font-size: 1.7rem; }
  .sf-card .name  { font-weight: 620; color: var(--sf-ink); margin-top: 6px; font-size: .93rem; }
  .sf-card .price { color: var(--sf-brand); font-weight: 700; margin-top: 3px; }
  .sf-card .meta  { font-size: .74rem; color: var(--sf-muted); margin-top: 7px; }
  .sf-tag {
    display: inline-block; font-size: .68rem; padding: 2px 8px; border-radius: 999px;
    background: #eaf1fd; color: var(--sf-brand); margin-top: 8px;
  }
  .sf-tag.warn { background: #fff2e0; color: #b26a00; }

  .sf-strip {
    display: flex; gap: 26px; flex-wrap: wrap; background: var(--sf-panel);
    border: 1px solid var(--sf-line); border-radius: 14px; padding: 16px 22px;
    margin: 22px 0 8px; font-size: .84rem; color: var(--sf-muted);
  }
  .sf-strip b { color: var(--sf-ink); display: block; font-size: .9rem; margin-bottom: 2px; }

  /* ---------- the floating launcher ---------- */
  .st-key-sf_launcher {
    position: fixed; bottom: 26px; right: 26px; z-index: 1001;
    width: 62px;
  }
  .st-key-sf_launcher button {
    width: 62px !important; height: 62px !important; border-radius: 50% !important;
    background: linear-gradient(135deg, var(--sf-brand), var(--sf-brand-dk)) !important;
    color: #fff !important; border: none !important; font-size: 1.5rem !important;
    box-shadow: 0 8px 26px rgba(22,104,227,.42) !important; padding: 0 !important;
  }
  .st-key-sf_launcher button:hover { transform: translateY(-2px); }
  .st-key-sf_launcher button p { font-size: 1.5rem !important; margin: 0 !important; }

  /* ---------- the chat panel ---------- */
  /* A rounded card floating above the launcher, the shape every support widget uses --
     not a full-height drawer, which swallowed the whole right side and left the input
     stranded at the bottom of the viewport. Wider than the first attempt (440 vs 396)
     so a refund explanation with a ticket number fits on two lines. */
  .st-key-sf_panel {
    /* Proportions taken from a production live-chat widget: distinctly taller than
       wide, roughly 400 x 660, with the conversation owning most of that. */
    position: fixed; bottom: 80px; right: 26px; z-index: 1000;
    width: 404px; max-width: calc(100vw - 40px);
    /* No fixed height here -- and that is the fix, not an omission.
       Pinning the panel to 600px clipped the input, because everything above it
       (header, chips, message list) added up to more than 600 and `overflow: hidden`
       cut off the remainder. Guessing that total again would just move the bug.
       Instead: .sf-msgs has a fixed height and every other child is a constant, so
       the panel's height is automatically constant too -- stable, and nothing to
       clip. */
    background: var(--sf-panel); border: 1px solid var(--sf-line);
    border-radius: 18px; box-shadow: 0 18px 52px rgba(15,35,70,.26);
    padding: 0 16px 10px;
    display: flex; flex-direction: column;
  }
  /* One narrow line: avatar, name, status -- not a stacked block. The two-line header
     was eating ~62px of a panel whose whole job is showing the conversation, and the
     status line does not need its own row to say "Online".
     sticky/top:0 so it stays put no matter what the panel does around it. */
  .sf-head {
    display: flex; align-items: center; gap: 9px; margin: 0 -16px 6px;
    background: linear-gradient(135deg, var(--sf-brand), var(--sf-brand-dk));
    color: #fff; padding: 9px 14px; border-radius: 18px 18px 0 0;
    position: sticky; top: 0; z-index: 2; flex: 0 0 auto;
  }
  .sf-head .av {
    width: 26px; height: 26px; border-radius: 50%; background: rgba(255,255,255,.2);
    display: flex; align-items: center; justify-content: center; font-size: 14px;
    flex: 0 0 26px;
  }
  .sf-head .t { font-weight: 650; font-size: .85rem; letter-spacing: -.1px; }
  .sf-head .s { font-size: .68rem; opacity: .92; margin-left: auto; white-space: nowrap; }
  .sf-dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #46e08a; margin-right: 5px;
  }

  /* The scrolling message list. column-reverse plus a reversed render order is the
     CSS-only way to keep a chat pinned to the newest message: the "top" of the scroll
     box is the bottom of the conversation, so it stays there on its own. */
  /* THE one height in the widget. Everything else -- header, chips, input -- is a
     constant, so fixing this fixes the panel's total height without anyone having to
     add the parts up.
     Stated outright rather than flex:1, because .sf-msgs is NOT a direct child of the
     panel: Streamlit wraps every element in its own block divs, so a flex chain would
     have to be re-established at each of them and would break the next time that
     markup changes.
     The second number is the safety margin and it decides whether the header stays on
     screen. It has to cover EVERYTHING else the panel carries -- header, hint row, two
     rows of chips, input, padding -- plus the gap the panel floats above the launcher.

     I estimated it twice and cropped the header twice. Measured off a screenshot:
     hint + chips + input come to ~285px, ~330 with the header, against the ~190 I had
     assumed. 440 is that, tightened spacing, and room to spare.

     Because the second term is in vh, browser zoom is handled for free: zooming in
     shrinks the viewport in CSS pixels, calc() shrinks the list with it, and the
     header stays put.

     The list keeps its height even when empty, so this is the panel at its TALLEST,
     not its average. */
  .sf-msgs {
    display: flex; flex-direction: column-reverse; overflow-y: auto;
    height: min(650px, calc(100vh - 320px)); min-height: 110px;
    padding-right: 4px; margin-bottom: 4px;
  }

  /* Heavy browser zoom, or a genuinely short window. min-height stops the list
     shrinking any further, so past this point the panel has to give up its float and
     its padding instead -- otherwise the header goes off the top again at 175%. */
  @media (max-height: 620px) {
    .st-key-sf_panel { bottom: 72px; padding-bottom: 6px; }
    .sf-msgs { min-height: 86px; }
    .st-key-sf_panel .stButton button { height: 26px !important; min-height: 26px !important; }
  }

  /* ---------- bubbles: user right, agent left ---------- */
  .sf-row { display: flex; margin-bottom: 10px; align-items: flex-end; gap: 7px; }
  .sf-row.ai   { justify-content: flex-start; }
  .sf-row.user { justify-content: flex-end; }
  .sf-av {
    width: 26px; height: 26px; border-radius: 50%; background: #eaf1fd;
    display: flex; align-items: center; justify-content: center; font-size: 13px;
    flex: 0 0 26px;
  }
  .sf-bubble {
    max-width: 78%; padding: 10px 13px; font-size: .86rem; line-height: 1.55;
    border-radius: 15px; word-wrap: break-word;
  }
  .sf-bubble.ai {
    background: #f2f5f9; color: var(--sf-ink); border-bottom-left-radius: 4px;
  }
  .sf-bubble.user {
    background: var(--sf-brand); color: #fff; border-bottom-right-radius: 4px;
  }
  .sf-bubble strong { font-weight: 650; }

  .sf-typing { padding: 11px 14px; }
  .sf-typing span {
    display: inline-block; width: 6px; height: 6px; margin-right: 3px;
    background: var(--sf-muted); border-radius: 50%; animation: sfb 1.2s infinite;
  }
  .sf-typing span:nth-child(2) { animation-delay: .18s; }
  .sf-typing span:nth-child(3) { animation-delay: .36s; }
  @keyframes sfb { 0%,60%,100% { opacity:.25; transform:translateY(0); }
                   30% { opacity:1; transform:translateY(-3px); } }

  .sf-hint { font-size: .74rem; color: var(--sf-muted); margin: 2px 0 8px; }

  /* the panel's own input and quick-start buttons */
  .st-key-sf_panel [data-testid="stChatInput"] { border-radius: 12px; }
  /* Compact chips. Streamlit's default button padding turned two rows into ~195px of
     the panel, which is what pushed the input past the bottom edge in the first place. */
  .st-key-sf_panel .stButton button {
    font-size: .72rem !important; padding: 2px 8px !important; min-height: 30px !important;
    height: 30px !important; border-radius: 999px !important;
    border: 1px solid var(--sf-line) !important; color: var(--sf-brand) !important;
    background: #f7faff !important; line-height: 1 !important;
  }
  .st-key-sf_panel .stButton button p { font-size: .72rem !important; margin: 0 !important; }

  /* The chips collapse toggle: a plain glyph, not a chip, so it reads as chrome
     rather than as a sixth case to click. */
  .st-key-sf_chips button {
    background: transparent !important; border: none !important;
    color: var(--sf-muted) !important; width: 26px !important; height: 26px !important;
    min-height: 26px !important; padding: 0 !important; border-radius: 7px !important;
  }
  .st-key-sf_chips button:hover {
    background: #eef2f7 !important; color: var(--sf-ink) !important;
  }
  /* Streamlit puts a gap and a margin around EVERY block. Four blocks of that added
     ~90px of invisible padding, which is a big part of why the panel kept outgrowing
     the viewport. Squeeze it all out; the widget is 404px wide and has no room for
     dashboard spacing. */
  .st-key-sf_panel [data-testid="stVerticalBlock"] { gap: .25rem !important; }
  .st-key-sf_panel [data-testid="stHorizontalBlock"] { gap: .25rem !important; }
  .st-key-sf_panel [data-testid="stElementContainer"] { margin: 0 !important; }
  .st-key-sf_panel [data-testid="stMarkdownContainer"] p { margin: 0 !important; }
  .st-key-sf_panel .stButton { margin: 0 !important; }

  /* ---------- sidebar console ---------- */
  [data-testid="stSidebar"] { background: #0f1720; }
  [data-testid="stSidebar"] * { color: #d7e0ea; }
  [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
    color: #fff; font-size: .8rem; letter-spacing: .8px; text-transform: uppercase;
    margin: 14px 0 6px;
  }
  .sf-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
  .sf-tile { background: #17222e; border: 1px solid #22303f; border-radius: 9px; padding: 8px 10px; }
  .sf-tile .k { font-size: .62rem; color: #7f92a6; letter-spacing: .5px; text-transform: uppercase; }
  .sf-tile .v { font-size: 1rem; font-weight: 650; color: #eaf1f8; margin-top: 1px; }
  .sf-tile .v.ok   { color: #46e08a; }
  .sf-tile .v.bad  { color: #ff7a7a; }
  .sf-tile .v.warn { color: #ffbf5c; }
  .sf-mono {
    font-family: ui-monospace, Consolas, monospace; font-size: .69rem; line-height: 1.5;
    background: #0b1219; border: 1px solid #22303f; border-radius: 8px;
    padding: 9px 11px; white-space: pre-wrap; color: #b9c8d8; max-height: 240px;
    overflow-y: auto;
  }
  .sf-deny  { color: #ff8a8a; }
  .sf-allow { color: #6fd39b; }
  .sf-obs   { color: #7f92a6; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# =========================================================================
# Session state
# =========================================================================
def _init():
    ss = st.session_state
    ss.setdefault("messages", [])       # [{role, content, tools:[...]}]
    ss.setdefault("session_id", None)   # for options.resume
    ss.setdefault("pending", None)      # a prompt waiting to be answered
    ss.setdefault("queue", [])          # remaining turns of a scripted case
    ss.setdefault("last", {})           # stats from the most recent turn
    ss.setdefault("open", False)        # is the chat widget open?
    ss.setdefault("booted", False)

    if not ss.booted:
        # First load only. Later re-runs must not wipe a live conversation.
        policy.reset_all()
        backend.reset_faults()
        ss.booted = True
        LOG.info("=" * 62)
        LOG.info("new browser session | model=%s base=%s",
                 agent.MODEL, (agent.AIRTABLE_BASE_ID or "NOT SET")[:12])


_init()
SS = st.session_state


def log_state(where: str) -> None:
    LOG.debug(
        "%s | open=%s msgs=%s pending=%s queued=%s session=%s | "
        "guidance=%s enforce=%s desc=%s",
        where, SS.open, len(SS.messages), bool(SS.pending), len(SS.queue),
        (SS.session_id or "-")[:8],
        policy.guidance_on(), policy.enforcing(), backend.description_mode(),
    )


log_state("rerun")


def reset_all():
    policy.reset_all()
    backend.reset_faults()
    SS.messages = []
    SS.session_id = None
    SS.pending = None
    SS.queue = []
    SS.last = {}


# =========================================================================
# Driving the agent
# =========================================================================
def run_turn(prompt: str, session_id: str | None) -> dict:
    """One customer message in, one agent reply out.

    Runs on its own thread with its own event loop, because spawning asyncio
    subprocesses from Streamlit's script thread is asking for trouble -- the tkinter
    GUI uses the same worker-thread shape for the same reason.

    NOTHING BELOW THIS LINE MAY TOUCH st.session_state.
    Streamlit binds session state to a ScriptRunContext, and that context is
    thread-local. A worker thread has none, so `st.session_state.session_id` raises
    "has no attribute" -- which is not a missing key at all, it is the wrong thread.
    So the session id is passed IN as an argument and handed BACK in the result, and
    the main thread is the only place state is read or written.
    """
    box: dict = {}

    def worker():
        try:
            box["result"] = asyncio.run(_one_turn(prompt, session_id))
        except Exception as exc:                     # surfaced in the UI, not swallowed
            # Log the traceback HERE, on the thread that raised. Re-raising on the
            # script thread loses the original frames, and the frames are the point.
            LOG.error("turn FAILED: %s", exc)
            LOG.debug("traceback:\n%s", traceback.format_exc())
            box["error"] = exc
            box["traceback"] = traceback.format_exc()

    t = threading.Thread(target=worker, name="agent-turn", daemon=True)
    LOG.info("--- turn start | resume=%s | prompt=%r",
             session_id[:8] if session_id else "NEW (first turn)", prompt[:90])
    t.start()
    t.join()

    if "error" in box:
        err = box["error"]
        err.shopfast_traceback = box.get("traceback", "")
        raise err
    return box["result"]


async def _one_turn(prompt: str, session_id: str | None) -> dict:
    options = agent.build_options()

    # Continuity without a long-lived client. Attribute set on the instance we were
    # handed -- shopfast_agent.py itself is untouched.
    if session_id:
        options.resume = session_id

    # Watermarks, so the log shows only THIS turn's hook and retry activity.
    hooks_before = len(HOOK_LOG)
    retries_before = len(backend.RETRY_LOG)

    reply: list[str] = []
    tools: list[dict] = []
    stats: dict = {}

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        reply.append(block.text.strip())
                        LOG.debug("  say  %r", block.text.strip()[:120])
                    elif isinstance(block, ToolUseBlock) and block.name.startswith("mcp__"):
                        tools.append({"name": block.name, "args": block.input,
                                      "result": None, "error": False})
                        LOG.info("  TOOL %s %s", block.name, str(block.input)[:150])
                    elif isinstance(block, ToolUseBlock):
                        LOG.debug("  sdk internal tool: %s", block.name)

            elif isinstance(message, UserMessage):
                for block in message.content or []:
                    if isinstance(block, ToolResultBlock):
                        text = str(block.content)
                        if len(text) > 600:
                            text = text[:600] + f" … (+{len(text) - 600} chars)"
                        LOG.info("  ->   %s%s", "[is_error] " if block.is_error else "",
                                 text[:200].replace("\n", " | "))
                        for entry in reversed(tools):
                            if entry["result"] is None:
                                entry["result"] = text
                                entry["error"] = bool(block.is_error)
                                break

            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                stats = {
                    "terminal": message.terminal_reason or message.stop_reason or "?",
                    "turns": message.num_turns,
                    "cost": message.total_cost_usd or 0.0,
                    "fresh": usage.get("input_tokens", 0),
                    "out": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_write": usage.get("cache_creation_input_tokens", 0),
                    "errors": message.errors,
                }
                if message.session_id:
                    session_id = message.session_id
                LOG.info("  turn end | terminal=%s turns=%s cost=$%.4f",
                         stats["terminal"], stats["turns"], stats["cost"])
                if stats["terminal"] in ("max_turns", "error_max_budget_usd"):
                    LOG.warning("  CAP HIT (%s) -- this turn did NOT succeed, however "
                                "finished the reply sounds.", stats["terminal"])
                if stats.get("errors"):
                    LOG.error("  sdk errors: %s", stats["errors"])

    for d in HOOK_LOG[hooks_before:]:
        if d.decision == "deny":
            LOG.warning("  HOOK DENY  %s -> %s : %s", d.hook, d.tool, d.reason)
        elif d.reason:
            LOG.debug("  hook %-5s %s : %s", d.decision, d.hook, d.reason)
    for r in backend.RETRY_LOG[retries_before:]:
        LOG.info("  retry order=%s attempt=%s status=%s %s wait=%.2fs",
                 r.order_id, r.attempt, r.status, r.outcome, r.delay_s)

    LOG.info("--- turn done | %s tool call(s), session=%s",
             len(tools), (session_id or "-")[:8])

    return {"reply": "\n\n".join(reply) or "(no reply)", "tools": tools,
            "stats": stats, "session_id": session_id}


# =========================================================================
# Rendering a bubble
# =========================================================================
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def md_lite(text: str) -> str:
    """Escape, then re-allow only bold and line breaks.

    The bubbles are hand-built HTML so the alignment can be controlled, which means
    anything the model writes has to be escaped or a stray < eats the rest of the
    panel. Bold survives because the agent leans on it for ticket numbers and amounts,
    and those are exactly what a customer scans for.
    """
    safe = html.escape(text)
    safe = _BOLD.sub(r"<strong>\1</strong>", safe)
    safe = re.sub(r"^\s*[-*]\s+", "• ", safe, flags=re.M)
    return safe.replace("\n", "<br>")


def bubble(role: str, text: str) -> str:
    body = md_lite(text)
    if role == "user":
        return f'<div class="sf-row user"><div class="sf-bubble user">{body}</div></div>'
    return (f'<div class="sf-row ai"><div class="sf-av">🛍️</div>'
            f'<div class="sf-bubble ai">{body}</div></div>')


# =========================================================================
# Sidebar -- the operator console
# =========================================================================
def start_case(key: str):
    """Queue a scripted ticket's turns. Defined above the sidebar because that is
    where the buttons live now."""
    ticket = agent.TICKETS[key]
    reset_all()
    SS.queue = list(ticket.turns)
    SS.pending = SS.queue.pop(0)
    SS.messages.append({"role": "user", "content": SS.pending, "tools": []})


# The five scripted cases. 2 and 5 are the interesting ones -- those are where the
# hooks have something to catch.
QUICK = [
    ("1", "Charged twice"),
    ("2", "$650 refund"),
    ("3", "Long case"),
    ("4", "Where is it?"),
    ("5", "Skip verify"),
]


def tile(label: str, value: str, tone: str = "") -> str:
    return (f'<div class="sf-tile"><div class="k">{label}</div>'
            f'<div class="v {tone}">{value}</div></div>')


with st.sidebar:
    st.markdown("## Agent console")
    st.caption("The customer never sees any of this.")

    st.markdown("### Layers")
    g_on = st.toggle(
        "Guidance", value=policy.guidance_on(),
        help="SKILL.md, the Policy tab, the eligibility verdict and the policy line in "
             "case facts. Everything that TELLS the agent a rule.")
    e_on = st.toggle(
        "Enforcement", value=policy.enforcing(),
        help="PreToolUse hooks. The thing that STOPS it.")
    d_good = st.toggle(
        "Rich tool descriptions", value=backend.description_mode() == "good",
        help="'good' spells out when to use each tool and when not to. 'vague' is one "
             "line each.")

    if g_on != policy.guidance_on():
        LOG.info("=== GUIDANCE -> %s ===", "ON" if g_on else "OFF")
        policy.set_guidance(g_on)
    if e_on != policy.enforcing():
        LOG.info("=== ENFORCEMENT -> %s ===", "ON" if e_on else "OFF")
        policy.set_enforcing(e_on)
    want = "good" if d_good else "vague"
    if want != backend.description_mode():
        LOG.info("=== DESCRIPTIONS -> %s ===", want)
    os.environ["SHOPFAST_DESCRIPTIONS"] = want

    if policy.guidance_on() and policy.enforcing():
        st.caption("Both on — the agent polices itself and the hooks stay silent.")
    elif not policy.guidance_on() and policy.enforcing():
        st.caption("Guidance off, hooks on — ask for a $650 refund and watch it blocked.")
    elif not policy.guidance_on() and not policy.enforcing():
        st.caption("Nothing left. Ask for a $650 refund and it will go through.")
    else:
        st.caption("Prompt-only. It works right up until it doesn't.")

    # Moved out of the chat panel: a customer would never see canned demo scenarios,
    # and in a 404px widget they cost ~90px of conversation.
    st.markdown("### Scripted cases")
    for _key, _label in QUICK:
        _t = agent.TICKETS[_key]
        if st.button(f"{_key}. {_label}", key=f"q{_key}", use_container_width=True,
                     help=f"{_t.title} — {_t.teaches}"):
            start_case(_key)
            SS.open = True          # a case is pointless with the widget shut
            st.rerun()

    st.markdown("### This case")
    last = SS.last
    denials = policy.denials()
    tiles = "".join([
        tile("Verified", "yes" if SESSION.customer_verified else "no",
             "ok" if SESSION.customer_verified else ""),
        tile("Hook blocks", str(len(denials)), "bad" if denials else ""),
        tile("Retries", str(len(backend.RETRY_LOG)), "warn" if backend.RETRY_LOG else ""),
        tile("Refunded", f"${CASE.total_refunded():,.2f}",
             "bad" if CASE.total_refunded() else ""),
        tile("Turns", str(last.get("turns", 0))),
        tile("Cost", f"${last.get('cost', 0.0):.4f}"),
    ])
    terminal = last.get("terminal", "—")
    tone = "ok" if terminal in ("completed", "end_turn") else ("bad" if terminal != "—" else "")
    tiles += tile("terminal_reason", terminal, tone)
    st.markdown(f'<div class="sf-tiles">{tiles}</div>', unsafe_allow_html=True)

    if terminal in ("max_turns", "error_max_budget_usd"):
        st.error(f"Hit a cap ({terminal}). This turn did NOT succeed, however finished "
                 f"the reply sounds.")

    st.markdown("### Tool calls, last turn")
    tools_last = SS.messages[-1].get("tools") if SS.messages else None
    if tools_last:
        for t in tools_last:
            short = t["name"].replace("mcp__", "").replace("__", " · ")
            st.markdown(f"**{short}**")
            st.caption(str(t["args"])[:180])
            if t["result"]:
                st.caption(("⚠ " if t["error"] else "") + t["result"][:220])
    else:
        st.caption("Nothing yet.")

    st.markdown("### Case facts")
    st.caption("Rebuilt from tool results, re-injected every turn.")
    st.markdown(f'<div class="sf-mono">{html.escape(CASE.render(SESSION))}</div>',
                unsafe_allow_html=True)

    st.markdown("### Hook decisions")
    if HOOK_LOG:
        rows = []
        for d in HOOK_LOG[-40:]:
            if d.decision == "deny":
                rows.append(f'<span class="sf-deny">DENY  {d.hook}: {d.reason}</span>')
            elif d.decision == "allow" and d.reason:
                rows.append(f'<span class="sf-allow">allow {d.hook}: {d.reason}</span>')
            elif d.reason:
                rows.append(f'<span class="sf-obs">·     {d.hook}: {d.reason}</span>')
        st.markdown(f'<div class="sf-mono">{"<br>".join(rows)}</div>',
                    unsafe_allow_html=True)
    else:
        st.caption("Nothing yet.")

    st.markdown("### Debug log")
    tail_n = st.slider("lines", 20, 400, 80, step=20, label_visibility="collapsed")
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    if lines:
        st.markdown(f'<div class="sf-mono">{html.escape(chr(10).join(lines[-tail_n:]))}</div>',
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("Download", "\n".join(lines), file_name=LOG_PATH.name,
                               use_container_width=True)
        with c2:
            if st.button("Clear log", use_container_width=True):
                try:
                    LOG_PATH.write_text("", encoding="utf-8")
                except OSError:
                    pass
                st.rerun()
    else:
        st.caption("Empty. Send a message and the turn trace lands here.")

    st.markdown("### ")
    if st.button("Reset conversation", use_container_width=True):
        LOG.info("=== conversation reset by user ===")
        reset_all()
        st.rerun()


# =========================================================================
# The storefront
# =========================================================================
# These six products ARE the rows in the Orders tab. The Wireless Headphones are order
# 12345 (the clean refund); the 4K Monitor is the $650 one that trips the cap. Browsing
# the page and reasoning in the agent are the same data.
PRODUCTS = [
    ("🎧", "Wireless Headphones", "429.99", "Order #12345", "Delivered", ""),
    ("💡", "Desk Lamp",           "189.50", "Order #67890", "Delivered", ""),
    ("🖥️", "4K Monitor",          "650.00", "Order #9876",  "Delayed — customs hold", "warn"),
    ("🪑", "Standing Desk",       "512.00", "Order #1004",  "Status pending", "warn"),
    ("☕", "Coffee Grinder",      "88.25",  "Order #55555", "Delivered", ""),
    ("⌨️", "Keyboard",            "145.00", "Order #4321",  "Delivered 46 days ago", "warn"),
]

st.markdown(
    '<div class="sf-nav">'
    '<div class="sf-logo">Shop<span>Fast</span></div>'
    '<div class="sf-navlinks"><span>Electronics</span><span>Home</span>'
    '<span>Orders</span><span>Help</span></div>'
    '</div>',
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="sf-hero">'
    '<h1>Everything for your desk, delivered fast.</h1>'
    '<p>Electronics and home goods, shipped nationwide. Free returns within 30 days, '
    'and refunds up to $500 handled instantly by our support assistant.</p>'
    '<span class="sf-pill">✓ Free 30-day returns</span>'
    '<span class="sf-pill">✓ Instant refunds under $500</span>'
    '<span class="sf-pill">✓ 24h human follow-up</span>'
    '</div>',
    unsafe_allow_html=True,
)

st.markdown('<div class="sf-sectitle">Your recent orders</div>', unsafe_allow_html=True)
cards = "".join(
    f'<div class="sf-card"><div class="emoji">{e}</div>'
    f'<div class="name">{n}</div><div class="price">${p}</div>'
    f'<div class="meta">{o}</div>'
    f'<span class="sf-tag {cls}">{status}</span></div>'
    for e, n, p, o, status, cls in PRODUCTS
)
st.markdown(f'<div class="sf-grid">{cards}</div>', unsafe_allow_html=True)

st.markdown(
    '<div class="sf-strip">'
    '<div><b>Charged twice?</b>Ask the assistant — refunds under $500 are instant.</div>'
    '<div><b>Order delayed?</b>It will tell you exactly why, from our tracking system.</div>'
    '<div><b>Bigger refund?</b>A human reviews it and replies within 24 hours.</div>'
    '</div>',
    unsafe_allow_html=True,
)

if not agent.AIRTABLE_PAT:
    st.error("AIRTABLE_PAT is not set — every lookup will fail.")


# =========================================================================
# The floating widget
# =========================================================================
if SS.open:
    with st.container(key="sf_panel"):
        st.markdown(
            '<div class="sf-head">'
            '<div class="av">🛍️</div>'
            '<div class="t">ShopFast Support</div>'
            '<div class="s"><span class="sf-dot"></span>Online</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # Messages first, chips down by the input. Every real support widget is laid
        # out this way -- quick replies sit where your eye already is when you are
        # about to type, and the conversation gets the whole panel above them. With
        # the chips on top they pushed the messages into a strip.
        #
        # Newest first, because .sf-msgs is column-reverse -- see the CSS.
        stream = "".join(bubble(m["role"], m["content"]) for m in reversed(SS.messages))
        if SS.pending:
            stream = ('<div class="sf-row ai"><div class="sf-av">🛍️</div>'
                      '<div class="sf-bubble ai sf-typing">'
                      '<span></span><span></span><span></span></div></div>') + stream
        st.markdown(f'<div class="sf-msgs">{stream}</div>', unsafe_allow_html=True)

        # No scripted-case chips here. They are an operator's control, not something a
        # customer would ever see, and they were taking ~90px off the conversation on
        # every screen. They live in the sidebar console with the rest of the demo
        # machinery; the panel is left as a plain support chat.

        if SS.pending:
            try:
                out = run_turn(SS.pending, SS.session_id)
            except Exception as exc:
                st.error(f"Sorry — something went wrong: {exc}")
                tb = getattr(exc, "shopfast_traceback", "") or traceback.format_exc()
                with st.expander("Traceback"):
                    st.code(tb, language="text")
                SS.pending = None
                SS.queue = []
                st.stop()

            SS.messages.append({"role": "assistant", "content": out["reply"],
                                "tools": out["tools"]})
            SS.last = out["stats"]
            SS.session_id = out["session_id"]     # written on the script thread only
            SS.pending = None
            if SS.queue:                          # a scripted case keeps going
                SS.pending = SS.queue.pop(0)
                SS.messages.append({"role": "user", "content": SS.pending, "tools": []})
            st.rerun()

        if prompt := st.chat_input("Type your message…"):
            SS.messages.append({"role": "user", "content": prompt, "tools": []})
            SS.pending = prompt
            st.rerun()

# The launcher sits below the panel, not under it -- the panel's bottom edge is at
# 104px and this is at 26px, so one control opens and closes the widget and the header
# stays clean.
with st.container(key="sf_launcher"):
    if st.button("✕" if SS.open else "💬", key="sf_toggle",
                 help="Close chat" if SS.open else "Chat with support"):
        SS.open = not SS.open
        LOG.info("=== widget %s ===", "opened" if SS.open else "closed")
        st.rerun()
