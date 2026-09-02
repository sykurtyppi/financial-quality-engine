#!/usr/bin/env python3
"""Decision-impact journal — the tool that decides whether the engine is worth keeping.

The one methodological rule: WRITE YOUR THESIS BEFORE YOU READ THE REPORT.
This CLI enforces that structurally by splitting each case into two steps with
timestamps, so hindsight cannot leak backward.

    # 1. Before reading anything — lock your prior view
    journal.py open NVDA --thesis "beat likely priced in; watching inventory" --conviction 3

    # 2. Generate the report (refused until your thesis is written)
    journal.py report NVDA           # EDGAR_IDENTITY must be set

    # 3. Read the report, fill the AFTER block (impact: one of the four codes)

    # 4. Weeks later, record what actually happened
    journal.py outcome NVDA --date 2026-07-31

    # 5. Any time — see where you stand
    journal.py tally

A browser UI over the identical entry files: `.venv/bin/uvicorn app.web:app`.
Core logic lives in app/services/journal/ so the CLI and the web UI never diverge.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.journal import store
from app.services.journal.reporting import build_report
from app.services.journal.schema_v2 import (
    Assumption,
    BeforeBlock,
    EntryV2,
    add_resolution,
    lock_entry,
    open_assumption_indices,
    verify_lock,
)


def cmd_open(args: argparse.Namespace) -> int:
    try:
        path = store.open_entry(args.ticker, args.thesis, args.conviction, args.action)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    except FileExistsError as e:
        print(f"Entry already exists: {e}. Not overwriting.", file=sys.stderr)
        return 1
    print(f"Opened {path}")
    print("Write your BEFORE block now (thesis + conviction), THEN run:")
    print(f"    journal.py report {args.ticker.upper()}")
    return 0


def _cmd_report_v2(path, args: argparse.Namespace) -> int:
    """v2 report path (round-10 finding 1). The v1 text parser cannot read a
    JSON-front-matter entry, so a v2 entry that reached `cmd_report` used to
    fail on `has_thesis(text)`. Now: verify the lock is intact, generate the
    report, THEN stamp `reported`. A tampered BEFORE block refuses to report
    (the entry no longer represents the preregistered claim)."""
    from datetime import datetime, timezone

    entry = store.load_v2(path)
    if not verify_lock(entry):
        print(
            f"{path.name}: LOCK BROKEN — refusing to generate report. The BEFORE "
            "block was edited after the entry was locked; the preregistered "
            "commitment no longer matches what is on disk. Run `journal.py verify` "
            "for details.",
            file=sys.stderr,
        )
        return 1
    if entry.reported is not None:
        print(
            "This case's report was already generated; not regenerating "
            "(prevents peeking-then-editing).",
            file=sys.stderr,
        )
        return 1
    print("Generating report...")
    try:
        out, overall = build_report(
            entry.ticker, with_docs=not args.no_docs, report_day=entry.day.isoformat(),
            fresh=getattr(args, "fresh", False),
        )
    except Exception as e:  # noqa: BLE001
        print(f"Report generation failed: {e}", file=sys.stderr)
        return 1
    if getattr(args, "defer_mark", False):
        # The watch flow marks only AFTER a successful audit: a failed audit
        # must leave the case retryable. Report exists; `reported` does not.
        print(f"overall: {overall} -> {out}")
        print("reported NOT stamped (--defer-mark) — run "
              f"`journal.py mark-reported {entry.ticker} --date {entry.day.isoformat()}` "
              "once the audit succeeds.")
        return 0
    updated = entry.model_copy(update={"reported": datetime.now(timezone.utc)})
    # allow_update=True: `reported` stamp is a legitimate in-place update.
    # save_v2 verifies the BEFORE hash is unchanged, so nothing else can slip in.
    store.save_v2(updated, path, allow_update=True)
    print(f"Report stamped at {updated.reported.isoformat()}.")
    print(f"overall: {overall} -> {out}")
    print(f"\nNow fill the AFTER block: `journal.py after {entry.ticker} --impact CODE "
          f"--conviction-after N` (or edit {path} directly).")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No open entry for {args.ticker.upper()}. Run `journal.py open` first.", file=sys.stderr)
        return 1
    if store.is_v2(path):
        return _cmd_report_v2(path, args)
    text = path.read_text()
    if not store.has_thesis(text):
        print("BEFORE block looks empty — write your thesis first. Refusing to generate the "
              "report (that is the whole point).", file=sys.stderr)
        return 1
    if store.is_reported(text):
        print("This case's report was already generated; not regenerating "
              "(prevents peeking-then-editing).", file=sys.stderr)
        return 1
    print("Generating report...")
    try:
        entry = store.parse_entry(path)
        out, overall = build_report(
            args.ticker, with_docs=not args.no_docs, report_day=entry["day"],
            fresh=getattr(args, "fresh", False),
        )
    except Exception as e:  # noqa: BLE001
        print(f"Report generation failed: {e}", file=sys.stderr)
        return 1
    if getattr(args, "defer_mark", False):
        print(f"overall: {overall} -> {out}")
        day = path.stem.split("_", 1)[1]
        print("reported NOT stamped (--defer-mark) — run "
              f"`journal.py mark-reported {args.ticker.upper()} --date {day}` "
              "once the audit succeeds.")
        return 0
    store.mark_reported(path)
    print(f"Thesis locked at {store.now_iso()}.")
    print(f"overall: {overall} -> {out}")
    print(f"\nNow read the report and fill the AFTER block in {path}")
    print(f"  impact: one of {', '.join(store.IMPACT_CODES)}")
    return 0


def _resolve_v2_or_exit(args: argparse.Namespace) -> tuple[object, object] | int:
    """Common preamble for v2 AFTER/OUTCOME commands: locate the entry, refuse
    v1, load, verify the lock, and refuse if the report has not been generated
    yet. Returns (path, entry) on success or a non-zero exit code (as int).

    Round-13 finding 1/2 (P1): AFTER captures the user's REACTION to the
    engine report and OUTCOME records what actually happened afterwards.
    Filling either BEFORE `report` has ever run degrades the "no
    peek-then-edit" guarantee from "BEFORE cannot be edited after reading"
    to "BEFORE is locked, but AFTER can be pre-filled before reading" —
    which defeats the whole measurement. `reported is None` is refused here.
    """
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    if not store.is_v2(path):
        print(f"{path.name}: v1 entry — this command targets v2 (openv2) entries. "
              f"Edit the file directly for v1.", file=sys.stderr)
        return 1
    entry = store.load_v2(path)
    if not verify_lock(entry):
        print(f"{path.name}: LOCK BROKEN — refusing to edit AFTER/OUTCOME on a "
              f"tampered entry. Run `journal.py verify {args.ticker.upper()}` "
              f"for details.", file=sys.stderr)
        return 1
    if entry.reported is None:
        print(f"{path.name}: report not yet generated — run "
              f"`journal.py report {args.ticker.upper()}` first. AFTER/OUTCOME "
              f"only make sense POST-report; filling them beforehand would "
              f"corrupt the causal chain the whole journal measures.",
              file=sys.stderr)
        return 1
    return path, entry


def _normalize_free_text(v):
    """`""` -> None (round-13 finding 5). Users pass empty strings sometimes;
    persisting them muddies the "is this field set?" question everywhere and
    the model treats None as "unset"."""
    if v is None:
        return None
    s = v.strip()
    return s or None


def cmd_after(args: argparse.Namespace) -> int:
    """Fill (or overwrite) the AFTER block on a v2 entry. AFTER captures the
    engine's IMPACT on the user's thesis — measured AFTER reading the report.
    The BEFORE hash is unchanged, so the lock survives.
    """
    got = _resolve_v2_or_exit(args)
    if isinstance(got, int):
        return got
    path, entry = got

    fields: dict = {}
    if args.impact is not None:
        fields["impact"] = args.impact
    if args.conviction_after is not None:
        fields["conviction_after"] = args.conviction_after
    # Round-13 finding 5: empty free-text becomes None, not "".
    surfaced = _normalize_free_text(args.surfaced)
    if surfaced is not None:
        fields["what_it_surfaced"] = surfaced
    disagreed = _normalize_free_text(args.disagreed)
    if disagreed is not None:
        fields["what_i_disagreed_with"] = disagreed
    if not fields:
        print("Nothing to update. Provide at least one of: --impact, "
              "--conviction-after, --surfaced, --disagreed.", file=sys.stderr)
        return 1

    try:
        updated = entry.model_copy(update={
            "after": entry.after.model_copy(update=fields),
        })
    except Exception as e:  # pydantic ValidationError on impact/conviction bounds
        print(f"AFTER update rejected: {e}", file=sys.stderr)
        return 1
    store.save_v2(updated, path, allow_update=True)
    changed = ", ".join(sorted(fields))
    print(f"{path.name}: AFTER updated ({changed}).")
    return 0


def _parse_bool(s: str) -> bool:
    """Parse a CLI truthiness string. `argparse` treats any nonempty --y as True
    by default — that's a footgun for `--y false`. Reject anything ambiguous."""
    s = (s or "").strip().lower()
    if s in ("true", "t", "yes", "y", "1"):
        return True
    if s in ("false", "f", "no", "n", "0"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {s!r}")


# Round-13 finding 4: factual OUTCOME fields are immutable once set. `y` enters
# Brier calibration and re-flipping it silently rewrites the scored outcome —
# exactly the class of retroactive edit BEFORE was hash-locked against. Same
# logic applies to `verdict` (subjective but final) and `outcome_date` (a
# factual timestamp). Free-text `what_happened` stays editable — typos and
# expanded prose are normal iterative use.
_OUTCOME_IMMUTABLE_ONCE_SET: frozenset[str] = frozenset({"y", "verdict", "outcome_date"})


def _cmd_outcome_v2(path, entry, args: argparse.Namespace) -> int:
    """v2 OUTCOME path: fill outcome_date, what_happened, verdict, y."""
    from datetime import date as _date

    fields: dict = {}
    if args.outcome_date is not None:
        try:
            od = _date.fromisoformat(args.outcome_date)
        except ValueError as e:
            print(f"Bad --outcome-date: {e}", file=sys.stderr)
            return 1
        # Round-13 finding 3: outcome cannot precede the preregistration.
        if od < entry.day:
            print(
                f"--outcome-date {od.isoformat()} precedes the preregistration "
                f"date {entry.day.isoformat()} — an outcome cannot have happened "
                f"before the thesis was written.",
                file=sys.stderr,
            )
            return 1
        fields["outcome_date"] = od
    if args.what_happened is not None:
        wh = _normalize_free_text(args.what_happened)
        if wh is not None:
            fields["what_happened"] = wh
    if args.verdict is not None:
        fields["verdict"] = args.verdict
    if args.y is not None:
        # `y` is the observed binary outcome of `before.outcome_definition`. If
        # the user did not preregister a definition (and a paired p_outcome),
        # setting `y` is meaningless — it doesn't score anything. Refuse.
        if not (entry.before.outcome_definition and entry.before.p_outcome is not None):
            print(
                "--y requires an entry with `outcome_definition` + `p_outcome` "
                "preregistered at openv2 time. Setting y otherwise produces an "
                "orphaned observation (not scored by Brier).",
                file=sys.stderr,
            )
            return 1
        fields["y"] = args.y
    if not fields:
        print("Nothing to update. Provide at least one of: --outcome-date, "
              "--what-happened, --verdict, --y.", file=sys.stderr)
        return 1

    # Round-13 finding 4: refuse to overwrite already-set factual fields.
    # Idempotent same-value re-set is allowed (harmless). Distinct re-set
    # refused — user must edit the file directly (git captures the change).
    for name in _OUTCOME_IMMUTABLE_ONCE_SET:
        if name in fields:
            existing = getattr(entry.outcome, name)
            if existing is not None and existing != fields[name]:
                print(
                    f"OUTCOME.{name} is already set to {existing!r} and cannot be "
                    f"changed to {fields[name]!r} through the CLI. Factual outcome "
                    f"fields are immutable once recorded — this is the same "
                    f"anti-hindsight guarantee BEFORE gets via the hash lock. "
                    f"If this is a genuine correction, edit the file directly "
                    f"so git records the change.",
                    file=sys.stderr,
                )
                return 1

    try:
        updated = entry.model_copy(update={
            "outcome": entry.outcome.model_copy(update=fields),
        })
    except Exception as e:  # pydantic ValidationError on verdict/y bounds
        print(f"OUTCOME update rejected: {e}", file=sys.stderr)
        return 1
    store.save_v2(updated, path, allow_update=True)
    changed = ", ".join(sorted(fields))
    print(f"{path.name}: OUTCOME updated ({changed}).")
    return 0


def cmd_outcome(args: argparse.Namespace) -> int:
    # v1 dispatch first, so v1 entries don't hit the v2-specific guards
    # (`reported` requirement, structured field refusals).
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is not None and not store.is_v2(path):
        print(f"Edit the OUTCOME block in {path} (outcome_date, what_happened, verdict).")
        return 0
    # v2 (or path-is-None): route through the shared preamble, which handles
    # missing entry, v1 rejection, lock verification, AND the round-13 F2
    # "reported must exist" check. Then dispatch to the field updater.
    got = _resolve_v2_or_exit(args)
    if isinstance(got, int):
        return got
    p, entry = got
    return _cmd_outcome_v2(p, entry, args)


def _parse_assumption(spec: str) -> Assumption:
    """Parse `metric,comparator,threshold,window,source,resolve_by`.

    Threshold is numeric if it parses, else a symbolic keyword.
    Source is optional (round-11 finding 2): pass an empty field to make the
    assumption numeric-only; setting a source form defers resolution to
    manual attestation until per-value provenance (P1-A) lands.
    """
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) != 6:
        raise argparse.ArgumentTypeError(
            f"assumption must have 6 comma-separated fields: "
            f"metric,comparator,threshold,window,source,resolve_by (got {len(parts)}). "
            f"Leave `source` empty for a numeric-only commitment: "
            f"'revenue,>,165000000,FY2026Q2,,2026-08-15'."
        )
    metric, cmp_, thr, window, source, resolve_by = parts
    try:
        threshold: float | str = float(thr)
    except ValueError:
        threshold = thr
    from datetime import date as _date
    return Assumption(
        metric=metric,
        comparator=cmp_,  # type: ignore[arg-type]  # Literal validated by pydantic
        threshold=threshold,
        window=window,
        source=source or None,  # type: ignore[arg-type]  # empty -> None (numeric-only)
        resolve_by=_date.fromisoformat(resolve_by),
    )


