# Runbook

Operational reference: how to set this project up, run it, and fix the things that
commonly go wrong. `PLAN.md` §§9-11 has the deeper "why" behind several of these
(written for the Google Sheets build); this file is the quick, task-oriented version,
kept current for whichever data layer the code actually runs on.

**Status (14 Sep 2026): the Airtable migration is done and live-verified.** The code
in this repo reads and writes Airtable, not Google Sheets, as of 13-14 Sep 2026 --
see `docs/OPEN_QUESTIONS.md` and `docs/MIGRATION_GOOGLE_TO_AIRTABLE.md` for the full
history. `PLAN.md` remains an accurate historical record of the *Sheets* build; it no
longer describes what running this code today actually does. This file does.

---

## 1. One-time setup

```
uv sync
cp .env.example .env
```

Fill in `.env`:

| Variable | Where it comes from |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key. |
| `AIRTABLE_PAT` | A Personal Access Token from airtable.com/create/tokens, scoped to the base with `data.records:read`, `data.records:write`, `schema.bases:read` -- deliberately **not** `schema.bases:write` (see the note on Issue Type below). |
| `AIRTABLE_BASE_ID` | The base ID (starts with `app`), from the base's URL or API docs. This project's base: `appOLrt6hPV9fduA2` ("CCAF Customer Support Agent"). |

**The step people miss:** the PAT must be scoped to *this specific base*, not just to
your account in general -- a token with no base access looks identical to a missing
token until you actually call something, and then every tool call 403s.

**Also required, and easy to miss:** Node/npm, because reads go through an external
MCP server (`airtable-mcp-server`) launched via `npx`. Confirm `node` and `npx` are on
`PATH` (`which npx`). Two gotchas found the hard way while wiring this up, both now
worked around in `shopfast_agent.py`'s `build_options()` so you shouldn't hit them, but
worth knowing about if you ever debug the `airtable` MCP server directly:

