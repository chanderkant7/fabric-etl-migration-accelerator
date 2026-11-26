#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Generates reviewable Power BI report source and REST payload artifacts.
.DESCRIPTION
    Regenerates the PBIR report in an isolated output folder, copies the PBIP
    entry file, and emits a Fabric create payload bound to the supplied
    semantic model ID without calling Fabric APIs.
.PARAMETER SemanticModelId
    Fabric semantic model GUID used in the deployment payload binding.
.PARAMETER WorkspaceName
    Fabric workspace display name used in the Power BI Desktop connection.
.PARAMETER SemanticModelName
    Fabric semantic model display name used in the Power BI Desktop connection.
.PARAMETER ReportName
    Report display name in the generated create payload.
.PARAMETER OutputDirectory
    Root directory for generated source and payload files.
.PARAMETER Force
    Replaces the generated report folder, PBIP entry file, and payload.
.EXAMPLE
    ./scripts/Generate-FabricPowerBIReportArtifacts.ps1 `
      -WorkspaceName '<workspace-name>' `
      -SemanticModelId '<semantic-model-guid>'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F-]{36}$')]
    [string]$SemanticModelId,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$WorkspaceName,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$SemanticModelName = 'ETL Migration Assessment Model',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$ReportName = 'ETL Migration Assessment',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory = (
        Join-Path $PSScriptRoot '..\dist\manual-deployment'
    ),

    [Parameter(Mandatory = $false)]
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function New-FabricPowerBIReportArtifacts {
    <#
    .SYNOPSIS
        Generates report source and payload files.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param()

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Node.js is required to generate Power BI report artifacts.'
    }
    $Generator = Join-Path $PSScriptRoot 'generate-powerbi-report.mjs'
    $Builder = Join-Path $PSScriptRoot 'build-powerbi-report-payload.mjs'
    foreach ($RequiredPath in @($Generator, $Builder)) {
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
            throw "Required report generator not found: $RequiredPath"
        }
    }

    $ResolvedOutput = [IO.Path]::GetFullPath($OutputDirectory)
    $ReportDirectory = Join-Path $ResolvedOutput 'ETL Migration Assessment.Report'
    $PbipPath = Join-Path $ResolvedOutput 'ETL Migration Assessment.pbip'
    $PayloadPath = Join-Path $ResolvedOutput 'powerbi-report-payload.json'
    Remove-GeneratedPath -Path $ReportDirectory -ExpectedSuffix '.Report'
    foreach ($FilePath in @($PbipPath, $PayloadPath)) {
        if ((Test-Path -LiteralPath $FilePath) -and -not $Force) {
            throw "Generated output already exists: $FilePath. Pass -Force to replace it."
        }
    }

    New-Item -ItemType Directory -Path $ResolvedOutput -Force | Out-Null
    & node $Generator --out-dir $ReportDirectory --force
    if ($LASTEXITCODE -ne 0) {
        throw 'Power BI report source generation failed.'
    }
    $PbirPath = Join-Path $ReportDirectory 'definition.pbir'
    $Pbir = Get-Content -LiteralPath $PbirPath -Raw | ConvertFrom-Json
    $Connection = [System.Data.Common.DbConnectionStringBuilder]::new()
    $Connection['Data Source'] = (
        "powerbi://api.powerbi.com/v1.0/myorg/$WorkspaceName"
    )
    $Connection['Initial Catalog'] = $SemanticModelName
    $Connection['Integrated Security'] = 'ClaimsToken'
    $Connection['semanticModelId'] = $SemanticModelId
    $Pbir.datasetReference = [pscustomobject]@{
        byConnection = [pscustomobject]@{
            connectionString = $Connection.ConnectionString
        }
    }
    $Pbir | ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $PbirPath -Encoding utf8
    Copy-Item -LiteralPath (
        Join-Path $PSScriptRoot '..\powerbi\ETL Migration Assessment.pbip'
    ) -Destination $PbipPath -Force

    & node $Builder --source-dir $ReportDirectory `
        --semantic-model-id $SemanticModelId --display-name $ReportName `
        --out $PayloadPath --force
    if ($LASTEXITCODE -ne 0) {
        throw 'Power BI report payload generation failed.'
    }
    Write-Host "Generated Power BI project: $PbipPath"
    Write-Host "Generated Power BI report source: $ReportDirectory"
    Write-Host "Generated Power BI report payload: $PayloadPath"
}

function Remove-GeneratedPath {
    <#
    .SYNOPSIS
        Removes one known generated directory when replacement is authorized.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSuffix
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not $Force) {
        throw "Generated output already exists: $Path. Pass -Force to replace it."
    }
    if (-not ([IO.Path]::GetFileName($Path).EndsWith($ExpectedSuffix))) {
        throw "Refusing to replace an unexpected output directory: $Path"
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        New-FabricPowerBIReportArtifacts
    }
    catch {
        Write-Error -ErrorAction Continue $_.Exception.Message
        exit 1
    }
}
