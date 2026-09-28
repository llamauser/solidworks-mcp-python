# One-time setup on the SolidWorks PC. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Safe to run again at any time (after a git pull, or to add AI providers).
param([string]$Python = "python", [switch]$SkipProviders)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-Python($exe) {
    try {
        & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

Write-Host "== Checking Python (3.11 or newer)" -ForegroundColor Cyan
if (-not (Test-Python $Python)) {
    Write-Host "Python 3.11+ was not found."
    $answer = Read-Host "Install Python 3.12 now with winget? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        winget install -e --id Python.Python.3.12 --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    if (-not (Test-Python $Python)) {
        throw "Install Python 3.11+ from python.org (tick 'Add python.exe to PATH'), then run this again."
    }
}

Write-Host "== Creating .venv and installing" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $Python -m venv .venv }
$py = (Resolve-Path ".venv\Scripts\python.exe").Path
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "== Running unit tests (no SolidWorks needed)" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Warning "Some unit tests failed. Please send the output above." }

if (-not $SkipProviders) {
    Write-Host ""
    Write-Host "== Connect AI model providers" -ForegroundColor Cyan
    & $py -m sw_agent setup
}

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  - Check SolidWorks works:      .venv\Scripts\python scripts\smoke_test.py"
Write-Host "  - See your AI providers:       .venv\Scripts\sw-agent status"
Write-Host "  - Add more providers later:    .venv\Scripts\sw-agent setup"
Write-Host "  - Use it from OpenCode, VS Code Copilot, Gemini CLI or Claude Desktop:"
Write-Host "                                 .venv\Scripts\sw-agent connect"
