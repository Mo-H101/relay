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
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$Prefix\Scripts\python.exe" -m pip install "$Source"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$BinDir = Join-Path $Prefix "Scripts"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Entries = @($UserPath -split ';' | Where-Object { $_ })
if ($Entries -notcontains $BinDir) {
    $NewPath = ($Entries + $BinDir) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "Added '$BinDir' to your user PATH."
} else {
    Write-Host "'$BinDir' is already on your user PATH."
}

Write-Host ""
Write-Host "Installation complete."
Write-Host ""
Write-Host "Relay was added to your user PATH, but this current window will"
Write-Host "not pick it up. Open a NEW terminal (cmd or PowerShell) and type:"
Write-Host ""
Write-Host "    relay"
Write-Host ""
Write-Host "If a new terminal still says 'relay' is not recognized, open one"
Write-Host "run as administrator and set the policy for the current session:"
Write-Host "    Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned"
