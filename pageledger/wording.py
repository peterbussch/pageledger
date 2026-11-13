"""What warning codes, hold reasons and page states mean, in plain words.

One table, used by the document report, the terminal and the review view. The
code stays beside the words wherever they are shown, so that a reader can look
it up in docs/warnings.md, whose "In plain words" section repeats this table
(a test keeps the two equal).
"""

from __future__ import annotations

# The docs of the release that introduced the report's wording. A report links here
# rather than to the installed version's, so that its bytes stay the same after an
# upgrade, and a reader without a checkout can follow the link.
DOCS = "https://github.com/peterbussch/pageledger/blob/v0.9.0/docs/"

# Why a page is held for a person, and the states a page is in before review.
# Each is a clause, without a full stop, that reads after a page number or a count.
HOLDS = {
    "source_defect": "The source page itself is damaged or incomplete",
    "numeric_column_conflict": "Numbers in a table do not fit their columns or rows",
    "numeric_disagreement": "Two readings give different numbers",
    "engine_disagreement": "Two readings share fewer than 60% of their words",
    "unconfirmed_model_output": "A generative model wrote the text, and no other engine confirmed it",
    "contested": "The model's and the OCR's readings differ in places not yet settled",
    "coverage_defect": "Text may be missing or damaged in the reading",
    "low_confidence": "The OCR engine was unsure of many words",
    "handwriting": "An engine judged the page to be handwriting",
    "unreadable": "An engine judged the page unreadable",
    "illustration": "An engine judged the page to be an illustration, not text",
    "blank_candidate": "No reading found text, or one came back empty: the page may be blank",
    "provider_failure": "The last attempt to read the page failed",
    "outcome_unknown": "A request to read the page may have been sent, but no result was saved",
    "pending": "Not read yet",
    "unreviewed_text": "Text selected and nothing held the page; no person has checked it",
    "adjudicated_text": (
        "Every difference between the model's and the OCR's readings is settled; "
        "no person has read the page"
    ),
}

# A blank candidate whose selected reading has text, when nothing else holds the page:
# an empty reading raised the hold, and no reading without a hold of its own has
# confirmed the text. BLANK_SCAN is the same when the empty reading is a scan's
# missing text layer, and BLANK_SHORT when no reading was empty (a reading too short
# to trust raised it). Where another reason holds the page, the blank hold is not shown.
BLANK_WITH_TEXT = (
    "An earlier reading was empty, and no reading without a hold of its own confirms the "
    "page's text"
)
BLANK_SCAN = "The page is a scan with no text of its own, and no other reading confirms the OCR"
BLANK_SHORT = "Very little text was read, and nothing else confirms it: the page may be blank"

