# First run: text, review, rerun, and replay

This maintained tutorial uses the built-in `text` adapter, a synthetic
three-page file, and no network, OCR engine, or provider. Run it in a fresh
scratch directory after installing PageLedger. It shows the ordinary reader
journey first: run, inspect, open the raw text and audit, export CSV, and verify.
The selective rerun follows. Bundle relocation and replay are last and optional.

This guide targets **0.4.1**. If that version is not yet on the package index,
install the exact candidate wheel by absolute path, then work outside the
checkout:

```bash
python -m venv /tmp/pageledger-0.4.1
. /tmp/pageledger-0.4.1/bin/activate
python -m pip install /absolute/path/to/pageledger-0.4.1-py3-none-any.whl
pageledger --version
mkdir /tmp/pageledger-reader-tutorial
cd /tmp/pageledger-reader-tutorial
```

As of 2026-09-06, `pip install pageledger` installed the older 0.2.0 stable
release. Use the documentation shipped with an older installed version rather
than assuming it has the 0.4.1 replay and reader behavior described here.

## Run and inspect

The form-feed characters below make three source pages. PageLedger preserves
their original numbering in IDs such as `doc_0001_page_0002`. The replacement
character on page 2 is deliberate: it creates one quality warning and one
review item.

```bash pageledger-tutorial
PYTHON="${PYTHON:-python}"
"$PYTHON" - <<'PY'
from pathlib import Path

pages = [
    "A clean opening page with ordinary prose for the tutorial reader.",
    "A damaged page contains one replacement character � for review.",
    "A clean closing page with enough ordinary prose to avoid a short-text signal.",
]
Path("sample.txt").write_text("\f".join(pages), encoding="utf-8")
PY

cat > pageledger.yml <<'YAML'
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
  max_rerun_depth: 2
YAML

pageledger run sample.txt --config pageledger.yml --out runs/first
pageledger inspect-run runs/first
pageledger inspect-run runs/first --csv > pages.csv

sed -n '1,5p' runs/first/raw/doc_0001_page_0002.txt
sed -n '1,80p' runs/first/audit.md
grep 'replacement_characters' pages.csv
pageledger verify-run runs/first
```

`quality.jsonl` contains observable signals, not a correctness verdict.
Likewise, an A signals grade means the configured heuristics did not fire; it
does not certify the transcription. Compare the raw page with the source image
or text before accepting it. Here, the CSV identifies source page 2 and the raw
path preserves that page identity.

The terminal says `Cost USD: unknown` because the local adapter did not report
a charge and the config supplied no accounting rate. That is not the same as a
known zero. If only some pages have cost evidence, PageLedger prints the known
subtotal as partial rather than presenting it as a complete charge.

## Rerun the selected page

Keep `sample.txt` in place: rerun verifies the original source bytes before it
does any extraction. The parent `rerun-manifest.yml` selects only page 2.

```bash pageledger-tutorial
pageledger rerun runs/first --config pageledger.yml --out runs/second
pageledger compare-runs runs/first runs/second
pageledger verify-run runs/second
```

This deliberately uses the same deterministic adapter on unchanged source, so
the result and warning remain unchanged. In a real project you might supply a
stronger adapter, but PageLedger does not assume a rerun is better and does not
assemble a corrected corpus automatically. Use `compare-runs`, inspect the
evidence, and explicitly select an output for downstream work.

## Record review decisions outside the run

Use a spreadsheet or other project-owned review log. The following CSV is an
example template, not a normative PageLedger artifact and not a claim that a
human made a decision. Its row remains `pending`; a reviewer would fill in the
decision, reason, identity/date, and selected output after checking the source.

```bash pageledger-tutorial
mkdir review
"$PYTHON" - <<'PY'
import csv
import json
from pathlib import Path

run_id = json.loads(Path("runs/first/manifest.json").read_text())["run_id"]
with Path("review/decisions.csv").open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow([
        "run_id", "page_id", "source", "page", "decision", "reason",
        "reviewer", "date", "selected_output",
    ])
    writer.writerow([
        run_id, "doc_0001_page_0002", "sample.txt", 2, "pending",
        "example only: inspect replacement-character signal", "", "", "",
    ])
PY

pageledger verify-run runs/first
pageledger verify-run runs/second
test -f review/decisions.csv
```

