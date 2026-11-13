"""A static review view: each page's image beside its text, in one HTML file.

The view is a rendering of a verified job, like report.md. It writes nothing into
the job. Decisions and corrected texts made in it leave the browser as a decisions
file (review.json, the sheet's bindings with the decided rows) or, without
corrections, as the same CSV and binding that `review --sheet` writes. They count
only once `review --apply` applies them.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import re
import shlex
import shutil
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from ._version import __version__
from .adapters import _run_ocr_command
from .adjudication import settle
from .checkpoint import atomic_bytes, file_digest, writer_lock
from .corrections import describe
from .document_report import (
    AGREEMENT_CAVEAT,
    _artifact_bytes,
    _attempt,
    _image_name,
    _lower,
    _short_comparison,
    empty_reading,
    held_words,
    needs_person,
    reader_name,
    relative_path,
    verify_document_report,
)
from .image_source import KIND, ImageSource, _read_page, carrier, page_target, snapshot_growth
from .processing import _check_source, _load, _refresh, verify_job
from .review_sheet import FIELDS, _row_id, binding_path, write_review_sheet
from .wording import CORRECTION, DECISION_GROUPS, HOLDS, REVIEWED, sentence
from .wording import warning as warning_words

IMAGE_CHOICES = ("all", "held", "none")
SHEET = "review.csv"
DECISIONS_FILE = "review.json"
# Stands for the downloaded decisions file in the command the view shows.
DOWNLOADED = "DOWNLOADED_FILE"
DOCS = f"https://github.com/peterbussch/pageledger/blob/v{__version__}/docs/"
_RENDER_SIDE = 1600
_RENDER_TIMEOUT_SECONDS = 120
# Characters a reader cannot see: controls and format characters (bidi overrides,
# zero-width spaces, soft hyphens, tags). Zero-width joiners shape Arabic, Persian
# and Indic script, so they are left as written. The editor's test is the same.
_SHOWN_AS_IS = "\t\n\u200c\u200d"
# The correction editor: a reading with controls a text box does not keep, or with
# bidirectional controls that reorder the text while it is edited, is corrected
# through a file; other invisible characters stay editable and are counted for
# the reviewer.
_UNEDITABLE_JS = r"/[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/"
_INVISIBLE_JS = r"/(?![\t\n])[\p{Cc}\p{Cf}]/gu"
# Comparing word by word costs up to the product of the two readings' word counts.
MAX_DIFF_CELLS = 25_000_000
_PIECES = re.compile(r"\s+|\S+")
_NO_POPPLER = (
    "Page images need pdftoppm on PATH; install Poppler, "
    "or write the view without images with --images none"
)
HOW_TO = "review-and-correct.md"
_DATA_OPEN = '<script type="application/json" id="review-data">'


def render_page_image(source: Path, page_number: int, target: Path) -> bool:
    """Write a JPEG of one source page to `target`; False when the source has no page images."""
    if source.suffix.lower() != ".pdf":
        return False
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        raise ValueError(_NO_POPPLER)
    with tempfile.TemporaryDirectory(prefix="pageledger-review-") as directory:
        output = Path(directory) / "page"
        _run_ocr_command(
            [
                pdftoppm,
                *("-f", str(page_number), "-l", str(page_number), "-singlefile"),
                *("-scale-to", str(_RENDER_SIDE), "-jpeg", "-jpegopt", "quality=80"),
                str(source),
                str(output),
            ],
            timeout=_RENDER_TIMEOUT_SECONDS,
            context=f"pdftoppm could not render page {page_number}",
        )
        target.write_bytes(output.with_suffix(".jpg").read_bytes())
    return True


def render_own_image(source: Path, image: dict, target: Path) -> bool:
    """Write a size-capped JPEG of one recorded page image, rendered through its own carrier.

    The image is read by its recorded name and hash, not by its place in the
    folder; False when Poppler is absent, so the view links to the image instead.
    """
    if shutil.which("pdftoppm") is None:
        return False
    path = source / image["name"] if source.is_dir() else source
    page = _read_page(path)
    if page.sha256 != image["sha256"]:
        raise ValueError(f"Page image changed since the job was verified: {path}")
    with carrier(ImageSource(path, (page,)), [1]) as (pdf, _sha256):
        return render_page_image(pdf, 1, target)


def write_review_html(job_dir: Path, out: Path, *, images: str = "all") -> dict:
    """Verify the job, then write the view, page images and a blank review sheet to `out`."""
    if images not in IMAGE_CHOICES:
        raise ValueError(f"--images must be one of: {', '.join(IMAGE_CHOICES)}")
    root = Path(job_dir).expanduser().resolve()
    verification = verify_job(root)
    if verification["status"] != "pass":
        raise ValueError(f"Document job does not verify: {verification['error']}")
    out = Path(out).expanduser().resolve()
    if out.exists() or out.is_symlink():
        raise ValueError(f"Output directory already exists: {out}")
    with writer_lock(root):
        job = _load(root)
        _refresh(job, root, materialize=False)
        try:
            source = _check_source(job)
        except ValueError:
            # Images a later fetch --resume added; each page is read by its own name.
            if snapshot_growth(job["source"]) is None:
                raise
            source = Path(job["source"]["path"]).expanduser()
        if images != "none" and source.suffix.lower() == ".pdf" and not shutil.which("pdftoppm"):
            raise ValueError(_NO_POPPLER)
        out.mkdir(parents=True)
        try:
            written = write_review_sheet(job, out / SHEET, root)
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)
            raise
    try:
        return _write_view(root, out, job, source, written, images)
    except BaseException:
        # A half-written folder would only be refused next time.
        shutil.rmtree(out, ignore_errors=True)
        raise


def view_state(job_dir: Path, out: Path) -> str:
    """Whether a folder holds a review view of the job as it is now: "current", "stale"
    (written for the job before it last changed), or "other" (not a review view of it).

    The view records the job's document.json as it was written from. Any change to the job, its status, readings or reviews, changes
    document.json; a newer PageLedger that only words things differently does not.
    """
    out = Path(out).expanduser()
    page = out / "index.html"
    if not all(path.is_file() for path in (page, out / SHEET, binding_path(out / SHEET))):
        return "other"
    root = Path(job_dir).expanduser().resolve()
    try:
        job_id = json.loads((root / "document.json").read_bytes())["job_id"]
        written = _view_record(page)
    except (OSError, ValueError, KeyError, TypeError):
        return "other"
    if written is None or written.get("job_id") != job_id:
        return "other"
    current = written.get("document_sha256") == file_digest(root / "document.json")
    return "current" if current else "stale"


def _view_record(page: Path) -> dict | None:
    """What a view records of the job it was written from, or None for an older view."""
    html_text = page.read_text(encoding="utf-8")
    start = html_text.find(_DATA_OPEN)
    end = html_text.find("</script>", start)
    if start < 0 or end < 0:
        return None
    data = json.loads(html_text[start + len(_DATA_OPEN) : end])
    view = data.get("view") if isinstance(data, dict) else None
    return view if isinstance(view, dict) else None


def _write_view(root: Path, out: Path, job: dict, source: Path, written: dict, images: str) -> dict:
    report = verify_document_report(root)
    sheet = Path(written["out"])
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    binding = binding_path(sheet).read_text(encoding="utf-8")
    rendered = 0
    pictures: dict[str, str] = {}
    recorded = job["source"]["images"] if job["source"].get("kind") == KIND else None
    with tempfile.TemporaryDirectory(prefix="pageledger-review-") as scratch:
        pdf = source
        if recorded is None and images != "none" and source.suffix.lower() == ".pdf":
            pdf = _verified_copy(source, job["source"]["sha256"], Path(scratch) / "source.pdf")
        for page in report["pages"]:
            if images == "none" or (images == "held" and not needs_person(page, report)):
                continue
            name = f"images/{_row_id(page)}.jpg"
            (out / "images").mkdir(exist_ok=True)
            drawn = (
                render_own_image(source, recorded[page["page_number"] - 1], out / name)
                if recorded is not None
                else render_page_image(pdf, page["page_number"], out / name)
            )
            if not drawn:
                (out / "images").rmdir()
                break
            pictures[page["page_id"]] = name
            rendered += 1
    view = _View(root, out, report, source, pictures)
    written_from = {
        "job_id": report["job_id"],
        "document_sha256": file_digest(root / "document.json"),
    }
    document = view.render(rows, binding, language=_language(job), written_from=written_from)
    atomic_bytes(out / "index.html", document.encode("utf-8"))
    return {
        "status": "written",
        "out": str(out / "index.html"),
        "sheet": str(sheet),
        "binding": written["binding"],
        "pages": len(report["pages"]),
        "images": rendered,
    }


def _verified_copy(source: Path, sha256: str, target: Path) -> Path:
    """Copy the source and check the copy is the recorded bytes, so Poppler renders them."""
    digest = hashlib.sha256()
    with source.open("rb") as reading, target.open("wb") as writing:
        for chunk in iter(lambda: reading.read(1024 * 1024), b""):
            digest.update(chunk)
            writing.write(chunk)
    if digest.hexdigest() != sha256:
        raise ValueError(f"Document job source changed since the job was verified: {source}")
    return target


def decisions_csv(rows: list[dict], decisions: dict[str, dict]) -> bytes:
    """The review sheet with the reviewer's columns filled in, as the page downloads it."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        chosen = decisions.get(row["row_id"], {})
        writer.writerow(
            {**row, "decision": chosen.get("decision", ""), "note": chosen.get("note", "")}
        )
    return buffer.getvalue().encode("utf-8-sig")


