# Draft corpus cases — NOT evidence

Each directory here is a real-filer case built offline from a committed
trimmed fixture (`tests/fixtures/real/`) with
`scripts/make_corpus_case.py --from-facts ... --corpus tests/corpus_drafts`.
Its `case.json` expectations are a copy of what the engine observed and
`reviewed` is null: nobody has checked them against a filing yet.

The corpus gate (`tests/integration/test_corpus.py`) reads `tests/corpus/`
only, so a draft can never count as validation. What is tested here
(`tests/integration/test_corpus_drafts.py`) is only that each draft is still
unreviewed and that the engine still observes what the draft recorded, so a
change to the engine that alters a draft is seen before anyone reviews it.

To pin one: work through its `REVIEW.md` (each check names the accession to
read), correct `expected` from the filings wherever the engine is wrong, fill
in `reviewed`, and move the directory to `tests/corpus/` (docs/corpus.md).

These fixtures carry no filing index, so none of these cases can exercise an
8-K Item 4.02, a same-day filing conflict, or a discontinued-operations
re-presentation; a 10-K/A or 10-Q/A that moves a scored figure is not in
them either. Those cases need a fresh EDGAR pull.
