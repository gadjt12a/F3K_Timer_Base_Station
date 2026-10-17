#!/usr/bin/env bash
# Report how much taller than one A4 page each handover sheet renders.
#
#   ./measure.sh
#   ./measure.sh 02-load-competition
#
# render.sh only says "1 page" or "2 pages", which is no help while trimming.
# This says by how much. The headless browser here has no working --dump-dom,
# so the page writes its own measured height into document.title and we read it
# back out of the PDF's metadata - the one channel that survives the round trip.
#
# The limit is the A4 printable height implied by @page in sheet.css:
#   297mm - 9mm top - 7mm bottom = 281mm = 1062 CSS px at 96dpi
# and the width the probe forces is the printable width:
#   210mm - 10mm - 10mm = 190mm = 718.11 CSS px
#
# Linux counterpart of measure.ps1, added 2026-10-17.
#
# UNVERIFIED ON THIS MACHINE: there is no Chrome or Chromium on the Fedora
# laptop, so this has never driven a browser here. Everything except the render
# itself is shared with render.sh, whose --count-only path IS tested.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# An explicit counter rather than testing ${#SHEETS[@]}: under `set -u` an
# array with no elements still counts as unset in this bash.
SHEETS=()
nsheets=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
        *) SHEETS+=("$1"); nsheets=$((nsheets + 1)); shift ;;
    esac
done
if [ "$nsheets" -eq 0 ]; then
    SHEETS=(01-hardware-setup 02-load-competition 03-run-competition)
fi

PAGE_PX=1062
WIDTH_PX='718.11'

# CHROME overrides; otherwise search, since Linux has no single blessed path.
if [ -n "${CHROME:-}" ] && [ -x "${CHROME}" ]; then
    CHROME_BIN="$CHROME"
else
    CHROME_BIN=''
    for c in google-chrome google-chrome-stable chromium chromium-browser \
             chromium-freeworld microsoft-edge; do
        if command -v "$c" >/dev/null 2>&1; then CHROME_BIN="$(command -v "$c")"; break; fi
    done
fi
if [ -z "$CHROME_BIN" ]; then
    printf 'No Chrome or Chromium found. Install one:\n' >&2
    printf '    sudo dnf install chromium\n' >&2
    printf 'or point CHROME at a binary.\n' >&2
    exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/f3k_measure_XXXXXXXX")
# A trap, not a tidy-up at the end: the original leaked its temp directory on
# any throw, and this script has three early exits.
trap 'rm -rf "$work"' EXIT

cp "$HERE/sheet.css" "$work/sheet.css"

probe='<script>document.body.style.width="'"$WIDTH_PX"'px";document.title="F3KH_"+document.body.scrollHeight+"_END";</script>'

for name in "${SHEETS[@]}"; do
    src="$HERE/$name.html"
    if [ ! -f "$src" ]; then
        printf 'MISSING  %s.html\n' "$name"
        continue
    fi

    probed="$work/$name.html"
    # The probe goes immediately before </body> so scrollHeight is measured
    # with the whole document laid out. Only the LAST </body> is replaced;
    # awk rather than sed because the probe contains both slashes and
    # ampersands, which sed would treat as a delimiter and a backreference.
    awk -v p="$probe" '
        { lines[NR] = $0 }
        /<\/body>/ { last = NR }
        END {
            for (i = 1; i <= NR; i++) {
                if (i == last) {
                    n = index(lines[i], "</body>")
                    printf "%s%s%s\n", substr(lines[i], 1, n - 1), p, substr(lines[i], n)
                } else {
                    print lines[i]
                }
            }
        }
    ' "$src" > "$probed"

    height=-1
    for attempt in 1 2 3; do
        g=$(mktemp -u XXXXXXXX)
        pdf="$work/$g.pdf"
        "$CHROME_BIN" --headless --disable-gpu --no-pdf-header-footer \
            --no-first-run --user-data-dir="$work/prof_$g" \
            --print-to-pdf="$pdf" "file://$probed" >/dev/null 2>&1
        if [ -f "$pdf" ]; then
            # grep -a: the title is plain text in the PDF but the file is
            # binary, so grep would otherwise refuse to print the match.
            h=$(grep -ao 'F3KH_[0-9]\+_END' "$pdf" 2>/dev/null \
                  | head -1 | grep -o '[0-9]\+' || true)
            if [ -n "$h" ]; then height="$h"; break; fi
        fi
        sleep 0.4
    done

    if [ "$height" -lt 0 ]; then
        printf 'FAIL     %s\n' "$name"
        continue
    fi

    over=$((height - PAGE_PX))
    if [ "$over" -le 0 ]; then
        spare=$((-over))
        mm=$(awk -v o="$spare" 'BEGIN{ printf "%.1f", o/96*25.4 }')
        printf 'FITS     %-22s %s px, %s px spare (%s mm)\n' "$name" "$height" "$spare" "$mm"
    else
        mm=$(awk -v o="$over" 'BEGIN{ printf "%.1f", o/96*25.4 }')
        pct=$(awk -v o="$over" -v p="$PAGE_PX" 'BEGIN{ printf "%.1f", o/p*100 }')
        printf 'OVER     %-22s %s px, over by %s px (%s mm, %s%%)\n' \
            "$name" "$height" "$over" "$mm" "$pct"
    fi
done
