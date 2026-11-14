# Tutorial verification

The executable blocks in `README.md`, `docs/first-run.md`,
`docs/document-first-run.md` and `docs/pdf-ocr-first-run.md` use the tag `bash pageledger-tutorial`. Run them with
`examples/run_first_run.py` from a source checkout. The helper runs the blocks
in one fresh working directory and uses the selected Python interpreter for
PageLedger commands.

The PDF/OCR CI journey prepares a fixture PDF before the tutorial is executed.
The executable blocks are the same ones readers use; CI provides the PDF and
installs Poppler and Tesseract.

`docs/document-first-run.md` needs only an installed PageLedger, except its
last section, which is marked for maintainers: those two blocks use the
source-checkout helper `examples/run_document_recovery.py` (simulated
interruption) and hand-written review files, and CI runs them with the rest.
