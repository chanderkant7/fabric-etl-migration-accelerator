# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

# FabricDeployment.psm1
# Purpose: Shared workspace, folder, item resolution, and definition deployment helpers.

#Requires -Version 7.0

$script:FabricApi = 'https://api.fabric.microsoft.com/v1'
$script:GuidPattern = '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'

Import-Module (Join-Path $PSScriptRoot 'FabricApi.psm1') -Force

function Get-FabricCollection {
    <#
    .SYNOPSIS
        Reads every page from a Fabric collection endpoint.
    .OUTPUTS
        Fabric collection entries.
    #>
    [CmdletBinding()]
    [OutputType([object])]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9-]+$')]
        [string]$SkillName
    )

    $NextUri = $Uri
    $Visited = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    while (-not [string]::IsNullOrWhiteSpace($NextUri)) {
        if (-not $Visited.Add($NextUri)) {
            throw "Repeated Fabric pagination URI: $NextUri"
        }
        $Page = Invoke-FabricRequest -Method Get -Uri $NextUri `
            -Token $Token -SkillName $SkillName
        foreach ($Entry in @($Page.value)) {
            $Entry
        }
        $NextUri = [string]$Page.continuationUri
    }
}

function Resolve-FabricWorkspace {
    <#
    .SYNOPSIS
        Resolves one existing Fabric workspace by exact name or GUID.
    .OUTPUTS
        Fabric workspace metadata.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory = $false)]
        [string]$WorkspaceName,

        [Parameter(Mandatory = $false)]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9-]+$')]
        [string]$SkillName
    )

    if (
        -not [string]::IsNullOrWhiteSpace($WorkspaceName) -and
        -not [string]::IsNullOrWhiteSpace($WorkspaceId)
    ) {
        throw 'Provide either -WorkspaceName or -WorkspaceId, not both.'
    }
    if (
        [string]::IsNullOrWhiteSpace($WorkspaceName) -and
        [string]::IsNullOrWhiteSpace($WorkspaceId)
    ) {
        $WorkspaceId = $env:FABRIC_WORKSPACE_ID
    }
    if (-not [string]::IsNullOrWhiteSpace($WorkspaceId)) {
        if ($WorkspaceId -notmatch $script:GuidPattern) {
            throw "WorkspaceId must be a valid GUID: $WorkspaceId"
        }
        $Workspace = Invoke-FabricRequest -Method Get `
            -Uri "$script:FabricApi/workspaces/$WorkspaceId" `
            -Token $Token -SkillName $SkillName
        return [pscustomobject]@{
            id          = [string]$Workspace.id
            displayName = [string]$Workspace.displayName
        }
    }
    if ([string]::IsNullOrWhiteSpace($WorkspaceName)) {
        throw 'Provide -WorkspaceName or -WorkspaceId (or set FABRIC_WORKSPACE_ID).'
    }

    $Matches = @(
        Get-FabricCollection -Uri "$script:FabricApi/workspaces" `
            -Token $Token -SkillName $SkillName |
            Where-Object { $_.displayName -ceq $WorkspaceName }
    )
    if ($Matches.Count -ne 1) {
        throw "Expected one workspace named '$WorkspaceName'; found $($Matches.Count)."
    }
    return [pscustomobject]@{
        id          = [string]$Matches[0].id
        displayName = [string]$Matches[0].displayName
    }
}

