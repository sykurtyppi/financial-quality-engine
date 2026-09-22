"""Engine invariants over generated period datasets.

The datasets span missing fields, zeros, and losses with cash burn — the
distress states where the scorer's not-meaningful guards and the P0-9
distress branch take over, and where hand-written fixtures are thinnest.

- Totality: `analyze` returns for any finite dataset.
- Flags (PR 0.3): every weighted component at red concern whose metric
  exists carries exactly one red flag, value or no value.
- Vocabulary (journal): every name `can_lock` admits as an engine metric is
  one the resolver can evaluate — never "unknown metric", so nothing an
  operator may lock can seal and then be unresolvable.
"""

from __future__ import annotations

from hypothesis import given, settings

from app.core.pipeline import RED_FLAG_CONCERN, analyze
from app.schemas.metrics import MetricStatus
from app.services.formulas.registry import compute_metrics
from app.services.journal.resolver import _lookup_metric_value
from app.services.metrics_registry import JOURNAL_LOCKABLE
from tests.strategies import period_datasets


@settings(max_examples=40)
@given(dataset=period_datasets())
def test_analyze_is_total_and_every_red_component_is_flagged_once(dataset):
    result = analyze(dataset)
    by_name = {m.name: m for m in result.metrics}
    qualifying: list[str] = []
    for block in result.block_scores:
        for c in block.components:
            if c.weight == 0 or c.concern_score is None or c.concern_score < RED_FLAG_CONCERN:
                continue
            if c.metric_name in by_name and c.metric_name not in qualifying:
                qualifying.append(c.metric_name)
    flagged = [f.evidence_metrics[0] for f in result.red_flags]
    assert len(flagged) == len(set(flagged)), "a metric flagged twice"
    if len(qualifying) <= 10:
        assert sorted(flagged) == sorted(qualifying)
    else:  # the list is capped at ten; every shown flag must still qualify
        assert len(flagged) == 10 and set(flagged) <= set(qualifying)


@settings(max_examples=40)
@given(dataset=period_datasets())
def test_every_lockable_metric_is_one_the_resolver_can_evaluate(dataset):
    bundle = compute_metrics(dataset)
    period = dataset.periods[-1]
    for name in JOURNAL_LOCKABLE:
        value, note, structural = _lookup_metric_value(name, period, bundle)
        assert "unknown metric" not in note, name
        if structural:
            # The one structural outcome allowed: a metric that is not
            # meaningful for these inputs (denominator zero and the like).
            assert MetricStatus.NOT_MEANINGFUL.value in note, (name, note)
