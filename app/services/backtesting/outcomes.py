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
"""

from __future__ import annotations

from datetime import date

from app.schemas.financials import CompanyDataset, PeriodFinancials


def _op_margin(p: PeriodFinancials) -> float | None:
    if p.operating_income is None or p.revenue is None or p.revenue <= 0:
        return None
    return p.operating_income / p.revenue


def _fcf_margin(p: PeriodFinancials) -> float | None:
    if p.cfo is None or p.capex is None or p.revenue is None or p.revenue <= 0:
        return None
    return (p.cfo - p.capex) / p.revenue


def forward_outcomes(full_dataset: CompanyDataset, as_of: date) -> dict[str, float | None]:
    periods = full_dataset.sorted_periods()
    # index of last period reported on/before as_of
    idx = -1
    for i, p in enumerate(periods):
        if p.period_end <= as_of:
            idx = i
    out: dict[str, float | None] = {
        "op_margin_chg_4q": None,
        "fcf_margin_chg_4q": None,
        "ni_growth_fwd_4q": None,
    }
    if idx < 0:
        return out

    if idx + 4 < len(periods):
        m0, m1 = _op_margin(periods[idx]), _op_margin(periods[idx + 4])
        if m0 is not None and m1 is not None:
            out["op_margin_chg_4q"] = m1 - m0

    trailing = periods[max(0, idx - 3) : idx + 1]
    forward = periods[idx + 1 : idx + 5]
    if len(trailing) == 4 and len(forward) == 4:
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
