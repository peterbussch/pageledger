# Third-party notices

The repository's optional brand source archive,
`brand/pageledger-logo-design.zip`, contains a subset of Archivo Bold and
copies of that subset embedded in SVG assets.

Archivo is copyright 2020 The Archivo Project Authors and is distributed under
the SIL Open Font License, Version 1.1. The archive carries the complete
copyright notice and license at `brand/fonts/OFL.txt`. Upstream source and
license history: <https://github.com/Omnibus-Type/Archivo>.

The `brand/` directory is repository material and is excluded from PageLedger's
wheel and source distribution by `MANIFEST.in`.

## Optional engines

PageLedger does not ship OCR engines or model weights. The optional `indra`
extra (`pip install 'pageledger[indra]'`) installs Datalab's
[indra-ocr](https://github.com/datalab-to/indra) package, whose code is
Apache-2.0. Its model weights, which indra-ocr downloads from
[datalab-to/indra-ocr](https://huggingface.co/datalab-to/indra-ocr) on first
use (about 76 MB, plus the math models with `math: true`), use a modified
OpenRAIL-M licence: free for research, personal use and organizations under
$20M in funding or revenue, and not for use that competes with Datalab's API.
Read the licence before commercial use; `pageledger doctor` repeats this note
when the package is installed.
