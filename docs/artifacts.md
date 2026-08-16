# Run artifacts

A run is a directory of plain files. No service, no database; everything
is inspectable with `cat`, `grep`, and `jq`. JSON/JSONL artifacts validate
against schemas in [`schemas/`](../schemas/); YAML artifacts use documented
field contracts, also tested in CI.

```text
runs/run-001/
├── manifest.json        # canonical run record
├── config-snapshot.yml  # the exact config that produced the run
├── route-map.yml        # which page went where, and why
├── raw/
│   └── doc_0001_page_0002.txt
├── normalized/          # schema-aligned records, one JSON file per structured page
│   └── doc_0001_page_0002.json
├── audit.json           # review + quarantine queues
├── audit.md             # human rendering of audit.json
├── provenance.jsonl     # one line per extracted page
├── quality.jsonl        # one line of diagnostics per page
├── cost.json            # usage and cost rollup, with cost_basis
├── run.log              # JSONL, one line per extractor call
└── rerun-manifest.yml   # executable plan for re-extracting flagged pages
```

`pageledger classify --out route-map.yml` produces a reviewable pair before
a run directory exists:

```text
route-map.yml                 # complete classifier-produced routing plan
route-map.evidence.jsonl      # one text-free signal/decision record per page
```

The evidence filename always uses `<out-stem>.evidence.jsonl`. Passing the
route map to `pageledger run --routes route-map.yml` executes the classified
plan and copies its decisions into the run artifacts.

The point is not only the extracted text but the evidence around it: which
pages were skipped, which engine ran with which options, what it cost and
on what basis, which pages failed or need review, and what should be rerun.

## What each file answers

`manifest.json` answers what ran. Run id, status, inputs with checksums and
page counts (plus the `--pages` selection when one was used), the config
snapshot's checksum, extractor identities (engine, version, options), and
summary counts. Runs configured with `run.adapter_order` also record an
optional `escalation` block with the chain's adapter names and this
generation's zero-based `step`. One chain entry is the effective adapter for
the whole generation. This is the canonical artifact; start here.
Spec: [`run-manifest-spec.md`](run-manifest-spec.md).

`config-snapshot.yml` records what you asked for. It is a verbatim copy of the
config, including one synthesized by `run --adapter`. Reproducing the run
starts from this file.

`route-map.yml` records where each page went. It contains page ids, source
hashes, types, actions, prompts, reasons, and classifier confidence. Built-in
run routing uses the configured default action and null confidence;
`pageledger classify` emits structural decisions with real, deliberately
uncalibrated confidence where the rule has one. `run --routes` executes and
preserves reviewed decisions from the built-in classifier, a custom classifier
hook, a human, or another external classifier. Extracted pages copy the value
to `provenance.jsonl` as `route.route_confidence`.
Spec: [`route-map-spec.md`](route-map-spec.md).

`<out-stem>.evidence.jsonl` explains a classifier-produced route map without
retaining page text. Each line joins the stable page identity to:

- `signals`: structural counts and ratios used by the decision;
- `probe`: adapter, adapter version, model, and result format; and
- `decision`: type, confidence, reason, action, and prompt.

The route map remains the executable artifact; the sibling evidence file is
the diagnostic sidecar. Evidence lines validate against
[`schemas/classify-evidence-line.schema.json`](../schemas/classify-evidence-line.schema.json).

`raw/` holds extractor output, one file per page, named by page id. Page ids
carry the source page number (`doc_0001_page_0081` is page 81 of the first
document), including under `--pages` selection and across reruns.

`provenance.jsonl` records what produced each page: source path and checksum,
engine and version, prompt hash, measured extraction seconds, usage, and
the raw artifact path. Months later,
`grep doc_0001_page_0042 runs/run-001/provenance.jsonl` answers what made
that page and how long it took.
Spec: [`provenance-spec.md`](provenance-spec.md).

`quality.jsonl` records how each page looks: character and word counts,
engine-reported confidence with per-word statistics, lexical shape,
script/orthography evidence, conservative LLM-output integrity evidence,
a warning list (`empty_text`, `low_confidence`, `instruction_echo`,
`output_inflation`, and others), and a grade
(`A`–`F` with `grade_basis` and per-axis detail). These are diagnostics
for a human, not accuracy scores; a grade is a deterministic summary of
this evidence, comparable only when the effective extractor identity matches.
Spec: [`provenance-spec.md`](provenance-spec.md) (companion section).

