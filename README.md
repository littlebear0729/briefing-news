# The Global Brief — a twice-daily, two-page newspaper, built by code

Twice a day the pipeline fetches candidates from a whitelist of authoritative feeds,
asks a model to *edit* them (choose, order, headline, compress — nothing else), renders
the result into one responsive newspaper page, checks that document, and sends it through
a Telegram bot as a self-contained HTML file that opens on a phone or a laptop.

**The design rule behind every decision here: code owns everything stable, the model
owns editorial judgement only.** The model never writes HTML, never writes a URL, never
decides layout or word counts. Those are versioned assets, config and code.

- Handover document: [`DESIGN.md`](DESIGN.md) — §11 is the implementation status, §12 the
  HTML-only revision

## Pipeline

```
config.yaml ──┐
sources.py ───┴─▶ fetch.py        build/articles-<date>-<ed>.json     deterministic, whitelisted
                       │
                       ▼
                  editorial.py    build/edition-<date>-<ed>.json      the ONLY model call
                       │
                       ▼
                  render.py       dist/global-brief-<date>-<ed>.html  responsive newspaper
                       │
                       ▼
                  preflight.py    structure · every paragraph present · footer per sheet
                       │          · whitelisted links · length on target
                       ▼
                  publish.py      Telegram sendDocument               idempotent delivery
                       ▲
              state/sent.json (ledger, committed back)
```

One artefact, one edit, one delivery. There is no PDF and no pagination: the brief asks
for *about two A4 pages of newspaper*, so length is a measured target rather than a
layout that clips.

## Quick start

```bash
pip install -r requirements.txt

# full edition (needs BRIEF_LLM_API_KEY, and TELEGRAM_* to publish)
python -m pipeline.run --edition am --publish

# stage by stage
python -m pipeline.fetch    --edition am --date 2026-10-13
python -m pipeline.editorial build/articles-2026-10-13-am.json
python -m pipeline.render   build/edition-2026-10-13-am.json --date 2026-10-13 --check
python -m pipeline.publish  --html dist/global-brief-2026-10-13-am.html \
                            --edition-json build/edition-2026-10-13-am.json \
                            --date 2026-10-13 --edition am
```

No key, no network? Replay the committed fixture:

```bash
python -m pipeline.render build/edition-2026-10-13-am.json --date 2026-10-13 --check
open dist/global-brief-2026-10-13-am.html      # or just drag it into a browser
python -m pipeline.publish --html dist/global-brief-2026-10-13-am.html --dry-run
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs offline and deterministically (a guard fixture fails any test that
reaches the network). It covers the whitelist, the fetch window/dedupe/caps, the schema
and citation contract, budget enforcement, the newspaper template and its styling
invariants, the pre-delivery checks (including "a dropped paragraph must fail"), the
Telegram idempotency/retry/token-safety paths, and the manifest. One test prints the
worst-case edition through a real Chromium and counts the sheets; it skips where no
browser is available.

## Environment

| Variable | Kind | Notes |
|---|---|---|
| `BRIEF_LLM_API_KEY` | secret | required |
| `BRIEF_LLM_BASE_URL` | var | empty string falls back to `config.yaml` |
| `BRIEF_LLM_MODEL` | var | pin the model version; change it in a PR |
| `BRIEF_LLM_RETRIES` / `BRIEF_LLM_TEMPERATURE` | var | CI stays at 0–0.2 |
| `TELEGRAM_BOT_TOKEN` | secret | dedicated bot |
| `TELEGRAM_CHAT_ID` | secret | comma-separated for several recipients |
| `TELEGRAM_ADMIN_CHAT_ID` | secret | failure alerts |

## The newspaper

One template (`templates/newspaper.html.j2`) plus shared CSS:

- **Responsive**, phone first: one column below 640px, two from 640px, the broadsheet
  grid from 1024px. Breakpoints are fixed and screen-scoped, so they cannot leak into
  print.
- **Newspaper furniture**: newsprint paper with a hairline grain, ink rules, a black
  masthead band, small-caps kickers and section bars, a drop cap on the lead, justified
  hyphenated body copy, ruled markets table, inline attributions ("— Reuters, AP").
  The nameplate rule is drawn in CSS, and there are no illustrations or images anywhere:
  the document is text, CSS and source links (a test enforces that).
- **Print** gets its own dense multi-column layout (`@page A4`), so printing the same
  file gives you an actual two-page paper.

Length is checked, not assumed. `pipeline.content.estimate_length` converts an edition
into A4-sheet equivalents; `layout.target_sheets` (2.0) with `length_tolerance` is the
band the preflight enforces; `MM_PER_SHEET` is calibrated from a measured print of the
committed fixture. `tests/test_layout.py` holds both the arithmetic and the measurement.

## Sources

`pipeline/sources.py` is the first gate: nothing enters the pool and nothing reaches the
page unless its host is on the whitelist (major wires, public broadcasters, serious
newspapers and journals, official institutions). `pipeline/config.yaml` lists the feeds —
55 of them across the five sections, every one verified live on 2026-10-13 — and records
the publishers that have discontinued or blocked public feeds (Reuters, AP, Politico,
OECD, IMF, ATP) so nobody re-adds them by guessing. Fetching is concurrent with retries:
one slow publisher cannot hold up the edition, and one blocking publisher cannot empty a
section.

## Delivery

`publish.py` sends the HTML as a document, one chat at a time, honouring Telegram's own
`retry_after` on HTTP 429, reopening the file handle for every attempt, and recording
each delivery in `state/sent.json` keyed `<date>-<EDITION>`. Re-running a job therefore
does not re-send; `--force` overrides deliberately. The bot token lives in the request
URL, so no URL is ever logged and every message passes through `redact()`.

Recipients must have started a conversation with the bot (`403 bot can't initiate
conversation`), and the ledger is committed back by the workflow — that commit is what
makes idempotency survive across runs.

## Automation

`.github/workflows/daily-brief.yml` runs at 01:15 and 13:15 UTC (the edition cutoffs),
plus manual dispatch for backfills. Jobs are split so no job holds both secrets: `build`
(the LLM key) → `publish` (the bot token, after the document checks) → `report` (failure
alert). All actions are pinned to commit SHAs; there is no browser to install.
`build/articles-*.json` and `build/edition-*.json` are uploaded as artifacts — the audit
evidence for what the model was given and what it produced.
