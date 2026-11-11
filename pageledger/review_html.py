"""A static review view: each page's image beside its text, in one HTML file.

The view is a rendering of a verified job, like report.md. It writes nothing into
the job. Decisions made in it leave the browser as the same CSV and binding that
`review --sheet` writes, and count only once `review --apply` applies them.
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
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

from ._version import __version__
from .adapters import _run_ocr_command
from .adjudication import settle
from .checkpoint import atomic_bytes, file_digest, writer_lock
from .document_report import (
    _DISPOSITION_LABELS,
    _STAGE_LABELS,
    _agreement,
    _artifact_bytes,
    _image_name,
    _needs_person,
    relative_path,
    verify_document_report,
)
from .image_source import KIND, ImageSource, _read_page, carrier, page_target, snapshot_growth
from .processing import _check_source, _load, _refresh, verify_job
from .processing_policy import warning_holds
from .review_sheet import DECISIONS, FIELDS, _row_id, binding_path, write_review_sheet

IMAGE_CHOICES = ("all", "held", "none")
SHEET = "review.csv"
DOCS = f"https://github.com/peterbussch/pageledger/blob/v{__version__}/docs/"
_RENDER_SIDE = 1600
_RENDER_TIMEOUT_SECONDS = 120
# Characters HTML cannot show as themselves; each is drawn as its Unicode control picture.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
_PIECES = re.compile(r"\s+|\S+")
_NO_POPPLER = (
    "Page images need pdftoppm on PATH; install Poppler, "
    "or write the view without images with --images none"
)
_DECISION_LABELS = {
    "accept": "accept: the selected text is right",
    "blank": "blank: the page has no text",
    "illustration": "illustration",
    "handwriting": "handwriting",
    "unreadable": "unreadable: the source cannot be read",
    "source_defect": "source_defect: the source page is damaged or incomplete",
}


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


def _write_view(root: Path, out: Path, job: dict, source: Path, written: dict, images: str) -> dict:
    report = verify_document_report(root)
    sheet = Path(written["out"])
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    binding = binding_path(sheet).read_text(encoding="utf-8")
    rendered = 0
    pictures: dict[str, str] = {}
    recorded = job["source"]["images"] if job["source"].get("kind") == KIND else None
    for page in report["pages"]:
        if images == "none" or (images == "held" and not _needs_person(page)):
            continue
        name = f"images/{_row_id(page)}.jpg"
        (out / "images").mkdir(exist_ok=True)
        drawn = (
            render_own_image(source, recorded[page["page_number"] - 1], out / name)
            if recorded is not None
            else render_page_image(source, page["page_number"], out / name)
        )
        if not drawn:
            (out / "images").rmdir()
            break
        pictures[page["page_id"]] = name
        rendered += 1
    view = _View(root, out, report, source, pictures)
    document = view.render(rows, binding, language=_language(job))
    atomic_bytes(out / "index.html", document.encode("utf-8"))
    return {
        "status": "written",
        "out": str(out / "index.html"),
        "sheet": str(sheet),
        "binding": written["binding"],
        "pages": len(report["pages"]),
        "images": rendered,
    }


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


def text_html(text: str) -> str:
    """Escape transcript text; control characters are drawn as visible symbols."""
    parts, last = [], 0
    for match in _CONTROL.finditer(text):
        parts.append(_e(text[last : match.start()]))
        code = ord(match.group())
        symbol = chr(0x2421 if code == 0x7F else 0x2400 + code)
        parts.append(f'<span class="ctl" title="U+{code:04X}">{symbol}</span>')
        last = match.end()
    parts.append(_e(text[last:]))
    return "".join(parts)


def diff_html(selected: str, other: str) -> str:
    """The selected text, every character in place, with the other reading's words marked.

    Words only the selected reading has are in <del>; the other reading's words
    are inserted in <ins> after the words they replace.
    """
    pieces = _PIECES.findall(selected)
    words = [index for index, piece in enumerate(pieces) if not piece.isspace()]
    theirs = [match for match in _PIECES.finditer(other) if not match.group().isspace()]
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
        self.holds = warning_holds(report)

    def render(self, rows: list[dict], binding: str, *, language: str | None) -> str:
        self.lang = f' lang="{_e(language)}"' if language else ""
        report = self.report
        ordered = sorted(
            report["pages"], key=lambda page: (not _needs_person(page), page["page_number"])
        )
        key = f"pageledger-review:{report['job_id']}:{hashlib.sha256(binding.encode()).hexdigest()[:16]}"
        data = {"key": key, "fields": list(FIELDS), "rows": rows, "binding": binding}
        embedded = (
            json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029")
        )
        title = Path(report["source"]["path"]).name
        counts = report["counts"]
        needing = sum(_needs_person(page) for page in report["pages"])
        command = (
            f"pageledger review {shlex.quote(str(self.root))} "
            f'--apply {shlex.quote(str(self.out / SHEET))} --reviewer "YOUR NAME"'
        )
        body = [
            '<a class="skip" href="#pages">Skip to the page list</a>',
            "<header>",
            f"<h1>{_e(title)}</h1>",
            f"<p>Job <code>{_e(report['job_id'])}</code>, status {_e(report['status'])}. "
            f"Source SHA-256 <code>{_e(report['source']['sha256'])}</code>.</p>",
            f"<p>{counts['selected_pages']} pages in this job; {counts['selected_outputs']} have "
            f"selected text; {needing} need a person; {counts['unresolved_pages']} are "
            "not yet reviewed as text or blank.</p>",
            "</header>",
            '<section class="decisions" aria-labelledby="decisions-title">',
            '<h2 id="decisions-title">Recording decisions</h2>',
            "<p><strong>Nothing you choose here is recorded in the job.</strong> This page is "
            "a reading copy of the job, written by <code>pageledger review</code>. "
            "Choose a decision under any page, then download the review sheet, save it in "
            f"this folder in place of the blank <code>{SHEET}</code> (its binding, "
            f"<code>{SHEET}.binding.json</code>, is already here), and apply it:</p>",
            f'<pre class="command">{_e(command)}</pre>',
            "<p>Add <code>--dry-run</code> first to check every decision without recording "
            "any. <code>review --apply</code> refuses the sheet if the job has changed since "
            "this view was written.</p>",
            "<noscript><p>Choosing decisions here needs JavaScript. Without it, fill in "
            f"<code>{SHEET}</code> in a spreadsheet instead.</p></noscript>",
            '<p class="controls">'
            '<button type="button" id="download-sheet" disabled>Download review.csv</button> '
            '<button type="button" id="download-binding" disabled>'
            "Download its binding</button> "
            '<button type="button" id="forget" disabled>Forget decisions in this browser</button>'
            "</p>",
            '<p id="storage" role="status"></p>',
            "</section>",
            '<section id="pages" aria-labelledby="pages-title">',
            '<h2 id="pages-title">Pages</h2>',
            "<p>Pages that need a person come first. Engine agreement is evidence, not proof: "
            "engines can share a mistake.</p>",
            '<div class="table"><table>',
            '<thead><tr><th scope="col">Page</th><th scope="col">State</th>'
            '<th scope="col">Held for</th><th scope="col">Engine agreement</th>'
            '<th scope="col">Review</th><th scope="col">Your decision</th></tr></thead><tbody>',
            *(self._row(page) for page in ordered),
            "</tbody></table></div></section>",
            *(self._page(page) for page in report["pages"]),
        ]
        return _document(title, "\n".join(body), embedded)

    def _row(self, page: dict) -> str:
        row_id = _row_id(page)
        reasons = ", ".join(_label(r) for r in page["review_reasons"]) or "nothing"
        return (
            f'<tr><th scope="row"><a href="#{row_id}">{page["page_number"]}'
            f"{_e(_image_name(self.report, page))}</a></th>"
            f"<td>{_e(_label(page['disposition']))}</td><td>{_e(reasons)}</td>"
            f"<td>{_e(_agreement(page, self.holds))}</td><td>{_e(_review(page))}</td>"
            f'<td class="chosen" data-row="{row_id}"></td></tr>'
        )

    def _page(self, page: dict) -> str:
        row_id, number = _row_id(page), page["page_number"]
        selected = page["selected_output"]
        path, fragment = page_target(self.report["source"], number)
        source_link = relative_path(str(self.source), self.out) + path + fragment
        facts = [
            ("State", _e(_label(page["disposition"]))),
            ("Held for", self._holds(page)),
            ("Engine agreement", _e(_agreement(page, self.holds))),
            ("Review", _e(_review(page))),
        ]
        warnings = [
            f"{_e(attempt['attempt_id'])}: "
            + ", ".join(f"<code>{_e(code)}</code>" for code in _codes(attempt))
            for attempt in page["attempts"]
            if _codes(attempt)
        ]
        if warnings:
            facts.append(
                (
                    "Warnings",
                    "; ".join(warnings)
                    + f" ({_doc('warnings.md', 'quality-warnings', 'what they mean')})",
                )
            )
        if selected is not None:
            what = (
                f"edition of attempt {selected['attempt_id']}, with settled spans applied"
                if selected.get("edition")
                else f"attempt {selected['attempt_id']}"
            )
            facts.append(("Selected text", f"{_e(what)} (<code>{_e(selected['path'])}</code>)"))
        open_spans, marked = self._open_spans(page)
        figure = self._figure(page, source_link)
        if selected is None:
            reading = (
                f"<p>No text is selected for this page ({_e(_label(page['disposition']))}).</p>"
            )
        else:
            shown = marked.get(selected["attempt_id"]) if not selected.get("edition") else None
            reading = (
                f'<h3>Selected text</h3><pre class="text" dir="auto"{self.lang}>\n'
                f"{shown or text_html(selected['text'])}</pre>"
            )
        others = self._others(page, marked)
        parts = [
            f'<section class="page" id="{row_id}" aria-labelledby="{row_id}-title">',
            f'<h2 id="{row_id}-title">Page {number}{_e(_image_name(self.report, page))}</h2>',
            '<p class="nav"><a href="#pages">Back to the page list</a></p>',
            "<dl>",
            *(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts),
            "</dl>",
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

    def _holds(self, page: dict) -> str:
        if not page["review_reasons"]:
            return "nothing"
        items = [_e(_label(reason)) for reason in page["review_reasons"]]
        link = _doc("warnings.md", "document-job-dispositions", "what they mean")
        return "; ".join(items) + f" ({link})"

    def _figure(self, page: dict, source_link: str) -> str:
        label = f"Page {page['page_number']}{_image_name(self.report, page)} in the source"
        links = [f'<a href="{_e(_href(source_link))}">{_e(label)}</a>']
        for attempt in page["attempts"]:
            evidence = attempt.get("input_evidence")
            if not evidence:
                continue
            path = self.root / attempt["run_path"] / evidence["artifact"]
            if file_digest(path) != evidence["sha256"]:
                raise ValueError(f"Image evidence for {page['page_id']} changed")
            target = relative_path(str(path), self.out)
            links.append(
                f'<a href="{_e(_href(target))}">The image sent to the model in '
                f"{_e(attempt['attempt_id'])}</a>"
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
            f"<p>Places where the model's reading ({_e(base)}) and the literal engine's "
            f"({_e(record['witness']['attempt'])}) differ and nothing has settled; {where}. "
            f"{_doc('processing-spec.md', 'contest-a-models-reading', 'How contest works')}.</p>"
            '<div class="table"><table><thead><tr><th scope="col">Kind</th>'
            '<th scope="col">Model read</th><th scope="col">Literal engine read</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
        return listing, marked

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
                if not selected.get("edition"):
                    continue
            text = self._reading(attempt)
            stage = _STAGE_LABELS.get(attempt["stage"], attempt["stage"])
            summary = f"{attempt['attempt_id']}: {stage}, {attempt['outcome']}"
            if text is None:
                blocks.append(f"<p>{_e(summary)}; no text was kept.</p>")
                continue
            parts = [f"<details><summary>{_e(summary)}</summary>"]
            if selected is not None and text != selected["text"]:
                parts.append(
                    "<h4>Compared with the selected text</h4>"
                    '<p class="legend">The selected text with <del>its own words struck</del> '
                    "and <ins>this reading's words</ins> after them.</p>"
                    f'<pre class="text diff" dir="auto"{self.lang}>\n'
                    f"{diff_html(selected['text'], text)}</pre>"
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
        choices = [("", "no decision: the page stays unreviewed")]
        if page["selected_output"] is not None:
            choices.append(("accept", _DECISION_LABELS["accept"]))
        for attempt in page["attempts"]:
            if (
                attempt["outcome"] == "completed"
                and attempt["raw_artifact"]
                and attempt["attempt_id"] != page["selected_attempt"]
            ):
                token = f"use:{attempt['attempt_id']}"
                choices.append((token, f"{token}: use this reading instead"))
        choices += [(token, _DECISION_LABELS[token]) for token in DECISIONS if token != "accept"]
        options = "".join(f'<option value="{_e(v)}">{_e(label)}</option>' for v, label in choices)
        return (
            f'<form class="decision" data-row="{row_id}" aria-labelledby="{row_id}-decide">'
            f'<h3 id="{row_id}-decide">Your decision</h3>'
            f'<label for="{row_id}-decision">Decision</label>'
            f'<select id="{row_id}-decision" name="decision" disabled>{options}</select>'
            f'<label for="{row_id}-note">Note (becomes the reason in the receipt)</label>'
            f'<textarea id="{row_id}-note" name="note" rows="2" disabled></textarea>'
            f'<p class="problem" role="alert"></p></form>'
        )


def _label(code: str) -> str:
    return f"{_DISPOSITION_LABELS.get(code, code)} ({code})"


def _review(page: dict) -> str:
    review = page.get("review")
    if review is None:
        return "not reviewed"
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    return (
        f"{_label(decision['disposition'])}, by {decision['reviewer']} at "
        f"{decision['reviewed_at']}: {decision['reason']}"
    )


def _href(path: str) -> str:
    return quote(path, safe="/#=")


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
        f'<script type="application/json" id="review-data">{embedded}</script>\n'
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

_SCRIPT = (
    CSV_JS
    + """
