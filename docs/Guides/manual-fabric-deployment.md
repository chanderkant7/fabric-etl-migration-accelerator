---
title: Manual Fabric Semantic Model and Report Deployment
description: Generate, review, and manually deploy the assessment semantic model and Power BI report
ms.date: 2026-11-26
ms.topic: how-to
---

## Manual Fabric Semantic Model and Report Deployment

Use this workflow when the assessment Lakehouse and Delta tables already exist
and you want to generate, review, and deploy the semantic model and Power BI
report as separate steps.

The generation steps are local-only. Fabric changes occur only when you run a
deployment script.

## Prerequisites

Install or configure:

* PowerShell 7
* Node.js 20 or later
* Azure CLI
* `powerbi-report-author` 0.3.0 or later for optional PBIR validation
* Contributor, Member, or Admin access to the target Fabric workspace
* An existing `ETL_Migration_Assessment_Lakehouse` containing the initialized
  `etl_*` Delta tables

Confirm the required commands:

```powershell
pwsh --version
node --version
az version
powerbi-report-author --version
```

## Authenticate and resolve Fabric IDs

Sign in and select the target tenant:

```powershell
az login --allow-no-subscriptions
```

Resolve the workspace and Lakehouse IDs:

```powershell
$WorkspaceName = "fabric-dev"
$LakehouseName = "ETL_Migration_Assessment_Lakehouse"

$WorkspaceId = az rest `
  --method get `
  --resource "https://api.fabric.microsoft.com" `
  --url "https://api.fabric.microsoft.com/v1/workspaces" `
  --headers "x-ms-fabric-skill=semantic-model-authoring" `
  --query "value[?displayName=='$WorkspaceName'] | [0].id" `
  --output tsv

$LakehouseId = az rest `
  --method get `
  --resource "https://api.fabric.microsoft.com" `
  --url "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/lakehouses" `
  --headers "x-ms-fabric-skill=semantic-model-authoring" `
  --query "value[?displayName=='$LakehouseName'] | [0].id" `
  --output tsv

if (-not $WorkspaceId -or -not $LakehouseId) {
  throw "Workspace or Lakehouse resolution failed."
}
```

## Generate semantic-model code

Generate resolved TMDL source and the semantic-model REST payload:

```powershell
pwsh -NoProfile -File scripts\Generate-FabricSemanticModelArtifacts.ps1 `
  -WorkspaceId $WorkspaceId `
  -LakehouseId $LakehouseId
```

Review:

* `dist\manual-deployment\ETL Migration Assessment Model.SemanticModel`
* `dist\manual-deployment\semantic-model-payload.json`

The generated TMDL contains the selected workspace and Lakehouse IDs. It must
not contain `__FABRIC_WORKSPACE_ID__` or `__FABRIC_LAKEHOUSE_ID__`.

## Deploy the semantic model

For first-time creation, omit `-AllowOverwrite`:

```powershell
$SemanticSummary = "dist\manual-deployment\semantic-deployment-summary.json"

pwsh -NoProfile -File scripts\Deploy-FabricSemanticModel.ps1 `
  -WorkspaceId $WorkspaceId `
  -LakehouseId $LakehouseId `
  -UseAzCli `
  -OutputSummaryPath $SemanticSummary
```

For an intentional update of an existing same-named model, add
`-AllowOverwrite`:

```powershell
pwsh -NoProfile -File scripts\Deploy-FabricSemanticModel.ps1 `
  -WorkspaceId $WorkspaceId `
  -LakehouseId $LakehouseId `
  -UseAzCli `
  -AllowOverwrite `
  -OutputSummaryPath $SemanticSummary
```

Read the deployed semantic model ID:

```powershell
$SemanticModelId = (
  Get-Content -LiteralPath $SemanticSummary -Raw | ConvertFrom-Json
).semanticModelId

if (-not $SemanticModelId) {
  throw "Semantic model deployment did not return an item ID."
}
```

The semantic-model script runs a Direct Lake framing refresh by default. Pass
`-SkipRefresh` only when refresh must be deferred.

## Generate Power BI report code

Generate PBIR/PBIP source and the report REST payload after the semantic model
ID is available:

```powershell
pwsh -NoProfile -File scripts\Generate-FabricPowerBIReportArtifacts.ps1 `
  -WorkspaceName $WorkspaceName `
  -SemanticModelId $SemanticModelId
