"""The pymorphy3 lexicon behind the rough trigger, on text shaped like the October 2026 pages."""

import pytest

pytest.importorskip("pymorphy3")

from pageledger.lexicon import MIN_WORDS, load_lexicon, roughness  # noqa: E402

CONFIG = {"provider": "pymorphy3", "language": "ru", "rough_below": 0.95}

PROSE = (
    "Модель смысл текст была предложена российским лингвистом и развита усилиями "
    "его учеников. Она описывает соответствие между смыслами и текстами естественного "
    "языка и остается особенной для многих прикладных систем и поныне. "
)
# RapidOCR reading two columns as one glued the halves of neighbouring lines.
GLUED = (
    "больдеревьев приховозможных Америтрудных междельным шинстве выгодреализации "
    "прорабо тано языкося двунашим мезнать существосами вавшие чинаются "
)


def test_clean_prose_is_not_rough():
    measure = roughness(PROSE * 2, load_lexicon(CONFIG))
    assert measure["known_share"] >= 0.95
    assert measure["unknown"] == []


def test_glued_column_halves_are_rough():
    measure = roughness(PROSE + GLUED * 2, load_lexicon(CONFIG))
    assert measure["known_share"] < 0.8
    assert "больдеревьев" in measure["unknown"]


def test_words_hyphenated_across_lines_are_rejoined():
    measure = roughness(
        PROSE * 2 + "Обсуждаются черты сход-\nства и различия.", load_lexicon(CONFIG)
    )
    assert "сход" not in measure["unknown"] and "ства" not in measure["unknown"]


def test_a_page_with_few_words_is_not_judged():
    assert roughness("Модели языка. Москва, 1962.", load_lexicon(CONFIG)) is None
    assert MIN_WORDS == 20


def test_the_lexicon_identity_names_its_versions():
    identity = load_lexicon(CONFIG).identity
    assert identity["provider"] == "pymorphy3" and identity["language"] == "ru"
    assert identity["version"] and identity["dictionary"]
