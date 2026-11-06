param(
    [string]$Name = ""
)

$ErrorActionPreference = "Stop"
$Python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found. Create .\venv and install .[dev] in this folder."
}
$PreviousExecutableName = $env:ED_FAST_TELEMETRY_EXE_NAME
$PreviousPythonPath = $env:PYTHONPATH
$SourcePath = Join-Path $PSScriptRoot "src"

try {
    $env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($PreviousPythonPath)) {
        $SourcePath
    }
    else {
        "$SourcePath$([IO.Path]::PathSeparator)$PreviousPythonPath"
    }
    if ([string]::IsNullOrWhiteSpace($Name)) {
        $Version = & $Python -c "from ed_fast_telemetry import DISPLAY_VERSION; print(DISPLAY_VERSION)"
        if ($LASTEXITCODE -ne 0) { throw "Could not read application version." }
        $Name = "ED-fast-telemetry-v$Version"
    }
    $env:ED_FAST_TELEMETRY_EXE_NAME = $Name
    Push-Location $PSScriptRoot
    try {
        & $Python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "ed_fast_telemetry.spec")
    }
    finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($null -eq $PreviousExecutableName) {
        Remove-Item Env:ED_FAST_TELEMETRY_EXE_NAME -ErrorAction SilentlyContinue
    }
    else {
        $env:ED_FAST_TELEMETRY_EXE_NAME = $PreviousExecutableName
    }
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
}

Write-Host "Built dist\$Name.exe"
