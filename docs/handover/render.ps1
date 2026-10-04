# Render the handover sheets to PDF and report the page count of each.
#
# Chrome's headless PDF path is the only renderer on this box, and it is flaky
# when several instances share a profile directory - so each render gets its own
# throwaway profile and up to three attempts. A sheet that reports anything other
# than "1 page" is a bug in the sheet, not in this script: they are one-pagers.

param(
    [string[]] $Sheets = @("01-hardware-setup", "02-load-competition", "03-run-competition"),
    [switch]   $CountOnly
)

$ErrorActionPreference = "Stop"

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { throw "Chrome not found at $chrome" }

$dir = $PSScriptRoot
$latin1 = [System.Text.Encoding]::GetEncoding(28591)

function Get-PdfPageCount([string] $path) {
    $text = $latin1.GetString([System.IO.File]::ReadAllBytes($path))
    $m = [regex]::Match($text, '/Count\s+(\d+)')
    if ($m.Success) { return [int] $m.Groups[1].Value }
    return -1
}

$exit = 0

foreach ($name in $Sheets) {
    $html = Join-Path $dir "$name.html"
    $pdf  = Join-Path $dir "$name.pdf"
    if (-not (Test-Path $html)) { Write-Host "MISSING  $name.html"; $exit = 1; continue }

    $ok = $false
    foreach ($attempt in 1..3) {
        $profile = Join-Path $env:TEMP ("f3k_pdf_" + [guid]::NewGuid().ToString("N"))
        $url = "file:///" + ($html -replace '\\', '/')
        if (-not $CountOnly) {
            & $chrome --headless --disable-gpu --no-pdf-header-footer --no-first-run `
                      --user-data-dir="$profile" --print-to-pdf="$pdf" $url 2>$null | Out-Null
        }
        try { Remove-Item -Recurse -Force $profile -ErrorAction Stop } catch {}
        if (Test-Path $pdf) { $ok = $true; break }
        Start-Sleep -Milliseconds 400
    }

    if (-not $ok) { Write-Host "FAIL     $name - Chrome produced no PDF"; $exit = 1; continue }

    $pages = Get-PdfPageCount $pdf
    $kb = [math]::Round((Get-Item $pdf).Length / 1kb, 1)
    if ($pages -eq 1) {
        Write-Host ("OK       {0,-22} 1 page   {1} KB" -f $name, $kb)
    } else {
        Write-Host ("OVERFLOW {0,-22} {1} pages {2} KB" -f $name, $pages, $kb)
        $exit = 1
    }
}

exit $exit
