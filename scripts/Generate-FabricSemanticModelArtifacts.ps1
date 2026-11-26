#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Generates reviewable semantic-model source and REST payload artifacts.
.DESCRIPTION
    Resolves workspace and Lakehouse tokens in the checked-in TMDL source,
    writes a standalone SemanticModel folder, and emits the corresponding
    Fabric create payload without calling Fabric APIs.
.PARAMETER WorkspaceId
    Fabric workspace GUID used in the Direct Lake expression.
.PARAMETER LakehouseId
    Fabric Lakehouse GUID used in the Direct Lake expression.
.PARAMETER SchemaName
    Optional Lakehouse schema name. Omit it for a schema-less Lakehouse.
.PARAMETER SemanticModelName
    Semantic model display name in the generated create payload.
.PARAMETER OutputDirectory
    Root directory for generated source and payload files.
.PARAMETER Force
    Replaces the generated semantic-model folder and payload if they exist.
.EXAMPLE
    ./scripts/Generate-FabricSemanticModelArtifacts.ps1 `
      -WorkspaceId '<workspace-guid>' -LakehouseId '<lakehouse-guid>'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F-]{36}$')]
    [string]$WorkspaceId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F-]{36}$')]
    [string]$LakehouseId,

    [Parameter(Mandatory = $false)]
    [string]$SchemaName,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$SemanticModelName = 'ETL Migration Assessment Model',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory = (
        Join-Path $PSScriptRoot '..\dist\manual-deployment'
    ),

    [Parameter(Mandatory = $false)]
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function New-FabricSemanticModelArtifacts {
    <#
    .SYNOPSIS
        Generates semantic-model source and payload files.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param()

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Node.js is required to generate semantic-model artifacts.'
    }
    $Builder = Join-Path $PSScriptRoot 'build-powerbi-semantic-model-payload.mjs'
    if (-not (Test-Path -LiteralPath $Builder -PathType Leaf)) {
        throw "Semantic model payload builder not found: $Builder"
    }

    $ResolvedOutput = [IO.Path]::GetFullPath($OutputDirectory)
    $ModelDirectory = Join-Path $ResolvedOutput (
        'ETL Migration Assessment Model.SemanticModel'
    )
    $PayloadPath = Join-Path $ResolvedOutput 'semantic-model-payload.json'
    Remove-GeneratedPath -Path $ModelDirectory -ExpectedSuffix '.SemanticModel'
    if ((Test-Path -LiteralPath $PayloadPath) -and -not $Force) {
        throw "Generated payload already exists: $PayloadPath. Pass -Force to replace it."
    }

    New-Item -ItemType Directory -Path $ResolvedOutput -Force | Out-Null
    $Arguments = @(
        $Builder
        '--workspace-id'
        $WorkspaceId
        '--lakehouse-id'
        $LakehouseId
        '--display-name'
        $SemanticModelName
        '--out'
        $PayloadPath
        '--force'
    )
    if ([string]::IsNullOrWhiteSpace($SchemaName)) {
        $Arguments += '--no-schema'
    }
    else {
        $Arguments += @('--schema-name', $SchemaName)
    }
    & node @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw 'Semantic model artifact generation failed.'
    }

    $Payload = Get-Content -LiteralPath $PayloadPath -Raw | ConvertFrom-Json
    foreach ($Part in @($Payload.definition.parts)) {
        $Destination = Join-Path $ModelDirectory (
            [string]$Part.path -replace '/', [IO.Path]::DirectorySeparatorChar
        )
        $Parent = Split-Path -Parent $Destination
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
        [IO.File]::WriteAllBytes(
            $Destination,
            [Convert]::FromBase64String([string]$Part.payload)
        )
    }
    Write-Host "Generated semantic model source: $ModelDirectory"
    Write-Host "Generated semantic model payload: $PayloadPath"
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
        New-FabricSemanticModelArtifacts
    }
    catch {
        Write-Error -ErrorAction Continue $_.Exception.Message
        exit 1
    }
}
