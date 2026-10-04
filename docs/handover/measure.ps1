# Report how much taller than one A4 page each handover sheet renders.
#
# render.ps1 only says "1 page" or "2 pages", which is no help while trimming.
# This says by how much. Chrome's headless build here has no working --dump-dom,
# so the page writes its own measured height into document.title and we read it
# back out of the PDF's metadata - the one channel that survives the round trip.
#
# The limit is the A4 printable height implied by @page in sheet.css:
#   297mm - 9mm top - 7mm bottom = 281mm = 1062 CSS px at 96dpi
# and the width the probe forces is the printable width:
#   210mm - 10mm - 10mm = 190mm = 718.11 CSS px

param(
    [string[]] $Sheets = @("01-hardware-setup", "02-load-competition", "03-run-competition")
)

$ErrorActionPreference = "Stop"

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { throw "Chrome not found at $chrome" }

$PAGE_PX  = 1062
$WIDTH_PX = "718.11"

$dir = $PSScriptRoot
$work = Join-Path $env:TEMP ("f3k_measure_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $work | Out-Null
Copy-Item (Join-Path $dir "sheet.css") (Join-Path $work "sheet.css") -Force

$latin1 = [System.Text.Encoding]::GetEncoding(28591)
$probe = '<script>document.body.style.width="' + $WIDTH_PX +
         'px";document.title="F3KH_"+document.body.scrollHeight+"_END";</script>'

foreach ($name in $Sheets) {
    $src = Join-Path $dir "$name.html"
    if (-not (Test-Path $src)) { Write-Host "MISSING  $name.html"; continue }

    $html = Get-Content $src -Raw
    $probed = Join-Path $work "$name.html"
    Set-Content $probed ($html -replace '</body>', "$probe</body>") -Encoding utf8

    $height = -1
    foreach ($attempt in 1..3) {
        $g = [guid]::NewGuid().ToString("N")
        $pdf = Join-Path $work "$g.pdf"
        $url = "file:///" + ($probed -replace '\\', '/')
        & $chrome --headless --disable-gpu --no-pdf-header-footer --no-first-run `
                  --user-data-dir="$work\prof_$g" --print-to-pdf="$pdf" $url 2>$null | Out-Null
        if (Test-Path $pdf) {
            $text = $latin1.GetString([System.IO.File]::ReadAllBytes($pdf))
            $m = [regex]::Match($text, 'F3KH_(\d+)_END')
            if ($m.Success) { $height = [int] $m.Groups[1].Value; break }
        }
        Start-Sleep -Milliseconds 400
    }

    if ($height -lt 0) { Write-Host ("FAIL     {0}" -f $name); continue }

    $over = $height - $PAGE_PX
    $mm = [math]::Round($over / 96 * 25.4, 1)
    $pct = [math]::Round($over / $PAGE_PX * 100, 1)
    if ($over -le 0) {
        Write-Host ("FITS     {0,-22} {1} px, {2} px spare ({3} mm)" -f $name, $height, (-$over), (-$mm))
    } else {
        Write-Host ("OVER     {0,-22} {1} px, over by {2} px ({3} mm, {4}%)" -f $name, $height, $over, $mm, $pct)
    }
}

try { Remove-Item -Recurse -Force $work -ErrorAction Stop } catch {}
