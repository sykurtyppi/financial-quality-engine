"""The valuation shadow card: an optional, non-scoring plane ABOVE the engine.

The engine scores accounting quality, distress, dilution and disclosure. It
does not answer the separate investment question — what expectations are
embedded in the current price, and are they reasonable? — and the external
review of 02c2aac (Hermes) recommended keeping the engine as the evidence
layer and adding a valuation and expectations plane above it, never putting
a multiple into a score. This package is phase 1 of that: one operator-
supplied price observation (`observation`), the filing facts available at
that moment and an enterprise-value bridge over them (`bridge`), multiples
where meaningful with the reason where not (`multiples`), the expectations
the price implies under explicit assumptions (`expectations`), rendered as
an appendix section (`render`) and ledgered on `Plane.VALUATION`.

Three data classes stay visibly separate on every line: filing-derived
facts, market observations and model assumptions. Nothing here reads a
score, writes into the analysis result or changes the decision card
(tests/unit/test_valuation_report.py pins that byte for byte).
"""
