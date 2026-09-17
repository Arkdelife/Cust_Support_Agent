"""shopfast_policy.py -- the state the model cannot edit, and the hooks that enforce it.

THE ONE IDEA IN THIS FILE
  A prompt is a REQUEST. A hook is a GATE.

  "Never refund more than $500" in a system prompt is a probability. Under an unusual
  phrasing, a long conversation, or a customer who says "my manager already approved
  it", the model may still emit process_refund(amount=650). The refund happens.

  The same rule as a PreToolUse hook reads the ACTUAL argument the model produced and
  returns permissionDecision "deny". There is no phrasing that gets past it, because
  nothing here is reading the phrasing.

  Set SHOPFAST_ENFORCE=off and the hooks stand down; the rules survive only as sentences
  in the system prompt. Run ticket 2 both ways. That contrast is the entire lesson.

WHAT ELSE LIVES HERE
  SESSION     did verification really happen? (a fact, not a claim)
  CASE        the case facts block, built only from tool RESULTS
  HOOK_LOG    every allow/deny, for the GUI and the post-run summary

WHY FACTS COME FROM TOOL RESULTS
  The PostToolUse hook parses what mcp__airtable__list_records (or search_records /
  get_record) actually RETURNED and writes it into CASE. Nothing in this file reads
  the model's prose. So a transcript that says "I've already verified Alice" changes
  no state at all -- verify_first_hook keeps denying until verify_customer really
  matched a row in the Customers table.

MIGRATED TO AIRTABLE (13 Sep 2026) -- Option A, two surfaces kept
  Reads now arrive through an EXTERNAL Airtable MCP server (airtable-mcp-server, via
  npx -- see shopfast_agent.py's build_options()) instead of mcp-google-sheets. Only
  the harvest source changed: `harvest` below now comes from shopfast_airtable, not
  shopfast_sheet, and the PostToolUse matcher in build_hooks() now matches the
  Airtable read tools. Every hook's actual LOGIC (verify-first, refund-cap, the
  redaction principle) is unchanged -- it never depended on where the data came from.
  See docs/ARCHITECTURE.md S3 and docs/OPEN_QUESTIONS.md decision 5.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from shopfast_airtable import harvest
from shopfast_sheet import (
    Customer,
    Order,
    Policy,
    normalize_order_id,
)

# --- the enforcement switch ---------------------------------------------
# The whole point of the demo. "off" downgrades every rule below to a sentence in the
# system prompt, which is exactly the anti-pattern the exam asks you to recognise.
#
# Deliberately a mutable flag behind a function, not a module constant: the GUI flips it
# between runs so you can show the A/B live without restarting the process.
_ENFORCE = os.getenv("SHOPFAST_ENFORCE", "on").strip().lower() not in ("off", "0", "false", "no")


def enforcing() -> bool:
    return _ENFORCE


def set_enforcing(value: bool) -> None:
    global _ENFORCE
    _ENFORCE = bool(value)


# The GUIDANCE layer, switched separately from the ENFORCEMENT layer.
#
# Added after a live run made the original demo fall flat. With SKILL.md loaded, the
# model read the Policy tab, saw $650 > $500, and escalated without ever CALLING
# process_refund -- so the hook never fired and "hook denials" read 0. Correct
# behaviour, useless demonstration: you cannot see a backstop that nothing hit.
#
# Two switches give the honest four-way matrix:
#
#   guidance  enforce   what happens on a $650 refund request
#   --------  -------   -------------------------------------------------------
#   on        on        model self-polices. Hook silent. (the well-built system)
#   on        off       model self-polices. Nothing behind it. (fragile: works
#                       until the phrasing, the model, or the day changes)
#   off       on        model ATTEMPTS the refund -> HOOK DENIES it  <- the point
#   off       off       model attempts the refund -> IT GOES THROUGH <- the loss
#
# The bottom two rows are the argument. The hook is not there for the model that
# already knows the rule; it is there for the one that does not.
_GUIDANCE = os.getenv("SHOPFAST_GUIDANCE", "on").strip().lower() not in (
    "off", "0", "false", "no")


def guidance_on() -> bool:
    return _GUIDANCE


def set_guidance(value: bool) -> None:
    global _GUIDANCE
    _GUIDANCE = bool(value)

# Tool namespaces. In-process tools are exposed as mcp__<server>__<tool>, so the server
# name we choose in mcp_servers becomes part of every tool name AND every hook matcher.
# Rename the server and you must rename these too -- the server has no idea what we
# called it.
SHOPFAST = "mcp__shopfast__"
AIRTABLE = "mcp__airtable__"

T_VERIFY = f"{SHOPFAST}verify_customer"
T_LOOKUP = f"{SHOPFAST}lookup_order"
T_REFUND = f"{SHOPFAST}process_refund"
T_ESCALATE = f"{SHOPFAST}escalate_to_human"
T_EMAIL = f"{SHOPFAST}send_email"

# Matchers are matched against the tool name and accept alternation, the same way
# "Write|Edit" does for built-in tools.
#
# escalate_to_human is deliberately NOT in this list. It is the safety valve: gating the
# escape hatch behind verification would mean an unverifiable customer could never reach
# a human, which is worse than the risk it prevents.
#
# lookup_order is in here too, added after a live run. Order contents, totals and
# delivery addresses are personal data: handing them to an unverified caller is a
# disclosure, not a convenience. The model reached this conclusion on its own and
# started asking for the email before looking anything up -- so the gate now matches
# the behaviour instead of contradicting it.
M_NEEDS_VERIFY = f"{T_REFUND}|{T_EMAIL}|{T_LOOKUP}"
M_REFUND = T_REFUND
# The three Airtable MCP tools that return row DATA (list_tables / describe_table
# return schema, not customer data, so they are not harvested and not matched here).
M_AIRTABLE_READ = (
    f"{AIRTABLE}list_records|{AIRTABLE}search_records|{AIRTABLE}get_record"
)


# --- clock ---------------------------------------------------------------
def today() -> date:
    """Overridable so the demo's return-window maths does not rot as months pass."""
    pinned = os.getenv("SHOPFAST_TODAY", "").strip()
    if pinned:
        try:
            return datetime.strptime(pinned, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


# --- session state -------------------------------------------------------
@dataclass
class Session:
    """Facts about THIS conversation that only a real tool result can change."""

    customer_verified: bool = False
    customer: Customer | None = None

    # Set by verify_customer when an email does NOT match any row. Lets the agent say
    # "that email isn't on the account" instead of silently looping.
    failed_verifications: int = 0

    # Pattern 5, trigger 5: turns since anything actually moved forward.
    turns_without_progress: int = 0

    def reset(self) -> None:
        self.customer_verified = False
        self.customer = None
        self.failed_verifications = 0
        self.turns_without_progress = 0


@dataclass
class Refund:
    order_id: str
    amount: float
    reason: str
    status: str          # "processed" | "blocked"


@dataclass
class Escalation:
    ticket_id: str
    order_id: str
    category: str
    reason: str


@dataclass
class CaseFacts:
    """The block re-injected on every user turn.

    Built ONLY from tool results. This is what stops a 20-turn conversation from
    quietly losing the order total, and it is why summarising the rest of the history
    is safe: the numbers were never only in the prose.
    """

    orders: dict[str, Order] = field(default_factory=dict)
    delays: dict[str, str] = field(default_factory=dict)
    customers: dict[str, Customer] = field(default_factory=dict)   # keyed by lowercase email
    policy: Policy = field(default_factory=Policy)

    refunds: list[Refund] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    emails_sent: int = 0

    # Order IDs the customer mentioned that were NOT found in the sheet. Tracked so the
    # agent says "I can't find that one" rather than answering about a nearby ID.
    unknown_orders: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.orders.clear()
        self.delays.clear()
        self.customers.clear()
        self.policy = Policy()
        self.refunds.clear()
        self.escalations.clear()
        self.emails_sent = 0
        self.unknown_orders.clear()

    # --- ingest ---------------------------------------------------------
    def absorb(self, payload: Any) -> str | None:
        """Fold an Airtable read into the case. Returns the table name(s) it recognised.

        One read can carry several tables (a batch of MCP content blocks), so this
        handles a list, not a single table.
        """
        got = harvest(payload)
        if not got.tabs:
            return None
        for cust in got.customers:
            if cust.email:
                self.customers[cust.email.lower()] = cust
        for order in got.orders:
            self.orders[order.order_id] = order
        self.delays.update(got.delays)
        for rule, value in got.policy_rules:
            self.policy.apply(rule, value)
        return ", ".join(got.tabs)

    def total_refunded(self) -> float:
        return sum(r.amount for r in self.refunds if r.status == "processed")

    def summary_for_agent(self, tab: str) -> str:
        """The REDACTED summary shown to the agent after a reference-table read.

        Shared by both read paths -- harvest_hook (Option A, the external Airtable
        MCP surface) and shopfast_backend.h_read_reference_data (Option B, the
        in-process fallback used on Streamlit Community Cloud, where Node/npx is not
        reliably available -- see docs/ARCHITECTURE.md S3 and S7). Same redaction
        principle either way: the caller has ALREADY absorbed the rows into CASE by
        the time this is called; this only decides what the agent gets told about it.
        """
        return (
            f"Loaded reference data: {tab}. "
            f"{len(self.customers)} customer record(s), {len(self.orders)} order(s), "
            f"{len(self.delays)} delay reason(s), policy "
            f"{'loaded' if self.policy.loaded else 'on defaults'}.\n"
            "The rows themselves are withheld: this table covers ALL customers, and it "
            "is not yours to quote. Use mcp__shopfast__verify_customer to identify the "
            "customer and mcp__shopfast__lookup_order for one order at a time. If you "
            "do not have an order ID, ask the customer for it -- you cannot list the "
            "records on file."
        )

    # --- render ---------------------------------------------------------
    def render(self, session: Session) -> str:
        """The fenced block. Short and stable -- it is re-sent every single turn."""
        lines = ["=== CASE FACTS (system-maintained, derived from tool results) ==="]

        if session.customer_verified and session.customer:
            c = session.customer
            lines.append(f"Customer      : {c.name} <{c.email}>  [{c.customer_id}]"
                         f"{'  tier ' + c.tier if c.tier else ''}")
            lines.append("Verified      : YES (matched a row in the Customers tab)")
        else:
            lines.append("Customer      : NOT VERIFIED")
            lines.append("Verified      : NO -- refunds and emails are blocked until "
                         "verify_customer succeeds")

        if self.orders:
            lines.append(f"Orders loaded : {len(self.orders)}")
            for oid, o in sorted(self.orders.items()):
                total = f"${o.total:,.2f}" if o.total is not None else "(blank)"
                bits = [f"  {oid}", o.status or "(blank status)", total]
                if o.item:
                    bits.append(o.item)
                if oid in self.delays:
                    bits.append(f"delay: {self.delays[oid]}")
                if o.missing:
                    bits.append("NOT FILLED IN: " + ", ".join(o.missing))
                lines.append("   " + "  |  ".join(bits))

        if self.unknown_orders:
            lines.append("Not in sheet  : " + ", ".join(sorted(self.unknown_orders))
                         + "  (do not answer about a different order instead)")

        # The case facts block is itself a guidance channel, and the sneakiest one:
        # it is re-injected on EVERY turn. Three live runs failed to make the hook fire
        # because this single line was quietly telling the model the $500 limit even
        # after SKILL.md, the Policy tab and the eligibility verdict had all been
        # removed. Guidance leaks through whatever you forget to look at.
        if guidance_on():
            p = self.policy
            lines.append(
                f"Policy        : auto-refund limit ${p.refund_auto_limit_usd:,.2f}  |  "
                f"return window {p.return_window_days} days  |  "
                f"{'from sheet' if p.loaded else 'DEFAULTS -- Policy tab not read yet'}"
            )

        if self.refunds:
            for r in self.refunds:
                lines.append(f"Refund        : {r.order_id}  ${r.amount:,.2f}  {r.status.upper()}"
                             f"  ({r.reason})")
            lines.append(f"Total refunded: ${self.total_refunded():,.2f}")
        if self.escalations:
            for e in self.escalations:
                lines.append(f"Escalated     : {e.ticket_id}  order {e.order_id or '-'}"
                             f"  [{e.category}]  {e.reason}")
        if self.emails_sent:
            lines.append(f"Emails sent   : {self.emails_sent}")

        lines.append(f"Today         : {today().isoformat()}")
        lines.append("=== END CASE FACTS ===")
        return "\n".join(lines)


SESSION = Session()
CASE = CaseFacts()


# --- hook decision log ---------------------------------------------------
@dataclass
class HookDecision:
    hook: str
    tool: str
    decision: str        # "allow" | "deny" | "observe"
    reason: str = ""


HOOK_LOG: list[HookDecision] = []


def reset_all() -> None:
    """Between tickets. The GUI calls this; so does --ticket all."""
    SESSION.reset()
    CASE.reset()
    HOOK_LOG.clear()


def _log(hook: str, tool: str, decision: str, reason: str = "") -> None:
    HOOK_LOG.append(HookDecision(hook=hook, tool=tool, decision=decision, reason=reason))


def denials() -> list[HookDecision]:
    return [d for d in HOOK_LOG if d.decision == "deny"]


# --- hook return shapes --------------------------------------------------
# Verified against claude_agent_sdk 0.2.128 types.py:413 and :448.
def _deny(reason: str) -> dict[str, Any]:
    """Block a tool call.

    The reason string is not decoration -- the model READS it and decides what to do
    next. A denial that only says "not allowed" leaves the model apologising to the
    customer with no next step, which looks like a broken agent. Every reason below
    therefore names the tool to call instead.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow() -> dict[str, Any]:
    """Say nothing and the call proceeds to the normal permission flow."""
    return {}


# --- hook 1: the audit hook ---------------------------------------------
# matcher=None, so it sees EVERY tool call. Built first and kept, because it is how you
# discover what tool names actually look like -- guessing the matcher string is the
# single most common way a hooks setup silently never fires.
async def audit_hook(input_data: dict[str, Any], tool_use_id: str | None,
                     context: dict[str, Any]) -> dict[str, Any]:
    tool = input_data.get("tool_name", "?")
    _log("audit", tool, "observe")
    if os.getenv("SHOPFAST_TRACE_HOOKS", "").strip().lower() in ("1", "on", "true", "yes"):
        print(f"    [hook audit] PreToolUse saw tool_name={tool!r} "
              f"args={input_data.get('tool_input')}")
    return _allow()


# --- hook 2: verify before you act --------------------------------------
async def verify_first_hook(input_data: dict[str, Any], tool_use_id: str | None,
                            context: dict[str, Any]) -> dict[str, Any]:
    """Tool-ORDER enforcement: no money and no mail until identity is established.

    The check reads SESSION, which only verify_customer can set, and only on a real
    match against the Customers tab. A customer (or a model) asserting "I verified
    last time" moves nothing here.
    """
    tool = input_data.get("tool_name", "?")

    if not enforcing():
        _log("verify_first", tool, "allow", "ENFORCE=off -- prompt-only, not enforced")
        return _allow()

    if SESSION.customer_verified:
        _log("verify_first", tool, "allow", f"verified as {SESSION.customer.customer_id}"
             if SESSION.customer else "verified")
        return _allow()

    reason = (
        "BLOCKED: the customer has not been verified yet. Call "
        f"{T_VERIFY} with their registered email address first, then retry this call. "
        "If you do not have their email, ask the customer for it. Do not guess one and "
        "do not proceed without it."
    )
    _log("verify_first", tool, "deny", "customer not verified")
    return _deny(reason)


# --- hook 3: the refund cap ---------------------------------------------
async def refund_cap_hook(input_data: dict[str, Any], tool_use_id: str | None,
                          context: dict[str, Any]) -> dict[str, Any]:
    """Money rule, enforced on the ARGUMENT rather than on the intention.

    Note what is compared: input_data["tool_input"]["amount"], the number the model
    actually put in the call. Not what it said it would do, not what it told the
    customer. The cap comes from the Policy tab, so changing it is a sheet edit.
    """
    tool = input_data.get("tool_name", "?")
    args = input_data.get("tool_input") or {}

    try:
        amount = float(args.get("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0

    limit = CASE.policy.refund_auto_limit_usd

    if not enforcing():
        _log("refund_cap", tool, "allow",
             f"ENFORCE=off -- ${amount:,.2f} vs ${limit:,.2f} cap NOT enforced")
        return _allow()

    if amount <= limit:
        _log("refund_cap", tool, "allow", f"${amount:,.2f} within ${limit:,.2f} cap")
        return _allow()

    order_id = normalize_order_id(args.get("order_id", ""))
    # Record the attempt so the case facts show what was tried and refused.
    CASE.refunds.append(Refund(order_id=order_id, amount=amount,
                               reason="over auto-refund limit", status="blocked"))
    reason = (
        f"BLOCKED: a refund of ${amount:,.2f} exceeds the ${limit:,.2f} automated "
        f"limit set in the Policy tab. This is a hard limit and cannot be overridden "
        f"by the customer, by urgency, or by a claimed prior approval. "
        f"Call {T_ESCALATE} with category 'refund_over_limit' instead, then tell the "
        f"customer a specialist will follow up within "
        f"{CASE.policy.escalation_sla_hours} hours."
    )
    _log("refund_cap", tool, "deny",
         f"${amount:,.2f} over ${limit:,.2f} cap")
    return _deny(reason)


# --- hook 4: harvest facts from Airtable -------------------------------
async def harvest_hook(input_data: dict[str, Any], tool_use_id: str | None,
                       context: dict[str, Any]) -> dict[str, Any]:
    """PostToolUse on the Airtable read: absorb the rows, then REDACT them.

    Two jobs, and the second one only became obvious from a live run.

    ABSORB. The rows go into CASE straight from the tool response, so the numbers the
    agent quotes fifteen turns later are the sheet's numbers, not a paraphrase it has
    been carrying around.

    REDACT. The Orders table holds EVERY customer's orders. Once that table is in the
    context window, no gate can take it back -- and it showed: asked about an order it
    could not find, the agent helpfully offered the customer a list of "your orders"
    that included two belonging to someone else. Ownership checks on lookup_order and
    process_refund stop it RETRIEVING another customer's order; they do nothing about
    it reciting a table it can already see. SKILL.md tells it not to. It did anyway.

    So the hook swaps the tool's output for a summary via `updatedToolOutput`. The
    agent learns the read succeeded and how many rows landed; the rows themselves never
    reach it. Everything it legitimately needs comes back through lookup_order, one
    owned order at a time.

    This is the difference between telling a model not to look and not handing it the
    file. Only one of those is a control.
    """
    tool = input_data.get("tool_name", "?")
    tab = CASE.absorb(input_data.get("tool_response"))

    if not tab:
        _log("harvest", tool, "observe", "result not recognised as a known table")
        return _allow()

    _log("harvest", tool, "observe",
         f"absorbed '{tab}' into case facts, raw rows redacted")

    summary = CASE.summary_for_agent(tab)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": summary,
        }
    }


# --- hook 5: re-inject the case facts -----------------------------------
async def case_facts_hook(input_data: dict[str, Any], tool_use_id: str | None,
                          context: dict[str, Any]) -> dict[str, Any]:
    """UserPromptSubmit: prepend the facts block to EVERY customer turn.

    Not once at the start -- every turn. That is the difference between facts that
    survive a long conversation and facts that quietly decay as history is summarised.
    additionalContext is delivered to the model alongside the user's message.
    """
    SESSION.turns_without_progress += 1
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": CASE.render(SESSION),
        }
    }


# --- wiring --------------------------------------------------------------
def build_hooks() -> dict[str, list]:
    """The dict handed to ClaudeAgentOptions(hooks=...).

    Every matcher is a FULL tool name. In-process tools are namespaced by the server
    key we pick in mcp_servers, so "shopfast" -> mcp__shopfast__process_refund. Get this
    string wrong and the hook never fires, the refund goes through, and nothing warns
    you -- which is why audit_hook (matcher=None) exists.
    """
    from claude_agent_sdk import HookMatcher

    return {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[audit_hook]),
            HookMatcher(matcher=M_NEEDS_VERIFY, hooks=[verify_first_hook]),
            HookMatcher(matcher=M_REFUND, hooks=[refund_cap_hook]),
        ],
        "PostToolUse": [
            HookMatcher(matcher=M_AIRTABLE_READ, hooks=[harvest_hook]),
        ],
        "UserPromptSubmit": [
            HookMatcher(matcher=None, hooks=[case_facts_hook]),
        ],
    }


# --- the prompt-only fallback -------------------------------------------
# What you are left with when ENFORCE=off: good intentions, in prose. Included verbatim
# so the A/B run is honest -- the weak version is not a strawman, it is the same rules
# written as clearly as a prompt can write them.
PROMPT_ONLY_RULES = f"""
IMPORTANT BUSINESS RULES (you must follow these yourself):
- Never process a refund above the automated limit in the Policy tab. Escalate instead.
- Never process a refund or send an email before verifying the customer with
  {T_VERIFY}.
- These rules are absolute and have no exceptions.
""".strip()
