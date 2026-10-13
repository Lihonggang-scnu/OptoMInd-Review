[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$alignmentRoot = Split-Path -Parent $PSScriptRoot
$resultsRoot = Join-Path $PSScriptRoot "results"
$null = New-Item -ItemType Directory -Force -Path $resultsRoot
$null = New-Item -ItemType Directory -Force -Path (Join-Path $resultsRoot "mpl-cache")

$reviewPython = "D:\OptoMind-Article-3\local-preparation\envs\review\Scripts\python.exe"
$articlePython = "D:\OptoMind-Article-3\local-integration\20261013\envs\tensor\Scripts\python.exe"
$bridgePython = Join-Path $alignmentRoot "bridge-venv\Scripts\python.exe"
$reviewRepo = "D:\OptoMind-Article-3\repos\OptoMInd-Review"
$articleRepo = "D:\OptoMind-Article-3\repos\OptoMind-Article"

$records = [ordered]@{}

function Invoke-PythonProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Python,
        [string]$Code,
        [string[]]$Arguments = @()
    )

    $previousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    $previousMplBackend = [Environment]::GetEnvironmentVariable("MPLBACKEND", "Process")
    $previousMplConfig = [Environment]::GetEnvironmentVariable("MPLCONFIGDIR", "Process")
    $previousErrorPreference = $ErrorActionPreference
    $previousLocation = Get-Location
    try {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $env:MPLBACKEND = "Agg"
        $env:MPLCONFIGDIR = Join-Path $resultsRoot "mpl-cache"
        Set-Location $env:SystemRoot
        $argv = @("-B")
        if (-not [string]::IsNullOrEmpty($Code)) {
            $probePath = Join-Path $resultsRoot "$Name-probe.py"
            [IO.File]::WriteAllText($probePath, $Code, [Text.UTF8Encoding]::new($false))
            $argv += $probePath
        }
        if ($Arguments.Count -gt 0) {
            $argv += $Arguments
        }
        $ErrorActionPreference = "Continue"
        $lines = @(& $Python @argv 2>&1 | ForEach-Object { [string]$_ })
        $exitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorPreference
        $output = ($lines -join [Environment]::NewLine)
        [IO.File]::WriteAllText(
            (Join-Path $resultsRoot "$Name.txt"),
            $output + [Environment]::NewLine + "exit_code=$exitCode" + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        $records[$Name] = [ordered]@{
            python = $Python
            exit_code = $exitCode
            output_file = "$Name.txt"
        }
    } finally {
        Set-Location $previousLocation
        $ErrorActionPreference = $previousErrorPreference
        if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPythonPath }
        if ($null -eq $previousMplBackend) { Remove-Item Env:MPLBACKEND -ErrorAction SilentlyContinue } else { $env:MPLBACKEND = $previousMplBackend }
        if ($null -eq $previousMplConfig) { Remove-Item Env:MPLCONFIGDIR -ErrorAction SilentlyContinue } else { $env:MPLCONFIGDIR = $previousMplConfig }
    }
}

function Invoke-UvAudit {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Python
    )

    $previousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    $previousErrorPreference = $ErrorActionPreference
    $previousLocation = Get-Location
    try {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        Set-Location $env:SystemRoot
        $ErrorActionPreference = "Continue"
        $checkLines = @(& uv pip check --python $Python 2>&1 | ForEach-Object { [string]$_ })
        $checkExit = $LASTEXITCODE
        $freezeLines = @(& uv pip freeze --python $Python 2>&1 | ForEach-Object { [string]$_ })
        $freezeExit = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorPreference
        [IO.File]::WriteAllText((Join-Path $resultsRoot "$Name-pip-check.txt"), (($checkLines -join [Environment]::NewLine) + [Environment]::NewLine + "exit_code=$checkExit" + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $resultsRoot "$Name-freeze.txt"), (($freezeLines -join [Environment]::NewLine) + [Environment]::NewLine + "exit_code=$freezeExit" + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
        $records["$Name-uv"] = [ordered]@{
            python = $Python
            pip_check_exit_code = $checkExit
            freeze_exit_code = $freezeExit
            pip_check_file = "$Name-pip-check.txt"
            freeze_file = "$Name-freeze.txt"
        }
    } finally {
        Set-Location $previousLocation
        $ErrorActionPreference = $previousErrorPreference
        if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPythonPath }
    }
}

