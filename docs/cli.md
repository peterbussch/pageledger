# CLI reference

Use `process` to manage a job, or `run` for an extraction run with one
adapter. `verify` and `inspect` take either a job or a run. `rerun` extracts
flagged pages into a new run; `resume` recovers pending work in place.
`classify` prepares routes, and `align` revises structured records from
retained output. `pageledger --help` (or `pageledger help`) lists the commands
for reading a document and for setting up; `pageledger help pipelines` lists
the commands for runs and `adjudicate`, and `pageledger help COMMAND` shows a
command's options.

`pageledger --version` prints the installed release. Invalid command-line
syntax returns exit code 2. Runtime errors return 1; commands can also return 1
for a failed result as shown below, and a command interrupted with Ctrl-C
returns 130. Commands with `--json` emit runtime errors
as JSON on stdout and a diagnostic on stderr.

For a complete text-only example, follow [First run](first-run.md).

## Command index

Each command below accepts `-h`/`--help`. Required arguments and options are
shown in the synopsis; optional path flags have no implicit path default,
except that `review` writes its view to `JOB_DIR-review` beside the job. Terms
such as stage, route and disposition are defined in the
[glossary](glossary.md).

| Command | Synopsis and options | Exit code 0 | Exit code 1 |
|---|---|---|---|
| `process` | `process SOURCE [--config FILE | --engine ENGINE --lang LANGS --model-dir DIR] --out DIR [--pages RANGE] [--adapter-path DIR] [--review FILE] [--json]` | Job completed or paused by a limit. | Job halted or failed, or runtime error. |
| `review` | `review JOB_DIR ([--out DIR] [--images all|held|none] | --sheet FILE | --apply FILE [--reviewer NAME] [--dry-run]) [--json]` | View or sheet written, or decisions checked or recorded. | With `--out`, the job does not verify or the folder exists; with `--apply`, the job is halted or failed; or runtime error. |
| `export` | `export JOB_DIR --out FILE [--format txt|md|jsonl|tei] [--reviewed-only]` | Verified export written. | Runtime error or refused export. |
| `verify` | `verify DIR [--json]` | The job or run verifies. | Verification fails, `DIR` is not a job or a run, or runtime error. |
| `inspect` | `inspect DIR [--json | --csv]` | Report or summary written. | Runtime error, `DIR` is not a job or a run, `--csv` on a job, or with `--json`, the job is halted or failed. `--json` and `--csv` are mutually exclusive (usage error 2). |
| `adjudicate` | `adjudicate JOB_DIR (--packets DIR [--term TERM]... | --decisions DIR --reviewer NAME [--dry-run]) [--json]` | Packets written, or decisions checked or recorded. | Refused decisions, a halted job (decisions are still recorded), or runtime error. |
| `init` | `init [--out FILE] [--engine ENGINE] [--lang LANGS] [--model-dir DIR] | init --run [--adapter text|pdf_text|pdf_ocr] [--out FILE]` | Config written or printed. | Runtime error. |
| `fetch` | `fetch URL --out DIR [--pages RANGE] [--delay SECONDS] [--max-image-bytes N] [--resume] [--json]` | Every selected canvas fetched. | Some canvases missing (the snapshot is `incomplete`), or runtime error. |
| `doctor` | `doctor [--config FILE] [--all] [--json]` | Diagnostics reported; findings do not change the exit code. | Runtime error. |
| `run` | `run INPUT... (--config FILE | --adapter text|pdf_text|pdf_ocr) --out DIR [--pages RANGE] [--routes FILE] [--resumable] [--dry-run] [--json] [--log-level LEVEL] [--adapter-path DIR]` | Run completed, including partial results without failed or unattempted pages. | Partial run with failed or unattempted pages, or runtime error. |
| `resume` | `resume RUN_DIR [--adapter-path DIR] [--raise-limit LIMIT=VALUE] [--json]` | Work resumed, paused, or already finalized. | Halted/failed result or runtime error. |
| `rerun` | `rerun PARENT_DIR --config FILE --out DIR [--dry-run] [--json] [--log-level LEVEL] [--adapter-path DIR]` | Rerun completed. | Any partial execution result or runtime error. |
| `compare` | `compare RUN_A RUN_B [--json]` | Comparison written, including when changes are unranked. | Runtime error. |
| `classify` | `classify [INPUT...] --out FILE [--config FILE] [--from-run DIR] [--adapter SPEC] [--adapter-path DIR] [--json]` | Route map written. | Runtime error. |
| `align` | `align RUN_DIR [--schema FILE] [--json] [--dry-run]` | Alignment applied or previewed. | Runtime error. |
| `bundle` | `bundle RUN_DIR --out DIR [--json]` | Bundle written. | Runtime error. |
| `replay` | `replay BUNDLE_DIR --out DIR [--adapter-path DIR] [--json]` | Outcome is `exact` or `evidence_compared`. | `deterministic_mismatch` or runtime error. |

