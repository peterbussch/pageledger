"""After a job: whether the OCR language it was given is probably the document's.

This is advice for the person at the terminal. Nothing here is recorded in the job,
and nothing here can fail it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .adapters import _COMMUNITY_MODELS
from .checkpoint import read_record
from .image_source import KIND

ORUS_RECIPE = _COMMUNITY_MODELS["orus"][1]

LANGUAGE_NAMES = {
    "eng": "English",
    "rus": "Russian",
    "ukr": "Ukrainian",
    "bel": "Belarusian",
    "pol": "Polish",
    "lit": "Lithuanian",
    "lav": "Latvian",
    "est": "Estonian",
    "fin": "Finnish",
    "swe": "Swedish",
    "deu": "German",
    "fra": "French",
    "ita": "Italian",
    "spa": "Spanish",
    "nld": "Dutch",
    "lat": "Latin",
    "ces": "Czech",
    "ell": "Greek",
    "heb": "Hebrew",
    "yid": "Yiddish",
    "kaz": "Kazakh",
    "tur": "Turkish",
}

# The script each language is written in, and the language to suggest for a script.
_SCRIPTS = {
    "Latin": (
        "eng", "fra", "deu", "spa", "ita", "nld", "pol", "lit", "lav", "est", "fin", "swe",
        "dan", "nor", "por", "ces", "slk", "slv", "hrv", "hun", "ron", "lat", "tur",
    ),
    "Cyrillic": ("rus", "orus", "ukr", "bel", "bul", "srp", "mkd", "kaz", "kir", "tgk", "mon"),
    "Greek": ("ell", "grc"),
    "Hebrew": ("heb", "yid"),
    "Arabic": ("ara", "fas", "urd"),
    "Georgian": ("kat",),
    "Armenian": ("hye",),
}  # fmt: skip
_SUGGEST = {
    "Latin": "eng",
    "Cyrillic": "rus",
    "Greek": "ell",
    "Hebrew": "heb",
    "Arabic": "ara",
    "Georgian": "kat",
    "Armenian": "hye",
}
# Tesseract's script detection named the wrong script below this confidence on the
# pages it was tried on (a page of English prose came back "Cyrillic", 0.00), and the
# right one above it.
_SURE = 0.5


def language_names(lang: str) -> str:
    """'rus+eng' as 'Russian and English'; a code with no name stays a code."""
    names = [LANGUAGE_NAMES.get(code, code) for code in lang.split("+")]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def advice(job_dir: Path, retry: str | None = None) -> list[str]:
    """Lines to print after a job when its OCR language looks wrong.

    `retry` is the command to run again, with {lang} where the language goes;
    without it, the job's language came from a config file, and the advice names
    the setting there.
    """
    try:
        return _advice(job_dir, retry)
    except (Exception, KeyboardInterrupt):
        # An interrupt here comes after the job finished: skip the advice, not the summary.
        return []


def _advice(job_dir: Path, retry: str | None) -> list[str]:
    job = read_record(job_dir / "job.json")
    ocr = job["policy"].get("local_ocr")
    if not ocr or ocr["adapter"] != "pdf_ocr":
        return []
    lang = str(ocr["adapter_options"].get("lang", "eng"))
    codes = lang.split("+")
    pages = json.loads((job_dir / "document.json").read_text(encoding="utf-8"))["pages"]
    lines = []
    read = [
        page
        for page in pages
        if any(
            item["stage"] == "local_ocr" and item["outcome"] == "completed"
            for item in page["attempts"]
        )
    ]
    unsure = [
        page
        for page in read
        if page.get("review") is None and "low_confidence" in page["review_reasons"]
    ]
    if read and 2 * len(unsure) > len(read):
        change = (
            "run it again with --lang and that language's code"
            if retry
            else "set lang (processing.local_ocr.adapter_options.lang) in the config to its code"
        )
        lines.append(
            f"OCR was unsure of many words on {len(unsure)} of the {len(read)} pages it read."
        )
        script = _script(job, [page["page_number"] for page in unsure[:2]])
        known = all(any(code in members for members in _SCRIPTS.values()) for code in codes)
        written = {name for name, members in _SCRIPTS.items() if set(codes) & set(members)}
        if known and script in written:
            lines.append(
                f"Their script, {script}, fits the language given ({lang}), so the cause may "
                "be the scan or the print."
            )
            lines.append(f"If the document is in another {script}-script language, {change}.")
        elif known and script in _SUGGEST:
            suggested = _SUGGEST[script]
            lines.append(f"They look like {script} script, which {lang} is not written in.")
            if retry:
                lines.append(f"Try --lang {suggested}:")
                lines.append(f"  {retry.format(lang=suggested)}")
            else:
                lines.append(
                    f"Try {suggested}: set lang (processing.local_ocr.adapter_options.lang) "
                    "in the config."
                )
        else:
            lines.append(f"That can mean the language given ({lang}) is not the document's.")
            lines.append(f"If so, {change}.")
        lines.append("`pageledger doctor` lists the languages installed.")
    orthography = any(
        "historical_orthography" in item.get("warnings", [])
        for page in pages
        for item in page["attempts"]
    )
    if orthography and "rus" in codes:
        lines += [
            "Some pages are in pre-1918 Russian spelling.",
            "Tesseract's stock rus model cannot write ѣ, і, ѳ or ѵ; the community orus model can.",
            f"How to install it: {ORUS_RECIPE}",
        ]
    return lines


def _script(job: dict, numbers: list[int]) -> str | None:
    """The script Tesseract sees on these pages, when it is sure and they agree."""
    tesseract = shutil.which("tesseract")
    if tesseract is None or not numbers:
        return None
    from .adapters import _tesseract_installed_langs

    if "osd" not in (_tesseract_installed_langs(tesseract) or ()):
        return None
    source = job["source"]
    path = Path(source["path"])
    seen = set()
    with tempfile.TemporaryDirectory(prefix="pageledger-osd-") as scratch:
        for number in numbers:
            if source.get("kind") == KIND:
                image = path / source["images"][number - 1]["name"] if path.is_dir() else path
            elif path.suffix.lower() == ".pdf" and shutil.which("pdftoppm"):
                stem = Path(scratch) / f"page{number}"
                subprocess.run(
                    ["pdftoppm", "-r", "150", "-gray", "-f", str(number), "-l", str(number)]
                    + ["-singlefile", str(path), str(stem)],
                    capture_output=True,
                    timeout=60,
                    check=True,
                )
                image = stem.with_suffix(".pgm")
            else:
                return None
            found = _osd(tesseract, image)
            if found is None:
                return None
            seen.add(found)
    return seen.pop() if len(seen) == 1 else None


def _osd(tesseract: str, image: Path) -> str | None:
    proc = subprocess.run(
        [tesseract, str(image), "stdout", "--psm", "0", "-l", "osd"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    script = re.search(r"^Script: (\w+)", proc.stdout, re.MULTILINE)
    sure = re.search(r"^Script confidence: ([\d.]+)", proc.stdout, re.MULTILINE)
    if proc.returncode != 0 or script is None or sure is None or float(sure.group(1)) < _SURE:
        return None
    return script.group(1)
