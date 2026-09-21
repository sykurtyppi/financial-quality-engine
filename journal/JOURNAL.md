# Decision-Impact Journal

The experiment that decides whether this engine is worth keeping. Every strategy
review converged on one gate — *does it change my decisions?* — and only this
journal, run blind over a real earnings season, can answer it.

> **Harness verified turnkey — 2026-07-04.** End-to-end smoke test passed: the
> thesis-lock refuses `report` until a real BEFORE thesis exists; `generate_report.py`
> runs (AAPL 31.2, 21 docs, all 11 sections incl. §2 scorecard = distress and
> §6/§8b narrative = disclosure monitor — the two validated signals); `tally`
> works. Nothing left to build — the only remaining input is your blind prior
> views, entered live over your own coverage names during earnings season. The
> theses must be yours: fabricated priors would defeat the one experiment designed
> to be un-foolable.

> **Season prep:** [SEASON_2026Q2.md](SEASON_2026Q2.md) — the Q2 2026 calendar,
> per-name consensus questions, and macro context (researched 2026-07-20).
> Context only; the theses are yours, written blind per case.

## Two ways to run it (same entry files)

- **CLI** — the loop below.
- **Web UI** — `.venv/bin/uvicorn app.web:app`, then open http://127.0.0.1:8000.
  A READER over the same `journal/entries/*.md` files: it shows the legacy (v1)
  queue and lists preregistered (v2) cases read-only with their lock status. It
  no longer opens cases, and it refuses to write into a v2 entry — the hash lock
  is verified by the CLI commands that own it (`openv2`, `report`, `after`,
  `resolve`). A dogfooding convenience, not a product surface.

## Earnings-night automation (scripts/watch.py)

The calendar side is automated; your side is not. `watch.py due` reminds you to
write the prior *before* a watched name prints, and `watch.py poll` waits for
the filing and runs step 2 the moment it lands. It will **refuse** to generate a
report for a name with no locked thesis — see
[docs/earnings_night_runbook.md](../docs/earnings_night_runbook.md). Calendar
lives in `watchlist.json` (timing only, never theses).

## The daily loop

```
# 1. BEFORE reading anything — lock your prior view, then pin it to the event
scripts/journal.py openv2 NVDA \
  --thesis "beat priced in; watching inventory" --conviction 3 --action hold \
  --assumption "revenue,>,57000000000,FY2027Q3,,2026-11-25"
scripts/watch.py link NVDA

# 2. Generate the report (refused until a locked thesis exists)
scripts/journal.py report NVDA           # EDGAR_IDENTITY must be set

# 3. Read the report, then record what it changed
scripts/journal.py after NVDA --impact changed_confidence --conviction-after 4

# 4. Weeks later — what actually happened
scripts/journal.py outcome NVDA --outcome-date 2026-12-01 \
  --what-happened "revenue printed at 59.1B" --verdict helped

# 5. Any time — where do I stand
scripts/journal.py tally
```

`openv2`, not `open`. The v1 `open` path still exists for the one legacy
entry, but a v1 entry cannot be hash-locked, `watch.py link` refuses to pin it
to an event, and `tally` excludes it from every inferential metric — so a case
opened that way produces no evidence. Steps 3 and 4 are CLI-only now; nothing
in the loop requires editing an entry file by hand, and the BEFORE block must
not be edited at all (rule 3 below is enforced by the hash, not by discipline).

See [docs/earnings_night_runbook.md](../docs/earnings_night_runbook.md) for the
assumption field format and why `source` is normally left empty.

## The four rules that make this real evidence (not a diary)

1. **Thesis before report, always.** The CLI enforces it; don't work around it.
2. **Log every case you open — including the boring ones.** Logging only the
   impressive hits is the single fastest way to fool yourself into keeping a tool
   that doesn't earn its place.
3. **No editing the BEFORE block after you've read the report.** If you were wrong,
   that's the data.
4. **Record outcomes weeks later, blind to how you feel about the tool now.**

## The decision gate

At ~20–30 cases with outcomes, `tally` will prompt the only question that matters:
**would you keep using this voluntarily, with no one watching?** If yes, the copilot
wedge is worth building. If no, it's a successful internal tool + portfolio artifact
and feature work stops. Pre-commit to honoring whichever answer comes back
(docs/evaluation_protocol.md).

Entries live in `journal/entries/` (gitignored — this is your private trading
journal, not repo content).
