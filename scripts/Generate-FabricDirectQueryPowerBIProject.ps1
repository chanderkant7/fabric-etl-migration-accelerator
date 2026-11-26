#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Generates a local Power BI project using DirectQuery over a Fabric SQL endpoint.
.DESCRIPTION
    Copies the checked-in semantic model, converts all Direct Lake entity
    partitions to DirectQuery M partitions, regenerates the PBIR report, and
    writes a Desktop-openable PBIP project without calling Fabric APIs.
.PARAMETER SqlEndpoint
    Fabric Lakehouse SQL analytics endpoint server name.
.PARAMETER DatabaseName
    SQL analytics endpoint database name.
.PARAMETER SchemaName
    SQL schema containing the eight assessment tables.
.PARAMETER OutputDirectory
    Root directory for the generated PBIP project.
.PARAMETER Force
    Replaces the generated DirectQuery project when it already exists.
.PARAMETER RequireValidation
    Fails when powerbi-report-author is unavailable instead of skipping validation.
.EXAMPLE
    ./scripts/Generate-FabricDirectQueryPowerBIProject.ps1 `
      -SqlEndpoint '<server>.datawarehouse.fabric.microsoft.com' `
      -DatabaseName 'ETL_Migration_Assessment_Lakehouse'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9.-]+\.datawarehouse\.fabric\.microsoft\.com$')]
    [string]$SqlEndpoint,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$DatabaseName = 'ETL_Migration_Assessment_Lakehouse',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$SchemaName = 'dbo',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory = (
        Join-Path $PSScriptRoot '..\dist\directquery-desktop'
    ),

    [Parameter(Mandatory = $false)]
    [switch]$Force,

    [Parameter(Mandatory = $false)]
    [switch]$RequireValidation
)

$ErrorActionPreference = 'Stop'