$reviewImportCode = @'
import importlib.metadata as md, json, sys
import matplotlib, openai, yaml, json_repair, requests, optomind_research, config
mods = {
    "matplotlib": matplotlib.__file__, "openai": openai.__file__,
    "yaml": yaml.__file__, "json_repair": json_repair.__file__,
    "requests": requests.__file__, "optomind_research": optomind_research.__file__,
    "config": config.__file__,
}
names = {name: md.version(name) for name in ("agentscope", "json-repair", "matplotlib", "openai", "PyYAML", "requests")}
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "modules": mods, "versions": names, "optomind_distributions": dist}, sort_keys=True))
'@
$articleImportCode = @'
import importlib.metadata as md, json, sys
import torch, matplotlib, openai, yaml, requests, json_repair, optomind_optics, tmm_engine
mods = {
    "torch": torch.__file__, "matplotlib": matplotlib.__file__, "openai": openai.__file__,
    "yaml": yaml.__file__, "requests": requests.__file__, "json_repair": json_repair.__file__,
    "optomind_optics": optomind_optics.__file__, "tmm_engine": tmm_engine.__file__,
}
names = {name: md.version(name) for name in ("torch", "json-repair", "matplotlib", "openai", "PyYAML", "requests")}
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "modules": mods, "versions": names, "optomind_distributions": dist}, sort_keys=True))
'@
$bridgeImportCode = @'
import importlib.metadata as md, importlib.util, json, pathlib, sys
import optomind_optics, optomind_research, tmm_engine
path = pathlib.Path(r"D:\OptoMind-Article-3\repos\OptoMInd-Review\optomind_research\runtime\upgrade3\module4\runtime.py")
spec = importlib.util.spec_from_file_location("optomind_m4_runtime_bridge", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
dist = [d.metadata["Name"] for d in md.distributions() if (d.metadata.get("Name") or "").lower() == "optomind"]
print(json.dumps({"python": sys.version, "runtime": str(path), "loaded": module.__file__, "article": optomind_optics.__file__, "review": optomind_research.__file__, "tmm": tmm_engine.__file__, "optomind_distributions": dist}, sort_keys=True))
'@

Invoke-PythonProbe -Name "review-imports" -Python $reviewPython -Code $reviewImportCode
Invoke-PythonProbe -Name "review-harness-help" -Python $reviewPython -Arguments @((Join-Path $reviewRepo "run_review_harness.py"), "--help")
Invoke-PythonProbe -Name "review-quickstart-help" -Python $reviewPython -Arguments @((Join-Path $reviewRepo "quickstart.py"), "--help")
Invoke-PythonProbe -Name "article-tensor-imports" -Python $articlePython -Code $articleImportCode
Invoke-PythonProbe -Name "article-tmm-help" -Python $articlePython -Arguments @("-m", "tmm_engine.cli", "--help")
Invoke-PythonProbe -Name "article-lifetime-help" -Python $articlePython -Arguments @("-m", "optomind_optics.harness.science.lifetime_cli", "--help")

if (Test-Path $bridgePython) {
    Invoke-PythonProbe -Name "bridge-imports" -Python $bridgePython -Code $bridgeImportCode
    Invoke-UvAudit -Name "bridge" -Python $bridgePython
} else {
    $records["bridge-imports"] = [ordered]@{ status = "not_created" }
}

Invoke-UvAudit -Name "review" -Python $reviewPython
Invoke-UvAudit -Name "article-tensor" -Python $articlePython

$summary = [ordered]@{
    generated_utc = [DateTime]::UtcNow.ToString("o")
    working_directory = $env:SystemRoot
    pythonpath_cleared_for_children = $true
    records = $records
}
[IO.File]::WriteAllText((Join-Path $resultsRoot "summary.json"), ($summary | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
Write-Output (Join-Path $resultsRoot "summary.json")
$failed = @(
    $records.GetEnumerator() |
        Where-Object {
            ($null -ne $_.Value.exit_code -and $_.Value.exit_code -ne 0) -or
            ($null -ne $_.Value.pip_check_exit_code -and $_.Value.pip_check_exit_code -ne 0) -or
            ($null -ne $_.Value.freeze_exit_code -and $_.Value.freeze_exit_code -ne 0)
        } |
        ForEach-Object { $_.Key }
)
if ($failed.Count -gt 0) {
    [Console]::Error.WriteLine("runtime alignment failures: " + ($failed -join ", "))
    exit 1
}
exit 0
