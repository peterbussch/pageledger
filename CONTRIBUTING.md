# Contributing to PageLedger

## Corpus and script reports

Reports from real collections help improve PageLedger's quality heuristics.
The 0.1.7 Unicode fixes followed reports of clean Indic-script prose being
flagged as OCR noise. If PageLedger misjudges your
collection, [open a corpus
report](https://github.com/peterbussch/pageledger/issues/new?template=corpus-report.yml)
with:

- the script/language, and the time period if the material is historical
- the adapter and engine (`pageledger doctor` output helps)
- the page count, and what PageLedger reported vs. what you expected
- a few lines of redacted raw text plus the `quality.jsonl` line(s) they
  produced

## Bugs and adapter requests

Use the [bug report or adapter request
forms](https://github.com/peterbussch/pageledger/issues/new/choose). For
bugs, include `pageledger --version`, OS/Python, and the exact command.

## Development

```bash
uv sync --frozen --extra dev --extra pdf
uv run --frozen --extra dev --extra pdf python -m pytest tests/pageledger/ -q
uv run --frozen --extra dev --extra pdf ruff check pageledger/ tests/ examples/ scripts/
uv run --frozen --extra dev --extra pdf ruff format --check pageledger/ tests/ examples/ scripts/
uv run --frozen --extra dev --extra pdf mypy pageledger/
```

[AGENTS.md](AGENTS.md) covers repository structure, test requirements and
implementation constraints. See [the release procedure](docs/releasing.md)
for package checks and publication.

Use `ruff format` with the same paths to format changes. CI uses the version
in `uv.lock` so local and automated checks agree.

Keep each change easy to review. Prefer direct control flow, descriptive names,
and helpers with one clear responsibility. Validate external data before using
it; report uncertainty rather than substituting a guessed value. Add regression
tests for changed behavior, and keep artifact schemas and their specs together.
Core depends only on PyYAML; extraction engines belong in adapters.
