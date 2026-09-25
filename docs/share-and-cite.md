# Share and cite PageLedger results

A reproducible citation identifies the source, the PageLedger artifact, and the
specific text being discussed. A successful verification establishes that
retained files match recorded hashes; it does not establish transcription
accuracy or publication rights.

## Cite a run or job

For a run, record its `run_id` from `manifest.json`, the manifest SHA-256, the
PageLedger version, and the source filename and SHA-256. A rerun also has a
`parent_run_id`; cite both generations when discussing changed output. To
calculate a file hash on macOS or Linux, use `shasum -a 256 manifest.json` or
`sha256sum manifest.json` respectively. Preserve the original manifest bytes.

For a document job, record `job_id`, source SHA-256, and the SHA-256 of
`job.json`. Cite the PageLedger version and the relevant page number and
attempt ID. A job transcript has its own file hash and identifies the job and
selected page outputs. Cite the transcript hash as well as the job ID when
quoting it. Verify before sharing with `pageledger verify-run RUN_DIR` or
`pageledger verify-job JOB_DIR`.

For an exported text file, cite its hash, job ID, source SHA-256, page number,
attempt ID, and output SHA-256. The export includes page-level review state and
output hashes; consult [Export document text](export.md) for formats and
selection rules.

Suggested citation template:

> PageLedger [version], [run ID or job ID], source [filename], SHA-256
> [source hash], artifact [filename], SHA-256 [artifact hash], page [number],
> attempt [attempt ID], output SHA-256 [output hash]. Accessed [date].

Use only identifiers that exist in the artifact. Omit fields that do not apply.

## What to deposit

Deposit the source or a stable, permissioned source reference; its checksum;
the configuration snapshot; and the complete run or job directory. Deposit a
run directory whole: `verify-run` checks every artifact its manifest declares,
including `config-snapshot.yml`, `route-map.yml`, `raw/`, `normalized/`,
`audit.json`, `audit.md`, `provenance.jsonl`, `quality.jsonl`, `cost.json`,
`run.log` and `rerun-manifest.yml`, and fails when one is missing. Deposit a
job directory whole as well: `job.json`, its attempts, selected outputs and
review receipts. Include `report.md` or `transcript.md`
when it helps readers, and include an export when a downstream corpus needs
plain text or TEI. Keep verification output and PageLedger version with the
deposit. Do not deposit credentials or unrelated private files.

## Check paths before sharing

Run manifests record absolute paths in `config.source_paths`, and
`route-map.yml` records them in `documents[].source`. A job's `job.json`
records both the job's `root` and the source's `path` as absolute paths. These
can expose account names and folder structure. Remove or redact
such identifying path evidence only in a clearly labeled sharing copy; doing
so changes the artifact and it will no longer verify against its original
hashes. Preserve the unmodified original privately when your retention policy
allows it.

New 0.6 reports and exports use relative source links and do not include
absolute local paths. Older reports may contain absolute paths. Check all
retained files before deposit, including logs, custom adapter output, and
screenshots. Document job directories cannot be bundled as run replay bundles;
deposit a verified copy of the job tree and its hashes instead.

## Cite PageLedger as software

Cite the PageLedger release used, with the project's recommended software
citation in [`CITATION.cff`](../CITATION.cff). Cite the source collection
separately. A software citation does not replace the run/job identifiers and
checksums needed to identify a particular result.
