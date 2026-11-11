"""Hold policy 0.9: an inherited OCR layer is a witness, and pages that lost what a witness kept are held."""

from pathlib import Path

import pytest
from test_escalation import _attempt, _page
from test_processing import launch, setup  # noqa: F401

from pageledger.adapters import PdfTextAdapter, _pdf_page_reading
from pageledger.checkpoint import read_record
from pageledger.processing import verify_job
from pageledger.processing_policy import assess_page, yat_words
from pageledger.quality import _content_coverage_metrics, _misread_warnings

LADDER = ("local_text", "local_ocr", "second_ocr", "local_image", "image", "second_opinion")
LAYER = ["ocr_text_layer"]
# Pre-reform words with ѣ, and the same words as stock Tesseract rus writes them.
KEPT = " ".join(["Бѣлоконскій служилъ въ мѣстѣ"] * 8)
LOST = " ".join(["Бълоконский служилъ въ мъсть"] * 8)


def _compared(left, right, ratio, numbers=()):
    return {
        "left_attempt": left,
        "right_attempt": right,
        "agreement_ratio": ratio,
        "spans": [],
        "number_differences": list(numbers),
    }


# ---------------------------------------------------------------------------
# Inherited OCR layers drawn in ordinary fonts (finding 2)
# ---------------------------------------------------------------------------


