"""shopfast_airtable.py -- the Airtable data layer. WIRED UP (13 Sep 2026).

STATUS: live. The base is built (see docs/AIRTABLE_SCHEMA.md -- six tables, real data
imported from the demo rows in PLAN.md section 5) and this file talks to it via
`pyairtable`. `shopfast_backend.py`'s two write call sites (h_process_refund,
h_escalate_to_human) now call `append_ticket` here instead of `shopfast_tickets`.

ARCHITECTURE DECISION THIS FILE IMPLEMENTS (Option A -- see docs/ARCHITECTURE.md S3
and docs/OPEN_QUESTIONS.md decision 5): two tool surfaces, kept.

  READS   -- external Airtable MCP server (`airtable-mcp-server`, launched via npx in
             shopfast_agent.py's build_options()). The agent calls
             mcp__airtable__list_records / search_records / get_record directly; a
             PostToolUse harvest hook in shopfast_policy.py parses those results into
             CASE via harvest() below. This module's own get_customers/get_orders/
             get_delays/get_policy functions are therefore NOT on the agent's live
             read path -- they exist for the isolation test, the Support Requests
             queue, offline debugging, and as the ready-made Option B fallback if the
             Phase 0 npx-on-Streamlit-Cloud check ever fails.

  ACTIONS -- in-process shopfast server (unchanged). append_ticket() below IS on the
             live path: the model asks to escalate or refund, this code decides what
             row gets created. Granting the model a write tool on the MCP read surface
             would defeat the point of that surface being read-only.

WHY THIS SHAPE
  shopfast_sheet.py's dataclasses (Customer, Order, Policy) and pure parsing helpers
  (normalize_order_id, parse_money, parse_date, detect_tab, rows_to_*) have no Google
  dependency in them at all -- they only know how to turn ALREADY-FETCHED rows into
  typed objects, keyed by column/field NAME. The Airtable base was built with the same
  field names as the Sheet's column headers on purpose (see docs/AIRTABLE_SCHEMA.md),
  so those parsers are reused here completely unchanged. Only the FETCHING, and the
  shape harvest() unwraps, differ.

  Same principle as shopfast_tickets.py before it: the write path is code, not
  something the model calls directly, so a forgotten write can't happen.

LIVE BASE (verified against the Airtable API, 13 Sep 2026)
  Base: appOLrt6hPV9fduA2 ("CCAF Customer Support Agent"). Table IDs below were
  returned by create_table/list_tables_for_base when the schema was built and are
  stable for the life of each table (renaming a table does not change its ID).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from shopfast_sheet import (
    Customer,
    Harvest,
    Order,
    Policy,
    blank,
    detect_tab,
    normalize_order_id,
    parse_date,
    parse_int,
    parse_money,
    rows_to_customers,
    rows_to_delays,
    rows_to_orders,
)

# --- table names AND ids ----------------------------------------------------
# Names match the Airtable field/table names in docs/AIRTABLE_SCHEMA.md, which in turn
# match the Sheet's column headers on purpose -- see the module docstring.
TABLE_CUSTOMERS = "Customers"
TABLE_ORDERS = "Orders"
TABLE_DELAYS = "Delays"
TABLE_POLICY = "Policy"
TABLE_TICKETS = "Tickets"
TABLE_SUPPORT_REQUESTS = "Support Requests"

# IDs, for the MCP read surface: the Airtable MCP server's tools take a tableId, not a
# name (see shopfast_agent.py's system_prompt(), which hands these to the model so it
# never has to discover or guess one). Also used below for the pyairtable write path,
# which accepts either a name or an ID -- ID is used for stability across a rename.
BASE_ID = "appOLrt6hPV9fduA2"
TABLE_IDS = {
    TABLE_CUSTOMERS: "tblRxtpTKbOlITIrM",
    TABLE_ORDERS: "tblBddCy3EHckkOv1",
    TABLE_DELAYS: "tblB4exPFFCj8cExb",
    TABLE_POLICY: "tbl9ZIILpspfmVS5v",
    TABLE_TICKETS: "tbluolLaq034BTTVX",
    TABLE_SUPPORT_REQUESTS: "tbl1bUHOEIoJ3D2xE",
}

# WHO DECIDED -- unchanged from shopfast_tickets.py. Two values, so the Tickets table
# can be filtered at a glance into "what the agent closed" vs "what it handed off".
HUMAN_DESK = "Handle by Human"
AI_DESK = "AI Refunds specialist"


def desk_for(category: str) -> str:      # noqa: ARG001 -- kept for call-site parity
    return HUMAN_DESK


# --- client bootstrap --------------------------------------------------------
# Same shape as shopfast_tickets._get_service(): build once, cache, fail loudly but
# only when actually asked to do something, so the rest of the agent can still start
# up even if Airtable isn't configured yet.
_tables: dict[str, Any] = {}
_client_error: str | None = None


def _table(name: str) -> Any:
    """Return a cached pyairtable.Table for the given table name.

    Raises RuntimeError with a clear message if AIRTABLE_PAT / AIRTABLE_BASE_ID are
    missing, or if pyairtable is not installed -- mirroring how
    shopfast_tickets._get_service() degraded: fail with a message a human can act on,
    never a bare traceback.
    """
    global _client_error
    if name in _tables:
        return _tables[name]

    pat = os.getenv("AIRTABLE_PAT", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip() or BASE_ID
    if not pat:
        raise RuntimeError(
            "AIRTABLE_PAT is not set. See .env.example."
        )

    try:
        from pyairtable import Table  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"pyairtable is not installed ({exc}). Run `uv sync`."
        ) from exc

    table_id = TABLE_IDS.get(name, name)
    table = Table(pat, base_id, table_id)
    _tables[name] = table
    return table


def available() -> tuple[bool, str]:
    """Can we talk to Airtable at all? Mirrors shopfast_tickets.available()."""
    if not os.getenv("AIRTABLE_PAT", "").strip():
        return False, "AIRTABLE_PAT is not set"
    try:
        _table(TABLE_CUSTOMERS).all(max_records=1)
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:                       # auth, network, bad base/table id
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ready"


# --- reads -------------------------------------------------------------------
# NOTE (Option A): these are NOT the agent's read path -- see the module docstring.
# The agent reads via the external Airtable MCP server; harvest() below (fed by the
# PostToolUse hook) is what actually fills CASE. These functions exist for the
# isolation test, the Support Requests queue, debugging, and the Option B fallback.
def _records_to_rows(records: list[dict[str, Any]]) -> tuple[list[Any], list[list[Any]]]:
    """Airtable's {"id":..., "fields": {...}} records -> a (header, rows) pair.

    Reusing rows_to_customers/rows_to_orders/rows_to_delays needs exactly this shape --
    the same one shopfast_sheet.py was already written to expect from a sheet range.
    """
    header: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in (rec.get("fields") or {}):
            if key not in seen:
                seen.add(key)
                header.append(key)
    rows = [[(rec.get("fields") or {}).get(h, "") for h in header] for rec in records]
    return header, rows


def get_customers() -> list[Customer]:
    """All rows from the Customers table, as Customer objects."""
    records = _table(TABLE_CUSTOMERS).all()
    header, rows = _records_to_rows(records)
    return rows_to_customers(header, rows)


def get_orders() -> list[Order]:
    """All rows from the Orders table, as Order objects.

    Keeps the same 'missing' tracking shopfast_sheet.rows_to_orders does -- a blank
    field is reported as not-filled-in, never invented.
    """
    records = _table(TABLE_ORDERS).all()
    header, rows = _records_to_rows(records)
    return rows_to_orders(header, rows)


def get_delays() -> dict[str, str]:
    """order_id -> reason, from the Delays table."""
    records = _table(TABLE_DELAYS).all()
    header, rows = _records_to_rows(records)
    return rows_to_delays(header, rows)


def get_policy() -> Policy:
    """The Policy table, folded into a Policy object via Policy.apply()."""
    policy = Policy()
    for rec in _table(TABLE_POLICY).all():
        fields = rec.get("fields") or {}
        policy.apply(fields.get("Rule", ""), fields.get("Value", ""))
    return policy


@dataclass
class SupportRequest:
    """One row from the Airtable Form intake table. See docs/AIRTABLE_SCHEMA.md."""

    request_id: str
    name: str
    email: str
    order_id: str
    issue_type: str
    description: str
    status: str


def get_new_support_requests() -> list[SupportRequest]:
    """Support Requests rows with Status == 'New' -- the Airtable Form intake queue.

    Not called from anywhere yet. Today this queue is meant to be worked by a human
    directly in the Airtable UI (see docs/ARCHITECTURE.md section 4.2).
    """
    out: list[SupportRequest] = []
    for rec in _table(TABLE_SUPPORT_REQUESTS).all(formula="{Status}='New'"):
        f = rec.get("fields") or {}
        out.append(SupportRequest(
            request_id=str(f.get("Request ID", "")),
            name=str(f.get("Name", "")),
            email=str(f.get("Email", "")).lower(),
            order_id=normalize_order_id(f.get("Order ID", "")),
            issue_type=str(f.get("Issue Type", "")),
            description=str(f.get("Description", "")),
            status=str(f.get("Status", "")),
        ))
    return out


# --- the harvester -------------------------------------------------------
# This is what actually feeds CASE under Option A: shopfast_policy.harvest_hook calls
# this on the tool_response of every mcp__airtable__{list_records,search_records,
# get_record} call, the same role shopfast_sheet.harvest() played for
# mcp__sheets__get_sheet_data. See docs/ARCHITECTURE.md S4.1.
def _find_record_lists(payload: Any, _depth: int = 0) -> Any:
    """Yield every Airtable 'records' array buried anywhere in an MCP tool result.

    The real shape from airtable-mcp-server, like every MCP tool result, arrives as a
    JSON STRING inside a content block:

        [{"type": "text", "text": '{"records": [{"id": "recXXX",
                                     "createdTime": "...",
                                     "fields": {"Customer ID": "CUST-7742", ...}},
                                     ...]}'}]

    get_record (a single record, no list wrapper) is folded in too, as a one-item list,
    so the harvester does not need a second code path for it.
    """
    if _depth > 8 or payload is None:
        return

    if isinstance(payload, str):
        text = payload.strip()
        if text:
            try:
                yield from _find_record_lists(json.loads(text), _depth + 1)
            except json.JSONDecodeError:
                return
        return

    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            yield payload["records"]
            return
        # get_record's result IS one record: {"id":..., "fields": {...}}.
        if isinstance(payload.get("fields"), dict) and "id" in payload:
            yield [payload]
            return
        for key in ("result", "content", "text", "data"):
            if key in payload:
                yield from _find_record_lists(payload[key], _depth + 1)
        return

    if isinstance(payload, list):
        for item in payload:
            yield from _find_record_lists(item, _depth + 1)


def harvest(payload: Any) -> Harvest:
    """Turn an Airtable MCP tool result into typed rows -- the Airtable-shaped
    equivalent of shopfast_sheet.harvest().

    Returns an empty Harvest rather than raising: a read we could not parse must not
    take the conversation down (same contract as shopfast_sheet.harvest()).
    """
    result = Harvest()
    for records in _find_record_lists(payload):
        if not records:
            continue
        header, rows = _records_to_rows(records)
        tab = detect_tab(header)
        if tab is None:
            continue
        if result.tab is None:
            result.tab = tab
        result.tabs.append(tab)
        result.row_count += len(rows)

        if tab == TABLE_CUSTOMERS:
            result.customers.extend(rows_to_customers(header, rows))
        elif tab == TABLE_ORDERS:
            result.orders.extend(rows_to_orders(header, rows))
        elif tab == TABLE_DELAYS:
            result.delays.update(rows_to_delays(header, rows))
        elif tab == TABLE_POLICY:
            i_rule = header.index("Rule") if "Rule" in header else None
            i_val = header.index("Value") if "Value" in header else None
            if i_rule is not None and i_val is not None:
                result.policy_rules.extend(
                    (str(row[i_rule]), row[i_val])
                    for row in rows
                    if str(row[i_rule] or "").strip()
                )
        # TABLE_TICKETS: intentionally not folded, same as the Sheets harvester --
        # Tickets is a write target, not case-facts data.
    return result


# --- writes --------------------------------------------------------------------
def append_ticket(
    customer_id: str,
    order_id: str,
    issue_type: str,
    reason: str,
    status: str = "Open",
    handled_by: str | None = None,
) -> tuple[bool, str]:
    """Create one row in the Tickets table. Replaces shopfast_tickets.append_ticket().

    Returns (True, the created record's formatted Ticket ID e.g. "TKT-1") on success,
    (False, an error string) on failure -- NEVER raises. Escalation is the safety valve
    of the whole agent; a write failure must not block the handoff (same failure policy
    as shopfast_tickets.append_ticket()).

    No `ticket_id` parameter: unlike the Sheets version, Airtable assigns the number on
    insert (Ticket Number is an Autonumber field; Ticket ID is a formula over it -- see
    docs/AIRTABLE_SCHEMA.md), so there is nothing to allocate beforehand and nothing
    that two concurrent escalations could race on reading.
    """
    try:
        table = _table(TABLE_TICKETS)
    except RuntimeError as exc:
        return False, str(exc)

    fields = {
        "Customer ID": customer_id or "",
        "Order ID": order_id or "",
        "Issue Type": issue_type or "other",
        "Status": status,
        "Handled By": handled_by or desk_for(issue_type),
        "Reason": reason,
    }
    try:
        created = table.create(fields)
    except Exception as exc:                        # auth, network, schema mismatch
        return False, f"{type(exc).__name__}: {exc}"

    ticket_id = (created.get("fields") or {}).get("Ticket ID")
    if not ticket_id:
        # The record was created but the formula field wasn't in the create response
        # (pyairtable returns only written fields by default) -- fetch it back.
        try:
            refetched = table.get(created["id"])
            ticket_id = (refetched.get("fields") or {}).get("Ticket ID")
        except Exception:
            pass
    return True, ticket_id or f"(created, id={created.get('id', '?')})"