function Resolve-FabricItem {
    <#
    .SYNOPSIS
        Resolves one existing Fabric item by exact name or GUID.
    .OUTPUTS
        Fabric item metadata.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $true)]
        [ValidateSet('lakehouses', 'semanticModels', 'reports')]
        [string]$Collection,

        [Parameter(Mandatory = $false)]
        [string]$DisplayName,

        [Parameter(Mandatory = $false)]
        [string]$ItemId,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9-]+$')]
        [string]$SkillName
    )

    if (
        -not [string]::IsNullOrWhiteSpace($DisplayName) -and
        -not [string]::IsNullOrWhiteSpace($ItemId)
    ) {
        throw 'Provide an item display name or item ID, not both.'
    }
    if (-not [string]::IsNullOrWhiteSpace($ItemId)) {
        if ($ItemId -notmatch $script:GuidPattern) {
            throw "ItemId must be a valid GUID: $ItemId"
        }
        return Invoke-FabricRequest -Method Get `
            -Uri "$script:FabricApi/workspaces/$WorkspaceId/$Collection/$ItemId" `
            -Token $Token -SkillName $SkillName
    }
    if ([string]::IsNullOrWhiteSpace($DisplayName)) {
        throw 'Provide an item display name or item ID.'
    }

    $Matches = @(
        Get-FabricCollection -Uri "$script:FabricApi/workspaces/$WorkspaceId/$Collection" `
            -Token $Token -SkillName $SkillName |
            Where-Object { $_.displayName -ceq $DisplayName }
    )
    if ($Matches.Count -ne 1) {
        throw "Expected one $Collection item named '$DisplayName'; found $($Matches.Count)."
    }
    return $Matches[0]
}

function Resolve-OrCreateFabricFolderPath {
    <#
    .SYNOPSIS
        Resolves or creates a slash-delimited Fabric workspace folder path.
    .OUTPUTS
        The final folder metadata, or null for the workspace root.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $false)]
        [AllowEmptyString()]
        [string]$FolderPath,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9-]+$')]
        [string]$SkillName
    )

    $Segments = @(
        $FolderPath.Replace('\', '/').Split('/') |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($Segments.Count -eq 0) {
        return $null
    }

    $Folders = @(
        Get-FabricCollection -Uri "$script:FabricApi/workspaces/$WorkspaceId/folders" `
            -Token $Token -SkillName $SkillName
    )
    $ParentId = $null
    $Current = $null
    foreach ($Segment in $Segments) {
        $Matches = @(
            $Folders | Where-Object {
                $_.displayName -ceq $Segment -and (
                    (
                        [string]::IsNullOrWhiteSpace($ParentId) -and
                        [string]::IsNullOrWhiteSpace([string]$_.parentFolderId)
                    ) -or
                    ([string]$_.parentFolderId -eq $ParentId)
                )
            }
        )
        if ($Matches.Count -gt 1) {
            throw "Multiple workspace folders matched '$Segment'."
        }
        if ($Matches.Count -eq 1) {
            $Current = $Matches[0]
            $ParentId = [string]$Current.id
            continue
        }

        $Body = @{ displayName = $Segment }
        if (-not [string]::IsNullOrWhiteSpace($ParentId)) {
            $Body['parentFolderId'] = $ParentId
        }
        $Current = Invoke-FabricRequest -Method Post `
            -Uri "$script:FabricApi/workspaces/$WorkspaceId/folders" `
            -Token $Token -SkillName $SkillName `
            -Body ($Body | ConvertTo-Json -Compress)
        $ParentId = [string]$Current.id
        $Folders += $Current
    }
    return [pscustomobject]@{
        id          = [string]$Current.id
        displayName = $FolderPath.Trim()
    }
}

function Move-FabricItemToFolder {
    <#
    .SYNOPSIS
        Moves a Fabric item into the requested folder when needed.
    .OUTPUTS
        None.
    #>
    [CmdletBinding()]
    [OutputType([void])]
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $true)]
        [string]$ItemId,

        [Parameter(Mandatory = $false)]
        [string]$CurrentFolderId,

        [Parameter(Mandatory = $false)]
        [string]$TargetFolderId,

        [Parameter(Mandatory = $true)]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [string]$SkillName
    )

    if (
        [string]::IsNullOrWhiteSpace($TargetFolderId) -or
        $CurrentFolderId -eq $TargetFolderId
    ) {
        return
    }
    $Body = @{ targetFolderId = $TargetFolderId } | ConvertTo-Json -Compress
    $null = Invoke-FabricRequest -Method Post `
        -Uri "$script:FabricApi/workspaces/$WorkspaceId/items/$ItemId/move" `
        -Token $Token -SkillName $SkillName -Body $Body
}

function Publish-FabricDefinedItem {
    <#
    .SYNOPSIS
        Creates a Fabric item from a definition payload or updates an existing item.
    .OUTPUTS
        Deployment result metadata.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$WorkspaceId,

        [Parameter(Mandatory = $true)]
        [ValidateSet('semanticModels', 'reports')]
        [string]$Collection,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$DisplayName,

        [Parameter(Mandatory = $true)]
        [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
        [string]$PayloadPath,

        [Parameter(Mandatory = $false)]
        [string]$FolderId,

        [Parameter(Mandatory = $false)]
        [switch]$AllowOverwrite,

        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$Token,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[a-z0-9-]+$')]
        [string]$SkillName
    )

    $Payload = Get-Content -LiteralPath $PayloadPath -Raw | ConvertFrom-Json
    $Matches = @(
        Get-FabricCollection -Uri "$script:FabricApi/workspaces/$WorkspaceId/$Collection" `
            -Token $Token -SkillName $SkillName |
            Where-Object { $_.displayName -ceq $DisplayName }
    )
    if ($Matches.Count -gt 1) {
        throw "Multiple $Collection items matched exact displayName '$DisplayName'."
    }
    if ($Matches.Count -eq 1) {
        if (-not $AllowOverwrite) {
            throw "$DisplayName already exists. Pass -AllowOverwrite to replace its definition."
        }
        $Existing = $Matches[0]
        Move-FabricItemToFolder -WorkspaceId $WorkspaceId -ItemId ([string]$Existing.id) `
            -CurrentFolderId ([string]$Existing.folderId) -TargetFolderId $FolderId `
            -Token $Token -SkillName $SkillName
        $HasPlatform = @($Payload.definition.parts.path) -contains '.platform'
        $UpdateUri = "$script:FabricApi/workspaces/$WorkspaceId/$Collection/$($Existing.id)/updateDefinition"
        if ($HasPlatform) {
            $UpdateUri += '?updateMetadata=true'
        }
        $UpdateBody = @{ definition = $Payload.definition } |
            ConvertTo-Json -Depth 20 -Compress
        $null = Invoke-FabricRequest -Method Post -Uri $UpdateUri `
            -Token $Token -SkillName $SkillName -Body $UpdateBody
        return [pscustomobject]@{
            id          = [string]$Existing.id
            displayName = $DisplayName
            action      = 'updated'
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($FolderId)) {
        $Payload | Add-Member -NotePropertyName folderId -NotePropertyValue $FolderId -Force
    }
    $CreateBody = $Payload | ConvertTo-Json -Depth 20 -Compress
    $Created = Invoke-FabricRequest -Method Post `
        -Uri "$script:FabricApi/workspaces/$WorkspaceId/$Collection" `
        -Token $Token -SkillName $SkillName -Body $CreateBody
    if (-not [string]::IsNullOrWhiteSpace([string]$Created.id)) {
        return [pscustomobject]@{
            id          = [string]$Created.id
            displayName = [string]$Created.displayName
            action      = 'created'
        }
    }

    $CreatedMatches = @(
        Get-FabricCollection -Uri "$script:FabricApi/workspaces/$WorkspaceId/$Collection" `
            -Token $Token -SkillName $SkillName |
            Where-Object { $_.displayName -ceq $DisplayName }
    )
    if ($CreatedMatches.Count -ne 1) {
        throw "Created $Collection item '$DisplayName' could not be resolved."
    }
    return [pscustomobject]@{
        id          = [string]$CreatedMatches[0].id
        displayName = [string]$CreatedMatches[0].displayName
        action      = 'created'
    }
}

Export-ModuleMember -Function @(
    'Publish-FabricDefinedItem'
    'Resolve-FabricItem'
    'Resolve-FabricWorkspace'
    'Resolve-OrCreateFabricFolderPath'
)
