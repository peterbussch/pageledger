# First job: process, check, correct and export

This tutorial makes one small job from a three-page text file, checks its
record, corrects a word and writes the text out. It needs only
`pip install pageledger`: no OCR engine, no network and no model. Type the
commands in an empty folder.

## Make a source

A text file's pages are separated by form feeds (`\f`). This one has three
short, made-up pages:

```bash pageledger-tutorial
PYTHON="${PYTHON:-python}"
printf 'Synthetic page one: a short archival-style paragraph.\fSynthetic page two: a second paragraph for the review example.\fSynthetic page three: the final paragraph in this fixture.' > sample-document.txt
```

## Process, inspect and verify

```bash pageledger-tutorial
pageledger process sample-document.txt --out jobs/complete
pageledger inspect jobs/complete
pageledger verify jobs/complete
```

`process` makes the job, the folder `jobs/complete`, and says how many pages
need a person and what to run next. A text file has no page images, so its own
text is the only reading; nothing is sent anywhere. Here all three pages need a
person, because a page with very little text is held in case text is missing:

```text
Finished: 3 pages read; 3 need a person.
The job is in jobs/complete.
Why they need a person:
  3 pages: the text file has very little text.
```

`inspect` prints the job's report, `report.md`: where each page stands, which
reading was selected, and then, for the record, the hashes and settings behind
every reading. The cost is shown as unknown, not zero, because the text reader
reports no charge.

`verify` checks the record: the source's fingerprint (its SHA-256 hash), every
saved reading, the selected text and the report.

```text
Status: pass
The record is intact: every file matches what was recorded. This does not check that the text is right.
```

A finished job is not a checked one. Whether the text is right is for a person
to decide, which is the next step.

## Correct a word

When the text is wrong, you record the right text rather than editing the
engine's. The job keeps both, with a receipt saying who changed what and from
which reading, so a later reader can check the correction against the page.

The review sheet is a spreadsheet with one row per page. Write it, put the
corrected text of page 2 in a file beside it, and type `correct:p0002.txt` in
page 2's `decision` column. In a spreadsheet program you would type it by hand
and save as CSV UTF-8; here a few lines of Python do it:

```bash pageledger-tutorial
pageledger review jobs/complete --sheet correct/review.csv
"$PYTHON" - <<'PY'
import csv
from pathlib import Path

Path("correct/p0002.txt").write_text(
    "Synthetic page two: a second paragraph for the correction example.",
    encoding="utf-8",
)
sheet = Path("correct/review.csv")
with sheet.open(encoding="utf-8-sig", newline="") as stream:
    rows = list(csv.DictReader(stream))
rows[1]["decision"] = "correct:p0002.txt"
rows[1]["note"] = "Synthetic fixture correction of one word."
with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
PY
pageledger review jobs/complete --apply correct/review.csv \
  --reviewer "synthetic tutorial reviewer" --dry-run
pageledger review jobs/complete --apply correct/review.csv \
  --reviewer "synthetic tutorial reviewer"
"$PYTHON" - <<'PY'
import json
from pathlib import Path

job = json.loads(Path("jobs/complete/job.json").read_text())["payload"]
print(json.dumps(job["pages"][1]["review"]["decisions"][0]["correction"], indent=2))
PY
pageledger verify jobs/complete
echo DOCUMENT_JOURNEY_CORRECTION_OK
```

