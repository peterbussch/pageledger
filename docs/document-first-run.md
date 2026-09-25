# First document job: process, inspect, verify, recover, and review

This tutorial is a small, offline document job. It uses PageLedger's built-in
`text` adapter and a three-page synthetic text fixture, so it needs no OCR
engine, network access, provider credential, or extra package. The optional
recovery helper records adapter calls in JSONL and reports unknown cost. JSONL
means one JSON object per line. These files show what this journey did; they do
not measure transcription accuracy.

The commands below are maintained executable blocks. From a source checkout,
run them with the standard-library helper:

```bash
python examples/run_first_run.py \
  --document "$PWD/docs/document-first-run.md" \
  --work-dir /tmp/pageledger-document-first-run \
  --python "$(command -v python)" \
  --source-root "$PWD" \
  --expected-version 0.5.2
```

The helper supplies the recovery helper path as
`PAGELEDGER_TUTORIAL_RECOVERY_HELPER`. If you copy the executable blocks out
of the helper, set that variable to the checkout's
`examples/run_document_recovery.py` first.

## Make a synthetic source and configuration

The form-feed character creates three source pages. The text is intentionally
ordinary and synthetic. A completed job means the configured processing stages
finished; it does not mean a person reviewed or approved the pages.

```bash pageledger-tutorial
PYTHON="${PYTHON:-python}"
mkdir -p jobs
"$PYTHON" - <<'PY'
from pathlib import Path

Path("sample-document.txt").write_text(
    "Synthetic page one: a short archival-style paragraph.\f"
    "Synthetic page two: a second paragraph for the review example.\f"
    "Synthetic page three: the final paragraph in this fixture.",
    encoding="utf-8",
)
PY
cat > document-job.yml <<'YAML'
schema_version: "0.1"
processing:
  local_text:
    adapter: text
  local_ocr: null
YAML
```

## Process, inspect, and verify a completed job

`process` writes the job and its child run to disk. `inspect-job` reads the
document report, while `verify-job` checks the source fingerprint (a SHA-256
hash of the file), saved attempts, selected output, and report consistency.

```bash pageledger-tutorial
pageledger process sample-document.txt --config document-job.yml --out jobs/complete
pageledger inspect-job jobs/complete
pageledger verify-job jobs/complete
```

The report shows unknown cost because the local adapter supplies no charge.
Unknown cost is different from a known zero. The selected text remains subject
to source review even though processing completed.

## Optional: interrupt after saving a response, then resume

The checked-in recovery helper uses a test-only hook outside PageLedger's
production code. It interrupts immediately after the first response record is
saved to disk, resumes the same job, and logs adapter calls. This demonstrates
that PageLedger reuses the saved response and does not extract page 1 twice.

```bash pageledger-tutorial
CALLS_PATH="$PWD/calls-interrupted.jsonl"
"$PYTHON" "${PAGELEDGER_TUTORIAL_RECOVERY_HELPER:-run_document_recovery.py}" \
  --source sample-document.txt --config document-job.yml \
  --out jobs/interrupted --calls "$CALLS_PATH"
pageledger inspect-job jobs/interrupted
pageledger verify-job jobs/interrupted
"$PYTHON" - <<'PY'
import json
from pathlib import Path

calls = [json.loads(line) for line in Path("calls-interrupted.jsonl").read_text().splitlines()]
assert [call["page_number"] for call in calls] == [1, 2, 3]
print("DOCUMENT_JOURNEY_RECOVERY_OK")
PY
```

The helper prints an interruption receipt before resuming. The call log is
concrete evidence that completed calls were not duplicated.

## Apply a review record tied to a page and output

Review records are tied to this source page and exact output. This example
first rejects a stale record without making an adapter call, then reviews only
page 1 with a clearly synthetic reviewer name. That name documents the
fixture; it is not a claim of human review.

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
if pageledger review-job jobs/complete --review stale-review.json; then
  echo "stale receipt unexpectedly accepted" >&2
  exit 1
fi
echo DOCUMENT_JOURNEY_STALE_RECEIPT_REJECTED
pageledger review-job jobs/complete --review valid-review.json
pageledger inspect-job jobs/complete
pageledger verify-job jobs/complete
echo DOCUMENT_JOURNEY_REVIEW_OK
```

The final report contains one reviewed page and two pages that still need
review. A completed processing status, a source review decision, and cost
evidence are separate facts.