def cmd_openv2(args: argparse.Namespace) -> int:
    """P1-E: open + lock a v2 entry in one step. The anti-annoyance rule is that
    thesis + conviction + one assumption row is all you need."""
    from datetime import date, datetime, timezone

    try:
        ticker = store.safe_ticker(args.ticker)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1

    try:
        assumptions = [_parse_assumption(spec) for spec in (args.assumption or [])]
    except (argparse.ArgumentTypeError, ValueError) as e:
        print(f"Bad --assumption: {e}", file=sys.stderr)
        return 1

    # Round-11 finding 1: refuse `--p-outcome` without `--outcome-definition`
    # at the CLI. The model-level validator also catches this, but a nice CLI
    # error beats a pydantic traceback for the common misuse. Because the
    # BEFORE block is hash-locked at openv2 time, users cannot add the
    # definition later without breaking the lock — so it has to be here now.
    if args.p_outcome is not None and not (args.outcome_definition and args.outcome_definition.strip()):
        print(
            "--p-outcome requires --outcome-definition. Brier is undefined "
            "without a preregistered event to score against; the lock hashes "
            "the BEFORE block, so you cannot add it after openv2.",
            file=sys.stderr,
        )
        return 1

    try:
        before = BeforeBlock(
            thesis=args.thesis,
            conviction=args.conviction,
            conviction_fine=args.conviction_fine,
            intended_action=args.action,
            catalyst=args.catalyst,
            contamination=args.contamination,
            outcome_definition=args.outcome_definition,
            p_outcome=args.p_outcome,
            reference_class=args.reference_class,
            assumptions=assumptions,
            falsifiers=args.falsifier or [],
        )
        day = date.today() if not args.date else date.fromisoformat(args.date)
        entry = EntryV2(
            ticker=ticker, day=day,
            opened=datetime.now(timezone.utc),
            before=before,
        )
    except Exception as e:  # pydantic ValidationError or bad --date
        print(f"Could not build entry: {e}", file=sys.stderr)
        return 1

    try:
        locked = lock_entry(entry)
    except ValueError as e:
        print(f"Cannot lock: {e}", file=sys.stderr)
        return 1

    try:
        path = store.save_v2(locked)
    except FileExistsError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    print(f"Locked {path}")
    print(f"before_sha256: {locked.before_sha256}")
    print(f"assumptions: {len(locked.before.assumptions)}"
          + (f", falsifiers: {len(locked.before.falsifiers)}" if locked.before.falsifiers else ""))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Recompute the BEFORE-block hash and report tamper status."""
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    if not store.is_v2(path):
        print(f"{path.name}: v1 entry (no hash lock)")
        return 0
    entry = store.load_v2(path)
    if verify_lock(entry):
        print(f"{path.name}: LOCK INTACT (sha256 {entry.before_sha256[:16]}…)")
        return 0
    print(f"{path.name}: LOCK BROKEN — BEFORE block has been edited since lock", file=sys.stderr)
    return 2


def cmd_resolve(args: argparse.Namespace) -> int:
    """Engine proposes met/violated/unresolvable for every open assumption on a
    v2 entry. Dry-run by default; `--commit` writes the resolutions back."""
    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry for {args.ticker.upper()}.", file=sys.stderr)
        return 1
    if not store.is_v2(path):
        print(f"{path.name}: v1 entry — resolutions only apply to v2 (openv2).", file=sys.stderr)
        return 1

    entry = store.load_v2(path)
    # Round-10 finding 2: a tampered BEFORE block invalidates every downstream
    # measurement. Refuse to propose or commit against it. The user must first
    # restore the file (git) or re-open a new entry.
    if not verify_lock(entry):
        print(
            f"{path.name}: LOCK BROKEN — refusing to resolve. The BEFORE block "
            "no longer matches its hash; propose/commit against a tampered "
            "preregistration would silently contaminate the calibration set.",
            file=sys.stderr,
        )
        return 1
    open_idxs = open_assumption_indices(entry)
    if not open_idxs:
        print(f"{path.name}: no open assumptions.")
        return 0

    # Import lazily so the resolver + pipeline aren't loaded for openv2/verify.
    from app.services.ingestion.edgar_adapter import fetch_dataset
    from app.services.formulas.registry import compute_metrics
    from app.services.journal.resolver import propose_resolution

    try:
        dataset, _ = fetch_dataset(entry.ticker)
    except Exception as e:  # noqa: BLE001 - a fetch failure must not corrupt the entry
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1
    bundle = compute_metrics(dataset)

    proposals = [
        propose_resolution(entry.before.assumptions[i], dataset, bundle, assumption_index=i)
        for i in open_idxs
    ]

    print(f"{path.name}: {len(proposals)} open assumption(s)")
    for r in proposals:
        a = entry.before.assumptions[r.assumption_index]
        print(f"  [{r.assumption_index}] {a.metric} {a.comparator} {a.threshold} @ {a.window}")
        obs = "n/a" if r.observed is None else f"{r.observed:g}"
        at = r.at.isoformat() if r.at else "—"
        print(f"      -> {r.state.upper():12s} observed={obs}  at={at}")
        if r.note:
            print(f"         note: {r.note}")

    if not args.commit:
        print("\n(dry-run; pass --commit to write these resolutions to the entry)")
        return 0

    # Round-10 finding 4: `pending` is NOT terminal. Never write it — that
    # would close the assumption and prevent retry after the filing arrives.
    terminal = [r for r in proposals if r.state != "pending"]
    pending = [r for r in proposals if r.state == "pending"]
    if not terminal:
        print("\nnothing to commit — all proposals are `pending` (retry after the filing arrives).")
        return 0
    updated = entry
    for r in terminal:
        updated = add_resolution(updated, r)
    # allow_update=True: writing resolutions is a legitimate in-place update
    # (add_resolution preserves BEFORE, so the hash matches — save_v2 verifies).
    store.save_v2(updated, path, allow_update=True)
    print(f"\ncommitted {len(terminal)} terminal resolution(s) to {path.name}"
          + (f"; left {len(pending)} pending for retry" if pending else ""))
    return 0


def _print_v2_section(v2: dict) -> None:
    print(f"\n== v2 entries — {v2['total']} preregistered ({v2['locked']} locked, "
          f"{v2['lock_broken']} LOCK BROKEN) ==")
    if v2["lock_broken"]:
        print("  ⚠ some BEFORE blocks were edited after locking; run "
              "`journal.py verify TICKER` to identify")
    if v2.get("parse_failed"):
        pf = v2["parse_failed"]
        print(f"  ⚠ {len(pf)} v2 file(s) failed to parse and were excluded: "
              f"{', '.join(pf[:5])}{' …' if len(pf) > 5 else ''}")
    r = v2["resolutions"]
    print(f"  Resolutions: {r['met']} met  ·  {r['violated']} violated  ·  "
          f"{r['unresolvable']} unresolvable"
          + (f"  ·  {r['pending']} pending" if r.get("pending") else ""))
    print(f"  Open assumptions still awaiting resolution: {v2['open_assumptions']}")
    if v2["overdue_assumptions"]:
        overdue = v2["overdue_assumptions"]
        print(f"  Past resolve_by ({len(overdue)}):")
        for ticker, day, metric in overdue[:8]:
            print(f"    {ticker} ({day}): {metric}")
        if len(overdue) > 8:
            print(f"    … +{len(overdue) - 8} more")
    n = v2["resolved_with_p_outcome"]
    if v2["brier"] is not None:
        print(f"  Brier: {v2['brier']:.3f}  (n={n})")
    elif n:
        print(f"  {n} case(s) resolved with p_outcome — Brier deferred until "
              f"n >= {v2['brier_min_n']} (Murphy floor).")


def cmd_tally(args: argparse.Namespace) -> int:
    t = store.tally()
    v2 = store.v2_tally()
    if not t["total"] and not v2["total"]:
        print("No journal entries yet. Start with `journal.py open TICKER` or `openv2`.")
        return 0
    if not t["total"]:
        _print_v2_section(v2)
        return 0
    print(f"Journal tally — {t['total']} cases logged ({t['scored']} scored, "
          f"{t['with_outcome']} with outcomes)\n")
    print("Impact (of scored cases):")
    for code in store.IMPACT_CODES:
        n = t["impact_counts"][code]
        pct = f"{n / t['scored']:.0%}" if t["scored"] else "—"
        print(f"  {code:20s} {n:3d}  ({pct})")
    if t["scored"]:
        print(f"\n  changed something (not no_value): {t['changed_any']}/{t['scored']} "
              f"({t['changed_any'] / t['scored']:.0%})")
        print(f"  conviction moved: {t['conv_moved']}, unchanged: {t['conv_same']}")
    if t["verdicts"]:
        print("\nOutcomes (where recorded):")
        for v, n in sorted(t["verdicts"].items(), key=lambda kv: -kv[1]):
            print(f"  {v:12s} {n}")
    missing_after = [e["name"] for e in t["entries"] if e["needs_after"]]
    missing_outcome = [e["name"] for e in t["entries"] if e["needs_outcome"]]
    if missing_after:
        print(f"\n{len(missing_after)} cases still need an AFTER block: "
              f"{', '.join(missing_after[:8])}{' ...' if len(missing_after) > 8 else ''}")
    if missing_outcome:
        print(f"{len(missing_outcome)} cases still need an OUTCOME (revisit in a few weeks).")
    if t["gate_ready"]:
        print("\n>=20 cases with outcomes: enough to judge. Decision gate — would you keep "
              "using it voluntarily? See docs/evaluation_protocol.md.")
    if v2["total"]:
        _print_v2_section(v2)
    return 0


def cmd_mark_reported(args: argparse.Namespace) -> int:
    """Stamp `reported` on an entry whose report was generated with
    --defer-mark. Kept separate so the watch flow can order it strictly AFTER
    a successful audit — report-then-mark, never mark-then-hope."""
    from datetime import datetime, timezone

    try:
        path = store.find_entry(args.ticker, args.date)
    except ValueError as e:
        print(f"Invalid ticker: {e}", file=sys.stderr)
        return 1
    if path is None:
        print(f"No entry for {args.ticker.upper()}"
              f"{' on ' + args.date if args.date else ''}.", file=sys.stderr)
        return 1
    if store.is_v2(path):
        entry = store.load_v2(path)
        if not verify_lock(entry):
            print(f"{path.name}: LOCK BROKEN — refusing to stamp a tampered entry.",
                  file=sys.stderr)
            return 1
        if entry.reported is not None:
            print(f"{path.name}: already reported.", file=sys.stderr)
            return 1
        updated = entry.model_copy(update={"reported": datetime.now(timezone.utc)})
        store.save_v2(updated, path, allow_update=True)
        print(f"Report stamped at {updated.reported.isoformat()}.")
        return 0
    text = path.read_text()
    if store.is_reported(text):
        print(f"{path.name}: already reported.", file=sys.stderr)
        return 1
    store.mark_reported(path)
    print(f"Reported stamped at {store.now_iso()}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Decision-impact journal for the earnings-quality engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_open = sub.add_parser("open", help="lock your prior view before reading the report")
    p_open.add_argument("ticker")
    p_open.add_argument("--thesis")
    p_open.add_argument("--conviction", type=int, choices=range(1, 6))
    p_open.add_argument("--action")
    p_open.set_defaults(func=cmd_open)

    p_rep = sub.add_parser("report", help="lock the thesis timestamp and generate the report")
    p_rep.add_argument("ticker")
    p_rep.add_argument("--date")
    p_rep.add_argument("--no-docs", action="store_true")
    p_rep.add_argument("--defer-mark", action="store_true",
                       help="generate but do NOT stamp `reported` — the watch flow "
                            "stamps only after a successful audit (mark-reported)")
    p_rep.add_argument("--fresh", action="store_true",
                       help="bypass the EDGAR cache (use on filing night)")
    p_rep.set_defaults(func=cmd_report)

    p_mark = sub.add_parser("mark-reported",
                            help="stamp `reported` after a deferred (--defer-mark) report")
    p_mark.add_argument("ticker")
    p_mark.add_argument("--date")
    p_mark.set_defaults(func=cmd_mark_reported)

    p_out = sub.add_parser("outcome", help="record what actually happened, weeks later")
    p_out.add_argument("ticker")
    p_out.add_argument("--date")
    # v2 field-setting args (ignored on v1 entries; v1 keeps the "edit the file" message).
    p_out.add_argument("--outcome-date", dest="outcome_date",
                       help="v2: date the outcome landed (YYYY-MM-DD)")
    p_out.add_argument("--what-happened", dest="what_happened",
                       help="v2: free-text description of what actually happened")
    p_out.add_argument("--verdict", choices=["helped", "neutral", "hurt", "too_early"],
                       help="v2: did the engine's report help the decision?")
    p_out.add_argument("--y", dest="y", type=_parse_bool,
                       help="v2: observed binary outcome for `outcome_definition` "
                            "(true/false) — enters Brier calibration")
    p_out.set_defaults(func=cmd_outcome)

    p_tal = sub.add_parser("tally", help="summarize decision impact across all cases")
    p_tal.set_defaults(func=cmd_tally)

    # ------ P1-E v2: preregistration + hash-lock ------
    p_o2 = sub.add_parser(
        "openv2",
        help="P1-E: lock a v2 (preregistered, hash-locked) entry in one step",
    )
    p_o2.add_argument("ticker")
    p_o2.add_argument("--thesis", required=True)
    p_o2.add_argument("--conviction", type=int, choices=range(1, 6), required=True)
    p_o2.add_argument("--action",
                      choices=["hold", "trim", "add", "avoid", "no_position"], required=True)
    p_o2.add_argument("--assumption", action="append", default=[],
                      help="repeatable: metric,comparator,threshold,window,source,resolve_by")
    p_o2.add_argument("--falsifier", action="append", default=[],
                      help='repeatable: "I am wrong if <observable>"')
    p_o2.add_argument("--catalyst", help="expected event + date")
    p_o2.add_argument("--contamination", help="what analysis PRECEDED this entry")
    p_o2.add_argument("--p-outcome", dest="p_outcome", type=float,
                      help="0.0–1.0; enables Brier scoring "
                           "(requires --outcome-definition)")
    p_o2.add_argument("--outcome-definition", dest="outcome_definition",
                      help="the BINARY event `--p-outcome` scores against "
                           "(concrete: e.g. 'MXL Q2 revenue > 165M by 2026-08-15')")
    p_o2.add_argument("--reference-class", dest="reference_class")
    p_o2.add_argument("--conviction-fine", dest="conviction_fine", type=int,
                      help="optional 0–100 fine grain")
    p_o2.add_argument("--date", help="entry day YYYY-MM-DD (defaults to today)")
    p_o2.set_defaults(func=cmd_openv2)

    p_ver = sub.add_parser("verify", help="recompute the v2 BEFORE-block hash and report tamper status")
    p_ver.add_argument("ticker")
    p_ver.add_argument("--date")
    p_ver.set_defaults(func=cmd_verify)

    p_res = sub.add_parser(
        "resolve",
        help="engine proposes met/violated/unresolvable for open assumptions (dry-run by default)",
    )
    p_res.add_argument("ticker")
    p_res.add_argument("--date")
    p_res.add_argument("--commit", action="store_true",
                       help="write the proposed resolutions back to the entry file")
    p_res.set_defaults(func=cmd_resolve)

    p_aft = sub.add_parser(
        "after",
        help="v2: fill the AFTER block (impact of the engine report on your view)",
    )
    p_aft.add_argument("ticker")
    p_aft.add_argument("--date")
    p_aft.add_argument("--impact",
                       choices=["changed_thesis", "changed_confidence",
                                "new_investigation", "no_value"])
    p_aft.add_argument("--conviction-after", dest="conviction_after",
                       type=int, choices=range(1, 6))
    p_aft.add_argument("--surfaced", help="what the report surfaced you didn't have")
    p_aft.add_argument("--disagreed", help="what the report claimed that you rejected")
    p_aft.set_defaults(func=cmd_after)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
