<#
.SYNOPSIS
    Build and verify a Relay release bundle (sdist + wheel) and stage it in
    release\relay-<version>\. Stops before tagging/publishing.

.DESCRIPTION
    Verifies a clean tree on the expected branch, reads the version from
    app\__version__.py, does a clean python -m build, writes SHA256SUMS,
    runs tests\test_packaging.py, installs the wheel into a throwaway
    virtualenv and smoke-tests the installed CLI + /health, then copies the
    artifacts and release documentation into release\relay-<version>\.

    The script NEVER tags, pushes, or publishes. Those are manual steps in
    RELEASE.md.

.PARAMETER Branch
    Expected branch name. Defaults to "master".

.EXAMPLE
    .\release.ps1
.EXAMPLE
    .\release.ps1 -Branch main
#>
[CmdletBinding()]
param(
    [string]$Branch = "master"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Write-Step([string]$Title) {
    Write-Host ""
    Write-Host ("==== {0} ====" -f $Title) -ForegroundColor Cyan
}

function Stop-WithError([string]$Msg) {
    Write-Host ("ERROR: " + $Msg) -ForegroundColor Red
    exit 1
}

Set-Location $Root

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
Write-Step "Preflight"

if (-not (Test-Path "app\__version__.py")) {
    Stop-WithError "app\__version__.py not found; run from the repository root."
}

$gitStatus = (git status --porcelain 2>$null)
if ($gitStatus) {
    Write-Host $gitStatus -ForegroundColor Yellow
    Stop-WithError "Working tree is not clean. Commit or stash changes before releasing."
}

$currentBranch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($currentBranch -ne $Branch) {
    Stop-WithError "On branch '$currentBranch'; expected '$Branch'. Pass -Branch to override."
}
if ($currentBranch -eq "HEAD") {
    Stop-WithError "Detached HEAD; check out the release branch first."
}

$commitFull = (git rev-parse HEAD).Trim()
$commitShort = (git rev-parse --short HEAD).Trim()
Write-Host "branch : $currentBranch"
Write-Host "commit : $commitFull"

$python = "python"
if (Test-Path ".venv\Scripts\python.exe") { $python = ".venv\Scripts\python.exe" }
Write-Host "python : $((& $python --version) 2>&1)"

# ---------------------------------------------------------------------------
# 2. Version
# ---------------------------------------------------------------------------
Write-Step "Version"

$src = Get-Content "app\__version__.py" -Raw
$m = [regex]::Match($src, '__version__\s*=\s*"([^"]+)"')
if (-not $m.Success) {
    Stop-WithError "Could not parse version from app\__version__.py"
}
$version = $m.Groups[1].Value.Trim()
if ($version -notmatch '^[0-9][0-9A-Za-z\.\-]*$') {
    Stop-WithError "Version '$version' is not a valid PEP 440 string."
}
Write-Host "version : $version"

# ---------------------------------------------------------------------------
# 3. Clean build
# ---------------------------------------------------------------------------
Write-Step "Clean build"

foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}
Get-ChildItem -Filter "*.egg-info" -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

& $python -m build --sdist --wheel --outdir dist
if ($LASTEXITCODE -ne 0) { Stop-WithError "python -m build failed (exit $LASTEXITCODE)." }

$whl = Get-ChildItem ("dist\relay-{0}-*.whl" -f $version) -ErrorAction Stop
$sdist = Get-ChildItem ("dist\relay-{0}.tar.gz" -f $version) -ErrorAction Stop
Write-Host ("wheel : {0} ({1:N0} bytes)" -f $whl.Name, $whl.Length)
Write-Host ("sdist : {0} ({1:N0} bytes)" -f $sdist.Name, $sdist.Length)

# ---------------------------------------------------------------------------
# 4. Checksums
# ---------------------------------------------------------------------------
Write-Step "Checksums"

$wheelHash = (Get-FileHash -Algorithm SHA256 $whl.FullName).Hash.ToLower()
$sdistHash = (Get-FileHash -Algorithm SHA256 $sdist.FullName).Hash.ToLower()
@(
    ("{0}  {1}" -f $wheelHash, $whl.Name),
    ("{0}  {1}" -f $sdistHash, $sdist.Name)
) | Set-Content -Path "dist\SHA256SUMS" -Encoding Ascii
Write-Host ("{0}  {1}" -f $wheelHash, $whl.Name)
Write-Host ("{0}  {1}" -f $sdistHash, $sdist.Name)

# ---------------------------------------------------------------------------
# 5. Packaging verification
# ---------------------------------------------------------------------------
Write-Step "Packaging verification (tests\test_packaging.py)"

& $python -m pytest tests\test_packaging.py -q
if ($LASTEXITCODE -ne 0) { Stop-WithError "tests\test_packaging.py failed (exit $LASTEXITCODE)." }

