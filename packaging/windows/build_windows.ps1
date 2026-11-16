# Build the Mini Search desktop app for Windows (PowerShell).
#
# Produces, in packaging\windows\dist\:
#   * MiniSearch-<version>-windows-x64.zip  – portable build (run mini-search.exe)
#   * MiniSearch-<version>-windows-x64-setup.exe – installer (only when Inno Setup
#     `iscc` is available; the portable zip is always produced)
#
# This must run on Windows: PyInstaller cannot cross-compile a Windows binary
# from Linux. The GitHub Actions workflow (.github/workflows/build-release.yml)
# runs it on a windows-latest runner.
#
# Usage:  pwsh -File packaging/windows/build_windows.ps1 -Version 7.9
param(
    [string]$Version = "7.9"
)
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $Here "..\..")
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
New-Item -ItemType Directory -Force -Path $Build, $Dist | Out-Null

Write-Host "==> Kontroluji Python"
python --version
python -m pip install --upgrade pip
# Same light profile as Linux/macOS: no torch / sentence-transformers.
python -m pip install -r (Join-Path $Root "requirements-mobile.txt")
python -m pip install "pyinstaller>=6.0"

Write-Host "==> PyInstaller"
python -m PyInstaller --clean --noconfirm `
    --distpath (Join-Path $Build "pyinstaller-dist") `
    --workpath (Join-Path $Build "pyinstaller-work") `
    (Join-Path $Root "packaging\minisearch.spec")

$Bundle = Join-Path $Build "pyinstaller-dist\mini-search"
if (-not (Test-Path (Join-Path $Bundle "mini-search.exe"))) {
    throw "PyInstaller bundle not found: $Bundle"
}

Write-Host "==> Portable zip"
$Portable = Join-Path $Dist "MiniSearch-$Version-windows-x64.zip"
if (Test-Path $Portable) { Remove-Item $Portable }
Compress-Archive -Path (Join-Path $Bundle "*") -DestinationPath $Portable

Write-Host "==> Installer (Inno Setup, optional)"
$Iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($Iscc) {
    & $Iscc "/DAppVersion=$Version" (Join-Path $Here "installer.iss")
} else {
    Write-Host "iscc (Inno Setup) nenalezen - preskakuji instalator, zip je hotovy."
}

Write-Host ""
Write-Host "==> Hotovo"
Get-ChildItem $Dist