function New-FabricDirectQueryPowerBIProject {
    <#
    .SYNOPSIS
        Generates and validates the DirectQuery PBIP project.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param()

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Node.js is required to generate the DirectQuery project.'
    }

    $ModelSource = Join-Path $PSScriptRoot (
        '..\powerbi\ETL Migration Assessment Model.SemanticModel'
    )
    $ReportSource = Join-Path $PSScriptRoot (
        '..\powerbi\ETL Migration Assessment.Report'
    )
    $ReportGenerator = Join-Path $PSScriptRoot 'generate-powerbi-report.mjs'
    $PbipSource = Join-Path $PSScriptRoot (
        '..\powerbi\ETL Migration Assessment.pbip'
    )
    foreach ($RequiredPath in @(
        $ModelSource
        $ReportSource
        $ReportGenerator
        $PbipSource
    )) {
        if (-not (Test-Path -LiteralPath $RequiredPath)) {
            throw "Required project source not found: $RequiredPath"
        }
    }

    $ResolvedOutput = [IO.Path]::GetFullPath($OutputDirectory)
    $ModelOutput = Join-Path $ResolvedOutput (
        'ETL Migration Assessment Model.SemanticModel'
    )
    $ReportOutput = Join-Path $ResolvedOutput 'ETL Migration Assessment.Report'
    $PbipOutput = Join-Path $ResolvedOutput 'ETL Migration Assessment.pbip'

    Assert-OutputDoesNotOverwriteSource -OutputPath $ModelOutput `
        -SourcePath $ModelSource
    Assert-OutputDoesNotOverwriteSource -OutputPath $ReportOutput `
        -SourcePath $ReportSource
    Assert-OutputDoesNotOverwriteSource -OutputPath $PbipOutput `
        -SourcePath $PbipSource

    Remove-GeneratedDirectory -Path $ModelOutput -ExpectedSuffix '.SemanticModel'
    Remove-GeneratedDirectory -Path $ReportOutput -ExpectedSuffix '.Report'
    if ((Test-Path -LiteralPath $PbipOutput) -and -not $Force) {
        throw "Generated project already exists: $PbipOutput. Pass -Force to replace it."
    }

    New-Item -ItemType Directory -Path $ResolvedOutput -Force | Out-Null
    Copy-Item -LiteralPath $ModelSource -Destination $ModelOutput -Recurse
    Convert-ModelToDirectQuery -ModelDirectory $ModelOutput

    & node $ReportGenerator --out-dir $ReportOutput --force
    if ($LASTEXITCODE -ne 0) {
        throw 'Power BI report source generation failed.'
    }
    Copy-Item -LiteralPath $PbipSource -Destination $PbipOutput -Force

    $Validator = Get-Command powerbi-report-author -ErrorAction SilentlyContinue
    if ($Validator) {
        & $Validator.Source validate $ReportOutput
        if ($LASTEXITCODE -ne 0) {
            throw 'Generated Power BI report validation failed.'
        }
    }
    elseif ($RequireValidation) {
        throw (
            'powerbi-report-author is required when -RequireValidation is set. ' +
            'Install @microsoft/powerbi-report-authoring-cli or omit the switch.'
        )
    }
    else {
        Write-Warning (
            'powerbi-report-author is unavailable; PBIR validation was skipped. ' +
            'Install @microsoft/powerbi-report-authoring-cli to enable validation.'
        )
    }

    Write-Host "Generated DirectQuery Power BI project: $PbipOutput"
    Write-Host "Semantic model source: $ModelOutput"
    Write-Host "Report source: $ReportOutput"
}

function Assert-OutputDoesNotOverwriteSource {
    <#
    .SYNOPSIS
        Prevents generated output from replacing checked-in source files.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$OutputPath,

        [Parameter(Mandatory = $true)]
        [string]$SourcePath
    )

    $ResolvedOutputPath = [IO.Path]::GetFullPath($OutputPath)
    $ResolvedSourcePath = [IO.Path]::GetFullPath($SourcePath)
    if (
        $ResolvedOutputPath.Equals(
            $ResolvedSourcePath,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Output path cannot overwrite checked-in source: $ResolvedSourcePath"
    }
}

function Convert-ModelToDirectQuery {
    <#
    .SYNOPSIS
        Converts Direct Lake expressions and partitions to DirectQuery.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
        [string]$ModelDirectory
    )

    $EscapedServer = $SqlEndpoint.Replace('"', '""')
    $EscapedDatabase = $DatabaseName.Replace('"', '""')
    $EscapedSchema = $SchemaName.Replace('"', '""')
    $ModelPath = Join-Path $ModelDirectory 'definition\model.tmdl'
    $ModelText = Get-Content -LiteralPath $ModelPath -Raw
    $ExpressionPattern = (
        "(?ms)^expression 'DirectLake - ETL Migration Assessment Model' =\r?\n" +
        "\tlet\r?\n" +
        "\t\tSource = AzureStorage\.DataLake\([^\r\n]+\)\r?\n" +
        "\tin\r?\n" +
        "\t\tSource\r?\n"
    )
    $ExpressionReplacement = @"
expression Server = "$EscapedServer" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Database = "$EscapedDatabase" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]

"@
    $UpdatedModel = [regex]::Replace(
        $ModelText,
        $ExpressionPattern,
        $ExpressionReplacement
    )
    if ($UpdatedModel -eq $ModelText) {
        throw 'Direct Lake model expression was not found.'
    }
    Set-Content -LiteralPath $ModelPath -Value $UpdatedModel -Encoding utf8 -NoNewline

    $PartitionPattern = (
        "(?ms)^\tpartition (?<partition>[^\r\n]+) = entity\r?\n" +
        "\t\tmode: directLake\r?\n" +
        "\t\tsource\r?\n" +
        "\t\t\tentityName: (?<entity>[^\r\n]+)\r?\n" +
        "(?:\t\t\tschemaName: [^\r\n]+\r?\n)?" +
        "\t\t\texpressionSource: [^\r\n]+\r?\n?"
    )
    $TableFiles = @(
        Get-ChildItem -LiteralPath (
            Join-Path $ModelDirectory 'definition\tables'
        ) -Filter '*.tmdl' -File
    )
    if ($TableFiles.Count -eq 0) {
        throw 'No semantic-model table definitions were found.'
    }
    foreach ($TableFile in $TableFiles) {
        $TableText = Get-Content -LiteralPath $TableFile.FullName -Raw
        $Match = [regex]::Match($TableText, $PartitionPattern)
        if (-not $Match.Success) {
            throw "Direct Lake partition not found in $($TableFile.Name)."
        }
        $PartitionName = $Match.Groups['partition'].Value.Trim()
        $EntityName = $Match.Groups['entity'].Value.Trim().Replace('"', '""')
        $Replacement = @"
`tpartition $PartitionName = m
`t`tmode: directQuery
`t`tsource =
`t`t`tlet
`t`t`t`tSource = Sql.Database(#"Server", #"Database"),
`t`t`t`tNavigation = Source{[Schema="$EscapedSchema", Item="$EntityName"]}[Data]
`t`t`tin
`t`t`t`tNavigation
"@
        $UpdatedTable = [regex]::Replace(
            $TableText,
            $PartitionPattern,
            $Replacement,
            1
        )
        Set-Content -LiteralPath $TableFile.FullName -Value $UpdatedTable `
            -Encoding utf8 -NoNewline
    }
}

function Remove-GeneratedDirectory {
    <#
    .SYNOPSIS
        Removes one known generated project directory when authorized.
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
        New-FabricDirectQueryPowerBIProject
    }
    catch {
        Write-Error -ErrorAction Continue $_.Exception.Message
        exit 1
    }
}