```

Review:

* `dist\manual-deployment\ETL Migration Assessment.pbip`
* `dist\manual-deployment\ETL Migration Assessment.Report`
* `dist\manual-deployment\powerbi-report-payload.json`

The PBIP source and REST payload both use a `byConnection` reference to the
deployed semantic model ID. Power BI Desktop cannot load this Direct Lake model
locally through a `byPath` reference. Deploy the semantic model to Fabric
before opening the generated PBIP.

Validate the generated report before deployment:

```powershell
powerbi-report-author validate `
  "dist\manual-deployment\ETL Migration Assessment.Report"
```

Do not continue if validation reports errors.

## Deploy the Power BI report

For first-time creation, omit `-AllowOverwrite`:

```powershell
$ReportSummary = "dist\manual-deployment\report-deployment-summary.json"

pwsh -NoProfile -File scripts\Deploy-FabricPowerBIReport.ps1 `
  -WorkspaceId $WorkspaceId `
  -SemanticModelId $SemanticModelId `
  -UseAzCli `
  -OutputSummaryPath $ReportSummary
```

For an intentional report update, add `-AllowOverwrite`:

```powershell
pwsh -NoProfile -File scripts\Deploy-FabricPowerBIReport.ps1 `
  -WorkspaceId $WorkspaceId `
  -SemanticModelId $SemanticModelId `
  -UseAzCli `
  -AllowOverwrite `
  -OutputSummaryPath $ReportSummary
```

Open the deployed report URL:

```powershell
$ReportResult = Get-Content -LiteralPath $ReportSummary -Raw |
  ConvertFrom-Json

Start-Process $ReportResult.reportUrl
```

## Regenerate existing local output

Generation stops when output already exists. Add `-Force` only when you intend
to replace the generated files:

```powershell
pwsh -NoProfile -File scripts\Generate-FabricSemanticModelArtifacts.ps1 `
  -WorkspaceId $WorkspaceId `
  -LakehouseId $LakehouseId `
  -Force

pwsh -NoProfile -File scripts\Generate-FabricPowerBIReportArtifacts.ps1 `
  -WorkspaceName $WorkspaceName `
  -SemanticModelId $SemanticModelId `
  -Force
```

`-Force` affects generated files under `dist\manual-deployment`. It does not
authorize replacement of Fabric items. Remote replacement still requires
`-AllowOverwrite` on the corresponding deployment script.

## Generate a Power BI Desktop DirectQuery project

Direct Lake semantic models must exist in Fabric before Power BI Desktop can
open a report connected to them. To create and publish the semantic model from
Power BI Desktop instead, generate the DirectQuery variant:

```powershell
pwsh -NoProfile -File scripts\Generate-FabricDirectQueryPowerBIProject.ps1 `
  -SqlEndpoint "bzz7mf3ncmce5ovoqo4brjwqi4-ctn7lkrvszkejfme6ehmqskeqm.datawarehouse.fabric.microsoft.com" `
  -DatabaseName "ETL_Migration_Assessment_Lakehouse"
```

The script writes a complete local project under
`dist\directquery-desktop`:

* `ETL Migration Assessment.pbip`
* `ETL Migration Assessment Model.SemanticModel`
* `ETL Migration Assessment.Report`

Every semantic-model table uses an M partition with `mode: directQuery` and
`Sql.Database`. The report retains a local `byPath` reference to the generated
semantic model.

The generator validates PBIR automatically when `powerbi-report-author` is
available. Without it, generation continues with a warning. Pass
`-RequireValidation` when a missing validator must fail the command.

Open the generated `.pbip` in Power BI Desktop. Sign in with an organizational
account that can query the Lakehouse SQL analytics endpoint, accept the
DirectQuery connection, verify the visuals, and publish the project to the
target workspace.

## Troubleshooting

| Symptom | Resolution |
|---------|------------|
| Workspace or Lakehouse ID is empty | Confirm the exact display name and the signed-in tenant |
| Same-named semantic model or report already exists | Review the existing item, then rerun with `-AllowOverwrite` |
| Report visuals are blank | Confirm the generated report payload references the newly deployed semantic model ID |
| Semantic-model refresh fails | Confirm the Lakehouse tables exist and match the checked-in TMDL table mappings |
| Generated output already exists | Review it, then use `-Force` only when replacement is intended |
| PBIR validation fails | Correct every reported schema or binding error before deploying |

