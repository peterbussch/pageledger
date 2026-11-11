"""Read-only source inventory; annotation presence is separate from extracted body text."""

from __future__ import annotations

import codecs
from pathlib import Path

from .adapters import _reading_pdf
from .image_source import KIND, is_image_source

PDF = "pdf"
TEXT = "text"
_SOURCES = (
    "A source is a PDF, a UTF-8 text file (pages separated by form feeds), "
    "one JPEG or PNG page image, or a folder of them."
)


def source_kind(source: Path) -> str:
    """What a document job reads: ``pdf``, ``page_images`` or ``text``; anything else is refused.

    A PDF is known by its suffix, so a damaged one still becomes a job that
    records why it cannot be read. Text is any file that decodes as UTF-8.
    """
    if not source.exists():
        raise ValueError(f"Source file was not found: {source}")
    if is_image_source(source):
        return KIND
    if source.is_dir():
        raise ValueError(f"{source} is a folder with no page images in it. {_SOURCES}")
    if source.stat().st_size == 0:
        raise ValueError(f"{source} is empty (0 bytes), so there is nothing to read.")
    if source.suffix.lower() == ".pdf":
        return PDF
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise ValueError(
            f"{source} is not a PDF, a page image or UTF-8 text, so PageLedger cannot "
            f"process it. {_SOURCES}"
        ) from None
    return TEXT


def inspect_source(source: Path) -> tuple[int, dict]:
    if source.suffix.lower() != ".pdf":
        text = source.read_text(encoding="utf-8")
        return text.count("\f") + 1, {"status": "none", "count": 0}
    with source.open("rb") as stream:
        # Reading the page list here surfaces a needed password or missing AES
        # support as a typed diagnostic before the inventory checks below.
        with _reading_pdf(source) as open_pdf:
            reader = open_pdf(stream, strict=True)
            page_total = len(reader.pages)
        seen = set()

        def count_tree(reference):
            node = reference.get_object()
            identity = id(node)
            if identity in seen:
                raise ValueError("Repeated or cyclic PDF page-tree node")
            seen.add(identity)
            if node.get("/Type") == "/Page":
                return 1
            if node.get("/Type") != "/Pages" or "/Kids" not in node:
                raise ValueError("Invalid PDF page tree")
            count = sum(count_tree(child) for child in node["/Kids"])
            declared = node.get("/Count")
            if not isinstance(declared, int) or declared != count:
                raise ValueError("Declared PDF page count differs from actual page inventory")
            return count

        catalog = reader.trailer["/Root"]
        if not isinstance(catalog, dict) or "/Pages" not in catalog:
            raise ValueError("Invalid PDF catalog")
        count = count_tree(catalog["/Pages"])
        if count < 1 or page_total != count:
            raise ValueError("PDF page inventory mismatch")
        annotations = 0
        for page in reader.pages:
            values = page.get("/Annots")
            if values is not None:
                values = values.get_object()
                if not isinstance(values, list):
                    raise ValueError("Invalid PDF annotation inventory")
                annotations += len(values)
        return count, {"status": "present" if annotations else "none", "count": annotations}
