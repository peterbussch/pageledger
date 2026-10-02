# Troubleshooting PageLedger

This guide is organized by the symptom you see. A typed diagnostic is a
PageLedger-authored error code and message. A typed adapter failure, such as
`AdapterFailure: MODEL_QUOTA (HTTP 429)`, shows its code; other adapter
messages are redacted, and when a message reads `<redacted>`, it does not
establish the cause.

## `missing_binary` or `missing_language_pack`

`pdf_ocr` cannot find Poppler's `pdftoppm`, Tesseract, or a requested Tesseract
language pack. Run `pageledger doctor` and inspect the command and language
availability. Install the missing system tool or language data, or change
`run.adapter_options.lang` to an installed language. `doctor` checks setup; a
successful doctor check does not test every input page.

A generic custom-adapter failure may instead appear redacted in `run.log`.
PageLedger does not reveal adapter-controlled stderr. Check the local setup
with `pageledger doctor`, then run the tool directly as described below.

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
| `MODEL_QUOTA` | A rate limit or an exhausted quota (HTTP 402 or 429). | Wait, or use another route. PageLedger sends one page at a time. |
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
repair damaged PDFs. In a document job, `source_container_invalid:<code>` is a
halted source-container failure; check the `job.json` failure details and
replace the source only if you can establish it is a valid, authorized copy.

## `render_limit` or `render_dpi_capped`

`render_dpi_capped` is a page warning, not a failed render: the page was
rendered below the requested DPI to remain within `max_render_pixels`. Check
the effective DPI in provenance. `render_limit` means fitting the page under
the configured pixel cap would require less than 72 DPI. Consider splitting the
page upstream or, if memory allows, increasing `max_render_pixels` in
`run.adapter_options`. See [page size and rendering](ocr-options.md#page-size-and-rendering).

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

## `verify-run` or `verify-job` reports a problem

Verification checks whether retained artifacts agree with their manifests and
hashes. Do not edit generated files to silence a failure. Keep a copy of the
original directory, read the verifier's named artifact and message, and
investigate whether a file was changed, omitted, or copied incompletely.
Compare a copied directory with its source. For a job, verify again after
restoring the exact missing or altered artifact; otherwise preserve the
verification failure as part of the record. See [artifact verification](artifacts.md).

## The command exits with code 134 after finishing

On macOS, ONNX Runtime (used by the `rapidocr` adapter) has once been seen to
abort while Python shut down, after a job had completed and written every
file. The message is `libc++abi: terminating due to uncaught exception of type
std::__1::system_error: recursive_mutex lock failed`. It could not be
reproduced in 18 further jobs. If you see it, run `pageledger verify-job` (or
`verify-run`) on the output: a `pass` means the work is complete and the exit
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

Adjust the output filename if Poppler names it differently; inspect the files
it actually produced. This direct test does not create PageLedger provenance
or prove that the rerun will succeed. Do not paste source text, credentials, or
sensitive paths into public issue reports.