# ---------------------------------------------------------------------------
# 6. Fresh-install smoke (installed wheel, not source)
# ---------------------------------------------------------------------------
if ($env:RELAY_SKIP_SMOKE -eq "1") {
    Write-Host "Skipping fresh-install smoke (RELAY_SKIP_SMOKE=1)." -ForegroundColor Yellow
} else {
    Write-Step "Fresh-install smoke"

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("relay-release-smoke-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        & $python -m venv "$tmp\venv"
        & "$tmp\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
        & "$tmp\venv\Scripts\python.exe" -m pip install --quiet --no-input $whl.FullName
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Wheel install into fresh venv failed (exit $LASTEXITCODE)." }

        $installedVersion = (& "$tmp\venv\Scripts\relay.exe" --version).Trim()
        Write-Host "relay --version : $installedVersion"
        if ($installedVersion -notmatch [regex]::Escape($version)) {
            Stop-WithError "Installed CLI version '$installedVersion' does not match source version '$version'."
        }

        & "$tmp\venv\Scripts\relay.exe" --help | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Installed 'relay --help' failed (exit $LASTEXITCODE)." }

        $port = Get-Random -Minimum 18000 -Maximum 28000
        $oldPort = $env:RELAY_PORT
        $oldState = $env:RELAY_STATE_DIR
        $oldPersist = $env:PERSISTENCE_ENABLED
        $env:RELAY_PORT = "$port"
        $env:RELAY_STATE_DIR = "$tmp\state"
        $env:PERSISTENCE_ENABLED = "false"

        $proc = $null
        try {
            $proc = Start-Process -FilePath "$tmp\venv\Scripts\relay.exe" -ArgumentList "serve" -PassThru -RedirectStandardOutput "$tmp\serve.out.log" -RedirectStandardError "$tmp\serve.err.log"
            $healthy = $false
            for ($i = 0; $i -lt 60; $i++) {
                Start-Sleep -Seconds 1
                if ($proc.HasExited) {
                    Get-Content "$tmp\serve.err.log" -ErrorAction SilentlyContinue | Write-Host
                    Stop-WithError "Installed 'relay serve' exited early (code $($proc.ExitCode))."
                }
                try {
                    $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
                    if ($resp.StatusCode -eq 200) {
                        $healthy = $true
                        Write-Host "GET /health -> $($resp.StatusCode) $($resp.Content)"
                        break
                    }
                } catch { }
            }
            if (-not $healthy) {
                Get-Content "$tmp\serve.err.log" -ErrorAction SilentlyContinue | Write-Host
                Stop-WithError "Installed 'relay serve' /health never returned 200 within 60s."
            }
        } finally {
            if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
            if ($null -eq $oldPort) { Remove-Item Env:RELAY_PORT -ErrorAction SilentlyContinue } else { $env:RELAY_PORT = $oldPort }
            if ($null -eq $oldState) { Remove-Item Env:RELAY_STATE_DIR -ErrorAction SilentlyContinue } else { $env:RELAY_STATE_DIR = $oldState }
            if ($null -eq $oldPersist) { Remove-Item Env:PERSISTENCE_ENABLED -ErrorAction SilentlyContinue } else { $env:PERSISTENCE_ENABLED = $oldPersist }
        }
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 7. Bundle
# ---------------------------------------------------------------------------
Write-Step "Bundle"

$bundle = Join-Path $Root ("release\relay-" + $version)
if (Test-Path $bundle) { Remove-Item -Recurse -Force $bundle }
New-Item -ItemType Directory -Path $bundle | Out-Null

Copy-Item $whl.FullName $bundle
Copy-Item $sdist.FullName $bundle
Copy-Item "dist\SHA256SUMS" $bundle

$docFiles = @(
    "README.md", "RELEASE.md", "CHANGELOG.md", "KNOWN-ISSUES.md",
    "TEST_REPORT_TEMPLATE.md", "BUG_REPORT_TEMPLATE.md",
    "docs\pre-release-checklist.md", "docs\post-install-verification.md",
    "docs\known-limitations.md", "docs\release-decisions.md",
    "docs\rollback-procedure.md"
)
foreach ($doc in $docFiles) {
    if (Test-Path $doc) { Copy-Item $doc $bundle }
}

@(
    ("# Relay {0} release bundle" -f $version),
    "",
    ("Generated:  {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz")),
    ("Branch:     {0}" -f $currentBranch),
    ("Commit:     {0}" -f $commitFull),
    ("Wheel:      {0} (SHA256 {1})" -f $whl.Name, $wheelHash),
    ("Sdist:      {0} (SHA256 {1})" -f $sdist.Name, $sdistHash),
    "",
    "This bundle is READY FOR REVIEW. Tagging, pushing, and publishing are",
    "manual steps described in RELEASE.md."
) | Set-Content -Path (Join-Path $bundle "MANIFEST.txt") -Encoding Ascii

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
Write-Step "Done"
Write-Host "Release bundle : $bundle" -ForegroundColor Green
Write-Host "Version        : $version"
Write-Host "Branch         : $currentBranch"
Write-Host "Commit         : $commitFull"
Write-Host ""
Write-Host "NEXT (manual, not performed): tag 'v$version', push the tag, publish to PyPI." -ForegroundColor Yellow