The dry run prints `Correction of page 2, from the text file: 1 word changed,
0 added, 0 removed` and records nothing; the second command records it. The
reviewer's name here is made up for the example. The receipt's `correction`
names the corrected text's file under `corrections/`, its SHA-256, the SHA-256
of the reading it started from, and the word counts. The engine's reading is
still in the job, unchanged; the report now shows page 2 as corrected by a
person. See [correct the text](processing-spec.md#correct-the-text).

## Review beside the page image

Checking a transcript means reading it against the page it came from. For a
PDF or page images, `review` writes a folder to open in a browser, with each
page's image beside its text and the other readings compared word by word:

```bash
pageledger review jobs/complete --out review --images none
```

This source is a text file, so it has no images and `--images none` is
needed. Choose a decision under each page or correct its text in the box below
it, download `review.json`, and record it with
`pageledger review jobs/complete --apply ~/Downloads/review.json --reviewer "Your Name"`.
Nothing is recorded until you do.
[Review and correct](review-and-correct.md) goes through each step.

## Export the text

```bash pageledger-tutorial
pageledger export jobs/complete --out sample-document.md
```

The export marks each page with whether a person checked it, and which reading
its text comes from. `--format` gives plain text, JSON lines or TEI instead;
see [export](export.md).

## Write the configuration out

Without `--config`, `process` uses the configuration that `init` writes for the
same options. To change what a job does (another OCR engine, a language, a
second engine), write it to a file, edit it, and pass it with `--config`:

```bash pageledger-tutorial
pageledger init --engine none --out document-job.yml
grep -v '^ *#' document-job.yml
```

Its comments say what each setting does. For a PDF, leave out `--engine none`:
the default adds Tesseract OCR for pages whose own text is missing or looks
wrong.

## For maintainers: the checks this page also runs

The blocks above are tested by running them in order. Two more blocks need a
source checkout and are not part of the tutorial; skip them. From a checkout,
the whole page runs with:

```bash
python examples/run_first_run.py \
  --document "$PWD/docs/document-first-run.md" \
  --work-dir /tmp/pageledger-document-first-run \
  --python "$(command -v python)" \
  --source-root "$PWD"
```

The first block interrupts a job just after its first reading is saved,
resumes it, and checks from a call log that page 1 was not read twice. The
helper it runs is `examples/run_document_recovery.py`, which `run_first_run.py`
passes as `PAGELEDGER_TUTORIAL_RECOVERY_HELPER`.

```bash pageledger-tutorial
CALLS_PATH="$PWD/calls-interrupted.jsonl"
"$PYTHON" "${PAGELEDGER_TUTORIAL_RECOVERY_HELPER:-run_document_recovery.py}" \
  --source sample-document.txt --config document-job.yml \
  --out jobs/interrupted --calls "$CALLS_PATH"
pageledger inspect jobs/interrupted
pageledger verify jobs/interrupted
"$PYTHON" - <<'PY'
import json
from pathlib import Path

calls = [json.loads(line) for line in Path("calls-interrupted.jsonl").read_text().splitlines()]
assert [call["page_number"] for call in calls] == [1, 2, 3]
print("DOCUMENT_JOURNEY_RECOVERY_OK")
PY
```

The second applies a review file written by hand: one tied to the wrong source,
which is refused, and one tied to page 1's exact reading, which is recorded.

```bash pageledger-tutorial
"$PYTHON" - <<'PY'
import json
from pathlib import Path

job = json.loads(Path("jobs/complete/job.json").read_text())["payload"]
page = job["pages"][0]
stale = {
    "schema_version": "0.1",
    "source_sha256": "0" * 64,
    "decisions": [{
        "page_id": page["page_id"], "page_number": page["page_number"],
        "disposition": "reviewed_text", "selected_attempt": None,
        "output_sha256": None, "reason": "stale fixture receipt",
        "reviewer": "synthetic tutorial reviewer",
        "reviewed_at": "2026-09-12T12:00:00Z",
    }],
}
Path("stale-review.json").write_text(json.dumps(stale), encoding="utf-8")

attempt = page["attempts"][0]
valid = {
    "schema_version": "0.1", "source_sha256": job["source"]["sha256"],
    "decisions": [{
        "page_id": page["page_id"], "page_number": page["page_number"],
        "disposition": "reviewed_text",
        "selected_attempt": attempt["attempt_id"],
        "output_sha256": attempt["raw_sha256"],
        "reason": "Synthetic fixture review tied to the selected output.",
        "reviewer": "synthetic tutorial reviewer",
        "reviewed_at": "2026-09-12T12:00:00Z",
    }],
}
Path("valid-review.json").write_text(json.dumps(valid), encoding="utf-8")
PY
if pageledger review jobs/complete --apply stale-review.json; then
  echo "stale receipt unexpectedly accepted" >&2
  exit 1
fi
echo DOCUMENT_JOURNEY_STALE_RECEIPT_REJECTED
pageledger review jobs/complete --apply valid-review.json
pageledger verify jobs/complete
echo DOCUMENT_JOURNEY_REVIEW_OK
```
