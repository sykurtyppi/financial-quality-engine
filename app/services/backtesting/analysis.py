"""Backtest analysis: rank correlations, quintile spreads, hit/false-positive
rates, archetype diagnostics. Pure functions over the results CSV; no third-
party dependencies (Spearman implemented directly).

Interpretation conventions:
- Scores are CONCERN scores. If a signal works, higher concern should
  associate with WORSE forward outcomes: negative IC vs returns/margins.
- "High risk" = overall score > 60 (the engine's negative-direction line).
- "Bad outcome" = 12M benchmark-relative return < -10%.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

HIGH_RISK = 60.0
BAD_RETURN = -0.10
MIN_SAMPLE = 30


def load_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open() as fh:
        return list(csv.DictReader(fh))


def ok_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get("status") == "ok"]


def _f(row: dict[str, str], key: str) -> float | None:
    v = row.get(key, "")
    if v in ("", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


@dataclass
class ICResult:
    signal: str
    outcome: str
    ic: float | None
    n: int


def component_ics(rows: list[dict[str, str]], signal_cols: list[str], outcome_cols: list[str]) -> list[ICResult]:
    out: list[ICResult] = []
    for sig in signal_cols:
        for outc in outcome_cols:
            pairs = [
                (_f(r, sig), _f(r, outc))
                for r in rows
                if _f(r, sig) is not None and _f(r, outc) is not None
            ]
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            out.append(ICResult(sig, outc, spearman(xs, ys), len(pairs)))  # type: ignore[arg-type]
    return out


def quintile_stats(rows: list[dict[str, str]], score_col: str, outcome_col: str) -> list[dict]:
    pairs = sorted(
        (
            (_f(r, score_col), _f(r, outcome_col))
            for r in rows
            if _f(r, score_col) is not None and _f(r, outcome_col) is not None
        ),
    )
    n = len(pairs)
    if n < MIN_SAMPLE:
        return []
    out = []
    for q in range(5):
        chunk = pairs[q * n // 5 : (q + 1) * n // 5]
        if not chunk:
            continue
        outcomes = [p[1] for p in chunk]
        out.append(
            {
                "quintile": q + 1,
                "score_range": (chunk[0][0], chunk[-1][0]),
                "mean_outcome": sum(outcomes) / len(outcomes),
                "median_outcome": sorted(outcomes)[len(outcomes) // 2],
                "n": len(chunk),
            }
        )
    return out


def hit_rates(rows: list[dict[str, str]], score_col: str = "overall") -> dict:
    scored = [
        r for r in rows if _f(r, score_col) is not None and _f(r, "rel_12m") is not None
    ]
    if not scored:
        return {}
    bad = [r for r in scored if _f(r, "rel_12m") < BAD_RETURN]  # type: ignore[operator]
    flagged = [r for r in scored if _f(r, score_col) > HIGH_RISK]  # type: ignore[operator]
    flagged_bad = [r for r in flagged if _f(r, "rel_12m") < BAD_RETURN]  # type: ignore[operator]
    return {
        "n": len(scored),
        "base_rate_bad": len(bad) / len(scored),
        "n_flagged": len(flagged),
        "hit_rate": (len(flagged_bad) / len(flagged)) if flagged else None,
        "false_positive_rate": (1 - len(flagged_bad) / len(flagged)) if flagged else None,
    }


def archetype_diagnostics(rows: list[dict[str, str]]) -> list[dict]:
    by_arch: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_arch[r["archetype"]].append(r)
    out = []
    for arch, rs in sorted(by_arch.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        scored = [r for r in ok if _f(r, "overall") is not None]
        flagged = [r for r in scored if _f(r, "overall") > HIGH_RISK]  # type: ignore[operator]
        flagged_with_ret = [r for r in flagged if _f(r, "rel_12m") is not None]
        benign = [r for r in flagged_with_ret if _f(r, "rel_12m") >= BAD_RETURN]  # type: ignore[operator]
        out.append(
            {
                "archetype": arch,
                "rows": len(rs),
                "scored": len(scored),
                "excluded": sum(1 for r in rs if r["status"] == "excluded_financial"),
                "stale_skips": sum(1 for r in rs if r["status"] == "skip_stale"),
                "mean_overall": (
                    sum(_f(r, "overall") for r in scored) / len(scored) if scored else None  # type: ignore[misc]
                ),
                "pct_flagged": (len(flagged) / len(scored)) if scored else None,
                "fp_rate_among_flagged": (len(benign) / len(flagged_with_ret)) if flagged_with_ret else None,
            }
        )
    return out


def stale_skip_signal(rows: list[dict[str, str]]) -> list[dict]:
    """skip_stale = the filer had not filed recent statements at the as-of
    date. Filing delays are themselves an accounting-risk event; report them."""
    by_ticker: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["status"] == "skip_stale":
            by_ticker[r["ticker"]] += 1
    return [
        {"ticker": t, "stale_asofs": c}
        for t, c in sorted(by_ticker.items(), key=lambda kv: -kv[1])
        if c > 0
    ]