Do not edit `audit.md`, `audit.json`, or raw files inside a verified run to keep
review notes: `audit.md` is a rendering of `audit.json`, and changing either it
or the raw evidence invalidates verification. External notes can point to both
run ID and page ID without rewriting the evidence.

## Optional: relocate a bundle and replay

Bundling is separate from rerunning. A verified bundle carries its own source
copy, so this isolated demonstration moves both the bundle and the original
source before replay. It does not disturb the earlier rerun.

```bash pageledger-tutorial
pageledger bundle runs/first --out first-bundle
mkdir relocated
mv first-bundle relocated/first-bundle
mv sample.txt relocated/original-source.moved
pageledger replay relocated/first-bundle --out runs/replayed
pageledger verify-run runs/replayed
```

An exact replay proves equality under PageLedger's documented deterministic
replay checks. It does not recreate a hermetic operating system, certify text
accuracy, or replace source review.

## Maintainer verification

<details>
<summary>Automated receipts for the documented journey</summary>

The maintained test executes every designated tutorial block in one shell, then
runs these behavior checks. Readers do not need this assertion code to follow
the journey.

```bash pageledger-tutorial
"$PYTHON" - <<'PY'
import csv
import json
from pathlib import Path

first_quality = [
    json.loads(line)
    for line in Path("runs/first/quality.jsonl").read_text().splitlines()
]
flagged = [row for row in first_quality if row["warnings"]]
assert [(row["page_id"], row["warnings"]) for row in flagged] == [
    ("doc_0001_page_0002", ["replacement_characters"])
]
rows = list(csv.DictReader(Path("pages.csv").open(newline="", encoding="utf-8")))
assert rows[1]["page_number"] == "2"
assert rows[1]["warnings"] == "replacement_characters"
print("TUTORIAL_WARNING_OK")

second_manifest = json.loads(Path("runs/second/manifest.json").read_text())
second_quality = [
    json.loads(line)
    for line in Path("runs/second/quality.jsonl").read_text().splitlines()
]
assert second_manifest["summary"]["pages_total"] == 1
assert [row["page_id"] for row in second_quality] == ["doc_0001_page_0002"]
assert second_quality[0]["warnings"] == ["replacement_characters"]
assert sorted(path.name for path in Path("runs/second/raw").iterdir()) == [
    "doc_0001_page_0002.txt"
]
print("TUTORIAL_RERUN_SELECTION_OK")

assert Path("review/decisions.csv").is_file()
print("TUTORIAL_EXTERNAL_REVIEW_INTEGRITY_OK")

replay = json.loads(Path("runs/replayed/replay.json").read_text())
assert replay["outcome"] == "exact"
assert replay["raw"]["equal"] == 3
assert replay["raw"]["different"] == 0
assert replay["raw"]["missing"] == 0
assert Path("relocated/original-source.moved").is_file()
assert not Path("sample.txt").exists()
print("TUTORIAL_REPLAY_RELOCATION_OK")
PY
```

</details>

Release verification can drive the same blocks through the standard-library
helper. Its exact-wheel mode clears `PYTHONPATH`, asserts the intended version,
and fails if the import comes from the checkout:

```bash
PYTHONPATH= /path/to/wheel-venv/bin/python /path/to/checkout/examples/run_first_run.py \
  --document /path/to/checkout/docs/first-run.md \
  --work-dir /tmp/pageledger-first-run \
  --python /path/to/wheel-venv/bin/python \
  --expected-version 0.4.1 \
  --forbid-import-root /path/to/checkout
```

The helper and document may be read by absolute path from the checkout; the
product import and every `pageledger` command come from the isolated wheel
environment.
