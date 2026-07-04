"""Decision-impact journal: shared store used by both the CLI (scripts/journal.py)
and the local web UI (app/web.py), so the two never duplicate parsing logic and
operate on the identical markdown entry files."""