def _language(job: dict) -> str | None:
    lexicon = (job.get("config", {}).get("processing") or {}).get("lexicon") or {}
    language = lexicon.get("language")
    return language if isinstance(language, str) else None


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _invisible(character: str) -> bool:
    return unicodedata.category(character) in {"Cc", "Cf"} and character not in _SHOWN_AS_IS


def text_html(text: str) -> str:
    """Escape transcript text; characters a reader cannot see are drawn as marked symbols."""
    parts, last = [], 0
    for index, character in enumerate(text):
        if not _invisible(character):
            continue
        parts.append(_e(text[last:index]))
        code = ord(character)
        if code < 0x20 or code == 0x7F:
            symbol = chr(0x2421 if code == 0x7F else 0x2400 + code)
        else:
            symbol = f"U+{code:04X}"
        title = f"U+{code:04X} {unicodedata.name(character, '')}".rstrip()
        parts.append(f'<span class="ctl" title="{_e(title)}">{symbol}</span>')
        last = index + 1
    parts.append(_e(text[last:]))
    return "".join(parts)


def diff_html(selected: str, other: str) -> str | None:
    """The selected text, every character in place, with the other reading's words marked.

    Words only the selected reading has are in <del>; the other reading's words
    are inserted in <ins> after the words they replace. None when the readings
    are too long to compare word by word.
    """
    pieces = _PIECES.findall(selected)
    words = [index for index, piece in enumerate(pieces) if not piece.isspace()]
    theirs = [match for match in _PIECES.finditer(other) if not match.group().isspace()]
    if (len(words) + 1) * (len(theirs) + 1) > MAX_DIFF_CELLS:
        return None
    matcher = SequenceMatcher(
        None, [pieces[i] for i in words], [m.group() for m in theirs], autojunk=False
    )
    changed: set[int] = set()
    inserted: dict[int, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            changed.update(range(i1, i2))
        if tag in {"replace", "insert"}:
            inserted[i2 - 1] = other[theirs[j1].start() : theirs[j2 - 1].end()]
    position = {piece: word for word, piece in enumerate(words)}
    chunks = []
    if -1 in inserted:
        chunks.append(f"<ins>{text_html(inserted[-1])}</ins>")
    for index, piece in enumerate(pieces):
        word = position.get(index)
        shown = text_html(piece)
        chunks.append(f"<del>{shown}</del>" if word in changed else shown)
        if word is not None and word in inserted:
            chunks.append(f"<ins>{text_html(inserted[word])}</ins>")
    return "".join(chunks)


def spans_html(text: str, spans: list[dict], prefix: str) -> str | None:
    """Text with open contested spans marked at their offsets, or None if they overlap."""
    ordered = sorted(spans, key=lambda span: (span["start"], span["end"]))
    chunks, last = [], 0
    for span in ordered:
        start, end = span["start"], span["end"]
        if start < last or end > len(text):
            return None
        chunks.append(text_html(text[last:start]))
        anchor = _e(f"{prefix}-{span['span_id']}")
        chunks.append(
            f'<mark class="open" id="{anchor}" data-kind="{_e(span["kind"])}" '
            f'title="{_e(_span_title(span))}">{text_html(text[start:end])}</mark>'
        )
        last = end
    chunks.append(text_html(text[last:]))
    return "".join(chunks)


def _span_title(span: dict) -> str:
    witness = span["witness"] or "nothing"
    return f"{span['kind']}, open: the literal engine read {witness!r}"


def _codes(attempt: dict) -> list[str]:
    codes = []
    for warning in attempt.get("warnings") or []:
        code = warning.get("type", warning.get("code")) if isinstance(warning, dict) else warning
        if isinstance(code, str):
            codes.append(code)
    return codes


def _doc(page: str, anchor: str, label: str) -> str:
    return f'<a href="{_e(DOCS + page + "#" + anchor)}">{_e(label)}</a>'


class _View:
    def __init__(
        self,
        root: Path,
        out: Path,
        report: dict,
        source: Path,
        pictures: dict[str, str],
    ) -> None:
        self.root, self.out, self.report = root, out, report
        self.source, self.pictures = source, pictures

    def render(
        self, rows: list[dict], binding: str, *, language: str | None, written_from: dict
    ) -> str:
        self.lang = f' lang="{_e(language)}"' if language else ""
        report = self.report
        ordered = sorted(
            report["pages"], key=lambda page: (not needs_person(page, report), page["page_number"])
        )
        key = f"pageledger-review:{report['job_id']}:{hashlib.sha256(binding.encode()).hexdigest()[:16]}"
        data = {
            "key": key,
            "fields": list(FIELDS),
            "rows": rows,
            "binding": binding,
            "texts": {_row_id(page): self._starts(page) for page in report["pages"]},
            # The job as the view was written from it, for `review` to tell a stale view.
            "view": written_from,
        }
        embedded = (
            json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029")
        )
        title = Path(report["source"]["path"]).name
        counts = report["counts"]
        needing = sum(needs_person(page, report) for page in report["pages"])
        command = (
            f"pageledger review {shlex.quote(str(self.root))} "
            f'--apply {DOWNLOADED} --reviewer "YOUR NAME"'
        )
        body = [
            '<a class="skip" href="#pages">Skip to the page list</a>',
            "<header>",
            f"<h1>{text_html(title)}</h1>",
            f"<p>{counts['selected_pages']} pages in this job; {needing} need a person; "
            f"{counts['unresolved_pages']} not yet reviewed as text or blank. "
            f"{_STATUS.get(report['status'], '')}</p>",
            "</header>",
            '<section class="decisions" aria-labelledby="decisions-title">',
            '<h2 id="decisions-title">Recording decisions</h2>',
            "<p><strong>Nothing you choose or type here is recorded in the job until the "
            "downloaded file is applied.</strong> Choose a decision under each page, or correct "
            "its text. This browser keeps your choices until you download them as "
            f"<code>{DECISIONS_FILE}</code>. Then run this, with the path of the file your "
            f"browser downloaded in place of <code>{DOWNLOADED}</code> (browsers save to "
            "your Downloads folder, and may name the file "
            f"<code>review (1).json</code>):</p>",
            f'<pre class="command">{_e(command)}</pre>',
            "<p>Add <code>--dry-run</code> to check the file first without recording anything. "
            f"{_doc(HOW_TO, 'review-and-correct', 'How to review and correct')} explains "
            "each step.</p>",
            '<p class="controls">'
            '<button type="button" id="download-decisions" disabled>'
            f"Download my decisions ({DECISIONS_FILE})</button> "
            '<button type="button" id="forget" disabled>Forget decisions in this browser</button>'
            "</p>",
            '<p id="download-problem" class="problem" role="alert"></p>',
            '<p id="storage" role="status"></p>',
            _choices_legend(),
            "<details><summary>Working in a spreadsheet instead</summary>",
            f"<p><code>{SHEET}</code> in this folder has one row per page; type a decision in "
            "its <code>decision</code> column (the codes in parentheses under <em>What each "
            "decision records</em>, or <code>use:</code> with a reading's ID, which each page "
            "lists under "
            "<em>From the record</em>), and apply the sheet with the same command, "
            f"naming <code>{SHEET}</code> in this folder. <code>review-decisions.txt</code> beside it lists "
            "the decisions. A sheet cannot carry corrected text in a cell: save the text in a "
            "file beside the sheet and write <code>correct:</code> and its name. Keep "
            f"<code>{SHEET}.binding.json</code> beside the sheet: it ties each row to the "
            "page's text as it was when the view was written.</p>",
            "<p>The decisions chosen in this page can also be downloaded as a filled-in sheet, "
            "except corrections:</p>",
            '<p class="controls">'
            '<button type="button" id="download-sheet" disabled>Download review.csv</button> '
            '<button type="button" id="download-binding" disabled>'
            f"Download {SHEET}.binding.json</button></p>",
            "</details>",
            "<noscript><p>Choosing decisions here needs JavaScript. Without it, fill in "
            f"<code>{SHEET}</code> in a spreadsheet instead.</p></noscript>",
            "</section>",
            '<section id="pages" aria-labelledby="pages-title">',
            '<h2 id="pages-title">Pages</h2>',
            "<p>Pages that need a person come first.</p>",
            '<div class="table"><table>',
            '<thead><tr><th scope="col">Page</th><th scope="col">Held for</th>'
            '<th scope="col">Review</th><th scope="col">Your decision</th></tr></thead><tbody>',
            *(self._row(page) for page in ordered),
            "</tbody></table></div></section>",
            *(self._page(page) for page in report["pages"]),
            '<p class="record">For the record: job '
            f"<code>{_e(report['job_id'])}</code>, status <code>{_e(report['status'])}</code>; "
            f"source SHA-256 <code>{_e(report['source']['sha256'])}</code>.</p>",
        ]
        return _document(title, "\n".join(body), embedded)

    def _row(self, page: dict) -> str:
        row_id = _row_id(page)
        return (
            f'<tr><th scope="row"><a href="#{row_id}">{page["page_number"]}'
            f"{text_html(_image_name(self.report, page))}</a></th>"
            f"<td>{_e(_why(page, self.report))}</td><td>{_e(_review(page))}</td>"
            f'<td class="chosen" data-row="{row_id}"></td></tr>'
        )

    def _page(self, page: dict) -> str:
        row_id, number = _row_id(page), page["page_number"]
        selected = page["selected_output"]
        path, fragment = page_target(self.report["source"], number)
        source_link = (relative_path(str(self.source), self.out) + path, fragment)
        facts = [
            ("Held for", _e(_why(page, self.report, every=True))),
            ("Selected text", _e(self._selected(page))),
            ("Engines agree", _e(_agreement(page, self.report))),
            ("Review", _e(_review(page))),
        ]
        # A page with no comparison the job weighed has nothing to say here.
        facts = [fact for fact in facts if fact[1] not in {"none counted", "none recorded"}]
        warnings = [
            f"{_e(sentence(self._name(attempt)))}: "
            + " ".join(_e(warning_words(code)) for code in _codes(attempt))
            for attempt in page["attempts"]
            if _codes(attempt) and not _imageless(attempt)
        ]
        if warnings:
            facts.append(("Warnings", "<br>".join(warnings)))
        open_spans, marked = self._open_spans(page)
        figure = self._figure(page, source_link)
        if selected is None:
            reading = f"<p>No text is selected for this page ({_e(_state(page))}).</p>"
        else:
            own = not (selected.get("edition") or selected.get("correction"))
            shown = marked.get(selected["attempt_id"]) if own else None
            reading = (
                f'<h3>Selected text</h3><pre class="text" dir="auto"{self.lang}>\n'
                f"{shown or text_html(selected['text'])}</pre>"
            )
        others = self._others(page, marked)
        parts = [
            f'<section class="page" id="{row_id}" aria-labelledby="{row_id}-title">',
            f'<h2 id="{row_id}-title">Page {number}{text_html(_image_name(self.report, page))}</h2>',
            '<p class="nav"><a href="#pages">Back to the page list</a></p>',
            "<dl>",
            *(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts),
            "</dl>",
            self._record(page),
            '<div class="sheet">',
            figure,
            '<div class="readings">',
            reading,
            open_spans,
            others,
            self._form(page),
            "</div></div></section>",
        ]
        return "\n".join(parts)

    def _name(self, attempt: dict) -> str:
        return reader_name(self.report, attempt)

    def _selected(self, page: dict) -> str:
        selected = page["selected_output"]
        if selected is None:
            return "none"
        attempt = next(a for a in page["attempts"] if a["attempt_id"] == selected["attempt_id"])
        name = sentence(self._name(attempt))
        correction = selected.get("correction")
        if correction:
            return (
                f"{sentence(CORRECTION)} of {self._name(attempt)}, by {correction['reviewer']} at "
                f"{correction['reviewed_at']} ({describe(correction['words'])})"
            )
        if selected.get("edition"):
            return f"{name}, with its settled differences applied"
        return name

    def _record(self, page: dict) -> str:
        """The page's codes and reading IDs, for looking up and for a sheet's use: decisions."""
        holds = ", ".join(f"<code>{_e(code)}</code>" for code in page["review_reasons"])
        readings = []
        for attempt in page["attempts"]:
            codes = " ".join(f"<code>{_e(code)}</code>" for code in _codes(attempt))
            failure = (attempt.get("failure") or {}).get("code")
            readings.append(
                f"<li>{_e(sentence(self._name(attempt)))}: <code>{_e(attempt['attempt_id'])}</code>"
                f", {_e(attempt['outcome'])}"
                + (f", failure <code>{_e(failure)}</code>" if failure else "")
                + (f"; {codes}" if codes else "")
                + "</li>"
            )
        selected = page["selected_output"]
        path = f"<p>Selected text: <code>{_e(selected['path'])}</code></p>" if selected else ""
        link = _doc("warnings.md", "document-job-dispositions", "what the codes mean")
        return (
            '<details class="record"><summary>From the record</summary>'
            f"<p>State <code>{_e(page['disposition'])}</code>"
            + (f"; held for {holds}" if holds else "")
            + f" ({link}).</p>"
            + (f"<ul>{''.join(readings)}</ul>" if readings else "")
            + path
            + "</details>"
        )

    def _figure(self, page: dict, source_link: tuple[str, str]) -> str:
        label = f"Page {page['page_number']}{_image_name(self.report, page)} in the source"
        links = [f'<a href="{_e(_href(*source_link))}">{text_html(label)}</a>']
        for attempt in page["attempts"]:
            evidence = attempt.get("input_evidence")
            if not evidence:
                continue
            tiled = evidence.get("kind") == "page_tiles"
            for image in evidence["tiles"] if tiled else [evidence]:
                path = self.root / attempt["run_path"] / image["artifact"]
                if file_digest(path) != image["sha256"]:
                    raise ValueError(f"Image evidence for {page['page_id']} changed")
                target = relative_path(str(path), self.out)
                what = f"Tile {image['tile_id']} sent" if tiled else "The image sent"
                links.append(
                    f'<a href="{_e(_href(target))}">{what} to {_e(self._name(attempt))}</a>'
                )
        picture = self.pictures.get(page["page_id"])
        image = (
            f'<img src="{_e(picture)}" alt="Page {page["page_number"]} of the source" '
            'loading="lazy" decoding="async">'
            if picture
            else '<p class="none">No page image in this view.</p>'
        )
        return (
            f'<figure class="image" tabindex="0">{image}<figcaption>'
            + "<br>".join(links)
            + "</figcaption></figure>"
        )

    def _open_spans(self, page: dict) -> tuple[str, dict[str, str]]:
        """The open spans as a list, and each reading they could be marked in."""
        contested = page.get("contested")
        if not contested:
            return "", {}
        record = json.loads(_artifact_bytes(self.root, contested["artifact"], contested["sha256"]))
        still_open, _ = settle(record, contested["sha256"], page.get("adjudications", []))
        if not still_open:
            return "<p>No contested span is open on this page.</p>", {}
        base = record["base"]["attempt"]
        attempt = next(a for a in page["attempts"] if a["attempt_id"] == base)
        text = self._reading(attempt) or ""
        marked = {}
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == record["base"]["text_sha256"]:
            shown = spans_html(text, still_open, _row_id(page))
            if shown is not None:
                marked[base] = shown

        def kind(span: dict) -> str:
            if base not in marked:
                return _e(span["kind"])
            return f'<a href="#{_e(_row_id(page))}-{_e(span["span_id"])}">{_e(span["kind"])}</a>'

        rows = "".join(
            f"<tr><td>{kind(span)}</td>"
            f'<td><span dir="auto"{self.lang}>{text_html(span["base"]) or "<em>nothing</em>"}'
            f'</span></td><td><span dir="auto"{self.lang}>'
            f"{text_html(span['witness']) or '<em>nothing</em>'}</span></td></tr>"
            for span in sorted(still_open, key=lambda span: span["start"])
        )
        where = (
            "marked in the text"
            if base in marked
            else "not marked in the text, which differs from the reading they were found in"
        )
        listing = (
            f"<h3>Open contested spans ({len(still_open)})</h3>"
            f"<p>Places where {_e(self._name(attempt))} and "
            f"{_e(self._name(self._attempt(page, record['witness']['attempt'])))} differ and "
            f"nothing has settled; {where}. "
            f"{_doc('processing-spec.md', 'contest-a-models-reading', 'How contest works')}.</p>"
            '<div class="table"><table><thead><tr><th scope="col">Kind</th>'
            '<th scope="col">Model read</th><th scope="col">Literal engine read</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
        return listing, marked

    def _starts(self, page: dict) -> dict[str, str]:
        """The readings a correction can start from, the selected text first under "".

        Then each completed attempt's own text by its ID, unless it is the selected
        text; a reading that is not UTF-8, or that is empty, is left out.
        """
        selected = page["selected_output"]
        starts = {} if selected is None else {"": selected["text"]}
        for attempt in page["attempts"]:
            if not self._usable(attempt):
                continue
            if (
                selected is not None
                and attempt["attempt_id"] == selected["attempt_id"]
                and not (selected.get("edition") or selected.get("correction"))
            ):
                continue
            content = _artifact_bytes(self.root, attempt["raw_artifact"], attempt["raw_sha256"])
            try:
                starts[attempt["attempt_id"]] = content.decode("utf-8")
            except UnicodeError:
                continue
        return starts

    def _attempt(self, page: dict, attempt_id: str) -> dict:
        return _attempt(page, attempt_id)

    def _usable(self, attempt: dict) -> bool:
        """A reading a person can choose or start a correction from: finished, kept,
        and with some text."""
        return (
            attempt["outcome"] == "completed"
            and attempt["raw_artifact"] is not None
            and not self._empty(attempt)
        )

    def _empty(self, attempt: dict) -> bool:
        """No text at all. A reading of symbols alone, such as `* * *`, is warned empty
        of letters and digits, but is shown, and a person can choose it."""
        return empty_reading(attempt) and not (self._reading(attempt) or "").strip()

    def _reading(self, attempt: dict) -> str | None:
        if attempt["raw_artifact"] is None:
            return None
        content = _artifact_bytes(self.root, attempt["raw_artifact"], attempt["raw_sha256"])
        return content.decode("utf-8", errors="replace")

    def _others(self, page: dict, marked: dict[str, str]) -> str:
        selected = page["selected_output"]
        blocks = []
        for attempt in page["attempts"]:
            if selected is not None and attempt["attempt_id"] == selected["attempt_id"]:
                if not (selected.get("edition") or selected.get("correction")):
                    continue
            if _imageless(attempt):
                continue
            text = self._reading(attempt)
            summary = sentence(self._name(attempt))
            if attempt["outcome"] != "completed":
                summary += f" ({_OUTCOMES.get(attempt['outcome'], attempt['outcome'])})"
            if text is None:
                blocks.append(f"<p>{_e(summary)}: no text was kept.</p>")
                continue
            if self._empty(attempt):
                blocks.append(f"<p>{_e(summary)}: empty, with no letters or digits.</p>")
                continue
            parts = [f"<details><summary>{_e(summary)}</summary>"]
            differs = selected is not None and text != selected["text"]
            diff = diff_html(selected["text"], text) if selected is not None and differs else None
            if differs and diff is None:
                parts.append(
                    "<h4>Compared with the selected text</h4>"
                    "<p>Not compared word by word: the two readings are too long for this "
                    "view. Both are shown in full.</p>"
                )
            elif differs:
                parts.append(
                    "<h4>Compared with the selected text</h4>"
                    '<p class="legend">The selected text with <del>its own words struck</del> '
                    "and <ins>this reading's words</ins> after them.</p>"
                    f'<pre class="text diff" dir="auto"{self.lang}>\n{diff}</pre>'
                )
            elif selected is not None:
                parts.append("<p>Identical to the selected text.</p>")
            parts.append(
                f'<h4>This reading</h4><pre class="text" dir="auto"{self.lang}>\n'
                f"{marked.get(attempt['attempt_id']) or text_html(text)}</pre></details>"
            )
            blocks.append("".join(parts))
        if not blocks:
            return ""
        return "<h3>Other readings</h3>" + "".join(blocks)

    def _form(self, page: dict) -> str:
        row_id = _row_id(page)
        starts = self._starts(page)
        offered = {
            "accept": page["selected_output"] is not None,
            "correct": bool(starts),
        }
        others = [
            attempt
            for attempt in page["attempts"]
            if self._usable(attempt) and attempt["attempt_id"] != page["selected_attempt"]
        ]
        groups = []
        for title, choices in DECISION_GROUPS:
            options = []
            for token, label, _records in choices:
                if token == "use:":
                    options += [
                        (f"use:{a['attempt_id']}", label.replace("READING", self._name(a)))
                        for a in others
                    ]
                elif offered.get(token, True):
                    options.append((token, label))
            if options:
                inner = "".join(
                    f'<option value="{_e(v)}">{_e(text)}</option>' for v, text in options
                )
                groups.append(f'<optgroup label="{_e(title)}">{inner}</optgroup>')
        options_html = '<option value="">No decision yet</option>' + "".join(groups)
        return (
            f'<form class="decision" data-row="{row_id}" aria-labelledby="{row_id}-decide">'
            f'<h3 id="{row_id}-decide">Your decision</h3>'
            f'<label for="{row_id}-decision">Decision</label>'
            f'<select id="{row_id}-decision" name="decision" disabled>{options_html}</select>'
            f'<label for="{row_id}-note">Note (recorded as the reason for the decision)</label>'
            f'<textarea id="{row_id}-note" name="note" rows="2" disabled></textarea>'
            f"{self._correct(page, starts) if starts else ''}"
            f'<p class="problem" role="alert"></p></form>'
        )

    def _correct(self, page: dict, starts: dict[str, str]) -> str:
        row_id = _row_id(page)
        names = [("", "the selected text")] if "" in starts else []
        names += [(key, sentence(self._name(self._attempt(page, key)))) for key in starts if key]
        options = "".join(f'<option value="{_e(v)}">{_e(label)}</option>' for v, label in names)
        return (
            f'<h4 id="{row_id}-correct">Correct the text</h4>'
            "<p>Edit the text to match the page. What you type is recorded exactly, "
            "spaces and all; only line endings are made uniform. The reading you start "
            "from stays in the job as it is.</p>"
            f'<label for="{row_id}-from">Start from</label>'
            f'<select id="{row_id}-from" name="from" disabled>{options}</select>'
            f'<label for="{row_id}-text">Corrected text</label>'
            f'<textarea id="{row_id}-text" name="text" rows="14" spellcheck="false" '
            f'dir="auto"{self.lang} disabled></textarea>'
            '<p class="changes" role="status"></p>'
            f'<pre class="text marked" dir="auto"{self.lang} '
            f'aria-label="Your changes to page {page["page_number"]}"></pre>'
        )


_STATUS = {
    "completed": "The job has finished.",
    "processing": "The job has not finished: some pages may still change.",
    "paused_budget": "The job is paused at a limit: some pages may still change.",
    "halted": "The job halted.",
}
_OUTCOMES = {"failed": "failed", "unknown": "no result was saved"}


def _agreement(page: dict, report: dict) -> str:
    """The comparison the job weighed; agreement is said not to prove the text."""
    said = _short_comparison(page, report)
    if said.endswith("of words agree"):
        said += f" ({_lower(AGREEMENT_CAVEAT).removesuffix('.')})"
    return said


def _imageless(attempt: dict) -> bool:
    """A page image's own text, which is always empty: the record lists it, the page does not."""
    return "no_text_layer" in _codes(attempt) and empty_reading(attempt)


def _why(page: dict, report: dict, *, every: bool = False) -> str:
    """What holds the page, in the report's page table's words; ``every`` gives all its
    causes, as the page's own section does."""
    return held_words(report, page, every=every, more="on the page")


def _state(page: dict) -> str:
    state = page["disposition"]
    return HOLDS.get(state) or REVIEWED.get(state) or state


def _review(page: dict) -> str:
    review = page.get("review")
    if review is None:
        return "not reviewed"
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    corrected = " with a correction" if "correction" in decision else ""
    words = REVIEWED.get(decision["disposition"], decision["disposition"])
    return (
        f"{words}{corrected}, by {decision['reviewer']} at "
        f"{decision['reviewed_at']}: {decision['reason']}"
    )


def _choices_legend() -> str:
    """What each decision records, grouped as the page's decision list groups them."""
    items = []
    for title, choices in DECISION_GROUPS:
        lines = "".join(
            f"<li>{_e(label.replace('READING', 'another reading'))} "
            f"(<code>{_e(token + 'READING' if token == 'use:' else token)}</code>): "
            f"records {_e(records)}.</li>"
            for token, label, records in choices
        )
        items.append(f"<li>{_e(title)}<ul>{lines}</ul></li>")
    return (
        "<details><summary>What each decision records</summary>"
        f'<ul class="legend">{"".join(items)}</ul></details>'
    )


def _href(path: str, fragment: str = "") -> str:
    return quote(path, safe="/") + fragment


def _hash(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _document(title: str, body: str, embedded: str) -> str:
    policy = (
        "default-src 'none'; img-src 'self' file:; "
        f"style-src '{_hash(_CSS)}'; script-src '{_hash(_SCRIPT)}'; "
        "base-uri 'none'; form-action 'none'"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(policy)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>Review: {_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<main>\n{body}\n</main>\n"
        f"{_DATA_OPEN}{embedded}</script>\n"
        f"<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )


_CSS = """
:root { --ink: #1f1d1a; --paper: #fbfaf7; --muted: #5f5a52; --line: #d9d4ca;
  --mark: #f3e1a6; --del: #f2c9c4; --ins: #cfe6cf; --accent: #2f5d8a; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #e6e2da; --paper: #191816; --muted: #a59f94; --line: #3a3732;
    --mark: #5a4a17; --del: #5b2a25; --ins: #24452a; --accent: #8db6e0; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.5 Georgia, "Iowan Old Style", "Times New Roman", serif; }
main { max-width: 1400px; margin: 0 auto; padding: 1rem; }
a { color: var(--accent); }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: -999px; }
.skip:focus { left: 1rem; top: 1rem; background: var(--paper); padding: .5rem; }
h1, h2, h3, h4, h5 { font-weight: 600; line-height: 1.25; }
code, pre, select, textarea, button { font-family: ui-monospace, Menlo, Consolas, monospace; }
code { font-size: .9em; overflow-wrap: anywhere; }
pre.text, pre.command { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 1rem;
  padding: .75rem; border: 1px solid var(--line); font-size: .95rem; line-height: 1.45; }
.decisions { border: 1px solid var(--line); padding: 0 1rem; margin: 1rem 0 2rem; }
.table { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .95rem; }
th, td { text-align: left; vertical-align: top; padding: .3rem .5rem;
  border-bottom: 1px solid var(--line); }
.page { border-top: 2px solid var(--line); margin-top: 2rem;
  content-visibility: auto; contain-intrinsic-size: auto 1200px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .2rem 1rem; }
dt { color: var(--muted); }
dd { margin: 0; }
.sheet { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.5rem; }
@media (max-width: 900px) { .sheet { grid-template-columns: minmax(0, 1fr); } }
.image { margin: 0; position: sticky; top: 0; align-self: start; max-height: 100vh;
  overflow: auto; }
.image img { display: block; width: 100%; height: auto; border: 1px solid var(--line); }
figcaption, .legend, .nav, .none { color: var(--muted); font-size: .9rem; }
mark.open { background: var(--mark); color: inherit; }
mark.open:empty { padding: 0 .15em; border-left: 2px solid var(--ink); }
mark.open::after { content: attr(data-kind); font-size: .65em; vertical-align: super;
  color: var(--muted); }
del { background: var(--del); color: inherit; }
ins { background: var(--ins); color: inherit; text-decoration: underline; margin-left: .2em; }
.ctl { color: var(--muted); border: 1px dotted var(--muted); }
details { border: 1px solid var(--line); padding: .3rem .6rem; margin-bottom: .5rem; }
summary { cursor: pointer; }
.decision { border: 1px solid var(--line); padding: 0 .8rem .8rem; margin-top: 1rem; }
.decision label { display: block; margin-top: .5rem; }
select, textarea { width: 100%; font-size: .95rem; color: inherit; background: var(--paper);
  border: 1px solid var(--muted); padding: .3rem; }
button { font-size: .95rem; padding: .3rem .7rem; }
.decision h4 { margin-bottom: .3rem; }
textarea[name="text"] { tab-size: 4; white-space: pre-wrap; }
.changes { color: var(--muted); font-size: .9rem; }
pre.marked:empty { display: none; }
.problem { color: #b3261e; }
@media (prefers-color-scheme: dark) { .problem { color: #f2b8b5; } }
"""

# buildCsv must stay a pure function: tests run it with node against review --apply.
CSV_JS = """
function buildCsv(fields, rows, decisions) {
  var quote = function (value) { return '"' + String(value).replace(/"/g, '""') + '"'; };
  var lines = [fields.map(quote).join(",")];
  rows.forEach(function (row) {
    var chosen = decisions[row.row_id] || {};
    lines.push(fields.map(function (field) {
      if (field === "decision" || field === "note") { return quote(chosen[field] || ""); }
      return quote(row[field]);
    }).join(","));
  });
  return "\\ufeff" + lines.join("\\n") + "\\n";
}
"""

# Pure functions too: tests run them with node. buildDecisions writes the decisions
# file review --apply reads; wordDiff marks a correction for the reviewer, while the
# counts recorded in the receipt are review --apply's own.
DECISIONS_JS = r"""
function normalizeLines(text) { return text.replace(/\r\n?/g, "\n"); }
function buildDecisions(binding, rows, decisions) {
  var list = [];
  rows.forEach(function (row) {
    var chosen = decisions[row.row_id];
    if (!chosen || !chosen.decision) { return; }
    var item = {row_id: row.row_id, page_number: row.page_number,
                decision: chosen.decision, note: chosen.note || ""};
    if (chosen.decision === "correct") {
      item.text = chosen.text;
      if (chosen.from) { item.from = chosen.from; }
    }
    list.push(item);
  });
  var envelope = {schema_version: "0.1", bindings: JSON.parse(binding).bindings, rows: list};
  return JSON.stringify(envelope, null, 2) + "\n";
}
function wordDiff(before, after) {
  var old = before.split(/\s+/).filter(function (word) { return word !== ""; });
  var pieces = after.match(/\s+|\S+/g) || [];
  var words = [], space = {}, lead = "";
  pieces.forEach(function (piece) {
    if (/^\s/.test(piece)) {
      if (words.length) { space[words.length - 1] = piece; } else { lead = piece; }
    } else { words.push(piece); }
  });
  var n = old.length, m = words.length;
  if ((n + 1) * (m + 1) > 4000000) { return null; }
  var width = m + 1, table = new Uint32Array((n + 1) * width);
  for (var i = n - 1; i >= 0; i--) {
    for (var j = m - 1; j >= 0; j--) {
      table[i * width + j] = old[i] === words[j] ? table[(i + 1) * width + j + 1] + 1
        : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  var parts = lead ? [{kind: "same", text: lead}] : [];
  var counts = {changed: 0, added: 0, removed: 0}, gone = 0, come = 0;
  function settle() {
    var paired = Math.min(gone, come);
    counts.changed += paired; counts.removed += gone - paired; counts.added += come - paired;
    gone = 0; come = 0;
  }
  i = 0; j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && old[i] === words[j]) {
      settle();
      parts.push({kind: "same", text: words[j] + (space[j] || "")});
      i++; j++;
    } else if (j >= m || (i < n && table[(i + 1) * width + j] >= table[i * width + j + 1])) {
      parts.push({kind: "del", text: old[i]});
      parts.push({kind: "same", text: " "});
      gone++; i++;
    } else {
      parts.push({kind: "ins", text: words[j]});
      parts.push({kind: "same", text: space[j] || ""});
      come++; j++;
    }
  }
  settle();
  return {counts: counts, parts: parts};
}
"""

_SCRIPT = (
    CSV_JS
    + DECISIONS_JS
    + """
(function () {
  var data = JSON.parse(document.getElementById("review-data").textContent);
  var UNEDITABLE = """
    + _UNEDITABLE_JS
    + """;
  var INVISIBLE = """
    + _INVISIBLE_JS
    + """;
  function hiddenNotice(text) {
    var found = text.match(INVISIBLE) || [];
    var codes = [];
    found.forEach(function (character) {
      var code = "U+" + character.codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
      if (codes.indexOf(code) < 0) { codes.push(code); }
    });
    if (!found.length) { return ""; }
    return "This reading holds " + found.length + " character" + (found.length === 1 ? "" : "s") +
      " a text box does not show (" + codes.join(", ") + "). They stay exactly where they " +
      "are unless you delete the text around them. ";
  }
  var FORMULA = /^[=+@\\t\\r]/;
  var status = document.getElementById("storage");
  var decisions = {};
  var kept = true;
  try {
    decisions = JSON.parse(window.localStorage.getItem(data.key) || "{}") || {};
  } catch (error) {
    kept = false;
  }
  function store() {
    try {
      window.localStorage.setItem(data.key, JSON.stringify(decisions));
      kept = true;
    } catch (error) {
      kept = false;
    }
    show();
  }
  function show() {
    var count = Object.keys(decisions).length;
    status.textContent = count + " page decision" + (count === 1 ? "" : "s") + " chosen. " +
      (kept ? "This browser keeps them for this job until you choose Forget."
            : "This browser is not keeping them: download them before you close the page.");
    document.querySelectorAll("td.chosen").forEach(function (cell) {
      var row = cell.getAttribute("data-row");
      var chosen = decisions[row];
      var shown = chosen ? chosen.decision : "";
      if (chosen && selects[row]) {
        Array.prototype.forEach.call(selects[row].options, function (option) {
          if (option.value === chosen.decision && option.text) { shown = option.text; }
        });
      }
      cell.textContent = shown;
    });
  }
  function save(name, text, type) {
    var url = URL.createObjectURL(new Blob([text], {type: type}));
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  var resets = [];
  var selects = {};
  var own = function (object, key) { return Object.prototype.hasOwnProperty.call(object, key); };
  document.querySelectorAll("form.decision").forEach(function (form) {
    var row = form.getAttribute("data-row");
    var select = form.elements.decision;
    selects[row] = select;
    var note = form.elements.note;
    var area = form.elements.text || null;
    var from = form.elements.from || null;
    var changes = form.querySelector(".changes");
    var marked = form.querySelector(".marked");
    var problem = form.querySelector(".problem");
    var starts = data.texts[row] || {};
    // The selected text, or on a page without one, the first reading there is.
    var first = own(starts, "") ? "" : Object.keys(starts)[0];
    var origin = first;
    var chosen = decisions[row];
    var valid = Boolean(chosen) && Array.prototype.some.call(select.options, function (option) {
      return option.value === chosen.decision;
    });
    if (valid && chosen.decision === "correct") {
      valid = area !== null && own(starts, chosen.from || "") && typeof chosen.text === "string";
    }
    if (valid) {
      select.value = chosen.decision;
      note.value = chosen.note || "";
    } else if (chosen) {
      delete decisions[row];
    }
    function start() { return normalizeLines(starts[origin]); }
    function edited() {
      return area !== null && !area.disabled && normalizeLines(area.value) !== start();
    }
    var notice = "";
    function load() {
      area.value = start();
      var unsafe = UNEDITABLE.test(start());
      area.disabled = unsafe || area.value !== start();
      notice = hiddenNotice(start());
      if (area.disabled) {
        changes.textContent = (unsafe
          ? "This reading holds control or text-direction characters (marked in it above) " +
            "that a text box would drop or that would reorder the text as you edit it. "
          : "A browser text box cannot hold this reading exactly. ") +
          "Correct it in a text file instead, with correct:FILE in a review sheet.";
      }
    }
    function mark() {
      while (marked.firstChild) { marked.removeChild(marked.firstChild); }
      if (area.disabled) { return; }
      var now = normalizeLines(area.value);
      if (now === start()) {
        changes.textContent = notice + "No change from the reading you started from.";
        return;
      }
      var diff = wordDiff(start(), now);
      if (diff === null) {
        changes.textContent = notice + "Changed. The page is too long to mark the changes here; " +
          "review --apply --dry-run counts them.";
        return;
      }
      var counts = diff.counts;
      changes.textContent = notice + "Changed here: " + counts.changed + " word" +
        (counts.changed === 1 ? "" : "s") + " changed, " + counts.added + " added, " +
        counts.removed + " removed. Struck words are the reading's, underlined words yours. " +
        "review --apply counts the change again and records its own count.";
      diff.parts.forEach(function (part) {
        if (part.kind === "same") {
          marked.appendChild(document.createTextNode(part.text));
          return;
        }
        var element = document.createElement(part.kind);
        element.textContent = part.text;
        marked.appendChild(element);
      });
    }
    function update() {
      if (FORMULA.test(note.value)) {
        problem.textContent = "A note cannot start with =, +, @ or a tab: review --apply " +
          "refuses it as a spreadsheet formula. This change is not kept.";
        return;
      }
      problem.textContent = "";
      if (select.value === "correct" && !edited()) {
        problem.textContent = "Edit the text below to record a correction. " +
          "If the text is right as it is, choose accept.";
        delete decisions[row];
      } else if (select.value === "correct") {
        decisions[row] = {decision: "correct", note: note.value, from: origin, text: area.value};
      } else if (select.value) {
        decisions[row] = {decision: select.value, note: note.value};
      } else {
        delete decisions[row];
      }
      store();
    }
    if (area !== null) {
      origin = valid && chosen.decision === "correct" ? chosen.from || "" : first;
      from.value = origin;
      load();
      if (valid && chosen.decision === "correct" && !area.disabled) { area.value = chosen.text; }
      mark();
      area.addEventListener("input", function () {
        if (edited()) {
          select.value = "correct";
        } else if (select.value === "correct") {
          select.value = "";
        }
        mark();
        update();
      });
      from.addEventListener("change", function () {
        if (edited() && !window.confirm("Replace your corrected text with this reading?")) {
          from.value = origin;
          return;
        }
        origin = from.value;
        load();
        mark();
        if (select.value === "correct") { select.value = ""; }
        update();
      });
      from.disabled = false;
    }
    form.addEventListener("submit", function (event) { event.preventDefault(); });
    select.addEventListener("change", function () {
      var other = select.value.slice(4);
      if (area !== null && select.value.indexOf("use:") === 0 && !edited() && own(starts, other)) {
        origin = other;
        from.value = other;
        load();
        mark();
      }
      update();
    });
    note.addEventListener("input", update);
    select.disabled = false;
    note.disabled = false;
    resets.push(function () {
      select.value = "";
      note.value = "";
      problem.textContent = "";
      if (area !== null) {
        origin = first;
        from.value = first;
        load();
        mark();
      }
    });
  });
  var downloadProblem = document.getElementById("download-problem");
  document.getElementById("download-decisions").addEventListener("click", function () {
    downloadProblem.textContent = "";
    save("review.json", buildDecisions(data.binding, data.rows, decisions), "application/json");
  });
  document.getElementById("download-sheet").addEventListener("click", function () {
    if (Object.keys(decisions).some(function (row) { return decisions[row].decision === "correct"; })) {
      downloadProblem.textContent = "A review sheet cannot carry corrected text. Download " +
        "review.json instead: it holds every decision, corrections included.";
      return;
    }
    downloadProblem.textContent = "";
    save("review.csv", buildCsv(data.fields, data.rows, decisions), "text/csv;charset=utf-8");
  });
  document.getElementById("download-binding").addEventListener("click", function () {
    save("review.csv.binding.json", data.binding, "application/json");
  });
  document.getElementById("forget").addEventListener("click", function () {
    if (!window.confirm("Forget every decision and correction made in this browser for this job?")) {
      return;
    }
    decisions = {};
    resets.forEach(function (reset) { reset(); });
    store();
  });
  ["download-decisions", "download-sheet", "download-binding", "forget"].forEach(function (id) {
    document.getElementById(id).disabled = false;
  });
  show();
})();
"""
)
