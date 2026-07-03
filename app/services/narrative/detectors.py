"""Deterministic narrative detectors: guidance shift, defensive tone,
risk-factor expansion, and high-severity risk-term emergence.

All lexicon-based with word-boundary matching. These are candidate flaggers:
every output carries verbatim excerpts and an explicit confidence, and the
scoring layer treats them as uncalibrated (the Narrative Drift block was not
testable in the v0.3 backtest).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.financials import DocumentType
from app.services.narrative.baselines import ComparisonSet, PeriodDocs

_SNIPPET_RADIUS = 90


def _sentences_with(text: str, pattern: re.Pattern[str], cap: int = 3) -> list[str]:
    out: list[str] = []
    for m in pattern.finditer(text):
        start = max(0, m.start() - _SNIPPET_RADIUS)
        end = min(len(text), m.end() + _SNIPPET_RADIUS)
        out.append("…" + " ".join(text[start:end].split()) + "…")
        if len(out) >= cap:
            break
    return out


def _count(text: str, terms: tuple[str, ...]) -> int:
    total = 0
    for term in terms:
        total += len(re.findall(rf"\b{re.escape(term)}\b", text, re.IGNORECASE))
    return total


def _density_per_1k(text: str, terms: tuple[str, ...]) -> float | None:
    words = len(text.split())
    if words == 0:
        return None
    return _count(text, terms) / words * 1000


# ---------------------------------------------------------------------------
# Guidance language shift
# ---------------------------------------------------------------------------

GUIDANCE_CONTEXT = ("guidance", "outlook", "we expect", "full-year", "full year", "forecast")
GUIDANCE_POSITIVE = ("raise", "raising", "raised", "increase our", "reaffirm", "reiterating", "reiterate", "maintain")
GUIDANCE_NEGATIVE = ("lower", "lowering", "lowered", "reduce our", "reducing our", "withdraw", "withdrawing", "suspend", "no longer providing")


@dataclass
class GuidanceShift:
    current_stance: int  # positive-word count minus negative-word count
    prior_stance: int | None
    shift: float | None  # prior - current; positive = deterioration
    guidance_discussed: bool
    excerpts: list[str] = field(default_factory=list)


def detect_guidance_shift(cmp: ComparisonSet) -> GuidanceShift:
    cur_text = cmp.current.all_text
    discussed = _count(cur_text, GUIDANCE_CONTEXT) > 0
    cur_stance = _count(cur_text, GUIDANCE_POSITIVE) - _count(cur_text, GUIDANCE_NEGATIVE)
    prior_stance: int | None = None
    if cmp.qoq is not None:
        prior_text = cmp.qoq.all_text
        prior_stance = _count(prior_text, GUIDANCE_POSITIVE) - _count(prior_text, GUIDANCE_NEGATIVE)
    pattern = re.compile(
        "|".join(rf"\b{re.escape(t)}\b" for t in GUIDANCE_NEGATIVE + GUIDANCE_POSITIVE),
        re.IGNORECASE,
    )
    return GuidanceShift(
        current_stance=cur_stance,
        prior_stance=prior_stance,
        shift=(float(prior_stance - cur_stance) if prior_stance is not None else None),
        guidance_discussed=discussed,
        excerpts=_sentences_with(cur_text, pattern),
    )


# ---------------------------------------------------------------------------
# Defensive tone
# ---------------------------------------------------------------------------

DEFENSIVE_TERMS = (
    "challenging",
    "headwinds",
    "macro environment",
    "macroeconomic uncertainty",
    "uncertainty",
    "softness",
    "elongated sales cycles",
    "cautious",
    "difficult",
    "pressure",
    "temporary",
    "transitory",
    "near-term",
    "volatility",
)


@dataclass
class ToneShift:
    current_density: float | None  # defensive terms per 1k words
    baseline_density: float | None  # trailing mean
    change: float | None  # current - baseline; positive = more defensive
    excerpts: list[str] = field(default_factory=list)


def detect_defensive_tone(cmp: ComparisonSet) -> ToneShift:
    cur = _density_per_1k(cmp.current.all_text, DEFENSIVE_TERMS)
    trailing = [
        d for p in cmp.trailing if (d := _density_per_1k(p.all_text, DEFENSIVE_TERMS)) is not None
    ]
    baseline = sum(trailing) / len(trailing) if trailing else None
    pattern = re.compile(
        "|".join(rf"\b{re.escape(t)}\b" for t in DEFENSIVE_TERMS), re.IGNORECASE
    )
    return ToneShift(
        current_density=cur,
        baseline_density=baseline,
        change=(cur - baseline) if (cur is not None and baseline is not None) else None,
        excerpts=_sentences_with(cmp.current.all_text, pattern),
    )


# ---------------------------------------------------------------------------
# Risk factors: expansion + high-severity term emergence
# ---------------------------------------------------------------------------

# Terms whose FIRST APPEARANCE in risk disclosure is itself worth surfacing.
HIGH_SEVERITY_RISK_TERMS = (
    "material weakness",
    "going concern",
    "substantial doubt",
    "restatement",
    "sec investigation",
    "sec inquiry",
    "subpoena",
    "internal control over financial reporting was not effective",
    "delisting",
)


@dataclass
class RiskFactorAnalysis:
    expansion_ratio: float | None  # current words / comparison words (YoY preferred)
    comparison_basis: str | None  # "yoy" or "qoq"
    new_high_severity_terms: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)


def _risk_text(p: PeriodDocs | None) -> str:
    if p is None:
        return ""
    return p.by_type.get(DocumentType.RISK_FACTORS, "")


def detect_risk_factor_changes(cmp: ComparisonSet) -> RiskFactorAnalysis:
    cur_text = _risk_text(cmp.current)
    # Risk factors are fullest in 10-Ks: prefer YoY (10-K vs prior 10-K).
    basis: str | None = None
    prior_text = ""
    if _risk_text(cmp.yoy):
        prior_text, basis = _risk_text(cmp.yoy), "yoy"
    elif _risk_text(cmp.qoq):
        prior_text, basis = _risk_text(cmp.qoq), "qoq"

    expansion = None
    if cur_text and prior_text:
        prior_words = len(prior_text.split())
        if prior_words > 0:
            expansion = len(cur_text.split()) / prior_words

    # High-severity emergence checks ALL current text against ALL trailing text
    # (a going-concern mention in MD&A matters as much as in risk factors).
    all_cur = cmp.current.all_text.lower()
    all_prior = "\n".join(p.all_text for p in cmp.trailing).lower()
    new_terms = [
        t for t in HIGH_SEVERITY_RISK_TERMS if t in all_cur and t not in all_prior
    ]
    excerpts: list[str] = []
    for t in new_terms:
        pattern = re.compile(re.escape(t), re.IGNORECASE)
        excerpts.extend(_sentences_with(cmp.current.all_text, pattern, cap=1))
    return RiskFactorAnalysis(
        expansion_ratio=expansion,
        comparison_basis=basis,
        new_high_severity_terms=new_terms,
        excerpts=excerpts,
    )
