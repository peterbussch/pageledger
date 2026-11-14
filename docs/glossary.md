# Glossary

PageLedger uses the terms below in reports, configuration, and artifact files.
The links point to the main page describing each term.

| Term | Meaning | See also |
|---|---|---|
| Action | What PageLedger should do with a routed page: `transcribe_text`, `skip`, or `review`. | [Routes](cli.md#classify) |
| At-rest observation | A check made while reading files; it does not prevent another process from changing them at the same time. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Attempt page | A page-level record of an extraction attempt; it can include a response and completion details. | [Checkpoint records](checkpoint-spec.md) |
| Attempt | The name the job's files give a reading (`attempts/`, `attempt_id`). See Reading. | [Durable job and attempts](processing-spec.md#durable-job-and-attempts) |
| Authority | The file or record used as the source of truth for a particular decision. | [Artifacts](artifacts.md#what-each-file-answers) |
| Bundle | A directory containing a verified run, copies of its source files, and an inventory. | [Bundle](cli.md#bundle) |
| Canonical | The designated record to consult for a fact. It does not mean the record is guaranteed correct. | [Artifacts](artifacts.md#what-each-file-answers) |
| Carrier | The PDF PageLedger writes for a run from page images, holding each image's own bytes, so the engines that read PDFs can read them. It is derived and deleted after the run; the images are the record. | [Page-image sources](run-manifest-spec.md#page-image-sources) |
| Child run | An ordinary run created by a job for a stage or attempt. | [Jobs](processing-spec.md#durable-job-and-attempts) |
| Contest | A word-by-word comparison of a model's reading with the OCR's, which settles some differences by fixed rules and leaves the rest open for a person. | [Contest a model's reading](processing-spec.md#contest-a-models-reading) |
| Correction | Text a reviewer typed to replace a page's reading. It is kept as a new file beside the engine's reading, which is never edited. | [Correct the text](processing-spec.md#correct-the-text) |
| Disposition | The job's current summary of a page, such as pending, selected text, or a review condition. | [Document report](document-report-spec.md) |
| Dogfooding | Using a tool or workflow on the team's own work; it is not a broad accuracy study. | [Classifier limits](capabilities-and-limits.md#known-limits) |
| Engine agreement | The share of words two engines read the same way on a page. It is evidence, not proof: engines can share a mistake. | [Compare engines on a page](processing-spec.md#compare-engines-on-a-page) |
| Escalation chain | The ordered adapters used across an original run and explicit reruns. | [Reruns](cli.md#rerun) |
| Evidence | A retained record that supports a stated identity, result, or decision. It does not by itself prove accuracy. | [Artifacts](artifacts.md) |
| Export | The selected text of a verified job, written as plain text, Markdown, JSONL or TEI. | [Export](export.md) |
| Fail closed | Refuse an operation when a required safety or integrity check cannot be satisfied. | [Verification](artifacts.md#verification) |
| Generation | A run's place in a rerun sequence: the original is generation zero, followed by explicit reruns. | [Reruns](cli.md#rerun) |
| Generative | Output a model writes rather than reads off the page, which can contain text that is not there. PageLedger holds such readings for review. | [Vision adapter](vision-adapter.md) |
| Grade basis | The kind of information behind a grade: text signals or schema checks. These bases are not interchangeable. | [Comparison](cli.md#compare) |
| Hold policy | The versioned rules that decide which reading a job selects and which pages it holds. A job records the policy it was written under and is always rebuilt by it; new jobs record `0.9`. | [Hold policy 0.9](processing-spec.md#hold-policy-09) |
| Hold | A recorded reason a page needs a person before its text is trusted, such as `coverage_defect` or `low_confidence`. The report gives each in plain words. | [Warnings and holds](warnings.md#in-plain-words) |
| Inherited OCR layer | A text layer that an earlier OCR program wrote into the PDF, or a library's ALTO file. A job treats it as one more witness beside its own OCR, not as the page's own text (`ocr_text_layer`). | [Hold policy 0.9](processing-spec.md#hold-policy-09) |
| Job | The folder `process` writes for one document: every reading of every page, the text selected for each, the holds, the review decisions and the report. The report calls it a document job, to tell it from a run. Its status is `completed` (shown as finished), `paused_budget` (paused at a limit), `halted` or, while it runs, `processing`. | [Document processing jobs](processing-spec.md) |
| Legacy | An artifact or value written by an earlier version. It may remain readable, but can lack evidence added later. | [Compatibility](run-manifest-spec.md#compatibility-policy) |
| Moved job | A job folder copied or moved from where it was written. It verifies, exports and can be reviewed there, but resumes only where it was written. | [Moved and copied jobs](processing-spec.md#moved-and-copied-jobs) |
| Native pixels | The pixel width and height of the image that is a page: a page image, or a scanned PDF page's image. | [Page images and native pixels](provenance-spec.md#page-images-and-native-pixels) |
| Needs a person | A page that has been read, has no review decision, is not waiting for a stage still to run, and is held or has no text. `process`, the report, the review view and the exports count these pages the same way. | [Report format 0.9](document-report-spec.md#report-format-09) |
| Non-hermetic | Not isolated from external software, credentials, services, or machine state. Replay is not hermetic. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Outcome unknown | The request started, but no durable response was saved; the provider may have processed it. | [Recovery](checkpoint-spec.md) |
| Page images | JPEG or PNG files, one per page. One file is a one-page document; a folder of them is one document, pages in natural order of file name. | [Page images](process-a-collection.md#page-images) |
| Quarantine | A policy outcome that keeps a page out of the executable rerun list. | [Artifacts](artifacts.md#what-each-file-answers) |
| Reading | One engine's text for one page, kept with what produced it. A job keeps every reading; the files call it an attempt. | [Durable job and attempts](processing-spec.md#durable-job-and-attempts) |
| Receipt | A source- and output-bound record of a human review decision. It records a review; it is not generated from engine confidence. | [Human review](document-report-spec.md#human-review-receipt) |
| Reproducibility materials | Adapter-declared files or binaries whose identities may be recorded for replay comparison. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Reproducibility profile | A report of identity information for an adapter and its declared materials. It does not attest to every dependency. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Review reason | A machine-readable label explaining why a page is held for review. | [Warnings and holds](warnings.md) |
| Review sheet | A CSV of a job's pages for recording decisions in a spreadsheet. | [Review in a spreadsheet](processing-spec.md#review-in-a-spreadsheet) |
| Review view | The folder `review --out` writes: a page to open in a browser, with each page's image beside its text, where a reviewer chooses decisions and corrects text. Nothing is recorded until `review --apply`. | [Review beside the page image](processing-spec.md#review-beside-the-page-image) |
| Route | A page's type and assigned action, with optional prompt, reason, and confidence. | [Classify](cli.md#classify) |
| Run | The result of `run`: one extraction tool applied to every page of some files, with the same per-page evidence as a job but no selection, holds or review. Runs are for building and testing pipelines. | [First run](first-run.md) |
| Selected text | The reading the job chose as a page's text, before any human review. | [Document report](document-report-spec.md) |
| Snapshot | The folder `fetch` writes from a IIIF server: the page images, the manifest as served, and a record binding each image to its URL. | [Fetch page images from a IIIF server](iiif.md) |
| Stage | One named part of a job, such as `local_text`, `local_ocr`, or `image`. | [Processing](processing-spec.md) |
| Taxonomy | The config mapping from page types to actions and optional prompts or review flags. | [Configuration](cli.md#configuration) |
| Text layer | The text a PDF carries on its pages, as a born-digital PDF does. A scan has none, or one an earlier OCR added. | [Start with local text and OCR](processing-spec.md#start-with-local-text-and-ocr) |
| Tile | A part of a dense page, cut along the white space in its layout (between columns or lines), that a vision model reads as its own image. | [Read dense pages in tiles](processing-spec.md#read-dense-pages-in-tiles) |
| Verify | `pageledger verify`: a check that every file of a job or run still matches what was recorded, here or after the folder has moved. A pass means the record is intact; it does not mean the text is right. | [Share and cite](share-and-cite.md) |
| Warning | A quality signal recorded for a page, such as `low_confidence` or `repetition_loop`. In a job, many warnings become holds. | [Warnings and holds](warnings.md) |

For core commands, see the [CLI reference](cli.md). For supported scope, see
[capabilities and limits](capabilities-and-limits.md).
