"""End-to-end tests for the FastAPI + Jinja2 + HTMX Python analyst UI."""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from accelerator.server import sharepoint
from accelerator.server.api import app
from accelerator.ui import router as ui_router

ROOT = Path(__file__).parents[1]


class _TextNodeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.lengths: list[int] = []

    def handle_data(self, data: str) -> None:
        if text := data.strip():
            self.lengths.append(len(text))


def test_python_ui_shell_and_static_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ACCELERATOR_WEB_ROOT", raising=False)
    with TestClient(app, base_url="http://localhost") as client:
        home = client.get("/")
        assert home.status_code == 200
        assert '<div id="root">' in home.text
        assert "/assets/htmx.min.js" in home.text
        assert "/assets/app.css" in home.text
        assert "/assets/app.js" in home.text
        assert "New Assessment Intake" in home.text

        for asset in ("/assets/htmx.min.js", "/assets/app.css", "/assets/app.js"):
            res = client.get(asset)
            assert res.status_code == 200
            assert len(res.content) > 100


def test_python_ui_end_to_end_intake_to_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ACCELERATOR_WEB_ROOT", raising=False)
    sample_file = ROOT / "alteryx-workflows" / "01_low_clean_orders.yxmd"

    with TestClient(app, base_url="http://localhost") as client:
        csrf = app.state.csrf_token
        headers = {"origin": "http://localhost", "x-accelerator-token": csrf}

        # 1. Local intake preview
        preview = client.post(
            "/ui/intake/local",
            data={"path": str(sample_file), "source_family": "alteryx"},
            headers=headers,
        )
        assert preview.status_code == 200
        assert "Snapshot Discovery Preview" in preview.text
        assert "01_low_clean_orders.yxmd" in preview.text

        # Extract snapshot_id from hidden input
        marker = 'name="snapshot_id" value="'
        start = preview.text.index(marker) + len(marker)
        snapshot_id = preview.text[start : preview.text.index('"', start)]

        # 2. Confirm and assess
        started = client.post(
            "/ui/runs/start",
            data={"snapshot_id": snapshot_id},
            headers=headers,
        )
        assert started.status_code == 200

        # Wait for the background worker to complete the run (matching HTMX polling)
        run_id = app.state.run_service.list_runs(limit=5, offset=0)[0][0].id
        progress = client.get(f"/ui/runs/{run_id}/progress")
        deadline = time.monotonic() + 10.0
        while "Portfolio Overview" not in progress.text and time.monotonic() < deadline:
            time.sleep(0.05)
            progress = client.get(f"/ui/runs/{run_id}/progress")
        assert progress.status_code == 200
        assert "Portfolio Overview" in progress.text
        assert "Leadership Recommendation" in progress.text
        assert "Projected F-SKU" in progress.text
        assert "hx-swap-oob" in progress.text
        assert "static mapping" in progress.text

        # 3b. Migration Plan tab
        plan = client.get(f"/ui/plan?run_id={run_id}")
        assert plan.status_code == 200
        assert "Evidence-Based Migration Plan" in plan.text
        assert "Delivery Capacity Sensitivity" in plan.text

        # 4. Workflows register + 6-tab drawer (distinct Source DAG vs Fabric Design)
        workflows = client.get(f"/ui/workflows?run_id={run_id}")
        assert workflows.status_code == 200
        assert "01_low_clean_orders.yxmd" in workflows.text

        for tab in ("summary", "source", "fabric", "risk", "validation", "deployment"):
            drawer = client.get(f"/ui/runs/{run_id}/workflows/0/drawer?tab={tab}")
            assert drawer.status_code == 200
            if tab == "source":
                assert "<svg " in drawer.text
                assert "Find / Next" in drawer.text
                assert "dag-inspector" in drawer.text
            elif tab == "fabric":
                assert "Target Architecture Interpretation" in drawer.text
                assert "Tool-to-Fabric Mapping Strategy" in drawer.text
                assert "<svg " not in drawer.text
            elif tab == "risk":
                assert "Static Risk Profile" in drawer.text

        # 5. Technical Details all 6 registers
        for reg in ("tools", "expressions", "endpoints", "dependencies", "findings", "connections"):
            tech = client.get(f"/ui/technical?run_id={run_id}&register={reg}")
            assert tech.status_code == 200

        # 6. Capacity Sizing GET + per-workflow editable POST recalculation
        cap_get = client.get(f"/ui/capacity?run_id={run_id}")
        assert cap_get.status_code == 200
        assert "Fabric Capacity Estimate" in cap_get.text
        assert "unconfirmed" not in cap_get.text.lower()
        assert 'name="wf_refresh_0"' in cap_get.text
        assert 'name="wf_rows_0"' in cap_get.text

        cap_post = client.post(
            "/ui/capacity",
            data={
                "run_id": run_id,
                "days_per_month": "30",
                "target_utilization": "0.75",
                "active_users_per_day": "5",
                "burst_window_minutes": "10",
                "spark_billing_mode": "autoscale",
                "wf_path_0": "01_low_clean_orders.yxmd",
                "wf_refresh_0": "60",
                "wf_rows_0": "50000",
            },
            headers=headers,
        )
        assert cap_post.status_code == 200
        assert 'value="30"' in cap_post.text
        assert 'value="50000"' in cap_post.text
        assert "Saved and recalculated capacity" in cap_post.text
        assert "Preliminary (confirmed)" in cap_post.text

        # 7. Fabric Deployment confirms compiler fit + recommended code preview + dry-run
        deploy = client.get(f"/ui/deployment?run_id={run_id}")
        assert deploy.status_code == 200
        assert "compiler_confirmed" in deploy.text
        assert "Generate Recommended Code" in deploy.text
        assert "Recommended (auto)" in deploy.text
        assert "Fabric Artifact Target" not in deploy.text

        code_preview = client.post(
            "/ui/deployment/preview",
            data={"run_id": run_id, "target": "recommended"},
            headers=headers,
        )
        assert code_preview.status_code == 200
        assert "Recommended Fabric Artifacts" in code_preview.text
        assert "01_low_clean_orders.yxmd" in code_preview.text
        assert "Dataflow Gen2" in code_preview.text

        dry = client.post(
            "/ui/deployment/dry-run",
            data={"run_id": run_id, "workspace_id": "dev-ws"},
            headers=headers,
        )
        assert dry.status_code == 200
        assert "Dry-Run Validation Result" in dry.text


