# Install loci as a CLI tool on Windows.
# Requires Python 3.12+. Run in PowerShell:
#   irm https://raw.githubusercontent.com/loci-knowledge/loci/main/install.ps1 | iex
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Package = "loci-wiki"
$MinMajor = 3; $MinMinor = 12

function Write-Info { Write-Host "=> $args" -ForegroundColor Green }
function Write-Err  { Write-Host "error: $args" -ForegroundColor Red; exit 1 }

# Find a suitable Python
function Find-Python {
    foreach ($cmd in @("python3.13","python3.12","python3","python","py")) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $cmd -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            $parts = $ver.Split(".")
            $maj = [int]$parts[0]; $min = [int]$parts[1]
            if ($maj -gt $MinMajor -or ($maj -eq $MinMajor -and $min -ge $MinMinor)) {
                return $cmd
            }
        } catch {}
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Err "Python $MinMajor.$MinMinor+ not found. Install from https://python.org/downloads/ and re-run."
}
$pyVer = & $Python --version 2>&1
Write-Info "Using $pyVer"

# uv tool install (preferred)
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Info "Installing via uv tool install..."
    & uv tool install $Package
    Write-Info "Done! Run: loci --help"
    exit 0
}

# pipx
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    Write-Info "Installing via pipx..."
    & pipx install $Package
    Write-Info "Done! Run: loci --help"
    exit 0
}

# pip --user fallback
Write-Info "Installing via pip (no uv or pipx found)..."
& $Python -m pip install --user --upgrade $Package
Write-Info "Done! Run: loci --help"
Write-Host ""
Write-Host "If 'loci' is not found, add Python's Scripts folder to PATH:" -ForegroundColor Yellow
Write-Host "  %APPDATA%\Python\Python3xx\Scripts" -ForegroundColor Yellow
