# Decision-Impact Journal

The experiment that decides whether this engine is worth keeping. Every strategy
review converged on one gate — *does it change my decisions?* — and only this
journal, run blind over a real earnings season, can answer it.

## The daily loop

```
# 1. BEFORE reading anything — lock your prior view
scripts/journal.py open NVDA --thesis "beat priced in; watching inventory" --conviction 3

# 2. Generate the report (refused until your thesis is written)
scripts/journal.py report NVDA           # EDGAR_IDENTITY must be set

# 3. Read the report, then fill the AFTER block in journal/entries/NVDA_<date>.md
#    impact:  one or more of  changed_thesis | changed_confidence | new_investigation | no_value

# 4. Weeks later — what actually happened
scripts/journal.py outcome NVDA          # then edit the OUTCOME block

# 5. Any time — where do I stand
scripts/journal.py tally
```

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
