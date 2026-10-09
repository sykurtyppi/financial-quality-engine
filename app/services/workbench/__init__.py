"""The workbench: the local web app's way in (r36).

The engine worked but nobody used it: the web UI was the journal's
dogfooding tool, with no ticker input, and its report page built only for
legacy v1 entries that can no longer be created. The workbench is a local,
single-user front end over what the CLI already does — type a ticker, see
the decision card; the watchlist; a ticker's report history; record a price
for the valuation shadow card — and nothing more. It changes no score and no
report text: a run is `reporting.build_report`, the CLI's publish path, and
every page reads the files that path writes (`report_files.read_live`, one
generation at a time).

No FastAPI here: `jobs` runs builds, `views` reads runs, `setup` says what
blocks a first run, `watching` reuses the watch script's add path. The routes
are in `app.web`.
"""
