"""Phase 4 adversarial test: the adjudicator must call obvious cosmetic changes
NOT material and obvious material changes material. Uses recorded blind judgments
(from the definitions alone) as the completion function — verifying both the
pipeline and that a correct judge distinguishes the two."""

from app.services.narrative.kpi_adjudicator import LlmAdjudicator
from app.services.narrative.kpi_extraction import KpiDefinitionPair

# (kpi, prior, current, expected_material, judgment)
COSMETIC = [
    ("Adjusted EBITDA",
     "Adjusted EBITDA is defined as net income before interest, taxes, depreciation and amortization.",
     "Adjusted EBITDA is defined as earnings before interest, taxes, depreciation and amortization.",
     False, {"materiality": "no_material_change", "direction": "neutral", "changed_clause": "",
             "explanation": "Rewording only; same components (interest, taxes, depreciation, amortization)."}),
    ("Free cash flow",
     "Free cash flow is defined as cash from operations less capital expenditures.",
     "Free cash flow is defined as operating cash flow minus capital expenditures.",
     False, {"materiality": "no_material_change", "direction": "neutral", "changed_clause": "",
             "explanation": "Synonyms only; identical calculation."}),
    ("Gross margin",
     "Gross margin is revenue less cost of goods sold, divided by revenue.",
     "Gross margin equals revenue minus cost of goods sold, as a percentage of revenue.",
     False, {"materiality": "no_material_change", "direction": "neutral", "changed_clause": "",
             "explanation": "Same formula reworded."}),
]

MATERIAL = [
    ("Adjusted EBITDA",
     "Adjusted EBITDA is net income before interest, taxes, depreciation and amortization.",
     "Adjusted EBITDA is net income before interest, taxes, depreciation, amortization, "
     "restructuring charges, litigation costs, and stock-based compensation.",
     True, {"materiality": "broadened_exclusions", "direction": "more_flattering",
            "changed_clause": "restructuring charges, litigation costs, and stock-based compensation",
            "explanation": "Now also excludes restructuring, litigation, and stock-based compensation."}),
    ("Adjusted EPS",
     "Adjusted EPS excludes amortization of intangibles.",
     "Adjusted EPS excludes amortization of intangibles and all stock-based compensation.",
     True, {"materiality": "broadened_exclusions", "direction": "more_flattering",
            "changed_clause": "and all stock-based compensation",
            "explanation": "Broadened to also exclude stock-based compensation."}),
    ("Billings",
     "Billings is revenue plus the change in deferred revenue.",
     "Billings is revenue plus the change in deferred revenue and change in unbilled receivables.",
     True, {"materiality": "changed_basis", "direction": "more_flattering",
            "changed_clause": "and change in unbilled receivables",
            "explanation": "Basis expanded to add change in unbilled receivables."}),
]


def _completion_table():
    table = {}
    for kpi, prior, cur, _, j in COSMETIC + MATERIAL:
        table[(kpi, cur)] = j
    def complete(system, user):  # noqa: ARG001
        import re
        km = re.search(r"^Metric: (.+)$", user, re.MULTILINE)
        cm = re.search(r'Current definition \([^)]*\): "(.*)"\s*$', user, re.DOTALL)
        if not km or not cm:
            return None
        return table.get((km.group(1).strip(), cm.group(1)))
    return complete


def test_adversarial_cosmetic_and_material(tmp_path):
    adj = LlmAdjudicator(complete=_completion_table(), model_id="adv", cache_dir=tmp_path)
    for i, (kpi, prior, cur, expected_material, _) in enumerate(COSMETIC + MATERIAL):
        pair = KpiDefinitionPair(
            evidence_id=f"KDP-{i:03d}", kpi=kpi, change_type="redefinition",
            prior_period="Q1", prior_definition=prior, current_period="Q2",
            current_definition=cur, prefilter_similarity=0.5,
        )
        result = adj.adjudicate(pair)
        assert result.is_material == expected_material, f"{kpi}: got {result.materiality}"


def test_cosmetic_all_suppressed(tmp_path):
    adj = LlmAdjudicator(complete=_completion_table(), model_id="adv", cache_dir=tmp_path)
    for i, (kpi, prior, cur, _, _) in enumerate(COSMETIC):
        pair = KpiDefinitionPair(
            evidence_id=f"C-{i}", kpi=kpi, change_type="redefinition", prior_period="Q1",
            prior_definition=prior, current_period="Q2", current_definition=cur, prefilter_similarity=0.5)
        assert not adj.adjudicate(pair).is_material
