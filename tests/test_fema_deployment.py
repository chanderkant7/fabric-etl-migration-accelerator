"""Hermetic tests for FEMA Fabric workspace deployment and schema bootstrap."""

import ast
import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
GUID_WS = "11111111-1111-4111-8111-111111111111"
GUID_FOLDER = "77777777-7777-4777-8777-777777777777"
GUID_LH = "22222222-2222-4222-8222-222222222222"
GUID_ENV = "33333333-3333-4333-8333-333333333333"
GUID_SM = "44444444-4444-4444-8444-444444444444"


def _load_notebook(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_assessment_notebook_supports_schema_bootstrap_and_matches_tmdl() -> None:
    nb_path = ROOT / "notebooks" / "fabric-portfolio-assessment.ipynb"
    nb = _load_notebook(nb_path)
    cells = nb["cells"]
    assert isinstance(cells, list)

    code_by_id: dict[str, str] = {}
    for cell in cells:
        assert isinstance(cell, dict)
        if cell.get("cell_type") == "code":
            source = "".join(cell["source"])
            ast.parse(source)
            cell_id = str(cell.get("id", ""))
            code_by_id[cell_id] = source

    assert "INITIALIZE_SCHEMA_ONLY = False" in code_by_id["parameters"]
    assert "CONFIRM_FABRIC_FIT = False" in code_by_id["parameters"]
    assert "REFRESH_SEMANTIC_MODEL = True" in code_by_id["parameters"]
    for guarded_id in ("preview", "assess"):
        assert "INITIALIZE_SCHEMA_ONLY" in code_by_id[guarded_id]
    for source in code_by_id.values():
        assert "displayHTML" not in source

    persist_src = code_by_id["persist-delta"]
    assert "mode('ignore').saveAsTable(table_name)" in persist_src
    assert "_refresh_direct_lake_model" in persist_src
    assert "'static_mapping'" in persist_src
    assert "'compiler_confirmed'" in persist_src
    assert "EstimatedEffortDays" in persist_src

    # Verify every TMDL table column matches the Spark StructType schema in persist-delta.
    tmdl_dir = (
        ROOT
        / "powerbi"
        / "ETL Migration Assessment Model.SemanticModel"
        / "definition"
        / "tables"
    )
    table_map: dict[str, list[str]] = {}
    for tmdl_file in sorted(tmdl_dir.glob("*.tmdl")):
        text = tmdl_file.read_text(encoding="utf-8")
        entity_match = re.search(r"entityName:\s*(\S+)", text)
        assert entity_match is not None
        entity_name = entity_match.group(1)
        columns = re.findall(r"^\tcolumn\s+([A-Za-z0-9_]+)", text, flags=re.MULTILINE)
        table_map[entity_name] = columns

    assert len(table_map) == 8
    for entity_name, columns in table_map.items():
        assert f"'{entity_name}'" in persist_src
        for col in columns:
            assert f"StructField('{col}'" in persist_src


def test_deploy_fema_notebook_is_valid_nbformat() -> None:
    nb_path = ROOT / "notebooks" / "Deploy_FEMA.ipynb"
    nb = _load_notebook(nb_path)
    assert nb["nbformat"] == 4
    cells = nb["cells"]
    assert isinstance(cells, list)
    code_cells = [c for c in cells if isinstance(c, dict) and c.get("cell_type") == "code"]
    assert len(code_cells) >= 3
    combined = []
    for cell in code_cells:
        src = "".join(cell["source"])
        ast.parse(src)
        combined.append(src)
    full_code = "\n".join(combined)
    assert "INITIALIZE_SCHEMA_ONLY" in full_code
    assert "semanticmodelid=" in full_code


def test_build_powerbi_report_and_notebook_payload_builders(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to test payload builders")

    report_out = tmp_path / "report-payload.json"
    rpt_proc = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "build-powerbi-report-payload.mjs"),
            "--semantic-model-id",
            GUID_SM,
            "--out",
            str(report_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rpt_proc.returncode == 0, rpt_proc.stderr
    rpt_payload = json.loads(report_out.read_text(encoding="utf-8"))
    assert rpt_payload["definition"]["format"] == "PBIR"
    parts = {part["path"]: part["payload"] for part in rpt_payload["definition"]["parts"]}
    assert not any(path == ".pbi" or path.startswith(".pbi/") for path in parts)
    pbir = json.loads(base64.b64decode(parts["definition.pbir"]).decode("utf-8"))
    assert pbir["datasetReference"] == {
        "byConnection": {"connectionString": f"semanticmodelid={GUID_SM}"}
    }

    nb_out = tmp_path / "notebook-payload.json"
    nb_proc = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "build-notebook-payload.mjs"),
            "--workspace-id",
            GUID_WS,
            "--lakehouse-id",
            GUID_LH,
            "--lakehouse-name",
            "ETL_Migration_Assessment_Lakehouse",
            "--environment-id",
            GUID_ENV,
            "--out",
            str(nb_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert nb_proc.returncode == 0, nb_proc.stderr
    nb_payload = json.loads(nb_out.read_text(encoding="utf-8"))
    assert nb_payload["definition"]["format"] == "ipynb"
    nb_parts = {part["path"]: part["payload"] for part in nb_payload["definition"]["parts"]}
    nb_doc = json.loads(base64.b64decode(nb_parts["notebook-content.ipynb"]).decode("utf-8"))
    assert nb_doc["metadata"]["dependencies"] == {
        "lakehouse": {
            "default_lakehouse": GUID_LH,
            "default_lakehouse_name": "ETL_Migration_Assessment_Lakehouse",
            "default_lakehouse_workspace_id": GUID_WS,
            "known_lakehouses": [{"id": GUID_LH}],
        },
        "environment": {
            "environmentId": GUID_ENV,
            "workspaceId": GUID_WS,
        },
    }


def test_build_fema_bundle_creates_clean_manifest(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to test bundle builder")

    bundle_dir = ROOT / "build" / f"test-fema-bundle-{os.getpid()}"
    try:
        proc = subprocess.run(
            [
                "node",
                str(ROOT / "scripts" / "build-fema-bundle.mjs"),
                "--out",
                str(bundle_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        manifest = json.loads((bundle_dir / "bundle-manifest.json").read_text(encoding="utf-8"))
        files = set(manifest["filesIncluded"])
        assert "scripts/deployment_order.json" in files
        assert "scripts/Deploy-AcceleratorWorkspace.ps1" in files
        assert "scripts/Deploy-FabricPowerBIReport.ps1" in files
        assert "scripts/Deploy-FabricSemanticModel.ps1" in files
        assert "scripts/FabricDeployment.psm1" in files
        assert "scripts/Generate-FabricDirectQueryPowerBIProject.ps1" in files
        assert "scripts/Generate-FabricPowerBIReportArtifacts.ps1" in files
        assert "scripts/Generate-FabricSemanticModelArtifacts.ps1" in files
        assert "scripts/generate-powerbi-report.mjs" in files
        assert "notebooks/Deploy_FEMA.ipynb" in files
        assert "notebooks/fabric-portfolio-assessment.ipynb" in files
        assert "powerbi/ETL Migration Assessment.pbip" in files
        assert not any(".pbi/" in item or item.endswith(".env") for item in files)

        pwsh = shutil.which("pwsh")
        if pwsh is not None:
            generated_dir = tmp_path / "bundled-directquery"
            generate = subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(
                        bundle_dir
                        / "scripts"
                        / "Generate-FabricDirectQueryPowerBIProject.ps1"
                    ),
                    "-SqlEndpoint",
                    "example.datawarehouse.fabric.microsoft.com",
                    "-DatabaseName",
                    "SyntheticLakehouse",
                    "-OutputDirectory",
                    str(generated_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert generate.returncode == 0, generate.stderr
            assert (generated_dir / "ETL Migration Assessment.pbip").is_file()
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)


def test_focused_assessment_stack_installer_uses_prebuilt_wheels(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise the focused installer")

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name in (
        "etl_migration_core-1.0.0-py3-none-any.whl",
        "etl_portfolio_assessment-1.0.0-py3-none-any.whl",
        "etl_target_fabric-1.0.0-py3-none-any.whl",
    ):
        (wheels / name).write_bytes(b"PK\x03\x04synthetic-wheel")
    summary = tmp_path / "summary.json"

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Deploy-FabricAssessmentStack.ps1"),
            "-WorkspaceName",
            "Synthetic Workspace",
            "-WheelDirectory",
            str(wheels),
            "-OutputSummaryPath",
            str(summary),
            "-DryRun",
            "-UseBrowserLogin",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "etl_migration_core-1.0.0-py3-none-any.whl" in proc.stdout
    result = json.loads(summary.read_text(encoding="utf-8-sig"))
    assert result["mode"] == "dry-run"
    assert result["deployFabricApp"] is False
    assert result["payloadPartsValidated"] is True
    assert Path(result["wheel"]).name == "etl_target_fabric-1.0.0-py3-none-any.whl"


def test_standalone_power_bi_deployment_scripts_support_dry_run(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise standalone deployments")

    cases = (
        (
            "Deploy-FabricSemanticModel.ps1",
            "semantic-summary.json",
            "semanticModelName",
            "ETL Migration Assessment Model",
        ),
        (
            "Deploy-FabricPowerBIReport.ps1",
            "report-summary.json",
            "reportName",
            "ETL Migration Assessment",
        ),
    )
    for script_name, summary_name, result_key, expected_name in cases:
        summary_path = tmp_path / summary_name
        proc = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-File",
                str(ROOT / "scripts" / script_name),
                "-WorkspaceName",
                "Synthetic Workspace",
                "-OutputSummaryPath",
                str(summary_path),
                "-DryRun",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        assert summary["mode"] == "dry-run"
        assert summary["payloadValidated"] is True
        assert summary[result_key] == expected_name


def test_manual_deployment_artifact_generators_emit_source_and_payloads(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise artifact generation")

    semantic = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Generate-FabricSemanticModelArtifacts.ps1"),
            "-WorkspaceId",
            GUID_WS,
            "-LakehouseId",
            GUID_LH,
            "-OutputDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert semantic.returncode == 0, semantic.stderr
    semantic_dir = tmp_path / "ETL Migration Assessment Model.SemanticModel"
    model_text = (semantic_dir / "definition" / "model.tmdl").read_text(
        encoding="utf-8"
    )
    assert "__FABRIC_WORKSPACE_ID__" not in model_text
    assert "__FABRIC_LAKEHOUSE_ID__" not in model_text
    semantic_payload = json.loads(
        (tmp_path / "semantic-model-payload.json").read_text(encoding="utf-8")
    )
    assert semantic_payload["definition"]["format"] == "TMDL"

    report = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Generate-FabricPowerBIReportArtifacts.ps1"),
            "-SemanticModelId",
            GUID_SM,
            "-WorkspaceName",
            "Synthetic Workspace",
            "-OutputDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.returncode == 0, report.stderr
    report_dir = tmp_path / "ETL Migration Assessment.Report"
    source_binding = json.loads(
        (report_dir / "definition.pbir").read_text(encoding="utf-8")
    )
    assert source_binding["datasetReference"] == {
        "byConnection": {
            "connectionString": (
                'Data Source="powerbi://api.powerbi.com/v1.0/myorg/'
                'Synthetic Workspace";'
                'Initial Catalog="ETL Migration Assessment Model";'
                "Integrated Security=ClaimsToken;"
                f"semanticModelId={GUID_SM}"
            )
        }
    }
    report_payload = json.loads(
        (tmp_path / "powerbi-report-payload.json").read_text(encoding="utf-8")
    )
    parts = {
        part["path"]: part["payload"]
        for part in report_payload["definition"]["parts"]
    }
    deployed_binding = json.loads(
        base64.b64decode(parts["definition.pbir"]).decode("utf-8")
    )
    assert deployed_binding["datasetReference"] == {
        "byConnection": {"connectionString": f"semanticmodelid={GUID_SM}"}
    }
    assert (tmp_path / "ETL Migration Assessment.pbip").is_file()


def test_directquery_generator_creates_local_desktop_model(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required")

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Generate-FabricDirectQueryPowerBIProject.ps1"),
            "-SqlEndpoint",
            "example.datawarehouse.fabric.microsoft.com",
            "-DatabaseName",
            "SyntheticLakehouse",
            "-OutputDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    model_dir = tmp_path / "ETL Migration Assessment Model.SemanticModel"
    model_text = (model_dir / "definition" / "model.tmdl").read_text(
        encoding="utf-8"
    )
    assert 'expression Server = "example.datawarehouse.fabric.microsoft.com"' in model_text
    assert 'expression Database = "SyntheticLakehouse"' in model_text
    assert "AzureStorage.DataLake" not in model_text

    table_files = sorted((model_dir / "definition" / "tables").glob("*.tmdl"))
    assert len(table_files) == 8
    for table_file in table_files:
        table_text = table_file.read_text(encoding="utf-8")
        assert "mode: directQuery" in table_text
        assert 'Sql.Database(#"Server", #"Database")' in table_text
        assert 'Schema="dbo"' in table_text
        assert "mode: directLake" not in table_text

    report_binding = json.loads(
        (
            tmp_path / "ETL Migration Assessment.Report" / "definition.pbir"
        ).read_text(encoding="utf-8")
    )
    assert report_binding["datasetReference"] == {
        "byPath": {"path": "../ETL Migration Assessment Model.SemanticModel"}
    }
    assert (tmp_path / "ETL Migration Assessment.pbip").is_file()


def test_directquery_generator_refuses_checked_in_source_output() -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required")

    source_model = (
        ROOT
        / "powerbi"
        / "ETL Migration Assessment Model.SemanticModel"
        / "definition"
        / "model.tmdl"
    )
    original_model = source_model.read_bytes()
    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Generate-FabricDirectQueryPowerBIProject.ps1"),
            "-SqlEndpoint",
            "example.datawarehouse.fabric.microsoft.com",
            "-OutputDirectory",
            str(ROOT / "powerbi"),
            "-Force",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "cannot overwrite checked-in source" in proc.stderr
    assert source_model.read_bytes() == original_model


def test_report_generator_escapes_connection_string_display_names(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    author = shutil.which("powerbi-report-author")
    if pwsh is None or author is None:
        pytest.skip("PowerShell 7 and powerbi-report-author are required")

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Generate-FabricPowerBIReportArtifacts.ps1"),
            "-WorkspaceName",
            "Finance;Prod",
            "-SemanticModelName",
            'Assessment "Model";Prod',
            "-SemanticModelId",
            GUID_SM,
            "-OutputDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report_dir = tmp_path / "ETL Migration Assessment.Report"
    pbir = json.loads((report_dir / "definition.pbir").read_text(encoding="utf-8-sig"))
    connection = pbir["datasetReference"]["byConnection"]["connectionString"]
    assert connection.startswith(
        'Data Source="powerbi://api.powerbi.com/v1.0/myorg/Finance;Prod";'
    )
    assert 'Initial Catalog=\'Assessment "Model";Prod\';' in connection

    validation = subprocess.run(
        [author, "validate", str(report_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validation.returncode == 0, validation.stderr


@pytest.mark.parametrize("login_mode", ["browser", "fallback", "cli"])
@pytest.mark.parametrize("original_broker_setting", [None, "true"])
def test_fabric_api_user_login_is_reused_for_fabric_and_power_bi_tokens(
    tmp_path: Path, login_mode: str, original_broker_setting: str | None,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise browser login")

    calls_path = tmp_path / "az-calls.json"
    module_path = ROOT / "scripts" / "FabricApi.psm1"
    login_argument = {
        "browser": "-UseBrowserLogin",
        "fallback": "-AllowAzCliFallback",
        "cli": "-UseAzCli",
    }[login_mode]
    broker_setup = (
        "if (Test-Path 'Env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS') { "
        "Remove-Item -LiteralPath 'Env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS' }"
        if original_broker_setting is None
        else "$env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS = 'true'"
    )
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:AZURE_CLIENT_ID = ''
$env:AZURE_TENANT_ID = ''
{broker_setup}
$global:AzCalls = [System.Collections.Generic.List[object]]::new()
$global:BrokerDuringLogin = $null
function global:az {{
    $global:AzCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'login') {{
        $global:BrokerDuringLogin = $env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS
    }}
    if ($args[0] -eq 'account') {{
        'synthetic-user-token'
    }}
}}
Import-Module '{module_path.as_posix()}' -Force
$FabricToken = Get-FabricAccessToken {login_argument} -TenantId 'tenant.example.com'
$PowerBiToken = Get-FabricAccessToken {login_argument} -TenantId 'tenant.example.com' `
    -Resource 'https://analysis.windows.net/powerbi/api'
[pscustomobject]@{{
    FabricToken = $FabricToken
    PowerBiToken = $PowerBiToken
    IsWindows = $IsWindows
    BrokerDuringLogin = $global:BrokerDuringLogin
    BrokerAfterLogin = $env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS
    Calls = @($global:AzCalls)
}} | ConvertTo-Json -Depth 8 -Compress |
    Set-Content -LiteralPath '{calls_path.as_posix()}' -Encoding utf8
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(calls_path.read_text(encoding="utf-8-sig"))
    assert result["FabricToken"] == "synthetic-user-token"
    assert result["PowerBiToken"] == "synthetic-user-token"
    assert result["BrokerAfterLogin"] == original_broker_setting
    token_calls = result["Calls"]
    if login_mode != "cli":
        assert token_calls[0] == [
            "login",
            "--allow-no-subscriptions",
            "--output",
            "none",
            "--tenant",
            "tenant.example.com",
        ]
        expected_broker = "false" if result["IsWindows"] else original_broker_setting
        assert result["BrokerDuringLogin"] == expected_broker
        token_calls = token_calls[1:]
    else:
        assert result["BrokerDuringLogin"] is None
    assert token_calls == [[
        "account",
        "get-access-token",
        "--resource",
        "https://api.fabric.microsoft.com",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ], [
        "account",
        "get-access-token",
        "--resource",
        "https://analysis.windows.net/powerbi/api",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]]


@pytest.mark.parametrize("failure_stage", ["login", "token"])
def test_fabric_api_browser_fallback_surfaces_authentication_failure(
    tmp_path: Path, failure_stage: str,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise authentication failure")

    calls_path = tmp_path / "auth-failure.json"
    harness = f"""
$ErrorActionPreference = 'Stop'
$env:AZURE_CLIENT_ID = ''
$env:AZURE_TENANT_ID = ''
$env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS = 'true'
$global:AzCalls = [System.Collections.Generic.List[object]]::new()
function global:az {{
    $global:AzCalls.Add(@($args))
    $Fail = (
        ('{failure_stage}' -eq 'login' -and $args[0] -eq 'login') -or
        ('{failure_stage}' -eq 'token' -and $args[0] -eq 'account')
    )
    $global:LASTEXITCODE = if ($Fail) {{ 1 }} else {{ 0 }}
    if ($args[0] -eq 'account' -and -not $Fail) {{ 'synthetic-token' }}
}}
Import-Module '{(ROOT / "scripts" / "FabricApi.psm1").as_posix()}' -Force
$AuthError = $null
try {{
    Get-FabricAccessToken -AllowAzCliFallback | Out-Null
}}
catch {{
    $AuthError = $_.Exception.Message
}}
[pscustomobject]@{{
    Error = $AuthError
    BrokerAfterLogin = $env:AZURE_CORE_ENABLE_BROKER_ON_WINDOWS
    Calls = @($global:AzCalls)
}} | ConvertTo-Json -Depth 8 -Compress |
    Set-Content -LiteralPath '{calls_path.as_posix()}' -Encoding utf8
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(calls_path.read_text(encoding="utf-8-sig"))
    assert result["BrokerAfterLogin"] == "true"
    if failure_stage == "login":
        assert "browser sign-in failed" in result["Error"]
        assert [call[0] for call in result["Calls"]] == ["login"]
    else:
        assert "Failed to acquire an access token via Azure CLI" in result["Error"]
        assert [call[0] for call in result["Calls"]] == ["login", "account"]


def test_focused_assessment_stack_rejects_conflicting_user_login_modes(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise login validation")

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    for name in (
        "etl_migration_core-1.0.0-py3-none-any.whl",
        "etl_portfolio_assessment-1.0.0-py3-none-any.whl",
        "etl_target_fabric-1.0.0-py3-none-any.whl",
    ):
        (wheels / name).write_bytes(b"PK\x03\x04synthetic-wheel")

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Deploy-FabricAssessmentStack.ps1"),
            "-WorkspaceName",
            "Synthetic Workspace",
            "-WheelDirectory",
            str(wheels),
            "-DryRun",
            "-UseAzCli",
            "-UseBrowserLogin",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "Use either -UseAzCli or -UseBrowserLogin, not both." in proc.stderr


def test_focused_assessment_stack_installer_rejects_incomplete_wheels(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise the focused installer")

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "etl_migration_core-1.0.0-py3-none-any.whl").write_bytes(
        b"PK\x03\x04synthetic-wheel"
    )

    proc = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "Deploy-FabricAssessmentStack.ps1"),
            "-WorkspaceName",
            "Synthetic Workspace",
            "-WheelDirectory",
            str(wheels),
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "Required wheel missing" in proc.stderr
    assert "etl_portfolio_assessment-*.whl" in proc.stderr


@pytest.mark.parametrize(
    ("cached_count", "rebuild", "skip_build", "dry_run", "expected_builds"),
    [
        (0, False, False, False, 1),
        (3, False, False, False, 0),
        (2, False, False, False, 1),
        (3, True, False, False, 1),
        (3, False, True, False, 0),
        (3, True, False, True, 0),
    ],
    ids=["cold-cache", "warm-cache", "partial-cache", "force-rebuild", "skip", "dry-run"],
)
def test_workspace_wheel_resolution_builds_only_when_needed(
    tmp_path: Path,
    cached_count: int,
    rebuild: bool,
    skip_build: bool,
    dry_run: bool,
    expected_builds: int,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise wheel resolution")

    result_path = tmp_path / "resolved-wheels.json"
    script_path = ROOT / "scripts" / "Deploy-AcceleratorWorkspace.ps1"
    wheel_names = (
        "etl_migration_core-1.0.0-py3-none-any.whl",
        "etl_portfolio_assessment-1.0.0-py3-none-any.whl",
        "etl_target_fabric-1.0.0-py3-none-any.whl",
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in wheel_names[:cached_count]:
        (dist / name).write_bytes(b"cached-wheel")
    unrelated_wheel = dist / "unrelated_dependency-1.0.0-py3-none-any.whl"
    unrelated_wheel.write_bytes(b"unrelated-wheel")
    rebuild_argument = "-Rebuild" if rebuild else ""
    harness = f"""
$ErrorActionPreference = 'Stop'
$RepoRoot = '{tmp_path.as_posix()}'
$global:BuildCalls = [System.Collections.Generic.List[object]]::new()
function global:python {{
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $global:BuildCalls.Add(@($Arguments))
    $WheelDirectory = $Arguments[$Arguments.IndexOf('--wheel-dir') + 1]
    New-Item -ItemType Directory -Path $WheelDirectory -Force | Out-Null
    @(
        'etl_migration_core-1.0.0-py3-none-any.whl',
        'etl_portfolio_assessment-1.0.0-py3-none-any.whl',
        'etl_target_fabric-1.0.0-py3-none-any.whl'
    ) | ForEach-Object {{
        [IO.File]::WriteAllBytes((Join-Path $WheelDirectory $_), [byte[]](80, 75, 3, 4))
    }}
    Write-Output 'Looking in indexes: https://packagefeedproxy.microsoft.io/pypi/simple'
    $global:LASTEXITCODE = 0
}}

$Tokens = $null
$Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile(
    '{script_path.as_posix()}',
    [ref]$Tokens,
    [ref]$Errors
)
if ($Errors.Count -gt 0) {{ throw $Errors[0] }}
$FunctionAst = $Ast.Find(
    {{
        param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq 'Resolve-WheelArtifacts'
    }},
    $true
)
Invoke-Expression $FunctionAst.Extent.Text

$ResolvedWheels = @(Resolve-WheelArtifacts {rebuild_argument} `
    -SkipBuild:${str(skip_build).lower()} `
    -IsDryRun:${str(dry_run).lower()})
[pscustomobject]@{{
    Wheels = $ResolvedWheels
    ReusedWheels = @(Resolve-WheelArtifacts)
    BuildCalls = @($global:BuildCalls)
}} | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath '{result_path.as_posix()}' -Encoding utf8
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    resolved = result["Wheels"]
    assert len(resolved) == 3
    assert all(Path(path).is_file() for path in resolved)
    assert {Path(path).name for path in resolved} == set(wheel_names)
    assert result["ReusedWheels"] == resolved
    assert len(result["BuildCalls"]) == expected_builds
    assert unrelated_wheel.read_bytes() == b"unrelated-wheel"
    if not expected_builds:
        assert all(Path(path).read_bytes() == b"cached-wheel" for path in resolved)


@pytest.mark.parametrize("conflict", ["-SkipBuild", "-ExplicitPath 'unused.whl'"])
def test_workspace_wheel_rebuild_rejects_conflicting_options(conflict: str) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise wheel resolution")

    script_path = ROOT / "scripts" / "Deploy-AcceleratorWorkspace.ps1"
    harness = f"""
$ErrorActionPreference = 'Stop'
$Tokens = $null
$Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile(
    '{script_path.as_posix()}', [ref]$Tokens, [ref]$Errors
)
$FunctionAst = $Ast.Find({{
    param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq 'Resolve-WheelArtifacts'
}}, $true)
Invoke-Expression $FunctionAst.Extent.Text
Resolve-WheelArtifacts -Rebuild {conflict}
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "-RebuildWheels cannot be combined with -SkipWheelBuild or -WheelPath" in proc.stderr


def _run_workspace_installer(
    tmp_path: Path,
    *,
    existing_items: bool,
    staged_libraries_missing: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise Deploy-AcceleratorWorkspace.ps1")

    deployment_root = tmp_path / "deployment"
    for directory in ("scripts", "powerbi"):
        shutil.copytree(
            ROOT / directory,
            deployment_root / directory,
            ignore=shutil.ignore_patterns(".pbi"),
        )
    notebook_dir = deployment_root / "notebooks"
    notebook_dir.mkdir()
    shutil.copy2(
        ROOT / "notebooks" / "fabric-portfolio-assessment.ipynb",
        notebook_dir / "fabric-portfolio-assessment.ipynb",
    )
    dist = deployment_root / "dist"
    dist.mkdir()
    for name in (
        "etl_migration_core-1.0.0-py3-none-any.whl",
        "etl_portfolio_assessment-1.0.0-py3-none-any.whl",
        "etl_target_fabric-1.0.0-py3-none-any.whl",
    ):
        (dist / name).write_bytes(b"PK\x03\x04synthetic-wheel")
    summary_path = tmp_path / "summary.json"
    calls_path = tmp_path / "calls.json"
    auth_calls_path = tmp_path / "auth-calls.json"
    missing_staged_libraries = (
        not existing_items
        if staged_libraries_missing is None
        else staged_libraries_missing
    )

    harness = f"""
$ErrorActionPreference = 'Stop'
$env:FABRIC_WORKSPACE_ID = '99999999-9999-4999-8999-999999999999'
$env:AZURE_CLIENT_ID = ''
$env:AZURE_TENANT_ID = ''
$global:ExistingItems = {'$true' if existing_items else '$false'}
$global:MissingStagedLibraries = {'$true' if missing_staged_libraries else '$false'}
$global:Calls = [System.Collections.Generic.List[object]]::new()
$global:AzCalls = [System.Collections.Generic.List[object]]::new()
$NbId = '55555555-5555-4555-8555-555555555555'
$RptId = '66666666-6666-4666-8666-666666666666'

function global:Import-Module {{
    param($Name, [switch]$Force)
    if ([IO.Path]::GetFileName($Name) -ne 'FabricApi.psm1') {{ throw "Unexpected module: $Name" }}
    Microsoft.PowerShell.Core\\Import-Module $Name -Force:$Force -Global `
        -Function Get-FabricAccessToken
}}
function global:az {{
    $global:AzCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'account') {{ 'synthetic-token' }}
}}
function global:python {{
    throw 'A deployment with cached wheels must not invoke Python.'
}}
function global:Invoke-FabricFileUpload {{
    param($Uri, $FilePath, $Token, $SkillName, $Mode, $FormFieldName)
    $global:Calls.Add([pscustomobject]@{{ Method = 'UPLOAD'; Uri = $Uri; Mode = $Mode }})
    return $null
}}
function global:Invoke-FabricRequest {{
    param($Method, $Uri, $Token, $SkillName, $Body, [switch]$FetchResult, [switch]$NoLroPoll)
    $Payload = if ($Body) {{ $Body | ConvertFrom-Json }} else {{ $null }}
    $global:Calls.Add([pscustomobject]@{{ Method = $Method; Uri = $Uri; Body = $Payload }})

    if ($Method -eq 'Get' -and $Uri -match '/workspaces$') {{
        return @{{
            value = @(@{{ id = '{GUID_WS}'; displayName = 'Synthetic Workspace' }})
            continuationUri = ''
        }}
    }}
    if ($Method -eq 'Get' -and $Uri -match '/folders$') {{
        if (-not $global:ExistingItems) {{
            return @{{ value = @(); continuationUri = '' }}
        }}
        return @{{
            value = @(@{{ id = '{GUID_FOLDER}'; displayName = 'FEMA' }})
            continuationUri = ''
        }}
    }}
    if ($Method -eq 'Get' -and $Uri -match '/lakehouses/{GUID_LH}$') {{
        return @{{
            id = '{GUID_LH}'
            properties = @{{}}
        }}
    }}
    if ($Method -eq 'Get' -and $Uri -match '/refreshes') {{
        return @{{
            value = @(@{{ status = 'Completed' }})
        }}
    }}
    if ($Method -eq 'Get' -and $Uri -match '/environments/{GUID_ENV}/staging/libraries$') {{
        if ($global:MissingStagedLibraries) {{
            $MissingLibraries = (
                'Fabric request failed (404): ' +
                '{{"errorCode":"EnvironmentLibrariesNotFound",' +
                '"message":"This environment does not have any staged libraries. ' +
                'Please upload libraries."}}'
            )
            throw $MissingLibraries
        }}
        return @{{
            customLibraries = @{{
                wheelFiles = @('fabric_etl_migration_accelerator-1.0.0-py3-none-any.whl')
            }}
        }}
    }}
    if ($Method -eq 'Get' -and $Uri -match '/environments/{GUID_ENV}$') {{
        return @{{
            id = '{GUID_ENV}'
            properties = @{{ publishDetails = @{{ state = 'Success' }} }}
        }}
    }}
    if ($Method -eq 'Delete' -and $Uri -match '/environments/{GUID_ENV}/staging/libraries') {{
        return @{{}}
    }}
    if (
        $Method -eq 'Get' -and
        $Uri -match '/(lakehouses|environments|notebooks|semanticModels|reports)$'
    ) {{
        $Col = $Matches[1]
        if (-not $global:ExistingItems) {{
            return @{{ value = @(); continuationUri = '' }}
        }}
        $Map = @{{
            lakehouses = @{{
                id = '{GUID_LH}'; displayName = 'ETL_Migration_Assessment_Lakehouse'
            }}
            environments = @{{
                id = '{GUID_ENV}'; displayName = 'ETL_Migration_Assessment_Env'
            }}
            notebooks = @{{
                id = $NbId; displayName = 'ETL Migration Assessment'
            }}
            semanticModels = @{{
                id = '{GUID_SM}'; displayName = 'ETL Migration Assessment Model'
            }}
            reports = @{{
                id = $RptId; displayName = 'ETL Migration Assessment'
            }}
        }}
        return @{{ value = @($Map[$Col]); continuationUri = '' }}
    }}
    if ($Method -eq 'Patch') {{
        return @{{}}
    }}
    if ($Method -eq 'Post') {{
        if ($Uri -match '/refreshes$') {{ return @{{}} }}
        if ($Uri -match '/folders$') {{
            return @{{ id = '{GUID_FOLDER}'; displayName = $Payload.displayName }}
        }}
        if ($Uri -match '/items/[^/]+/move$') {{ return @{{ value = @() }} }}
        if ($Uri -match '/staging/publish') {{ return @{{}} }}
        if ($Uri -match '/jobs/execute/instances') {{ return @{{ status = 'Completed' }} }}
        if ($Uri -match '/updateDefinition') {{ return @{{}} }}
        if ($Uri -match '/lakehouses$') {{
            return @{{ id = '{GUID_LH}'; displayName = $Payload.displayName }}
        }}
        if ($Uri -match '/environments$') {{
            return @{{ id = '{GUID_ENV}'; displayName = $Payload.displayName }}
        }}
        if ($Uri -match '/notebooks$') {{
            return @{{ id = $NbId; displayName = $Payload.displayName }}
        }}
        if ($Uri -match '/semanticModels$') {{
            return @{{ id = '{GUID_SM}'; displayName = $Payload.displayName }}
        }}
        if ($Uri -match '/reports$') {{
            return @{{ id = $RptId; displayName = $Payload.displayName }}
        }}
    }}
    throw "Unhandled mocked Fabric call: $Method $Uri"
}}

& '{(deployment_root / "scripts" / "Deploy-AcceleratorWorkspace.ps1").as_posix()}' `
    -WorkspaceName 'Synthetic Workspace' `
    -SkipFabricApp `
    -OutputSummaryPath '{summary_path.as_posix()}'

$global:Calls | ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath '{calls_path.as_posix()}' -Encoding utf8
$global:AzCalls | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath '{auth_calls_path.as_posix()}' -Encoding utf8
"""
    return subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
    )


def test_workspace_installer_fresh_and_idempotent_upgrade(tmp_path: Path) -> None:
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh = _run_workspace_installer(fresh_dir, existing_items=False)
    assert fresh.returncode == 0, fresh.stderr
    assert "Reusing cached workspace wheels" in fresh.stdout
    fresh_summary = json.loads((fresh_dir / "summary.json").read_text(encoding="utf-8"))
    assert fresh_summary["mode"] == "deployed"
    assert fresh_summary["workspaceId"] == GUID_WS
    assert fresh_summary["folderId"] == GUID_FOLDER
    assert fresh_summary["folderName"] == "FEMA"
    assert fresh_summary["lakehouseId"] == GUID_LH
    assert fresh_summary["semanticModelId"] == GUID_SM

    auth_calls = json.loads((fresh_dir / "auth-calls.json").read_text(encoding="utf-8"))
    assert [call[0] for call in auth_calls] == ["login", "account", "account"]
    assert auth_calls[1][3] == "https://api.fabric.microsoft.com"
    assert auth_calls[2][3] == "https://analysis.windows.net/powerbi/api"
    fresh_calls = json.loads((fresh_dir / "calls.json").read_text(encoding="utf-8"))
    uris = [call["Uri"] for call in fresh_calls]
    assert any(
        u.endswith("/folders") and c["Method"] == "Post" and c["Body"]["displayName"] == "FEMA"
        for c, u in zip(fresh_calls, uris, strict=True)
    )
    for item_suffix in (
        "/lakehouses",
        "/environments",
        "/notebooks",
        "/semanticModels",
        "/reports",
    ):
        created_call = next(
            c for c in fresh_calls if c["Method"] == "Post" and c["Uri"].endswith(item_suffix)
        )
        assert created_call["Body"]["folderId"] == GUID_FOLDER
    assert any(
        "/staging/libraries/" in u and c["Method"] == "UPLOAD"
        for c, u in zip(fresh_calls, uris, strict=True)
    )
    assert "Environment has no staged libraries yet" in fresh.stdout
    assert any("/jobs/execute/instances" in u for u in uris)
    bootstrap_call = next(c for c in fresh_calls if "/jobs/execute/instances" in c["Uri"])
    params = {p["name"]: p["value"] for p in bootstrap_call["Body"]["parameters"]}
    assert params["INITIALIZE_SCHEMA_ONLY"] is True
    assert params["PERSIST_DELTA_TABLES"] is False

    upgrade_dir = tmp_path / "upgrade"
    upgrade_dir.mkdir()
    upgrade = _run_workspace_installer(upgrade_dir, existing_items=True)
    assert upgrade.returncode == 0, upgrade.stderr
    upgrade_calls = json.loads((upgrade_dir / "calls.json").read_text(encoding="utf-8"))
    move_calls = [
        c for c in upgrade_calls if c["Method"] == "Post" and c["Uri"].endswith("/move")
    ]
    assert len(move_calls) == 5
    assert all(c["Body"]["targetFolderId"] == GUID_FOLDER for c in move_calls)
    update_uris = [
        c["Uri"] for c in upgrade_calls if "/updateDefinition" in c["Uri"]
    ]
    assert len(update_uris) == 3  # Notebook, SemanticModel, Report updated in place
    assert any(
        "/notebooks/" in u and u.endswith("?updateMetadata=true") for u in update_uris
    )
    assert any(
        "/reports/" in u and u.endswith("?updateMetadata=true") for u in update_uris
    )
    assert any(
        "/semanticModels/" in u and u.endswith("/updateDefinition") for u in update_uris
    )
    delete_calls = [
        c for c in upgrade_calls
        if c["Method"] == "Delete" and "/staging/libraries?libraryToDelete=" in c["Uri"]
    ]
    assert len(delete_calls) == 1
    assert "fabric_etl_migration_accelerator-1.0.0-py3-none-any.whl" in delete_calls[0]["Uri"]

    retry_dir = tmp_path / "retry-after-empty-environment"
    retry_dir.mkdir()
    retry = _run_workspace_installer(
        retry_dir,
        existing_items=True,
        staged_libraries_missing=True,
    )
    assert retry.returncode == 0, retry.stderr
    assert "Environment has no staged libraries yet" in retry.stdout


def test_workspace_installer_restores_recorded_recoverable_app(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required to exercise Fabric App recovery")

    app_dir = tmp_path / "app"
    deployments_dir = app_dir / "rayfin"
    deployments_dir.mkdir(parents=True)
    app_id = "88888888-8888-4888-8888-888888888888"
    registry = {
        "active": "Synthetic Workspace",
        "deployments": {
            "Synthetic Workspace": {
                "fabricItemId": app_id,
                "itemName": "fema-etl-migration-app",
                "fabricWorkspaceId": GUID_WS,
            }
        },
    }
    (deployments_dir / ".deployments.json").write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    calls_path = tmp_path / "restore-calls.json"
    script_path = ROOT / "scripts" / "Deploy-AcceleratorWorkspace.ps1"
    harness = f"""
$ErrorActionPreference = 'Stop'
$global:Calls = [System.Collections.Generic.List[object]]::new()
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{script_path.as_posix()}', [ref]$Tokens, [ref]$Errors
)
$FunctionAst = $Ast.Find({{
    param($Node)
    $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $Node.Name -eq 'Restore-RecordedFabricAppIfRecoverable'
}}, $true)
Invoke-Expression $FunctionAst.Extent.Text
$GuidPattern = (
    '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-' +
    '[1-5][0-9a-fA-F]{{3}}-[89abAB][0-9a-fA-F]{{3}}-' +
    '[0-9a-fA-F]{{12}}$'
)
$FabricApi = 'https://api.fabric.microsoft.com/v1'

function global:Invoke-FabricRequest {{
    param($Method, $Uri, $Token, $SkillName, $Body)
    $global:Calls.Add([pscustomobject]@{{ Method = $Method; Uri = $Uri }})
    if ($Method -eq 'Get') {{
        return @{{ value = @(@{{
            id = '{app_id}'
            displayName = 'fema-etl-migration-app'
            type = 'AppBackend'
        }}) }}
    }}
    if ($Method -eq 'Post' -and $Uri -match '/recover$') {{
        return @{{ status = 'Succeeded' }}
    }}
    throw "Unexpected call: $Method $Uri"
}}

$Restored = Restore-RecordedFabricAppIfRecoverable `
    -WorkspaceGuid '{GUID_WS}' `
    -WorkspaceName 'Synthetic Workspace' `
    -AppDirectory '{app_dir.as_posix()}' `
    -AppName 'fema-etl-migration-app' `
    -Token 'synthetic-token'
if (-not $Restored) {{ throw 'Expected the AppBackend to be restored.' }}
$Mismatched = Restore-RecordedFabricAppIfRecoverable `
    -WorkspaceGuid '{GUID_WS}' `
    -WorkspaceName 'Synthetic Workspace' `
    -AppDirectory '{app_dir.as_posix()}' `
    -AppName 'different-app' `
    -Token 'synthetic-token'
if ($Mismatched) {{ throw 'A mismatched AppBackend must not be restored.' }}
$global:Calls | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath '{calls_path.as_posix()}' -Encoding utf8
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", harness],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Restoring deleted Fabric App backend" in proc.stdout
    calls = json.loads(calls_path.read_text(encoding="utf-8"))
    assert calls == [
        {
            "Method": "Get",
            "Uri": (
                f"https://api.fabric.microsoft.com/v1/workspaces/{GUID_WS}"
                "/recoverableItems?type=AppBackend&recoverableByMe=true"
            ),
        },
        {
            "Method": "Post",
            "Uri": (
                f"https://api.fabric.microsoft.com/v1/workspaces/{GUID_WS}"
                f"/recoverableItems/{app_id}/recover"
            ),
        },
    ]


def test_fabric_environment_excludes_web_server_dependencies() -> None:
    env_text = (ROOT / "scripts" / "fabric-environment.yml").read_text(encoding="utf-8")
    assert env_text.splitlines()[0] == "dependencies:"
    assert not any(line.startswith("name:") for line in env_text.splitlines())
    for forbidden in ("fastapi", "uvicorn", "typer", "pyjwt", "python-multipart"):
        assert forbidden not in env_text.casefold()
    assert "defusedxml" in env_text
    assert "pydantic" in env_text
