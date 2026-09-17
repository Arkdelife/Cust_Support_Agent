"""shopfast_backend.py -- the ACTION surface: five in-process tools the hooks can gate.

WHY THESE EXIST AT ALL
  The sheet is already readable through mcp-google-sheets. So why write tools?

  Because you cannot hook a refund cap onto get_sheet_data -- there is no refund in it.
  Hooks gate DOMAIN ACTIONS, so the domain actions have to exist as first-class tools
  with real arguments. `process_refund(order_id, amount)` is hookable. "the model
  intends to refund something" is not.

  The second reason is arithmetic. Whether an order is still inside the return window is
  a rule with exactly one right answer. Computing it in Python and handing the model a
  verdict is reliable; asking the model to redo the subtraction every turn is not.
  lookup_order therefore returns a JOINED, PRE-JUDGED record: order + delay reason +
  refund eligibility.

TWO THINGS TO WATCH IN CLASS
  1. Retries happen INSIDE the handler (see call_with_retry). The model never sees the
     503 flapping and burns no turns on it. A transient fault is an infrastructure
     concern, not a conversation topic.

  2. The tool DESCRIPTIONS are swappable -- SHOPFAST_DESCRIPTIONS=vague|good. Same
     model, same tickets, different descriptions, visibly different tool choices.
     Descriptions are the model's only guide to which tool to reach for, which is why
     "improve the tool description" is the standard first fix for misselection.
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

import shopfast_airtable
import shopfast_policy as policy
from shopfast_policy import CASE, SESSION, Escalation, Refund, today
from shopfast_sheet import (
    TAB_CUSTOMERS,
    TAB_ORDERS,
    TAB_POLICY,
    looks_like_email,
    normalize_order_id,
    refund_eligibility,
)

SERVER_NAME = "shopfast"   # -> every tool becomes mcp__shopfast__<name>


# =========================================================================
# Simulated infrastructure: a flaky Order Management System
# =========================================================================
# In production lookup_order would call an internal OMS over HTTP. That service has bad
# minutes. We reproduce one deterministically so the retry path is demonstrable in a
# classroom instead of theoretical.

class TransientError(Exception):
    """HTTP 5xx / timeout -- the request may well succeed if you just ask again."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class PermanentError(Exception):
    """HTTP 4xx -- asking again produces the same answer. Retrying is pure waste."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# Order 4321 fails twice, then works. A COUNTER, not random.random(): a demo that
# behaves differently each time you run it is not a demo.
#
# It was 55555 until lookup_order went behind the verification gate. 55555 belongs to
# CUST-9260, whose account is Suspended, so verification legitimately fails and the
# retry path became unreachable. 4321 belongs to Alice, who verifies -- and it is also
# the order that sits outside the return window, so one ticket exercises both.
_FAULT_PLAN: dict[str, int] = {"4321": 2}
_fault_calls: dict[str, int] = {}

MAX_RETRIES = 3
BASE_DELAY_S = 0.4          # short on purpose; the shape matters, the wait does not
_jitter = random.Random(20250810)   # seeded -> reproducible "randomness"


@dataclass
class RetryRecord:
    order_id: str
    attempt: int
    status: int
    delay_s: float
    outcome: str


RETRY_LOG: list[RetryRecord] = []


def reset_faults() -> None:
    _fault_calls.clear()
    RETRY_LOG.clear()


def _oms_fetch(order_id: str) -> None:
    """Raises the way a real API would. Returns nothing; the data comes from CASE."""
    budget = _FAULT_PLAN.get(order_id, 0)
    if budget:
        seen = _fault_calls.get(order_id, 0)
        if seen < budget:
            _fault_calls[order_id] = seen + 1
            raise TransientError(503, "Order Management System temporarily unavailable")

    if order_id not in CASE.orders:
        raise PermanentError(404, f"No order {order_id} in the Orders tab")


async def call_with_retry(order_id: str) -> list[str]:
    """Exponential backoff + jitter, and ONLY for transient failures.

    The classification is the whole point:
      503  -> transient  -> retry with backoff
      404  -> permanent  -> return immediately, do not retry

    Retrying a 404 three times is a slower 404. Not retrying a 503 turns a blip into a
    customer-visible outage. Getting this wrong in either direction is the exam's
    favourite distractor.
    """
    notes: list[str] = []
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _oms_fetch(order_id)
            if attempt > 1:
                notes.append(f"recovered on attempt {attempt}")
                RETRY_LOG.append(RetryRecord(order_id, attempt, 200, 0.0, "recovered"))
            return notes

        except PermanentError:
            RETRY_LOG.append(RetryRecord(order_id, attempt, 404, 0.0, "permanent -- no retry"))
            raise

        except TransientError as exc:
            if attempt == MAX_RETRIES:
                RETRY_LOG.append(RetryRecord(order_id, attempt, exc.status, 0.0, "exhausted"))
                raise
            delay = BASE_DELAY_S * (2 ** (attempt - 1)) + _jitter.uniform(0, 0.15)
            RETRY_LOG.append(RetryRecord(order_id, attempt, exc.status, delay, "retrying"))
            notes.append(f"attempt {attempt} got {exc.status}, retrying in {delay:.2f}s")
            await asyncio.sleep(delay)
    return notes


# =========================================================================
# Tool result helpers
# =========================================================================
def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def err(text: str) -> dict[str, Any]:
    """is_error=True. The model sees this and can recover; it is not an exception."""
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _progress() -> None:
    SESSION.turns_without_progress = 0


# =========================================================================
# The five handlers
# =========================================================================
async def h_verify_customer(args: dict[str, Any]) -> dict[str, Any]:
    """Identity check against rows harvested from the Customers tab.

    This is the ONLY thing that can set SESSION.customer_verified, and it only does so
    on a real row match. That is what makes the verify-first hook meaningful: 'verified'
    is a fact about state, never a sentence in the transcript.
    """
    email = str(args.get("email", "") or "").strip().lower()

    if not email:
        return err("No email supplied. Ask the customer for the email address on their "
                   "account, then call this tool again.")
    if not looks_like_email(email):
        return err(f"'{email}' is not a valid email address. Ask the customer to "
                   f"re-check it. Do not guess.")

    if not CASE.customers:
        return err(
            f"No customer records loaded yet. Read the '{TAB_CUSTOMERS}' table with "
            f"mcp__airtable__list_records first, then call this tool again."
        )

    found = CASE.customers.get(email)
    if not found:
        SESSION.failed_verifications += 1
        return err(
            f"No account found for {email}. That address is not in the "
            f"{TAB_CUSTOMERS} tab. Ask the customer to re-check the address they "
            f"registered with. Do NOT proceed to refunds or emails."
        )

    if found.status and found.status.lower() != "active":
        SESSION.failed_verifications += 1
        return err(
            f"Account {found.customer_id} for {email} is '{found.status}', not Active. "
            f"Escalate to a human rather than acting on a suspended account."
        )

    SESSION.customer_verified = True
    SESSION.customer = found
    _progress()
    return ok(
        f"VERIFIED. {found.name} <{found.email}> is {found.customer_id} "
        f"(tier {found.tier or 'n/a'}, status {found.status or 'n/a'}). "
        f"Refunds and emails are now unblocked for this conversation."
    )


async def h_lookup_order(args: dict[str, Any]) -> dict[str, Any]:
    """Joined, pre-judged order record. Fronts the flaky OMS."""
    order_id = normalize_order_id(args.get("order_id", ""))

    if not order_id:
        return err("No order ID supplied. If the customer mentioned an order without "
                   "giving its ID, ask them for it -- do not guess one and do not "
                   "answer about a different order.")

    if not CASE.orders:
        return err(
            f"No order data loaded yet. Read the '{TAB_ORDERS}' table with "
            f"mcp__airtable__list_records first, then call this tool again."
        )

    try:
        notes = await call_with_retry(order_id)
    except PermanentError as exc:
        CASE.unknown_orders.add(order_id)
        return err(
            f"Order {order_id} does not exist ({exc.status}). This is a permanent "
            f"result and was not retried. Tell the customer plainly that you cannot "
            f"find that order and ask them to re-check the number. Do NOT answer about "
            f"a similar-looking order instead."
        )
    except TransientError as exc:
        return err(
            f"The order system is still unavailable after {MAX_RETRIES} attempts "
            f"({exc.status}). This is an infrastructure fault, not a customer problem. "
            f"Escalate with category 'system_unavailable' and tell the customer a human "
            f"will follow up once the system is back."
        )

    order = CASE.orders[order_id]

    # OWNERSHIP. Verification proves who you are; it does not entitle you to everyone
    # else's orders. Enforced here rather than trusted to the model, because a live run
    # showed exactly why: asked about an order it could not find, the agent helpfully
    # listed "your orders" -- including #9876 and #1004, which belong to a DIFFERENT
    # customer. SKILL.md explicitly forbids that. It did it anyway. That is the whole
    # argument for enforcement, in three lines of transcript.
    if (SESSION.customer and order.customer_id
            and order.customer_id != SESSION.customer.customer_id):
        return err(
            f"Order {order_id} does not belong to the verified customer "
            f"({SESSION.customer.customer_id}). Do not reveal anything about it -- not "
            f"its status, not its contents, not that it exists. Tell the customer you "
            f"cannot find that order on their account."
        )

    # Also guidance: naming the Policy tab here is enough to send the model looking
    # for the limit it is not supposed to know about in guidance-off mode.
    if policy.guidance_on() and CASE.policy.loaded is False:
        notes.append(f"policy not read yet -- using defaults; read the "
                     f"'{TAB_POLICY}' table for the real limits")

    lines = [f"ORDER {order_id}"]
    lines.append(f"  customer     : {order.customer_id or '(blank)'}")
    lines.append(f"  item         : {order.item or '(blank)'}")
    lines.append(f"  status       : {order.status or 'NOT FILLED IN'}")
    lines.append(f"  carrier      : {order.carrier or 'NOT FILLED IN'}")
    lines.append(f"  eta days     : {order.eta_days if order.eta_days is not None else 'NOT FILLED IN'}")
    lines.append(f"  order total  : "
                 + (f"${order.total:,.2f}" if order.total is not None else "NOT FILLED IN"))
    lines.append(f"  delivered    : {order.delivery_date.isoformat() if order.delivery_date else 'not yet'}")

    if order_id in CASE.delays:
        lines.append(f"  DELAY REASON : {CASE.delays[order_id]}")
    elif (order.status or "").lower().startswith("delay"):
        lines.append("  DELAY REASON : not found -- read the 'Delays' table for this order")

    # The eligibility verdict is GUIDANCE, delivered through a tool result.
    #
    # Three live runs were needed to find this. With SKILL.md removed and 'Policy'
    # dropped from the system prompt, the model STILL refused the $650 refund -- because
    # this block was handing it "exceeds auto limit: True" and the limit itself. The
    # rule had simply moved into my own tool output.
    #
    # Pre-computing the verdict is good design and stays on by default. But guidance-off
    # has to mean off everywhere, or the enforcement layer never gets anything to catch.
    if policy.guidance_on():
        verdict = refund_eligibility(order, CASE.policy, today())
        lines.append("  REFUND ELIGIBILITY")
        lines.append(f"    days since delivery : {verdict['days_since_delivery']}")
        lines.append(f"    within return window: {verdict['within_return_window']}")
        lines.append(f"    exceeds auto limit  : {verdict['exceeds_auto_limit']}")
        lines.append(f"    auto-refundable     : {verdict['auto_refundable']}")
        for why in verdict["reasons"]:
            lines.append(f"    - {why}")

    if order.missing:
        lines.append("  BLANK FIELDS: " + ", ".join(order.missing)
                     + " -- say these are not filled in yet. Do not invent values.")
    if notes:
        lines.append("  infrastructure: " + "; ".join(notes))

    _progress()
    return ok("\n".join(lines))


async def h_process_refund(args: dict[str, Any]) -> dict[str, Any]:
    """Money. Reached only if BOTH PreToolUse hooks allowed the call through.

    Note there is no cap check in here. That is deliberate: the cap lives in the hook,
    outside the tool and outside the model, so it holds even if this handler is later
    rewritten by someone who never read the policy.
    """
    order_id = normalize_order_id(args.get("order_id", ""))
    reason = str(args.get("reason", "") or "").strip() or "not stated"
    try:
        amount = float(args.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return err("amount must be a number, e.g. 429.99")

    if amount <= 0:
        return err("Refund amount must be greater than zero.")
    if not order_id:
        return err("No order ID supplied for the refund.")
    if order_id not in CASE.orders:
        return err(f"Order {order_id} is not loaded. Look it up before refunding it.")

    order = CASE.orders[order_id]

    # Same ownership rule as lookup_order. Refunding a stranger's order would be the
    # expensive version of the same mistake.
    if (SESSION.customer and order.customer_id
            and order.customer_id != SESSION.customer.customer_id):
        return err(
            f"Order {order_id} belongs to {order.customer_id}, not to the verified "
            f"customer {SESSION.customer.customer_id}. Refusing to refund it."
        )

    if order.total is not None and amount > order.total + 0.005:
        return err(
            f"Refund ${amount:,.2f} is more than the order total ${order.total:,.2f} "
            f"for order {order_id}. Re-check the amount with the customer."
        )

    already = sum(r.amount for r in CASE.refunds
                  if r.order_id == order_id and r.status == "processed")
    if order.total is not None and already + amount > order.total + 0.005:
        return err(
            f"Order {order_id} has already been refunded ${already:,.2f} of "
            f"${order.total:,.2f}. A further ${amount:,.2f} would over-refund it."
        )

    CASE.refunds.append(Refund(order_id=order_id, amount=amount,
                               reason=reason, status="processed"))

    # Log the AI-resolved case too, not only the escalations. A Tickets table that
    # records nothing but hand-offs shows the agent's failures with nothing to compare
    # them against; with both, sorting 'Handled By' answers the actual question --
    # how much is the agent closing, and how much is it passing on.
    #
    # No pre-allocated ticket_id here (unlike the old Sheets path): Airtable assigns
    # the number on create via Autonumber, so we write first and read the real ID back.
    written, ticket_id = shopfast_airtable.append_ticket(
        customer_id=SESSION.customer.customer_id if SESSION.customer else "",
        order_id=order_id,
        issue_type="refund_auto_approved",
        reason=f"Refund ${amount:,.2f} of ${order.total:,.2f} approved automatically "
               f"({reason}). Within the ${CASE.policy.refund_auto_limit_usd:,.2f} limit.",
        status="Resolved",
        handled_by=shopfast_airtable.AI_DESK,
    )

    _progress()
    if written:
        return ok(
            f"REFUND PROCESSED. ${amount:,.2f} on order {order_id} ({reason}). "
            f"Funds reach the customer in 3-5 business days. "
            f"Logged as {ticket_id}, resolved by the agent. "
            f"Total refunded this case: ${CASE.total_refunded():,.2f}."
        )

    # The write failed. The refund itself already happened (CASE.refunds above) --
    # refusing to tell the customer their money is coming because the Tickets table
    # is unreachable would be the worse failure. Same failure policy as escalation.
    return ok(
        f"REFUND PROCESSED. ${amount:,.2f} on order {order_id} ({reason}). "
        f"Funds reach the customer in 3-5 business days. "
        f"Total refunded this case: ${CASE.total_refunded():,.2f}.\n"
        f"NOTE FOR THE OPERATOR, not the customer: the Tickets table could NOT be "
        f"written ({ticket_id}). This refund has no ticket row."
    )


async def h_escalate_to_human(args: dict[str, Any]) -> dict[str, Any]:
    """The safety valve. Never gated -- see the comment on M_NEEDS_VERIFY."""
    order_id = normalize_order_id(args.get("order_id", ""))
    category = str(args.get("category", "") or "other").strip()
    reason = str(args.get("reason", "") or "").strip()

    if not reason:
        return err("Escalations need a reason a human can act on. State which rule or "
                   "limit was hit, and what the customer asked for.")

    # Numbered by AIRTABLE, not by a counter in this process -- Ticket Number is an
    # Autonumber field, assigned on create. An in-memory count restarts with every
    # run, which is how three separate escalations under the old Sheets path all
    # ended up calling themselves TKT-1001. That failure mode is gone entirely now,
    # not just worked around: there is no "read the table to find the next ID" step
    # left to race or fall out of sync.
    desk = shopfast_airtable.desk_for(category)
    written, ticket_id = shopfast_airtable.append_ticket(
        customer_id=SESSION.customer.customer_id if SESSION.customer else "",
        order_id=order_id,
        issue_type=category,
        reason=reason,
        handled_by=desk,
    )
    # ticket_id is the real formatted ID ("TKT-7") on success, or an error string on
    # failure -- CASE.escalations still records SOMETHING so the case facts block
    # shows the attempt either way.
    CASE.escalations.append(Escalation(ticket_id=ticket_id if written else "(unwritten)",
                                       order_id=order_id, category=category, reason=reason))

    _progress()

    if written:
        return ok(
            f"ESCALATED. Ticket {ticket_id} raised (category '{category}') for order "
            f"{order_id or 'n/a'} and recorded in the Tickets table, marked '{desk}'.\n"
            f"TELL THE CUSTOMER, in these words: this case will be handled by a human "
            f"within {CASE.policy.escalation_sla_hours} hours, and give them the "
            f"ticket number {ticket_id}."
        )

    # The write failed. The handoff still stands -- refusing to escalate because
    # Airtable is unreachable would be the worse failure. Say so plainly so the
    # agent does not promise a paper trail that is not there. Since Airtable did not
    # assign an ID, there is no ticket number to give the customer this time.
    return ok(
        f"ESCALATED (unlogged). Case for order {order_id or 'n/a'} handed off "
        f"(category '{category}'), marked '{desk}'.\n"
        f"TELL THE CUSTOMER, in these words: this case will be handled by a human "
        f"within {CASE.policy.escalation_sla_hours} hours. Do NOT give them a ticket "
        f"number -- none was assigned.\n"
        f"NOTE FOR THE OPERATOR, not the customer: the Tickets table could NOT be "
        f"written ({ticket_id}). The escalation exists in this session only."
    )


async def h_send_email(args: dict[str, Any]) -> dict[str, Any]:
    to = str(args.get("to", "") or "").strip().lower()
    subject = str(args.get("subject", "") or "").strip()
    body = str(args.get("body", "") or "").strip()

    if not looks_like_email(to):
        return err(f"'{to}' is not a valid email address.")
    if not subject or not body:
        return err("An email needs both a subject and a body.")

    # Verified state is already guaranteed by the hook; this catches the other mistake,
    # mailing a DIFFERENT address than the verified one.
    if SESSION.customer and to != SESSION.customer.email.lower():
        return err(
            f"Refusing to email {to}: the verified customer on this case is "
            f"{SESSION.customer.email}. Confirmations go to the address on the account."
        )

    CASE.emails_sent += 1
    _progress()
    return ok(f"EMAIL SENT to {to} -- subject '{subject}' ({len(body)} chars).")


# =========================================================================
# The sixth handler: in-process reads (Option B, added 14 Sep 2026)
# =========================================================================
# NOT part of the original five. Only registered when SHOPFAST_MCP_MODE=inprocess
# (see build_server()/tool_names() below and shopfast_agent.py's DATA_MODE) --
# the fallback for Streamlit Community Cloud, where the external, npx-launched
# Airtable MCP server (Option A) cannot reliably run: airtable-mcp-server depends on
# express@>=18, and Streamlit Cloud's Debian 11 base ships Node 12.x by default
# (verified via the npm registry's "engines" field and Streamlit's own docs, which
# don't mention Node.js at all -- see docs/ARCHITECTURE.md S3 and S7).
#
# Option A (the terminal build, the GUI, and Streamlit run locally) is UNCHANGED and
# still the default -- this tool does not replace mcp__airtable__list_records, it
# stands in for it only when that server isn't available. Same redaction principle
# either way, via CASE.summary_for_agent(): the rows are absorbed into CASE, but never
# handed to the model as a table it could recite.
TABLE_GETTERS: dict[str, Any] = {
    shopfast_airtable.TABLE_CUSTOMERS: shopfast_airtable.get_customers,
    shopfast_airtable.TABLE_ORDERS: shopfast_airtable.get_orders,
    shopfast_airtable.TABLE_DELAYS: shopfast_airtable.get_delays,
    shopfast_airtable.TABLE_POLICY: shopfast_airtable.get_policy,
}


async def h_read_reference_data(args: dict[str, Any]) -> dict[str, Any]:
    """Load one reference table straight from Airtable via pyairtable, no MCP hop.

    Populates CASE directly from the typed objects shopfast_airtable's getters
    already return -- there is no MCP content-block JSON to unwrap here, so this
    does NOT go through shopfast_airtable.harvest() (that parser exists for the
    external-MCP path's serialized tool_response shape, which this path never has).
    """
    table = str(args.get("table", "") or "").strip()
    if table not in TABLE_GETTERS:
        return err(
            f"'{table}' is not a known table. Use one of: "
            + ", ".join(TABLE_GETTERS)
        )

    try:
        rows = TABLE_GETTERS[table]()
    except RuntimeError as exc:                     # not configured / not installed
        return err(f"Could not read '{table}': {exc}")
    except Exception as exc:                        # auth, network, schema mismatch
        return err(f"Could not read '{table}' ({type(exc).__name__}): {exc}")

    if table == shopfast_airtable.TABLE_CUSTOMERS:
        for cust in rows:
            if cust.email:
                CASE.customers[cust.email.lower()] = cust
    elif table == shopfast_airtable.TABLE_ORDERS:
        for order in rows:
            CASE.orders[order.order_id] = order
    elif table == shopfast_airtable.TABLE_DELAYS:
        CASE.delays.update(rows)
    elif table == shopfast_airtable.TABLE_POLICY:
        CASE.policy = rows                           # get_policy() returns a Policy

    return ok(CASE.summary_for_agent(table))


# =========================================================================
# Tool descriptions: the A/B
# =========================================================================
# Pattern 7. The model has nothing else to go on when picking a tool, so this text IS
# the routing logic. Run the same tickets under each set and count the wrong calls.

DESCRIPTIONS_VAGUE: dict[str, str] = {
    "verify_customer": "Verifies a customer.",
    "lookup_order": "Gets order details.",
    "process_refund": "Processes a refund.",
    "escalate_to_human": "Escalates to a human.",
    "send_email": "Sends an email.",
}

DESCRIPTIONS_GOOD: dict[str, str] = {
    "verify_customer": (
        "Verify the customer's identity against the Customers tab BEFORE any refund or "
        "email. Input: email (the address on their account).\n"
        "Use when: starting any case that will touch money or messaging; the customer "
        "has just given you their email.\n"
        "Do NOT use for: looking up an order (use lookup_order); checking what is on "
        "file after verification (the case facts block already has it).\n"
        "Examples: 'my email is alice.brown@example.com' -> verify_customer("
        "email='alice.brown@example.com')."
    ),
    "lookup_order": (
        "Get one order's full record: status, carrier, ETA, total, delay reason, and a "
        "computed refund-eligibility verdict. Input: order_id (digits; '#12345' and "
        "'12345' are both fine).\n"
        "Use when: the customer asks where an order is, what happened to it, whether it "
        "is refundable, or names any order ID.\n"
        "Do NOT use for: account or profile questions (use verify_customer); orders the "
        "customer has not identified -- ask for the ID instead of guessing.\n"
        "Examples: 'where is my order #12345?' -> lookup_order(order_id='12345'). "
        "'can I still return the desk lamp?' -> ask which order, then lookup_order."
    ),
    "process_refund": (
        "Issue a refund against one order. Inputs: order_id, amount (USD), reason.\n"
        "Use when: the customer is owed money, the order is inside the return window, "
        "and the amount is at or below the automated limit in the Policy tab.\n"
        "Do NOT use for: amounts above the automated limit -- those must go to "
        "escalate_to_human; unverified customers; orders you have not looked up.\n"
        "Examples: 'I was charged twice for #12345' -> lookup_order first, then "
        "process_refund(order_id='12345', amount=429.99, reason='duplicate charge')."
    ),
    "escalate_to_human": (
        "Hand the case to a human specialist and raise a ticket. Inputs: order_id "
        "(optional), category, reason.\n"
        "Use when: the customer asks for a human; a refund exceeds the automated limit; "
        "the request needs a policy exception (outside the return window); no tool "
        "exists for what they need; or three turns have passed with no progress.\n"
        "Do NOT use for: anything you can resolve with the other tools -- unnecessary "
        "escalation is a failure, not a safe default.\n"
        "Examples: refund of $650 blocked by the limit -> escalate_to_human("
        "order_id='9876', category='refund_over_limit', reason='customer requests "
        "$650 refund, above the $500 automated limit')."
    ),
    "send_email": (
        "Send a confirmation email to the VERIFIED customer's address. Inputs: to, "
        "subject, body.\n"
        "Use when: the customer asks for written confirmation of a refund or "
        "escalation.\n"
        "Do NOT use for: any address other than the one on the verified account; "
        "unverified customers; routine replies -- just answer in the conversation.\n"
        "Examples: 'can you email me confirmation?' -> send_email(to=<verified email>, "
        "subject='Refund confirmation for order 12345', body=...)."
    ),
}

# Not part of the vague/good A/B above -- read_reference_data is plumbing, not a
# domain action, so it gets one solid description regardless of SHOPFAST_DESCRIPTIONS.
# Present in both dicts so _descriptions()'s single-dict lookup works either way.
_READ_REFERENCE_DESCRIPTION = (
    "Load one reference table -- Customers, Orders, Delays, or Policy -- so the other "
    "tools have data to work against. Input: table (exactly one of those four names).\n"
    "Use when: the case facts block shows a table as not yet loaded, at the START of "
    "a case, once per table.\n"
    "Do NOT use for: a table already loaded this conversation -- check the case facts "
    "block first, re-reading refreshes nothing and only costs a call; getting one "
    "order's live detail (use lookup_order, not this).\n"
    "Examples: starting a new case -> read_reference_data(table='Customers'), then "
    "read_reference_data(table='Orders'), then 'Delays', then 'Policy' -- four calls, "
    "once each."
)
DESCRIPTIONS_VAGUE["read_reference_data"] = _READ_REFERENCE_DESCRIPTION
DESCRIPTIONS_GOOD["read_reference_data"] = _READ_REFERENCE_DESCRIPTION


def description_mode() -> str:
    mode = os.getenv("SHOPFAST_DESCRIPTIONS", "good").strip().lower()
    return "vague" if mode.startswith("v") else "good"


def _descriptions() -> dict[str, str]:
    return DESCRIPTIONS_VAGUE if description_mode() == "vague" else DESCRIPTIONS_GOOD


# =========================================================================
# Server
# =========================================================================
# Tools are built by CALLING tool(...) rather than using @tool as a decorator, because
# the description has to be chosen at runtime for the A/B above. Same result: a list of
# SdkMcpTool objects.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "verify_customer": {"email": str},
    "lookup_order": {"order_id": str},
    "process_refund": {"order_id": str, "amount": float, "reason": str},
    "escalate_to_human": {"order_id": str, "category": str, "reason": str},
    "send_email": {"to": str, "subject": str, "body": str},
}

_HANDLERS = {
    "verify_customer": h_verify_customer,
    "lookup_order": h_lookup_order,
    "process_refund": h_process_refund,
    "escalate_to_human": h_escalate_to_human,
    "send_email": h_send_email,
}

# The sixth tool, added ONLY in "inprocess" mode (Option B -- see h_read_reference_data
# above). Kept out of _SCHEMAS/_HANDLERS above so "external" mode (the default -- the
# terminal build, the GUI, Streamlit run locally) is completely unaffected: same five
# tools, same allowed_tools, same behaviour already verified live against Airtable.
_READ_SCHEMA = {"table": str}


def _names_for(mode: str) -> list[str]:
    names = list(_SCHEMAS)
    if mode == "inprocess":
        names.append("read_reference_data")
    return names


def build_server(mode: str = "external"):
    """The in-process MCP server. No subprocess, no IPC, direct access to CASE.

    mode="inprocess" adds read_reference_data (Option B) -- see the module-level note
    above h_read_reference_data for why this exists and when it's used.
    """
    descriptions = _descriptions()
    schemas = dict(_SCHEMAS)
    handlers = dict(_HANDLERS)
    if mode == "inprocess":
        schemas["read_reference_data"] = _READ_SCHEMA
        handlers["read_reference_data"] = h_read_reference_data
    tools = [
        tool(name, descriptions[name], schemas[name])(handlers[name])
        for name in _names_for(mode)
    ]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def tool_names(mode: str = "external") -> list[str]:
    return [f"mcp__{SERVER_NAME}__{name}" for name in _names_for(mode)]
