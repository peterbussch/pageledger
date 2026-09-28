# Tutorial verification

The executable blocks in `README.md`, `docs/first-run.md`, and
`docs/pdf-ocr-first-run.md` use the tag `bash pageledger-tutorial`. Run them with
`examples/run_first_run.py` from a source checkout. The helper runs the blocks
in one fresh working directory and uses the selected Python interpreter for
PageLedger commands.

The PDF/OCR CI journey prepares a fixture PDF before the tutorial is executed.
The executable blocks are the same ones readers use; CI provides the PDF and
installs Poppler and Tesseract.

The document-job tutorial has a source-checkout test helper for simulated
recovery. It is not a standalone wheel tutorial.
