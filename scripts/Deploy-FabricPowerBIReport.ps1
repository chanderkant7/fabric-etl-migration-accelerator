#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Deploys the assessment Power BI report to Microsoft Fabric.
.DESCRIPTION
    Resolves an existing workspace and semantic model, builds a PBIR payload
    bound to that model, and creates the report or explicitly overwrites its
    existing definition.
.PARAMETER WorkspaceName
    Target Fabric workspace display name.
.PARAMETER WorkspaceId
    Target Fabric workspace GUID. Use either WorkspaceName or WorkspaceId.
.PARAMETER SemanticModelName
    Existing semantic model display name.
.PARAMETER SemanticModelId
    Existing semantic model GUID. Overrides SemanticModelName.
.PARAMETER FolderName
    Workspace folder path for the report. Pass an empty string for root.
.PARAMETER ReportName
    Power BI report display name.
.PARAMETER AllowOverwrite
    Authorizes replacement of an existing same-named report definition.
.PARAMETER UseAzCli
    Reuses the current Azure CLI sign-in.
.PARAMETER UseBrowserLogin
    Opens Azure CLI browser sign-in before deployment.
.PARAMETER TenantId
    Optional Microsoft Entra tenant for browser sign-in.
.PARAMETER OutputSummaryPath
    Optional path for a nonsecret deployment summary JSON file.
.PARAMETER DryRun
    Builds and validates the payload without calling Fabric.
.EXAMPLE
    ./scripts/Deploy-FabricPowerBIReport.ps1 -WorkspaceName 'fabric-dev' -DryRun
.EXAMPLE
    ./scripts/Deploy-FabricPowerBIReport.ps1 -WorkspaceName 'fabric-dev' `
      -UseAzCli -AllowOverwrite
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$WorkspaceName,

    [Parameter(Mandatory = $false)]
    [string]$WorkspaceId,

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$SemanticModelName = 'ETL Migration Assessment Model',

    [Parameter(Mandatory = $false)]
    [string]$SemanticModelId,

    [Parameter(Mandatory = $false)]
    [AllowEmptyString()]
    [string]$FolderName = 'FEMA',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$ReportName = 'ETL Migration Assessment',

    [Parameter(Mandatory = $false)]
    [switch]$AllowOverwrite,

    [Parameter(Mandatory = $false)]
    [switch]$UseAzCli,

    [Parameter(Mandatory = $false)]
    [switch]$UseBrowserLogin,

    [Parameter(Mandatory = $false)]
    [string]$TenantId = $env:AZURE_TENANT_ID,

    [Parameter(Mandatory = $false)]
    [string]$OutputSummaryPath,

    [Parameter(Mandatory = $false)]
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'

Import-Module (Join-Path $PSScriptRoot 'FabricApi.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'FabricDeployment.psm1') -Force

function Invoke-FabricPowerBIReportDeployment {
    <#
    .SYNOPSIS
        Executes report payload generation and deployment.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param()

    if ($UseAzCli -and $UseBrowserLogin) {
        throw 'Use either -UseAzCli or -UseBrowserLogin, not both.'
    }
    $Builder = Join-Path $PSScriptRoot 'build-powerbi-report-payload.mjs'
    if (-not (Test-Path -LiteralPath $Builder -PathType Leaf)) {
        throw "Power BI report payload builder not found: $Builder"
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Node.js is required to build the Power BI report payload.'
    }

    $TempDir = Join-Path ([IO.Path]::GetTempPath()) (
        'fabric-powerbi-report-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $TempDir | Out-Null
    try {
        $PayloadPath = Join-Path $TempDir 'report-payload.json'
        if ($DryRun) {
            $SyntheticWorkspaceId = if ($WorkspaceId -match $GuidPattern) {
                $WorkspaceId
            }
            else {
                '11111111-1111-4111-8111-111111111111'
            }
            $SyntheticSemanticModelId = if ($SemanticModelId -match $GuidPattern) {
                $SemanticModelId
            }
            else {
                '44444444-4444-4444-8444-444444444444'
            }
            & node $Builder --semantic-model-id $SyntheticSemanticModelId `
                --display-name $ReportName --out $PayloadPath --force
            if ($LASTEXITCODE -ne 0) {
                throw 'Power BI report payload validation failed.'
            }
            $Summary = [ordered]@{
                mode              = 'dry-run'
                workspace         = if ($WorkspaceName) { $WorkspaceName } else { $SyntheticWorkspaceId }
                semanticModel     = if ($SemanticModelId) { $SemanticModelId } else { $SemanticModelName }
                folderName        = $FolderName
                reportName        = $ReportName
                payloadValidated  = $true
            }
            Write-DeploymentSummary -Summary $Summary
            return
        }

        $Token = Get-FabricAccessToken -AllowAzCliFallback -UseAzCli:$UseAzCli `
            -UseBrowserLogin:$UseBrowserLogin -TenantId $TenantId
        $Workspace = Resolve-FabricWorkspace -WorkspaceName $WorkspaceName `
            -WorkspaceId $WorkspaceId -Token $Token -SkillName 'powerbi-report-cli'
        $ResolvedSemanticModelName = if ($SemanticModelId) {
            $null
        }
        else {
            $SemanticModelName
        }
        $SemanticModel = Resolve-FabricItem -WorkspaceId $Workspace.id `
            -Collection 'semanticModels' -DisplayName $ResolvedSemanticModelName `
            -ItemId $SemanticModelId -Token $Token -SkillName 'powerbi-report-cli'
        $Folder = Resolve-OrCreateFabricFolderPath -WorkspaceId $Workspace.id `
            -FolderPath $FolderName -Token $Token -SkillName 'powerbi-report-cli'
        $FolderId = if ($Folder) { [string]$Folder.id } else { $null }

        & node $Builder --semantic-model-id $SemanticModel.id `
            --display-name $ReportName --out $PayloadPath --force
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to build the Power BI report payload.'
        }
        $Report = Publish-FabricDefinedItem -WorkspaceId $Workspace.id `
            -Collection 'reports' -DisplayName $ReportName `
            -PayloadPath $PayloadPath -FolderId $FolderId `
            -AllowOverwrite:$AllowOverwrite -Token $Token `
            -SkillName 'powerbi-report-cli'
        $ReportUrl = (
            "https://app.fabric.microsoft.com/groups/$($Workspace.id)/" +
            "reports/$($Report.id)"
        )
        $Summary = [ordered]@{
            mode              = 'deployed'
            workspaceId       = $Workspace.id
            workspaceName     = $Workspace.displayName
            semanticModelId   = $SemanticModel.id
            reportId          = $Report.id
            reportName        = $Report.displayName
            reportAction      = $Report.action
            reportUrl         = $ReportUrl
            folderId          = $FolderId
        }
        Write-DeploymentSummary -Summary $Summary
    }
    finally {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Write-DeploymentSummary {
    <#
    .SYNOPSIS
        Writes deployment summary output to the console and optional JSON file.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$Summary
    )

    $Json = $Summary | ConvertTo-Json -Depth 10
    if (-not [string]::IsNullOrWhiteSpace($OutputSummaryPath)) {
        $Json | Set-Content -LiteralPath $OutputSummaryPath -Encoding utf8
    }
    Write-Host $Json
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-FabricPowerBIReportDeployment
    }
    catch {
        Write-Error -ErrorAction Continue $_.Exception.Message
        exit 1
    }
}
