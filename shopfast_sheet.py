"""shopfast_sheet.py -- the shape of the Google Sheet, and how facts get OUT of it.

Nothing in this file talks to Google. The `mcp-google-sheets` server does that, in its
own process, using the service account. This file only knows:

  1. what the tabs and columns are called, and
  2. how to turn a `get_sheet_data` RESULT back into typed rows.

Point (2) is the load-bearing one. The agent's case facts are built from what the sheet
tool actually RETURNED, never from what the model said about it. A model that claims
"I verified Alice already" changes nothing here, because nothing in this file listens to
prose -- it only parses tool output.

Tab detection is done by HEADER CONTENT, not by the range that was requested. The model
is free to ask for "Orders", "Orders!A1:I50" or the whole spreadsheet; we identify each
table by the columns it has. That keeps the harvester working no matter how the model
phrases its read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# --- tab names -----------------------------------------------------------
# Change these if you named the tabs differently when you built the sheet.
TAB_CUSTOMERS = "Customers"
TAB_ORDERS = "Orders"
TAB_DELAYS = "Delays"
TAB_POLICY = "Policy"
TAB_TICKETS = "Tickets"

ALL_TABS = [TAB_CUSTOMERS, TAB_ORDERS, TAB_DELAYS, TAB_POLICY, TAB_TICKETS]

# --- header signatures ---------------------------------------------------
# A table is whichever signature is a SUBSET of its header row. Order-independent,
# case-insensitive, and tolerant of extra columns you add later.
_SIGNATURES: list[tuple[str, set[str]]] = [
    (TAB_CUSTOMERS, {"customer id", "email"}),
    (TAB_ORDERS, {"order id", "order total"}),
    (TAB_POLICY, {"rule", "value"}),
    (TAB_TICKETS, {"ticket id", "status"}),
    (TAB_DELAYS, {"order id", "reason"}),   # last: 'order id'+'reason' is the loosest
]


def _norm_header(cell: Any) -> str:
    return str(cell or "").strip().lower()


def detect_tab(header: list[Any]) -> str | None:
    """Which tab is this table? Decided by its columns, not by the range asked for."""
    cells = {_norm_header(c) for c in header}
    for name, signature in _SIGNATURES:
        if signature <= cells:
            return name
    return None


# --- normalising ---------------------------------------------------------
def normalize_order_id(raw: Any) -> str:
    """'#12345' / ' 12345 ' / 12345.0 -> '12345'.

    Customers type '#12345'; Sheets hands back '12345' (and sometimes '12345.0' if the
    column got read as a number). Every comparison in this project goes through here, so
    a lookup never misses on punctuation alone.
    """
    text = str(raw or "").strip().lstrip("#").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def parse_money(raw: Any) -> float | None:
    """'$429.99' / '429.99' / '' -> 429.99 / None. Blank stays None, never 0.0.

    A blank cell is NOT zero. Returning 0.0 here would let the agent tell a customer
    their order total is $0.00, which reads like a fact and is a fabrication.
    """
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_date(raw: Any) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def blank(raw: Any) -> bool:
    """True for a cell that is empty or literally says 'unknown'."""
    return str(raw or "").strip().lower() in ("", "unknown", "n/a", "-")


# --- typed rows ----------------------------------------------------------
@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str = ""
    status: str = ""


@dataclass
class Order:
    order_id: str
    customer_id: str = ""
    order_date: date | None = None
    delivery_date: date | None = None
    status: str = ""
    carrier: str = ""
    eta_days: int | None = None
    item: str = ""
    total: float | None = None

    # Cells that were genuinely blank in the sheet. The agent must SAY a field is not
    # filled in rather than guess it, so we track which ones so it can be checked.
    missing: list[str] = field(default_factory=list)


@dataclass
class Policy:
    """Business rules read from the sheet, NOT hardcoded.

    The refund cap is the number the enforcement hook compares against. Keeping it in
    the sheet means you can change the cap and re-run the demo without touching code --
    and it makes the point that policy is configuration, not a constant baked into a
    prompt.
    """

    refund_auto_limit_usd: float = 500.0
    return_window_days: int = 30
    escalation_sla_hours: int = 24
    max_turns_before_escalation: int = 3

    loaded: bool = False   # False = still on the defaults above

    def apply(self, rule: str, value: Any) -> None:
        key = str(rule or "").strip().lower().replace(" ", "_")
        if not hasattr(self, key) or key == "loaded":
            return
        current = getattr(self, key)
        parsed = parse_money(value) if isinstance(current, float) else parse_int(value)
        if parsed is not None:
            setattr(self, key, parsed)
            self.loaded = True


# --- row -> object -------------------------------------------------------
def _index(header: list[Any], *names: str) -> int | None:
    """Column index by name, so re-ordering columns in the sheet cannot break us."""
    cells = [_norm_header(c) for c in header]
    for name in names:
        if name in cells:
            return cells.index(name)
    return None


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def rows_to_customers(header: list[Any], rows: list[list[Any]]) -> list[Customer]:
    i_id = _index(header, "customer id")
    i_name = _index(header, "name")
    i_mail = _index(header, "email")
    i_tier = _index(header, "tier")
    i_stat = _index(header, "account status", "status")
    out: list[Customer] = []
    for row in rows:
        email = str(_cell(row, i_mail) or "").strip()
        cid = str(_cell(row, i_id) or "").strip()
        if not email and not cid:
            continue
        out.append(Customer(
            customer_id=cid,
            name=str(_cell(row, i_name) or "").strip(),
            email=email.lower(),
            tier=str(_cell(row, i_tier) or "").strip(),
            status=str(_cell(row, i_stat) or "").strip(),
        ))
    return out


def rows_to_orders(header: list[Any], rows: list[list[Any]]) -> list[Order]:
    i_oid = _index(header, "order id")
    i_cid = _index(header, "customer id")
    i_odate = _index(header, "order date")
    i_ddate = _index(header, "delivery date")
    i_stat = _index(header, "status")
    i_car = _index(header, "carrier")
    i_eta = _index(header, "eta days")
    i_item = _index(header, "item")
    i_total = _index(header, "order total", "total")

    out: list[Order] = []
    for row in rows:
        oid = normalize_order_id(_cell(row, i_oid))
        if not oid:
            continue
        order = Order(
            order_id=oid,
            customer_id=str(_cell(row, i_cid) or "").strip(),
            order_date=parse_date(_cell(row, i_odate)),
            delivery_date=parse_date(_cell(row, i_ddate)),
            status=str(_cell(row, i_stat) or "").strip(),
            carrier=str(_cell(row, i_car) or "").strip(),
            eta_days=parse_int(_cell(row, i_eta)),
            item=str(_cell(row, i_item) or "").strip(),
            total=parse_money(_cell(row, i_total)),
        )
        # Record blanks explicitly. Order 1004 in the demo sheet has no carrier and no
        # ETA on purpose: the agent has to say so instead of inventing one.
        for label, idx in (("Status", i_stat), ("Carrier", i_car), ("ETA Days", i_eta),
                           ("Order Total", i_total)):
            if blank(_cell(row, idx)):
                order.missing.append(label)
        out.append(order)
    return out


def rows_to_delays(header: list[Any], rows: list[list[Any]]) -> dict[str, str]:
    i_oid = _index(header, "order id")
    i_why = _index(header, "reason")
    out: dict[str, str] = {}
    for row in rows:
        oid = normalize_order_id(_cell(row, i_oid))
        why = str(_cell(row, i_why) or "").strip()
        if oid and why:
            out[oid] = why
    return out


# --- the harvester -------------------------------------------------------
def _iter_tables(payload: Any, _depth: int = 0) -> Any:
    """Yield every table (list-of-rows) buried anywhere in a sheets tool result.

    The real shape from mcp-google-sheets is nested four levels deep and arrives as a
    JSON STRING inside an MCP content block:

        [{"type": "text", "text": '{"result": {"spreadsheetId": "...",
                                    "valueRanges": [{"range": "Customers",
                                                     "values": [[...], [...]]}]}}'}]

    An earlier version of this walked one key at a time and stopped at the first thing
    it did not recognise -- `valueRanges` -- so every read silently harvested nothing
    and the case facts stayed empty. Nothing raised; the agent simply had no data.

    Hence a generator that recurses rather than a loop that unwraps: one response can
    legitimately carry SEVERAL tables (get_multiple_sheet_data returns one valueRange
    per tab), and dropping all but the first would be the same bug wearing a hat.
    """
    if _depth > 8 or payload is None:
        return

    if isinstance(payload, str):
        text = payload.strip()
        if text:
            try:
                yield from _iter_tables(json.loads(text), _depth + 1)
            except json.JSONDecodeError:
                return
        return

    if isinstance(payload, dict):
        # A range object, or anything else carrying the rows directly.
        if isinstance(payload.get("values"), list):
            yield payload["values"]
            return
        for key in ("valueRanges", "result", "content", "data", "rows", "text", "sheets"):
            if key in payload:
                yield from _iter_tables(payload[key], _depth + 1)
                return
        return

    if isinstance(payload, list):
        if not payload:
            return
        first = payload[0]
        if isinstance(first, list):
            yield payload                      # <- a table at last
            return
        if isinstance(first, dict):
            # A content list, or a list of valueRange objects: recurse into each.
            if {"text", "values", "valueRanges", "result"} & set(first):
                for item in payload:
                    yield from _iter_tables(item, _depth + 1)
                return
            # A list of records: rebuild header + rows so the rest of the file needs
            # no second code path.
            keys = list(first.keys())
            yield [keys] + [[row.get(k, "") for k in keys] for row in payload]
        return


@dataclass
class Harvest:
    """Everything a sheet read told us. Empty fields mean 'not in this read'."""

    tab: str | None = None                                  # first tab recognised
    tabs: list[str] = field(default_factory=list)           # all of them
    customers: list[Customer] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    delays: dict[str, str] = field(default_factory=dict)
    policy_rules: list[tuple[str, Any]] = field(default_factory=list)
    row_count: int = 0


def _fold(result: Harvest, header: list[Any], rows: list[list[Any]], tab: str) -> None:
    if tab == TAB_CUSTOMERS:
        result.customers.extend(rows_to_customers(header, rows))
    elif tab == TAB_ORDERS:
        result.orders.extend(rows_to_orders(header, rows))
    elif tab == TAB_DELAYS:
        result.delays.update(rows_to_delays(header, rows))
    elif tab == TAB_POLICY:
        i_rule = _index(header, "rule")
        i_val = _index(header, "value")
        result.policy_rules.extend(
            (str(_cell(r, i_rule)), _cell(r, i_val))
            for r in rows
            if str(_cell(r, i_rule) or "").strip()
        )


def harvest(payload: Any) -> Harvest:
    """Turn a sheets tool result into typed rows.

    Returns an empty Harvest rather than raising: a read we could not parse must not
    take the conversation down. The caller notices because `tab` stays None -- which is
    exactly how the valueRanges bug stayed invisible, so the hook logs that case.
    """
    result = Harvest()
    for table in _iter_tables(payload):
        if not table or len(table) < 2:
            continue
        header = table[0]
        rows = [r for r in table[1:] if any(str(c).strip() for c in r)]
        tab = detect_tab(header)
        if tab is None:
            continue
        if result.tab is None:
            result.tab = tab
        result.tabs.append(tab)
        result.row_count += len(rows)
        _fold(result, header, rows, tab)
    return result


# --- eligibility ---------------------------------------------------------
# Deterministic business arithmetic. This lives in code, not in the model's head:
# "is this order still inside the return window" is a rule with one right answer, and
# a language model re-deriving it every turn is a liability, not a feature.
def days_since_delivery(order: Order, today: date) -> int | None:
    if order.delivery_date is None:
        return None
    return (today - order.delivery_date).days


def refund_eligibility(order: Order, policy: Policy, today: date) -> dict[str, Any]:
    """Structured verdict. Never a bare bool -- the agent has to explain WHY."""
    age = days_since_delivery(order, today)
    within_window = None if age is None else age <= policy.return_window_days
    over_cap = None if order.total is None else order.total > policy.refund_auto_limit_usd

    reasons: list[str] = []
    if within_window is False:
        reasons.append(
            f"delivered {age} days ago, outside the {policy.return_window_days}-day "
            f"return window -- needs a policy exception"
        )
    if over_cap:
        reasons.append(
            f"order total ${order.total:,.2f} is above the "
            f"${policy.refund_auto_limit_usd:,.2f} automated limit"
        )
    if age is None:
        reasons.append("no delivery date on file, so the return window cannot be checked")

    return {
        "days_since_delivery": age,
        "within_return_window": within_window,
        "exceeds_auto_limit": over_cap,
        "auto_refundable": bool(within_window and not over_cap),
        "reasons": reasons,
    }


# Dot-free character classes on both sides of the dot: without that the two greedy
# groups can both match dots and the engine backtracks exponentially on a long
# near-miss.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")


def looks_like_email(text: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(str(text or "").strip()))
