# Contributing to PageLedger

## Corpus and script reports (most wanted)

PageLedger's quality signals are heuristics, and real collections are how
they improve — the 0.1.7 Unicode fixes exist because clean Indic-script
prose was being flagged as OCR noise. If PageLedger misjudges your
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
pip install -e ".[dev,pdf]"
python -m pytest tests/pageledger/ -q
ruff check pageledger/ tests/ examples/
```

[AGENTS.md](AGENTS.md) is the full operating guide: repository
orientation, test gotchas, and the constraints changes must respect
(dependency-light core, adapters stay thin wrappers, record uncertainty
rather than silently fixing it, docs never describe unimplemented
behavior as current).
