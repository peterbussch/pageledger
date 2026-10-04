"""Shared fixtures for the PageLedger test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

import pageledger
import pageledger.adapters as adapters_module

_CHECKOUT = Path(__file__).resolve().parents[2]
_IMPORTED = Path(pageledger.__file__).resolve()
if not _IMPORTED.is_relative_to(_CHECKOUT):
    # Another checkout's editable install (e.g. ~/Code/pageledger/.venv run
    # from a worktree) would otherwise be tested silently in place of this one.
    raise pytest.UsageError(
        f"tests in {_CHECKOUT} imported pageledger from {_IMPORTED}; "
        "use this checkout's own environment (uv run) or put it first on PYTHONPATH"
    )


@pytest.fixture(autouse=True)
def _clear_adapter_identity_caches():
    """Keep adapter runtime identity caches from leaking between tests.

    Both are keyed on binary paths that mocked tests fake identically, so a
    warm cache would let one test's fake answers satisfy another's.
    """
    adapters_module._tesseract_model_string.cache_clear()
    adapters_module._tesseract_installed_langs.cache_clear()
    adapters_module._pypdf_model_string.cache_clear()
    adapters_module._pdftoppm_model_string.cache_clear()
    yield
    adapters_module._tesseract_model_string.cache_clear()
    adapters_module._tesseract_installed_langs.cache_clear()
    adapters_module._pypdf_model_string.cache_clear()
    adapters_module._pdftoppm_model_string.cache_clear()
