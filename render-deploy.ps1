param(
    [string]$ServiceId = "",
    [string]$Branch = "main",
    [switch]$SkipDeploy,
    [switch]$LocalRun
)

$ErrorActionPreference = "Stop"

function Import-EnvFile {
    param(
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        throw ".env file not found at: $Path"
    }

    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()

        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        if ($line -match '^([^=]+?)\s*=\s*(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()

            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }

            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

$envFile = Join-Path $PSScriptRoot ".env"
Import-EnvFile -Path $envFile

if (-not $SkipDeploy) {
    if (-not $ServiceId) {
        $ServiceId = $env:RENDER_SERVICE_ID
    }

    if (-not $ServiceId) {
        throw "Missing Render service ID. Pass -ServiceId or set RENDER_SERVICE_ID in your environment."
    }

    Write-Host "Loading .env variables into the process..."
    Write-Host "Deploying Render service: $ServiceId"
    render deploy --service-id $ServiceId --branch $Branch
}

if ($LocalRun) {
    Write-Host "Running local app build and startup with the same .env values..."
    uv sync --frozen
    uv run --frozen uvicorn web.app:app --host 0.0.0.0 --port $env:PORT
}
