# Stability snapshots

`tests/pageledger/test_stability.py` compares the package with these files. They
record the 0.8.0 contract that [docs/stability.md](../../../docs/stability.md)
describes. Update them only with `PAGELEDGER_UPDATE_STABILITY=1` (see the test
module's docstring), which refuses to write a snapshot when something in it was
removed or narrowed.

| File | Contents |
|---|---|
| `cli.json` | Every command, positional argument and option, with `required`, `choices` and mutually exclusive groups, read from the argparse parser. |
| `schemas/` | Copies of `schemas/*.json`. The current schema must accept everything its copy accepts. |
| `taxonomies.json` | Warning codes (from the table in `docs/warnings.md`), the hold each warning files, hold reasons, review dispositions, hold policies, page states, stages, triggers, contest rule sets and their rules, export formats, report formats, grades, adapter failure and diagnostic codes. |
| `adapter-protocol.json` | The attributes an adapter must declare and the fields of `ExtractionResult`. |
| `config-keys.json` | Known top-level and `run` keys, and the processing stages. |
| `config-0.8.yml`, `config-0.8.effective.json` | A configuration that sets every documented key, and the values and processing policy 0.8.0 read from it. |

## Runs and jobs written by published releases

`releases/<version>/` holds a run and a document job written by that release from
PyPI, unmodified except that Git drops their empty directories (`normalized/`,
`partials/`); the tests verify them that way, since verification reads a missing
empty directory as empty. They were written on
2026-11-10 in `/tmp/pageledger-golden` (recorded as `/private/tmp/...` on macOS),
from `releases/sample.txt` (two pages; the second, `short`, raises `short_text`):

```bash
cd /tmp/pageledger-golden
uvx --from 'pageledger==VERSION' pageledger init-config --out pageledger-VERSION.yml
uvx --from 'pageledger==VERSION' pageledger run sample.txt --config pageledger-VERSION.yml --out run-VERSION
uvx --from 'pageledger==VERSION' pageledger process sample.txt --config processing.yml --out job-VERSION
```

with `VERSION` 0.5.2, 0.6.0 and 0.7.0. `releases/<version>/pageledger.yml` is
that release's `init-config` output; `releases/processing.yml` is shared. The jobs
record hold policy none (0.5.2), `0.6` (0.6.0), `0.6.1` (0.7.0) and `0.8` (0.8.0).

0.8.0 was built but never published to PyPI. `releases/0.8.0/` was written the same
way, the same day, from the local build
`pageledger-0.8.0-py3-none-any.whl` (SHA-256
`0d350f2dc1079c5abbd2caf1040ea6474d6f93638d3e4d81ed3c99741d27de5d`), with
`uvx --from pageledger-0.8.0-py3-none-any.whl pageledger ...` in place of
`'pageledger==VERSION'`.

Each release's own `verify-run` and `verify-job` passed on what it wrote, and the
0.8.0 `verify_run` and `verify_job` passed on the first six in place (checked once, by
hand: the suite cannot recreate the recorded directory); the 0.9.0 ones passed on
0.8.0's run and job in place. Moved, the runs
verify with `source_missing`, or `source_relocated` through a source index.
From 0.9.0 the jobs verify after a move too, adding warning `job_relocated`:
`root` in `job.json` records where a job was written and is no longer part of
its identity.
