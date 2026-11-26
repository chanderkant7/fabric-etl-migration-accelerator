#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Builds and launches the local ETL Migration Accelerator app using built Python wheels.
.DESCRIPTION
    Builds all four workspace wheels (etl-migration-core, etl-portfolio-assessment,
    etl-target-fabric, and fabric-etl-migration-accelerator with embedded Jinja2
    and HTMX UI assets), installs them into an isolated non-editable runtime
    virtual environment under build/app-venv, verifies non-editable wheel
    resolution, and runs the local web UI server (or Windows desktop utility).
.PARAMETER Port
    Loopback HTTP port for the web server (default: 8050).
.PARAMETER WheelDirectory
    Directory containing built .whl files (default: dist).
.PARAMETER Desktop
    Launches the native Windows WebView2 desktop window instead of the HTTP server.
.PARAMETER SkipWheelBuild
    Uses existing .whl files in WheelDirectory instead of running pip wheel.
.PARAMETER VerifyOnly
    Installs the wheels, verifies non-editable metadata, probes /health, /, and
    /api/targets on Port, and exits without leaving a running process.
.EXAMPLE
    .\scripts\Start-LocalApp.ps1
.EXAMPLE
    .\scripts\Start-LocalApp.ps1 -Port 8050 -VerifyOnly
.NOTES
    Runs the local analyst application from built .whl distributions rather than
    editable workspace source mounts.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidateRange(1024, 65535)]
    [int]$Port = 8050,

    [Parameter(Mandatory = $false)]
    [string]$WheelDirectory = 'dist',

    [Parameter(Mandatory = $false)]
    [switch]$Desktop,

    [Parameter(Mandatory = $false)]
    [switch]$SkipWheelBuild,

    [Parameter(Mandatory = $false)]
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'

#region Functions
function Resolve-WorkspaceWheels {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Directory
    )

    $Patterns = @(
        'etl_migration_core-*.whl',
        'etl_portfolio_assessment-*.whl',
        'etl_target_fabric-*.whl',
        'fabric_etl_migration_accelerator-*.whl'
    )
    $Wheels = [System.Collections.Generic.List[string]]::new()
    foreach ($Pattern in $Patterns) {
        $Match = Get-ChildItem -LiteralPath $Directory -Filter $Pattern -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if (-not $Match) {
            throw "Missing built wheel matching '$Pattern' in $Directory. Run 'python -m pip wheel --no-deps --wheel-dir dist ./packages/etl-migration-core ./packages/etl-portfolio-assessment ./packages/etl-target-fabric .'."
        }
        $Wheels.Add($Match.FullName)
    }
    return $Wheels.ToArray()
}

function Get-VenvPythonPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$VenvRoot
    )

    $WindowsPython = Join-Path $VenvRoot 'Scripts\python.exe'
    if (Test-Path -LiteralPath $WindowsPython -PathType Leaf) {
        return $WindowsPython
    }
    return (Join-Path $VenvRoot 'bin/python')
}

function Get-VenvToolPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$VenvRoot
    )

    $WindowsTool = Join-Path $VenvRoot 'Scripts\tool.exe'
    if (Test-Path -LiteralPath $WindowsTool -PathType Leaf) {
        return $WindowsTool
    }
    return (Join-Path $VenvRoot 'bin/tool')
}
#endregion Functions

#region Main Execution
if ($MyInvocation.InvocationName -ne '.') {
    $RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    $DistRoot = if ([IO.Path]::IsPathRooted($WheelDirectory)) {
        [IO.Path]::GetFullPath($WheelDirectory)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $WheelDirectory))
    }
    $AppVenv = Join-Path $RepositoryRoot 'build\app-venv'

    Push-Location $RepositoryRoot
    try {
        if (-not $SkipWheelBuild) {
            if (Test-Path -LiteralPath $DistRoot -PathType Container) {
                foreach ($Pattern in @(
                    'etl_migration_core-*.whl',
                    'etl_portfolio_assessment-*.whl',
                    'etl_target_fabric-*.whl',
                    'fabric_etl_migration_accelerator-*.whl'
                )) {
                    Get-ChildItem -LiteralPath $DistRoot -Filter $Pattern -File | Remove-Item -Force
                }
            }
            if (-not (Test-Path -LiteralPath $DistRoot -PathType Container)) {
                New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
            }
            python -m pip wheel --no-deps --wheel-dir $DistRoot `
                ./packages/etl-migration-core `
                ./packages/etl-portfolio-assessment `
                ./packages/etl-target-fabric `
                .
            if ($LASTEXITCODE -ne 0) { throw 'python -m pip wheel failed.' }
        }

        $ResolvedWheels = @(Resolve-WorkspaceWheels -Directory $DistRoot)

        if (-not (Test-Path -LiteralPath $AppVenv -PathType Container)) {
            python -m venv $AppVenv
            if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment at $AppVenv." }
        }

        $VenvPython = Get-VenvPythonPath -VenvRoot $AppVenv
        if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            throw "App runtime Python executable not found: $VenvPython"
        }

        $AppRequirement = if ($Desktop) { 'fabric-etl-migration-accelerator[desktop]' } else { 'fabric-etl-migration-accelerator' }
        & $VenvPython -m pip install --find-links $DistRoot --upgrade $AppRequirement
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install runtime dependencies into build/app-venv.' }

        & $VenvPython -m pip install --no-deps --force-reinstall @ResolvedWheels
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install workspace wheels into build/app-venv.' }

        $VenvTool = Get-VenvToolPath -VenvRoot $AppVenv

        $VerifyMetadataScript = @'