# What is wrong with a reading, by the cause of its hold (a warning or classification
# code, or a rule of the job's own). Each is a clause, without a full stop, that reads
# after the reading's name: "Tesseract OCR (rus) wrote a capital Ъ inside words, …".
CAUSES = {
    "alignment": "does not fit the table structure the job expected",
    "blank": "judged the page blank",
    "blank_candidate": "found that the page may be blank",
    "capital_sign_in_word": (
        "wrote a capital Ъ or Ь inside words, as OCR for modern Russian does with pre-1918 letters"
    ),
    "clipped_text": "had some of its text clipped, its engine reports",
    "column_conflict": "has values that conflict with their table columns",
    "control_characters": "holds control characters that stand for no visible text",
    "coverage_defect": "may be missing text",
    "digits_only_text": "is nearly all digits, as when a table's words are lost",
    "empty_reading": "is empty",
    "empty_text": "is empty, with no letters or digits",
    "foreign_script_characters": (
        "has a few letters from an unrelated script, usually symbols read through the wrong font map"
    ),
    "fragmented": "has its text broken into fragments",
    "fragmented_text": "has many words broken into pieces",
    "handwriting": "judged the page to be handwriting",
    "historical_letters_lost": "lacks the pre-reform letters (ѣ, і, final ъ) the job expects",
    "illustration": "judged the page to be an illustration",
    "instruction_echo": (
        "holds a chat-template marker: the model may have returned its own instructions"
    ),
    "joined": "has words run together",
    "joined_text": "has words run together",
    "labels_without_values": (
        "has table labels that end in dot leaders with no number after them: the values seem lost"
    ),
    "layer_outvoted": "disagrees with two of the job's readings that agree with each other",
    "letters_lost": "lost the old-spelling letters (such as ѣ) that a different reading kept",
    "low_confidence": "was unsure of many words",
    "missing_content": "is missing content, its engine reports",
    "missing_required_columns": "lacks a table column the job requires",
    "mixed_script_tokens": "has words that mix look-alike Latin, Cyrillic or Greek letters",
    "model_commentary": "added a note of its own that is not on the page",
    "numeric_column_conflict": "has numbers that conflict with their table columns",
    "output_truncated": "was cut off",
    "partial_transcription": "says it left some text out",
    "private_use_characters": (
        "uses a font's private codes for several characters, which may stand for lost digits "
        "or letters"
    ),
    "reader_short": "has far fewer words than the OCR reading",
    "repeated_page_text": "is the same short text as several other pages, often a stamp or watermark",
    "repetition_loop": "repeats itself in a loop",
    "replacement_characters": "has replacement characters where bytes could not be decoded",
    "script_mismatch": "has fewer than half its letters in the configured script",
    "source_defect": "found the source page damaged or incomplete",
    "sparse": "has very little text",
    "split_words": "has many words broken by stray spaces",
    "suspicious_embedded_text_delta": "is far longer or shorter than the PDF's own text",
    "suspicious_symbol_density": "has many unusual symbols, often from a damaged text layer",
    "tile_edge_cut": "has a line of text cut by a tile's edge, which may be read twice or not at all",
    "tile_failed": "lacks the text of one or more tiles the model would not read",
    "truncated_text": "is truncated",
    "unknown": "could not be classified",
    "unreadable": "judged the page unreadable",
}
# Where the flawed reading is not the selected text. An inherited OCR layer holds the
# page only while no other reading confirms the selected one; any other reading always.
# The hold is not a formality: the layer has been right where a clean-looking OCR was not.
NOT_CONFIRMED = (
    "the selected text is {selected}; {name}, {other}, {flaw}, so their comparison does not "
    "count and nothing confirms the selected text"
)
ONLY_OTHER = "the only other reading"
COULD_CONFIRM = "the reading that could have confirmed it"
# What to do about pages held that way, said once in the report.
UNCONFIRMED_ADVICE = (
    "Check each page against its image in the review view, where both readings are shown: an "
    "OCR reading with no warning of its own can still be wrong, and an earlier OCR with a "
    "flaw of its own can have those words right.",
    "For a whole book of such pages, add a second OCR engine (`second_ocr` in the config; "
    "`pageledger init` writes it as a commented block), so that a second reading can confirm "
    "the first.",
    f"Why these pages are held: [Hold policy 0.9]({DOCS}processing-spec.md#hold-policy-09), "
    'under "What holding for the layer\'s warnings costs".',
)
ANOTHER_READING = (
    "the selected text is {selected}; {name}, another reading of the page, {flaw}, so it "
    "cannot confirm the selected text"
)

# A person's review decision.
REVIEWED = {
    "reviewed_text": "A person checked the text against the page",
    "reviewed_blank": "A person confirmed the page is blank",
    "illustration": "A person recorded the page as an illustration",
    "handwriting": "A person recorded the page as handwriting",
    "unreadable": "A person recorded the page as unreadable",
    "source_defect": "A person recorded a problem with the source page",
}

