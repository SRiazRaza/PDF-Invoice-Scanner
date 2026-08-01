# One-time setup for a new Windows machine.
# Installs Git and Python 3.12 (via winget when possible), clones the project
# and installs its dependencies. Run in PowerShell with:
#
#   powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1
#
# Optional: pass a destination folder, e.g.
#   powershell -ExecutionPolicy Bypass -File .\setup_other_computer.ps1 -Dest D:\InvoiceScanner
#
# NOTE: Python 3.13 / 3.14 are NOT supported by the OCR engine
# (rapidocr-onnxruntime needs Python 3.10 - 3.12). If the machine already has
# a newer Python, this script installs 3.12 alongside it and uses it for the
# project.

param(
    [string]$Dest = ""
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== PDF Invoice Scanner - one-time setup ===" -ForegroundColor Cyan
Write-Host ""

# Helper: run a Python command given either "python" or "py -3.12"
function Invoke-Py {
    param([string]$Cand, [string]$Code)
    $parts = $Cand -split " "
    if ($parts.Count -eq 1) {
        return & $parts[0] -c $Code 2>$null
    }
    return & $parts[0] $parts[1] -c $Code 2>$null
}

# 1) Git -------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[OK] Git is already installed." -ForegroundColor Green
} else {
    Write-Host "Installing Git via winget ..."
    try {
        winget install --id Git.Git -e --source winget `
            --accept-package-agreements --accept-source-agreements
    } catch {
        Write-Host "winget failed to install Git. Install it manually from" -ForegroundColor Yellow
        Write-Host "https://git-scm.com/download/win and run this script again." -ForegroundColor Yellow
    }
}

# 2) Python (needs 3.10 - 3.12) ---------------------------------------------
$py = ""
foreach ($cand in @("python", "py -3.12", "py -3.11", "py -3.10")) {
    if ($cand -eq "python" -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
        continue
    }
    if ($cand -like "py*" -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
        continue
    }
    $ver = Invoke-Py $cand "import sys; print('%d.%d' % sys.version_info[:2])"
    if ($LASTEXITCODE -eq 0 -and $ver -match "^\d+\.\d+$") {
        $major, $minor = $ver.Split(".")
        if ([int]$major -eq 3 -and [int]$minor -le 12) {
            $py = $cand
            break
        }
    }
}

if ($py) {
    Write-Host "[OK] Compatible Python found: $py" -ForegroundColor Green
} else {
    Write-Host "No compatible Python found (this project needs 3.10 - 3.12)." -ForegroundColor Yellow
    Write-Host "Installing Python 3.12 via winget ..."
    try {
        winget install --id Python.Python.3.12 -e --source winget `
            --accept-package-agreements --accept-source-agreements
    } catch {
        Write-Host "winget failed. Install Python 3.12 manually from" -ForegroundColor Yellow
        Write-Host "https://www.python.org/downloads/ (tick 'Add python.exe to PATH')," -ForegroundColor Yellow
        Write-Host "then open a NEW PowerShell window and run this script again." -ForegroundColor Yellow
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $py = "py -3.12"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $py = "python"
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git is not available. Install it from https://git-scm.com/download/win" -ForegroundColor Red
    Write-Host "then open a new PowerShell window and run this script again."
    exit 1
}

if (-not $py) {
    Write-Host "Python 3.10 - 3.12 is not available. See the messages above." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Installed versions:" -ForegroundColor Yellow
git --version
Invoke-Py $py "import sys; print('Python', sys.version)"

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
Invoke-Py $py "-m pip install -r requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dependency install failed. Try again with:" -ForegroundColor Yellow
    Write-Host "  $py -m pip install pymupdf rapidocr-onnxruntime"
    exit 1
}

Write-Host ""
Write-Host "Setup finished!" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Put your PDF invoices into:  $Dest"
Write-Host "  2. Run:"
Write-Host "       cd '$Dest'"
Write-Host "       $py invoice_extractor.py --input . --output invoices_extracted.csv"
Write-Host "  3. Results are written to invoices_extracted.csv in that folder."
