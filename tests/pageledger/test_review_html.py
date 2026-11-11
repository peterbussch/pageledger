"""The review view: a reading copy of a job whose decisions leave as a review sheet."""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from test_processing import TEXT, StageAdapter, _contesting, launch, setup  # noqa: F401

from pageledger import review_html, runner
from pageledger.checkpoint import read_record
from pageledger.cli import main
from pageledger.corrections import word_changes
from pageledger.processing import create_review_sheet, review_job, verify_job
from pageledger.review_html import (
    CSV_JS,
    DECISIONS_JS,
    decisions_csv,
    diff_html,
    render_page_image,
    text_html,
    write_review_html,
)

# Leading blank lines, which an HTML parser drops after <pre> unless guarded.
HOSTILE = (
    "\n\n"
    + (
        'Before </script><script>alert("x")</script> & \'quoted\' "words" <b>bold</b>\r\n'
        "  indented\ttabbed  line\n"
        "سطر عربي mixed with Latin\n"
    )
    * 3
)


@pytest.fixture
def job(tmp_path, monkeypatch):
    """Two pages; page 2 is held after local text, and OCR reads it as hostile text."""
    source = tmp_path / "scans" / "source.txt"
    source.parent.mkdir()
    source.write_text(TEXT + "\f" + TEXT)
    config = tmp_path / "config.yml"
    stages = {stage: {"adapter": stage} for stage in ("local_text", "local_ocr")}
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": stages}))

    def adapter(name, *args, **kwargs):
        stage = StageAdapter()
        object.__setattr__(stage, "stage", name)
        object.__setattr__(stage, "defective", {2})
        object.__setattr__(stage, "texts", {"local_ocr": HOSTILE})
        return stage

    monkeypatch.setattr(runner, "load_adapter", adapter)
    monkeypatch.delenv("PAGELEDGER_REVIEWER", raising=False)
    root = tmp_path / "job"
    from pageledger.processing import process

    process(source=source, config_path=config, out_dir=root)
    return root


def _embedded(document: str) -> dict:
    found = re.search(
        r'<script type="application/json" id="review-data">(.*?)</script>', document, re.S
    )
    assert found
    return json.loads(found.group(1))


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".resume.lock"
    }


def test_the_view_shows_each_page_and_changes_nothing_in_the_job(job, tmp_path):
    before = _files(job)
    result = write_review_html(job, tmp_path / "view", images="none")

    assert _files(job) == before
    assert result["pages"] == 2 and result["images"] == 0
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert sorted(path.name for path in (tmp_path / "view").iterdir()) == [
        "index.html",
        "review.csv",
        "review.csv.binding.json",
    ]
    assert 'id="p0001"' in document and 'id="p0002"' in document
    # The held page comes first in the list.
    assert document.index('href="#p0002"') < document.index('href="#p0001"')
    assert "Text may be missing or damaged in the reading (coverage_defect)" in document
    assert "warnings.md#document-job-dispositions" in document
    assert "recorded in the job until the downloaded file is applied." in document
    assert "review.json" in document
    assert "pageledger review " in document and "--apply" in document
    assert "--reviewer" in document
    assert "No page image in this view." in document
    # Nothing is fetched; the only web addresses are links to the documentation.
    assert not re.search(r"src=\"[a-z]+:|<link|@import|url\(", document)
    links = re.findall(r'href="(https?:[^"]*)"', document)
    assert links and all(link.startswith(review_html.DOCS) for link in links)
    assert "default-src &#x27;none&#x27;" in document


