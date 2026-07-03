"""Offline sanity tests for the restatement forensic control. Network path is
validated live via scripts/run_restatement_control.py."""

from app.services.backtesting.restatement_control import (
    ACCOUNTING_BLOCKS,
    ACCOUNTING_CONCERN,
    CASES,
    CaseResult,
    HorizonScore,
    RestatementCase,
)


class TestCaseSet:
    def test_accounting_blocks_are_the_forensic_ones(self):
        # The forensic claim rests on these two blocks specifically.
        assert "Earnings Quality" in ACCOUNTING_BLOCKS
        assert "Revenue Quality" in ACCOUNTING_BLOCKS

    def test_has_pure_forensic_cases(self):
        pure = [c for c in CASES if c.healthy_at_time]
        assert len(pure) >= 4  # the real test needs healthy-at-time misstaters

    def test_includes_channel_stuffing_benchmark(self):
        # MiMedx is the textbook receivables-vs-revenue case Revenue Quality targets.
        assert any(c.name == "MiMedx" and c.healthy_at_time for c in CASES)


class TestAccountingDrivenLogic:
    def _case(self, blocks: dict[str, float]) -> CaseResult:
        rc = RestatementCase("X", 1, True, "")
        h = HorizonScore(6, None, "ok", overall=50.0, blocks=blocks)  # type: ignore[arg-type]
        return CaseResult(rc, None, 100, False, [h])

    def test_accounting_driven_true_when_accounting_block_high(self):
        r = self._case({"Revenue Quality": ACCOUNTING_CONCERN + 5, "Cash Conversion": 40})
        assert r.accounting_driven is True

    def test_accounting_driven_false_when_only_distress_high(self):
        r = self._case({"Revenue Quality": 30, "Cash Conversion": 85})
        assert r.accounting_driven is False

    def test_best_accounting_block_ignores_distress_blocks(self):
        r = self._case({"Earnings Quality": 55, "Balance Sheet Stress": 90})
        assert r.best_accounting_block == 55
