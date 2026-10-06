# Meeting Recorder - Windows installer
# Run from the repo root: .\install_win.ps1
# Requires Python 3.10+ and internet access.

param([switch]$StartNow)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

# --- 1. Check Python ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found. Install Python 3.10+ from https://python.org and re-run."
    exit 1
}
$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pyVersion -lt [version]"3.10") {
    Write-Error "Python $pyVersion found, need 3.10+."
    exit 1
}
Write-Host "Python $pyVersion OK"

# --- 2. Create venv ---
$venvDir = Join-Path $RepoRoot "venv_win"
if (-not (Test-Path $venvDir)) {
    Write-Host "Creating venv..."
    & python -m venv $venvDir
}
$pip = Join-Path $venvDir "Scripts\pip.exe"
$pythonw = Join-Path $venvDir "Scripts\pythonw.exe"

# --- 3. Install dependencies ---
Write-Host "Installing dependencies..."
& $pip install -r (Join-Path $RepoRoot "requirements_win.txt") --quiet
Write-Host "Dependencies installed."

# --- 4. Create .env if missing ---
$envFile = Join-Path $RepoRoot ".env"
$envExample = Join-Path $RepoRoot "config.example.env"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host ""
    Write-Host "Created .env - edit it before first use:"
    Write-Host "  VAULT_PATH=\\wsl.localhost\Ubuntu\home\<user>\Documents\vault"
    Write-Host "  SUMMARIZER_BACKEND=anthropic_api"
    Write-Host "  ANTHROPIC_API_KEY=sk-ant-..."
    Write-Host ""
}

# --- 5. Autostart shortcut ---
$startupDir = [System.Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "meeting-recorder.lnk"

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m recorder.daemon"
$shortcut.WorkingDirectory = $RepoRoot
$shortcut.WindowStyle = 7
$shortcut.Description = "Meeting Recorder daemon"
$shortcut.Save()
Write-Host "Autostart shortcut created: $shortcutPath"

# --- 6. Optionally start now ---
if ($StartNow) {
    Write-Host "Starting daemon..."
    Start-Process $pythonw "-m recorder.daemon" -WorkingDirectory $RepoRoot -WindowStyle Hidden
    Write-Host "Daemon started. Tray icon should appear shortly."
}

Write-Host ""
Write-Host "Done. Hotkeys: Ctrl+Shift+R = start | Ctrl+Shift+S = stop + transcribe"
