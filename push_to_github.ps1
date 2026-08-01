# Pushes this project to a new private GitHub repository.
#
# Prerequisites:
#   1. Install GitHub CLI:  https://cli.github.com/  (winget install GitHub.cli)
#   2. Log in once:         gh auth login
#
# Then run this script:     .\push_to_github.ps1

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "GitHub CLI (gh) is not installed." -ForegroundColor Red
    Write-Host "Install it from https://cli.github.com/ or run: winget install GitHub.cli"
    exit 1
}

$auth = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "You are not logged in to GitHub. Run: gh auth login" -ForegroundColor Red
    Write-Host $auth
    exit 1
}

if (-not (Test-Path .git)) {
    Write-Host "Initialising git repository..."
    git init
}

Write-Host "Creating private repository and pushing..."
gh repo create pdf-invoice-scanner --private --source=. --push

Write-Host ""
Write-Host "Done. Clone it on the other computer with:"
Write-Host "  git clone https://github.com/$((gh api user -q .login))/pdf-invoice-scanner.git"
