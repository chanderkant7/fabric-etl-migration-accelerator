#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
#Requires -Version 7.0

<#
.SYNOPSIS
    Deploys the Direct Lake assessment semantic model to Microsoft Fabric.
.DESCRIPTION
    Resolves an existing workspace and Lakehouse, builds the TMDL payload,
    creates the semantic model or explicitly overwrites its definition, and
    optionally triggers a Direct Lake framing refresh.
.PARAMETER WorkspaceName
    Target Fabric workspace display name.
.PARAMETER WorkspaceId
    Target Fabric workspace GUID. Use either WorkspaceName or WorkspaceId.
.PARAMETER LakehouseName
    Existing Lakehouse display name.
.PARAMETER LakehouseId
    Existing Lakehouse GUID. Overrides LakehouseName.
.PARAMETER FolderName
    Workspace folder path for the semantic model. Pass an empty string for root.
.PARAMETER SemanticModelName
    Semantic model display name.
.PARAMETER AllowOverwrite
    Authorizes replacement of an existing same-named semantic model definition.
.PARAMETER SkipRefresh
    Skips the Direct Lake framing refresh after deployment.
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
    ./scripts/Deploy-FabricSemanticModel.ps1 -WorkspaceName 'fabric-dev' -DryRun
.EXAMPLE
    ./scripts/Deploy-FabricSemanticModel.ps1 -WorkspaceName 'fabric-dev' `
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
    [string]$LakehouseName = 'ETL_Migration_Assessment_Lakehouse',

    [Parameter(Mandatory = $false)]
    [string]$LakehouseId,

    [Parameter(Mandatory = $false)]
    [AllowEmptyString()]
    [string]$FolderName = 'FEMA',

    [Parameter(Mandatory = $false)]
    [ValidateNotNullOrEmpty()]
    [string]$SemanticModelName = 'ETL Migration Assessment Model',

    [Parameter(Mandatory = $false)]
    [switch]$AllowOverwrite,

    [Parameter(Mandatory = $false)]
    [switch]$SkipRefresh,

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
$FabricApi = 'https://api.fabric.microsoft.com/v1'
$GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'

Import-Module (Join-Path $PSScriptRoot 'FabricApi.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'FabricDeployment.psm1') -Force

function Invoke-FabricSemanticModelDeployment {
    <#
    .SYNOPSIS
        Executes semantic model payload generation and deployment.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param()

    if ($UseAzCli -and $UseBrowserLogin) {
        throw 'Use either -UseAzCli or -UseBrowserLogin, not both.'
    }
    $Builder = Join-Path $PSScriptRoot 'build-powerbi-semantic-model-payload.mjs'
    if (-not (Test-Path -LiteralPath $Builder -PathType Leaf)) {
        throw "Semantic model payload builder not found: $Builder"
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        throw 'Node.js is required to build the semantic model payload.'
    }

    $TempDir = Join-Path ([IO.Path]::GetTempPath()) (
        'fabric-semantic-model-' + [guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $TempDir | Out-Null
    try {
        $PayloadPath = Join-Path $TempDir 'semantic-model-payload.json'
        if ($DryRun) {
            $SyntheticWorkspaceId = if ($WorkspaceId -match $GuidPattern) {
                $WorkspaceId
            }
            else {
                '11111111-1111-4111-8111-111111111111'
            }
            $SyntheticLakehouseId = if ($LakehouseId -match $GuidPattern) {
                $LakehouseId
            }
            else {
                '22222222-2222-4222-8222-222222222222'
            }
            & node $Builder --workspace-id $SyntheticWorkspaceId `
                --lakehouse-id $SyntheticLakehouseId `
                --display-name $SemanticModelName --no-schema `
                --out $PayloadPath --force
            if ($LASTEXITCODE -ne 0) {
                throw 'Semantic model payload validation failed.'
            }
            $Summary = [ordered]@{
                mode              = 'dry-run'
                workspace         = if ($WorkspaceName) { $WorkspaceName } else { $SyntheticWorkspaceId }
                lakehouse         = if ($LakehouseId) { $LakehouseId } else { $LakehouseName }
                folderName        = $FolderName
                semanticModelName = $SemanticModelName
                payloadValidated  = $true
            }
            Write-DeploymentSummary -Summary $Summary
            return
        }

        $Token = Get-FabricAccessToken -AllowAzCliFallback -UseAzCli:$UseAzCli `
            -UseBrowserLogin:$UseBrowserLogin -TenantId $TenantId
        $Workspace = Resolve-FabricWorkspace -WorkspaceName $WorkspaceName `
            -WorkspaceId $WorkspaceId -Token $Token `
            -SkillName 'semantic-model-authoring'
        $ResolvedLakehouseName = if ($LakehouseId) { $null } else { $LakehouseName }
        $Lakehouse = Resolve-FabricItem -WorkspaceId $Workspace.id `
            -Collection 'lakehouses' -DisplayName $ResolvedLakehouseName `
            -ItemId $LakehouseId -Token $Token -SkillName 'semantic-model-authoring'
        $Folder = Resolve-OrCreateFabricFolderPath -WorkspaceId $Workspace.id `
            -FolderPath $FolderName -Token $Token -SkillName 'semantic-model-authoring'
        $FolderId = if ($Folder) { [string]$Folder.id } else { $null }

        $LakehouseDetails = Invoke-FabricRequest -Method Get `
            -Uri "$FabricApi/workspaces/$($Workspace.id)/lakehouses/$($Lakehouse.id)" `
            -Token $Token -SkillName 'semantic-model-authoring'
        $DefaultSchema = [string]$LakehouseDetails.properties.defaultSchema
        $BuilderArguments = @(
            $Builder
            '--workspace-id'
            $Workspace.id
            '--lakehouse-id'
            $Lakehouse.id
            '--display-name'
            $SemanticModelName
            '--out'
            $PayloadPath
            '--force'
        )
        if ([string]::IsNullOrWhiteSpace($DefaultSchema)) {
            $BuilderArguments += '--no-schema'
        }
        else {
            $BuilderArguments += @('--schema-name', $DefaultSchema)
        }
        & node @BuilderArguments
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to build the semantic model payload.'
        }

        $SemanticModel = Publish-FabricDefinedItem -WorkspaceId $Workspace.id `
            -Collection 'semanticModels' -DisplayName $SemanticModelName `
            -PayloadPath $PayloadPath -FolderId $FolderId `
            -AllowOverwrite:$AllowOverwrite -Token $Token `
            -SkillName 'semantic-model-authoring'

        if (-not $SkipRefresh) {
            Invoke-SemanticModelRefresh -WorkspaceId $Workspace.id `
                -SemanticModelId $SemanticModel.id
        }
        $Summary = [ordered]@{
            mode                = 'deployed'
            workspaceId         = $Workspace.id
            workspaceName       = $Workspace.displayName
            lakehouseId         = $Lakehouse.id
            semanticModelId     = $SemanticModel.id
            semanticModelName   = $SemanticModel.displayName
            semanticModelAction = $SemanticModel.action
            folderId            = $FolderId
            refreshTriggered    = (-not $SkipRefresh)
        }
        Write-DeploymentSummary -Summary $Summary
    }
    finally {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-SemanticModelRefresh {
    <#
    .SYNOPSIS
        Triggers and waits for a Direct Lake framing refresh.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $true)]
        [string]$SemanticModelId
    )

    $PbiToken = Get-FabricAccessToken -AllowAzCliFallback -UseAzCli:$UseAzCli `
        -UseBrowserLogin:$UseBrowserLogin -TenantId $TenantId `
        -Resource 'https://analysis.windows.net/powerbi/api'
    $RefreshUri = (
        "https://api.powerbi.com/v1.0/myorg/groups/$WorkspaceId/" +
        "datasets/$SemanticModelId/refreshes"
    )
    $Body = @{ notifyOption = 'NoNotification' } | ConvertTo-Json -Compress
    $null = Invoke-FabricRequest -Method Post -Uri $RefreshUri `
        -Token $PbiToken -SkillName 'semantic-model-authoring' `
        -Body $Body -NoLroPoll
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        Start-Sleep -Seconds 3
        $History = Invoke-FabricRequest -Method Get `
            -Uri "${RefreshUri}?`$top=1" -Token $PbiToken `
            -SkillName 'semantic-model-authoring'
        $Latest = @($History.value) | Select-Object -First 1
        if ($null -eq $Latest) {
            continue
        }
        if ($Latest.status -eq 'Completed') {
            Write-Host 'Semantic model Direct Lake framing refresh completed.'
            return
        }
        if ($Latest.status -in @('Failed', 'Cancelled', 'Disabled')) {
            throw "Semantic model refresh ended in $($Latest.status): $($Latest.serviceExceptionJson)"
        }
    }
    throw 'Timed out waiting for the semantic model refresh.'
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
        Invoke-FabricSemanticModelDeployment
    }
    catch {
        Write-Error -ErrorAction Continue $_.Exception.Message
        exit 1
    }
}
