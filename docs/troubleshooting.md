# Troubleshooting PageLedger

This guide is organized by the symptom you see. A typed diagnostic is a
PageLedger-authored error code and message. A typed adapter failure, such as
`AdapterFailure: MODEL_QUOTA (HTTP 429)`, shows its code; other adapter
messages are redacted, and when a message reads `<redacted>`, it does not
establish the cause.

## `missing_binary` or `missing_language_pack`

`pdf_ocr` cannot find Poppler's `pdftoppm`, Tesseract, or a requested Tesseract
language pack. Run `pageledger doctor` and inspect the command and language
availability. Tesseract uses three-letter language codes; an unavailable code
message suggests close installed codes and names the number available. Install
the missing system tool or language data, or set `--lang` (without a config),
`run.adapter_options.lang` (in a run config), or
`processing.local_ocr.adapter_options.lang` (in a job config). `doctor` checks
setup; a successful doctor check does not test every input page.

## A source file or output folder is missing or already in use

If `process` says the source file was not found, check the spelling and path.
If the output folder already exists, inspect it with `pageledger inspect DIR`,
continue the job with `pageledger resume DIR`, or choose another `--out` folder.
PageLedger checks these paths before printing the default-config notice.
`run` also reports a missing source as a named PageLedger error.

If a selected page is past the end, PageLedger names that page and the source's
page count. Correct the `--pages` range; for example, a 124-page source cannot
include page 125.

## "is not a PDF, a page image or UTF-8 text"

`process` decides what kind of source it has before the job exists. A source
is a PDF, a UTF-8 text file (pages separated by form feeds), one JPEG or PNG
page image, or a folder of them. A Word file, a spreadsheet or a folder with
no page images in it (`is a folder with no page images in it`) is refused, and
no job folder is written. Export a Word file as PDF first; convert TIFF or
JPEG 2000 page images to PNG (see `unsupported_image` below).

## A review sheet was renamed

Keep the `.binding.json` file beside the CSV. If a spreadsheet saved the CSV
under a new name, `review --apply` can locate the unique matching binding in
that folder. If there is no unique match, rename the binding to
`SHEET.csv.binding.json`. The binding still checks each row against the job's
selected output and hashes.

## A review is refused

