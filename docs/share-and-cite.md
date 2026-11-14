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
quoting it. Verify before sharing with `pageledger verify DIR`, for a run or a job.

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
run directory whole: `verify` checks every artifact its manifest declares,
including `config-snapshot.yml`, `route-map.yml`, `raw/`, `normalized/`,
`audit.json`, `audit.md`, `provenance.jsonl`, `quality.jsonl`, `cost.json`,
`run.log` and `rerun-manifest.yml`, and fails when one is missing (an empty
directory may be left out). Deposit a
job directory whole as well: `job.json`, its attempts, selected outputs and
review receipts. Include `report.md` or `transcript.md`
when it helps readers, and include an export when a downstream corpus needs
plain text or TEI. Keep verification output and PageLedger version with the
deposit. Do not deposit credentials or unrelated private files.

## Check paths before sharing

Run manifests record absolute paths in `config.source_paths`, and
`route-map.yml` records them in `documents[].source`. A job's `job.json`
records both the job's `root` and the source's `path` as absolute paths, and
`relocations` records the directory of any copy that was reviewed. These
can expose account names and folder structure. Remove or redact
such identifying path evidence only in a clearly labeled sharing copy; doing
so changes the artifact and it will no longer verify against its original
hashes. Preserve the unmodified original privately when your retention policy
allows it.

New 0.6 reports and exports use relative source links and do not include
absolute local paths. Older reports may contain absolute paths. Check all
retained files before deposit, including logs, custom adapter output, and
screenshots. Document job directories cannot be bundled as run replay bundles.

## Deposit a job

A job directory verifies wherever it is. Its `job.json` records the directory
it was written in as a fact, like the source's path; verification checks the
files themselves.

1. Verify the job where you wrote it: `pageledger verify jobs/book --json > verification.json`.
2. Copy or archive the whole directory, hidden `.job/` and `attempts/*/.checkpoint/`
   included (for example `tar -czf book-job.tar.gz -C jobs book`). Empty
   directories may be left out; Git and some archive tools drop them.
3. Record `shasum -a 256 jobs/book/job.json` and the archive's hash with the deposit,
   alongside `verification.json` and the PageLedger version.

`verify` finds any inconsistency inside the job, a byte changed, removed or
added anywhere, but not a consistent rewrite: someone who changes `job.json` and
then rebuilds the report and hashes with PageLedger's own code leaves a job that
verifies. Nothing outside the job anchors it. Deposit it in a repository that
timestamps deposits or assigns a DOI, or publish the hash of `job.json`, so a
reader can check the copy they have against what you published.

A reader who unpacks the deposit anywhere runs `pageledger verify book`.
It passes with warning `job_relocated`, naming the directory the job was
written in and the one it is in now, and with `source_missing` when the reader
does not have the source. A reader who has the source can list it in a
[source index](checkpoint-spec.md#moved-source-files); verification then
checks its bytes and reports `source_relocated`. A file at the recorded source
path with other bytes fails verification. `inspect`, `export` and
`review --sheet` work on the copy as they do on the original.

A colleague can review the copy with `review --apply`, or settle contested spans
with `adjudicate --decisions`, with or without the source; without it, each
command gives the same `source_missing` warning as `verify`. The job records
each change made away from its original directory under `relocations` in
`job.json`, and the report says so; `verify` reports it as
`job_changed_elsewhere`. Such a review changes `job.json`, so cite the new hash.
`resume` works only in the directory the job was written in, so that two
copies cannot both pay for the same pages: move the job back there to resume it.

## Jobs, runs and bundles you receive

A job, run or bundle folder is plain files, and anyone who sends you one could
have edited them. Looking at such a folder runs none of its code: `verify`,
`inspect`, `export`, `review` and `compare` read its files and run no engine.
Continuing one is different, because the folder's configuration says what to
run: `resume` on a job or run, and `replay` on a bundle, start its engines
again.

- **Code it names.** A configuration may name its own adapter as Python code
  (`module:object`). PageLedger's built-in engines load as always. Any other
  code runs only from a folder you name with `--adapter-path`, never from the
  received folder itself, and the object must be an adapter class defined in
  that folder; a plain function is never called. For an adapter installed with
  pip, give its own package folder inside site-packages; site-packages itself
  is refused, since any installed code could then be named. Give
  `--adapter-path` only if you trust where the job came from and the code in
  that folder: an adapter is ordinary Python and can do anything you can.
- **Servers it names.** A configuration may send page images, and the API key
  of an environment variable it names, to a model server. On `resume` or
  `replay`, a server that is not on this machine is used only if you name its
  address with `--trust-endpoint URL`. The refusal gives the exact option to
  pass. A server on this machine (`localhost`) needs no naming.

So: do not resume a job, or replay a bundle, that someone sent you unless you
trust where it came from. `verify` passing does not make it trustworthy; it
says only that the files agree with each other. When an unfinished job names
its own code, `verify` says so. Both refusals come before anything changes, so
a refused job can be resumed later with the right options.

Your own `process --config`, `run --config` and `rerun --config` are trusted
as written, since you chose them. To report a way around these rules, see
[SECURITY.md](https://github.com/peterbussch/pageledger/blob/main/SECURITY.md).

## Cite PageLedger as software

Cite the PageLedger release used, with the project's recommended software
citation in [`CITATION.cff`](../CITATION.cff). Cite the source collection
separately. A software citation does not replace the run/job identifiers and
checksums needed to identify a particular result.
