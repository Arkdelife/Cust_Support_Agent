[README.md](https://github.com/user-attachments/files/32328231/README.md)
# ShopFast Support Agent

A customer-support resolution agent built on the Claude Agent SDK, created as a
teaching artifact for the CCA-F exam (Agentic Architecture, Tool Design & MCP, Context
& Reliability) — and shaped so it can also stand on its own as a demonstrable product.

It resolves ShopFast (a fictional e-commerce store) support cases — refunds, duplicate
charges, delivery delays, order questions — through four in-process tools
(`verify_customer`, `lookup_order`, `process_refund`, `escalate_to_human`, plus
`send_email`), with real `PreToolUse`/`PostToolUse`/`UserPromptSubmit` hooks enforcing
business rules that the model cannot talk its way past. Target: 80%+ first-contact
resolution, with a clean, auditable hand-off to a human for the rest.

## Status

**Working today, live-verified: the Airtable-based build.** The migration described
in `docs/MIGRATION_GOOGLE_TO_AIRTABLE.md` is done -- reads go through an external
Airtable MCP server (`airtable-mcp-server`, via `npx`), actions and ticket writes stay
in-process (`shopfast_airtable.py`, via `pyairtable`), and the full ticket suite
(including the guidance/enforce A/B matrix) has been re-run live against the real
base and produces the same results `PLAN.md` first verified on Sheets. Run it with the
Quick start below.

`PLAN.md` is the historical record of the original, live-verified **Google Sheets**
build -- kept intact as a strong exam artifact, but it no longer describes what this
code actually does today. `docs/OPEN_QUESTIONS.md` has the full decision log for the
migration; `docs/ARCHITECTURE.md` has the target design it followed.

**Not yet done:** deploying the Streamlit widget to Streamlit Community Cloud (see
`RUNBOOK.md` §7) -- including the untested question of whether that container can run
`npx` at all, which `docs/ARCHITECTURE.md` §3 flags as the reason a second, in-process
fallback (Option B) exists ready-made in `shopfast_airtable.py`.

**Claude workspace for this project:** `wrkspc_01CzT9zjh75daxYeqUEDmNZe`.

**Model:** `claude-haiku-4-5-20251001` for every run mode (terminal, GUI, Streamlit) — set via `SHOPFAST_MODEL` in `.env`, default in `shopfast_agent.py` if unset.

## Documents in this project

| File | What it's for |
|---|---|
| `PLAN.md` | The original build plan and the live-verified results (11 real runs) for the Google Sheets version. Historical record — the code has since migrated to Airtable (see Status above), so this no longer describes how it behaves today. |
| `RUNBOOK.md` | How to actually run this thing: setup, every ticket, the GUI, the Streamlit widget, common failures. |
| `docs/ARCHITECTURE.md` | The target architecture once Airtable + Streamlit Cloud are in place, and why each change is being made. |
| `docs/AIRTABLE_SCHEMA.md` | The exact base/table/field design to build in Airtable. |
| `docs/MIGRATION_GOOGLE_TO_AIRTABLE.md` | The phased checklist for carrying out the migration. |
| `docs/ENHANCEMENTS.md` | Ideas for turning this from a demo into a pitchable product. |
| `docs/OPEN_QUESTIONS.md` | What's been decided, and what still needs your input. |

## Quick start (today's Airtable build)

```
uv sync
cp .env.example .env    # fill in ANTHROPIC_API_KEY, AIRTABLE_PAT, AIRTABLE_BASE_ID
uv run shopfast_agent.py --ticket 1
```

Also needs Node/`npx` on `PATH` -- see `RUNBOOK.md` §1 for why, and the two startup
gotchas already worked around in code.

Full setup, every ticket, the GUI, and the Streamlit widget: see `RUNBOOK.md`.

## Project structure

```
Cust_Support_Agent/
├── PLAN.md                          the original build plan + live results
├── README.md                        this file
├── RUNBOOK.md                       how to run and operate this project
├── pyproject.toml / uv.lock         Python dependencies (uv)
├── requirements.txt                 dependencies for Streamlit Community Cloud deploy
├── .env.example                     template for local secrets — copy to .env
├── .streamlit/secrets.toml.example  template for Streamlit Cloud secrets
├── .claude/skills/shopfast-support/SKILL.md   the guidance layer the agent loads
├── shopfast_sheet.py                row-parsing helpers, reused unchanged by shopfast_airtable.py
├── shopfast_backend.py              the five in-process tools the hooks gate
├── shopfast_policy.py               session state, case facts, the four hooks
├── shopfast_tickets.py              DEPRECATED — the old Sheets ticket-write path, unused
├── shopfast_agent.py                wiring, the five scripted tickets, the REPL
├── shopfast_agent_gui.py            tkinter engineer's-view demo window
├── shopfast_streamlit.py            the agent as a storefront chat widget
├── shopfast_airtable.py             the live Airtable data layer (reads + the ticket-write path)
└── docs/                            planning documents (see table above) + architecture.html
```

**A note on the folder layout:** this folder is the canonical project. A duplicate
copy that used to sit at `CCAF_Assignments/Customer_support_agent/` was **deleted on
13 Sep 2026** (moved to the Trash, so it is recoverable until the Trash is emptied)
after a `diff -rq` confirmed no file existed only in that copy. See
`docs/OPEN_QUESTIONS.md` decision 1 for the pre-deletion checks and one security
follow-up it turned up.