(function () {
  var data = JSON.parse(document.getElementById("review-data").textContent);
  var FORMULA = /^[=+@]/;
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
            : "This browser is not keeping them: download the sheet before you close the page.");
    document.querySelectorAll("td.chosen").forEach(function (cell) {
      var chosen = decisions[cell.getAttribute("data-row")];
      cell.textContent = chosen ? chosen.decision : "";
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
  document.querySelectorAll("form.decision").forEach(function (form) {
    var row = form.getAttribute("data-row");
    var select = form.elements.decision;
    var note = form.elements.note;
    var problem = form.querySelector(".problem");
    var chosen = decisions[row];
    if (chosen && Array.prototype.some.call(select.options, function (option) {
      return option.value === chosen.decision;
    })) {
      select.value = chosen.decision;
      note.value = chosen.note || "";
    } else if (chosen) {
      delete decisions[row];
    }
    function update() {
      if (FORMULA.test(note.value)) {
        problem.textContent = "A note cannot start with =, + or @: review --apply refuses it " +
          "as a spreadsheet formula. This change is not kept.";
        return;
      }
      problem.textContent = "";
      if (select.value) {
        decisions[row] = {decision: select.value, note: note.value};
      } else {
        delete decisions[row];
      }
      store();
    }
    form.addEventListener("submit", function (event) { event.preventDefault(); });
    select.addEventListener("change", update);
    note.addEventListener("input", update);
    select.disabled = false;
    note.disabled = false;
  });
  document.getElementById("download-sheet").addEventListener("click", function () {
    save("review.csv", buildCsv(data.fields, data.rows, decisions), "text/csv;charset=utf-8");
  });
  document.getElementById("download-binding").addEventListener("click", function () {
    save("review.csv.binding.json", data.binding, "application/json");
  });
  document.getElementById("forget").addEventListener("click", function () {
    if (!window.confirm("Forget every decision chosen in this browser for this job?")) { return; }
    decisions = {};
    document.querySelectorAll("form.decision").forEach(function (form) {
      form.elements.decision.value = "";
      form.elements.note.value = "";
    });
    store();
  });
  ["download-sheet", "download-binding", "forget"].forEach(function (id) {
    document.getElementById(id).disabled = false;
  });
  show();
})();
"""
)
