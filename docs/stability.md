# Stability and compatibility

From 1.0, PageLedger keeps the things listed below stable for every 1.x
release, so a run or job you cite today can still be read and verified with a
later 1.x version. Each promise in the table is checked by the tests named
beside it, mostly in `tests/pageledger/test_stability.py`, against snapshots of
the 0.8.0 contract in `tests/fixtures/stability/`; the tests fail if anything
in them is removed, renamed or narrowed.

"Stable" means: a name keeps its meaning, and nothing a 0.8 user relied on is
taken away. New commands, options, fields, codes and rule sets may be added in
any minor release.

## What is stable

| Surface | The promise | Checked by |
|---|---|---|
| Commands | Every command, positional argument and option in [the CLI reference](cli.md) keeps its name. An option does not become required, lose an allowed value, or become exclusive with another existing option. | `cli.json` snapshot of the argparse parser |
| Exit codes | 0 for success, 1 for a runtime error or a failed result as [the command index](cli.md#command-index) describes, 2 for a usage error. | `test_exit_codes`: `--version`, usage errors, and `verify-run` passing and failing |
| Configuration | Every key in [the configuration table](cli.md#configuration) is still accepted and means what it meant: a 0.8 config file loads without warnings and compiles to the same processing policy, because a job records that policy. | `config-keys.json`, `config-0.8.yml` and its effective values |
| Adapter protocol | A custom adapter written to [the protocol](adapter-protocol.md) keeps working: no new required attribute, `extract(source, *, page_id, page_number, action, prompt=None)` is called exactly so, and `ExtractionResult` keeps its fields with no new required one. Built-in adapter names (`text`, `pdf_text`, `pdf`, `pdf_ocr`, `vision`, `rapidocr`, `indra`) keep resolving. | a strict adapter run through `classify`, `run`, `run --resumable` and `process`; `adapter-protocol.json` |
| Artifacts | The run files in [the artifact list](artifacts.md) and a job's `job.json`, `document.json`, `report.md` and `transcript.md` keep their names, and each JSON Schema in `schemas/` only grows: no field removed or renamed, no type narrowed, no allowed value removed, nothing newly required. Artifacts keep `schema_version: "0.1"`, which is the 1.0 artifact contract. | `schemas/` snapshots; run file names in `taxonomies.json`; a new job's `job.json`, `document.json`, `report.md` and `transcript.md`; artifacts from 0.5.2, 0.6.0 and 0.7.0 validate against the current schemas |
| Warning codes and holds | Each code in [the warnings table](warnings.md) keeps its name and keeps filing the same hold. Hold reasons, dispositions and review dispositions keep their names. Codes may be added, never renamed or given a new meaning. | `taxonomies.json`, which reads the codes from the warnings table; each must still appear in the package's code |
| Hold policies | A job is rebuilt by the rules it was written under (`hold_policy` absent, `0.6`, `0.6.1`, `0.8`), so new selection rules never change an old job's result. See [document report](document-report-spec.md). | `taxonomies.json`; existing hold-policy tests |
| Contest rule sets | A rule set's name versions its whole procedure. `ru-print-0.1` and `ru-print-0.2` keep exactly their rules; a change is a new name. | `taxonomies.json` holds each rule set's rules |
| Codes in records | Page states, stages, escalation triggers, export formats, report formats, grades, and adapter failure and diagnostic codes keep their names. | `taxonomies.json` |

YAML artifacts (`route-map.yml`, `rerun-manifest.yml`, `config-snapshot.yml`)
have no JSON Schema; their spec documents describe their fields, and only the
field checks in `tests/pageledger/test_schemas.py` enforce them.

## What is not covered

- The wording of Markdown renderings such as `audit.md`, `report.md` and
  `transcript.md`, and of human-readable command output and log messages.
  `audit.md` is a rendering of `audit.json`, which is covered.
- The keys of `--json` command output, and the issue codes in a verification
  report (`source_missing` and the like). They are not yet frozen by tests.
- Grades and quality signals across versions. A later release may grade the
  same text differently; a run records the grades it computed.
- The Python API beyond the adapter protocol (`ExtractionResult`,
  `AdapterFailure`, `adapter_conformance_check`). Other modules are internal.
- Example adapters and scripts under `examples/`. The suite checks that they
  still have the protocol's shape, but they may change in any release.
- Anything a document marks as experimental.
- What external engines and models return. PageLedger records it; it cannot
  hold it still.

## Deprecation and major versions

A stable item is removed or changed incompatibly only in a major release
(2.0). Before that, it is marked deprecated in `CHANGELOG.md` and in its
reference page for at least one minor release, and keeps working until the
major release removes it. A major release may also raise `schema_version`; its
changelog then says how to read or migrate older artifacts, and older ones stay
readable as legacy (see the
[compatibility policy](run-manifest-spec.md#compatibility-policy)).

## Older runs and jobs

The suite holds a run and a document job written by each of 0.5.2, 0.6.0 and
0.7.0 from PyPI (`tests/fixtures/stability/releases/`, with the commands that
wrote them).

- **Runs** from all three verify with the current `verify-run` after being
  moved to another directory: with warning `source_missing` when the source is
  gone, or `source_relocated` when a [source index](checkpoint-spec.md) finds it.
  Move the whole directory, including an empty `normalized/`: Git and some
  archive tools drop empty directories, and `verify-run` then fails with
  `artifact_missing`.
- **Jobs** from all three verified in place with 0.8.0 (checked once, on
  2026-11-10, where they were written), and their stage runs still verify when
  moved. A job does not verify after its
  directory is moved or copied elsewhere, from any version including the
  current one: `job.json` records the job's absolute directory, and `verify-job`
  fails with "Document job identity changed". Verify a job where it was written,
  and keep its hashes with any copy you deposit.
- `tests/pageledger/test_processing.py` also rebuilds jobs shaped as 0.5 and
  0.6.0 wrote them (no hold policy, policy `0.6`), and
  `tests/pageledger/test_hold_policy_08.py` checks that the 0.8 rules stay off
  for jobs written under earlier policies.
