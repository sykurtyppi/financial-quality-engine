"""Backtest universe: ~75 companies stratified across archetypes.

HONEST LIMITATION (stated everywhere results appear): this universe is drawn
from currently listed tickers, so it is SURVIVORSHIP-BIASED — companies that
collapsed and delisted (including actual accounting frauds) are absent. This
biases measured true-positive rates DOWN and measured false-positive rates UP.
A delisting-inclusive vendor dataset is required to remove this bias.

`stress_cases` are companies that experienced publicly documented ex-post
stress (filing delays, inventory writedowns, near-distress, heavy dilution) —
used as a qualitative validation set, not proof.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseMember:
    ticker: str
    archetype: str
    sector: str
    # Pin the EDGAR entity when the live ticker->CIK registry no longer points
    # at the filer whose history the backtest needs (a holding-company
    # reorganization gives the ticker a brand-new CIK with no past filings).
    cik: int | None = None


UNIVERSE: list[UniverseMember] = [
    # Hypergrowth SaaS
    UniverseMember("SNOW", "hypergrowth_saas", "Software"),
    UniverseMember("DDOG", "hypergrowth_saas", "Software"),
    UniverseMember("CRWD", "hypergrowth_saas", "Software"),
    UniverseMember("NET", "hypergrowth_saas", "Software"),
    UniverseMember("MDB", "hypergrowth_saas", "Software"),
    UniverseMember("ZS", "hypergrowth_saas", "Software"),
    UniverseMember("TWLO", "hypergrowth_saas", "Software"),
    UniverseMember("PLTR", "hypergrowth_saas", "Software"),
    UniverseMember("BILL", "hypergrowth_saas", "Software"),
    UniverseMember("U", "hypergrowth_saas", "Software"),
    # Capex-heavy AI / infrastructure
    UniverseMember("MSFT", "capex_ai_infra", "Software"),
    UniverseMember("META", "capex_ai_infra", "Internet"),
    UniverseMember("GOOGL", "capex_ai_infra", "Internet"),
    UniverseMember("AMZN", "capex_ai_infra", "Internet/Retail"),
    UniverseMember("ORCL", "capex_ai_infra", "Software"),
    UniverseMember("EQIX", "capex_ai_infra", "Data Centers"),
    UniverseMember("DLR", "capex_ai_infra", "Data Centers"),
    UniverseMember("NVDA", "capex_ai_infra", "Semiconductors"),
    # Cyclicals
    UniverseMember("CAT", "cyclical", "Industrials"),
    UniverseMember("DE", "cyclical", "Industrials"),
    UniverseMember("NUE", "cyclical", "Steel"),
    UniverseMember("FCX", "cyclical", "Mining"),
    UniverseMember("GM", "cyclical", "Autos"),
    UniverseMember("F", "cyclical", "Autos"),
    UniverseMember("WHR", "cyclical", "Consumer Durables"),
    UniverseMember("LEN", "cyclical", "Homebuilders"),
    UniverseMember("DOW", "cyclical", "Chemicals"),
    # Serial acquirers
    UniverseMember("DHR", "serial_acquirer", "Life Sciences"),
    UniverseMember("TMO", "serial_acquirer", "Life Sciences"),
    UniverseMember("ROP", "serial_acquirer", "Industrials/Software"),
    UniverseMember("AME", "serial_acquirer", "Industrials"),
    UniverseMember("TDG", "serial_acquirer", "Aerospace"),
    UniverseMember("HEI", "serial_acquirer", "Aerospace"),
    # Energy
    # 2026 holding-company reorganization: the registry maps XOM to ExxonMobil
    # Holdings Corp (CIK 2115436, facts from FY2026 only). The 2011-2026
    # history lives under Exxon Mobil Corporation, CIK 34088.
    UniverseMember("XOM", "energy", "Energy", cik=34088),
    UniverseMember("CVX", "energy", "Energy"),
    UniverseMember("OXY", "energy", "Energy"),
    UniverseMember("DVN", "energy", "Energy"),
    UniverseMember("SLB", "energy", "Energy Services"),
    UniverseMember("HAL", "energy", "Energy Services"),
    # Banks / financials (verify the exclusion path; not scored)
    UniverseMember("JPM", "bank_financial", "Banks"),
    UniverseMember("BAC", "bank_financial", "Banks"),
    UniverseMember("GS", "bank_financial", "Banks"),
    UniverseMember("SCHW", "bank_financial", "Brokers"),
    # Staples / defensive controls
    UniverseMember("KO", "control_staples", "Consumer Staples"),
    UniverseMember("PEP", "control_staples", "Consumer Staples"),
    UniverseMember("PG", "control_staples", "Consumer Staples"),
    UniverseMember("CL", "control_staples", "Consumer Staples"),
    UniverseMember("GIS", "control_staples", "Consumer Staples"),
    # Healthcare
    UniverseMember("JNJ", "control_healthcare", "Pharma"),
    UniverseMember("MRK", "control_healthcare", "Pharma"),
    UniverseMember("LLY", "control_healthcare", "Pharma"),
    UniverseMember("ABBV", "control_healthcare", "Pharma"),
    UniverseMember("CVS", "control_healthcare", "Health Services"),
    # Retail / consumer
    UniverseMember("WMT", "control_retail", "Retail"),
    UniverseMember("TGT", "control_retail", "Retail"),
    UniverseMember("HD", "control_retail", "Retail"),
    UniverseMember("LOW", "control_retail", "Retail"),
    UniverseMember("NKE", "control_retail", "Consumer"),
    UniverseMember("SBUX", "control_retail", "Consumer"),
    UniverseMember("LULU", "control_retail", "Consumer"),
    # Hardware / semis
    UniverseMember("AAPL", "control_tech", "Hardware"),
    UniverseMember("AVGO", "control_tech", "Semiconductors"),
    UniverseMember("AMD", "control_tech", "Semiconductors"),
    UniverseMember("INTC", "control_tech", "Semiconductors"),
    UniverseMember("MU", "control_tech", "Semiconductors"),
    UniverseMember("TXN", "control_tech", "Semiconductors"),
    UniverseMember("QCOM", "control_tech", "Semiconductors"),
    UniverseMember("CRM", "control_tech", "Software"),
    # Known ex-post stress cases (qualitative validation set)
    UniverseMember("SMCI", "stress_case", "Hardware"),       # 2024 10-K delay, auditor resignation
    UniverseMember("PTON", "stress_case", "Consumer"),       # 2021-22 inventory/demand collapse
    UniverseMember("CVNA", "stress_case", "Retail"),         # 2022 near-distress, leverage
    UniverseMember("OPEN", "stress_case", "Real Estate"),    # inventory-heavy model stress
    UniverseMember("BYND", "stress_case", "Consumer"),       # cash burn, inventory writedowns
    UniverseMember("LCID", "stress_case", "Autos"),          # dilution, cash burn
    UniverseMember("W", "stress_case", "Retail"),            # cash burn cycles
]
