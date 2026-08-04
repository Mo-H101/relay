# Relay one-command installer (Windows PowerShell)
#
# Usage:
#   .\install.ps1                        # install from the local checkout
#   .\install.ps1 -Source <pip-source>   # e.g. a GitHub URL or PyPI name
#   .\install.ps1 -Prefix <dir>          # where the venv is created
#
# Once Relay is published to PyPI, `pip install relay` is the primary
# path; this script is a convenience wrapper around that same pip flow.

param(
    [string]$Source = "",
    [string]$Prefix = "$env:USERPROFILE\.relay"
)

$ErrorActionPreference = "Stop"

if (-not $Source) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "pyproject.toml")) {
        $Source = $PSScriptRoot
    }
}

if (-not $Source) {
    Write-Error "No install source. Pass -Source <pip-source> or run inside the Relay checkout."
}

$Py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

& $Py -m venv "$Prefix"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& "$Prefix\Scripts\python.exe" -m pip install --upgrade pip
& "$Prefix\Scripts\python.exe" -m pip install "$Source"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$BinDir = Join-Path $Prefix "Scripts"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Entries = @($UserPath -split ';' | Where-Object { $_ })
if ($Entries -notcontains $BinDir) {
    $NewPath = ($Entries + $BinDir) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "Added '$BinDir' to your user PATH."
}

Write-Host ""
Write-Host "Installation complete. Type 'relay' to start Relay."