Any command-line syntax error, including a missing required argument or an
unrecognized option, exits 2. `--version` exits 0. Commands that earlier
releases named differently still accept those names; see
[earlier command names](#earlier-command-names).

## process

```bash
pageledger process book.pdf --out jobs/book
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
```

Processes one source document through local text, OCR, and optional image
stages. The source is a PDF, a text file, an image file or a folder of page
images (see [run](#run) for how a folder is read). `--out` is required, and
the output directory must be new.

Reading a PDF needs the `pdf` extra (`pip install "pageledger[pdf]"`); without
it a PDF is refused before the job directory is created, with that command.

Without `--config`, the job runs with exactly the configuration
`init` writes for the same `--engine`, `--lang` and
`--model-dir` options (see [init](#init)) and records it as it
would record that file, so the job verifies, resumes and rebuilds like any
other. No config file is read from the working directory and nothing is
detected from the machine: the engine is `pdf_ocr` with `--lang eng` unless
the options say otherwise, for a PDF and for page images, and `none` for a
text source (any file that reads as UTF-8, such as `.txt` or `.md`). A source
that is none of these, or does not exist, is refused with a message that says
what a source can be: as a usage error (exit 2) when `process` would choose the
engine, and otherwise (exit 1) before the job directory is created; so is an
empty file. One line on stderr says what the job will read and with which OCR
engine and language. `process --help` shows `--lang` but not `--engine` and
`--model-dir`, which still work; `init` documents them. Before the job directory is created, the engine is loaded
and its preflight run; a missing package or tool is an error naming what to
install, and pointing to `pageledger doctor` where the engine's own message
does not. `--engine`, `--lang` and `--model-dir` together with `--config` are
a usage error (exit 2).

With `--config`, use the [processing guide](processing-spec.md) to write the
config, or start from `init`.
Jobs retain checkpoints automatically. Stages use `processing`
profiles; `run.adapter_order` is reserved for explicit rerun generations and
is rejected here. So are `run` budget, pricing, grading, rerun and retry
settings, which a job would otherwise ignore; set limits under
`processing.limits`. A leftover `run.adapter` or `taxonomy` produces a
warning.

| Flag | Effect |
|---|---|
| `--pages "1-10,15"` | Select source pages while preserving original numbering. |
| `--adapter-path DIR` | Add a trusted import directory for custom adapters. |
| `--review FILE` | Apply source-bound human review receipts before extraction. |
| `--engine pdf_ocr\|rapidocr\|indra\|none` | Without `--config`: the local OCR engine, or no OCR stage. |
| `--lang LANGS` | Without `--config`: Tesseract languages for `pdf_ocr`, e.g. `rus+eng`. |
| `--model-dir DIR` | Without `--config`: the recognizer directory `rapidocr` requires, or a model directory for `indra`. |
| `--json` | Print the job result as JSON. |

`--pages` accepts a comma-separated list of pages and ranges, for example
`--pages "1-10,15"`. If the selection ends past the source, PageLedger names
the first page outside it and the source's page count. If `--out` already
exists, inspect or resume the job there, or choose another output folder.

`process` ends with a few lines in plain words, for a finished, paused or
halted job alike: how many pages were read and how many need a person, where
the job is, why pages are held (up to three causes, one line each with its count
of pages, each naming the reading and what is wrong with it, as
[warnings.md](warnings.md#what-is-wrong-with-a-reading) words it), the one
command to run next (`review`, or `resume` for a job that did not finish) and
the `export` command that writes the text out
(`pageledger export JOB --out NAME.md`, NAME being the job folder's name; `review
--apply` and the job's report suggest the same). `--json` prints the job result
instead.

An error `process` meets before the job starts, such as a missing language pack
or tool, is printed as its sentence with the diagnostic code last, in
parentheses: `pageledger: error: Tesseract's orus model is not installed. …
(missing_language_pack)`. With `--json` the error object is unchanged; so are
the exit codes.

While a job runs, `process` and `resume` show on stderr how many pages are
done, how many need a person so far, and what is being read now: one line
rewritten in place on a terminal, and otherwise a plain line now and then once
the job has run for 15 seconds; nothing with `--json`. When most pages read by
Tesseract were held because it was unsure of many words, the end of the output
says the language given may not be the document's and how to try another; if
Tesseract's script detection (its `osd` model) is sure which script two of
those pages are in, it names a language to try, with the command to run: the
same `process` command with that `--lang`, the `--pages`, `--engine`,
`--model-dir`, `--adapter-path` and `--review` it was given, and `--out` with the
language added to the folder's name. A job run with `--config` is told which
setting in the config to change instead, since `--lang` cannot go with it. When pages carry pre-1918
Russian spelling and the language was `rus`, it points to the `orus` recipe.
This advice is printed only, never recorded in the job.

Ctrl-C stops a job cleanly (exit 130): the pages read so far are kept, and the
message gives the `pageledger resume` command that continues it. `run` with
`--resumable`, `fetch` and `review` say the same for their folders.

A job can finish with unresolved pages. `completed` means processing finished,
`paused_budget` means a processing limit was reached (continue with
`resume --raise-limit`), and `halted` records a stop that will not be retried
automatically. `process` and `resume` exit 0 for a paused job, which is an
expected outcome of a configured limit, and 1 for a halted or failed one.

## review

```bash
pageledger review jobs/book --out review/
pageledger review jobs/book --out review/ --images held
pageledger review jobs/book --sheet review.csv
pageledger review jobs/book --apply review.csv --reviewer "Name" --dry-run
pageledger review jobs/book --apply review.csv --reviewer "Name"
pageledger review jobs/book --apply ~/Downloads/review.json --reviewer "Name"
pageledger review jobs/book --apply reviewed-pages.json
```

`review` does one of three things, chosen by at most one of `--out`, `--sheet`
and `--apply`; giving more than one is a usage error (exit 2).

With none of them, or with `--out DIR`, it verifies the job, then writes a new
folder holding
`index.html`, a reading view of every page with its image beside its text, and
the blank `review.csv`, binding and `review-decisions.txt` that `--sheet` writes,
and says so in two lines: where the view is, and that a spreadsheet user fills
in `review.csv` as `review-decisions.txt` explains. Decisions chosen and
texts corrected in the view are downloaded as `review.json` (where the browser
saves downloads), a decisions file
(or, without corrections, as that `review.csv`), and recorded only by `--apply`
with the downloaded file's path.
The folder is `JOB_DIR-review` beside the job unless `--out` names another. If
that folder exists and is a review view written from the job as it is now (the
view records the SHA-256 of the `document.json` it was made from), `review` says
it is up to date and where it is, and exits 0; if the job has changed since
(decisions applied, a resume), it says so and asks for `--out DIR` (exit 1); any
other folder there is refused. So running the same command again is safe,
`--images none` included. `--images all` or `held` asks for a new view, as does
`--out DIR`, and an existing folder is then refused.
`--images` renders a page image for every page (`all`, the default), only the
pages that need a person (`held`), or none (`none`, which needs no Poppler).
See [Review beside the page image](processing-spec.md#review-beside-the-page-image).

`--sheet FILE` writes only the CSV of the job's pages, to fill in with a
spreadsheet, its binding beside it, and `review-decisions.txt`, which lists the
decisions and the readings each page can use. It refuses a sheet or binding that
already exists, and a sheet inside the job's folder. If `review-decisions.txt`
there belongs to another sheet or is someone's own file, the guide is named after
the sheet instead (`r2-decisions.txt` for `r2.csv`), and `review --sheet` prints
its path. The cells a person reads lead with
words, the code or ID last: `disposition` ("Text may be missing or damaged in the
reading (coverage_defect)"), `attempt_summary` (the reading's name, its first
words, its length and its ID) and `review_reasons`. Applying a sheet reads only
`row_id`, `page_number`, `decision` and `note`, so a sheet written by 0.8 applies
as before.

`--apply FILE` records human decisions from that sheet, from a decisions file
(JSON with the sheet's bindings, which the view downloads), or from a JSON file
following the [review receipt contract](document-report-spec.md#human-review-receipt),
without extracting anything and keeping earlier receipts. A sheet or decisions
file may correct a page's text with `correct:FILE` (or, in a decisions file,
`correct` and the text itself); the correction is kept as a new reading and
the machine readings are not changed. Decisions from a sheet or decisions file
need `--reviewer NAME` or the `PAGELEDGER_REVIEWER` variable; `--dry-run` checks
and counts them, and prints how many words each correction changes, without
recording any. See
[Review in a spreadsheet](processing-spec.md#review-in-a-spreadsheet) and
[Correct the text](processing-spec.md#correct-the-text).
After recording, `--apply` says how many pages were accepted, corrected or
marked blank and so on, each correction with the reading it started from by name
("Correction of page 60, from Tesseract OCR (eng): 1 word changed, 0 added, 0
removed"), how many still need a person, and the next command.
`--images` goes only with the review view, and `--reviewer` and `--dry-run` only with
`--apply` (usage error 2 otherwise). All three modes accept `--json`.
`review`, `export` and `adjudicate` given a run folder, and `compare`, `rerun`
and `align` given a job, say which kind of folder it is and which command fits.

## export

```bash
pageledger export jobs/book --out book.txt
pageledger export jobs/book --format jsonl --out book.jsonl --reviewed-only
```

Writes a job's selected text page by page as `txt`, `md`, `jsonl` or `tei`,
with each page's review state and the reading that produced it. Without
`--format`, the format follows the end of `--out`: `.txt`, `.md`, `.jsonl`, and
`.xml` or `.tei` for TEI; any other ending is a usage error (exit 2). A job that does
not verify is refused. `--reviewed-only` keeps only pages with a human review
receipt. `--out` may not be inside the job, the job's source, a folder or a
link, or an existing file that is not an earlier export of the same job; writing
over your own earlier export is allowed. See [Export document text](export.md)
for the formats and for citing an export.

## run

```bash
pageledger run scans/ --config pageledger.yml --out runs/run-001/
pageledger run scan.pdf --adapter pdf_ocr --out runs/run-001/
pageledger run scan.pdf --config pageledger.yml --routes reviewed-routes.yml --out runs/run-002/
```

Extracts every routed page of the inputs into a new run directory. Inputs are
files or directories. Each input directory expands to its direct child files
in name order; subfolders are not searched. Hidden children, whose
names start with `.` (such as macOS `.DS_Store` files and `._*` sidecars), are
skipped and listed as `skipped_inputs` in the run result and manifest. An
explicitly named hidden file is still read. Document numbers follow the sorted
expanded input set, so skipped hidden files do not shift numbering. `--out`
must not already exist.

A folder that holds JPEG or PNG files is different: it is one document of page
images, its pages in natural order of file name (`page-2.jpg` before
`page-10.jpg`). Every other entry in it, a text file, a TIFF or a subfolder,
stops the run before any page is read; hidden files are skipped as above. A
single `.jpg`, `.jpeg` or `.png` file is a one-page document. TIFF and JPEG 2000
are refused with `unsupported_image` and the conversion to make, a damaged
image with `malformed_image`. The `text` adapter refuses page images; `pdf_text`
reads no text from them (`no_text_layer`). See
[a folder of page scans](process-a-collection.md#page-images).

To process a collection as separate jobs, run once per file. This
shell loop uses direct child files and skips hidden names:

```bash
mkdir -p jobs
for source in collection/*; do
  [ -f "$source" ] || continue
  case "$(basename "$source")" in .*) continue ;; esac
  name=$(basename "$source")
  pageledger process "$source" --config processing.yml --out "jobs/$name"
done
```

Exactly one of `--config` or `--adapter` is required:

- `--config pageledger.yml` uses your YAML config.
- `--adapter text|pdf_text|pdf_ocr` runs a built-in adapter with generated
  defaults, no YAML needed. The generated config is recorded in
  `config-snapshot.yml`. PageLedger never reads a config file it was not
  explicitly given, so a stray `pageledger.yml` in the working directory
  has no effect.

Other flags:

| Flag | Effect |
|---|---|
| `--pages "1-8,81,100-110"` | Extract only these source pages (single input). Page ids keep the source numbering, so provenance stays truthful when you sample a large volume. Recorded in `manifest.inputs[].pages`. |
| `--routes FILE` | Execute a complete route map from `pageledger classify`, a human, or an external classifier. Requires `--config`; cannot be combined with `--adapter` or `--pages`. |
| `--dry-run` | Write the route map and planning artifacts without calling extractors. Inspect routing before spending money. |
| `--resumable` | Keep page attempts so an interrupted run can be resumed in place. Needs zero automatic retries and `on_page_error: stop`; cannot be combined with `--dry-run`. See the [checkpoint contract](checkpoint-spec.md). |
| `--json` | Machine-readable result on stdout; errors as JSON too. |
| `--log-level LEVEL` | Minimum `run.log` event level: DEBUG, INFO, WARNING, ERROR. |
| `--adapter-path DIR` | Add a directory to `sys.path` so custom adapters named by `run.adapter` or `run.adapter_order` can be imported. |

An imported route map must cover every page of every supplied input exactly
once, use the configured taxonomy, and contain only actions supported by the
configured adapter. Relative source paths resolve from the route-map directory.
Hashes and page counts are checked when supplied; older maps without them are
accepted with warnings and the current values are recorded.
`--dry-run --routes` preserves the proposed decisions without calling
`extract()`.

Human run summaries read `cost.json`. `Cost USD:
unknown` means no complete dollar total was established; known zero remains
`0.0`, and a known subtotal with unknown pages is explicitly labeled partial.
Adapter-reported totals are labeled as such; configured-rate totals are called
estimates and explicitly distinguished from provider charges; mixed-basis
totals name both evidence sources.
Dry-run output says that no extraction was performed, so its zero is not
presented as a provider charge or projected bill. `--json` result mappings are
unchanged; use `cost.json` for the `cost_known`, `cost_usd`, and
`cost_basis` fields.

## resume

```bash
pageledger run book.pdf --adapter pdf_ocr --resumable --out runs/book/
# After interruption, with the same source files and adapter environment:
pageledger resume runs/book/
pageledger inspect runs/book/
pageledger verify runs/book/
```

Continues an individual run created with `run --resumable`, or a job
created with `process`, in its existing directory. For job recovery
and processing limits, see [document processing](processing-spec.md). For
individual-run recovery rules, see the [checkpoint contract](checkpoint-spec.md).
`--adapter-path DIR` adds a trusted adapter import directory; `--json` emits a
machine-readable result. Resuming runs the adapters the folder's own records
name. A built-in adapter loads as always, but a custom one (`module:object`)
runs only if its module is in a folder given with `--adapter-path`, outside the
job or run, and the object is defined there. From these records only an adapter
class or object (with `supports` and `extract`) is built, never a plain function.
An adapter installed with pip is allowed by giving its own package folder, for
example `.../site-packages/their_reader`; site-packages itself, or a folder above
it, is refused. Otherwise `resume` refuses before anything changes. The same holds
for your own jobs and runs. A model server off this machine that the records name is used
only when given with `--trust-endpoint URL` (one per server; not shown in
`--help`, and named in the refusal); a server on this machine needs nothing.
Give either only for a folder whose origin you trust: see
[jobs, runs and bundles you receive](share-and-cite.md#jobs-runs-and-bundles-you-receive). `--raise-limit LIMIT=VALUE` applies only to a paused
job; see [one budget for the job](processing-spec.md#one-budget-for-the-job).
If a source file has moved, list its new path in a
[source index](checkpoint-spec.md#moved-source-files) (`PAGELEDGER_SOURCE_INDEX`
or `~/.config/pageledger/source-index.tsv`); resume finds it by SHA-256 and
keeps the recorded path in its records. A job resumes only in the
directory it was written in; on a moved or copied job, `resume` stops and
names that directory.

## classify

```bash
pageledger classify scans/ --config pageledger.yml --out route-map.yml
pageledger classify report.pdf --adapter pdf_text --out route-map.yml --json
pageledger classify --from-run runs/run-001/ --config pageledger.yml --out route-map-v2.yml
```

Probes every page and writes an executable route map plus
`<out-stem>.evidence.jsonl`. The built-in rules classify structural page
shape (`blank`, `sparse`, `prose`, `table_likely`, `unknown`); a configured
hook can replace the decision for a domain taxonomy. This is a separate
stage: `run` never invokes it automatically. Review the evidence, edit the
map if needed, then pass it unchanged to `run --routes`.

Exactly one input mode is allowed:

- Positional files/directories run a cheap probe. Probe precedence is
  `--adapter`, then `classify.adapter`, then `pdf_text` for PDFs or `text` for
  other supported inputs.
- `--from-run DIR` reclassifies retained raw evidence without paying for
  extraction again. The parent must be a complete, executed, original run;
  dry runs, reruns, and `--pages` samples are rejected. It cannot be combined
  with positional inputs or `--adapter`.

Page images have no text layer to probe, so `classify` refuses a folder of
them or an image file; read them with an OCR engine first, then classify that
run with `--from-run`.

`classify` has no `--pages` filter: route maps must cover every source page to
remain valid when passed to `run --routes`, which cannot be combined with
`run --pages`.

Flags:

| Flag | Effect |
|---|---|
| `--config FILE` | Optional taxonomy, action mapping, thresholds, probe adapter, hook, and minimum confidence. |
| `--out FILE` | Required route-map YAML path; the evidence sidecar is written beside it. Refused if either file already exists. |
| `--from-run DIR` | Use a parent run's manifest, routes, quality, provenance, and raw artifacts instead of probing inputs. |
| `--adapter SPEC` | Probe adapter name or `module.path:Object`; overrides `classify.adapter`. |
| `--adapter-path DIR` | Add a directory to `sys.path` for custom probe adapters or classifier hooks. |
| `--json` | Print the classification summary as JSON. |

A configured taxonomy must contain every type the active classifier can emit;
this upfront gate is what guarantees the emitted map can round-trip through
`run --routes`. An empty taxonomy is allowed and conservatively routes every
page to review. See [classifier.md](classifier.md).

## rerun

```bash
pageledger rerun runs/run-001/ --config stronger.yml --out runs/run-002/
```

Re-extracts exactly the pages listed in the parent run's
`rerun-manifest.yml` (the pages that were flagged for review), preserving
their page ids and recording parent lineage. A plain `run.adapter` config can
select a stronger engine, or one `run.adapter_order` can define the whole
generation-indexed chain: entry 0 is the original run, entry 1 the first
rerun, and so on. Each entry can carry its own options.

Chain exhaustion with pending pages produces `rerun_status:
chain_exhausted`; those pages stay in human review and no items are marked
executable. `run.max_rerun_depth` is an independent cap and takes precedence
when both limits are reached. The supplied config
is authoritative: if it disagrees with the parent's recorded next adapter,
PageLedger prints an escalation warning and uses the config. Source-integrity
changes fail closed before the child directory is created. The parent ledger
must pass `verify`, and its executable queue must still match the audit,
routes, config, grades, quarantine, and lineage evidence. `rerun` takes the
same `--dry-run`, `--json`, `--log-level`, and `--adapter-path` flags as `run`.
It writes a separate selected-page run and does not choose or assemble a final
corrected corpus for you.

## align

```bash
pageledger align runs/run-001/
pageledger align runs/run-001/ --schema table-v2.yml
pageledger align runs/run-001/ --schema table-v2.yml --dry-run
```

Re-aligns an existing run's structured raw pages against a schema and
regrades every page without re-extracting, so iterating on column
aliases costs nothing even when the extraction was paid OCR or a VLM.
Without `--schema` the run's own `config-snapshot.yml` schema is used;
with it, the file (a bare schema mapping or any config with a `schema:`
section) is snapshotted into the run directory as
`align-schema-snapshot.yml`. Rewrites `normalized/`, the grade fields in
`quality.jsonl`, the grade-threshold audit entries, and the rerun
manifest; records the mutation in `run.log` and a `manifest.json`
`alignment` block. `--json` for machine-readable output.

Human output prints separate `Grades (signals)` and `Grades (schema)`
distributions. JSON retains `grade_distribution` for compatibility and adds
`grade_distribution_by_basis` at the top level and in both `before` and
`after`.

`--dry-run` computes the complete replacement in memory and reports
before/after grades, review-queue size, and normalized-record count without
writing any artifact or schema snapshot. Applied alignment stages every
derived file first and writes `manifest.json` last as the commit indicator;
the multi-file update is deliberately not described as a transaction.

## compare

```bash
pageledger compare runs/run-001/ runs/run-002/
```

Compares shared pages by character and word counts, extraction time, warnings,
grades, adapters, provenance, and cost. Directional totals such as “improved”
and “resolved” are counted only when the stated comparison gates pass.
`--json` reports extraction and grade comparability separately for each shared
page id.
When extractors differ, grades are not ranked. The text report still shows
page lengths and warning changes side by side.

| Is a directional improvement counted? | Rule |
|---|---|
| Extraction and warning changes | Only when source bytes, source page, and effective extractor identity match. Identity includes adapter/version, model, prompt hash, determinism, input/output types, capabilities, and adapter-options hash. |
| Grade changes | Also requires the same PageLedger version, grading policy, grade basis, and—when schema-aware—the same schema identity. |
| Different or unknown identities | Changes are shown but unranked. If no page qualifies, the report says `not assessed`, not zero improvement. |

Grade policy includes merged grade thresholds and the low-confidence floor
from the retained config or external alignment schema.

Adapter option values are not copied into the comparison report. Inputs must
be contained regular files; symbolic links are rejected.

## verify

```bash
pageledger verify jobs/book
pageledger verify runs/run-001/
pageledger verify runs/run-001/ --json
```

Checks a job or a run, whichever `DIR` is. A folder holding
`job.json` is a job, written by `process`; a folder holding `manifest.json` or
`checkpoint.json` is a run, written by `run`, or one stage of a job under its
`attempts/` folder (`verify jobs/book/attempts/local_ocr` checks that stage's
run alone). A folder with neither, or with both, is refused with an error that
names these files (exit 1), as are a file and a missing path. `--json` prints
the report the job or run check produces; the two have different fields.

A pass prints:

```text
Status: pass
The record is intact: every file matches what was recorded. This does not check that the text is right.
```

A failure names the file that does not match what was recorded.

For a job, `verify` checks the source, retained attempts, selections, and
report, in whatever directory the job is now. A job that did not finish (its
`job.json` still says `processing`, because it was interrupted or is still
running) fails with `"reason": "unfinished"` in the JSON result, prints
`Status: fail (unfinished)`, and says to run `resume`; a finished job is checked
in full. A job that halted or failed can still pass, because its record is
intact; `verify` then adds a line saying that the job itself halted, how many
pages it read and what to do next (`job_status` and `job_next_action` in the
JSON result). It reports warnings:
`job_relocated` when the job is not in the directory it was written in,
`job_changed_elsewhere` for a review or adjudication recorded on such a copy,
and `source_missing` or `source_relocated` as for a run; other bytes at the
source's recorded path fail. For a IIIF snapshot it also reports
`iiif_snapshot_incomplete`, `iiif_record_changed` and `iiif_snapshot_grown` (see
[durable job and attempts](processing-spec.md#durable-job-and-attempts)). See
[deposit a job](share-and-cite.md#deposit-a-job).

For a run, it checks that the files in the run directory agree with one another: manifest
declarations, identifiers, hashes, page counts, raw and normalized references,
quality totals, audit/rerun items, alignment-schema snapshots, and cost totals.
A missing `raw/` directory is read as an empty one, and a missing
`normalized/` too when the run has no schema, since Git and some archive tools
drop empty directories; a file the run records in either is still required. Missing or changed external source files are warnings, and a source found
through the [source index](checkpoint-spec.md#moved-source-files) is reported
as `source_relocated`; malformed or inconsistent ledger artifacts are errors
and produce exit code 1. Verification
checks coherence, not extraction accuracy, and is not a replacement for the
JSON Schema test suite.

Some files of a run are worked out from others, so `verify` works them out again
from the bound readings and the config and compares: each `normalized/` page
(aligned again from its raw reading with the schema in force), each page's grade
in `quality.jsonl`, and the review and quarantine queues in `audit.json`. An edit
to one of them fails with `normalized_evidence_mismatch`,
`normalized_artifact_missing`, `grade_evidence_mismatch`,
`quality_evidence_mismatch` (a quality line that no longer matches what the
engine reported) or `audit_queue_mismatch`. A run whose provenance lost a page's
source identity or route fails with `source_identity_missing` or
`route_evidence_mismatch`, and an unreadable config snapshot with
`config_snapshot_invalid`. The rules for these files have changed over time, so
a run written by an older PageLedger whose rules differ from today's gets the
warning `derived_evidence_differs`, naming the file and the version, and still
passes. Text-derived quality signals (repetition, language, the comparison with
a PDF's text layer) are not worked out again.

Hidden files under `raw/` or `normalized/`, such as the `.DS_Store` and `._*`
files Finder or a copy to an exFAT drive leaves, are named in the warning
`stray_file` and otherwise ignored. An unreadable source file is the warning
`source_unreadable`. A job reports its own warnings only; a stage's warnings,
such as `stray_file`, show with `verify JOB/attempts/<stage>`.

For page images, verification also checks that the recorded page list hashes
to the source identity and that each page's provenance names the image listed
at its place. When the folder has changed, the `source_changed` warning names
each image that changed, went missing or was added. When the images still
match, the carrier PDF is rebuilt and compared with its recorded hash; a
difference is the warning `carrier_not_reproduced`, which means another
PageLedger version wrote it. For a IIIF snapshot, `iiif_snapshot_incomplete`
says the snapshot was incomplete when the run read it, and
`iiif_record_changed` that its `iiif-fetch.json` differs now while the images
match.

## bundle

```bash
pageledger bundle RUN_DIR --out BUNDLE_DIR
pageledger bundle RUN_DIR --out BUNDLE_DIR --json
```

Creates a new directory bundle only after `RUN_DIR` passes `verify` and is
an ordinary, generation-zero execute run. The bundle contains an unchanged
`baseline/` run (manifest, config, and all declared artifacts), copied source
files under `sources/`, an inventory with SHA-256 hashes, and
`replay-route-map.yml`. `bundle.json` is the index and records the baseline
manifest hash, extractor identity/profile, source identities, and portable
paths; replay evidence records the exact `bundle.json` index hash. The output
directory must not already exist; no archive is created.

`bundle` accepts a run that was not itself a rerun. Bundle the original run;
reruns cannot be replayed yet. A job is not a run and is shared as its
folder; see [Deposit a job](share-and-cite.md#deposit-a-job).

The only accepted flags are `--out DIR` (required) and `--json`. There are no
config, adapter, or source override flags. Credential filtering checks only
normalized exact denylist keys in mappings under `adapter_options` or
`hook_options` in the config snapshot and persisted extractor options. It does
not scan values, arbitrary fields or text, sources, raw artifacts, or logs.
Warning: do not treat a bundle as scrubbed of secrets or personal data. A
deterministic local adapter that records no reproducibility profile (such as
`alto`, `rapidocr` or `indra`) can still be bundled; its replay compares the
output as evidence (`evidence_compared`) and never calls it exact.

## replay

```bash
pageledger replay BUNDLE_DIR --out RUN_DIR
pageledger replay BUNDLE_DIR --out RUN_DIR --adapter-path TRUSTED_DIR --json
```

Validates the untrusted directory bundle, checks its baseline and inventory,
loads the locally available adapter named by the bundle, and runs the ordinary
PageLedger extraction path against the bundled sources. A built-in adapter loads
as always. A custom one (`module:object`) runs only if its module is in the
folder given with `--adapter-path DIR`, a locally trusted import path that must
not be equal to, inside, or above the bundle, and the object is defined there.
Only an adapter class or object is built from a bundle, never a plain function;
for an installed adapter give its own package folder, never site-packages itself.
Otherwise replay stops with `adapter_not_trusted` before anything is imported.
A model server off this machine that the bundle names is used only when given
with `--trust-endpoint URL` (not shown in `--help`). See
[jobs, runs and bundles you receive](share-and-cite.md#jobs-runs-and-bundles-you-receive). `--out DIR` is required and must
not already exist. `--json` emits the result and `replay.json` records baseline/local
extractor linkage, profile match, raw equal/different/missing counts, and the
comparison object.
Human output also prints `Raw comparison: N equal / N different / N missing`;
these counts are evidence, not an authenticity claim. See the [replay
boundary](capabilities-and-limits.md#verified-replay-boundary).

See the [command index](#command-index) for exit codes. `--json` includes
replay `outcome` and structured `error`/`code` fields on failure. A replay that
has not finished after 6 hours is stopped with `replay_timeout`; any other
failure inside the replay names the kind of error that ended it.

## fetch

```bash
pageledger fetch https://example.org/iiif/item/manifest.json --out sources/item
pageledger fetch https://example.org/iiif/item/manifest.json --out sources/item --resume
```

Fetches the page images of a IIIF Presentation 2 or 3 manifest, an Image API
2 or 3 `info.json`, or an image service base URL into a new folder: one JPEG
per canvas at the top level (`0001.jpg`, named by canvas position), the
manifest and each `info.json` under `iiif/`, and the
[fetch record](iiif-fetch-spec.md) `iiif-fetch.json`. `--out` must not exist
unless `--resume` is given. `--pages` selects canvases by their position in
the manifest. `--delay` sets the seconds between requests (default 1).
`--max-image-bytes` refuses larger images (default 268435456, 256 MiB).
`--resume` checks the images already fetched against the record and fetches
only the missing canvases; it needs the same URL as the first fetch, and
`--pages`, if given, must repeat the first fetch's selection. Without `--json`, progress goes to stderr and the result, each
`iiif_below_native` warning and each missing canvas to stdout; `--json`
prints a summary with `status`, `fetched`, `fetched_now`, `missing`,
`warnings` and `stopped_by`. A URL whose query carries something that looks
like a key, token or password (`token=`, `api_key=`, `signature=` and the like)
is refused before any request, because it would be written into the fetch
record and the job's report. A server that refuses the connection, a host name
that cannot be found and a certificate that fails are not retried: the fetch
stops and says which it was. See [Fetch page images from a IIIF
server](iiif.md) for what is fetched and the limits.

## inspect

```bash
pageledger inspect jobs/book
pageledger inspect jobs/book --json
pageledger inspect runs/run-001/
pageledger inspect runs/run-001/ --csv > pages.csv
```

Shows a job or a run, recognized as [verify](#verify) recognizes
them. For a job, `inspect` displays `report.md`, which opens with the job's
status, the pages that need a person and why, and the next command, then a table
of the pages and the record of how each was read, or `document.json` with `--json`; `--csv` is for runs and is refused for a job (exit 1).

For a run, it summarizes the run directory: status, page counts, warnings, failures,
review-queue size, records normalized, grade distributions grouped by evidence
basis, cost, and artifact presence. Human output labels each distribution as
`Grades (signals)`, `Grades (schema)`, or `Grades (unknown)` for older graded
entries; it never merges these into an unlabeled headline. JSON retains the
aggregate `grade_distribution` for compatibility and adds
`grade_distribution_by_basis`. `--csv` writes one row per page (page id,
counts, confidence, warnings, grade, grade basis, cost, timing) for triage in a
spreadsheet. `--json` emits the summary as JSON.

Human cost wording uses the same evidence distinctions as `run` and `rerun`:
unknown, known zero/nonzero, or a qualified partial subtotal. The JSON summary
retains its existing field names for machine consumers.

## init

```bash
pageledger init --out processing.yml
# or, for Russian and English print:
pageledger init --lang rus+eng --out processing.yml
# or, for RapidOCR:
pageledger init --engine rapidocr --model-dir models/cyrillic-rec --out processing.yml
```

Writes a config to edit, to the screen or to `--out FILE`. `--out` never
replaces an existing file: choose another name or move the old one away. By default it is a
commented `processing.yml` for `process`: a
local OCR stage after the source's own text, `max_image_pages: 0` so no paid
stage can run, and the optional stages (`second_ocr`, `local_image`, `image`,
`second_opinion`, `contest`) as commented blocks, each with one line on what
it does and a link to its section of the [processing guide](processing-spec.md).
`local_text` is left out, so the source decides it.

| Option | Effect |
|---|---|
| `--engine pdf_ocr` | Tesseract, the default. `--lang` sets its languages (default `eng`). |
| `--engine rapidocr` | RapidOCR; `--model-dir` (required) names the PP-OCRv5 recognizer directory, which sets the language. |
| `--engine indra` | Indra OCR; it picks a recognizer for each line's script, so it takes no `--lang`. `--model-dir` is optional. |
| `--engine none` | No OCR stage, for text files; the optional stages are left out because they need one. |

`--model-dir` is written as an absolute path. A language or model option the
engine does not take is a usage error (exit 2).

```bash
pageledger init --run --out pageledger.yml
pageledger init --run --adapter pdf_ocr --out pageledger-ocr.yml
```

`--run` writes a minimal valid config for `run` instead. With `--adapter pdf_ocr`
the config includes `adapter_options` (`dpi: 300`, `lang: eng`) so the knobs
non-English collections need are visible from the start. `--adapter` without
`--run`, or an engine option with it, is a usage error (exit 2).

## doctor

```bash
pageledger doctor
pageledger doctor --all
pageledger doctor --json
pageledger doctor --config processing.yml
```

Read-only diagnostics. `doctor` says what `process` can do on this
machine: read a PDF's own text, which OCR engine reads scans and images, the
common languages installed for `--lang`, and which optional engines are not
installed; and one line saying that `doctor --all` shows every detail. With
`--all`, or with `--config`, the details follow: Python runtime, optional packages, external
commands with versions and install hints, installed Tesseract language
packs (the valid values for `--lang` and an adapter's `lang`), and whether
cloud OCR/VLM keys are set, without printing their values. With `--config`,
doctor reports only the keys that config names through an adapter's
`env_key`, such as a gateway's own key, or says it names none. A missing config
file or one that is not valid YAML is named in a sentence.

## Earlier command names

Releases before 0.9.0 named some commands differently. Those names still work,
with the same options, the same output and the same exit codes, and will keep
working through every 1.x release (see [stability](stability.md)); they are not
listed by `pageledger --help`. On a job and a run written by 0.8.0, each printed
what 0.8.0 printed, with these additions: the JSON result of `verify-job` has a
`warnings` list (empty when there is nothing to report) and, for a job that
halted or failed, `job_status` and `job_next_action`, which it also prints as one
line; and `review-sheet` also
writes `review-decisions.txt`, a guide to the decisions, beside the sheet, whose
`disposition`, `attempt_summary` and `review_reasons` cells now lead with words
(the codes and IDs follow in parentheses; applying a sheet does not read them). They
follow 0.9's rules where those changed what a command accepts: a moved job
verifies, with a warning, where 0.8 refused it.

| Earlier name | Now |
|---|---|
| `inspect-job JOB_DIR` | `inspect JOB_DIR` |
| `inspect-run RUN_DIR` | `inspect RUN_DIR` |
| `verify-job JOB_DIR` | `verify JOB_DIR` |
| `verify-run RUN_DIR` | `verify RUN_DIR` |
| `review-sheet JOB_DIR --out FILE` | `review JOB_DIR --sheet FILE` |
| `review-job JOB_DIR --review FILE` | `review JOB_DIR --apply FILE` |
| `init-config` | `init --run` |
| `init-config --processing` | `init` |
| `compare-runs RUN_A RUN_B` | `compare RUN_A RUN_B` |

An earlier name takes only its own kind of folder: `verify-run` on a job
reports what it would report on a run with no `manifest.json`.

## Configuration

The recommended starting point is one `pageledger.yml` with optional
`classify`, plus `taxonomy`, `schema`, and `run` sections; `init --run` writes
the minimal form and
[`examples/pageledger.yml`](examples/pageledger.yml) is a commented copy.

`run` needs an extraction route. Without `taxonomy.page_types`, `--routes` or
`--adapter`, every page would go to review and nothing would be extracted, so
the command stops with "No extraction route" and names the fixes. A dry run
needs no route. For a deliberate review-only run, map a page type to
`default_action: review`.

Configuration sections and defaults:

| Key | Type | Default / allowed values |
|---|---|---|
| `schema_version` | string | `0.1` |
| `dataset_citation.label`, `.text` | strings | Optional citation label and text. |
| `language.script` | string | Optional: `Cyrillic`, `Latin`, `Greek`, `Arabic`, `Devanagari`. |
| `language.orthography` | string | Optional: `prereform`. |
| `taxonomy.page_types` | mapping | Optional; entries may have `default_action` (`transcribe_text`, `skip`, `review`), `prompt` (string), and `review` (boolean, default false). |
| `classify.adapter` | string | No default; absent means suffix-based probe (`pdf_text` for PDF, `text` otherwise). |
| `classify.adapter_options` | mapping | Empty; passed to the probe adapter. |
| `classify.hook`, `classify.hook_options` | string, mapping | Optional hook import and options. |
| `classify.min_confidence` | number 0–1 | `0.5`. |
| `classify.thresholds` | mapping | Classifier threshold overrides; omitted values use built-in defaults. |
| `run.adapter` | string | Optional: `text`, `pdf_text`, `pdf_ocr`, `rapidocr`, `indra`, `vision`, `alto`, or a custom adapter's `module:object` import path. |
| `run.adapter_options` | mapping | Empty; adapter-specific. Cannot accompany `adapter_order`. |
| `run.adapter_order` | non-empty list | Optional chain; entries are adapter strings or `{adapter, adapter_options}` mappings. Mutually exclusive with `adapter` and top-level `adapter_options`. |
| `run.budget.max_pages`, `max_tokens`, `max_usd` | non-negative integer, integer, number | No cap unless set. `max_pages` counts only the pages routed to the adapter. `process` does not read `run.budget`; its limits are `processing.limits` (below). |
| `run.budget.unknown_usage` | string | `stop`: under `max_tokens` or `max_usd`, a page whose adapter did not report that unit stops the run before the next request, since the cap can no longer be checked. `continue` reads on. Adapters that declare themselves local are exempt. |
| `run.budget.warn_pages`, `warn_tokens`, `warn_usd` | non-negative integer, integer, number | No absolute warning unless set. |
| `run.budget.warn_at_percent` | number 0–100 | No percentage warning unless set; relative to a configured cap. |
| `run.pricing.cost_per_page`, `cost_per_1k_tokens` | non-negative numbers | No configured rate. Used only when adapter-reported cost is absent. |
| `run.retry.max_retries` | non-negative integer | `0`. |
| `run.retry.backoff` | string | `none`; `exponential` is also allowed. |
| `run.on_page_error` | string | `stop`; or `continue`. |
| `run.max_consecutive_failures` | non-negative integer | `0` disables the circuit breaker. |
| `run.grading.review_below_grade` | string | Off; one of `A`, `B`, `C`, `D`, `F`. |
| `run.grading.thresholds` | mapping | Optional overrides for `confidence` (`A`, `B`, `C`, `D`), `required_column_coverage` (`A`, `B`, `C`), and `arithmetic_pass_rate` (`A`, `B`, `C`); numeric values merge over built-in thresholds. |
| `run.rerun_if`, `run.quarantine_if` | list of single-key mappings | Empty. Rules support `grade_below`, `missing_required_columns: true`, `arithmetic_failure_rate_above` (0–1). |
| `run.max_rerun_depth` | non-negative integer | `2`. |
| `schema.name` | string | Required when `schema` exists. |
| `schema.columns[].name` | string | Required. `type` is `string` (default), `integer`, or `number`; `required` is boolean (default false); `aliases` is a list of strings (default empty). |
| `schema.checks[]` | list | Optional entries require `name` and `expression`; `tolerance` is a non-negative number (default 0). Expressions allow one `==` and `+`, `-`, `*` over declared numeric columns and numeric constants. |
| `schema.quality.minimum_required_column_coverage`, `low_confidence_threshold` | numbers 0–1 | Optional grading floors. |
| `processing.local_text`, `local_ocr`, `second_ocr`, `local_image`, `image`, `second_opinion` | stage mappings or null | Left out, `local_text` is chosen by the source (`pdf_text` for PDFs and page images, `text` otherwise) and cannot be `null`; `local_ocr` is `pdf_ocr` for PDFs and page images, and `null` turns OCR off; the other stages are off. Each stage has `adapter`, `adapter_options` (mapping, empty by default), and `prompt` (non-empty string). `second_ocr`, `local_image` and `image` need a `local_ocr` stage; `second_opinion` needs `image`; `image` needs a positive `max_image_pages` and `local_image` a positive `max_local_image_pages`. See [the ladder](processing-spec.md#the-ladder-a-second-ocr-engine-and-a-local-model). |
| `processing.limits.max_attempt_pages`, `max_tokens`, `max_cost_usd` | non-negative integer, integer, number | No limit unless set. |
| `processing.limits.max_image_pages` | non-negative integer | `0`. Must be positive to enable image stage. |
| `processing.limits.max_local_image_pages` | non-negative integer | `0`; accepted only with a `local_image` stage, and must then be positive. Not counted by `max_image_pages`. |
| `processing.escalate_on` | list | `[hold]`. Adds `rough`, `disagreement` or `always` to what makes a page climb; must include `hold`, each once. See [climb on roughness and disagreement](processing-spec.md#climb-on-roughness-and-disagreement). |
| `processing.lexicon` | mapping | Optional; `provider: pymorphy3` with `language: ru`, or `provider: wordlist` with `path`; `rough_below` (a share above 0, at most 1, default 1) only with the `rough` trigger. Needs the `rough` trigger or `contest`. |
| `processing.contest` | mapping | Optional; `{rules}`, one of `ru-print-0.1`, `ru-print-0.2`, `ru-print-0.3`, `latin-print-0.1`. Needs an `image` or `local_image` stage. See [contest a model's reading](processing-spec.md#contest-a-models-reading). |
| `processing.tiling` | mapping | Optional; `stages` (the stages read in tiles: `[image]`, the only one supported so far), `layout` (`local_text`, `local_ocr` or `second_ocr`, a stage whose adapter reports a layout), `min_line_pixels` (positive integer, default `32`), `max_tiles` (1–99, default `24`). Requires `processing.limits.max_image_requests`. See [Read dense pages in tiles](processing-spec.md#read-dense-pages-in-tiles). |
| `processing.limits.max_image_requests` | positive integer | Required with `processing.tiling` and accepted only then: the image requests (one per tile) the paid stages may send. |
| `processing.links.article`, `.custody` | non-empty strings | Optional caller-supplied links; PageLedger does not verify them. |
| `processing.benchmark` | mapping | Optional; `{stage, every_nth_page}` requires an enabled stage and positive integer interval. |
| `processing.batch` | mapping | Optional; `{pages, concurrent}` positive integers, `concurrent` no more than `pages`. Rebuilds the job once per batch and reads `image` and `second_opinion` pages concurrently; ignored when `max_tokens` or `max_cost_usd` is set. |

A new run refuses a key under `run.budget` it does not know, since a misspelt
cap would silently be no cap; a misspelt key under `run.pricing`, `run.retry`,
`run.grading` or `classify` is warned about. A config recorded by an earlier run
still loads, so old runs stay readable.

Built-in `pdf_ocr` adapter options are `dpi` (integer 50–1200, default 300),
`lang` (Tesseract language codes separated by `+`, default `eng`) and
`max_render_pixels` (integer 1,000,000–400,000,000, default 60,000,000).
Rendering has a 120-second per-page timeout and OCR a 300-second per-page
timeout. Adapter-specific options are passed through; custom adapters define
their own options.

Common `run` settings:

```yaml
taxonomy:
  page_types:
    blank: {default_action: skip}
    sparse: {default_action: transcribe_text, review: true}
    prose: {default_action: transcribe_text}
    table_likely: {default_action: transcribe_text, review: true}
    unknown: {default_action: transcribe_text, review: true}

classify:
  min_confidence: 0.5
  # adapter: pdf_ocr             # omit for per-suffix probe defaults
  # hook: my_project.routes:Classifier
  thresholds:
    table_column_line_ratio: 0.015

run:
  # adapter_order is mutually exclusive with run.adapter/adapter_options.
  adapter_order:
    - adapter: pdf_ocr
      adapter_options: {dpi: 400, lang: rus}
    - adapter: my_project.adapters:StrongerAdapter
      adapter_options: {model: stronger-model}
  budget:
    max_pages: 500              # refuse before extraction if the plan is larger
    max_usd: 5.00
    warn_pages: 400             # alerts do not require a matching cap
    warn_tokens: 500000
    warn_usd: 4.00
    warn_at_percent: 80         # cap-relative warning remains supported
  retry:
    max_retries: 2
    backoff: exponential
  on_page_error: continue         # stop (default) | continue
  max_consecutive_failures: 3     # circuit breaker; 0 disables
  pricing:
    cost_per_page: 0.0015       # only if you want derived cost estimates
  grading:
    review_below_grade: C       # queue pages graded below C (off by default)
  rerun_if:
    - grade_below: C
    - missing_required_columns: true
    - arithmetic_failure_rate_above: 0.05
  quarantine_if:
    - grade_below: D
  max_rerun_depth: 2
```

Each budget unit records one structured first crossing. If absolute and
cap-relative thresholds coexist, the lower effective value wins; an exact tie
is labeled `absolute`. `cost.json` also groups extracted-page usage and
resolved cost under `by_adapter` and `by_page_type`; skipped/review-only pages
do not appear in those rollups.

With `on_page_error: continue`, exhausted page failures are recorded and the
run continues. Any failed page makes the final run `partial` and the CLI exits
nonzero after writing complete artifacts. The circuit breaker halts after the
configured number of consecutive failed pages. Failed pages and pages not
attempted after a halt are written to the rerun manifest.

`rerun_if` adds matching pages to the review queue and rerun manifest.
`quarantine_if` records matching pages in the quarantine queue and excludes
them from rerun items. Rules are evaluated after grading. Each list item must
be a single-key mapping. The predicates are:

- `grade_below` with one of `A, B, C, D, F`. The comparison is strict.
- `missing_required_columns: true`. It only matches aligned pages.
- `arithmetic_failure_rate_above` with a number from 0 to 1. It only
  matches aligned pages with an arithmetic pass rate. The comparison is strict:
  a recorded 70% failure rate does not exceed a threshold of `0.7`.

The older `run.grading.review_below_grade` setting still works and can be
used with `rerun_if`. Overlapping reasons are kept in `audit.json` and
joined on the single rerun item.

Split config examples (`page-taxonomy.yml`, `table-schema.yml`,
`run-policy.yml`) live in [`examples/`](examples/) for larger projects.