# What a warning on a reading says. Full sentences.
WARNINGS = {
    "alto_geometry_mismatch": "The ALTO file's word boxes do not fit this page, so they are not trusted.",
    "alto_invalid": "The page's ALTO file could not be used.",
    "alto_layout_rejected": (
        "The ALTO layout disagreed with where the OCR found text, so the tiles followed the OCR."
    ),
    "alto_missing": "No ALTO file was found for this page.",
    "blank": "An engine judged the page blank.",
    "blank_candidate": "The page may be blank.",
    "capital_sign_in_word": (
        "Capital Ъ or Ь stand inside lower-case words: OCR trained on modern spelling "
        "misreading pre-reform print."
    ),
    "clipped_text": "The engine reports that some text was clipped.",
    "column_conflict": "Values and their table columns conflict.",
    "control_characters": "The reading holds control characters that stand for no visible text.",
    "coverage_defect": "Text may be missing from the reading.",
    "digits_only_text": "The reading is nearly all digits, as when a table's words were lost.",
    "empty_text": "The reading is empty: it has no letters or digits.",
    "engine_disagreement": "Two readings share fewer than 60% of their words.",
    "foreign_script_characters": (
        "A few letters from an unrelated script appear, usually symbols read through the "
        "wrong font map."
    ),
    "fragmented": "The page's text is broken into fragments.",
    "fragmented_text": "Many words are unusually short, as if broken into pieces.",
    "handwriting": "An engine judged the page to be handwriting.",
    "historical_letters_lost": (
        "The job expects pre-reform spelling, but the reading lacks its letters (ѣ, і, final ъ)."
    ),
    "historical_orthography": (
        "The text has pre-reform Russian spelling (letters such as ѣ and і), which an "
        "engine trained on modern spelling cannot write."
    ),
    "illustration": "An engine judged the page to be an illustration.",
    "instruction_echo": (
        "The reading holds a chat-template marker: a model may have returned its own instructions."
    ),
    "joined": "Words appear run together.",
    "joined_text": "Many Latin-script words are run together, with few spaces.",
    "labels_without_values": (
        "Table labels end in dot leaders with no number after them: the values seem lost."
    ),
    "low_confidence": "The engine marked at least a quarter of the words as uncertain.",
    "missing_content": "Content is reported missing from the reading.",
    "missing_required_columns": "A required table column was not found.",
    "mixed_script_tokens": "Some words mix look-alike Latin, Cyrillic or Greek letters.",
    "model_commentary": "A generative reader added a note of its own that is not on the page.",
    "multi_column_rows": "The engine read several columns side by side as single lines.",
    "no_text_layer": "The source is page images, which carry no text of their own.",
    "numeric_column_conflict": "Numbers conflict with their table columns.",
    "numeric_disagreement": "Two readings give different numbers.",
    "ocr_text_layer": "The PDF's text layer is an earlier OCR of the scan, not typeset text.",
    "output_inflation": (
        "A rerun's reading is far longer than the earlier one, which can mean repeated or "
        "invented text."
    ),
    "output_truncated": "The engine's output was cut off.",
    "partial_transcription": "A generative reader says it left some text out.",
    "private_use_characters": (
        "Several characters use a font's private codes, which may stand for lost digits or letters."
    ),
    "render_dpi_capped": (
        "The page was rendered for OCR at a lower resolution than asked, to stay within "
        "the pixel limit."
    ),
    "render_upsampled": (
        "The page was rendered at more pixels than its source image has; the extra pixels "
        "add no detail."
    ),
    "repeated_page_text": (
        "The same short text makes up the whole reading of several pages, often a stamp "
        "or watermark."
    ),
    "repetition_loop": "The text repeats in a loop, as a vision model sometimes does.",
    "replacement_characters": "The reading has replacement characters where bytes could not be decoded.",
    "script_mismatch": "Fewer than half the letters are in the configured script.",
    "short_text": "Fewer than ten characters were read.",
    "source_defect": "The source itself appears damaged or incomplete.",
    "sparse": "Very little text was found on the page.",
    "split_words": "Many words are broken by stray spaces.",
    "suspicious_embedded_text_delta": (
        "The OCR reading and the PDF's text layer differ greatly in length."
    ),
    "suspicious_symbol_density": (
        "The reading has many unusual symbols, often from a damaged text layer."
    ),
    "tile_edge_cut": "A tile's edge cuts through a line of text, which may be read twice or not at all.",
    "tile_failed": "The model would not read one or more tiles of the page; their text is missing.",
    "tile_layout_missing": "There was no layout to cut the page along, so it was read whole.",
    "tile_text_small": "In some tiles the print is smaller than the configured minimum.",
    "truncated_text": "The reading is truncated.",
    "unconfirmed_model_output": "A generative model wrote the reading, and no other engine confirmed it.",
    "unknown": "The page's text could not be classified.",
    "unreadable": "An engine judged the page unreadable.",
}

