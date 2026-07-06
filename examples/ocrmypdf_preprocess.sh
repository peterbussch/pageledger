#!/usr/bin/env bash
set -euo pipefail

input_pdf="${1:?usage: ocrmypdf_preprocess.sh input.pdf output.pdf}"
output_pdf="${2:?usage: ocrmypdf_preprocess.sh input.pdf output.pdf}"

ocrmypdf --skip-text --output-type pdf "$input_pdf" "$output_pdf"