- A per-server `env` dict in `mcp_servers` becomes the subprocess's **entire**
  environment, not an overlay on the parent's -- so `PATH` has to be passed through
  explicitly or `npx` itself is unresolvable. (The well-known MCP-client gotcha; the
  same one Claude Desktop's docs warn about for any `npx`-launched server.)
- A stale, root-owned `~/.npm` cache (an old, unrelated npm bug, nothing to do with
  this project) can make every `npx` call fail with `EACCES`. Worked around by
  pointing `NPM_CONFIG_CACHE` at a project-local `.npm-cache/` directory instead of
  requiring `sudo chown` on your global cache.

## 2. Running a ticket

```
uv run shopfast_agent.py --ticket 1        # duplicate charge, clean refund
uv run shopfast_agent.py --ticket 2        # refund above the cap -- see below
uv run shopfast_agent.py --ticket 3        # long conversation, case-facts recall
uv run shopfast_agent.py --ticket 4        # 503 retry vs 404 no-retry
uv run shopfast_agent.py --ticket 5        # "I already verified last time"
uv run shopfast_agent.py --ticket all      # all five, in sequence
uv run shopfast_agent.py --chat            # free-form REPL; type 'facts' to see the case-facts block
```

### The pattern-2 A/B (the most important demo in the build)

```
uv run shopfast_agent.py --ticket 2 --guidance on  --enforce on
uv run shopfast_agent.py --ticket 2 --guidance off --enforce on  --descriptions vague
uv run shopfast_agent.py --ticket 2 --guidance off --enforce off --descriptions vague
```

Expected, per `PLAN.md` §11.1: 0 denials / 1 denial + blocked / $650 processed.
**Re-verified live against Airtable on 14 Sep 2026, same result, all three combos:**
0 denials (model self-escalated as TKT-4) / `refund_cap` denied the $650 call, escalated
as TKT-5 / $650 processed as TKT-6 with 0 denials. The Policy table's
`refund_auto_limit_usd=500` row, read live from Airtable, drove the hook decision in
the middle run exactly as the Sheets version did. If the third run does **not** show
the refund going through, something upstream (a leftover env var, a cached `.pyc`) is
still supplying guidance -- check `SHOPFAST_ENFORCE` and `SHOPFAST_GUIDANCE` aren't
already set in your shell environment overriding the flags.

## 3. Running the GUI

```
uv run shopfast_agent_gui.py
```

Engineer's view: live case-facts panel, conversation, hook decision log, and an
ENFORCE on/off toggle for a live A/B without restarting.

## 4. Running the Streamlit widget

```
uv run --with streamlit streamlit run shopfast_streamlit.py
```

Customer's view: a storefront page with a chat launcher. No tool names or hook
decisions in the chat bubble -- that detail is in the sidebar console instead.

**Known limitation, unchanged for now:** case facts and session state are shared
across every browser tab pointed at this one running process (see
`docs/ARCHITECTURE.md` §6). Fine for a single demo session; two people using it at once
will see each other's conversation.

## 5. Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `AIRTABLE_PAT and/or AIRTABLE_BASE_ID are not set` | `.env` incomplete, or in the wrong directory, or accidentally reset to `.env.example`'s blanks | Confirm `.env` is at the project root and has real values -- `diff .env .env.example` should NOT come back empty. |
| Every Airtable call 401s | PAT is invalid, expired, or revoked | Regenerate at airtable.com/create/tokens. Sanity-check independent of this code: `curl -H "Authorization: Bearer $AIRTABLE_PAT" https://api.airtable.com/v0/meta/whoami` should return your user, not an UNAUTHORIZED error. |
| Every Airtable call 403s | PAT is valid but not scoped/granted to this base | Re-scope the token to the base at airtable.com/create/tokens, with `data.records:read`, `data.records:write`, `schema.bases:read`. |
| `mcp servers ... airtable=failed` at startup, no `mcp__airtable__*` tools offered | `npx` unresolvable in the MCP subprocess's environment, or a stale root-owned `~/.npm` cache causing `EACCES` | Both are worked around already in `build_options()` (`PATH` and `NPM_CONFIG_CACHE` are passed explicitly). If you still see this, run `npx -y airtable-mcp-server` by hand from a plain shell and read its stderr directly -- that is how both of these were originally diagnosed. |
| `422 INVALID_MULTIPLE_CHOICE_OPTIONS` on an escalation write | The model invented an `Issue Type` category and the Tickets table field used to be a `singleSelect`, which rejects unlisted options without `schema.bases:write` on the PAT | Already fixed by making `Issue Type` a plain text field (13 Sep 2026) -- if you see this again, something recreated the field as a select. |
| Hook never seems to fire | Wrong matcher string | Run with `SHOPFAST_TRACE_HOOKS=on` -- the `audit_hook` (matcher=`None`) logs every real tool name it sees, which is the fastest way to find a typo in a matcher (e.g. `mcp__airtable__list_records`, not `mcp__sheets__get_sheet_data`). |
| `UnicodeEncodeError` on Windows | Console defaulting to `cp1252` | Already handled in `shopfast_agent.py` (`sys.stdout.reconfigure(encoding="utf-8", ...)`) -- if you still see this, you're likely running an older copy of the file. |
| A ticket seems to loop or stall | `max_turns` too low, or the model genuinely stuck | Check `terminal_reason` in the printed summary before assuming a bug -- `max_turns` / `error_max_budget_usd` are cap hits, not crashes. |

## 6. Verification checklist (from `PLAN.md` §10, re-run live against Airtable 13-14 Sep 2026)

1. `--ticket 1` -> verify -> read -> refund; clean `terminal_reason`; 0 denials. **PASS** -- refund $429.99 on order 12345, logged as a real Tickets row.
2. `--ticket 2` -> exactly 1 hook denial, an `escalate_to_human` call, no refund
   recorded. **PASS** (see the pattern-2 A/B re-run in §2 above for the full matrix).
3. `--ticket 2` with `SHOPFAST_ENFORCE=off` -> refund goes through (the contrast is
   the point). **PASS.**
4. `--ticket 3` -> the recall question is answered correctly from the injected case
   facts, sourced from a live Airtable read.
5. `--ticket 4` -> retry log shows 2x503 then success; the 404 variant shows no
   retries. Unaffected by the data-layer swap -- this fault is simulated in
   `shopfast_backend.py`, not a real Airtable call.
6. `SHOPFAST_DESCRIPTIONS=vague` across all tickets -> visibly more wrong tool calls.
7. `uv run shopfast_agent_gui.py` -> every ticket button works; the ENFORCE toggle
   changes ticket 2's outcome live.

## 7. Deploying to Streamlit Community Cloud

Not yet done from this repo -- next step once you're ready. Sketch:

1. Push this repo to GitHub.
2. On share.streamlit.io, point a new app at `shopfast_streamlit.py`.
3. Paste secrets into the app's Settings -> Secrets panel (see
   `.streamlit/secrets.toml.example` for the shape): `ANTHROPIC_API_KEY`,
   `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`.
4. **Untested risk, carried over from `docs/ARCHITECTURE.md` §7/§3:** confirm `npx`
   actually works inside Streamlit Cloud's container before relying on this for a live
   demo -- that container may not have Node on `PATH`, and this has only been verified
   on a local machine so far. If it fails, fall back to Option B (in-process
   `pyairtable` reads, no external MCP server) -- `shopfast_airtable.py`'s
   `get_customers`/`get_orders`/`get_delays`/`get_policy` functions already exist for
   exactly this fallback.
5. Deploy, then re-run the verification checklist above against the live URL before
   sharing the link further.

**Rotating the Airtable PAT:** generate a new token scoped the same way, update it in
both your local `.env` and the Streamlit Cloud Secrets panel, then revoke the old
token in Airtable's account settings. No service to re-share, unlike the Google Sheet
step the old setup needed.