# What to do about a warning that holds nothing.
ADVICE = {
    "alto_geometry_mismatch": "Check that the ALTO files come from the same scan as the pages.",
    "alto_invalid": "Open the ALTO file and check that it is this page's.",
    "alto_layout_rejected": "Check that the ALTO files belong to these page images.",
    "alto_missing": "Check the adapter's `name` template against the files' real names.",
    "historical_orthography": (
        "Stock OCR models for modern Russian, such as Tesseract `rus`, cannot write ѣ, і, ѳ "
        "or ѵ, so their readings drop or garble these letters. For pre-1918 print, try a "
        "model that writes them on a few pages first, such as the community `orus` model: in "
        "PageLedger's test it kept more of these letters, but made more errors overall ("
        f"[engine recipes]({DOCS}engine-recipes.md"
        "#tesseract-with-the-community-orus-model-macos-and-linux))."
    ),
    "multi_column_rows": (
        "Check the line order against the page; for RapidOCR set `reading_order: columns` "
        "and read the page again."
    ),
    "no_text_layer": "Nothing: these pages are read by OCR.",
    "ocr_text_layer": (
        "Treat the layer as one OCR reading among others; where the job read the page again, "
        "it compared the two."
    ),
    "output_inflation": "Compare both readings with the page; longer is not necessarily more complete.",
    "render_dpi_capped": (
        "If a reading is poor, raise `max_render_pixels` as far as memory allows and read the "
        "page again."
    ),
    "render_upsampled": (
        "Judge the reading by the source image, and look for a higher-resolution copy of the page."
    ),
    "short_text": "Accept the text only if the page really holds this little.",
    "tile_layout_missing": "Nothing to fix: the page was read whole rather than in tiles.",
    "tile_text_small": "Expect more misreadings there; a higher `max_tiles` or `max_image_side` helps.",
}

UNKNOWN_WARNING = "A warning from the engine's adapter; see that adapter's documentation."

# A reading named by what made it, for jobs whose report records no engines.
_STAGE_READERS = {
    "local_text": "the page's own text",
    "local_ocr": "the OCR engine",
    "second_ocr": "the second OCR engine",
    "local_image": "the local image model",
    "image": "the image model",
    "second_opinion": "the second-opinion model",
}
CORRECTION = "a person's correction"


def reading_name(
    stage: str, engine: dict | None = None, *, layer: bool = False, images: bool = False
) -> str:
    """What made a reading, in words: the engine the job's stage ran, from the report's
    ``engines``. ``layer`` marks a text layer an earlier OCR wrote; ``images`` a source
    of page images, which have no text of their own."""
    adapter = (engine or {}).get("adapter")
    if adapter in {"pdf_text", "pdf"} or (adapter is None and stage == "local_text"):
        name = (
            "the page image's own text"
            if images
            else "the PDF's own text"
            if adapter
            else _STAGE_READERS["local_text"]
        )
        return name + (" (an earlier OCR)" if layer else "")
    if adapter == "text":
        return "the text file"
    if adapter == "pdf_ocr":
        name = (
            f"Tesseract OCR ({engine['lang']})"
            if engine and engine.get("lang")
            else "Tesseract OCR"
        )
    elif adapter == "rapidocr":
        name = "RapidOCR"
    elif adapter == "indra":
        name = "Indra OCR"
    elif adapter == "alto":
        name = "the page's ALTO file"
    elif adapter == "vision":
        local = "local " if stage == "local_image" else ""
        model = (engine or {}).get("model")
        name = f"the {local}vision model {model}" if model else f"the {local}vision model"
    elif adapter:
        name = f"the engine {adapter}"
    else:
        return _STAGE_READERS.get(stage, f"the {stage} stage")
    if stage == "second_ocr":
        return f"the second OCR engine ({name})"
    if stage == "second_opinion":
        return f"the second opinion ({name})"
    return name


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


# The review view's decisions, grouped as a person decides, with what each records.
# `use:` stands for a reading the page offers; the recorded values are review --apply's.
DECISION_GROUPS = (
    (
        "The text is right",
        (("accept", "The selected text is right", "that you checked the text against the page"),),
    ),
    (
        "Use another reading",
        (("use:", "Use READING instead", "that another reading is right; it becomes the text"),),
    ),
    (
        "I corrected the text",
        (
            (
                "correct",
                "My corrected text, below",
                "your text as a new reading; the engines' readings stay as they are",
            ),
        ),
    ),
    (
        "This page has no text to transcribe",
        (
            ("blank", "Blank page", "that the page has no text"),
            ("illustration", "Illustration, map or chart", "that the page is a picture, not text"),
            ("handwriting", "Handwriting", "that the page is handwritten, left for a specialist"),
        ),
    ),
    (
        "The page cannot be read",
        (
            ("unreadable", "Unreadable", "that the page cannot be read in this copy"),
            (
                "source_defect",
                "Damaged or missing source page",
                "that the source page is damaged or incomplete; another copy is needed",
            ),
        ),
    ),
)


def warning(code: str) -> str:
    return WARNINGS.get(code, UNKNOWN_WARNING)


def warning_code(item: object) -> str | None:
    """The code of a warning recorded as a string or as a mapping."""
    code = item.get("type", item.get("code")) if isinstance(item, dict) else item
    return code if isinstance(code, str) else None
