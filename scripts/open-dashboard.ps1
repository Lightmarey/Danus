param(
    [Parameter(Position = 0)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$Project,

    [Parameter(Position = 1)]
    [ValidateRange(1, 65535)]
    [int]$Port,

    [switch]$NoOpen
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($Port) {
    $env:DASHBOARD_PORT = [string]$Port
}

$python = $env:DANUS_PYTHON_BIN
if (-not $python) { $python = $env:DANUS_PY }
if (-not $python) {
    $venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) { $python = $venvPython }
}
if (-not $python) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

Push-Location $repoRoot
try {
    if (-not $Project) {
        $projectJson = & $python -m danus.orchestration list --json
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        $projects = @(($projectJson | ConvertFrom-Json) | ForEach-Object { $_.project })
        if ($projects.Count -eq 0) {
            throw 'No Danus projects were found.'
        }
        Write-Host 'Danus projects:'
        for ($i = 0; $i -lt $projects.Count; $i++) {
            Write-Host ("  {0}) {1}" -f ($i + 1), $projects[$i])
        }
        while (-not $Project) {
            $answer = Read-Host "Select project [1-$($projects.Count)] (default 1)"
            if (-not $answer) { $answer = '1' }
            $number = 0
            if ([int]::TryParse($answer, [ref]$number) -and $number -ge 1 -and $number -le $projects.Count) {
                $Project = $projects[$number - 1]
            }
            else {
                Write-Warning 'Enter one of the displayed numbers.'
            }
        }
    }

    & $python -m danus.orchestration services up dashboard $Project
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $logs = & $python -m danus.orchestration services logs "dashboard-$Project"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $match = [regex]::Matches(
        ($logs -join "`n"),
        'http://127\.0\.0\.1:[0-9]+/#control-token=[^\s]+'
    ) | Select-Object -Last 1
    if (-not $match) {
        throw "Dashboard started, but its capability URL was not found in logs for dashboard-$Project."
    }

    $url = $match.Value
    Write-Output $url
    if (-not $NoOpen) {
        Start-Process $url
    }
}
finally {
    Pop-Location
}
