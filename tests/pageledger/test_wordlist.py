"""A plain word list as the lexicon, for the rough trigger and contest in any language."""

import hashlib
import unicodedata

import pytest

from pageledger.lexicon import load_lexicon, roughness
from pageledger.processing_config import processing_config

PROSE = (
    "The committee met in the spring and agreed that the society would publish "
    "its proceedings every year, with an index of members and their addresses. "
)
WORDS = sorted({word.strip(".,").lower() for word in PROSE.split()} | {"café", "naïve"})
# Two columns read as one glue the halves of neighbouring lines.
GLUED = "commitociety pubtee proceemeet agreings addrespring yeasses indmembex "


@pytest.fixture
def wordlist(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    return path


def _lexicon(path):
    return load_lexicon({"provider": "wordlist", "path": str(path)})


def test_clean_prose_is_not_rough_and_glued_halves_are(wordlist):
    lexicon = _lexicon(wordlist)
    assert roughness(PROSE * 2, lexicon)["known_share"] == 1.0
    glued = roughness(PROSE + GLUED * 2, lexicon)
    assert glued["known_share"] < 0.8
    assert "commitociety" in glued["unknown"]


def test_words_match_in_any_case_and_any_unicode_form(tmp_path):
    path = tmp_path / "words.txt"
    # The list spells café with a combining accent and in capitals; the page does not.
    path.write_text("CAFÉ\nStraße\n", encoding="utf-8")
    lexicon = _lexicon(path)
    assert lexicon.known("café") and lexicon.known("Café") and lexicon.known("CAFÉ")
    assert lexicon.known(unicodedata.normalize("NFD", "café"))
    assert lexicon.known("STRASSE")
    assert not lexicon.known("cafe")


def test_words_hyphenated_across_lines_are_rejoined_and_capitals_are_not(wordlist):
    lexicon = _lexicon(wordlist)
    measure = roughness(PROSE * 2 + "The pro-\nceedings of the Anglo-\nSaxon society.", lexicon)
    assert "pro" not in measure["unknown"] and "ceedings" not in measure["unknown"]
    assert "Anglo" in measure["unknown"] and "Saxon" in measure["unknown"]


def test_a_page_with_few_words_is_not_judged(wordlist):
    assert roughness("The society met.", _lexicon(wordlist)) is None


def test_the_identity_names_the_lists_hash_size_and_matching(wordlist):
    identity = _lexicon(wordlist).identity
    data = wordlist.read_bytes()
    assert identity == {
        "provider": "wordlist",
        "path": str(wordlist.resolve()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "words": len(WORDS),
        "match": "nfc-casefold",
    }


def test_a_changed_list_has_another_identity(wordlist):
    before = _lexicon(wordlist)
    wordlist.write_text(wordlist.read_text(encoding="utf-8") + "zebra\n", encoding="utf-8")
    after = _lexicon(wordlist)
    assert after.identity["sha256"] != before.identity["sha256"]
    assert after.known("zebra") and not before.known("zebra")


def test_blank_lines_a_byte_order_mark_and_repeats_are_not_words(tmp_path):
    path = tmp_path / "words.txt"
    path.write_bytes("﻿apple\n\n  apple  \nApple\npear\n".encode())
    lexicon = _lexicon(path)
    assert lexicon.identity["words"] == 2
    assert lexicon.known("apple") and lexicon.known("pear")


@pytest.mark.parametrize(
    "content,message",
    [(None, "cannot read the word list"), (b"\xff\xfe\x00", "not UTF-8"), (b"\n \n", "no words")],
)
def test_an_unusable_list_is_refused(tmp_path, content, message):
    path = tmp_path / "words.txt"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ValueError, match=message):
        _lexicon(path)


def _config(**processing):
    return {
        "processing": {
            "local_text": {"adapter": "pdf_text"},
            "local_ocr": {"adapter": "pdf_ocr"},
            "limits": {"max_image_pages": 0},
            **processing,
        }
    }


def test_the_config_names_a_word_list_by_path():
    lexicon = {"provider": "wordlist", "path": "words.txt", "rough_below": 0.9}
    config = processing_config(_config(escalate_on=["hold", "rough"], lexicon=lexicon), pdf=True)
    assert config["lexicon"] == lexicon


@pytest.mark.parametrize(
    "lexicon",
    [
        {"provider": "wordlist", "rough_below": 0.9},
        {"provider": "wordlist", "path": "", "rough_below": 0.9},
        {"provider": "wordlist", "path": "words.txt", "language": "en", "rough_below": 0.9},
        {"provider": "wordlist", "path": ["words.txt"], "rough_below": 0.9},
        {"provider": "pymorphy3", "path": "words.txt", "rough_below": 0.9},
    ],
)
def test_a_malformed_word_list_config_is_refused(lexicon):
    with pytest.raises(ValueError, match="provider: wordlist with the path"):
        processing_config(_config(escalate_on=["hold", "rough"], lexicon=lexicon), pdf=True)
