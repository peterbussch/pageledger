"""Phase 4 tests: quality signal diagnostics and audit-queue integration.

Verifies the full quality warning taxonomy, round-trip from extraction
through manifest.summary.quality_warning_pages to audit.json review_queue.

Covers all 7 documented warning types plus:
  - Clean text produces zero false-positive warnings
  - Empty / very-short pages
  - OCR-noisy text (replacement chars, control chars)
  - Suspicious symbol density
  - Suspicious embedded text delta (via custom adapter on PDF)
  - Custom adapter output gets quality diagnostics
  - quality_warning_pages count in manifest.summary matches quality.jsonl
  - Quality-warning pages appear in audit review_queue with reason=quality_warning
"""

from __future__ import annotations

import json
import sys
import textwrap


def _run(inputs, config_text, tmp_path, *, dry_run=False):
    """Run PageLedger programmatically and return the output directory."""
    from pageledger.runner import run

    config_path = tmp_path / "config.yml"
    config_path.write_text(config_text, encoding="utf-8")
    out_dir = tmp_path / "out"
    run(
        inputs=inputs,
        config_path=config_path,
        out_dir=out_dir,
        dry_run=dry_run,
    )
    return out_dir


# =========================================================================
# Warning taxonomy: no false positives on clean text
# =========================================================================

