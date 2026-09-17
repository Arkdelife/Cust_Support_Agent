"""shopfast_tickets.py -- writing an escalation into the Tickets tab, from CODE.

DEPRECATED (13 Sep 2026). Nothing in the live agent imports this file any more --
shopfast_backend.py's two call sites (h_process_refund, h_escalate_to_human) were
switched to shopfast_airtable.append_ticket() when the project migrated off Google
Sheets (see docs/MIGRATION_GOOGLE_TO_AIRTABLE.md, verified live 13-14 Sep 2026).
Kept in the repo, per that migration plan's own Phase 2 note, until you're confident
you won't roll back to the Sheets build -- then delete it along with
google-api-python-client / google-auth if they haven't already been removed from
pyproject.toml.

WHY THIS WAS NOT A TOOL THE MODEL CALLED

  The obvious way to persist a ticket is to grant the model mcp__sheets__add_rows and
  tell it, in SKILL.md, to append a row after every escalation. That works most of the
  time. "Most of the time" is the problem: a forgotten row means a customer was told
  "ticket TKT-1001 has been raised" and no ticket exists. The agent lied, politely,
  and nothing in the transcript looks wrong.

  So the write lives here instead, in the same function that allocates the ticket ID.
  The model cannot skip it, cannot mis-order the columns, and cannot decide this one
  is not worth recording. It is the same argument as the refund-cap hook, one layer
  down: anything that must happen belongs in code, not in an instruction.

THE SPLIT THIS CREATES

  Reads  -- the model, through mcp-google-sheets, read-only tools only.
  Writes -- this file, through the Sheets API, with no model in the loop.

  Two auth paths to one spreadsheet, on purpose. The read surface stays narrow and
  model-driven; the write surface is narrow and code-driven. Granting the model
  add_rows would have collapsed both into one wide surface.

FAILURE POLICY

  Nothing here raises. Escalation is the safety valve of the whole agent -- if the
  sheet is unreachable, the customer must still get their ticket number and their
  handoff. A failed write is reported in the tool result and logged, and the run
  continues. Losing the audit row is bad; refusing to escalate is worse.
"""

from __future__ import annotations

import os
import re
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB = "Tickets"
# Must match the header row exactly, left to right.
COLUMNS = ["Ticket ID", "Customer ID", "Order ID", "Issue Type",
           "Status", "Handled By", "Reason"]
RANGE = f"{TAB}!A:G"

# WHO DECIDED -- the "Handled By" column. Two values, and only two, so it can be
# filtered at a glance:
#
#   Handle by Human       policy did not allow the agent to close it. Still open.
#   AI Refunds specialist the agent decided and acted within policy. Already done.
#
# Every case gets a row, not just the escalations -- otherwise the sheet only ever
# shows the failures and there is nothing to compare them against. Sorting this column
# answers "what is the agent actually closing, and what is it handing over?", which is
# the number anyone running a support desk wants.
#
# The specific reason is not lost: it stays in Issue Type (refund_over_limit,
# policy_exception, refund_auto_approved, ...).
HUMAN_DESK = "Handle by Human"
AI_DESK = "AI Refunds specialist"


def desk_for(category: str) -> str:      # noqa: ARG001 -- category kept for the caller
    return HUMAN_DESK


_service = None
_service_error: str | None = None


def _get_service() -> Any:
    """Build the Sheets client once and keep it.

    Cached because an escalation should not pay an auth round-trip, and because a
    failure here is permanent for the run -- a missing key file will not appear
    halfway through a conversation.
    """
    global _service, _service_error
    if _service is not None or _service_error is not None:
        return _service

    path = os.getenv("SERVICE_ACCOUNT_PATH", "").strip()
    if not path:
        _service_error = "SERVICE_ACCOUNT_PATH is not set"
        return None
    if not os.path.isfile(path):
        _service_error = f"SERVICE_ACCOUNT_PATH does not exist: {path}"
        return None

    try:
        # Imported lazily so the rest of the agent still runs with these packages
        # absent -- the demo degrades to in-memory tickets instead of failing to boot.
        from google.oauth2 import service_account            # type: ignore
        from googleapiclient.discovery import build          # type: ignore
    except ImportError as exc:
        _service_error = (f"google api client not installed ({exc}). "
                          f"Run: uv sync")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES)
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as exc:                        # auth, network, malformed key
        _service_error = f"could not authenticate: {exc}"
        return None
    return _service


TICKET_PREFIX = "TKT-"
FIRST_TICKET = 1001
_TICKET_RE = re.compile(rf"{re.escape(TICKET_PREFIX)}(\d+)")


def next_ticket_id(fallback_seq: int = 0) -> str:
    """Highest ticket number already in the sheet, plus one.

    Numbering from an in-memory counter looked fine and was not: that counter resets
    every run, so every fresh conversation produced TKT-1001 again and the sheet filled
    with rows sharing an ID. A ticket number a human cannot use to find the ticket is
    not a ticket number.

    The sheet is the source of truth because it is the thing that survives the process.
    `fallback_seq` is only used when the sheet cannot be read at all -- normally the
    count of escalations already raised this session, so two offline escalations still
    differ.

    Not safe against two agents escalating at the same instant; both would read the
    same maximum. Fine for one support desk, wrong for a fleet -- that needs the ID
    allocated by whatever owns the ticket table.
    """
    sheet_id = os.getenv("SHOPFAST_SHEET_ID", "").strip()
    service = _get_service()
    if service is None or not sheet_id:
        return f"{TICKET_PREFIX}{FIRST_TICKET + fallback_seq}"

    try:
        res = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"{TAB}!A2:A")
            .execute()
        )
    except Exception:
        return f"{TICKET_PREFIX}{FIRST_TICKET + fallback_seq}"

    highest = FIRST_TICKET - 1
    for row in res.get("values", []):
        if not row:
            continue
        match = _TICKET_RE.fullmatch(str(row[0]).strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{TICKET_PREFIX}{highest + 1}"


def available() -> tuple[bool, str]:
    """Can we write? Called at startup so the failure is visible before it matters."""
    if not os.getenv("SHOPFAST_SHEET_ID", "").strip():
        return False, "SHOPFAST_SHEET_ID is not set"
    if _get_service() is None:
        return False, _service_error or "unavailable"
    return True, "ready"


def append_ticket(
    ticket_id: str,
    customer_id: str,
    order_id: str,
    issue_type: str,
    reason: str,
    status: str = "Open",
    handled_by: str | None = None,
) -> tuple[bool, str]:
    """Append one row to the Tickets tab. Returns (written, human-readable detail).

    USER_ENTERED so Sheets parses the values the way a person typing them would;
    INSERT_ROWS so a filter or a table on the tab does not get overwritten.
    """
    sheet_id = os.getenv("SHOPFAST_SHEET_ID", "").strip()
    if not sheet_id:
        return False, "SHOPFAST_SHEET_ID is not set"

    service = _get_service()
    if service is None:
        return False, _service_error or "sheets client unavailable"

    row = [
        ticket_id,
        customer_id or "",
        order_id or "",
        issue_type or "other",
        status,
        handled_by or desk_for(issue_type),
        # Sheets treats a leading = as a formula. A reason starting with one would
        # land as #NAME? or, worse, execute something.
        ("'" + reason) if reason.startswith("=") else reason,
    ]

    try:
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=sheet_id,
                range=RANGE,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            .execute()
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    updated = (result.get("updates") or {}).get("updatedRange", RANGE)
    return True, updated
