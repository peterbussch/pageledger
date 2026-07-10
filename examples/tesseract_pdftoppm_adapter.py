"""Compatibility import for the former Tesseract/pdftoppm example.

PageLedger now ships this adapter as ``pdf_ocr``. Existing configs that name
``tesseract_pdftoppm_adapter:TesseractPdftoppmAdapter`` keep working while new
configs should use ``run.adapter: pdf_ocr`` directly.
"""

from pageledger.adapters import PdfOcrAdapter

TesseractPdftoppmAdapter = PdfOcrAdapter