- `This review file was made for another document`: a JSON review receipt
  names a different source (its `source_sha256`) than the job holds, usually
  because it was written for another job. `This review file is not a review
  receipt` means its top-level fields are not exactly `schema_version`,
  `source_sha256` and `decisions`. See the
  [receipt contract](document-report-spec.md#human-review-receipt).
- `Page N carries text corrected by ...; a JSON receipt cannot replace it`:
  decide a corrected page in a review sheet or the review view, which show the
  correction.
- `The correction for page N is empty`: a correction cannot be empty. If the
  page has no text, record it as `blank`.
- `The correction for page N is identical to the reading it started from`:
  nothing was corrected. Record the page as `accept` instead.
- The warning `blank_with_text`: a page recorded as blank has a selected
  reading of 50 or more words. Check the page; the decision is still recorded
  unless you change it.

## `bundle` refuses a directory

`bundle` accepts a run that was not itself a rerun. Bundle the original run;
reruns cannot be replayed yet. A job is shared as its folder, not as
a replay bundle; follow [Deposit a job](share-and-cite.md#deposit-a-job).

## `compare` shows incomparable grades

PageLedger does not rank grades from different extractors. The text report
still shows page lengths and warning changes side by side for each shared page.

## `engine_timeout`

A built-in OCR subprocess exceeded its time limit. Check whether the source
page is unusually large or the machine is under load. Try a smaller page range
or a lower `dpi`, while checking that reduced resolution remains useful. For a
custom adapter, set an explicit subprocess timeout and handle its timeout; see
[adapter subprocess guidance](adapter-protocol.md#timeout-and-subprocess-guidance).

## A model page fails with `MODEL_...`

These codes come from the [`vision` adapter](vision-adapter.md), and from
custom adapters that use the same failure codes.

| Code | Likely cause | What to do |
|---|---|---|
| `MODEL_NETWORK_ERROR` | Nothing answers at `base_url`. | Start the server, or correct its host and port; `curl {base_url}/models` should answer. |
| `MODEL_HTTP_ERROR` | The server answered with an error, shown as its HTTP status: often a `base_url` without its `/v1`, a model name the server does not know, or a rejected key (401, 403). | Compare `base_url` and `model` with the names `{base_url}/models` lists, and check the key. |
| `MODEL_UNAVAILABLE` | The variable that `env_key` names is not set. | Set it in the shell that runs PageLedger. |
| `MODEL_QUOTA` | A rate limit or an exhausted quota (HTTP 402 or 429). | Wait, or use another route. A job with `processing.batch` sends up to `concurrent` image pages at once: lower `concurrent` if the provider limits requests per minute. Otherwise PageLedger sends one page at a time. |
| `MODEL_TIMEOUT` | No answer within `timeout_seconds`. | A local model can need longer on a large page: raise `timeout_seconds`, up to 600, or lower `max_image_side`. |
| `MODEL_OUTPUT_TRUNCATED` | The reading reached `max_tokens`: the model spent it reasoning, looped, or the page is very long. | See [models that reason before answering](vision-adapter.md#models-that-reason-before-answering) and [time, heat and loops](vision-adapter.md#local-models-time-heat-and-loops). |
| `MODEL_CONTENT_FILTERED`, `MODEL_RECITATION` | The provider declined the page. | Read that page with another engine or by hand. |
| `MODEL_EMPTY_RESPONSE`, `MODEL_INVALID_RESPONSE` | No reading, or an answer that is not a chat completion. | Read one page with `--pages`, and check that the model accepts images. |
| `IMAGE_RENDER_ERROR` | The page image could not be made small enough for the request. | Lower `max_image_side`. |

## `unsupported_encryption`, `missing_crypto_dependency`, or `malformed_pdf`

These PDF diagnostics mean the file needs a password or unsupported encryption,
AES support is unavailable, or pypdf could not parse the file. Install the
optional PDF support with `pip install 'pageledger[pdf]'` for the AES support,
or use an unprotected, valid copy. PageLedger does not accept passwords or
repair damaged PDFs. In a job, `source_container_invalid:<code>` is a
halted source-container failure; check the `job.json` failure details and
replace the source only if you can establish it is a valid, authorized copy.

When reading a damaged PDF, pypdf may report that it repaired structural
objects. PageLedger summarizes these notices on stderr in one line, and does the
same for pypdf's notices that it needs the optional `fontTools` package to
decode a font fully; other pypdf warnings stay visible.

## `unsupported_image` or `malformed_image`

PageLedger reads page images in JPEG and PNG. `unsupported_image` names a file
it cannot carry faithfully, such as a TIFF, a JPEG 2000 master, an interlaced
PNG, an image stored mirrored or one larger than 400,000,000 pixels, and the
message names a command that converts it. Convert the folder, then run the converted files: they are
what PageLedger records. `malformed_image` names a damaged or truncated file;
replace it with a good copy. Either stops the command before any page is read.

## A text file is "not UTF-8" (`unreadable_text`)

A text source must be UTF-8. `process` refuses another file before the job
exists ("is not a PDF, a page image or UTF-8 text"); in a run the page fails with
`unreadable_text`, naming the first byte that cannot be read as UTF-8. Open the
file in a text editor, save it with the encoding UTF-8, and read it again. PageLedger does not guess another encoding, because a wrong
guess would change letters without saying so.

## "This job names its own adapter" or "a server it names itself"

`resume` and `replay` refuse to run Python code, or to send page images to a
model server off this machine, that a job, run or bundle names in its own files,
unless you name that code's folder with `--adapter-path` or the server with
`--trust-endpoint`. The refusal comes before anything changes, so the folder is
as it was. If the folder is your own, or you trust where it came from and that
code or server, run the command again with the option the message gives;
otherwise do not. See
[jobs, runs and bundles you receive](share-and-cite.md#jobs-runs-and-bundles-you-receive).

## `render_limit` or `render_dpi_capped`

`render_dpi_capped` is a page warning, not a failed render: the page was
rendered below the requested DPI to remain within `max_render_pixels`. Check
the effective DPI in provenance. When the same page also has `render_upsampled`,
the render already holds more pixels than the scan, so raising the cap adds
nothing: ignore the DPI warning. `render_limit` means fitting the page under
the configured pixel cap would require less than 72 DPI. Consider splitting the
page upstream or, if memory allows, increasing `max_render_pixels`: in a job
under `processing.local_ocr.adapter_options`, in a run under
`run.adapter_options`. See [page size and rendering](ocr-options.md#page-size-and-rendering).

## "It resumes only where it was written"

A job that has been moved or copied verifies, exports and can be reviewed
where it is, but `resume` refuses it: its stage runs are bound to the folder it
was written in, and two resumable copies could pay for the same pages twice.
Move it back to the folder the message names to resume it.

## A word list is missing or changed

A job that judges words with a word list (`processing.lexicon` with
`provider: wordlist`) records the list's path and SHA-256. `resume` and
`verify` refuse the job when the file at that path has changed (`This job
judged words with the word list /path/words.txt (SHA-256 3f2a9c…); the installed
lexicon is …`) or is gone (`processing.lexicon cannot read the word list …`):
restore the recorded file. A new list is read only by a new job. `cannot read
the word list`, `has no words` and `is not UTF-8 text` on a new job mean the
file named in the config is not a plain UTF-8 list of one word per line. A job
that judges words with `pymorphy3` is refused the same way when another version
of it or its dictionary is installed: install the recorded versions.

## An `alto` name template is refused

`name: "{stem}.xml"` pairs each page image with the ALTO file of the same name,
so it is refused for a PDF, whose pages have no file names: use
`name: "{page:04d}.xml"` there. `{page}` is refused for page images whose names
carry numbers that are not their positions in the folder, as in a IIIF snapshot
with a canvas missing: use `{stem}`. See
[the ALTO recipe](engine-recipes.md#alto-from-a-library-any-platform-no-engine).

## `fetch` refuses a URL or a canvas

- `is a loopback, private or link-local address, and only the host of the URL
  you named ... may be one`: a manifest, image service or redirect led to a
  machine on your own network that you did not name. Name a manifest on the
  same host as its images.
- `refused a plain http URL under the https URL you named` or `refused a
  redirect from https to http`: under an `https` URL nothing is fetched over
  `http`. An `http://` image service id is asked for over `https` first; when
  that does not answer, the canvas fails.
- `the URL names a IIIF collection`, `Image API 1 services are not supported`:
  fetch one of the collection's manifests, or the item from another server.

The fetch stops with an incomplete record when a server keeps refusing; `fetch
--resume` with the same URL fetches only what is missing. See the
[IIIF limits](iiif.md#limits).

## A job is paused or halted

A job with status `paused_budget` reached a processing limit and can continue
after a limit is raised. For example:

```bash
pageledger resume jobs/book --raise-limit max_attempt_pages=500
```

Choose a limit that is appropriate for the remaining work. Resume records the
new limit in the job history. A halted job does not automatically continue:
inspect `job.json` and `report.md` for the recorded failure. Correct the cause
and follow the recovery instructions for that failure; do not assume a halted
paid request is safe to repeat when its outcome is unknown. See
[processing and recovery](processing-spec.md).

## `verify` reports a problem

Verification checks whether the retained files agree with their records and
hashes; it does not judge the text. A pass says so on its second line: `The
record is intact: every file matches what was recorded. This does not check that
the text is right.` A failure names the file and what is wrong.
Do not edit generated files to silence it. Keep a copy of the folder,
investigate whether a file was changed, left out or copied incompletely, and
verify again after restoring the exact file; otherwise keep the failure as part
of the record. See [artifact verification](artifacts.md).

`Status: fail (unfinished)` with `This job did not finish` means the job was
interrupted or is still running: run the `pageledger resume` command it names,
then verify again.

`verify` can pass with these warnings, which describe where the job, run or
source is now rather than a fault in the record:

| Warning | Meaning |
|---|---|
| `job_relocated` | The job is not in the folder it was written in. It still verifies in full. |
| `job_changed_elsewhere` | A review or adjudication was recorded on a copy of the job, away from where it was written. |
| `source_missing` | The source is not at its recorded path and no [source index](checkpoint-spec.md#moved-source-files) entry finds it. The job's own evidence was checked. |
| `source_relocated` | The source was found through the source index. |
| `source_changed` | For a run only: the source no longer matches what the run read; for page images, each image that changed, went missing or was added is named. A job whose source changed fails instead. |
| `carrier_not_reproduced` | The PDF PageLedger builds from page images came out differently, which means another PageLedger version wrote it; the images match. |
| `iiif_snapshot_incomplete`, `iiif_record_changed`, `iiif_snapshot_grown` | The IIIF snapshot was incomplete when read, its fetch record has changed since, or a later `fetch --resume` added images; see [process the snapshot](iiif.md#process-the-snapshot). |

| `stray_file` | For a run: a hidden file under `raw/` or `normalized/`, such as the `.DS_Store` or `._*` files Finder or a copy to an exFAT drive leaves. It is ignored. |
| `source_unreadable` | The source is there but could not be read (for example, no permission). |
| `derived_evidence_differs` | For a run written by an older PageLedger: a normalized page, grade or queue worked out again by today's rules differs from what that version recorded. The file and version are named. |

Other bytes at the source's recorded path fail verification.

## The command exits with code 134 after finishing

On macOS, ONNX Runtime (used by the `rapidocr` adapter) has once been seen to
abort while Python shut down, after a job had completed and written every
file. The message is `libc++abi: terminating due to uncaught exception of type
std::__1::system_error: recursive_mutex lock failed`. It could not be
reproduced in 18 further jobs. If you see it, run `pageledger verify` on
the output: a `pass` means the work is complete and the exit
code came from shutdown. Scripts that call PageLedger should decide from the
verification, not from the exit code alone.

## One page keeps failing

For a run, inspect `run.log`, `manifest.json`, and `rerun-manifest.yml`. Rerun
uses only the pages listed in the parent's rerun manifest. For a targeted
reproduction, use the source file and page selection directly:

```bash
pageledger run scans/book.pdf --config pageledger.yml --pages 42 --out runs/page-42
```

The config must contain the same route and adapter settings. To isolate a
built-in PDF OCR failure from PageLedger, render that page and invoke
Tesseract directly:

```bash
mkdir -p /tmp/pageledger-debug
pdftoppm -f 42 -l 42 -r 300 -png scans/book.pdf /tmp/pageledger-debug/page
tesseract /tmp/pageledger-debug/page-42.png stdout -l eng
```

A failure in a custom adapter may appear redacted in `run.log`: PageLedger
does not reveal adapter-controlled stderr. Running the tool directly, as above,
shows its own message.

Adjust the output filename if Poppler names it differently; inspect the files
it actually produced. This direct test does not create PageLedger provenance
or prove that the rerun will succeed. Do not paste source text, credentials, or
sensitive paths into public issue reports.
