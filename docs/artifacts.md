# Run artifacts

A run is a directory of plain files. No service, no database; everything
is inspectable with `cat`, `grep`, and `jq`, and every artifact validates
against a JSON Schema in [`schemas/`](../schemas/).

```text
runs/run-001/
├── manifest.json        # canonical run record
├── config-snapshot.yml  # the exact config that produced the run
├── route-map.yml        # which page went where, and why
├── raw/
│   └── doc_0001_page_0002.txt
├── normalized/          # reserved for schema alignment (empty in 0.1.x)
├── audit.json           # review + quarantine queues
├── audit.md             # human rendering of audit.json
├── provenance.jsonl     # one line per extracted page
├── quality.jsonl        # one line of diagnostics per page
├── cost.json            # usage and cost rollup, with cost_basis
├── run.log              # JSONL, one line per extractor call
└── rerun-manifest.yml   # executable plan for re-extracting flagged pages
```

The point is not only the extracted text but the evidence around it: which
pages were skipped, which engine ran with which options, what it cost and
on what basis, which pages failed or need review, and what should be rerun.

## What each file answers

**manifest.json** — what ran. Run id, status, inputs with checksums and
page counts (plus the `--pages` selection when one was used), the config
snapshot's checksum, extractor identities (engine, version, options), and
summary counts. This is the canonical artifact; start here.
Spec: [`run-manifest-spec.md`](run-manifest-spec.md).

**config-snapshot.yml** — what you asked for. A verbatim copy of the
config, including one synthesized by `run --adapter`. Reproducing the run
starts from this file.

**route-map.yml** — where each page went. Page ids, types, actions, and
reasons. In the alpha every page follows the configured default action;
the route map is where a future classifier would record its decisions.
Spec: [`route-map-spec.md`](route-map-spec.md).

**raw/** — extractor output, one file per page, named by page id. Page ids
carry the source page number (`doc_0001_page_0081` is page 81 of the first
document), including under `--pages` selection and across reruns.

**provenance.jsonl** — what produced each page. Source path and checksum,
engine and version, prompt hash, measured extraction seconds, usage, and
the raw artifact path. Months later,
`grep doc_0001_page_0042 runs/run-001/provenance.jsonl` answers what made
that page and how long it took.
Spec: [`provenance-spec.md`](provenance-spec.md).

**quality.jsonl** — how each page looks. Character and word counts,
engine-reported confidence with per-word statistics, lexical shape,
script/orthography evidence, and a warning list (`empty_text`,
`low_confidence`, `historical_orthography`, and five others). These are
diagnostics for a human, not accuracy scores.
Spec: [`provenance-spec.md`](provenance-spec.md) (companion section).

**cost.json** — what it cost, and how we know. Usage totals plus
`cost_basis`: `adapter_reported` (the provider said so), `configured_rate`
(derived from your configured prices), `mixed`, or `none` (a free local
engine; PageLedger refuses to invent dollars for it).

**run.log** — what happened, in order. One JSON line per extractor call
with timestamp, page id, adapter, status, and any error, so partial or
failed runs stay greppable.

**audit.json / audit.md** — what needs human eyes. Pages queued for review
(with reasons) and quarantined pages. `audit.md` is a rendering of
`audit.json`, never a second source of truth.
Spec: [`audit-spec.md`](audit-spec.md).

**rerun-manifest.yml** — what to do next. An executable list of flagged
pages that `pageledger rerun` re-extracts with the config you give it,
preserving page ids and lineage.
Spec: [`rerun-manifest-spec.md`](rerun-manifest-spec.md).

## Compatibility

Artifact fields follow the compatibility policy in
[`run-manifest-spec.md`](run-manifest-spec.md): additions are backward
compatible within a schema version, and the schemas in
[`schemas/`](../schemas/) are the machine-readable authority, enforced by
`tests/pageledger/test_schemas.py`.