def test_the_content_policy_allows_exactly_the_views_own_style_and_script(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    policy = html.unescape(re.search(r'Content-Security-Policy" content="([^"]*)"', document)[1])
    style = re.search(r"<style>(.*?)</style>", document, re.S)[1]
    script = re.findall(r"<script>(.*?)</script>", document, re.S)
    assert len(script) == 1
    for body, directive in ((style, "style-src"), (script[0], "script-src")):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"{directive} 'sha256-{digest}'" in policy


def test_job_text_is_escaped_and_shown_exactly(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")

    assert 'alert("x")' not in document
    assert "<b>bold</b>" not in document
    # Only the view's own two scripts close a script element.
    assert document.count("</script>") == 2
    shown = text_html(HOSTILE)
    assert '<span class="ctl" title="U+000D">␍</span>' in shown
    assert f'<pre class="text" dir="auto">\n{shown}</pre>' in document
    assert html.unescape(re.sub(r"<span class=\"ctl\"[^>]*>␍</span>", "\r", shown)) == HOSTILE


@pytest.mark.parametrize(
    "character",
    ["‮", "⁦", "‏", "​", "­", "﻿", "\x85", "\U000e0041"],
    ids=["rlo", "lri", "rlm", "zwsp", "soft-hyphen", "bom", "c1-nel", "tag"],
)
def test_characters_a_reader_cannot_see_are_drawn(character):
    text = f"pay {character}01$ now"
    shown = text_html(text)
    code = f"U+{ord(character):04X}"
    assert character not in shown
    assert f'<span class="ctl" title="{code}' in shown and f">{code}</span>" in shown
    restored = re.sub(r'<span class="ctl" title="U\+([0-9A-F]+)[^"]*">[^<]*</span>', "", shown)
    assert html.unescape(restored) == text.replace(character, "")


def test_joiners_that_shape_a_script_are_left_as_written():
    persian = "می‌خواهم क्‍ष"
    assert text_html(persian) == persian


def test_a_link_to_a_page_image_keeps_a_hash_in_its_name():
    assert review_html._href("../scans/scan#2.jpg") == "../scans/scan%232.jpg"
    assert review_html._href("../a b.pdf", "#page=3") == "../a%20b.pdf#page=3"
    assert review_html._href("javascript:alert(1)").startswith("javascript%3A")


def test_readings_too_long_to_compare_are_shown_without_a_diff(job, tmp_path, monkeypatch):
    assert diff_html("a b c", "a x c") is not None
    monkeypatch.setattr(review_html, "MAX_DIFF_CELLS", 16)
    assert diff_html("a b c", "a x c") is not None
    assert diff_html("a b c d", "a x c d") is None
    monkeypatch.setattr(review_html, "MAX_DIFF_CELLS", 4)
    write_review_html(job, tmp_path / "view", images="none")
    page = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    page = page[page.index('id="p0002"') :]
    assert "Not compared word by word" in page and "<del>" not in page
    assert text_html(HOSTILE) in page


def test_the_source_rendered_is_a_copy_of_the_verified_bytes(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4 recorded")
    recorded = hashlib.sha256(source.read_bytes()).hexdigest()
    copy = review_html._verified_copy(source, recorded, tmp_path / "copy.pdf")
    assert copy.read_bytes() == source.read_bytes()
    source.write_bytes(b"%PDF-1.4 swapped")
    with pytest.raises(ValueError, match="changed since the job was verified"):
        review_html._verified_copy(source, recorded, tmp_path / "again.pdf")


def test_embedded_rows_and_binding_are_what_review_sheet_writes(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    # A sheet at the same depth, so its links to the source are the same.
    sheet = tmp_path / "sheet" / "review.csv"
    create_review_sheet(job, sheet)
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        assert data["rows"] == list(csv.DictReader(stream))
    assert data["binding"] == sheet.with_name("review.csv.binding.json").read_text()
    assert (tmp_path / "view" / "review.csv").read_bytes() == sheet.read_bytes()
    assert decisions_csv(data["rows"], {}) == sheet.read_bytes()


def test_decisions_downloaded_from_the_page_are_accepted_by_review_job(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    page2 = read_record(job / "job.json")["pages"][1]
    ocr = next(a["attempt_id"] for a in page2["attempts"] if a["stage"] == "local_ocr")
    decisions = {
        "p0001": {"decision": "accept", "note": 'Checked, "twice", against\nthe scan'},
        "p0002": {"decision": f"use:{ocr}", "note": ""},
    }
    sheet = tmp_path / "view" / "review.csv"
    sheet.write_bytes(decisions_csv(data["rows"], decisions))

    result = review_job(job, sheet, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 2}
    pages = read_record(job / "job.json")["pages"]
    assert pages[0]["review"]["decisions"][0]["reason"] == 'Checked, "twice", against\nthe scan'
    assert pages[1]["selected_attempt"] == ocr
    assert verify_job(job)["status"] == "pass"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_javascript_builds_the_same_sheet(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    decisions = {"p0001": {"decision": "blank", "note": 'a "note", with commas\nand lines'}}
    program = (
        CSV_JS
        + "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        + "process.stdout.write(Buffer.from(buildCsv(input.fields, input.rows, input.decisions)));"
    )
    built = subprocess.run(
        ["node", "-e", program],
        input=json.dumps({**data, "decisions": decisions}).encode(),
        capture_output=True,
        check=True,
    ).stdout
    assert built == decisions_csv(data["rows"], decisions)
    sheet = tmp_path / "view" / "review.csv"
    sheet.write_bytes(built)
    assert review_job(job, sheet, reviewer="A. Reader")["decisions"] == {"reviewed_blank": 1}


def test_the_decision_choices_are_review_jobs_own(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    page = document[document.index('id="p0002"') :]
    values = re.findall(r'<option value="([^"]*)"', page[: page.index("</select>")])
    record = read_record(job / "job.json")["pages"][1]
    others = [
        f"use:{a['attempt_id']}"
        for a in record["attempts"]
        if a["attempt_id"] != record["selected_attempt"] and a["outcome"] == "completed"
    ]
    assert others
    assert values == [
        "",
        "accept",
        *others,
        "correct",
        "blank",
        "illustration",
        "handwriting",
        "unreadable",
        "source_defect",
    ]


def test_other_readings_show_their_differences_from_the_selected_text(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    page = document[document.index('id="p0002"') :]
    assert "<details><summary>" in page
    assert "<del>" in page and "<ins>" in page


def test_a_word_diff_keeps_every_character_of_the_selected_text():
    selected = "one  two\tthree\nfour <five>"
    other = "zero one too three four <five> six"
    shown = diff_html(selected, other)
    assert "<del>two</del><ins>too</ins>" in shown
    assert shown.startswith("<ins>zero</ins>")
    assert shown.endswith("<ins>six</ins>")
    kept = re.sub(r"<ins>.*?</ins>", "", shown)
    assert html.unescape(re.sub(r"</?del>", "", kept)) == selected


def test_page_images_follow_the_option(job, tmp_path, monkeypatch):
    rendered = []

    def render(source, page_number, target):
        rendered.append(page_number)
        target.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(review_html, "render_page_image", render)
    result = write_review_html(job, tmp_path / "held", images="held")
    assert rendered == [2] and result["images"] == 1
    document = (tmp_path / "held" / "index.html").read_text(encoding="utf-8")
    assert '<img src="images/p0002.jpg"' in document
    assert '<img src="images/p0001.jpg"' not in document
    assert (tmp_path / "held" / "images" / "p0002.jpg").read_bytes() == b"jpeg"

    rendered.clear()
    write_review_html(job, tmp_path / "all", images="all")
    assert rendered == [1, 2]


def test_a_failed_render_leaves_no_folder(job, tmp_path, monkeypatch):
    def render(source, page_number, target):
        raise RuntimeError("pdftoppm could not render page 2")

    monkeypatch.setattr(review_html, "render_page_image", render)
    with pytest.raises(RuntimeError):
        write_review_html(job, tmp_path / "view", images="held")
    assert not (tmp_path / "view").exists()


def test_a_text_source_has_no_page_images(job, tmp_path):
    result = write_review_html(job, tmp_path / "view")
    assert result["images"] == 0
    assert not (tmp_path / "view" / "images").exists()


def test_rendering_a_pdf_page_needs_poppler(tmp_path, monkeypatch):
    assert render_page_image(tmp_path / "page.txt", 1, tmp_path / "x.jpg") is False
    monkeypatch.setattr(review_html.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="--images none"):
        render_page_image(tmp_path / "page.pdf", 1, tmp_path / "x.jpg")


def test_the_view_refuses_an_existing_folder_and_a_job_that_does_not_verify(job, tmp_path):
    (tmp_path / "taken").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        write_review_html(job, tmp_path / "taken", images="none")
    report = job / "report.md"
    report.write_text(report.read_text() + "edited\n")
    with pytest.raises(ValueError, match="does not verify"):
        write_review_html(job, tmp_path / "view", images="none")
    assert not (tmp_path / "view").exists()


def test_the_command_writes_the_view(job, tmp_path, capsys):
    assert main(["review", str(job), "--out", str(tmp_path / "view"), "--images", "none"]) == 0
    assert "Review view:" in capsys.readouterr().out
    assert main(["review", str(job), "--out", str(tmp_path / "view"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_open_contested_spans_are_marked_in_the_text(setup, monkeypatch, tmp_path):  # noqa: F811
    _contesting(setup, monkeypatch)
    assert launch(setup, pages="1")["status"] == "completed"
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["disposition"] == "contested"

    write_review_html(setup[2], tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")

    assert document.count('<mark class="open"') == page["contested"]["open"] == 8
    assert 'data-kind="word"' in document
    assert "Open contested spans (8)" in document
    assert "marked in the text" in document
    assert '<mark class="open" id="p0001-sp_' in document
    assert 'href="#p0001-sp_' in document
    marked = document[document.index("<h3>Selected text</h3>") :]
    assert marked.index('<mark class="open"') < marked.index("</pre>")


def _node(program: str, payload: object) -> bytes:
    return subprocess.run(
        ["node", "-e", CSV_JS + DECISIONS_JS + program],
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=True,
    ).stdout


def test_the_view_embeds_each_reading_a_correction_can_start_from(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    data = _embedded(document)
    page = read_record(job / "job.json")["pages"][1]
    ocr = next(a for a in page["attempts"] if a["stage"] == "local_ocr")
    # The selected text, exactly, and the other completed reading by its attempt ID.
    assert page["selected_attempt"] != ocr["attempt_id"]
    assert data["texts"]["p0002"] == {"": TEXT, ocr["attempt_id"]: HOSTILE}
    assert set(data["texts"]["p0001"]) == {""}
    assert 'name="text"' in document and 'name="from"' in document
    assert "Correct the text" in document
    # Job text reaches the page only as escaped markup, JSON and textarea values.
    assert "innerHTML" not in review_html._SCRIPT
    assert document.count("</script>") == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_correction_downloaded_from_the_page_is_recorded_exactly(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    # ѣ, a combining accent, a tab, trailing spaces and Windows line endings.
    typed = TEXT.replace("synthetic", "синтетическій ѣ е́\t", 1) + "  \r\nlast line  \r\n"
    page2 = read_record(job / "job.json")["pages"][1]
    ocr = next(a for a in page2["attempts"] if a["stage"] == "local_ocr")
    decisions = {
        "p0001": {"decision": "correct", "note": "typed", "from": "", "text": typed},
        "p0002": {
            "decision": "correct",
            "note": "",
            "from": ocr["attempt_id"],
            "text": HOSTILE + "\nadded",
        },
    }
    built = _node(
        "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "process.stdout.write(buildDecisions(input.binding, input.rows, input.decisions));",
        {**data, "decisions": decisions},
    )
    path = tmp_path / "view" / "review.json"
    path.write_bytes(built)
    schemas = Path(__file__).resolve().parents[2] / "schemas"
    validate(json.loads(built), json.loads((schemas / "review-decisions.schema.json").read_text()))

    result = review_job(job, path, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 2}
    first, second = read_record(job / "job.json")["pages"]
    stored = job / first["review"]["decisions"][0]["correction"]["artifact"]
    assert stored.read_bytes() == typed.replace("\r\n", "\n").encode("utf-8")
    assert first["review"]["decisions"][0]["reason"] == "typed"
    assert second["selected_attempt"] == ocr["attempt_id"]
    assert second["review"]["decisions"][0]["correction"]["words"] == {
        "changed": 0,
        "added": 1,
        "removed": 0,
    }
    assert verify_job(job)["status"] == "pass"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_word_diff_marks_changes_as_review_apply_counts_them():
    pairs = [
        ["a b c d", "a x c d"],
        ["Тула стоит", "Туза  стоит\nна реке"],
        ["one two three", "one three"],
        ["хлѣб", "хлеб"],
    ]
    out = json.loads(
        _node(
            "const pairs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(pairs.map(p => wordDiff(p[0], p[1]))));",
            pairs,
        )
    )
    for (before, after), diff in zip(pairs, out, strict=True):
        assert diff["counts"] == word_changes(before, after)
        kept = "".join(part["text"] for part in diff["parts"] if part["kind"] != "del")
        assert kept.split() == after.split()
    assert {"kind": "del", "text": "b"} in out[0]["parts"]
    assert {"kind": "ins", "text": "x"} in out[0]["parts"]


def test_a_corrected_page_shows_the_correction_and_the_reading_beneath(job, tmp_path):
    sheet = tmp_path / "sheet" / "review.csv"
    create_review_sheet(job, sheet)
    (sheet.parent / "p1.txt").write_text(TEXT.replace("prose", "verse"), encoding="utf-8")
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    sheet.write_bytes(decisions_csv(rows, {"p0001": {"decision": "correct:p1.txt"}}))
    review_job(job, sheet, reviewer="A. Reader")

    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    data = _embedded(document)
    page = read_record(job / "job.json")["pages"][0]
    assert "a correction by A. Reader at" in document
    assert (
        "A person checked the text against the page (reviewed_text) with a correction, "
        "by A. Reader" in document
    )
    assert data["texts"]["p0001"] == {
        "": TEXT.replace("prose", "verse"),
        page["selected_attempt"]: TEXT,
    }


# A stand-in for the few browser objects the view's script touches, so its event
# wiring runs in node: each form is built from the options the page itself offers.
_STUB_DOM = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const saved = [];
function element(attributes) {
  return {value: "", disabled: true, textContent: "", handlers: {}, children: [],
    attributes: attributes || {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
    get firstChild() { return this.children[0] || null; },
    getAttribute(name) { return this.attributes[name]; },
    fire(type) { (this.handlers[type] || function () {})({preventDefault() {}}); }};
}
function choice(values) {
  const node = element();
  let current = "";
  node.options = values.map(value => ({value}));
  Object.defineProperty(node, "value", {
    get() { return current; }, set(value) { current = values.includes(value) ? value : ""; }});
  return node;
}
function load(storage) {
  const ids = {};
  for (const id of ["download-decisions", "download-sheet", "download-binding", "forget",
                    "storage", "download-problem"]) { ids[id] = element(); }
  ids["review-data"] = {textContent: JSON.stringify(input.data)};
  const forms = input.forms.map(spec => {
    const parts = {decision: choice(spec.decision), note: element(),
      text: spec.from ? element() : undefined, from: spec.from ? choice(spec.from) : undefined};
    const found = {".changes": element(), ".marked": element(), ".problem": element()};
    const form = element({"data-row": spec.row});
    form.elements = parts;
    form.querySelector = selector => found[selector];
    return form;
  });
  global.document = {
    getElementById: id => ids[id],
    querySelectorAll: selector => selector === "form.decision" ? forms : [],
    createElement: tag => ({tag, textContent: "", click() { saved.push(this); }, remove() {}}),
    createTextNode: text => ({text}),
    body: {appendChild() {}},
  };
  global.window = {confirm: () => true, localStorage: {
    getItem: key => storage[key] === undefined ? null : storage[key],
    setItem: (key, value) => { storage[key] = value; }}};
  global.Blob = function (parts) { this.text = parts.join(""); };
  global.URL = {createObjectURL: blob => blob, revokeObjectURL() {}};
  new Function(input.script)();
  const byRow = {};
  forms.forEach(form => { byRow[form.getAttribute("data-row")] = form; });
  return {ids, forms: byRow};
}
"""


def _forms(document: str) -> list[dict]:
    forms = []
    for row, body in re.findall(r'<form class="decision" data-row="(p\d+)"(.*?)</form>', document):
        selects = dict(
            re.findall(r'<select id="p\d+-(\w+)" name="\w+" disabled>(.*?)</select>', body)
        )
        values = {
            name: re.findall(r'<option value="([^"]*)"', options)
            for name, options in selects.items()
        }
        forms.append({"row": row, "decision": values["decision"], "from": values.get("from")})
    return forms


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_script_keeps_a_correction_across_a_reload_and_forgets_it(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    data = _embedded(document)
    forms = _forms(document)
    # A page without selected text starts from its first reading.
    ocr = next(key for key in data["texts"]["p0002"] if key)
    data["texts"]["p0002"] = {ocr: data["texts"]["p0002"][ocr]}
    for form in forms:
        if form["row"] == "p0002":
            form["from"] = [ocr]
    program = (
        _STUB_DOM
        + r"""
const storage = {};
let page = load(storage);
const first = page.forms.p0001, second = page.forms.p0002;
const report = {start: first.elements.text.value, secondFrom: second.elements.from.value,
                secondStart: second.elements.text.value};
first.elements.text.value = first.elements.text.value + "\tadded  ";
first.elements.text.fire("input");
report.decision = first.elements.decision.value;
report.marked = first.querySelector(".marked").children.map(c => c.tag || "text");
page.ids["download-sheet"].fire("click");
report.sheetProblem = page.ids["download-problem"].textContent;
page.ids["download-decisions"].fire("click");
report.download = saved[saved.length - 1].href.text;
page = load(storage);
report.reloaded = {decision: page.forms.p0001.elements.decision.value,
                   text: page.forms.p0001.elements.text.value};
page.ids["forget"].fire("click");
report.forgotten = {decision: page.forms.p0001.elements.decision.value,
                    text: page.forms.p0001.elements.text.value,
                    stored: JSON.parse(Object.values(storage)[0])};
process.stdout.write(JSON.stringify(report));
"""
    )
    report = json.loads(
        _node(program, {"data": data, "forms": forms, "script": review_html._SCRIPT})
    )

    assert report["start"] == TEXT
    assert report["secondFrom"] == ocr and report["secondStart"] == HOSTILE.replace("\r\n", "\n")
    assert report["decision"] == "correct"
    assert "ins" in report["marked"]
    assert "cannot carry corrected text" in report["sheetProblem"]
    assert report["reloaded"] == {"decision": "correct", "text": TEXT + "\tadded  "}
    assert report["forgotten"] == {"decision": "", "text": TEXT, "stored": {}}
    path = tmp_path / "view" / "review.json"
    path.write_text(report["download"], encoding="utf-8")
    review_job(job, path, reviewer="A. Reader")
    page = read_record(job / "job.json")["pages"][0]
    stored = job / page["review"]["decisions"][0]["correction"]["artifact"]
    assert stored.read_text(encoding="utf-8") == TEXT + "\tadded  "


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("control", ["‮", "⁧", "‏", "\x85", "\x07"])
def test_the_editor_declines_a_reading_with_controls_and_notes_like_formulas(
    job, tmp_path, control
):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    data = _embedded(document)
    data["texts"]["p0001"] = {"": f"pay {control}01$ now"}
    persian = "می‌خواهم"
    data["texts"]["p0002"] = {key: persian for key in data["texts"]["p0002"]}
    program = (
        _STUB_DOM
        + r"""
const page = load({});
const first = page.forms.p0001, second = page.forms.p0002;
const report = {declined: first.elements.text.disabled,
                why: first.querySelector(".changes").textContent,
                shaped: second.elements.text.disabled, problems: []};
for (const note of ["=1", "\t=1", "+1", "@x", "-1 word illegible"]) {
  second.elements.note.value = note;
  second.elements.note.fire("input");
  report.problems.push(second.querySelector(".problem").textContent);
}
process.stdout.write(JSON.stringify(report));
"""
    )
    report = json.loads(
        _node(program, {"data": data, "forms": _forms(document), "script": review_html._SCRIPT})
    )
    assert report["declined"] is True and "text-direction" in report["why"]
    assert report["shaped"] is False
    assert all("spreadsheet formula" in problem for problem in report["problems"][:4])
    assert report["problems"][4] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_correction_keeps_soft_hyphens_and_zero_width_spaces_byte_for_byte(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    data = _embedded(document)
    reading = "Gover­nment​directory of Tula, 1912.\nIvanov, I.­P. ﻿clerk"
    data["texts"]["p0001"] = {"": reading}
    program = (
        _STUB_DOM
        + r"""
const page = load({});
const first = page.forms.p0001;
const report = {editable: !first.elements.text.disabled,
                before: first.querySelector(".changes").textContent};
first.elements.text.value = first.elements.text.value.replace("Tula", "Tver");
first.elements.text.fire("input");
report.after = first.querySelector(".changes").textContent;
page.ids["download-decisions"].fire("click");
report.download = saved[saved.length - 1].href.text;
process.stdout.write(JSON.stringify(report));
"""
    )
    report = json.loads(
        _node(program, {"data": data, "forms": _forms(document), "script": review_html._SCRIPT})
    )
    assert report["editable"] is True
    for message in (report["before"], report["after"]):
        assert "holds 4 characters a text box does not show (U+00AD, U+200B, U+FEFF)" in message
        assert "stay exactly where they are" in message
    path = tmp_path / "view" / "review.json"
    path.write_text(report["download"], encoding="utf-8")
    expected = reading.replace("Tula", "Tver")
    assert json.loads(report["download"])["rows"][0]["text"] == expected
    review_job(job, path, reviewer="A. Reader")
    page = read_record(job / "job.json")["pages"][0]
    stored = job / page["review"]["decisions"][0]["correction"]["artifact"]
    assert stored.read_bytes() == expected.encode("utf-8")
