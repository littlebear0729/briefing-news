# The Global Brief — a twice-daily, two-page newspaper, built by code

Twice a day the pipeline fetches candidates from a whitelist of authoritative feeds,
asks a model to *edit* them (choose, order, headline, compress — nothing else), renders
the result into a two-page A4 newspaper, prints it to PDF, proves the PDF is intact, and
sends it through a Telegram bot.

**The design rule behind every decision here: code owns everything stable, the model
owns editorial judgement only.** The model never writes HTML, never writes a URL, never
decides pagination or word counts. Those are versioned assets, config and code.

- Handover document: [`DESIGN.md`](DESIGN.md)
- Status against its checklist: [`DESIGN.md` §11](DESIGN.md) *(what is implemented, what is calibrated, what is not)*

## Pipeline

```
config.yaml ──┐
sources.py ───┴─▶ fetch.py        build/articles-<date>-<ed>.json     deterministic, whitelisted
                       │
                       ▼
                  editorial.py    build/edition-<date>-<ed>.json      the ONLY model call
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
   render.py                     render_mobile.py
   dist/global-brief-*.html      dist/mobile-*.html                    same JSON, two artefacts
        │                              │
        └──────────────┬───────────────┘
                       ▼
                  export.py       dist/*.pdf                             print + assertions
                       ▼
                  publish.py      Telegram sendDocument                 idempotent delivery
                       ▲
              state/sent.json (ledger, committed back)
```

## Quick start

```bash
pip install -r requirements.txt
python -m playwright install chromium        # print engine

# full edition (needs BRIEF_LLM_API_KEY, and TELEGRAM_* to publish)
python -m pipeline.run --edition am --publish

# stage by stage
python -m pipeline.fetch    --edition am --date 2026-10-13
python -m pipeline.editorial build/articles-2026-10-13-am.json
python -m pipeline.render         build/edition-2026-10-13-am.json --date 2026-10-13
python -m pipeline.render_mobile  build/edition-2026-10-13-am.json --date 2026-10-13
python -m pipeline.export         dist/mobile-2026-10-13-am.html --check
python -m pipeline.publish        --pdf dist/mobile-2026-10-13-am.pdf \
                                  --edition-json build/edition-2026-10-13-am.json \
                                  --date 2026-10-13 --edition am
```

No key, no network? Replay the committed fixture — it renders and prints end to end:

```bash
python -m pipeline.render_mobile build/edition-2026-10-13-am.json --date 2026-10-13
python -m pipeline.export dist/mobile-2026-10-13-am.html --check
python -m pipeline.publish --pdf dist/mobile-2026-10-13-am.pdf --dry-run
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs offline and deterministically (a guard fixture fails any test that
reaches the network). It covers the whitelist, the fetch window/dedupe/caps, the schema
and citation contract, budget enforcement, both renderers, the PDF assertion gates, the
Telegram idempotency/retry/token-safety paths, and the manifest. The one test marked
`print_check` prints the **worst-case** edition (every paragraph at its word limit) in a
real browser and asserts it is exactly two A4 pages with all content present; it skips
where no Chromium is available.

## Environment

| Variable | Kind | Notes |
|---|---|---|
| `BRIEF_LLM_API_KEY` | secret | required |
| `BRIEF_LLM_BASE_URL` | var | empty string falls back to `config.yaml` |
| `BRIEF_LLM_MODEL` | var | pin the model version; change it in a PR |
| `BRIEF_LLM_RETRIES` / `BRIEF_LLM_TEMPERATURE` | var | CI stays at 0–0.2 |
| `TELEGRAM_BOT_TOKEN` | secret | dedicated bot; never send it to a group chat by accident |
| `TELEGRAM_CHAT_ID` | secret | comma-separated for several recipients |
| `TELEGRAM_ADMIN_CHAT_ID` | secret | failure alerts |
| `BRIEF_PDF_ENGINE` | var | `auto` (default), `playwright`, `chrome` |
| `BRIEF_CHROME_PATH` | var | explicit browser for the `chrome` engine |

## Layout, print and the calibration

Two artefacts, one edition JSON:

- `templates/brief.html.j2` — print-first; the A4 geometry is always on, so a browser
  preview *is* the PDF.
- `templates/mobile.html.j2` — responsive: one column below 640px, two from 640px, the
  broadsheet grid from 1024px; prints the same two A4 pages as the desktop artefact.

Both share `templates/_pages.j2` (page markup) and `templates/_print.j2` (print
geometry), which is what keeps the printed paper identical between them.

Three things worth knowing before changing the layout:

1. **No CSS multi-column inside a fixed page box.** A multicol container that cannot fit
   fragments into new columns — in paged media, extra sheets — so content is dropped by
   `overflow: hidden` or painted over itself, and `scrollHeight` reports none of it. This
   was a real defect: the mobile PDF silently lost the lead's last paragraph. Columns are
   explicit elements instead.
2. **Page two's columns are packed by code** (`pipeline.content.plan_inside_columns`):
   the natural section order ends 1 017px into a 1 018px column, wasting a fifth of the
   page. Greedy largest-first packs the same content into 894px / 894px.
3. **`config.budgets` is calibrated, not guessed.** The maxima are the largest values
   whose worst case still fits both boxes. `tests/test_layout.py` enforces that
   arithmetic and can print it for real.

`export.assert_pdf` is the compensating control for `overflow: hidden`: it asserts the
page count, the fixed footer on *every* page, and — the part that actually catches
clipping — that the closing words of every section still appear in the printed file.

## Delivery

`publish.py` sends the mobile PDF with `sendDocument`, one chat at a time, honouring
Telegram's own `retry_after` on HTTP 429, reopening the file handle for every attempt,
and recording each delivery in `state/sent.json` keyed `<date>-<EDITION>`. Re-running a
job therefore does not re-send; `--force` overrides deliberately. The bot token lives in
the request URL, so no URL is ever logged and every message passes through `redact()`.

Recipients must have started a conversation with the bot (`403 bot can't initiate
conversation`), and the ledger is committed back by the workflow — that commit is what
makes idempotency survive across runs.

## Automation

`.github/workflows/daily-brief.yml` runs at 01:15 and 13:15 UTC (the edition cutoffs),
plus manual dispatch for backfills. Jobs are split so no job holds both secrets: `build`
(the LLM key) → `publish` (the bot token, after the PDF assertions) → `report` (failure
alert). All actions are pinned to commit SHAs. GitHub cron is best effort — it can run
late or drop a run, so don't promise a reader a delivery minute.

`build/articles-*.json` and `build/edition-*.json` are uploaded as artifacts: they are
the audit evidence for what the model was given and what it produced.
