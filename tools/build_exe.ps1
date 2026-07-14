# build_exe.ps1 — Build F3KSync.exe from gs_sync.py using PyInstaller.
#
# Run from the project root:
#   .\tools\build_exe.ps1
#
# After a successful build, deploy the exe to the Pi:
#   scp dist\F3KSync.exe f3kpi:~/f3k_base/downloads/F3KSync.exe
#
# The base station then serves it at http://<pi>/downloads/F3KSync.exe

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Set-Location $ProjectRoot

# Ensure PyInstaller is available
if (-not (pip show pyinstaller 2>$null)) {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

# Clean previous build artefacts
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist\F3KSync.exe") { Remove-Item -Force "dist\F3KSync.exe" }
if (Test-Path "F3KSync.spec") { Remove-Item -Force "F3KSync.spec" }

Write-Host "Building F3KSync.exe..."
pyinstaller `
    --onefile `
    --noconsole `
    --name F3KSync `
    tools\gs_sync.py

if (-not (Test-Path "dist\F3KSync.exe")) {
    Write-Error "Build failed — dist\F3KSync.exe not found."
    exit 1
}

$size = [math]::Round((Get-Item "dist\F3KSync.exe").Length / 1MB, 1)
Write-Host ""
Write-Host ("Build complete: dist\F3KSync.exe (" + $size + " MB)")
Write-Host ""
Write-Host "Deploy to Pi:"
Write-Host "  ssh f3kpi mkdir -p ~/f3k_base/downloads"
Write-Host "  scp dist\F3KSync.exe f3kpi:~/f3k_base/downloads/F3KSync.exe"