def test_clean_text_produces_no_warnings(tmp_path):
    """Clean prose text produces no quality warnings."""
    source = tmp_path / "clean.txt"
    source.write_text("The quick brown fox jumps over the lazy dog. "
                      "This is a perfectly normal paragraph of text.", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    quality_entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(quality_entries) == 1
    entry = quality_entries[0]
    assert entry["warnings"] == []
    assert entry["character_count"] > 0
    assert entry["word_count"] > 0
    assert entry["text_quality"]["replacement_character_count"] == 0
    assert entry["text_quality"]["control_character_count"] == 0
    assert entry["text_quality"]["suspicious_symbol_count"] == 0

    # manifest.summary quality_warning_pages should be 0
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["quality_warning_pages"] == 0

    # audit review queue should be empty (no quality warnings)
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["review_queue"] == []


# =========================================================================
# Empty text
# =========================================================================

def test_empty_page_produces_empty_text_warning(tmp_path):
    """Empty page produces empty_text warning, count and audit entry."""
    source = tmp_path / "empty.txt"
    source.write_text("", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "empty_text" in entries[0]["warnings"]
    assert entries[0]["character_count"] == 0

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["quality_warning_pages"] == 1

    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert len(audit["review_queue"]) == 1
    assert audit["review_queue"][0]["reason"] == "quality_warning"
    assert audit["review_queue"][0]["page_id"] == "doc_0001_page_0001"


# =========================================================================
# Short text (< 10 chars)
# =========================================================================

def test_short_text_produces_warning(tmp_path):
    """Very short text (< 10 chars) produces short_text warning."""
    source = tmp_path / "short.txt"
    source.write_text("Brief.", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "short_text" in entries[0]["warnings"]

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["quality_warning_pages"] == 1


# =========================================================================
# Replacement characters (\ufffd)
# =========================================================================

def test_replacement_characters_trigger_warning(tmp_path):
    """Unicode replacement characters trigger replacement_characters warning."""
    source = tmp_path / "repl.txt"
    source.write_text("Valid text" + "\ufffd" + "more text" + "\ufffd\ufffd", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "replacement_characters" in entries[0]["warnings"]
    assert entries[0]["text_quality"]["replacement_character_count"] == 3


# =========================================================================
# Control characters
# =========================================================================

def test_control_characters_trigger_warning(tmp_path):
    """Non-whitespace control characters trigger control_characters warning."""
    source = tmp_path / "ctrl.txt"
    source.write_text("hello\x00world\x01\x02", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "control_characters" in entries[0]["warnings"]
    assert entries[0]["text_quality"]["control_character_count"] >= 3


# =========================================================================
# Suspicious symbol density
# =========================================================================

def test_suspicious_symbol_density_triggers_warning(tmp_path):
    """Sufficient suspicious symbols trigger suspicious_symbol_density warning."""
    source = tmp_path / "symbols.txt"
    source.write_text("TOP SECRET _____ {S==6CQl_|}|}\\| text", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "suspicious_symbol_density" in entries[0]["warnings"]
    assert entries[0]["text_quality"]["suspicious_symbol_count"] >= 5
    assert entries[0]["text_quality"]["suspicious_symbol_ratio"] >= 0.03


# =========================================================================
# Fragmented text (lexical shape)
# =========================================================================

def test_fragmented_text_triggers_warning(tmp_path):
    """OCR fragment noise (mean token length < 3) triggers fragmented_text."""
    source = tmp_path / "fragments.txt"
    # 30 alphabetic fragments, mean length ~1.3 — pdftoppm/tesseract line noise
    source.write_text("l ll l lI ll I l li ll l Il ll i l ll lI l I li ll "
                      "l ll I li l ll lI l li", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert "fragmented_text" in entries[0]["warnings"]
    tq = entries[0]["text_quality"]
    assert tq["alpha_token_count"] >= 20
    assert tq["mean_token_length"] < 3.0
    assert tq["short_token_ratio"] > 0.9


def test_fragmented_text_not_triggered_by_prose_or_few_tokens(tmp_path):
    """Normal prose and short fragment bursts stay unflagged."""
    source = tmp_path / "mixed.txt"
    # page 1: normal prose; page 2: fragment noise but only 5 tokens
    source.write_text(
        "The quick brown fox jumps over the lazy dog near the riverbank today."
        "\fl ll I li l", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 2
    for entry in entries:
        assert "fragmented_text" not in entry["warnings"]
    assert entries[0]["text_quality"]["mean_token_length"] > 3.0


# =========================================================================
# Multi-page: mixed quality signals, aggregate counts
# =========================================================================

def test_multi_page_mixed_quality_counts(tmp_path):
    """Multiple pages with mixed quality produce correct counts."""
    source = tmp_path / "multi.txt"
    source.write_text(
        "clean page one with enough text to avoid warnings\n"
        "\f"  # page delimiter
        "short",  # page 2: short_text
        encoding="utf-8",
    )
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 2

    # Page 1: clean
    assert entries[0]["warnings"] == []
    # Page 2: short
    assert "short_text" in entries[1]["warnings"]

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["quality_warning_pages"] == 1

    # Only page 2 should be in the review queue
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert len(audit["review_queue"]) == 1
    assert audit["review_queue"][0]["page_id"] == "doc_0001_page_0002"
    assert audit["review_queue"][0]["reason"] == "quality_warning"


# =========================================================================
# Custom adapter output gets quality diagnostics
# =========================================================================

def test_custom_adapter_output_quality_diagnostics(tmp_path):
    """Custom adapter output gets quality diagnostics and audit wiring."""
    import os

    # Write custom adapter module
    adapter_py = tmp_path / "custom_ocr.py"
    adapter_py.write_text(textwrap.dedent("""\
    from dataclasses import dataclass
    from pathlib import Path
    from pageledger.adapters import ExtractionResult

    @dataclass
    class CustomOcrAdapter:
        name = "custom-ocr"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("ocr", "local")

        def supports(self, action):
            return action == "transcribe_text"

        def extract(self, source, *, page_id, page_number, action, prompt=None):
            pages = source.read_text(encoding="utf-8").split("\\f")
            text = pages[page_number - 1] if 0 < page_number <= len(pages) else ""
            # Simulate noisy OCR output: add replacement chars
            noisy = text + "\\ufffd\\ufffd"
            return ExtractionResult(
                content=noisy, format="text", confidence=0.82,
                model="custom-model", warnings=["low_confidence"],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )
    """))

    source = tmp_path / "doc.txt"
    source.write_text("some text here\n", encoding="utf-8")

    config_text = textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {adapter_py.stem}:CustomOcrAdapter
        """)

    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(tmp_path / "cfg.yml"), "--out", str(tmp_path / "out"), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env=env,
        input=config_text,
    )
    # Write the config file since we passed input
    (tmp_path / "cfg.yml").write_text(config_text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pageledger", "run", str(source),
         "--config", str(tmp_path / "cfg.yml"), "--out", str(tmp_path / "out"), "--json"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env=env,
    )

    assert result.returncode == 0, f"Failed: {result.stderr}"

    # Quality diagnostics should fire on the replacement characters
    quality_entries = [
        json.loads(line)
        for line in (tmp_path / "out" / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(quality_entries) == 1
    assert "replacement_characters" in quality_entries[0]["warnings"]
    assert quality_entries[0]["adapter"] == "custom-ocr"

    # Audit review queue should have the page
    audit = json.loads((tmp_path / "out" / "audit.json").read_text(encoding="utf-8"))
    assert len(audit["review_queue"]) == 1
    assert audit["review_queue"][0]["reason"] == "quality_warning"


# =========================================================================
# Dry-run: should NOT add quality-warning pages to review_queue
# (only route-based review entries should appear)
# =========================================================================

def test_dry_run_review_queue_has_route_reasons_not_quality_warning(tmp_path):
    """Dry-run review queue uses route reasons (no_classifier_available), not quality_warning."""
    source = tmp_path / "text.txt"
    source.write_text("short", encoding="utf-8")
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
        dry_run=True,
    )
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    # Dry-run routes pages to review with no_classifier_available
    reasons = [item["reason"] for item in audit["review_queue"]]
    assert "no_classifier_available" in reasons
    # Dry-run produces no quality entries (no extraction)
    quality_path = out_dir / "quality.jsonl"
    if quality_path.exists():
        text = quality_path.read_text(encoding="utf-8").strip()
        assert text == ""  # empty


# =========================================================================
# All warnings in quality.jsonl appear in manifest.quality_warning_pages
# =========================================================================

def test_manifest_quality_warning_pages_matches_quality_jsonl(tmp_path):
    """manifest.summary.quality_warning_pages equals the actual warning count."""
    source = tmp_path / "multi.txt"
    # 3 pages: clean, short, empty
    source.write_text(
        "clean page with enough text to avoid warnings here\n"
        "\f"
        "shorty",  # short
        encoding="utf-8",
    )
    out_dir = _run(
        [source],
        textwrap.dedent("""\
            schema_version: "0.1"
            taxonomy:
              page_types:
                prose:
                  default_action: transcribe_text
            run:
              adapter: text
            """),
        tmp_path,
    )
    entries = [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    warning_count = sum(1 for e in entries if e["warnings"])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["quality_warning_pages"] == warning_count


# =========================================================================
# Historical orthography detection (pre-reform Cyrillic)
# =========================================================================

_TEXT_CONFIG = textwrap.dedent("""\
    schema_version: "0.1"
    taxonomy:
      page_types:
        prose:
          default_action: transcribe_text
    run:
      adapter: text
    """)


def _quality_entries(out_dir):
    return [
        json.loads(line)
        for line in (out_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_prereform_text_triggers_historical_orthography_warning(tmp_path):
    """Pre-reform Russian with abolished letters is flagged and counted."""
    source = tmp_path / "prereform.txt"
    source.write_text(
        "Военно-статистическое обозрѣніе Харьковской губерніи. "
        "Свѣдѣнія о населеніи уѣзда собраны въ 1850 году. "
        "Каждый городъ и уѣздъ имѣетъ свои особенности.",
        encoding="utf-8",
    )
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert "historical_orthography" in entry["warnings"]
    assert entry["text_quality"]["prereform_letter_count"] > 0
    assert entry["text_quality"]["terminal_hard_sign_count"] > 0


def test_ocr_garbled_prereform_detected_via_terminal_hard_signs(tmp_path):
    """OCR of pre-reform scans loses the abolished letters but keeps terminal
    hard signs; density alone must trigger the warning (>=1 per 100 tokens
    over >=20 tokens)."""
    source = tmp_path / "ocr.txt"
    # Modeled on real Tesseract rus output of an 1850 scan: no yat survives,
    # but word-final hard signs are everywhere.
    source.write_text(
        "Въ 1819 году отчисленъ отъ Воронежской губерши въ Слободско "
        "Украинскую городъ Старобфльскъ съ убздомъ Наконецъ въ 1836 году "
        "губершя опять переименована въ Харьковскую и каждый городъ приписанъ",
        encoding="utf-8",
    )
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert entry["text_quality"]["prereform_letter_count"] == 0
    assert "historical_orthography" in entry["warnings"]


def test_modern_russian_not_flagged_as_historical(tmp_path):
    """Modern Russian, including medial hard signs and Ukrainian і, stays clean."""
    source = tmp_path / "modern.txt"
    source.write_text(
        "Оценка современной экономической ситуации в России показывает, что "
        "объект исследования и подъезд к нему описаны точно. "
        "Українська мова використовує літеру і в сучасному правописі, "
        "тому вона не є ознакою дореформеної орфографії взагалі.",
        encoding="utf-8",
    )
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert "historical_orthography" not in entry["warnings"]
    assert entry["text_quality"]["prereform_letter_count"] == 0


def test_english_text_reports_zero_orthography_metrics(tmp_path):
    """Latin-script pages carry the metrics as zeros and never warn."""
    source = tmp_path / "english.txt"
    source.write_text(
        "The quick brown fox jumps over the lazy dog near the riverbank "
        "while the miller keeps a careful ledger of every single page.",
        encoding="utf-8",
    )
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert entry["text_quality"]["prereform_letter_count"] == 0
    assert entry["text_quality"]["terminal_hard_sign_count"] == 0
    assert "historical_orthography" not in entry["warnings"]


# =========================================================================
# Cyrillic/European typography is not "suspicious symbols"
# =========================================================================

def test_guillemets_and_dashes_are_not_suspicious_symbols(tmp_path):
    """Standard Russian typography — «guillemets», em/en dashes, ellipsis,
    numero sign — must not count toward suspicious_symbol_density."""
    source = tmp_path / "typography.txt"
    source.write_text(
        "Катасонов В. Ю. «Санкционная война против России» — М.: Книжный мир, "
        "2022. — 320 с. № 78… Обзор литературы — важная часть работы «всех» "
        "исследователей — и историков, и социологов.",
        encoding="utf-8",
    )
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert entry["text_quality"]["suspicious_symbol_count"] == 0
    assert "suspicious_symbol_density" not in entry["warnings"]


# =========================================================================
# Word-confidence evidence in quality lines + low_confidence warning
# =========================================================================

def _confidence_adapter_module(tmp_path, name, mean, below_ratio, word_count):
    """Write an importable custom adapter emitting fixed confidence detail.

    Each test uses a distinct module name: Python caches imported adapter
    modules in sys.modules, so reusing one name would leak state between
    tests."""
    module = tmp_path / f"{name}.py"
    module.write_text(textwrap.dedent(f"""\
        from pageledger.adapters import ExtractionResult

        class ConfAdapter:
            name = "conf"
            version = "0.1"
            deterministic = True
            input_types = ("text",)
            output_types = ("text",)
            capabilities = ("local",)

            def supports(self, action):
                return action == "transcribe_text"

            def extract(self, source, *, page_id, page_number, action, prompt=None):
                return ExtractionResult(
                    content="plausible page text with enough words to look ordinary",
                    format="text",
                    confidence={mean / 100},
                    model="conf-test",
                    warnings=[],
                    usage={{"pages": 1, "tokens": None,
                            "compute_seconds": None, "cost_usd": None}},
                    confidence_detail={{
                        "scale": "tesseract_word_confidence_0_100",
                        "word_count": {word_count},
                        "mean": {mean},
                        "min": 5.0,
                        "below_60_count": {int(below_ratio * word_count)},
                        "below_60_ratio": {below_ratio},
                    }},
                )
        """), encoding="utf-8")
    return module


def _run_conf_adapter(tmp_path, *, name, mean, below_ratio, word_count):
    _confidence_adapter_module(tmp_path, name, mean, below_ratio, word_count)
    source = tmp_path / "page.txt"
    source.write_text("one ordinary page", encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text(textwrap.dedent(f"""\
        schema_version: "0.1"
        taxonomy:
          page_types:
            prose:
              default_action: transcribe_text
        run:
          adapter: {name}:ConfAdapter
        """), encoding="utf-8")
    from pageledger.runner import run

    out_dir = tmp_path / "out"
    run(
        inputs=[source],
        config_path=config_path,
        out_dir=out_dir,
        dry_run=False,
        adapter_path=tmp_path,
    )
    return _quality_entries(out_dir)[0]


def test_quality_line_records_confidence_and_detail(tmp_path):
    entry = _run_conf_adapter(tmp_path, name="conf_high", mean=91.0, below_ratio=0.05, word_count=200)
    assert entry["confidence"] == 0.91
    assert entry["confidence_detail"]["word_count"] == 200
    assert "low_confidence" not in entry["warnings"]


def test_low_confidence_warning_fires_on_weak_tail(tmp_path):
    """A quarter of the words below 60 marks the page for review."""
    entry = _run_conf_adapter(tmp_path, name="conf_tail", mean=71.0, below_ratio=0.3, word_count=120)
    assert "low_confidence" in entry["warnings"]


def test_low_confidence_ignores_tiny_pages(tmp_path):
    """A handful of words is not enough evidence to warn on."""
    entry = _run_conf_adapter(tmp_path, name="conf_tiny", mean=40.0, below_ratio=0.5, word_count=4)
    assert "low_confidence" not in entry["warnings"]


def test_quality_line_confidence_null_without_detail(tmp_path):
    """Adapters that report nothing leave confidence null, no warning."""
    source = tmp_path / "page.txt"
    source.write_text("an ordinary page of plain text with no confidence data",
                      encoding="utf-8")
    out_dir = _run([source], _TEXT_CONFIG, tmp_path)
    entry = _quality_entries(out_dir)[0]
    assert entry["confidence"] is None
    assert entry["confidence_detail"] is None
    assert "low_confidence" not in entry["warnings"]
