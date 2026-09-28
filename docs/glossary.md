# Glossary

PageLedger uses the terms below in reports, configuration, and artifact files.
The links point to the main page describing each term.

| Term | Meaning | See also |
|---|---|---|
| Action | What PageLedger should do with a routed page: `transcribe_text`, `skip`, or `review`. | [Routes](cli.md#classify) |
| At-rest observation | A check made while reading files; it does not prevent another process from changing them at the same time. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Attempt | One adapter's work on a page, including its output and recorded result. | [Document jobs](processing-spec.md#durable-job-and-attempts) |
| Attempt page | A page-level record of an extraction attempt; it can include a response and completion details. | [Checkpoint records](checkpoint-spec.md) |
| Authority | The file or record used as the source of truth for a particular decision. | [Artifacts](artifacts.md#what-each-file-answers) |
| Bundle | A directory containing a verified run, copies of its source files, and an inventory. | [Bundle](cli.md#bundle) |
| Canonical | The designated record to consult for a fact. It does not mean the record is guaranteed correct. | [Artifacts](artifacts.md#what-each-file-answers) |
| Child run | An ordinary run created by a document job for a stage or attempt. | [Document jobs](processing-spec.md#durable-job-and-attempts) |
| Disposition | The job's current summary of a page, such as pending, selected text, or a review condition. | [Document report](document-report-spec.md) |
| Dogfooding | Using a tool or workflow on the team's own work; it is not a broad accuracy study. | [Classifier limits](capabilities-and-limits.md#known-limits) |
| Engine agreement | The share of words two engines read the same way on a page. It is evidence, not proof: engines can share a mistake. | [Compare engines on a page](processing-spec.md#compare-engines-on-a-page) |
| Escalation chain | The ordered adapters used across an original run and explicit reruns. | [Reruns](cli.md#rerun) |
| Evidence | A retained record that supports a stated identity, result, or decision. It does not by itself prove accuracy. | [Artifacts](artifacts.md) |
| Export | The selected text of a verified job, written as plain text, Markdown, JSONL or TEI. | [Export](export.md) |
| Fail closed | Refuse an operation when a required safety or integrity check cannot be satisfied. | [Verification](artifacts.md#verification) |
| Generation | A run's place in a rerun sequence: the original is generation zero, followed by explicit reruns. | [Reruns](cli.md#rerun) |
| Generative | Output a model writes rather than reads off the page, which can contain text that is not there. PageLedger holds such readings for review. | [Vision adapter](vision-adapter.md) |
| Grade basis | The kind of information behind a grade: text signals or schema checks. These bases are not interchangeable. | [Comparison](cli.md#compare-runs) |
| Hold | A recorded reason a page or attempt remains unresolved or needs a person. | [Warnings and holds](warnings.md) |
| Job | A document-level record of staged attempts, selected text, review state, and report. | [Processing](processing-spec.md) |
| Legacy | An artifact or value written by an earlier version. It may remain readable, but can lack evidence added later. | [Compatibility](run-manifest-spec.md#compatibility-policy) |
| Non-hermetic | Not isolated from external software, credentials, services, or machine state. Replay is not hermetic. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Outcome unknown | The request started, but no durable response was saved; the provider may have processed it. | [Recovery](checkpoint-spec.md) |
| Quarantine | A policy outcome that keeps a page out of the executable rerun list. | [Artifacts](artifacts.md#what-each-file-answers) |
| Receipt | A source- and output-bound record of a human review decision. It records a review; it is not generated from engine confidence. | [Human review](document-report-spec.md#human-review-receipt) |
| Reproducibility materials | Adapter-declared files or binaries whose identities may be recorded for replay comparison. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Reproducibility profile | A report of identity information for an adapter and its declared materials. It does not attest to every dependency. | [Replay boundary](capabilities-and-limits.md#verified-replay-boundary) |
| Review reason | A machine-readable label explaining why a page is held for review. | [Warnings and holds](warnings.md) |
| Review sheet | A CSV of a job's pages for recording decisions in a spreadsheet. | [Review in a spreadsheet](processing-spec.md#review-in-a-spreadsheet) |
| Route | A page's type and assigned action, with optional prompt, reason, and confidence. | [Classify](cli.md#classify) |
| Selected text | The attempt the job's policy chose as a page's text, before any human review. | [Document report](document-report-spec.md) |
| Stage | One named part of a document job, such as `local_text`, `local_ocr`, or `image`. | [Processing](processing-spec.md) |
| Taxonomy | The config mapping from page types to actions and optional prompts or review flags. | [Configuration](cli.md#configuration) |
| Warning | A quality signal recorded for a page, such as `low_confidence` or `repetition_loop`. In a job, many warnings become holds. | [Warnings and holds](warnings.md) |

For core commands, see the [CLI reference](cli.md). For supported scope, see
[capabilities and limits](capabilities-and-limits.md).
