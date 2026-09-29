# One-time setup on the SolidWorks PC, and the update step. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# It installs the Python part (the SolidWorks tools), installs OpenCode or updates it to the latest
# release, and configures OpenCode for this folder. Safe to run again at any time.
param([string]$Python = "python", [switch]$Quiet, [switch]$SkipTests)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Test-Python($exe) {
    try {
        & $exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

# ------------------------------------------------------------------ Python part
Write-Host "== Checking Python (3.11 or newer)" -ForegroundColor Cyan
if (-not (Test-Python $Python)) {
    Write-Host "Python 3.11+ was not found."
    $answer = if ($Quiet) { "y" } else { Read-Host "Install Python 3.12 now with winget? [Y/n]" }
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    if (-not (Test-Python $Python)) {
        throw "Install Python 3.11+ from python.org (tick 'Add python.exe to PATH'), then run this again."
    }
}

Write-Host "== Installing the SolidWorks tools (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $Python -m venv .venv }
$py = (Resolve-Path ".venv\Scripts\python.exe").Path
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ------------------------------------------------------------------ OpenCode
function Find-OpenCode {
    $cmd = Get-Command opencode -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @((Join-Path $env:USERPROFILE ".opencode\bin\opencode.exe"), (Join-Path $env:APPDATA "npm\opencode.cmd"))) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Test-Avx2 {
    try {
        $k = Add-Type -MemberDefinition '[DllImport("kernel32.dll")] public static extern bool IsProcessorFeaturePresent(int f);' `
            -Name Kernel32Avx -Namespace SwAssistant -PassThru
        return $k::IsProcessorFeaturePresent(40)
    } catch { return $true }
}

function Install-OpenCodeRelease($version) {
    $bin = Join-Path $env:USERPROFILE ".opencode\bin"
    $asset = if (Test-Avx2) { "opencode-windows-x64.zip" } else { "opencode-windows-x64-baseline.zip" }
    $url = "https://github.com/anomalyco/opencode/releases/download/v$version/$asset"
    $tmp = Join-Path $env:TEMP ("opencode-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp, $bin | Out-Null
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp $asset) -UseBasicParsing
    Expand-Archive -Path (Join-Path $tmp $asset) -DestinationPath $tmp -Force
    $exe = Get-ChildItem -Path $tmp -Recurse -Filter "opencode*.exe" | Select-Object -First 1
    if (-not $exe) { throw "The OpenCode download did not contain opencode.exe." }
    try {
        Copy-Item $exe.FullName (Join-Path $bin "opencode.exe") -Force
    } catch {
        throw "Could not replace opencode.exe (is OpenCode still open?). Close every OpenCode window and the assistant, then run this again."
    }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (($userPath -split ";") -contains $bin)) {
        [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $bin), "User")
    }
    if (-not (($env:Path -split ";") -contains $bin)) { $env:Path = $env:Path + ";" + $bin }
}

Write-Host "== Installing or updating OpenCode" -ForegroundColor Cyan
$latest = $null
try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/anomalyco/opencode/releases/latest" `
        -Headers @{ "User-Agent" = "SolidWorks-Assistant-installer" }
    $latest = $release.tag_name.TrimStart("v")
} catch {
    Write-Warning "Could not check the latest OpenCode version (no internet?): $($_.Exception.Message)"
}
$oc = Find-OpenCode
$current = ""
if ($oc) { try { $current = (& $oc --version 2>$null | Select-Object -First 1).Trim() } catch { $current = "" } }
if (-not $latest) {
    if (-not $oc) { throw "OpenCode is not installed and the latest version could not be downloaded. Check the internet connection, then run this again." }
    Write-Host "Keeping OpenCode $current."
} elseif ($current -eq $latest) {
    Write-Host "OpenCode $current is the latest version."
} elseif ($oc -and $oc -like "*\npm\*") {
    Write-Host "Updating OpenCode $current -> $latest with npm"
    npm install -g "opencode-ai@$latest"
} else {
    if ($current) { Write-Host "Updating OpenCode $current -> $latest" } else { Write-Host "Installing OpenCode $latest" }
    Install-OpenCodeRelease $latest
}
$oc = Find-OpenCode
if (-not $oc) { throw "OpenCode was not found after installing it." }
Write-Host ("OpenCode " + (& $oc --version | Select-Object -First 1) + " at $oc")

Write-Host "== Configuring OpenCode for this folder" -ForegroundColor Cyan
& $py -m sw_agent sync
Write-Host "opencode.json starts the SolidWorks tools; the 'solidworks' agent is in .opencode\agents."

if (-not $SkipTests) {
    Write-Host "== Running unit tests (no SolidWorks needed)" -ForegroundColor Cyan
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { Write-Warning "Some unit tests failed. Please send the output above." }
}

if (-not $Quiet) {
    Write-Host ""
    Write-Host "OpenCode's own free models work without any key." -ForegroundColor Cyan
    $answer = Read-Host "Connect another AI provider now (OpenRouter, OpenAI, Gemini, Groq ...)? [y/N]"
    if ($answer -match "^[Yy]") { & $py -m sw_agent connect }

    $answer = Read-Host "Create desktop shortcuts 'SolidWorks Assistant' and 'SolidWorks Assistant - Tools'? [Y/n]"
    if ($answer -eq "" -or $answer -match "^[Yy]") {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shell = New-Object -ComObject WScript.Shell
        foreach ($item in @(
            @("SolidWorks Assistant", "SolidWorks Assistant.bat", "Build and edit SolidWorks parts by describing them"),
            @("SolidWorks Assistant - Tools", "SolidWorks Assistant - Tools.bat", "OpenCode in a terminal, check SolidWorks, logs, AI providers, update")
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
Write-Host "  - Start the assistant:   double-click 'SolidWorks Assistant' (opens in your browser)"
Write-Host "  - Everything else:       double-click 'SolidWorks Assistant - Tools'"
Write-Host "                           (OpenCode in a terminal, check SolidWorks, logs, AI providers, update)"
