"""Forward fundamental outcomes, computed on the FULL (non-point-in-time)
dataset — outcomes are allowed to see the future; signals are not.

Proxies and their honest labels (analyst-estimate data is not freely
available, so 'earnings revisions' cannot be tested directly):

- op_margin_chg_4q     — realized operating-margin change, t+4 quarters vs t
- fcf_margin_chg_4q    — mean FCF margin of next 4 quarters minus mean of the
                         trailing 4 (a realized-deterioration proxy for
                         "negative FCF surprise")
- ni_growth_fwd_4q     — realized net-income growth, next 4 quarters vs
                         trailing 4 (proxy standing in for revisions)

Anchoring (round-16 F1): the signal is computed on a point-in-time slice whose
newest period is the last quarter *filed* by as_of; a quarter can have ended
before as_of without being filed yet, so re-deriving the base here as the last
quarter *ended* by as_of can start the outcome one quarter ahead of the scored
baseline. Callers with a PIT slice must pass that slice's newest period_end as
`anchor`; the ended-by-as_of derivation remains only for callers without one.
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import CompanyDataset, PeriodFinancials, PeriodType
from app.services.formulas.ttm import MAX_GAP_DAYS, MIN_GAP_DAYS


def _op_margin(p: PeriodFinancials) -> float | None:
    if p.operating_income is None or p.revenue is None or p.revenue <= 0:
        return None
    return p.operating_income / p.revenue


def _fcf_margin(p: PeriodFinancials) -> float | None:
    if p.cfo is None or p.capex is None or p.revenue is None or p.revenue <= 0:
        return None
    return (p.cfo - p.capex) / p.revenue


def _quarter_window(
    periods: list[PeriodFinancials], start: int, stop: int
) -> list[PeriodFinancials] | None:
    """periods[start:stop] if it is a full run of consecutive fiscal quarters.

    Positional offsets like idx+4 only mean "four quarters later" when nothing
    in between is missing, annual, or duplicated — otherwise the horizon
    silently shifts. Bounds match app/services/formulas/ttm.py.
    """
    if start < 0 or stop > len(periods):
        return None
    window = periods[start:stop]
    if any(p.period_type is not PeriodType.QUARTER for p in window):
        return None
    for a, b in zip(window, window[1:]):
        gap = (b.period_end - a.period_end).days
        if not (MIN_GAP_DAYS <= gap <= MAX_GAP_DAYS):
            return None
    return window


def forward_outcomes(
    full_dataset: CompanyDataset, as_of: date, anchor: date | None = None
) -> dict[str, float | None]:
    periods = full_dataset.sorted_periods()
    out: dict[str, float | None] = {
        "op_margin_chg_4q": None,
        "fcf_margin_chg_4q": None,
        "ni_growth_fwd_4q": None,
    }
    if anchor is not None:
        # Exact match to the signal's PIT anchor; a miss means the full
        # dataset disagrees with the scored slice — refuse rather than guess.
        idx = next((i for i, p in enumerate(periods) if p.period_end == anchor), -1)
    else:
        # index of last period ended on/before as_of (pre-anchor behavior)
        idx = -1
        for i, p in enumerate(periods):
            if p.period_end <= as_of:
                idx = i
    if idx < 0:
        return out

    span = _quarter_window(periods, idx, idx + 5)
    if span is not None:
        m0, m1 = _op_margin(span[0]), _op_margin(span[4])
        if m0 is not None and m1 is not None:
            out["op_margin_chg_4q"] = m1 - m0

    full_span = _quarter_window(periods, idx - 3, idx + 5)
    if full_span is not None:
        trailing, forward = full_span[:4], full_span[4:]
        t_margins = [_fcf_margin(p) for p in trailing]
        f_margins = [_fcf_margin(p) for p in forward]
        if all(v is not None for v in t_margins + f_margins):
            out["fcf_margin_chg_4q"] = sum(f_margins) / 4 - sum(t_margins) / 4  # type: ignore[arg-type]

        t_ni = [p.net_income for p in trailing]
        f_ni = [p.net_income for p in forward]
        if all(v is not None for v in t_ni + f_ni):
            base = sum(t_ni)  # type: ignore[arg-type]
            if abs(base) > 0:
                out["ni_growth_fwd_4q"] = (sum(f_ni) - base) / abs(base)  # type: ignore[arg-type]
    return out
