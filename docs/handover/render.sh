#!/usr/bin/env bash
# Render the handover sheets to PDF and report the page count of each.
#
#   ./render.sh                     # render all three and count pages
#   ./render.sh --count-only        # count the PDFs already on disk
#   ./render.sh 02-load-competition # one sheet
#
# A headless browser's PDF path is the only renderer available, and it is flaky
# when several instances share a profile directory - so each render gets its own
# throwaway profile and up to three attempts. A sheet that reports anything
# other than "1 page" is a bug in the sheet, not in this script: they are
# one-pagers.
#
# Linux counterpart of render.ps1, added 2026-10-17.
#
# THE RENDER HALF IS UNVERIFIED ON THIS MACHINE: there is no Chrome or Chromium
# installed on the Fedora laptop, so nothing here has driven a browser. Install
# one (`sudo dnf install chromium`) and the first run is a test of this script
# as much as of the sheets. `--count-only` IS verified, against the PDFs already
# committed in this directory.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

COUNT_ONLY=0
# An explicit counter rather than testing ${#SHEETS[@]}: under `set -u` an
# array with no elements still counts as unset in this bash, so the length
# test aborts the script before it can apply the defaults.
SHEETS=()
nsheets=0

while [ $# -gt 0 ]; do
    case "$1" in
        --count-only) COUNT_ONLY=1; shift ;;
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) printf 'Unknown option: %s\n' "$1" >&2; exit 1 ;;
        *) SHEETS+=("$1"); nsheets=$((nsheets + 1)); shift ;;
    esac
done

if [ "$nsheets" -eq 0 ]; then
    SHEETS=(01-hardware-setup 02-load-competition 03-run-competition)
fi

# --- find a browser -------------------------------------------------------
# CHROME overrides, the same shape as TSHARK_PATH elsewhere. Unlike the
# Windows original there is no single blessed install path on Linux, so this
# searches rather than hard-coding one.
find_chrome() {
    if [ -n "${CHROME:-}" ]; then
        [ -x "$CHROME" ] && { printf '%s\n' "$CHROME"; return 0; }
        printf 'CHROME is set to %s, which is not executable.\n' "$CHROME" >&2
        return 1
    fi
    local c
    for c in google-chrome google-chrome-stable chromium chromium-browser \
             chromium-freeworld microsoft-edge; do
        if command -v "$c" >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

# Only needed when actually rendering: --count-only must still work on a box
# with no browser at all, which is the whole reason it is a separate mode.
CHROME_BIN=''
if [ "$COUNT_ONLY" -ne 1 ]; then
    if ! CHROME_BIN="$(find_chrome)"; then
        printf 'No Chrome or Chromium found. Either install one:\n' >&2
        printf '    sudo dnf install chromium\n' >&2
        printf 'or point CHROME at a binary, or use --count-only to read the\n' >&2
        printf 'PDFs already in this directory.\n' >&2
        exit 1
    fi
fi

# --- page count -----------------------------------------------------------
# The page tree's /Count, read straight out of the file. grep -a because a PDF
# is binary and grep would otherwise just say "binary file matches"; the
# PowerShell original decoded the bytes as latin-1 for the same reason, which
# is a roundabout way of saying "do not touch the high bytes".
#
# head -1 matters: /Count appears once per page-tree node, and the FIRST is the
# root. A nested tree would otherwise report a subtree's count.
pdf_pages() {
    local path="$1" n
    n=$(grep -ao '/Count[[:space:]]*[0-9]\+' "$path" 2>/dev/null \
          | head -1 | grep -o '[0-9]\+' || true)
    printf '%s\n' "${n:--1}"
}

exit_code=0

for name in "${SHEETS[@]}"; do
    html="$HERE/$name.html"
    pdf="$HERE/$name.pdf"

    if [ ! -f "$html" ]; then
        printf 'MISSING  %s.html\n' "$name"
        exit_code=1
        continue
    fi

    ok=0
    for attempt in 1 2 3; do
        profile=$(mktemp -d "${TMPDIR:-/tmp}/f3k_pdf_XXXXXXXX")
        if [ "$COUNT_ONLY" -ne 1 ]; then
            "$CHROME_BIN" --headless --disable-gpu --no-pdf-header-footer \
                --no-first-run --user-data-dir="$profile" \
                --print-to-pdf="$pdf" "file://$html" >/dev/null 2>&1
        fi
        rm -rf "$profile"
        if [ -f "$pdf" ]; then ok=1; break; fi
        sleep 0.4
    done

    if [ "$ok" -ne 1 ]; then
        printf 'FAIL     %s - the browser produced no PDF\n' "$name"
        exit_code=1
        continue
    fi

    pages=$(pdf_pages "$pdf")
    kb=$(awk -v b="$(stat -c%s "$pdf")" 'BEGIN{ printf "%.1f", b/1024 }')
    if [ "$pages" = '1' ]; then
        printf 'OK       %-22s 1 page   %s KB\n' "$name" "$kb"
    else
        printf 'OVERFLOW %-22s %s pages %s KB\n' "$name" "$pages" "$kb"
        exit_code=1
    fi
done

exit "$exit_code"
