"""Shared fixtures for the PageLedger test suite."""

from __future__ import annotations

import pytest

import pageledger.adapters as adapters_module


@pytest.fixture(autouse=True)
def _clear_tesseract_caches():
    """Keep tesseract subprocess caches from leaking between tests.

    Both are keyed on binary paths that mocked tests fake identically, so a
    warm cache would let one test's fake answers satisfy another's.
    """
    adapters_module._tesseract_model_string.cache_clear()
    adapters_module._tesseract_installed_langs.cache_clear()
    yield
    adapters_module._tesseract_model_string.cache_clear()
    adapters_module._tesseract_installed_langs.cache_clear()
