# Set up a Vectra-180 development checkout on Windows.
#
# This is for working on the project. To *run* it on a Raspberry Pi, use
# deploy/install-pi.sh instead -- that one installs a service, not a toolchain.

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor White }
function Write-Warn { param([string]$Message) Write-Host " !! $Message" -ForegroundColor Yellow }

function Test-Installed {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

Set-Location -Path $PSScriptRoot

# -- uv ----------------------------------------------------------------------

if (-not (Test-Installed "uv")) {
    Write-Step "Installing uv"
    Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1" | Invoke-Expression
    # The installer edits your user PATH, but this session already read it.
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}

if (-not (Test-Installed "uv")) {
    throw "uv is still not on PATH -- open a new terminal and re-run this"
}

# -- the project -------------------------------------------------------------

Write-Step "Installing dependencies"
uv sync --all-extras
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

Write-Step "Installing the pre-commit hooks"
uv run pre-commit install
if ($LASTEXITCODE -ne 0) { throw "pre-commit install failed" }

# -- optional system tools ---------------------------------------------------

if (-not (Test-Installed "ffmpeg")) {
    Write-Warn "ffmpeg is not installed. Recording falls back to the OpenCV writer: winget install Gyan.FFmpeg"
}

# -- prove it works ----------------------------------------------------------

Write-Step "Running the checks"
uv run ruff check src tests
if ($LASTEXITCODE -ne 0) { throw "ruff found problems" }

uv run mypy src tests
if ($LASTEXITCODE -ne 0) { throw "mypy found problems" }

uv run pytest -q -m "not integration"
if ($LASTEXITCODE -ne 0) { throw "the test suite failed" }

Write-Host ""
Write-Host "Ready." -ForegroundColor Green
Write-Host ""
Write-Host "  uv run vectra180 doctor   check this machine can record"
Write-Host "  uv run vectra180 run      record and serve until stopped"
Write-Host "  uv run vectra180 view     open the desktop control panel"
