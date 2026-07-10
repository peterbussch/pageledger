#!/usr/bin/env bash
set -euo pipefail

# Split each two-page PDF spread into left and right PNG files.
#
# The output files use names such as page_0007_left.png and
# page_0007_right.png. They are new pages, not the original PDF pages. Keep
# this filename mapping with any later PageLedger run so a derived page can
# still be traced to the source spread.
#
# qpdf can first split the source into one-page PDF files for inspection:
#   qpdf --split-pages input.pdf spread-%d.pdf
# qpdf does not crop a page into halves. The pdftoppm -x and -W options below
# perform the actual pixel crop.

input_pdf="${1:?usage: split_spreads.sh input.pdf output_dir [dpi]}"
output_dir="${2:?usage: split_spreads.sh input.pdf output_dir [dpi]}"
dpi="${3:-300}"

for command in pdfinfo pdftoppm awk; do
  command -v "$command" >/dev/null || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

[[ "$dpi" =~ ^[1-9][0-9]*$ ]] || {
  echo "dpi must be a positive integer" >&2
  exit 1
}

mkdir -p "$output_dir"
pages="$(pdfinfo "$input_pdf" | awk '/^Pages:/ {print $2; exit}')"
[[ "$pages" =~ ^[1-9][0-9]*$ ]] || {
  echo "could not read page count from $input_pdf" >&2
  exit 1
}

for ((page = 1; page <= pages; page++)); do
  width_points="$(
    pdfinfo -f "$page" -l "$page" "$input_pdf" |
      awk '/size:/ {
        for (i = 1; i <= NF; i++) {
          if ($i == "size:") {
            print $(i + 1)
            exit
          }
        }
      }'
  )"
  [[ "$width_points" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
    echo "could not read width for source page $page" >&2
    exit 1
  }

  width_px="$(awk -v points="$width_points" -v dpi="$dpi" \
    'BEGIN {printf "%d", (points * dpi / 72) + 0.5}')"
  left_width=$((width_px / 2))
  right_width=$((width_px - left_width))
  printf -v page_id "%04d" "$page"

  pdftoppm -f "$page" -l "$page" -r "$dpi" -x 0 -W "$left_width" \
    -singlefile -png "$input_pdf" "$output_dir/page_${page_id}_left"
  pdftoppm -f "$page" -l "$page" -r "$dpi" -x "$left_width" -W "$right_width" \
    -singlefile -png "$input_pdf" "$output_dir/page_${page_id}_right"
done
