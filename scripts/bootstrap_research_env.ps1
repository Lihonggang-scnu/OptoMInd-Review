# Bootstrap the OptoMind research environment (Windows).
#
# Usage:
#   .\scripts\bootstrap_research_env.ps1
#   .\scripts\bootstrap_research_env.ps1 -WithInstitutionalAccess
#
# Default install does NOT download the Playwright Chromium bundle
# (~400 MB): the institutional_access branch that needs it is disabled by
# default (config/literature_backends.yaml). Pass -WithInstitutionalAccess
# only if you explicitly want that branch.

param(
    [switch]$WithInstitutionalAccess
)

$ErrorActionPreference = "Stop"

Write-Host "== OptoMind research environment bootstrap =="

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsFile = Join-Path $ProjectRoot "requirements-research.txt"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is required. Install it first: https://docs.astral.sh/uv/"
}

Write-Host "[1/4] Creating venv and installing research dependencies..."
uv venv --allow-existing $VenvDir
uv pip install --python $VenvPython -r $RequirementsFile

Write-Host "[2/4] Checking Graphviz executables..."
foreach ($tool in @("dot")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Warning "$tool not found on PATH; some experiment-graph renders may be skipped (non-blocking)."
    }
}

Write-Host "[3/4] Checking TeX toolchain (latexmk / xelatex)..."
$texMissing = @()
foreach ($tool in @("latexmk", "xelatex")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $texMissing += $tool
    }
}
if ($texMissing.Count -gt 0) {
    Write-Warning ("{0} not found; the run will skip PDF compilation and still produce .tex/.md." -f ($texMissing -join ", "))
} else {
    Write-Host "TeX toolchain found; PDF compilation enabled."
}

if ($WithInstitutionalAccess) {
    Write-Host "[4/4] Institutional branch requested: installing Playwright Chromium (~400 MB)..."
    uv pip install --python $VenvPython -e ".[institutional]"
    & (Join-Path $VenvDir "Scripts\playwright.exe") install chromium
    & (Join-Path $VenvDir "Scripts\playwright.exe") --version
} else {
    Write-Host "[4/4] Skipping Playwright Chromium: default OA route does not need it."
    Write-Host "      (Pass -WithInstitutionalAccess to install it.)"
}

Write-Host "Bootstrap complete."