`normalized/` holds what the schema aligner extracted, one
`{page_id}.json` per structured page: records keyed by declared columns,
matched/missing/extra headers, coercion errors with the raw strings,
structural-loss issues, and arithmetic-check results. Written during
`pageledger run` when the config
has a `schema` section, and rewritten by `pageledger align`. Plain-text
pages produce no normalized file.
Spec: [`normalized-spec.md`](normalized-spec.md).

`cost.json` records what it cost and how we know. It has usage totals plus
`cost_basis`: `adapter_reported` (the provider said so), `configured_rate`
(derived from your configured prices), `mixed`, or `none` (cost is unknown or
unreported; this can be a free local engine or an adapter that omitted cost
evidence). PageLedger refuses to invent dollars.

Three optional 0.1-compatible fields add operational detail:

- `alerts` records the first warning-threshold crossing per unit (`pages`,
  `tokens`, or `usd`), including threshold kind, current value, page id, and
  timestamp. Absolute `warn_*` thresholds can alert without a hard cap;
  `warn_at_percent` is relative to a configured cap.
- `by_adapter` groups usage and resolved cost by
  `provenance.extractor.adapter`.
- `by_page_type` groups the same fields by `provenance.route.type`.

Each rollup contains `pages`, `tokens`, `compute_seconds`, `cost_usd`, and
`cost_known`. Both mappings are derived from provenance, so they count
successfully extracted pages only: skipped, review-only, failed, and
not-attempted pages do not create buckets. They are absent when there are no
provenance entries.

`run.log` records what happened in order. It has one JSON line per extractor call
with timestamp, page id, adapter, status, and any error, so partial or
failed runs stay greppable.

`audit.json` and `audit.md` record what needs human eyes. They contain pages queued for review
and pages held out of reruns by `quarantine_if`. Policy reasons use stable
strings such as `rerun_if:grade_below` and
`quarantine_if:missing_required_columns`. `audit.md` is a rendering of
`audit.json`, never a second source of truth. Queue entries preserve route
confidence, including real confidence from `pageledger classify`; null still
means the classifier could not make a confident decision or no classifier
ran.
`verify-run` regenerates the Markdown rendering in memory and fails when
`audit.md` is stale or edited. It also verifies `result.raw_sha256` for current
provenance lines, warning rather than failing solely because an older
schema-0.1 line predates raw-output digests.
Spec: [`audit-spec.md`](audit-spec.md).

`rerun-manifest.yml` records what to do next. It is an executable list of flagged
pages that `pageledger rerun` re-extracts with the config you give it,
preserving page ids and lineage. Each item records `previous_grade`; a
page flagged for more than one reason appears once, reasons joined
(`quality_warning+grade_below_threshold`). Pages in the quarantine queue
do not appear in rerun items, even if they also have review reasons.
For adapter chains, its optional `escalation` block adds the planned
`next_adapter`. If candidates remain after the chain ends, status is
`chain_exhausted`, items are cleared, and the audit review queue remains the
human worklist. The config supplied to `rerun` is authoritative; a mismatch
with the recorded plan produces an escalation warning and the config wins.
Spec: [`rerun-manifest-spec.md`](rerun-manifest-spec.md).

## Re-alignment

`pageledger align <run-dir>` is the one sanctioned mutation of a run
directory: it re-derives `normalized/`, the grade fields in
`quality.jsonl`, the grade-threshold audit entries, and the rerun
manifest from the untouched `raw/` evidence. `--dry-run` previews the same
derived result without changing the run. Applied output is staged before
replacement, individual artifact writes are atomic, the mutation is logged
in `run.log` (`status: aligned`), and `manifest.json` is written last with an
`alignment` block (timestamp, schema source and hash, PageLedger version) as
the commit point. This is crash-honest, not a cross-file transaction. An
external `--schema` file is snapshotted as `align-schema-snapshot.yml` only
when the preview is applied.

## Verification

`pageledger verify-run <run-dir>` checks the relationships among these files:
declared paths, identifiers, hashes, page counts, raw/normalized provenance,
quality totals, audit/rerun references, and cost totals. Internal corruption is
an error; a missing or changed external source is a warning because the ledger
itself remains inspectable. Verification does not judge OCR accuracy and does
not replace the build-time JSON Schema suite.

## Compatibility

Artifact fields follow the compatibility policy in
[`run-manifest-spec.md`](run-manifest-spec.md): additions are backward
compatible within a schema version. PageLedger 0.2.0 therefore retains
`schema_version: "0.1"` for its optional classifier, escalation, alert, and
rollup fields. The schemas in
[`schemas/`](../schemas/) are the machine-readable authority, enforced by
`tests/pageledger/test_schemas.py`. Built wheels install the same contracts
under the environment's `share/pageledger/schemas/` directory.
