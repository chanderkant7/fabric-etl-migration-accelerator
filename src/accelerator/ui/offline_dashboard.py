"""Build a deterministic, self-contained offline portfolio dashboard package."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from accelerator.runtime.models import Run
from etl_target_fabric.capacity.model import CapacityPortfolioEstimate

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
_README = """Microsoft Fabric ETL Migration Accelerator
Offline Portfolio Assessment Dashboard

Open index.html in a modern browser. The dashboard is self-contained and does
not make network requests. Supporting assessment, workflow, and capacity data
is available under the data directory.
"""


def build_offline_dashboard_package(
    run: Run,
    capacity: CapacityPortfolioEstimate | None,
    *,
    assessment_csv: str,
) -> bytes:
    """Return a deterministic ZIP containing an offline dashboard and source data."""
    if run.status != "completed" or run.result is None:
        raise ValueError("Complete an assessment before downloading the offline dashboard")

    dashboard_data = {
        "runId": run.id,
        "status": run.status,
        "routingBasis": run.routing_basis,
        "workflowCount": run.result.workflow_count,
        "failedCount": run.result.failed_count,
        "workflows": [
            {
                "path": item.path,
                "sourceProduct": item.source_product,
                "sourcePlatform": item.source_platform,
                "assetType": item.asset_type,
                "nativeId": item.native_id,
                "folder": item.folder,
                "toolCount": item.tool_count,
                "connectionCount": item.connection_count,
                "tools": [tool.model_dump(mode="json") for tool in item.tools],
                "complexityScore": item.complexity_score,
                "complexityBand": item.complexity_band,
                "recommendedTarget": item.recommended_target,
                "targetComponents": item.target_components,
                "recommendationReason": item.recommendation_reason,
                "migrationWave": item.migration_wave,
                "estimatedEffortDays": item.estimated_effort_days,
                "unsupportedTools": item.unsupported_tools,
                "dependencies": item.dependencies,
                "dataSources": item.data_sources,
                "dependencyCount": len(item.dependencies),
                "dataSourceCount": len(item.data_sources),
                "registryVersion": item.registry_version,
                "rulesVersion": item.rules_version,
                "graphSchemaVersion": item.graph_schema_version,
            }
            for item in run.result.assessments
        ],
        "capacity": capacity.model_dump(mode="json") if capacity is not None else None,
    }
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(("html",)),
        keep_trailing_newline=True,
    )
    index_html = environment.get_template("offline_portfolio_dashboard.html").render(
        data=dashboard_data
    )
    assessment_json = run.result.model_dump_json(indent=2)
    capacity_json = capacity.model_dump_json(indent=2) if capacity is not None else json.dumps(None)

    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        _write_text(archive, "index.html", index_html)
        _write_text(archive, "README.txt", _README)
        _write_text(archive, "data/assessment.json", assessment_json)
        _write_text(archive, "data/workflows.csv", assessment_csv)
        _write_text(archive, "data/capacity.json", capacity_json)
    return output.getvalue()


def _write_text(archive: zipfile.ZipFile, path: str, content: str) -> None:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content.encode("utf-8"), compresslevel=9)


__all__ = ["build_offline_dashboard_package"]