import importlib.metadata as m
import json
from pathlib import Path
import accelerator
import etl_migration_core
import etl_portfolio_assessment
import etl_target_fabric

names = [
    "etl-migration-core",
    "etl-portfolio-assessment",
    "etl-target-fabric",
    "fabric-etl-migration-accelerator",
]
records = []
for name in names:
    dist = m.distribution(name)
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise SystemExit(f"Package {name} is still editable: {direct}")
    records.append({
        "name": name,
        "version": dist.version,
        "url": direct.get("url", ""),
    })

for mod in (accelerator, etl_migration_core, etl_portfolio_assessment, etl_target_fabric):
    mod_path = Path(mod.__file__).resolve()
    if "site-packages" not in mod_path.parts:
        raise SystemExit(f"Module {mod.__name__} did not load from site-packages: {mod_path}")

print(json.dumps({"packages": records, "ui_root": str(Path(accelerator.__file__).resolve().parent / "ui")}))
'@
        $VerifyOutput = $VerifyMetadataScript | & $VenvPython -
        if ($LASTEXITCODE -ne 0) { throw 'Wheel installation verification failed.' }

        if ($VerifyOnly) {
            $ServerProc = Start-Process -FilePath $VenvTool -ArgumentList @('serve', '--port', "$Port") -WorkingDirectory $RepositoryRoot -PassThru
            try {
                $BaseUrl = "http://127.0.0.1:$Port"
                $Ready = $false
                for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
                    Start-Sleep -Milliseconds 250
                    try {
                        $Health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 2
                        if ($Health.status -eq 'ok') {
                            $Ready = $true
                            break
                        }
                    }
                    catch {
                        continue
                    }
                }
                if (-not $Ready) {
                    throw "Wheel-backed server did not respond on $BaseUrl/health."
                }
                $Html = (Invoke-WebRequest -Uri "$BaseUrl/" -UseBasicParsing -TimeoutSec 5).Content
                if ($Html -notmatch '<div id="root">' -or $Html -notmatch '/assets/htmx\.min\.js') {
                    throw "Wheel-backed Python UI did not serve expected HTML/HTMX assets from $BaseUrl/."
                }
                $Targets = Invoke-RestMethod -Uri "$BaseUrl/api/targets" -TimeoutSec 5
                [pscustomobject]@{
                    status   = 'verified'
                    url      = $BaseUrl
                    wheels   = ($ResolvedWheels | ForEach-Object { [IO.Path]::GetFileName($_) })
                    metadata = ($VerifyOutput | ConvertFrom-Json)
                    targets  = @($Targets.targets | ForEach-Object { $_.platform_id })
                } | ConvertTo-Json -Depth 6
            }
            finally {
                if ($ServerProc -and -not $ServerProc.HasExited) {
                    Stop-Process -Id $ServerProc.Id -Force
                }
            }
            return
        }

        Write-Host "Starting ETL Migration Accelerator from built wheels ($(($ResolvedWheels | ForEach-Object { [IO.Path]::GetFileName($_) }) -join ', '))..."
        if ($Desktop) {
            & $VenvTool desktop
        }
        else {
            Write-Host "Analyst UI: http://127.0.0.1:$Port"
            & $VenvTool serve --port $Port
        }
        if ($LASTEXITCODE -ne 0) {
            throw "App exited with code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
#endregion Main Execution
