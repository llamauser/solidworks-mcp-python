# One-time setup on the SolidWorks PC. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host "== Checking Python" -ForegroundColor Cyan
& $Python -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10 or newer is required'; print(sys.version)"
if ($LASTEXITCODE -ne 0) { throw "Install Python 3.10+ from python.org (tick 'Add python.exe to PATH')." }

Write-Host "== Creating .venv and installing sw_mcp" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $Python -m venv .venv }
$py = (Resolve-Path ".venv\Scripts\python.exe").Path
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "== Writing opencode.json with this PC's Python path" -ForegroundColor Cyan
$escaped = $py.Replace('\', '\\')
$json = @"
{
  "`$schema": "https://opencode.ai/config.json",
  "mcp": {
    "solidworks": {
      "type": "local",
      "command": ["$escaped", "-m", "sw_mcp"],
      "enabled": true,
      "timeout": 10000
    }
  },
  "agent": {
    "build": {
      "permission": {
        "bash": "deny",
        "edit": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "task": "deny",
        "solidworks_*": "allow"
      }
    }
  }
}
"@
[System.IO.File]::WriteAllText((Join-Path $root "opencode.json"), $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "== Running unit tests (no SolidWorks needed)" -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Warning "Some unit tests failed. Please send the output above." }

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  1. Open SolidWorks (or let the smoke test start it)."
Write-Host "  2. Run the end-to-end check:   .venv\Scripts\python scripts\smoke_test.py"
Write-Host "     and send back smoke_test_report.txt"
Write-Host "  3. Start OpenCode in this folder:   opencode"
Write-Host "     Use the default 'build' agent (in this folder it is locked to the SolidWorks tools), pick a model, and ask:"
Write-Host "     'What version of SolidWorks is running?'"
