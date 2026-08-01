# One-time setup for a new Windows machine.
# Installs Git and Python (via winget), clones the project and installs its
# dependencies. Run in PowerShell with:
#
#   powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1
#
# Optional: pass a destination folder, e.g.
#   powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1 -Dest D:\InvoiceScanner

param(
    [string]$Dest = ""
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== PDF Invoice Scanner - one-time setup ===" -ForegroundColor Cyan
Write-Host ""

# 1) Git -------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[OK] Git is already installed." -ForegroundColor Green
} else {
    Write-Host "Installing Git via winget ..."
    winget install --id Git.Git -e --source winget `
        --accept-package-agreements --accept-source-agreements
}

# 2) Python -----------------------------------------------------------------
if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "[OK] Python is already installed." -ForegroundColor Green
} else {
    Write-Host "Installing Python via winget ..."
    winget install --id Python.Python.3.12 -e --source winget `
        --accept-package-agreements --accept-source-agreements
}

# Refresh PATH so tools installed just now are visible in this session.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

Write-Host ""
Write-Host "Installed versions:" -ForegroundColor Yellow
git --version
if (Get-Command python -ErrorAction SilentlyContinue) {
    python --version
} else {
    py -3 --version
}

# 3) Clone the project -------------------------------------------------------
if (-not $Dest) {
    $Dest = Join-Path (Get-Location) "PDF-Invoice-Scanner"
}

if (Test-Path (Join-Path $Dest "invoice_extractor.py")) {
    Write-Host "[OK] Project already cloned at $Dest" -ForegroundColor Green
} else {
    Write-Host "Cloning project to $Dest ..."
    git clone https://github.com/SRiazRaza/PDF-Invoice-Scanner.git $Dest
}

Set-Location $Dest

# 4) Dependencies -------------------------------------------------------------
Write-Host "Installing Python dependencies ..."
if (Get-Command python -ErrorAction SilentlyContinue) {
    python -m pip install -r requirements.txt
} else {
    py -3 -m pip install -r requirements.txt
}

Write-Host ""
Write-Host "Setup finished!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Put your PDF invoices into:  $Dest"
Write-Host "  2. Run:"
Write-Host "       cd '$Dest'"
Write-Host "       python invoice_extractor.py --input . --output invoices_extracted.csv"
Write-Host "  3. Results are written to invoices_extracted.csv in that folder."
