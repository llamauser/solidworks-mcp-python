# One-time setup on the SolidWorks PC. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Safe to run again at any time (after a git pull, or to add AI providers).
param([string]$Python = "python", [switch]$SkipProviders, [switch]$Quiet)

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

if (-not ($SkipProviders -or $Quiet)) {
    Write-Host ""
    Write-Host "== Connect AI model providers" -ForegroundColor Cyan
    & $py -m sw_agent setup
}

if (-not $Quiet) {
    $answer = Read-Host "Create desktop shortcuts 'SolidWorks Assistant' and 'SolidWorks Assistant - Tools'? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shell = New-Object -ComObject WScript.Shell
        foreach ($item in @(
            @("SolidWorks Assistant", "SolidWorks Assistant.bat", "Build and edit SolidWorks parts by describing them"),
            @("SolidWorks Assistant - Tools", "SolidWorks Assistant - Tools.bat", "Check SolidWorks, collect logs, AI providers, update")
        )) {
            $link = $shell.CreateShortcut((Join-Path $desktop ($item[0] + ".lnk")))
            $link.TargetPath = Join-Path $root $item[1]
            $link.WorkingDirectory = $root
            $link.Description = $item[2]
            $link.Save()
        }
        Write-Host "Shortcuts created on your desktop."
    }
}

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  - Start the assistant:   double-click 'SolidWorks Assistant'"
Write-Host "  - Everything else:       double-click 'SolidWorks Assistant - Tools'"
Write-Host "                           (check SolidWorks, collect logs, AI providers, update)"
