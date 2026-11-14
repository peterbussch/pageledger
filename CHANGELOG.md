# Changelog

Release changes follow the [artifact compatibility policy](docs/run-manifest-spec.md#compatibility-policy)
and, from 1.0, [stability](docs/stability.md).

## 0.9.0 - 2026-11-10

0.9.0 is the candidate for 1.0. It writes down the contract that 1.0 will keep
(commands and options, exit codes, configuration keys, the adapter protocol,
artifact schemas, warning codes and holds) and tests the package against it;
see [stability](docs/stability.md). And it closes the gaps real use found: page
images and IIIF items as sources, a library's own OCR as a witness, dense pages
read in tiles, Latin-script print, a review view with corrections, and a first
job that needs no configuration file. Artifacts keep `schema_version: "0.1"`;
every change to them is an addition. 0.8.0 was prepared but not published, so
if you come from 0.7, read its notes below too.

### Added

Starting a job

- `process SOURCE --out DIR` runs without `--config`: Tesseract in English for
  a PDF or page images (`--lang` chooses another language), no OCR for a text
  file, nothing paid. The job records exactly the configuration `init` writes,
  and one line on the screen says what will be read and how. `--engine` and
  `--model-dir` work on `process` too but are left out of its `--help`; `init`
  is where an engine is chosen. A missing engine stops the command before the
  job exists, naming what to install. See
  [start with local text and OCR](docs/processing-spec.md#start-with-local-text-and-ocr).
- `process` and `resume` show progress while a job runs: pages done, pages
  that need a person so far, and what is being read now. `resume` of a job ends
  with the same summary as `process`. See [process](docs/cli.md#process).
- Ctrl-C stops a job without a traceback, keeps what was read and prints the
  `resume` command (exit 130); `run`, `fetch` and `review` say the same for
  their folders. See [process](docs/cli.md#process).
- When most pages Tesseract read were held for low confidence, `process` says
  the language may be wrong and, when Tesseract's script detection is sure,
  which language to try, with the command to run: the same command with that
  `--lang` and its `--pages` and other options, so a sample stays a sample; on
  pre-1918 Russian read with `rus` it says only that the community `orus`
  model keeps the old letters but made more errors overall in our test, with
  the command to try it on a sample. After `resume` it gives both ways to
  change the language. A missing language pack or tool is said as its
  sentence, with the code last. See
  [process](docs/cli.md#process).
- `--lang orus` without that model installed names its recipe instead of
  suggesting similar codes. See
  [the orus recipe](docs/engine-recipes.md#tesseract-with-the-community-orus-model-macos-and-linux).
- `init` writes a commented `processing.yml` with each optional stage as a
  commented block; [`docs/examples/processing.yml`](docs/examples/processing.yml)
  is that file. See [init](docs/cli.md#init).
- `process` ends with the opening of the job's report: status, pages read, how
  many need a person and why (each cause on a line of its own, with its count
  of pages), the next command, and `export JOB --out NAME.md`, the one export
  command `review --apply` and the report suggest too. It warns at the start when
  the stages could read more pages than `max_attempt_pages` allows, and the
  report says why a configured stage read no pages. See [process](docs/cli.md#process).

Sources

- Page images: a JPEG or PNG file is a one-page document, a folder of them one
  document in natural order of file name. Hidden files are skipped; anything
  else in the folder stops the command before a page is read. TIFF, JPEG 2000
  and other formats are refused with `unsupported_image` and the conversion to
  make, as is an image over 400 million pixels; a damaged one gives
  `malformed_image`. Each image is hashed, and `verify` names one that changed,
  went missing or was added. Reports show a page's file name after its number,
  as `9 (0081.jpg)`. See [page images](docs/process-a-collection.md#page-images).
- The built-in engines read page images through a *carrier*, a PDF holding the
  images' own bytes, sized from the density each image declares (300 pixels
  per inch when it declares none); the run records its hash. An adapter that
  declares `image` and not `pdf` gets the image files. See
  [page-image sources](docs/run-manifest-spec.md#page-image-sources).
- `fetch URL --out DIR` copies a IIIF item's page images, at native size or the
  largest the server allows (`iiif_below_native`), into a snapshot folder with
  the manifest as served and a fetch record, `iiif-fetch.json`, binding each
  image's hash to its URLs ([spec](docs/iiif-fetch-spec.md)). It is polite to
  the server, and `--resume` fetches only what is missing. It refuses a JPEG
  that a folder of page images could not read (stored mirrored, or with two
  frame headers). A snapshot is a
  source: the job records the fetch record and each page's canvas, so a page
  can be cited by its canvas. See [fetch page images from a IIIF server](docs/iiif.md).
- An `alto` adapter reads the OCR a library published with its scans as ALTO
  XML, one file per page, found by page number (`{page:04d}.xml`) or by the
  page image's name (`{stem}.xml`). Its readings are marked `ocr_text_layer`,
  so in a job they are a witness beside the job's own OCR. Unsafe or oversized
  files are refused. A page's confidence is given only when the file scores
  every word (`unscored_count` says how many it did not). New warnings:
  `alto_missing`, `alto_invalid`, `alto_geometry_mismatch`. See [the ALTO recipe](docs/engine-recipes.md#alto-from-a-library-any-platform-no-engine).

Reading

- Tiled reads: with `processing.tiling`, the `image` stage's `vision` model
  reads a dense page in tiles cut along the white space of the OCR's or the
  ALTO file's layout, one request per tile, bounded by the new limit
  `max_image_requests` (counted in `usage.image_requests`). Every tile sent is
  kept as evidence and checked by `verify`, and adjudication packets carry each
  span's tile image. New warnings: `tile_failed` and `tile_edge_cut` (held),
  `tile_text_small`, `tile_layout_missing` and `alto_layout_rejected`. See
  [read dense pages in tiles](docs/processing-spec.md#read-dense-pages-in-tiles).
- Adapters can return the boxes of the lines or words they found
  (`ExtractionResult.layout`); `rapidocr` and `alto` do, and a run records the
  layout's hash. See [result fields](docs/adapter-protocol.md#result-fields).
- Contest rule sets `latin-print-0.1` for Latin-script print and
  `ru-print-0.3` for Russian. Both keep accents, vowel signs and other
  combining marks inside words, and rejoin a word hyphenated at a line end in
  any script (`при-ѣхалъ`, `Ver-änderung`); `ru-print-0.1` and `ru-print-0.2`
  keep their spans exactly. See
  [contest a model's reading](docs/processing-spec.md#contest-a-models-reading).
- A word list as the lexicon (`processing.lexicon: {provider: wordlist, path:
  FILE}`), so the `rough` trigger and contest judge words in any language. The
  job records the list's hash; `resume` and `verify` refuse the job if it
  changes. See [a word list as the lexicon](docs/processing-spec.md#a-word-list-as-the-lexicon).
- `adjudicate --packets DIR --term WORD` writes packets only for open spans
  near the named words. See [settle contested spans](docs/processing-spec.md#settle-contested-spans).
- Warnings that hold a page: `split_words` (words broken by stray spaces),
  `capital_sign_in_word` (a capital Ъ or Ь inside a word, as modern OCR reads
  pre-reform print) and `labels_without_values` (a table whose numbers were
  lost). Warnings that hold nothing: `render_upsampled` (rendered at more
  pixels than the source image has; custom adapters can add it with
  `adapters.render_warnings`), `no_text_layer` (`pdf_text` on page images) and
  `multi_column_rows` (RapidOCR read columns side by side). See [warnings](docs/warnings.md).
- `rapidocr` reads a recognizer's dictionary from its `inference.yml` and
  records the dictionary's hash. See
  [the RapidOCR recipe](docs/engine-recipes.md#rapidocr-with-pp-ocrv5-english-macos-and-linux-cpu).

Reviewing and exporting

- `review JOB` writes a review view into `JOB-review` beside the job (or
  `--out DIR`) to open in a browser: each page's
  image beside its text, the other readings compared word by word, and why the
  page needs a person in plain words, those pages first; codes and reading IDs
  are folded under "From the record". Readings are named by what made them
  ("Tesseract OCR (rus)", "the PDF's own text"), and a reading with no text is
  not offered to use or to correct from (one made only of symbols, such as
  `* * *`, is). The decisions are grouped as a person
  decides ("The text is right", "Use another reading", "I corrected the text",
  "This page has no text to transcribe", "The page cannot be read"), with what
  each records. One download, `review.json`
  ([schema](schemas/review-decisions.schema.json)), carries decisions and
  corrections (a file written by hand may give `page_number` as a number), and
  is recorded only by `review --apply`, given the path where the browser saved
  it; the review sheet
  route is folded under "Working in a spreadsheet instead". Page images of PDFs
  and of image folders are rendered with Poppler; `--images held` renders only
  held pages, and `--images none` needs no Poppler (a view of page images then
  links to each image, and does not list the image's own, always empty, text).
  `review` says what is in the folder for a person: the page to open, and the
  sheet with its guide for a spreadsheet. Asked again, it says when the view
  there is up to date (`--images none` included), or that the job has changed
  since, by a resume, a review or anything else, and a new `--out` is needed. See [review and correct](docs/review-and-correct.md),
  a new how-to, and
  [review beside the page image](docs/processing-spec.md#review-beside-the-page-image).
- Every review sheet, from `review --sheet` or in the review view's folder, has
  `review-decisions.txt` beside it: the command that applies it, the decisions
  and what each records, and the readings each page can use. The sheet's
  `disposition`, `attempt_summary` and `review_reasons` cells lead with words,
  the code or reading ID last; applying a sheet does not read them, so 0.8's
  sheets apply as before. `review --sheet` never writes over an existing
  sheet, its binding or someone's own `review-decisions.txt` (the guide is
  then `NAME-decisions.txt`). A decided row of a sheet written before the
  page's last review is refused rather than replacing that review; empty
  spreadsheet rows are skipped; a sheet saved with semicolons, or not as UTF-8,
  gets a sentence saying to save it as CSV UTF-8 (comma delimited). See
  [review in a spreadsheet](docs/processing-spec.md#review-in-a-spreadsheet).
- A reviewer can correct a page's text, in the view or with `correct:FILE` in a
  sheet. The correction is a new file; the engine's reading is never edited.
  The receipt names what it started from and the words changed (`--dry-run`
  prints them first, naming the reading by its engine), `verify` checks it,
  and the report, transcript and exports mark the text as a person's: a
  corrected page's export header says whose correction of which reading the
  text is, and gives the corrected reading's ID and hash after it. A plain JSON
  receipt cannot replace a corrected page's text, and a receipt whose word
  counts do not match its texts fails `verify`. See [correct the text](docs/processing-spec.md#correct-the-text).
- A moved or copied job can be verified, exported and reviewed where it is.
  `verify` reports `job_relocated`, and a review or adjudication made on a copy
  is recorded (`relocations`) and reported as `job_changed_elsewhere`. See
  [moved and copied jobs](docs/processing-spec.md#moved-and-copied-jobs).
- One table of plain words for every hold, page state, review decision and
  warning, shared by the report, `process`, `inspect` and the review view, and
  repeated in [warnings](docs/warnings.md#in-plain-words). Where the job
  records a hold's cause, the hold is said by it: which reading, and what is
  wrong with it ("Tesseract OCR (rus) wrote a capital Ъ or Ь inside words, as
  OCR for modern Russian does with pre-1918 letters"); when the flawed reading
  is an earlier OCR layer and not the selected text, that nothing confirms the
  selected text, and what a person can do. See
  [what is wrong with a reading](docs/warnings.md#what-is-wrong-with-a-reading).
- JSONL exports give each page's `state`, `review_reasons` and `warnings`;
  TEI marks each page's state, holds and warnings, and gives each page's
  reading, the SHA-256 of its text and its reviewer in a provenance note.
  Text and Markdown export headers use the report's words: `State:`, and a
  `Reading:` line saying what made the reading ("the PDF's own text (an earlier
  OCR)") before naming the engine. An export claims an edition only when the
  page's text is the edition. See [export](docs/export.md).
- `review --apply` says what it recorded in plain words (accepted, corrected,
  blank and so on), how many pages still need a person, and the next command:
  `export` once none do. The earlier name `review-job` keeps its output. See
  [review](docs/cli.md#review).
- `export` takes the format from the end of `--out` (`.txt`, `.md`, `.jsonl`,
  `.xml`) when `--format` is not given. See [export](docs/cli.md#export).
- `review --apply` warns (`blank_with_text`) when a page recorded as blank has
  a reading of 50 or more words. See
  [troubleshooting](docs/troubleshooting.md#a-review-is-refused).

The contract

- [Stability](docs/stability.md): what stays stable from 1.0, what does not,
  the deprecation rule, and which older runs and jobs verify. Tests hold the
  package to it and to runs and jobs written by 0.5.2, 0.6.0, 0.7.0 and 0.8.0.

### Changed

- **Commands have single-word names:** `inspect-job` and `inspect-run` are
  `inspect`; `verify-job` and `verify-run` are `verify`; `review-sheet --out`
  is `review --sheet`; `review-job --review` is `review --apply`;
  `init-config` is `init --run` and `init-config --processing` is `init`;
  `compare-runs` is `compare`. `verify` and `inspect` tell a job from a run by
  its folder. The earlier names work as before through every 1.x release, but
  `--help` no longer lists them. `--help` (or `pageledger help`) lists only the
  commands for reading a document and setting up; `pageledger help pipelines`
  lists the commands for runs and `adjudicate`. A run command given a job, or
  a job command given a run, says which kind of folder it is and which command
  fits. See [earlier command names](docs/cli.md#earlier-command-names) and
  [the CLI reference](docs/cli.md).
- **A folder of JPEG or PNG files is one document,** not a set of one-page
  documents, and every file in it must be a page image. An adapter that reads
  images itself gets JPEG and PNG only; TIFF, JPEG 2000 and WebP are refused. See
  [page images](docs/process-a-collection.md#page-images).
- **`process` refuses an unsupported source before the job exists.** A source
  is a PDF, page images, or text that reads as UTF-8; anything else, such as a
  `.docx` or a folder without page images, is a usage error and no job folder
  is written, where 0.8 wrote a halted job. So is an empty file of any kind.
  A `.md` source no longer gets OCR. See
  [troubleshooting](docs/troubleshooting.md#is-not-a-pdf-a-page-image-or-utf-8-text).
- **New jobs record hold policy 0.9.** An earlier OCR in a PDF's text layer is
  recognised in any font and kept as a witness beside the job's own OCR: its
  disagreement holds the page, and two of the job's engines that agree outvote
  it. A reading that lost the pre-reform ѣ another reading kept is held. When
  every reading is held, the best supported one is shown. A reading that never
  finished raises no hold, `suspicious_embedded_text_delta` measured against an
  earlier OCR layer holds neither reading, and no climb is recorded toward a
  stage the job does not have. A typeset text layer held only for a few stray
  characters (at most 1% of its letters) stays the selected text, held, with
  the OCR as a witness; earlier policies replaced it with the OCR. Jobs written
  under earlier policies rebuild unchanged. See
  [hold policy 0.9](docs/processing-spec.md#hold-policy-09).
  `pdf_text` marks these layers `ocr_text_layer` in plain runs too, so more of
  their pages enter the review queue.
- **New jobs write report format 0.9:** `report.md` is the record of how the
  text was made, with a summary on top. It opens with what happened, the pages
  that need a person and why in plain words, and the one next command, to run
  in the job's folder; the source by its file name, its path written
  out once; then one table of the pages (what holds each, its selected text,
  its review); then the record, a short list per page with every hold and its
  cause, reading ID, outcome, warning and comparison, and hashes and custody
  last. Readings are named by what made them, from `engines`, which
  `document.json` gains with `hold_causes`; a scan page that another reason
  holds is no longer also called blank, though the record keeps the reason. A
  25-page scan's report falls from about 300 lines to about 120. `transcript.md`
  names each selected reading the same way. `process` ends with a few plain
  lines: what was read, how many pages need a person and why, where the job is,
  and the next command. `process`, `inspect`, the review view and the exports
  count pages that need a person the same way, and a page still waiting for a
  stage of an unfinished job is shown as not finished. Reports of older jobs
  render as before. See [report format 0.9](docs/document-report-spec.md#report-format-09).
- **`verify` reports more and needs less.** It no longer needs the source: a
  missing one is the warning `source_missing`, one found through the source
  index `source_relocated`. Its result has a `warnings` list, which can also
  hold `job_relocated`, `job_changed_elsewhere`, `carrier_not_reproduced`,
  `iiif_snapshot_incomplete`, `iiif_record_changed` and `iiif_snapshot_grown`;
  the status stays `pass`. A job that did not finish fails with
  `"reason": "unfinished"` and says to run `resume`. A `raw/`, `partials/` or
  (without a schema) `normalized/` folder that Git or an archive tool dropped
  because it was empty is read as empty. A pass says what it means: "The
  record is intact: every file matches what was recorded. This does not check
  that the text is right." A failure names the changed file (`transcript.md`,
  `report.md`, or a reading and its page). See
  [troubleshooting](docs/troubleshooting.md#verify-reports-a-problem).
- **`verify` on a run derives its normalized records, grades and review and
  quarantine queues again** from the readings and the config, and fails when
  one was edited. A run whose provenance has lost a page's source identity or
  route now fails, where it was a warning. Runs written by an earlier release
  whose rules differ get the warning `derived_evidence_differs`, naming the
  file, and still pass. Hidden files that Finder or a copy to exFAT leaves
  (`.DS_Store`, `._*`) are named in the warning `stray_file` and no longer
  break `verify`; a damaged recovery record, a deeply nested file or an
  unreadable source is reported, not a crash (an unreadable source is the
  warning `source_unreadable`). See [verification](docs/artifacts.md#verification).
- `review --apply` and `adjudicate --decisions` work on a moved job and without
  its source. `resume` refuses a job that is not in the folder it was written
  in. See
  [moved and copied jobs](docs/processing-spec.md#moved-and-copied-jobs).
- **`export` writes no machine text for a page a person reviewed as blank, an
  illustration, handwriting, unreadable or a source defect** (null `text` in
  JSONL, a `<gap/>` in TEI). Before, `--reviewed-only` exported such text as if
  checked: a page reviewed as blank was exported with 1,295 words of OCR. See
  [export](docs/export.md).
- `render_upsampled` applies to PDF runs too: a new `pdf_ocr` run of a scan
  under 200 pixels per inch warns on every page, which in a plain run puts
  each page in the review queue and lowers its grade. `pdf_ocr` may now render
  a page image below 72 DPI, down to half its own density, instead of stopping
  with `render_limit`. See
  [page size and rendering](docs/ocr-options.md#page-size-and-rendering).
- `doctor` opens with what `process` can do on this machine (PDF text, which
  OCR engine, the common languages installed for `--lang`, optional engines
  not installed) and stops there; `doctor --all` adds the details as before;
  `--json` is unchanged. See
  [doctor](docs/cli.md#doctor).
- A new job makes `partials/` only when it keeps a partial reading. See
  [artifacts](docs/artifacts.md).
- A review file for another document, or one that is not a review receipt, is
  refused saying which. See
  [troubleshooting](docs/troubleshooting.md#a-review-is-refused).
- The README leads with `process`, and [the documentation](docs/README.md) is
  arranged by task; "Choose OCR settings" is folded into
  [non-English and historical OCR](docs/multilingual-ocr.md) and
  [OCR options](docs/ocr-options.md).

- **Jobs, runs and bundles you receive.** `resume` and `replay` no longer
  run Python code that a job, run or bundle names: such an adapter runs only
  from a folder you name with `--adapter-path`, never from the received folder,
  and must be defined there. They no longer send page images, or an API key, to
  a model server off this machine that the record names, unless you name it
  with `--trust-endpoint URL`; the refusal gives the option. `verify` says when
  an unfinished job names its own adapter code. Both refusals come before
  anything changes. Your own `--config` stays trusted. See
  [jobs, runs and bundles you receive](docs/share-and-cite.md#jobs-runs-and-bundles-you-receive)
  and [SECURITY.md](SECURITY.md).
- **Nothing is written over.** `export --out` refuses the job's source, a path
  inside the job, a folder or link, and a file that is not an earlier export of
  the same job (it now works through symlinked folders such as `/tmp`).
  `init --out` and `classify --out` refuse an existing file.
- **Page text cannot pass for structure.** `transcript.md` and the Markdown
  export put each page's text in a fenced block, so a line in the text such as
  `## Page 2` or `<!--` cannot pass for a heading or hide a page, and names
  are escaped. The text export gives each page's length in characters and
  lines, so the text can be cut out exactly. The JSONL export escapes U+2028,
  U+2029 and U+0085, and TEI keeps them. `inspect --csv` cannot carry
  spreadsheet formulas.
- **`fetch` records no credentials.** A URL with a key, token or password in
  its query is refused before any request, and such a value in a URL a server
  redirects to is recorded as `redacted`.
- **Text files keep their bytes.** A text file's reading keeps its line
  endings (CRLF and CR stay), so its hash matches the source page; a
  byte-order mark and a closing form feed no longer make text or an empty
  page; a file that is not UTF-8 is refused by `process` before the job exists,
  and by `run` with `unreadable_text`. The `text`
  and `pdf_ocr` adapters are version 0.2 (`pdf_ocr` also asks Tesseract for no
  page separator, and a sharp logo no longer raises its render resolution).
- **Right-to-left rows.** `alto`, RapidOCR's `rows` order (adapter version
  0.2) and tiled readings write a Hebrew or Arabic-script row from its
  right-hand end instead of reversing it, and the reading's engine description
  says how many rows were read that way. Columns are still taken left to right.
- **Budgets.** Under `max_usd` or `max_tokens`, a run stops before the next
  request when a paid adapter did not report what the last page used (new key
  `run.budget.unknown_usage: stop | continue`). Adapters that run on this
  machine are exempt: RapidOCR, and `vision` when its server is local, now
  declare `local`. Every budget stop is written to `run.log`, and `max_pages`
  counts only pages the adapter will read, so `--dry-run` is never refused for
  it. A misspelt key under `run.budget` stops a new run; misspelt keys under
  `run.retry`, `run.pricing`, `run.grading` and `classify` are warned about.
- **Engine agreement in new jobs keeps combining marks,** so readings that
  differ only in vowel signs, viramas or points (Hindi, pointed Hebrew) no
  longer count as agreeing. The Russian lexicon judges pre-reform and stressed
  words whole in new jobs.
- **Climbing under hold policy 0.9.** A page whose readings are all held
  climbs to the next stage whichever is shown first; a typeset layer kept for a
  few stray characters no longer buys a model reading that could not replace
  it; a local reader that fails with nothing after it is recorded as
  `provider_failure`, and held pages name only stages the job has.
- **Schema alignment** removes a separator only where it groups thousands
  (thin and narrow no-break spaces included), so `1,5` and `12 34` are recorded
  as coercion errors instead of read as 15 and 1234.
- **`rerun`** keeps each page's type, prompt and review hold from the earlier
  run, and refuses a rerun plan naming pages the earlier run did not route.
- **A run whose adapter records no reproducibility profile** (`alto`,
  `rapidocr`, `indra`) can be bundled; replay compares its output as evidence
  (`evidence_compared`) and never calls it exact. `replay` stops after 6 hours
  without a result (`replay_timeout`) and says which kind of error ended it.
- `doctor` lists the languages `--lang` takes (not `osd` or `equ`) and, with
  `--config`, only the API keys that config needs; a missing or unparsable
  config is named in a sentence. Bare `pageledger` shows the commands; usage
  mistakes print their sentence without the usage line; a missing review or
  config file is named in plain words. After an interrupt, the command to
  continue keeps `--adapter-path` (and `fetch`'s keeps `--pages`, `--delay` and
  `--max-image-bytes`).
- `indra` is no longer accepted as the layout for tiling, which it never
  provided.
- Report wording in format 0.9: a flawed other reading "cannot confirm the
  selected text"; a page with very little text is no longer said to have had an
  empty reading; engine agreement is again said to be evidence, not proof;
  render advice no longer asks for more pixels when the render already exceeds
  the scan; the `orus` advice says it made more errors overall in testing;
  doc references are links to the release's documentation.

### Fixed

- A job whose stage could not start (an adapter that will not load, a folder
  that cannot be made) says why and records the stage as `not_started`;
  `resume` then reads those pages, for which nothing was sent. Resuming a
  custom-adapter job without `--adapter-path` no longer halts it for good.
- With `processing.batch`, a reader that fails on one page no longer skips the
  other pages of its batch, and a failure part-way through a batch no longer
  leaves a stale report.
- A review applied to an interrupted job no longer stops it for good: `resume`
  finishes it. `process --review` with a review file that does not fit leaves
  no half-made job folder.
- `fetch` stops a server that keeps a response trickling in, and no longer
  retries a refused connection, a certificate that fails or an unknown host
  name: it says which, stops asking for further canvases, and suggests
  `--resume` only when there is a fetch to resume. It accepts `HTTPS://` in
  capitals; deeply nested manifest labels no longer make an unusable fetch
  record; a mistyped huge `--pages` range is refused at once, here and in
  `process` and `run`; control characters in a manifest's ids are shown
  quoted.
- The `vision` adapter keeps the readings and tiles it paid for when an
  endpoint reports a nonsensical usage number or a too-deeply nested body,
  stops a response that trickles past `timeout_seconds`, and refuses an
  over-long prompt (more than about 64 KB) before any page is paid for.
- A JPEG page image that ends before its image data is refused as damaged, as
  `fetch` already refused it.
- `compare` and `bundle` refuse damaged run files with a sentence, and
  `compare` says when no grades could be compared instead of listing every page
  as a grade change.
- A malformed configuration value (a list where a word belongs, a misnamed
  later `adapter_order` step) is refused with a sentence, and a custom
  adapter's import error shows its cause.
- `foreign_script_characters` no longer counts the spacing caron (ˇ) that some
  LaTeX fonts write beside a letter for "č": a born-digital page citing archives
  in transliteration was held and sent to OCR. See
  [warnings](docs/warnings.md#quality-warnings).
- One timeout of the `vision` adapter halted a job. The page is now asked for
  once more and, if that fails, held as `provider_failure` while the job goes
  on, as 0.8 promised; a tiled page keeps the tiles already read. See
  [failed remote reads](docs/processing-spec.md#failed-remote-reads).
- An image page that failed just as `max_image_pages` was reached lost its
  retry; it now gets it after `resume --raise-limit`. A page a person reviewed
  while the job was paused is not read again. See
  [failed remote reads](docs/processing-spec.md#failed-remote-reads).
- `review --apply` also refuses a sheet note starting with a tab or carriage
  return, which a spreadsheet can read as a formula. See
  [review in a spreadsheet](docs/processing-spec.md#review-in-a-spreadsheet).
- A resumable run, and so every job, of a folder holding `.DS_Store` or other
  hidden files failed its own checkpoint check. See
  [page images](docs/process-a-collection.md#page-images).
- `resume` tells a job from a run as `verify` and `inspect` do. See
  [resume](docs/cli.md#resume).
- Clearer messages for a missing source, an existing output folder, a page
  range past the end, a missing Tesseract language (with close installed
  codes), and what `bundle` and `compare` refuse; a renamed review sheet finds
  its binding when only one matches; pypdf's repair notices are summarized. See
  [troubleshooting](docs/troubleshooting.md).

### Compatibility

- New fields are optional and new files are additions: among them `iiif`,
  `relocations`, `hold_causes`, `layout_sha256`, `native_pixels`, `page_tiles`,
  a receipt's `correction`, a packet's `terms`, and the schemas `iiif-fetch`
  and `review-decisions`. See
  [the schemas](schemas/).
- Jobs and runs written by 0.5.2 to 0.8 verify, rebuild and render as before,
  under the hold policy and report format they recorded, including after a
  move. New jobs record hold policy `0.9` and report format `0.9`. See
  [older runs and jobs](docs/stability.md#older-runs-and-jobs).
- Earlier command names work through 1.x, and `process --json` output and
  exit codes are unchanged. `verify` can now pass where 0.8 failed: on a job
  or run whose source is missing (`source_missing`) and on a moved job
  (`job_relocated`). A script that needs the source present must check the
  `warnings` list, not only `pass`. An unfinished job still fails. See
  [verify](docs/cli.md#verify).
- `fetch` lets only the host of the URL you name be a loopback, private or
  link-local address, and under an `https` URL fetches nothing over `http`
  (an `http://` image service is asked for over `https`; the record keeps the
  declared id). A service or redirect that breaks either rule fails its canvas. See
  [IIIF limits](docs/iiif.md#limits).
- Built-in adapter versions that changed: `text` and `pdf_ocr` 0.2 (line
  endings kept, no closing form feed, render resolution), `rapidocr` 0.2
  (right-to-left rows); `rapidocr` and a local `vision` server now declare
  `local`, and `indra` no longer declares `layout`. Their extractor identities
  differ from 0.8's, so replaying an older bundle made with them reports an
  identity mismatch.
- A new required adapter attribute, or a change to the `extract()` call, now
  waits for a major release. See [the adapter protocol](docs/adapter-protocol.md).

### Known limits

- One OCR engine's unconfirmed reading of an image-only scan passes unless it
  carries a warning of its own, as do two-page spreads read across the gutter
  and books re-typeset from OCR without their scans; confidence is judged per
  page, not per word. See
  [hold policy 0.9](docs/processing-spec.md#hold-policy-09).
- A born-digital page whose text lies under an opaque page-sized image, or one
  made in an OCR program's editor, is taken for an earlier OCR layer. See
  [hold policy 0.9](docs/processing-spec.md#hold-policy-09).
- `alto` reads one page per file; `verify` does not read the ALTO file again;
  its boxes are checked against the job's OCR only when tiles are planned from
  them. hOCR, PAGE XML, ALTO as the contest witness and ALTO found through a
  IIIF manifest are not supported. See
  [capabilities and limits](docs/capabilities-and-limits.md#extract-pages).
- A job on a IIIF snapshot that a later `fetch --resume` grew cannot be
  resumed. See
  [process the snapshot](docs/iiif.md#process-the-snapshot).
- `render_upsampled` judges a PDF page's largest image by its proportions, not
  its drawn size, so a small picture with the page's shape on a born-digital
  page is warned about. See
  [page images](docs/capabilities-and-limits.md#page-images).
- Tiles follow a layout, never a blind grid; only the `image` stage and the
  `vision` adapter read tiles; column order assumes left-to-right print, so a
  right-to-left page in several columns reads its columns in the wrong order. See
  [capabilities and limits](docs/capabilities-and-limits.md#process-a-document).
- `classify`, `bundle` and `replay` refuse page images or runs of them
  (`classify --from-run` works on such a run). See
  [page images](docs/capabilities-and-limits.md#page-images).
- A bundle replays exactly only where the recorded operating-system release
  and Python patch version match, and the Tesseract identity is the hash of
  whatever `tesseract` is on PATH. `fetch`'s private-address check can be
  passed by a host whose name changes its answer between lookup and connection,
  and a server that sends its headers slowly is bounded only per read. `verify`
  does not re-derive a run's text-quality warnings, and holds a run to the
  rules of the version its manifest names, which is not itself hashed. One
  warning of any kind grades a run page B, and a table whose required cells
  are all empty can still grade A or B on the schema axis. See
  [known limits](docs/capabilities-and-limits.md#known-limits).

### Measured

Measurements on the pages named, not accuracy figures:

- Hold policy 0.9 was set on 74 pages of 20 research PDFs, each classed against
  its scan. Pages left wrong and not held fell from 28 under 0.8 to 8; wrong
  and held rose from 34 to 54; no right page was held. The 12 right pages are
  all born-digital, so no right OCR page was in the sample. Rebuilt under 0.9,
  the 135 pages of the 0.8 dogfood keep every hold, and its 5 right pages
  still pass. See [measured on the 74 dogfood pages](docs/processing-spec.md#measured-on-the-74-dogfood-pages).
- `latin-print-0.1` settled 81 of 1,118 spans on the 54 directory tiles its
  letter pairs were chosen on (not an independent test); the 8 settlements
  checked were right. On 20 crops of later directories it settled 1 of
  720. See
  [contest](docs/processing-spec.md#contest-a-models-reading).
- On those tiles a general English word list knew 56 to 58% of the words in
  good and bad readings alike, so `rough` does not help on directories. See
  [a word list as the lexicon](docs/processing-spec.md#a-word-list-as-the-lexicon).

## 0.8.0 - 2026-10-19

### Added

- A built-in `indra` adapter for [Indra OCR](https://huggingface.co/datalab-to/indra-ocr), a small OCR model
  that runs on the CPU: `pip install 'pageledger[indra]'`. It reads one page at a time in `mode: ocr` (the
  default) or `auto`, at a chosen `dpi`, optionally with `math: true` (formulas as LaTeX) or `output:
  markdown`. Provenance records the indra-ocr version, a hash of the model files, the mode and the dpi; line
  confidences become quality evidence, with a line Indra found but could not read counted as unread, and the
  layout blocks, rotation and skew are kept with them. The model weights are not shipped and have their own licence
  (modified OpenRAIL-M); `THIRD_PARTY_NOTICES.md` and `doctor` say so.
- Two optional ladder stages between local OCR and the image stage: `second_ocr`, a second literal engine,
  and `local_image`, a vision model on this machine read through the `vision` adapter. A page climbs one rung
  at a time on the configured triggers. The local model has its own budget, `max_local_image_pages`, keeps
  the image it read as evidence, and a failed local call hands its page to the next stage instead of halting
  the job. From `local_image` a page climbs to the paid `image` stage only on evidence against the local
  reading (a hold, or disagreement with a literal reading); `always` does not lift a clean local reading. In
  a contested job the paid reader is the base over the local one. See
  [the ladder](docs/processing-spec.md#the-ladder-a-second-ocr-engine-and-a-local-model).
- Contest rule set `ru-print-0.2`: a grouped span (an unpaired run, or words only one side has) with a digit
  on either side is a `number`, as paired words always were. `ru-print-0.1` is unchanged.
- `model_commentary` and `partial_transcription` warnings for a generative reader's own remarks: a note about
  the image or its legibility, or a bracket saying what it left out. Both hold the page as a coverage defect.
- `ocr_text_layer`, a `pdf_text` warning for a page whose text layer is drawn in an OCR font
  (`GlyphLessFont`: Tesseract, OCRmyPDF and Internet Archive scans), that is, an earlier OCR of the scan. Like
  any warning, it puts the page in a plain run's review queue and counts against its signals grade, so a `run`
  of such a scan with `pdf_text` now queues every page that has layer text.
- `doctor` reports the `pymorphy3` (`ru` extra) and `indra_ocr` packages, with the extra that installs each.
- `processing.batch: {pages, concurrent}` rebuilds the job index once per batch instead of after every page, and
  reads `image` and `second_opinion` pages concurrently. Records are those of an unbatched job. See
  [batches](docs/processing-spec.md#batches-and-concurrent-readers).

### Changed

- New jobs record hold policy `0.8`, which adds four rules. A text layer that two agreeing later readings
  both disagree with is held, and the length warning those readings carried for differing from it is set
  aside, so a junk layer no longer wins the transcript. A reader that returns under half the literal OCR's
  words is held as incomplete. A page with an `ocr_text_layer` climbs to local OCR (trigger `ocr_layer`), so
  an inherited OCR is checked instead of accepted. And in a ladder with a `second_ocr` stage, a local OCR
  reading that no other clean reading confirms climbs to the second engine, and on to a reader when that
  engine's own reading is held (trigger `uncorroborated`); an inherited OCR layer that agrees counts as
  confirmation. One engine's unconfirmed reading was how five of the six wrong-but-accepted truth pages of the
  0.8 dogfood got through. Jobs written under 0.6 and 0.6.1 are rebuilt by their own rules and verify
  unchanged.
- `pdf_text` counts pages from the page tree and reads only the pages a job selects. A `--pages` sample of a
  long PDF no longer extracts the whole document's text first.
- A remote reader's page that times out or meets a network, HTTP or availability error is requested once more;
  if the retry fails too, the page is held as `provider_failure` and the job goes on. Any failure of the
  optional `second_opinion` witness now holds only its page. 0.7 halted the whole job, and the vault's intake
  then started the document again: one Haiku timeout on page 159 of 274 cost a seven-hour job. See
  [failed remote reads](docs/processing-spec.md#failed-remote-reads).

### Fixed

- An optional second opinion that reports `MODEL_QUOTA` without an HTTP status (a subscription reader's usage
  limit) now holds its page like a 429 instead of halting the job.
- `process` builds every configured stage's adapter before the job exists, so a bad option on a later stage
  (a vision `timeout_seconds` above 600, say) fails at once and names the stage, instead of halting the job when
  the first page reaches it.
- A job halted while a child run was still setting up can be verified. The job recorded
  `attempt_stopped:<stage>:<error>`, but `verify-job` rebuilt it as `initialization_incomplete` and refused the
  job's own report.
- `adjudicate --decisions` records an answer without a `note` with an empty note, and names a refused decision by
  its position and what is wrong with it, instead of refusing the page as "an invalid or repeated decision".

### Compatibility

- `job.schema.json` and `document.schema.json` accept the stages `second_ocr` and `local_image`, hold policy
  `0.8`, the triggers `ocr_layer` and `uncorroborated`, the usage field `local_image_calls` and the limit `max_local_image_pages`.
  The new stages and limit appear in a job's policy only when configured, so older configs compile to the
  policy their jobs recorded.

## 0.7.1 - 2026-10-07

### Added

- A source index finds originals that have moved since a run or document job
  started. Each line of the TSV maps a SHA-256 to an absolute path; PageLedger
  reads `PAGELEDGER_SOURCE_INDEX` (one or more files, separated by the platform
  path separator) or, when that is unset, `~/.config/pageledger/source-index.tsv`.
  `resume`, `verify-job`, `rerun` and `bundle` read the indexed file only when
  its digest matches the recorded one. Checkpoints, `job.json`, manifests and
  provenance keep the recorded path; nothing sealed is rewritten. See
  [moved source files](docs/checkpoint-spec.md#moved-source-files).
- `verify-run` reports a source found through the index as the warning
  `source_relocated` instead of `source_missing`.

### Changed

- A document job binds its stage runs to its source by SHA-256 and page count
  rather than by path, so a stage started after the source moved, which records
  the path it actually read, still belongs to the job.

## 0.7.0 - 2026-10-01

0.6.1 was prepared but never published; its changes ship in 0.7.0 and are
listed here.

### Added

- Document jobs can climb on more than warning holds. `processing.escalate_on`
  adds `rough`, for a reading whose share of known words falls below a
  threshold (the Russian lexicon comes with `pip install 'pageledger[ru]'`);
  `disagreement`, for clean readings that disagree, which used to go straight
  to review; and `always`, which sends every page to the image stage. Triggers
  lift a page at most to the image stage. Each one is recorded on the page with
  its evidence, and the report shows the work done at each stage.
- `processing.contest` compares a model's reading with the literal engine's,
  word by word, and keeps each difference as a span in
  `contested/<page_id>.json`. Look-alike letters and RapidOCR's listed glyph
  confusions are settled by rule; a literal non-word those confusions do not
  explain stays open, because it may be the printer's misprint. A page with open
  spans is held as `contested`, and `verify-job` rebuilds the spans. A job that
  contests readings takes the model's text, checked against the OCR, even where
  the OCR reads clean.
- `pageledger adjudicate` settles contested spans against the page image.
  `--packets` writes each contested page's open spans and the image the model
  read, blind to which engine read what; `--decisions` records the answers as
  receipts bound to those spans. When no span is open the page is
  `adjudicated_text`, kept apart from `reviewed_text`, and its edition (the
  model's reading with the settled changes applied) is what the transcript and
  exports carry. `verify-job` rebuilds each edition from its receipts. On a
  hand-checked Russian article, OCR differed from the checked text in 91 words,
  the model's reading in 17, and the adjudicated edition in 4.
- A `foreign_script_characters` warning for a few letters from a script that
  doesn't belong on the page, usually a math font read through the wrong
  character map. On the test documents it found all nine pages where 0.6 found
  two.
- `reading_order: columns` for the `rapidocr` adapter reads a page set in
  columns one column at a time, after cutting the page at its whitespace. The
  default, `rows`, still reads across the page, which suits tables. On a
  two-column journal article the default glued the halves of neighbouring lines
  together without any warning.
- `doctor --config FILE` also reports, without their values, the environment
  variables that config's adapters name through `env_key`, such as a model
  gateway's own key. Before, doctor listed only four fixed provider keys.

### Fixed

- A scanned page with no text layer no longer waits for a person once OCR reads
  it without concern. The empty text layer used to leave a `blank_candidate`
  hold on every such page, so a whole scanned book showed as "needing a person"
  even where OCR had read each page. Only a hold raised by an empty reading is
  cleared, and only by a non-generative engine; an engine's own judgement that a
  page is blank stays. New jobs record `hold_policy: "0.6.1"`;
  jobs written by 0.6.0 keep their holds and still verify.

### Documentation

- Troubleshooting covers an exit code 134 that ONNX Runtime can raise while
  Python shuts down after a finished `rapidocr` job: verify the output and
  decide from that.

### Compatibility

- `job.schema.json` and `document.schema.json` accept `hold_policy` `"0.6"` or
  `"0.6.1"`, and the new page fields `triggers`, `contested`, `edition` and
  `adjudications`, present only in jobs that configure them. Artifacts written
  by 0.6.0 verify unchanged.
- New schemas: `contested.schema.json`, `adjudication-packet.schema.json` and
  `adjudication-decisions.schema.json`. Adjudication receipts are their own
  record (`schema_version: "0.2"`), separate from human review receipts.
- The `ru` extra pins `pymorphy3` and its dictionary exactly: a job records its
  lexicon and refuses to verify under another one.
- The rule set name (`ru-print-0.1`) versions the whole contest procedure. A
  later change that alters spans takes a new name, and a job contested under an
  older procedure refuses to continue rather than orphan its receipts.

## 0.6.0 - 2026-09-28

### Added

- Documentation for working with a real collection: a glossary; a reference of
  every warning, hold and disposition with its likely cause and what to do;
  how-to guides for processing a collection, choosing OCR settings, planning
  time and cost, troubleshooting, and sharing and citing results; and engine
  recipes. The documentation index is grouped into tutorials, how-to guides,
  reference and explanation, and maintainer records moved to
  `docs/maintainers/`. The PDF/OCR tutorial now runs in CI against a generated
  scan, and README links work on PyPI. Capabilities and limits are grouped by
  task, and each recovery rule is stated once: for runs in the checkpoint
  specification, for document jobs in the processing specification.
  `docs/examples/processing.yml` now matches the configs shown in the README
  and processing specification (a job pauses after 100 attempted pages), a
  test keeps them identical, and its optional image stage uses the built-in
  `vision` adapter.
- Warnings for text layers that exist but carry little of the page:
  `digits_only_text` (a table layer that kept its digits and lost its words),
  `mixed_script_tokens` (Latin look-alikes inside Cyrillic words, or the
  reverse), `private_use_characters` (characters lost to font-specific code
  points, such as old-style digits) and, across a run, `repeated_page_text`
  (the same short stamp on three or more pages). Quality lines gain
  `letter_count`, `digit_count`, `mixed_script_token_ratio` and
  `private_use_count`. The thresholds were measured on 5,702 sampled pages
  from a research library before they were set, and `process` treats these
  warnings as coverage defects, so such a page moves on to OCR.
- Warnings for the ways vision models fail. `repetition_loop` flags a model
  stuck in a loop: one line on much of the page, an ending repeated 20 or more
  times, or one letter repeated 40 times. On 287 outputs of 24 transcribed
  pages by 11 engines it flagged 16 pages, every one a loop, and nothing from
  classic OCR engines, hosted models or the reference. A new top-level
  `language` block enables `script_mismatch`, for a page read mostly in another
  script, and with `orthography: prereform`, `historical_letters_lost`, for
  pre-reform text returned in modern spelling; PageLedger does not guess a
  language. Quality lines gain `largest_identical_line_count`,
  `longest_repeated_tail_length` and `longest_letter_run`, and document jobs
  hold such pages as coverage defects.
- Document jobs compare the engines' readings of each page. When the selected
  reading and another clean one agree on fewer than 60% of words, the page is
  held as `engine_disagreement`; when a number differs, as
  `numeric_disagreement`. A reading from a generative adapter, such as
  `vision`, stays in review as `unconfirmed_model_output` until another engine
  agrees with it. A plain `run` holds every page a generative adapter read in
  its review queue, with the same reason. `processing.benchmark` runs a stage
  on every Nth page even when it is not needed, to compare engines on a
  sample.
- A page type can set `review: true` to be extracted and still held for
  review: after extraction the page joins the review queue with reason
  `route_review:<type>`. `classify` copies the flag into route maps; route
  maps without it behave as before.
- Engine recipes (`docs/engine-recipes.md`) for putting a stronger engine than
  Tesseract behind PageLedger: a local vision model through `mlx_vlm.server`
  or llama.cpp, RapidOCR with the PP-OCRv5 Cyrillic recognizer (the built-in
  `rapidocr` adapter below), Tesseract's community `orus` model for
  pre-reform print, Apple Vision on macOS (`examples/apple_vision_adapter.py`)
  and hosted models through a gateway, each with what it was measured to do
  well and badly on real pages.
- A built-in `rapidocr` adapter reads scans with RapidOCR and a PP-OCRv5
  recognizer you supply, such as the Cyrillic one that keeps pre-reform
  letters. Install it with `pip install 'pageledger[rapidocr]'`; it runs on
  the CPU and needs Poppler. `pageledger doctor` reports whether RapidOCR and
  ONNX Runtime are installed.
- A built-in `vision` adapter reads pages with a vision model behind an
  OpenAI-compatible endpoint, on this machine (`llama-server`,
  `mlx_vlm.server`) or hosted. It renders each page with Poppler within the
  request's size limit, sends pages to another machine only over `https://`
  with `allow_remote: true`, reads the key only from a named environment
  variable, and refuses redirects. Finish states become typed failures,
  including the new `MODEL_CONTENT_FILTERED` and `MODEL_RECITATION`. In
  document jobs' image stages it keeps the exact JPEG sent as image evidence.
  `reasoning_effort` asks a model that reasons before answering to reason
  less, so its token budget goes to the reading.
- `pageledger review-sheet JOB --out review.csv` writes a CSV for reviewing a
  job in a spreadsheet: a link to each page, its disposition, the start of its
  text and its review reasons, with empty `decision` and `note` columns.
  `review-job --review review.csv --reviewer NAME` records the decisions after
  checking the whole sheet against the evidence each row was written from,
  which a binding file beside the sheet records. A stale, duplicated or
  formula-bearing row stops the import before anything is recorded, and
  `--dry-run` checks and counts the decisions without recording them.
- `pageledger export JOB --format txt|md|jsonl|tei --out FILE` writes a
  verified job's selected text page by page, with each page's review state,
  the attempt and engine that produced its text, and the text's SHA-256.
  `--reviewed-only` keeps only reviewed pages. The TEI is a minimal TEI P5
  document with a `<pb>` for every source page and reviewed and unreviewed
  pages marked apart. Links to the source are relative to the exported file.

### Changed

- Document reports list the pages that need a person first, then the rest, and
  show separately the text the policy selected, whether engines agree on it,
  and who reviewed it. Reports written before 0.6 keep their layout.
- Document reports name the source by its path relative to the job directory,
  so a shared `report.md`, `transcript.md` or `document.json` no longer shows
  where its owner keeps files. New reports record `report_format: "0.6"`;
  reports written by 0.5.1 keep their absolute paths and still verify.
- `init-config` now extracts `sparse`, `table_likely` and `unknown` pages with
  `review: true`. They were previously routed to review without extraction,
  so a first `classify` and `run --routes` on a statistical volume extracted
  none of its tables.
- `run` now stops with "No extraction route" when a config has no
  `taxonomy.page_types` and neither `--routes` nor `--adapter` is given.
  Such a run previously sent every page to review, extracted nothing and
  exited successfully. Dry runs and deliberate review-only taxonomies still
  work. Documentation and example configs that omitted the taxonomy now
  include it or say where to paste them.
- `process` rejects `run` settings that a document job would ignore: budget,
  pricing, grading, `rerun_if`, `quarantine_if`, adapter options, rerun depth
  and consecutive-failure limits. A `run.budget.max_pages: 1` previously let a
  job process every page without a warning. A leftover `run.adapter` or
  `taxonomy` now produces a warning, and the processing examples drop both.
- The `pdf` extra now installs `pypdf[crypto]`, which adds the `cryptography`
  package. AES-encrypted PDFs that open without a password, typically files
  that only restrict printing or copying, previously failed in `pdf_text`, in
  PDF page counting and in `process`; 20 of the library PDFs in our test
  collection were such files. `process` now accepts them too.
- In document jobs, a page with the `low_confidence` warning now gets its own
  hold, reported as "The engine was unsure of some words". It previously
  shared the `coverage_defect` hold and the wording "Possible missing or
  incomplete content", which claimed more than an engine's doubt shows. New
  jobs record `hold_policy: "0.6"`; jobs written by earlier versions keep
  their original holds and still verify.
- A document job that reaches a processing limit now pauses instead of
  halting. A `max_attempt_pages` smaller than the first batch previously
  stopped the job before any page ran, and a halted job could not continue.
  The job now processes the pages the limit allows and ends `paused_budget`;
  `pageledger resume JOB_DIR --raise-limit max_attempt_pages=N` records the
  higher limit in `limits_history` and continues. Unknown paid usage under a
  token or cost limit still halts.

### Removed

- `examples/openai_image_adapter.py`, the example image adapter. The built-in
  `vision` adapter reads pages in a document job's image and second-opinion
  stages and keeps the same image evidence. To move a config, name
  `adapter: vision` with `base_url`, `model` and `env_key`; a host other than
  this machine also needs an `https://` address and `allow_remote: true`.
  `vision` takes `max_tokens` where the example took `max_output_tokens`, and
  does not limit models to the Gemini and DeepSeek families.

### Fixed

- Encrypted and damaged PDFs stop with a typed message that names the file:
  `unsupported_encryption` for a file that needs a password or uses a
  non-standard handler such as an Internet Archive lending copy,
  `malformed_pdf` for one pypdf cannot parse, and `missing_crypto_dependency`
  when AES support is missing. Previously a password-protected or non-PDF file
  ended `run` with a Python traceback, and a 3.4 GB file with a corrupt
  cross-reference offset failed with "negative seek value" after `pdf_text`
  had read the whole file into memory. `pdf_text` now reads PDFs from disk as
  it parses them. A `process` job halted by such a file records the code, for
  example `source_container_invalid:unsupported_encryption`, and says what to
  do next.
- `pdf_ocr` no longer renders oversized pages at full DPI. Some scans declare
  pages far larger than the paper: an Internet Archive scan of a 1911
  provincial memorial book declares pages 1.75 by 2.47 metres, about 600
  megapixels each at 300 DPI. Each page is now measured first and rendered at
  the highest DPI that fits within the new `max_render_pixels` adapter option
  (default 60,000,000). A lowered page carries the warning
  `render_dpi_capped`, and its provenance records both values, for example
  `dpi=94 (requested 300)`. A page that would need less than 72 DPI stops with
  `render_limit`.
- `pdf_ocr` no longer downsamples scans filed on undersized pages. An 1872
  volume declares 18 by 29 mm pages holding scans about 440 pixels wide, which
  300 DPI rendered at half their resolution. When pypdf is installed, a page
  whose largest embedded image has a higher resolution than the requested DPI
  is rendered at the image's resolution, up to 1200 DPI, and its provenance
  says so, for example `dpi=621 (requested 300, native image)`. Image sizes
  are read from the PDF without decoding the images, once per file.
- A missing `pdftoppm`, `tesseract` or Tesseract language pack now stops a
  `pdf_ocr` run before any page is read, with a message such as
  `missing_language_pack: ... Installed: eng, osd`. Previously the run started,
  failed on the first page, and showed only `RuntimeError: <redacted>`.
  PageLedger's own setup diagnostics carry a typed code and are shown in full;
  messages from adapters stay redacted. A page that fails with a typed
  failure now names its code and HTTP status, for example
  `AdapterFailure: MODEL_QUOTA (HTTP 429)`, on screen and in `run.log`,
  whichever adapter raised it: the codes come from a fixed list.
- Directory inputs skip hidden files such as macOS `.DS_Store` and `._*`
  sidecars. A `.DS_Store` file previously became the first document of a run
  and shifted every document number; on PDF adapters it failed the run. Skipped
  names are reported as `skipped_inputs` in the run result and manifest.
- Replay no longer fails as an incompatible environment when the replay worker
  reports the same text encoding with different capitalization ("UTF-8"
  versus "utf-8"). This happened wherever no `LANG` was set, because Python's
  locale coercion changes the child process's report. Profiles now record
  canonical codec names, and bundles recorded before the fix still replay.

### Maintenance

- A real-document harness for evaluating releases. `corpus/manifest.public.yml`
  lists 23 public-domain scans with their source, SHA-256 and the pages each
  tier runs. `scripts/corpus/run.py` runs a tier as verified document jobs,
  `compare.py` shows what changed between two runs, `sweep.py` checks that every
  PDF under a folder opens, and `score.py` scores a run, or one stage's engine
  within it, against reference transcriptions, reproducing the calibration
  grader's numbers.
- Documentation tests: every complete config in the docs and in
  `docs/examples/` loads, every relative link and heading anchor resolves,
  version strings in the README, docs index, skill and route-map
  specification match the package, and the README's first run executes.

### Compatibility

- New artifact fields are optional, so artifacts written by earlier versions
  still validate: `skipped_inputs` in manifests and checkpoints, the seven new
  `text_quality` counts in quality lines, and `hold_policy` and
  `limits_history` in jobs and document reports. Job and report `status` may
  now be `paused_budget`, and a report's `report_format` may be `"0.6"`. Eight
  retained 0.5.1 document jobs, seven of them with `low_confidence` pages, pass
  `verify-job` unchanged.
- The `pdf` extra adds `cryptography` (through `pypdf[crypto]`), and the new
  `rapidocr` extra installs RapidOCR and ONNX Runtime. Core still depends only
  on PyYAML.

## 0.5.2 - 2026-09-12

### Fixed

- `classify --from-run` rejects truncated, duplicate, out-of-range, and
  misidentified page inventories against the original run manifest.

### Maintenance

- Release preparation now reads and accounts for Copilot review guidance before
  merge and publication.

## 0.5.1 - 2026-09-12

### Changed

- Document reports distinguish the selected extraction from retained review
  evidence, so an empty initial text layer does not obscure a later OCR result.
  Review holds and decisions tied to source pages keep their original meaning.
  Existing 0.5.0 reports still verify without being rewritten.
- Added a maintained document-job tutorial and installed-package verification
  of processing, interrupted recovery, inspection and review. Both the wheel
  and source distribution run both maintained tutorials outside the checkout.
- Added a 60-page source validation report with per-page evidence, observed
  OCR errors, and clear limits. A reader-trial form records the human checks
  still needed; no participant results or accuracy rates are claimed.
- The optional image adapter now accepts explicit JPEG byte and dimension
  limits for gateways with smaller inputs. A separate live VLM test records
  provider failures and retained evidence; it did not produce a transcription.

### Fixed

- Ordinary runs stop before another adapter call when recorded token or cost
  usage has reached its cap. Negative adapter usage is rejected.
- Replay bundles cannot add a page selection that was absent from the original
  run. Malformed review receipts, route fields and schema column types produce
  validation errors with useful context.
- Malformed parent routes are rejected before classification writes new output.

### Maintenance

- Shared package checks replace duplicated workflow code. The locked CI and
  release checks enforce consistent Python formatting across source, tests,
  examples and scripts.

## 0.5.0 - 2026-09-11

### Added

- Document processing jobs with local text extraction, local OCR and optional
  image-model stages. `process` writes a page-referenced transcript, a document
  report and a review queue; `inspect-job` summarizes progress and `verify-job`
  checks the recorded evidence.
- Durable page checkpoints and `resume` for interrupted runs and jobs.
  Completed pages are verified before reuse. Changed sources, configuration
  mismatches and damaged checkpoints stop recovery. Attempts with an unknown
  provider outcome remain held for review rather than being retried automatically.
- Explicit processing limits for attempt pages, image pages, tokens and cost.
  An exhausted limit stops further attempts, including when usage reaches the
  limit exactly.
- Image evidence that records the source page, render or crop, dimensions,
  hashes and adapter identity. An optional OpenAI-compatible example adapter
  supplies image transport without adding a provider SDK to core.

### Changed

- Consolidated CLI error handling, fresh/resumed page accounting, extractor
  metadata and path validation. Removed dead aliases and replay bookkeeping.
- Updated the documentation around document jobs, recovery, review and the
  existing single-run workflow. Removed obsolete implementation plans and
  local readiness reports from the maintained documentation tree.

### Fixed

- When no shared pages are comparable, the human `compare-runs` summary now
  says warning and grade changes were not assessed instead of rendering
  directional zero totals. Per-page evidence and JSON counts are unchanged.
- Verification now rejects incomplete replay-page coverage, malformed page
  identities and malformed worker outcomes. Rerun-depth checks use the same
  configuration interpretation as execution.
- Integer columns and integer-only arithmetic preserve values beyond floating
  point precision. CSV parser failures produce structured error evidence.
- Strict failure-rate policy thresholds handle equality correctly. Existing
  arithmetic failure-delta serialization is retained whenever it preserves
  the exact value.
- Child runs are bound to their job configuration, and source-page links in
  document reports resolve from their output directory.

### Compatibility

- Existing run artifacts retain `schema_version: "0.1"`. New checkpoint,
  image-evidence and job artifacts have their own schemas. Generation-indexed
  `run.adapter_order` semantics are unchanged.
- Core still depends only on PyYAML; PDF support remains optional. OCR engines
  and provider dependencies belong to the selected adapters.
- Grades and verification describe recorded evidence. They do not certify
  transcription accuracy, downstream interpretation or original-file custody.
- Includes the reader fixes prepared for 0.4.2, which was not published.

## 0.4.1 - 2026-09-06

Available as the source tag `v0.4.1`; this version was not published to PyPI.

### Changed

- Hardened replay around a fresh-interpreter worker, one adapter instance per
  execution, canonical verification, and provenance binding.
- Human replay output discloses raw equal/different/missing counts, making its
  exact/evidence/mismatch guarantee inspectable without reading JSON.
- Human run, rerun, and inspection summaries distinguish unknown cost, known
  zero or nonzero cost, and partial known subtotals without changing machine
  result or artifact contracts.
- Added a maintained offline reader journey that runs, inspects raw/audit/CSV
  evidence, verifies, reruns the selected page, records external review notes,
  and exercises relocation/replay from a fresh scratch directory.
- Reordered the beginner path around finding and reviewing extracted text;
  bundle/replay is now a separate optional relocation step.

### Compatibility

- Replay's honest trust boundaries are documented, including adapter-declared
  material evidence and the non-hermetic credential/network boundary.
- The public CLI, JSON/result mappings, and artifact schemas are unchanged.

## 0.4.0 - 2026-08-17

### Added

- Verified directory bundles and replay evidence preserve the baseline run,
  source files, portable route map, and inventory for relocation and replay.
- Reproducibility profiles record adapter-declared material hashes when an
  adapter can provide them, with explicit exact/evidence limitations.
- Raw comparison evidence and replay linkage make exact, mismatch, and
  evidence-only outcomes inspectable without adding a runtime dependency.

### Compatibility

- Verified replay is intentionally non-hermetic: it does not install an
  environment, bundle code or models, establish cloud identity, or guarantee
  identical external services. Missing optional material evidence blocks
  deterministic exactness but does not block ordinary runs.

## 0.3.0a1 - 2026-08-16

### Added

- **Auditable local Docling example:** `examples/docling_adapter.py` invokes a
  machine-level Docling installation without adding its ML stack to PageLedger
  core. Standard mode converts once per document and derives page-level
  Markdown; local VLM mode processes only requested pages. Provenance records
  the Docling version, pipeline, VLM preset, and batch mode, while known visual
  or table serialization losses become adapter-native warnings.
- Raw extraction artifacts now carry SHA-256 identities in provenance and are
  checked by `verify-run`. Current manifests and route maps record the
  PageLedger package version; built-in PDF adapters record their effective
  parser, OCR, renderer, and material runtime settings.
- Latin-script joined/run-on hidden text now produces explicit quality and
  classifier evidence instead of silently receiving an A signals grade.

### Fixed

- Adapter-controlled exception messages, stdout, and stderr are omitted from
  default terminal/log error envelopes, preventing heuristic redaction misses
  from becoming secret disclosures.
- Adapter-native result warnings now enter `quality.jsonl`, grading, and audit
  routing as well as provenance; backends can no longer report known partial
  output without affecting review.
- The Docling example now advertises actions and capabilities by pipeline and
  rejects route prompts it cannot apply, so standard extraction cannot be
  mistaken for a VLM call or record a no-op prompt hash.
- Review-only routes have explicit accounting, grade distributions are labeled
  by evidence basis, and audit Markdown is verified as a rendering of
  `audit.json` rather than an independent source of truth.
- `compare-runs` now ranks transitions only when the full effective extractor
  identity, including adapter-option hashes, matches. Grade direction also
  requires matching PageLedger versions, effective grading policy, evidence
  bases, and schema identity, so incompatible claims remain visible but
  unranked.
- Reruns verify the parent ledger and source bytes before extraction, rederive
  their executable plan, preserve the source document page count, and record
  durable lineage depth.
- Verification now checks retained alignment-schema hashes and comparison uses
  canonical schema content, so pre/post-alignment grades can be related without
  trusting a mutable or missing schema snapshot.
- Missing raw hashes now fail integrity verification even for readable legacy
  evidence, so deleting generator-version metadata cannot obtain warning-only
  treatment; manifest, raw, normalized, and alignment symlinks also cannot make
  verification or comparison read outside the run directory.

### Release process

- Package publication is now a manual, tag-only, environment-gated workflow
  that verifies exact release metadata and smoke-tests the exact built wheel
  before any PyPI upload. Verification is the safe default; production also
  requires the exact tag to be typed and a reviewer-protected, non-bypassable,
  tag-restricted GitHub environment. CI keeps a frozen dependency lane
  alongside latest-dependency coverage, with third-party actions pinned to
  commit SHAs.
- The tracked Archivo font subset now carries its upstream OFL and copyright
  notice inside the brand archive; distributable third-party notices are
  included while brand source assets remain excluded from package archives.

### Compatibility

- Artifact schema version remains `0.1`. New fields are additive and optional
  for older artifacts. Current verification is stricter for raw-file tampering,
  rerun lineage/source drift, and audit-render divergence.
- Core still depends only on PyYAML. Docling remains an optional machine-level
  example integration and is not added to PageLedger dependencies or `uv.lock`.

## 0.2.0 - 2026-07-17

### Added

- **Structural page classification:** `pageledger classify` probes inputs or
  reuses retained evidence from a complete run and emits a route map that can
  execute unchanged through `pageledger run --routes`. The dependency-free
  classifier distinguishes `blank`, `sparse`, `prose`, `table_likely`, and
  `unknown`, records every signal and decision in an evidence sidecar, and
  supports importable classifier hooks for domain taxonomies.
- **Generation-indexed adapter escalation:** `run.adapter_order` selects one
  adapter and option set per original/rerun generation. Manifests record the
  planned chain and current step; exhausted chains leave pending pages in the
  human review queue with `chain_exhausted` unless the independent rerun-depth
  cap was reached first.
- **Budget alerts and grouped cost evidence:** capless `warn_pages`,
  `warn_tokens`, and `warn_usd` thresholds record one first-crossing alert per
  unit. `cost.json` can now group extracted-page usage and resolved cost by
  adapter and routed page type.

### Changed

- Classifier output is now the built-in producer for the reviewed route-map
  executor introduced in 0.1.6. Classification remains an explicit stage;
  ordinary `run` commands do not invoke it automatically.
- Rerun config remains authoritative when it differs from a parent's recorded
  next adapter. PageLedger records and prints the disagreement before using
  the supplied config. `max_rerun_depth` remains independent of chain length.
- Absolute and cap-relative budget thresholds share a first-crossing record;
  the lower effective threshold wins, with explicit absolute thresholds
  winning exact ties. Existing per-page `run.log` warning behavior is retained.

### Compatibility

- The package version is 0.2.0, while every artifact and config contract keeps
  `schema_version: "0.1"`. Manifest escalation fields and cost alerts/rollups
  are additive and optional; existing 0.1 artifacts remain valid.
- Core still depends only on PyYAML. The classifier adds no bundled model,
  computer-vision runtime, provider SDK, pricing catalog, or domain taxonomy.

## 0.1.7 - 2026-07-11

### Fixed

- **Unicode-script quality correctness:** lexical metrics now tokenize Unicode
  letters together with their combining marks. Clean Devanagari, Bengali,
  Gujarati, Gurmukhi, Tamil, Telugu, Kannada, and Malayalam prose no longer
  looks like one-character OCR fragments or suspicious symbol noise. Arabic,
  Kazakh Cyrillic, Latin, and Russian regression coverage protects the same
  script-safe boundary while existing OCR-fragment fixtures still warn.
- Per-page `word_count` now uses the same Unicode letter-plus-mark tokens as the
  lexical quality metrics instead of disagreeing with them on combining-mark
  scripts.

### Changed

- Built wheels now include the machine-readable JSON Schema contracts under
  `share/pageledger/schemas`; CI and the publish workflow inspect the installed
  or built wheel for them.
- Multilingual documentation now names the clean scripts under regression
  coverage instead of claiming universal language neutrality. README routing,
  review, confidence, historical-model, and reproducibility language is more
  precise.
- `cost_basis: none` is documented as unknown or unreported cost evidence, not
  proof that an adapter was free.

### Compatibility

- Artifact schema version remains `0.1`; no field was added, removed, renamed,
  or retyped. Corrected `word_count`, `suspicious_symbol_count`, and lexical
  metrics can differ from 0.1.6 for scripts that use combining marks or
  non-ASCII punctuation.

## 0.1.6 - 2026-07-10

### Added

- **Executable external routing:** `pageledger run --routes route-map.yml`
  validates and executes complete per-page type, action, confidence, and prompt
  decisions from a human or external classifier. Source coverage, page IDs,
  taxonomy types, hashes, and adapter action support are checked before output
  is created; the executed map and its source hash remain in the ledger.
- **Long-run page failure policy:** `run.on_page_error: continue` finishes
  independent later pages after retry exhaustion. A configurable consecutive-
  failure breaker stops dead services, while failed and not-attempted pages
  enter the audit queue and executable rerun manifest.
- Optional manifest failure counters, route source identity, route document
  hashes/page counts, and per-page derived cost evidence.

### Fixed

- `pageledger align` now re-evaluates `rerun_if` and `quarantine_if` instead of
  leaving policy decisions stale after schema evidence changes.
- Token-priced runs no longer report a known zero cost when an adapter omits
  token usage. Configured per-page rates now appear in `inspect-run --csv`.
- `verify-run` checks route/provenance agreement and failure accounting.
- The current quality schema once again accepts original 0.1 lines that predate
  confidence details and grading; current runs continue to emit those fields.

### Changed

- Release publishing now runs tests, Ruff, and mypy before building or
  publishing. Python 3.14 joins the tested matrix.
- Artifact schema version remains `0.1`; all new artifact fields are optional.

## 0.1.5 - 2026-07-10

### Added

- **Page policies** under `run.rerun_if` and `run.quarantine_if` can act on
  grades, missing required columns, and arithmetic failure rates. Quarantine
  takes precedence over rerun while the audit queue keeps every reason for
  review.
- **Ollama cleanup adapter example** for a Tesseract-first local workflow. It
  calls Ollama over its HTTP API, disables model reasoning, strips thought
  blocks defensively, and records prompt and completion token counts.
- **Two-page spread splitting recipe** using Poppler tools, with page-mapping
  cautions for downstream provenance.
- Static type checking for the package in CI.

### Changed

- Split quality analysis, budget accounting, and read-only reports out of the
  runner module. Compatibility imports preserve the existing Python surface.
- Updated GitHub Actions to the current supported majors: checkout 7,
  setup-python 6, upload-artifact 7, and download-artifact 8.
- Reworked the documentation from top to bottom for accurate claims, clearer
  language, easier navigation, and more useful package and repository search
  metadata.

### Compatibility

- Artifact schema version remains `0.1`. Existing fields keep their names and
  types; policy-free configurations keep their previous behavior.
- `run.grading.review_below_grade` remains supported alongside the new policy
  rules. Multi-adapter `adapter_order` escalation chains remain a design
  target.

## 0.1.4 - 2026-07-10

### Added

- **`pageledger verify-run`** checks cross-artifact ledger coherence:
  declared files, run/schema identities, config and source hashes, route and
  extraction counts, raw/normalized references, audit/rerun membership, and
  cost totals. External source drift is a warning; internal inconsistency is
  an error. It uses only stdlib and PyYAML and does not claim OCR correctness.
- **Read-only alignment preview** with `pageledger align ... --dry-run`,
  reporting before/after grades, review counts, and normalized records without
  writing a schema snapshot or changing the run.
- **Output-integrity evidence** in `quality.jsonl`: `instruction_echo` for
  high-specificity chat-template markers and `output_inflation` when a rerun
  is at least 4× and 1,000 characters larger than its parent page.
- **Alignment structure accounting** records duplicate headers, row-width
  mismatches, and ignored Markdown tables instead of silently discarding that
  ambiguity. Structural loss caps the schema grade at B.
- Top-level `pageledger --version`.

### Changed

- `compare-runs` now ranks warning and grade changes only when provenance
  proves the source bytes, source page, and adapter match. Changed-source,
  cross-adapter, and legacy-unknown transitions remain visible but unranked.
- Configuration, schema, adapter results, and artifact writers reject
  malformed mappings, booleans/fractions used as integers, unsupported schema
  versions, protocol-incomplete adapters, and non-finite JSON values.
- Built-in adapter identity is immutable; custom adapter metadata is required
  rather than invented. Adapter classes/factories are constructed once per
  execution.
- New-run and re-alignment manifests are written last as commit indicators.
  Re-alignment stages all derived output before replacing existing files.
- The duplicate Tesseract example is now a compatibility alias for the built-in
  `PdfOcrAdapter`; the TSV and local-LLM examples gained stricter bounds and
  output validation.

### Compatibility

- Artifact schema version remains `0.1`; new artifact fields are optional and
  no existing field was removed, renamed, or retyped.
- Malformed configs and adapters that depended on undocumented metadata
  invention now fail with explicit errors.

## 0.1.3 - 2026-07-08

### Added

- **Schema aligner** (previously a design target): the config `schema`
  section now drives extraction. Structured page output (`markdown_table`,
  `json`, `csv`) is mapped to declared columns with exact alias matching
  (casefold, collapsed whitespace; never fuzzy), `integer`/`number`
  coercion tolerant of thousand separators, and arithmetic `checks` with
  tolerance, writing one `normalized/{page_id}.json` record per page
  (new JSON Schema `schemas/normalized-page.schema.json`, spec
  `docs/normalized-spec.md`). Coercion failures and failed checks are
  recorded evidence, never silent fixes; unparseable structured payloads
  produce a record with `parse_error` set. Plain-text pages are not
  aligned. Check expressions are validated against an AST whitelist at
  config load.
- **Per-page quality grades (A–F)** (previously a design target):
  `quality.jsonl` lines now carry `grade`, `grade_basis`
  (`signals_only`/`schema_aware`), and `grade_detail`. Grades take the
  worst of a signals axis (confidence bands, warning counts, hard F on
  `empty_text`) and a schema axis (required-column coverage, arithmetic
  pass rate, coercion cap at B). Rendered surfaces always show the basis.
  `A (signals)` and `A (schema)` are different claims because grades are
  evidence summaries per adapter, not calibrated accuracy. Thresholds are
  configurable under `run.grading.thresholds`; the schema `quality` keys
  act as floors.
- **`pageledger align <run-dir> [--schema file.yml]`** re-aligns and
  regrade an existing run from its raw pages without re-extracting, so
  iterating on a schema costs nothing even when extraction was paid.
  Atomic rewrites; `manifest.json` written last with a new `alignment`
  block (timestamp, schema source, hash, version); external schemas
  snapshotted as `align-schema-snapshot.yml`; the mutation logged in
  `run.log` (`status: aligned`).
- **`run.grading.review_below_grade`** sends pages graded strictly below the
  configured letter join the review queue (reason `grade_below_threshold`)
  and the rerun manifest. Off by default: grading annotates without
  changing review behavior unless you opt in. This is the shipped subset
  of the `rerun_if` design target.
- Rerun manifests now fill `previous_grade` (previously always null) and
  list a multi-reason page once with reasons joined
  (`quality_warning+grade_below_threshold`).
- `inspect-run` reports `records_normalized` and a grade distribution;
  `inspect-run --csv` gains `grade` and `grade_basis` columns;
  `compare-runs` counts grades improved/regressed and renders transitions
  (`C (signals)→A (schema)`); audit.md queues gain a grade column.
- `examples/tesseract_tsv_table_adapter.py` clusters Tesseract TSV words
  clustered into a `markdown_table` by pixel coordinates. Deliberately
  naive column clustering, shipped as the structured-output adapter
  example the aligner needs.

### Changed

- The `schema` config section is now strictly validated at load
  (previously parsed and ignored). A schema without `columns`, which was legal
  as an inert stub in 0.1.2, is now a config error.
- Raw artifacts for adapters returning dict/list content are written as
  JSON (`json.dumps`, `ensure_ascii=False`), not Python `repr`. Byte-level
  change for third-party structured adapters; required for `align` to
  re-parse raw pages.
- The prose-calibrated shape warnings (`suspicious_symbol_density`,
  `fragmented_text`) no longer fire on structured formats
  (`markdown_table`/`json`/`csv`), where pipes and braces are construction,
  not garble.
- `quality.jsonl` lines now require the grade fields; external validators
  holding the 0.1 quality-line schema will reject pre-0.1.3 artifacts
  against the new schema (same precedent as `confidence_detail` in 0.1.2).

## 0.1.2 - 2026-07-07

### Added

- Word-level OCR confidence: `pdf_ocr` reads Tesseract's TSV output and
  reports mean page confidence (`ExtractionResult.confidence`, 0–1) plus
  per-word statistics in a new optional `confidence_detail` field. Both are
  recorded in `quality.jsonl`. A new `low_confidence` warning fires when a
  quarter of a page's words fall under engine confidence 60 (10+ words),
  catching weak passages a page mean would hide.
- Historical-orthography detection: `quality.jsonl` now counts letters
  abolished by the 1918 Russian reform (`prereform_letter_count`) and
  word-final hard signs (`terminal_hard_sign_count`), and a
  `historical_orthography` warning flags pre-1918 pages where an OCR model
  trained on modern text will degrade. Calibrated on an 1850 gubernia
  review: 21 terminal ъ per 100 tokens vs 0.00 in modern Russian.
- `--pages` on `run` extracts only the listed source pages
  (`--pages "1-8,81,100-110"`). Page ids keep the source numbering, so
  sampling a large volume no longer means splitting the PDF and losing
  page identity in the ledger. The selection is recorded in
  `manifest.inputs[].pages`.
- `run --adapter text|pdf_text|pdf_ocr` runs a built-in adapter without
  writing a YAML config. The generated defaults are recorded in
  `config-snapshot.yml`; no implicit config file is ever read.
- `inspect-run --csv` writes one row per page (counts, confidence, warnings,
  cost, timing) for spreadsheet triage.
- `pageledger doctor` lists installed Tesseract language packs, and
  `pdf_ocr` fails before extraction with the installed-language list when
  `run.adapter_options.lang` names a pack that is not installed.
- `init-config --adapter pdf_ocr` now includes `adapter_options`
  (`dpi`, `lang`) so the knobs non-English collections need are visible.
- `examples/prereform_normalizer_adapter.py` provides OCR plus pre-1918 Russian
  orthography canonicalization (ѣ→е, і→и, ѳ→ф, ѵ→и, morphology-aware ъ),
  with every rewrite counted in a result warning.

### Changed

- Standard European/Cyrillic typography («guillemets», em/en dashes,
  ellipsis, №, §, °) no longer counts toward `suspicious_symbol_density`,
  which was flagging ordinary Russian bibliographies.

## 0.1.1 - 2026-07-07

### Added

- Built-in `pdf_ocr` adapter OCRs scanned PDFs with locally installed
  Tesseract, rendering pages via `pdftoppm`. No new Python dependencies;
  missing binaries fail with a `pageledger doctor` hint. Reports
  `model: "tesseract <version>"` and measured `compute_seconds` per page.
- `run.adapter_options` is a config mapping passed to the adapter
  constructor. `pdf_ocr` takes `dpi` (default 300) and `lang` (default
  `eng`, `+`-joined for multiple languages). Custom adapter classes and
  factories receive the same options, lifting the old no-argument
  constructor limitation. Options are recorded in `manifest.extractors`
  (new optional `options` field, additive and backward compatible).
- `--adapter-path` on `run` and `rerun` adds a directory to `sys.path`
  so custom adapter modules load without setting PYTHONPATH.
- `pageledger init-config --adapter pdf_ocr`.
- Lexical-shape quality metrics (`alpha_token_count`, `mean_token_length`,
  `short_token_ratio`) in `quality.jsonl`, and a `fragmented_text` warning
  for OCR fragment noise (mean token length < 3 across 20+ tokens). These
  are additive optional fields; the warning taxonomy grows to seven items.
- `docs/examples/jfk-scanned-archive.md` is an end-to-end walkthrough for a
  107-page declassified scanned document from the National Archives.
- Ruff linting (`[tool.ruff]` in pyproject, dev extra, CI lint job).

### Changed

- Docs rewritten around the built-in OCR path: README quickstart,
  `docs/pdf-ocr-first-run.md` (no more `PYTHONPATH=examples`),
  `docs/ocr-options.md` decision matrix, `docs/adapter-protocol.md`
  (adapter options, `--adapter-path`).

## 0.1.0 - 2026-07-06

### Added

- `pageledger run` is an extraction command supporting dry-run artifact
  generation and built-in `text` and `pdf_text` adapter execution.
- `pageledger rerun` re-extracts exactly the pages listed in a previous
  run's `rerun-manifest.yml`, preserving page ids, recording parent lineage,
  enforcing `run.max_rerun_depth`, and warning when a source checksum no
  longer matches the parent manifest.
- `pageledger compare-runs` creates a page-by-page diff of two run directories:
  character/word deltas, warnings resolved/introduced, adapters, cost.
- `pageledger init-config` generates a minimal valid config to stdout or file.
- `pageledger inspect-run` summarizes a completed or failed run directory.
- `pageledger doctor` reports optional dependencies, external tool versions,
  and redacted cloud environment status.
- Filesystem-native run artifacts: `manifest.json`, `route-map.yml`,
  `config-snapshot.yml`, `audit.json`, `audit.md`, `provenance.jsonl`,
  `quality.jsonl`, `cost.json`, `run.log`, `rerun-manifest.yml`, and per-page
  output under `raw/`.
- Page-denominated budget enforcement caps pages, tokens, and dollars
  with configurable warning thresholds, enforced preflight and mid-run.
- Cost provenance: `cost.json` reports `cost_basis` (`adapter_reported`,
  `configured_rate`, `mixed`, `none`) and runner-measured
  `extraction_seconds`; provenance lines carry per-page `extraction_seconds`.
- Retry with configurable `max_retries` and optional exponential backoff
  (`retry.backoff: exponential`, 0.5 s base doubling to an 8 s cap).
- Quality signal diagnostics with seven-item warning taxonomy (`empty_text`,
  `short_text`, `replacement_characters`, `control_characters`,
  `suspicious_symbol_density`, `fragmented_text`,
  `suspicious_embedded_text_delta`).
- Quality-warning pages automatically routed to audit `review_queue` with
  `reason: "quality_warning"`.
- `quality_warning_pages` rollup count in `manifest.summary`.
- Custom adapters via `module.path:object` import strings.
- Adapter conformance checker: `pageledger.adapters.adapter_conformance_check()`.
- Adapter metadata validation at load time (name, version, deterministic,
  input_types, output_types, capabilities, supports, extract).
- `usage.pages == 1` enforcement means each `extract()` call handles exactly one page.
- Executable rerun manifests: `rerun_status`
  (`executable`/`empty_queue`/`no_further_generations`), `rerun_depth`
  generation tracking, and the `max_rerun_depth == 0` guard producing empty
  items.
- Config validation with key-path error messages and suspicious-config warnings
  (empty taxonomy, unknown top-level keys, impossible budget thresholds).
- JSON Schema files for all JSON/JSONL artifacts under `schemas/`.
- Schema validation tests covering dry-run, execute, budget failure, adapter
  failure, and empty-review-queue scenarios.
- Failure recovery: partial-run guarantees documented with scenario table,
  write-order, and common error/user-action mappings.
- Comprehensive test suite: 197 tests covering all CLI commands, adapters,
  quality signals, rerun execution, cross-run comparison, cost provenance,
  schemas, failure paths, and edge-case inputs.
- CI workflow with test matrix (with/without PDF extra, Python 3.10–3.13)
  and wheel/sdist smoke tests.
- A source-tree release checklist for maintainers.

### Changed

- "Core Modules" → "Design Architecture" with explicit implementation-status
  labels in README.
- Added "Current Runtime Capabilities" (three tiers) and "Known Limits" sections.
- Claim audit: removed optimistic score language, constrained the public promise
  to implemented behavior only.
- `adapter-protocol.md` rewritten with frozen contract, `usage.pages == 1` rule,
  and subprocess/timeout guidance.

### Removed

- Python 3.14 classifier (not yet released).

### Performance

- Tested scale: 5,000 text pages in 2.4s (~2,100 pages/sec) locally.
  Artifact counts verified: raw files == provenance lines == quality lines
  == pages extracted. Stress tests reproducible without credentials or
  private data.

### Fixed

- `--json` now emits parseable error JSON (`{"status": "error", "error": "..."}`)
  on failure instead of unstructured stderr-only errors.
- Unreadable input files now produce clean `RuntimeError` instead of raw
  `PermissionError` tracebacks.
- Rerun manifest `max_rerun_depth == 0` now correctly produces empty `items: []`
  with `rerun_status: "no_further_generations"`.