def test_python_ui_enforces_csrf_and_server_mode_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    with TestClient(app, base_url="http://localhost") as client:
        # Missing CSRF token on browser POST is rejected
        rejected = client.post(
            "/ui/intake/local",
            data={"path": "alteryx-workflows"},
            headers={"origin": "http://localhost"},
        )
        assert rejected.status_code == 403

    monkeypatch.setenv("ACCELERATOR_MODE", "server")
    monkeypatch.setenv("ACCELERATOR_API_TOKEN", "top-secret-token")
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/ui/intake").status_code == 401
        ok = client.get(
            "/ui/intake",
            headers={"Authorization": "Bearer top-secret-token"},
        )
        assert ok.status_code == 200


def test_python_ui_downloads_offline_portfolio_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "portfolio"
    source.mkdir()
    fixtures = ROOT / "tests" / "fixtures" / "powercenter"
    shutil.copy2(fixtures / "basic_repository.xml", source / "basic_repository.xml")
    shutil.copy2(
        fixtures / "router_validation_repository.xml",
        source / "router_validation_repository.xml",
    )

    with TestClient(app, base_url="http://localhost") as client:
        csrf = app.state.csrf_token
        headers = {"origin": "http://localhost", "x-accelerator-token": csrf}
        preview = client.post(
            "/ui/intake/local",
            data={"path": str(source), "source_family": "informatica"},
            headers=headers,
        )
        assert preview.status_code == 200
        assert "Snapshot Discovery Preview" in preview.text
        marker = 'name="snapshot_id" value="'
        start = preview.text.index(marker) + len(marker)
        snapshot_id = preview.text[start : preview.text.index('"', start)]
        started = client.post(
            "/ui/runs/start",
            data={"snapshot_id": snapshot_id},
            headers=headers,
        )
        assert started.status_code == 200

        run_id = app.state.run_service.list_runs(limit=1, offset=0)[0][0].id
        deadline = time.monotonic() + 10.0
        run = app.state.run_service.get_run(run_id)
        while run.status not in {"completed", "failed"} and time.monotonic() < deadline:
            time.sleep(0.05)
            run = app.state.run_service.get_run(run_id)
        assert run.status == "completed", run.error

        portfolio = client.get(f"/ui/portfolio?run_id={run_id}")
        assert portfolio.status_code == 200
        assert "Routing basis: static mapping" in portfolio.text
        assert "Confirm Fabric Fit" in portfolio.text
        assert f'/api/runs/{run_id}/offline-dashboard' in portfolio.text
        evidence_gap = re.search(
            r"<h3>Assessment Confidence.*?</div>",
            portfolio.text,
            flags=re.DOTALL,
        )
        assert evidence_gap is not None
        text_nodes = _TextNodeParser()
        text_nodes.feed(evidence_gap.group())
        assert text_nodes.lengths
        assert max(text_nodes.lengths) <= 256

        dashboard = client.get(f"/api/runs/{run_id}/offline-dashboard")
        repeated_dashboard = client.get(f"/api/runs/{run_id}/offline-dashboard")

        deploy = client.get(f"/ui/deployment?run_id={run_id}")
        assert deploy.status_code == 200
        assert "compiler_confirmed" in deploy.text
        confirmed_portfolio = client.get(f"/ui/portfolio?run_id={run_id}")
        assert confirmed_portfolio.status_code == 200
        assert "Leadership Recommendation" in confirmed_portfolio.text
        assert "Routing basis:" not in confirmed_portfolio.text
        assert "Fabric targets, waves, and effort reflect compiler-confirmed routes." not in (
            confirmed_portfolio.text
        )
        assert "Confirm Fabric Fit" not in confirmed_portfolio.text

    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"] == "application/zip"
    assert dashboard.headers["x-routing-basis"] == "static_mapping"
    assert (
        dashboard.headers["content-disposition"]
        == f'attachment; filename="portfolio-assessment-{run_id}.zip"'
    )
    assert dashboard.content == repeated_dashboard.content
    with zipfile.ZipFile(io.BytesIO(dashboard.content)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [
            "index.html",
            "README.txt",
            "data/assessment.json",
            "data/workflows.csv",
            "data/capacity.json",
        ]
        offline_html = archive.read("index.html").decode()
        assert "Portfolio Assessment Dashboard" in offline_html
        assert "Offline · Self-contained" in offline_html
        assert "Leadership readiness" in offline_html
        assert "Capacity scenarios" in offline_html
        assert "Workflow details" in offline_html
        assert "Tool inventory" in offline_html
        assert "Fabric recommendation" in offline_html
        assert "Clear filters" in offline_html
        assert 'role="tab"' in offline_html
        assert "Overview" in offline_html
        assert "Workflows" in offline_html
        assert "Migration Waves" in offline_html
        assert "Wave summary" in offline_html
        assert "Capacity" in offline_html
        assert "Evidence &amp; Gaps" not in offline_html
        assert run_id in offline_html
        assert "<script src=" not in offline_html
        assert "<link rel=\"stylesheet\"" not in offline_html
        assessment = json.loads(archive.read("data/assessment.json"))
        assert assessment["workflow_count"] == 2
        assert "wf_orders" in offline_html
        assert "wf_STG_Ledger_Validation" in offline_html
        assert archive.read("data/workflows.csv").decode().startswith(
            "Workflow,Tools,Complexity,Target,Remaining effort days"
        )
        assert isinstance(json.loads(archive.read("data/capacity.json")), dict)


def test_python_ui_previews_sharepoint_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    sample_file = ROOT / "tests" / "fixtures" / "powercenter" / "basic_repository.xml"
    received_urls: list[str] = []

    def fake_stage(folder_url: str, sandbox: Path) -> list[Path]:
        received_urls.append(folder_url)
        destination = sandbox / sample_file.name
        destination.write_bytes(sample_file.read_bytes())
        return [destination]

    monkeypatch.setattr(ui_router, "stage_sharepoint_folder", fake_stage)
    with TestClient(app, base_url="http://localhost") as client:
        intake = client.get("/ui/intake")
        assert intake.status_code == 200
        assert 'hx-post="/ui/intake/sharepoint"' in intake.text
        assert "SharePoint Folder URL" in intake.text

        csrf = app.state.csrf_token
        preview = client.post(
            "/ui/intake/sharepoint",
            data={
                "folder_url": (
                    "https://contoso.sharepoint.com/sites/migrations/"
                    "Shared%20Documents/exports"
                ),
                "source_family": "informatica",
            },
            headers={"origin": "http://localhost", "x-accelerator-token": csrf},
        )

    assert preview.status_code == 200
    assert "Snapshot Discovery Preview" in preview.text
    assert sample_file.name in preview.text
    assert received_urls == [
        "https://contoso.sharepoint.com/sites/migrations/Shared%20Documents/exports"
    ]


def test_python_ui_displays_graph_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCELERATOR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AZURE_TENANT_ID", "synthetic-tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "synthetic-secret")
    payload = json.dumps(
        {
            "error": {
                "code": "invalidRequest",
                "message": "The sharing link is invalid.",
                "innerError": {"request-id": "11111111-1111-4111-8111-111111111111"},
            }
        }
    ).encode()

    def fake_open(request: Request, *, timeout: float) -> io.BytesIO:
        del timeout
        if "login.microsoftonline.com" in request.full_url:
            return io.BytesIO(b'{"access_token": "synthetic-token"}')
        raise HTTPError(request.full_url, 400, "Bad Request", Message(), io.BytesIO(payload))

    monkeypatch.setattr(sharepoint, "_open", fake_open)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/ui/intake/sharepoint",
            data={"folder_url": "https://contoso.sharepoint.com/:f:/g/synthetic?e=private"},
            headers={
                "origin": "http://localhost",
                "x-accelerator-token": app.state.csrf_token,
            },
        )

    assert response.status_code == 200
    assert "Microsoft Graph could not resolve the SharePoint sharing link" in response.text
    assert "HTTP 400" in response.text
    assert "invalidRequest" in response.text
    assert "The sharing link is invalid." in response.text
    assert "11111111-1111-4111-8111-111111111111" in response.text
