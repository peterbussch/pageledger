# Real-document corpus

PageLedger is tested on real historical documents, not only synthetic files.
`manifest.public.yml` lists the rights-cleared ones: Internet Archive and
similar public-domain scans, each with its source URL, SHA-256 and the pages
to run. Library-held documents go in `manifest.local.yml`, which has the same
shape and is git-ignored.

## Tiers

- **S**, the failure-mode suite: a few pages from each kind of document that
  has broken PageLedger before (hollow text layers, oversized and undersized
  pages, damaged files).
- **M**, the release evaluation pool: a few hundred pages across the kinds of
  pages in the collection (prose, tables, directories, pre-reform print).
- **L**, endurance: whole volumes, for memory, scale and resume.

## Running a tier

```bash
python scripts/corpus/run.py --manifest corpus/manifest.public.yml --tier S \
  --profile scripts/corpus/profiles/local.yml --corpus-root /Volumes/Kinodrive
```

The runner checks each source against its SHA-256 before running it, runs one
`pageledger process` job per document, verifies each job, and writes
`results.jsonl` (one line per page), `summary.md` and `run.json` to
`~/pageledger-corpus-runs/<time>-<tier>-<profile>/`. Repeat `--manifest` to
add the local manifest; `--only ID ...` runs selected documents. To measure a
baseline, point `--pageledger` at another installed version, for example
`--pageledger "/path/to/venv-0.5.2/bin/pageledger"`.

`scripts/corpus/compare.py RUN_A RUN_B` lists the pages whose text,
disposition or review reasons changed between two runs, with the engines each
used. `scripts/corpus/sweep.py --root DIR --out FILE` checks that every PDF
under a folder opens and yields text, one child process per file.

## Rights

Public items and results from them may be published. Local items, and all page
text extracted from them, stay local.