def _pdf(
    path: Path,
    content: bytes,
    *,
    producer: str | None = None,
    image: dict | None = None,
    form: bytes | None = None,
    states: dict | None = None,
) -> Path:
    """A 200 x 300 point page with one font, an optional image, an optional form, and
    graphics states named /GS1, /GS2... with the given keys."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Times-Roman"),
        }
    )
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    resources = DictionaryObject({NameObject("/Font"): fonts})
    xobjects = DictionaryObject()
    if image is not None:
        picture = DecodedStreamObject()
        picture.set_data(b"\x80" * 4)
        picture.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(image.get("width", 2)),
                NameObject("/Height"): NumberObject(image.get("height", 3)),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        if image.get("smask"):
            mask = DecodedStreamObject()
            mask.set_data(b"\x00" * 4)
            mask.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(2),
                    NameObject("/Height"): NumberObject(3),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            picture[NameObject("/SMask")] = writer._add_object(mask)
        reference = writer._add_object(picture)
        if form is None:
            xobjects[NameObject("/Im1")] = reference
        else:
            scan = DecodedStreamObject()
            scan.set_data(form)
            scan.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Form"),
                    NameObject("/BBox"): ArrayObject(
                        [FloatObject(0), FloatObject(0), FloatObject(200), FloatObject(300)]
                    ),
                    NameObject("/Resources"): DictionaryObject(
                        {
                            NameObject("/Font"): fonts,
                            NameObject("/XObject"): DictionaryObject(
                                {NameObject("/Im1"): reference}
                            ),
                        }
                    ),
                }
            )
            xobjects[NameObject("/Fm1")] = writer._add_object(scan)
        resources[NameObject("/XObject")] = xobjects
    if states:
        resources[NameObject("/ExtGState")] = DictionaryObject(
            {
                NameObject(name): DictionaryObject(
                    {
                        NameObject(key): FloatObject(value)
                        if isinstance(value, float)
                        else NameObject(value)
                        for key, value in keys.items()
                    }
                )
                for name, keys in states.items()
            }
        )
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if producer:
        writer.add_metadata({"/Producer": producer})
    writer.write(path)
    return path


TEXT = b"BT /F1 12 Tf 20 200 Td (Hello PageLedger) Tj ET"
SCAN = b"q 200 0 0 300 0 0 cm /Im1 Do Q"
LOGO = b"q 20 0 0 30 10 10 cm /Im1 Do Q"


def _signals(path: Path) -> list[str] | None:
    evidence = _pdf_page_reading(path, 1)[1]
    return None if evidence is None else evidence["signals"]


def test_invisible_text_over_a_page_image_is_an_ocr_layer(tmp_path):
    pytest.importorskip("pypdf")
    path = _pdf(tmp_path / "lura.pdf", SCAN + b" 3 Tr " + TEXT, image={})
    assert _signals(path) == ["invisible_text"]
    read = PdfTextAdapter().extract(path, page_id="p", page_number=1, action="transcribe_text")
    assert read.warnings == ["ocr_text_layer"]
    assert read.confidence_detail == {"ocr_text_layer": {"signals": ["invisible_text"]}}


def test_text_painted_under_an_opaque_page_image_is_an_ocr_layer(tmp_path):
    pytest.importorskip("pypdf")
    # ABBYY FineReader 11 draws its text first and lays the scan over it.
    path = _pdf(
        tmp_path / "abbyy.pdf", TEXT + b" " + SCAN, image={}, producer="ABBYY FineReader 11"
    )
    evidence = _pdf_page_reading(path, 1)[1]
    assert evidence == {
        "signals": ["text_under_image", "producer"],
        "producer": "ABBYY FineReader 11",
    }


def test_a_scan_and_its_invisible_text_inside_a_form_are_found(tmp_path):
    pytest.importorskip("pypdf")
    # ABBYY Recognition Server's 1788 dictionary: the page draws one form holding both.
    form = SCAN + b" 3 Tr " + TEXT
    path = _pdf(tmp_path / "server.pdf", b"q /Fm1 Do Q", image={}, form=form)
    assert _signals(path) == ["invisible_text"]


def test_an_ocr_producer_counts_only_on_a_page_with_a_page_image(tmp_path):
    pytest.importorskip("pypdf")
    on_scan = _pdf(tmp_path / "scan.pdf", SCAN + b" " + TEXT, image={}, producer="OmniPage 19")
    assert _signals(on_scan) == ["producer"]
    typeset = _pdf(tmp_path / "typeset.pdf", TEXT, producer="ABBYY FineReader PDF 15")
    assert _signals(typeset) is None


@pytest.mark.parametrize(
    "content,image",
    [
        # A born-digital page with a full-page background picture under visible text.
        (SCAN + b" " + TEXT, {}),
        # A transparent full-page watermark laid over the text.
        (TEXT + b" " + SCAN, {"smask": True}),
        # A small logo drawn after the text.
        (TEXT + b" " + LOGO, {}),
        # Invisible text with no page image (a tagged accessibility layer).
        (b"3 Tr " + TEXT, None),
    ],
)
def test_a_born_digital_page_is_not_an_ocr_layer(tmp_path, content, image):
    pytest.importorskip("pypdf")
    path = _pdf(tmp_path / "digital.pdf", content, image=image, producer="Microsoft Word")
    assert _signals(path) is None
    read = PdfTextAdapter().extract(path, page_id="p", page_number=1, action="transcribe_text")
    assert read.warnings == [] and read.confidence_detail is None


STAMPS = {
    "/GS1": {"/ca": 0.15, "/CA": 0.15},
    "/GS2": {"/BM": "/Multiply"},
    "/GS3": {"/ca": 1.0, "/CA": 1.0},
    "/GS4": {"/BM": "/Normal"},
}


@pytest.mark.parametrize(
    "stamp",
    [
        # A watermark stamped over the text at 15% opacity, with no mask on the image.
        b"q /GS1 gs " + SCAN + b" Q",
        # One multiplied over it, as a highlight is.
        b"q /GS2 gs " + SCAN + b" Q",
        # A later state that sets only the blend mode keeps the alpha before it.
        b"q /GS1 gs /GS4 gs " + SCAN + b" Q",
        # Inside a form, the state set before drawing it still applies.
        b"q /GS1 gs /Fm1 Do Q",
    ],
)
def test_a_see_through_watermark_over_typeset_text_is_not_an_ocr_layer(tmp_path, stamp):
    pytest.importorskip("pypdf")
    form = SCAN if b"/Fm1" in stamp else None
    path = _pdf(tmp_path / "stamped.pdf", TEXT + b" " + stamp, image={}, form=form, states=STAMPS)
    assert _signals(path) is None


def test_a_scan_painted_opaquely_after_a_graphics_state_still_hides_its_text(tmp_path):
    pytest.importorskip("pypdf")
    # The alpha set for the text is restored to 1 before the scan, or popped with Q.
    for drawing in (
        b"/GS1 gs " + TEXT + b" /GS3 gs " + SCAN,
        b"q /GS1 gs " + TEXT + b" Q " + SCAN,
    ):
        path = _pdf(tmp_path / "abbyy.pdf", drawing, image={}, states=STAMPS)
        assert _signals(path) == ["text_under_image"]


def test_a_drawing_the_detector_cannot_follow_gives_no_signal_and_the_text_is_read(
    tmp_path, monkeypatch
):
    pytest.importorskip("pypdf")
    import pypdf.generic
    from pypdf.errors import PdfReadError

    path = _pdf(tmp_path / "lura.pdf", SCAN + b" 3 Tr " + TEXT, image={})

    def broken(*args, **kwargs):
        raise PdfReadError("Unexpected end of stream")

    monkeypatch.setattr(pypdf.generic, "ContentStream", broken)
    text, evidence = _pdf_page_reading(path, 1)
    assert "Hello PageLedger" in text
    assert evidence is None


# ---------------------------------------------------------------------------
# Readings with broken words, impossible capitals, or labels without values
# ---------------------------------------------------------------------------


def test_a_layer_that_splits_off_the_last_letter_of_words_is_flagged():
    # The 1939 Leningrad directory: "Сырны й комбина т 12 6".
    split = " ".join(["Сырны й комбина т Декабристо в набережна я"] * 10)
    assert "split_words" in _misread_warnings(split)
    modern = " ".join(["В Москве и в Твери у реки с домом к г. Тула я и он а также"] * 10)
    assert "split_words" not in _misread_warnings(modern)
    # A textbook marks stress with a combining accent, which does not split the word.
    stressed = " ".join(["е́хать я е́ду ты е́дешь он е́дет мы е́дем"] * 10)
    assert "split_words" not in _misread_warnings(stressed)
    # Ukrainian and Belarusian have one-letter words Russian lacks.
    ukrainian = " ".join(["Він і вона й діти з міста пішли у школу"] * 10)
    belarusian = " ".join(["Ён і яна з дзецьмі пайшлі ў школу а потым"] * 10)
    assert _misread_warnings(ukrainian) == [] and _misread_warnings(belarusian) == []


def test_units_after_numbers_and_church_slavonic_marks_are_not_split_words():
    # A Soviet production table writes a unit of one letter in every row.
    table = "\n".join(
        [
            "Электроэнергия, млрд кВт·ч 48,3 91,2 170,2 292,3",
            *(f"{name}, млн т 31,1 37,9 70,8 147,9" for name in ("Нефть", "Уголь", "Сталь")),
            "Ткани хлопчатобумажные, млрд м 3,9 3,8 5,1 6,0",
            *(f"Продукт номер {n}, млн т 1,{n}" for n in range(12)),
        ]
    )
    assert _misread_warnings(table) == []
    units = " ".join(["Высота стены 5 м и ширина 3 м, вес 4 т, время 2 ч, объём 7 л."] * 8)
    assert _misread_warnings(units) == []
    # A titlo over a numeral or an abbreviation is a mark on the letter, not a letter.
    slavonic = " ".join(["Бг҃ъ ѿ нбсе призрѣ̀ на сыны̀ гл҃ава а҃ стїхъ в҃ г҃ д҃ є҃ ѕ҃ з҃ и҃ ѳ҃ і҃"] * 4)
    assert _misread_warnings(slavonic) == []
    # Letters a misreading put inside numbers, or after a number on the next line, still count.
    garbled = " ".join(["Сырны й комбина т 6,6т 18.69т,6 2.377\nМ е л е н к"] * 10)
    assert "split_words" in _misread_warnings(garbled)


def test_a_capital_hard_sign_inside_a_lower_case_word_is_flagged():
    misread = " ".join(["благочестивЪйшихЪ золотниковЪ мЪсто городъ"] * 15)
    assert "capital_sign_in_word" in _misread_warnings(misread)
    # Pre-reform words end in a lower-case ъ, and a heading is in capitals throughout.
    correct = " ".join(["ГОРОДЪ ТУЛА городъ служилъ въ мѣстѣ ОБЪЯВЛЕНІЕ подъѣздъ"] * 15)
    assert "capital_sign_in_word" not in _misread_warnings(correct)


def test_table_labels_whose_values_are_missing_are_flagged():
    labels = "\n".join(f"{n} Волость {n} . . . ." for n in range(4, 38))
    assert _misread_warnings(labels) == ["labels_without_values"]
    with_values = "\n".join(f"{n} Волость . . . . 1.590 4.234" for n in range(4, 38))
    assert _misread_warnings(with_values) == []
    # Columns extracted one after another: the values follow the labels.
    serialized = labels + "\n" + "\n".join("1.590 4.234 378" for _ in range(34))
    assert _misread_warnings(serialized) == []
    # Dialogue ending its lines in an ellipsis is not a table.
    dialogue = "\n".join("— Ты еще Салтычиху вспомни..." for _ in range(20))
    assert _misread_warnings(dialogue) == []
    # A table of contents numbered in roman numerals has its values.
    contents = "\n".join(
        f"Глава {n}. О предмете номер {n} ........ {roman}"
        for n, roman in enumerate("i ii iii iv v vi vii viii ix x xi XII".split(), 1)
    )
    assert _misread_warnings(contents) == []
    # A blank form reads like a table whose values were lost, and is held the same way.
    fields = "Фамилия Имя Отчество Год Место Национальность Язык Образование Профессия Адрес"
    form = "\n".join(f"{label} ...................." for label in fields.split())
    assert _misread_warnings(form) == ["labels_without_values"]


# ---------------------------------------------------------------------------
# The inherited layer and the job's own OCR as two witnesses (finding 1)
# ---------------------------------------------------------------------------


def _kiev(ocr_text=LOST, ratio=0.69):
    # The 1913 Kiev directory: the Internet Archive layer keeps ѣ and і; stock rus cannot.
    layer = _attempt("a1", "local_text", KEPT, warnings=LAYER)
    ocr = _attempt("a2", "local_ocr", ocr_text)
    return _page(layer, ocr, comparisons=[_compared("a1", "a2", ratio)])


def test_yat_words_count_only_well_formed_words():
    assert yat_words(KEPT) == 16
    assert yat_words(LOST) == 0
    assert yat_words("ѣ *Ѣм* ,ѣ« ГОРОДѢ мѣ") == 0


def test_ocr_that_lost_the_layer_s_pre_reform_letters_is_held_and_the_layer_selected():
    page = _kiev()
    before = assess_page(page, ocr_layer_climbs=True, corroborate=True)
    assert (before["selected_attempt"], before["disposition"]) == ("a2", "unreviewed_text")
    after = assess_page(page, ocr_layer_climbs=True, corroborate=True, witnesses=True)
    assert after["selected_attempt"] == "a1"
    assert after["disposition"] == "coverage_defect"
    assert after["next_action"] == "review"


def test_ocr_that_kept_the_letters_settles_with_the_layer():
    agreed = assess_page(_kiev(KEPT, 0.95), ocr_layer_climbs=True, witnesses=True)
    assert (agreed["selected_attempt"], agreed["disposition"]) == ("a2", "unreviewed_text")


def test_a_modern_russian_page_where_layer_and_ocr_agree_is_not_held():
    layer = _attempt("a1", "local_text", "В Москве открыт новый завод", warnings=LAYER)
    ocr = _attempt("a2", "local_ocr", "В Москве открыт новый завод")
    page = _page(layer, ocr, comparisons=[_compared("a1", "a2", 1.0)])
    result = assess_page(page, ocr_layer_climbs=True, corroborate=True, witnesses=True)
    assert (result["selected_attempt"], result["disposition"]) == ("a2", "unreviewed_text")
    assert result["review_reasons"] == []


def test_a_layer_that_disagrees_holds_the_page_but_buys_no_reader():
    page = _kiev(LOST.replace("служилъ", "служил"), 0.3)
    page["attempts"][0]["text"] = page["attempts"][1]["text"].replace("Бълоконский", "Б.")
    page["comparisons"][0]["number_differences"] = [{"number": "255", "side": "left"}]
    result = assess_page(
        page, ocr_layer_climbs=True, witnesses=True, escalate_on=("hold", "disagreement")
    )
    assert result["selected_attempt"] == "a2"
    assert "engine_disagreement" in result["review_reasons"]
    # The layer's numbers are not compared: it misread one on every right Internet Archive page.
    assert "numeric_disagreement" not in result["review_reasons"]
    assert result["next_action"] == "review"
    # Under 0.8 the layer was set aside and the page passed unread by anyone else.
    before = assess_page(page, ocr_layer_climbs=True, escalate_on=("hold", "disagreement"))
    assert before["disposition"] == "unreviewed_text"


def test_two_of_our_engines_that_agree_outvote_the_layer():
    page = _kiev(LOST, 0.3)
    page["attempts"][0]["text"] = "Б. служил"
    page["attempts"].append(_attempt("a3", "second_ocr", LOST))
    page["comparisons"] += [
        _compared("a1", "a3", 0.3, numbers=[{"number": "255", "side": "left"}]),
        _compared("a2", "a3", 0.97),
    ]
    result = assess_page(page, ocr_layer_climbs=True, witnesses=True, stages=LADDER)
    assert (result["selected_attempt"], result["disposition"]) == ("a2", "unreviewed_text")


def test_a_disagreeing_layer_still_sends_the_reading_to_the_second_ocr():
    page = _kiev(LOST.replace("служилъ", "служил"), 0.3)
    page["attempts"][0]["text"] = "Б. служил"
    result = assess_page(
        page, ocr_layer_climbs=True, corroborate=True, witnesses=True, stages=LADDER
    )
    assert result["next_action"] == "second_ocr"
    assert result["triggers"][-1]["trigger"] == "uncorroborated"


def test_a_junk_layer_does_not_win_over_the_ocr_it_disagrees_with():
    # Teledir finding 10, with one OCR engine: the OCR is longer than the junk layer.
    layer = _attempt("a1", "local_text", "Wl ASHI NGT0N ~~ dlr", warnings=LAYER)
    ocr = _attempt(
        "a2", "local_ocr", "WASHINGTON DIRECTORY 1934", warnings=["suspicious_embedded_text_delta"]
    )
    page = _page(layer, ocr, comparisons=[_compared("a1", "a2", 0.2)])
    result = assess_page(page, ocr_layer_climbs=True, corroborate=True, witnesses=True)
    assert result["selected_attempt"] == "a2"
    assert "coverage_defect" not in result["review_reasons"]
    assert result["disposition"] == "engine_disagreement"
    # A born-digital layer is the page's own text: the length warning still holds the OCR.
    typeset = _page({**layer, "warnings": []}, ocr, comparisons=page["comparisons"])
    held = assess_page(typeset, witnesses=True)
    assert "coverage_defect" in held["review_reasons"]


def test_a_damaged_layer_holds_the_page_until_another_reading_confirms_the_ocr():
    # The 1939 Leningrad directory: the layer split its words, and the one OCR reading
    # of the page misread names too.
    layer = _attempt("a1", "local_text", "Сырны й комбина т", warnings=[*LAYER, "split_words"])
    ocr = _attempt("a2", "local_ocr", "Сырный комбинат")
    page = _page(layer, ocr, comparisons=[_compared("a1", "a2", 0.2)])
    before = assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=LADDER)
    assert before["disposition"] == "unreviewed_text"
    held = assess_page(page, ocr_layer_climbs=True, corroborate=True, witnesses=True)
    assert (held["selected_attempt"], held["disposition"]) == ("a2", "coverage_defect")
    assert held["next_action"] == "review"
    # With a second OCR stage, the reading goes there; one that agrees settles the page.
    climbs = assess_page(page, corroborate=True, witnesses=True, stages=LADDER)
    assert climbs["next_action"] == "second_ocr"
    page["attempts"].append(_attempt("a3", "second_ocr", "Сырный комбинат"))
    page["comparisons"] += [_compared("a1", "a3", 0.2), _compared("a2", "a3", 0.95)]
    settled = assess_page(page, corroborate=True, witnesses=True, stages=LADDER)
    assert (settled["disposition"], settled["next_action"]) == ("unreviewed_text", "review")


# ---------------------------------------------------------------------------
# When every reading is held (finding 8), and attempts that never finished (finding 16)
# ---------------------------------------------------------------------------


def test_the_reading_that_kept_the_letters_is_shown_when_all_are_held():
    # D05 p3: stock rus kept no pre-reform letters, orus kept 95; both low confidence.
    rus = _attempt("a2", "local_ocr", LOST, warnings=["low_confidence"])
    orus = _attempt("a3", "second_ocr", KEPT, warnings=["low_confidence"])
    page = _page(_attempt("a1", "local_text", " "), rus, orus)
    assert assess_page(page, stages=LADDER)["selected_attempt"] == "a2"
    assert assess_page(page, stages=LADDER, witnesses=True)["selected_attempt"] == "a3"


def test_a_garbled_layer_is_not_shown_before_an_ocr_held_only_for_low_confidence():
    # D03: the garbled LuraDocument layer was selected because it came first.
    layer = _attempt(
        "a1",
        "local_text",
        "Дмошюнз «одст^н* {{ }} [[ ]] || \\\\ ~~",
        warnings=[*LAYER, "suspicious_symbol_density"],
    )
    ocr = _attempt("a2", "local_ocr", "Ревизские сказки крестьян", warnings=["low_confidence"])
    page = _page(layer, ocr)
    assert assess_page(page, ocr_layer_climbs=True)["selected_attempt"] == "a2"
    plain = _page({**layer, "warnings": ["suspicious_symbol_density"]}, ocr)
    assert assess_page(plain)["selected_attempt"] == "a1"
    assert assess_page(plain, witnesses=True)["selected_attempt"] == "a2"


def test_an_attempt_that_never_finished_is_not_a_blank_reading():
    # kill1 p3: the OCR attempt killed mid-page, then its retry.
    killed = _attempt("a2", "local_ocr", "", outcome="outcome_unknown")
    killed["classification"] = {"type": "blank", "reason": "blank_text"}
    retry = _attempt("a3", "local_ocr", "Readable text", warnings=["low_confidence"])
    page = _page(_attempt("a1", "local_text", "Readable text from a layer"), killed, retry)
    before = assess_page(page)
    assert "blank_candidate" in before["review_reasons"]
    after = assess_page(page, witnesses=True)
    assert after["review_reasons"] == ["low_confidence"]


def test_no_climb_is_recorded_toward_a_stage_the_job_does_not_run():
    # D05 with a second OCR: each page counted twice, once toward an image stage it lacks.
    rus = _attempt("a2", "local_ocr", "Readable text")
    orus = _attempt("a3", "second_ocr", "Other words", warnings=["low_confidence"])
    page = _page(_attempt("a1", "local_text", " "), rus, orus)
    ladder = ("local_text", "local_ocr", "second_ocr", "image", "second_opinion")
    configured = ("local_text", "local_ocr", "second_ocr")
    before = assess_page(page, corroborate=True, text_refutes_blank=True, stages=ladder)
    assert [t["stage"] for t in before["triggers"]] == ["local_ocr", "second_ocr"]
    after = assess_page(
        page,
        corroborate=True,
        text_refutes_blank=True,
        stages=ladder,
        witnesses=True,
        configured=configured,
    )
    assert [t["stage"] for t in after["triggers"]] == ["local_ocr"]


# ---------------------------------------------------------------------------
# Jobs: 0.9 is recorded, and a job written under 0.8 rebuilds as it was
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# A typeset layer held only for a few flagged characters (simplicity audit, document A)
# ---------------------------------------------------------------------------

# A page of ordinary French whose layer carries three letters of a foreign script, as a
# LaTeX layer's archive citations did; and the OCR of it in the wrong language.
TYPESET = " ".join(["Les impôts de la Commission furent levés à la tête du district."] * 40)
FLAGGED = TYPESET + " Vseobሀsሀcaja perepis, ሀc. 1, d. 148."
GARBLED = TYPESET.replace("ô", "é").replace("ê", "é")


def _typeset(layer_text=FLAGGED, warnings=("foreign_script_characters",), ratio=0.89):
    layer = _attempt("a1", "local_text", layer_text, warnings=list(warnings))
    ocr = _attempt("a2", "local_ocr", GARBLED)
    return _page(layer, ocr, comparisons=[_compared("a1", "a2", ratio)])


def test_a_typeset_layer_with_a_few_stray_characters_stays_selected_and_held():
    result = assess_page(_typeset(), ocr_layer_climbs=True, corroborate=True, witnesses=True)
    assert result["selected_attempt"] == "a1"
    # Held, so a person sees the flagged characters with the OCR beside them; no climb.
    assert result["disposition"] == "coverage_defect"
    assert result["review_reasons"] == ["coverage_defect"]
    assert result["next_action"] == "review" and result["triggers"] == []
    # Hold policy 0.8 replaced the typeset text with the OCR, as it always had.
    before = assess_page(_typeset(), ocr_layer_climbs=True, corroborate=True)
    assert (before["selected_attempt"], before["disposition"]) == ("a2", "coverage_defect")


def test_a_kept_typeset_layer_that_the_ocr_contradicts_is_held_but_buys_no_reader():
    page = _typeset(ratio=0.3)
    result = assess_page(page, escalate_on=("hold", "disagreement"), stages=LADDER, witnesses=True)
    assert result["selected_attempt"] == "a1"
    assert "engine_disagreement" in result["review_reasons"]
    assert result["next_action"] == "review"


@pytest.mark.parametrize(
    "layer_text,warnings",
    [
        # Flagged characters past 1% of the letters: the layer's map failed, not a citation.
        (TYPESET + " " + "ሀ" * 60, ("foreign_script_characters",)),
        # Any other hold says the layer itself is unusable.
        (FLAGGED, ("foreign_script_characters", "fragmented_text")),
        (FLAGGED, ("foreign_script_characters", "replacement_characters")),
        # An earlier OCR is a witness under the 0.9 rules, not typeset text.
        (FLAGGED, ("foreign_script_characters", "ocr_text_layer")),
        # A warning that does not flag single characters.
        (TYPESET, ("joined_text",)),
    ],
)
def test_a_clean_re_reading_still_replaces_a_layer_that_is_not_merely_flagged(layer_text, warnings):
    result = assess_page(_typeset(layer_text, warnings), witnesses=True)
    assert result["selected_attempt"] == "a2"


def test_a_caron_beside_its_letter_is_not_a_foreign_script():
    # pdfTeX writes "č" in some fonts as a spacing caron then "c".
    cited = TYPESET + " GARF, f. 102, ˇc. 1; Vseobˇsˇcaja perepis; uˇcreˇzdenii."
    assert _content_coverage_metrics(cited, [])["foreign_script_count"] == 0
    assert _content_coverage_metrics(FLAGGED, [])["foreign_script_count"] == 3


def _kiev_job(setup):  # noqa: F811
    setup[3].update(
        defective=set(),
        texts={"local_text": KEPT, "local_ocr": LOST},
        warn={"local_text": LAYER},
    )


def test_a_0_9_job_holds_the_kiev_page_and_verifies(setup):  # noqa: F811
    _kiev_job(setup)
    assert launch(setup, pages="1")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["hold_policy"] == "0.9"
    page = job["pages"][0]
    assert page["selected_attempt"].startswith("local_text")
    assert page["disposition"] == "coverage_defect"
    assert verify_job(setup[2])["status"] == "pass"


def test_a_job_written_under_0_8_rebuilds_and_verifies_unchanged(setup, monkeypatch):  # noqa: F811
    import pageledger.processing as processing_module

    _kiev_job(setup)
    with monkeypatch.context() as patch:
        patch.setattr(processing_module, "HOLD_POLICY", "0.8")
        assert launch(setup, pages="1")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["hold_policy"] == "0.8"
    page = job["pages"][0]
    assert page["selected_attempt"].startswith("local_ocr")
    assert page["disposition"] == "unreviewed_text"
    assert verify_job(setup[2])["status"] == "pass"
