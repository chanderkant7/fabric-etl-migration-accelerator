"""FastAPI + Jinja2 + HTMX router for the Python analyst workbench."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from accelerator import __version__
from accelerator.delivery.base import DeploymentTarget
from accelerator.delivery.github import GitHubDeploymentResult, GitHubDeploymentSelection
from accelerator.delivery.gitlab import GitLabDeploymentResult, GitLabDeploymentSelection
from accelerator.runtime.models import Run, RunStatus, SnapshotPreview
from accelerator.runtime.runs import RunService
from accelerator.server.sharepoint import SharePointIntakeError, stage_sharepoint_folder
from accelerator.server.uploads import stage_uploads
from accelerator.settings import Settings
from accelerator.ui.graph_svg import (
    compute_graph_shape,
    render_workflow_svg,
    support_metadata,
)
from etl_migration_core.intake.archive import InputRejectedError
from etl_migration_core.targets.registry import TargetPlatformRegistry
from etl_portfolio_assessment.evidence.extractor import EvidenceResponse, WorkflowEvidence
from etl_target_fabric.assessment.schemas import WorkflowAssessmentResponse
from etl_target_fabric.capacity.model import (
    CapacityPortfolioEstimate,
    CapacitySettings,
    CapacityUpdate,
    CapacityWorkflowInput,
)
from etl_target_fabric.compilers.alteryx import Candidate, Target
from etl_target_fabric.delivery.deployer import dry_run

UI_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = UI_ROOT / "templates"
STATIC_DIR = UI_ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_WAVE_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "Wave 1",
        "Wave 1",
        "Low complexity, single Fabric target. Resolve prerequisites and source bindings.",
    ),
    (
        "Pilot",
        "Wave 2",
        "Medium complexity / Pilot. Prove representative transformations and controls.",
    ),
    (
        "Wave 2",
        "Wave 3",
        "High complexity or Mixed Fabric solution. Apply reviewed patterns to backlog.",
    ),
    (
        "Redesign",
        "Wave 4",
        "Manual rework / Redesign. Redesign unsupported or specialist semantics.",
    ),
)
_WAVE_LABEL_MAP: dict[str, str] = {raw: label for raw, label, _ in _WAVE_SPECS}
_WAVE_ORDER: dict[str, int] = {raw: idx for idx, (raw, _, _) in enumerate(_WAVE_SPECS)}


def wave_display(raw_wave: str) -> str:
    """Normalize engine wave token into consistent Wave 1-4 presentation label."""
    return _WAVE_LABEL_MAP.get(raw_wave, raw_wave)


templates.env.filters["wave_label"] = wave_display


@dataclass(frozen=True)
class PortfolioSummary:
    total: int
    total_tools: int
    total_connections: int
    avg_complexity: float
    total_effort: float
    native_ready: int
    manual_rework: int
    high_complexity: int
    manual_count: int
    total_findings: int
    total_dependencies: int
    total_endpoints: int
    by_target: list[tuple[str, int]]
    by_wave: list[tuple[str, int, float]]
    by_complexity: list[tuple[str, int]]
    complexity_bars: list[tuple[str, int, str]]
    top_workflow: tuple[int, WorkflowAssessmentResponse] | None
    projected_sku: str
    capacity_context: str


@dataclass(frozen=True)
class WaveSummary:
    value: str
    label: str
    guidance: str
    count: int
    effort: float
    target_summary: str


def _safe_capacity(service: RunService, run: Run | None) -> CapacityPortfolioEstimate | None:
    if run is None or run.status != "completed":
        return None
    try:
        return service.get_capacity(run.id)
    except (KeyError, ValueError):
        return None


def _uploaded_preview_target(staged: list[Path], directory: Path) -> Path:
    if len(staged) == 1 and staged[0].suffix.lower() in {".zip", ".yxzp"}:
        return staged[0]
    return directory


_COMPONENT_TO_TARGET: dict[str, Target] = {
    "Dataflow Gen2": "dataflow",
    "Fabric Notebook": "notebook",
    "Fabric Pipeline": "pipeline",
}


def _recommended_targets_for(wf: WorkflowAssessmentResponse) -> tuple[Target, ...]:
    """Return the compiler target kind(s) matching a workflow's confirmed Fabric recommendation."""
    found: list[Target] = []
    for comp in wf.target_components:
        mapped = _COMPONENT_TO_TARGET.get(comp)
        if mapped and mapped not in found:
            found.append(mapped)
    if not found and wf.recommended_target in _COMPONENT_TO_TARGET:
        found.append(_COMPONENT_TO_TARGET[wf.recommended_target])
    return tuple(found) if found else ("notebook",)


def _recommended_deployment_target(wf: WorkflowAssessmentResponse) -> DeploymentTarget:
    """Resolve the single CI/CD DeploymentTarget matching a workflow's confirmed Fabric route."""
    rec = _recommended_targets_for(wf)
    if len(rec) > 1:
        return "bundle"
    return rec[0]


def _deployment_counts(assessments: list[WorkflowAssessmentResponse]) -> dict[str, int]:
    counts = {"dataflow": 0, "notebook": 0, "pipeline": 0, "manual": 0}
    for wf in assessments:
        if wf.recommended_target == "Manual rework":
            counts["manual"] += 1
            continue
        for t in _recommended_targets_for(wf):
            counts[t] += 1
    return counts


def _optimized_sku(estimate: CapacityPortfolioEstimate | None) -> str:
    if estimate is None or estimate.status == "not_available":
        return "Not available"
    for sc in estimate.scenarios:
        if sc.name == "Optimized":
            return sc.recommended_sku
    return estimate.scenarios[0].recommended_sku if estimate.scenarios else "Not available"


def _review_priority(value: int, medium: int, high: int) -> str:
    if value >= high:
        return "High"
    if value >= medium:
        return "Medium"
    return "Low"


def _tool_mapping_bars(assessments: list[WorkflowAssessmentResponse]) -> list[tuple[str, int, str]]:
    rewrite = 0
    new_impl = 0
    neutral = 0
    for wf in assessments:
        for t in wf.tools:
            if t.support in {"dataflow", "pipeline", "supported", "native"}:
                rewrite += t.count
            elif t.support in {"notebook", "manual", "unsupported", "partial"}:
                new_impl += t.count
            else:
                neutral += t.count
    return [
        ("Rewrite in Fabric", rewrite, "ok"),
        ("New implementation / Redesign", new_impl, "warn"),
        ("No equivalent needed", neutral, "info"),
    ]


def _summarize_waves(assessments: list[WorkflowAssessmentResponse]) -> list[WaveSummary]:
    by_wave_count: Counter[str] = Counter()
    by_wave_effort: defaultdict[str, float] = defaultdict(float)
    by_wave_targets: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for wf in assessments:
        by_wave_count[wf.migration_wave] += 1
        by_wave_effort[wf.migration_wave] += wf.estimated_effort_days
        by_wave_targets[wf.migration_wave][wf.recommended_target] += 1

    summaries: list[WaveSummary] = []
    known = {w[0] for w in _WAVE_SPECS}
    for value, label, guidance in _WAVE_SPECS:
        t_counts = by_wave_targets[value]
        t_summary = " · ".join(f"{cnt} {tgt}" for tgt, cnt in t_counts.most_common())
        summaries.append(
            WaveSummary(
                value=value,
                label=label,
                guidance=guidance,
                count=by_wave_count[value],
                effort=round(by_wave_effort[value], 1),
                target_summary=t_summary,
            )
        )
    for extra in sorted(set(by_wave_count) - known):
        t_counts = by_wave_targets[extra]
        summaries.append(
            WaveSummary(
                value=extra,
                label=extra,
                guidance="Additional engine wave allocation.",
                count=by_wave_count[extra],
                effort=round(by_wave_effort[extra], 1),
                target_summary=" · ".join(f"{cnt} {tgt}" for tgt, cnt in t_counts.most_common()),
            )
        )
    return summaries


def _summarize_portfolio(
    run: Run,
    capacity: CapacityPortfolioEstimate | None = None,
) -> PortfolioSummary:
    if run.result is None or not run.result.assessments:
        return PortfolioSummary(
            total=0,
            total_tools=0,
            total_connections=0,
            avg_complexity=0.0,
            total_effort=0.0,
            native_ready=0,
            manual_rework=0,
            high_complexity=0,
            manual_count=0,
            total_findings=0,
            total_dependencies=0,
            total_endpoints=0,
            by_target=[],
            by_wave=[],
            by_complexity=[],
            complexity_bars=[("Low", 0, "ok"), ("Medium", 0, "warn"), ("High", 0, "err")],
            top_workflow=None,
            projected_sku="Not available",
            capacity_context="Complete an assessment first",
        )
    assessments = run.result.assessments
    total = len(assessments)
    total_tools = sum(a.tool_count for a in assessments)
    total_connections = sum(a.connection_count for a in assessments)
    avg_complexity = round(sum(a.complexity_score for a in assessments) / total, 1)
    total_effort = round(sum(a.estimated_effort_days for a in assessments), 1)
    manual_count = sum(1 for a in assessments if a.unsupported_tools)
    manual_rework = sum(1 for a in assessments if a.recommended_target == "Manual rework")
    native_ready = total - manual_rework
    high_complexity = sum(1 for a in assessments if a.complexity_band in {"High", "Critical"})
    total_findings = run.result.failed_count + sum(len(a.unsupported_tools) for a in assessments)
    total_dependencies = sum(len(a.dependencies) for a in assessments)
    total_endpoints = sum(len(a.data_sources) for a in assessments)

    target_counts = Counter(a.recommended_target for a in assessments)
    wave_counts: Counter[str] = Counter()
    wave_effort: defaultdict[str, float] = defaultdict(float)
    complexity_counts: Counter[str] = Counter()
    grouped_complexity: Counter[str] = Counter()
    for a in assessments:
        wave_counts[a.migration_wave] += 1
        wave_effort[a.migration_wave] += a.estimated_effort_days
        complexity_counts[a.complexity_band] += 1
        grouped_complexity["High" if a.complexity_band == "Critical" else a.complexity_band] += 1

    by_wave = sorted(
        [(wave_display(w), wave_counts[w], round(wave_effort[w], 1)) for w in wave_counts],
        key=lambda item: (
            next((_WAVE_ORDER[k] for k, lbl in _WAVE_LABEL_MAP.items() if lbl == item[0]), 99),
            item[0],
        ),
    )
    top_idx, top_wf = max(enumerate(assessments), key=lambda pair: pair[1].complexity_score)
    projected_sku = _optimized_sku(capacity)
    if capacity is not None:
        suffix = " · Default schedule/rows" if capacity.status == "inputs_required" else ""
        if capacity.settings.spark_billing_mode == "autoscale":
            suffix += " · Spark Autoscale"
        capacity_context = f"{capacity.monthly_cu_hours:.1f} CU-h/mo{suffix}"
    else:
        capacity_context = "Optimized concurrency scenario"

    return PortfolioSummary(
        total=total,
        total_tools=total_tools,
        total_connections=total_connections,
        avg_complexity=avg_complexity,
        total_effort=total_effort,
        native_ready=native_ready,
        manual_rework=manual_rework,
        high_complexity=high_complexity,
        manual_count=manual_count,
        total_findings=total_findings,
        total_dependencies=total_dependencies,
        total_endpoints=total_endpoints,
        by_target=target_counts.most_common(),
        by_wave=by_wave,
        by_complexity=complexity_counts.most_common(),
        complexity_bars=[
            ("Low", grouped_complexity["Low"], "ok"),
            ("Medium", grouped_complexity["Medium"], "warn"),
            ("High", grouped_complexity["High"], "err"),
        ],
        top_workflow=(top_idx, top_wf),
        projected_sku=projected_sku,
        capacity_context=capacity_context,
    )


def _resolve_active_run(service: RunService, run_id: str | None) -> Run | None:
    if run_id:
        return service.get_run(run_id)
    runs, _ = service.list_runs(limit=10, offset=0)
    for item in runs:
        if item.status == "completed":
            return service.get_run(item.id)
    if runs:
        return service.get_run(runs[0].id)
    return None


def _recent_runs(service: RunService) -> list[RunStatus]:
    runs, _ = service.list_runs(limit=25, offset=0)
    return runs


def create_ui_router(
    *,
    get_run_service: Callable[[], RunService],
    notify_worker: Callable[[], None],
    get_target_registry: Callable[[], TargetPlatformRegistry],
    dispatch_github: Callable[[str, GitHubDeploymentSelection], GitHubDeploymentResult],
    dispatch_gitlab: Callable[[str, GitLabDeploymentSelection], GitLabDeploymentResult],
) -> APIRouter:
    """Create the server-rendered Jinja2 + HTMX UI router."""
    router = APIRouter(tags=["ui"], include_in_schema=False)

    def _base_context(
        request: Request,
        *,
        active_tab: str,
        initial_partial: str,
        active_run: Run | None = None,
    ) -> dict[str, object]:
        settings = Settings.from_environment()
        service = get_run_service()
        runs = _recent_runs(service)
        return {
            "request": request,
            "version": __version__,
            "csrf_token": getattr(request.app.state, "csrf_token", ""),
            "deployment_mode": settings.deployment_mode,
            "allow_local_filesystem": settings.allow_local_filesystem,
            "active_tab": active_tab,
            "initial_partial": initial_partial,
            "runs": runs,
            "active_run_id": active_run.id if active_run else "",
            "run": active_run,
            "snapshot": None,
            "error": None,
            "local_path": "",
            "source_family": "",
        }

    @router.get("/", response_class=HTMLResponse)
    def ui_home(request: Request, run_id: str | None = None) -> HTMLResponse:
        service = get_run_service()
        active_run = _resolve_active_run(service, run_id) if run_id else None
        if active_run is not None:
            cap = _safe_capacity(service, active_run)
            ctx = _base_context(
                request,
                active_tab="portfolio",
                initial_partial="partials/portfolio.html",
                active_run=active_run,
            )
            ctx["summary"] = _summarize_portfolio(active_run, cap)
            ctx["capacity"] = cap
        else:
            ctx = _base_context(
                request,
                active_tab="intake",
                initial_partial="partials/intake.html",
                active_run=_resolve_active_run(service, None),
            )
        response = templates.TemplateResponse(request, "base.html", ctx)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/ui/intake", response_class=HTMLResponse)
    def ui_intake(request: Request) -> HTMLResponse:
        settings = Settings.from_environment()
        service = get_run_service()
        return templates.TemplateResponse(
            request,
            "partials/intake.html",
            {
                "allow_local_filesystem": settings.allow_local_filesystem,
                "runs": _recent_runs(service),
                "snapshot": None,
                "error": None,
                "local_path": "",
                "sharepoint_url": "",
                "source_family": "",
            },
        )

    @router.post("/ui/intake/local", response_class=HTMLResponse)
    def ui_intake_local(
        request: Request,
        path: Annotated[str, Form()],
        source_family: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        settings = Settings.from_environment()
        service = get_run_service()
        error: str | None = None
        snapshot: SnapshotPreview | None = None
        cleaned = path.strip()
        if not settings.allow_local_filesystem:
            error = "Local filesystem access is disabled in server mode."
        elif cleaned.startswith(("\\\\", "//")):
            error = "UNC paths are not accepted."
        else:
            try:
                snapshot = service.preview(Path(cleaned))
            except (InputRejectedError, ValueError, OSError) as exc:
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "partials/intake.html",
            {
                "allow_local_filesystem": settings.allow_local_filesystem,
                "runs": _recent_runs(service),
                "snapshot": snapshot,
                "error": error,
                "local_path": cleaned,
                "sharepoint_url": "",
                "source_family": source_family,
            },
        )

    @router.post("/ui/intake/upload", response_class=HTMLResponse)
    async def ui_intake_upload(
        request: Request,
        files: Annotated[list[UploadFile], File()],
        source_family: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        settings = Settings.from_environment()
        service = get_run_service()
        error: str | None = None
        snapshot: SnapshotPreview | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="accelerator-ui-upload-") as directory:
                sandbox = Path(directory)
                staged = await stage_uploads(files, sandbox)
                preview_target = _uploaded_preview_target(staged, sandbox)
                snapshot = await run_in_threadpool(service.preview, preview_target)
        except (HTTPException, InputRejectedError, ValueError, OSError) as exc:
            error = str(exc.detail if isinstance(exc, HTTPException) else exc)
        return templates.TemplateResponse(
            request,
            "partials/intake.html",
            {
                "allow_local_filesystem": settings.allow_local_filesystem,
                "runs": _recent_runs(service),
                "snapshot": snapshot,
                "error": error,
                "local_path": "",
                "sharepoint_url": "",
                "source_family": source_family,
            },
        )

    @router.post("/ui/intake/sharepoint", response_class=HTMLResponse)
    async def ui_intake_sharepoint(
        request: Request,
        folder_url: Annotated[str, Form()],
        source_family: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        settings = Settings.from_environment()
        service = get_run_service()
        error: str | None = None
        snapshot: SnapshotPreview | None = None
        cleaned_url = folder_url.strip()
        try:
            with tempfile.TemporaryDirectory(prefix="accelerator-ui-sharepoint-") as directory:
                sandbox = Path(directory)
                staged = await run_in_threadpool(
                    stage_sharepoint_folder, cleaned_url, sandbox
                )
                preview_target = _uploaded_preview_target(staged, sandbox)
                snapshot = await run_in_threadpool(service.preview, preview_target)
        except (InputRejectedError, SharePointIntakeError, ValueError, OSError) as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "partials/intake.html",
            {
                "allow_local_filesystem": settings.allow_local_filesystem,
                "runs": _recent_runs(service),
                "snapshot": snapshot,
                "error": error,
                "local_path": "",
                "sharepoint_url": cleaned_url,
                "source_family": source_family,
            },
        )

    @router.post("/ui/runs/start", response_class=HTMLResponse)
    def ui_start_run(
        request: Request,
        snapshot_id: Annotated[str, Form()],
    ) -> HTMLResponse:
        service = get_run_service()
        try:
            created = service.create_run(snapshot_id)
        except (KeyError, ValueError) as exc:
            settings = Settings.from_environment()
            return templates.TemplateResponse(
                request,
                "partials/intake.html",
                {
                    "allow_local_filesystem": settings.allow_local_filesystem,
                    "runs": _recent_runs(service),
                    "snapshot": None,
                    "error": str(exc),
                    "local_path": "",
                    "sharepoint_url": "",
                    "source_family": "",
                },
            )
        notify_worker()
        return templates.TemplateResponse(
            request,
            "partials/run_progress.html",
            {"run": created},
        )

    @router.get("/ui/runs/{identity}/progress", response_class=HTMLResponse)
    def ui_run_progress(request: Request, identity: str) -> HTMLResponse:
        service = get_run_service()
        run = service.get_run(identity)
        if run is None:
            raise HTTPException(status_code=404, detail="Assessment run not found")
        if run.status == "completed":
            cap = _safe_capacity(service, run)
            return templates.TemplateResponse(
                request,
                "partials/portfolio.html",
                {
                    "run": run,
                    "summary": _summarize_portfolio(run, cap),
                    "capacity": cap,
                    "oob_runs": _recent_runs(service),
                },
            )
        return templates.TemplateResponse(
            request,
            "partials/run_progress.html",
            {"run": run},
        )

    @router.post("/ui/runs/{identity}/cancel", response_class=HTMLResponse)
    def ui_cancel_run(request: Request, identity: str) -> HTMLResponse:
        service = get_run_service()
        run = service.cancel(identity)
        if run is None:
            raise HTTPException(status_code=404, detail="Assessment run not found")
        return templates.TemplateResponse(
            request,
            "partials/run_progress.html",
            {"run": run},
        )

    @router.get("/ui/portfolio", response_class=HTMLResponse)
    def ui_portfolio(request: Request, run_id: str | None = None) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        cap = _safe_capacity(service, run)
        summary = _summarize_portfolio(run, cap) if run else None
        return templates.TemplateResponse(
            request,
            "partials/portfolio.html",
            {"run": run, "summary": summary, "capacity": cap},
        )

    @router.get("/ui/plan", response_class=HTMLResponse)
    def ui_plan(
        request: Request,
        run_id: str | None = None,
        wave: str = "",
        q: str = "",
        target: str = "",
        complexity: str = "",
    ) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        assessments = run.result.assessments if (run and run.result) else []
        waves = _summarize_waves(assessments)
        total_effort = round(sum(a.estimated_effort_days for a in assessments), 1)
        target_options = sorted({a.recommended_target for a in assessments})
        complexity_options = sorted({a.complexity_band for a in assessments})
        query = q.strip().casefold()
        filtered = [
            (idx, wf)
            for idx, wf in enumerate(assessments)
            if (not wave or wf.migration_wave == wave)
            and (not target or wf.recommended_target == target)
            and (not complexity or wf.complexity_band == complexity)
            and (
                not query
                or query
                in f"{wf.path} {wf.recommended_target} {wf.recommendation_reason}".casefold()
            )
        ]
        return templates.TemplateResponse(
            request,
            "partials/migration_plan.html",
            {
                "run": run,
                "waves": waves,
                "total_effort": total_effort,
                "selected_wave": wave,
                "q": q,
                "target": target,
                "complexity": complexity,
                "target_options": target_options,
                "complexity_options": complexity_options,
                "workflows": filtered,
            },
        )

    @router.get("/ui/workflows", response_class=HTMLResponse)
    def ui_workflows(
        request: Request,
        run_id: str | None = None,
        q: str = "",
        wave: str = "",
        target: str = "",
    ) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        filtered: list[tuple[int, object]] = []
        wave_options: list[str] = []
        target_options: list[str] = []
        if run and run.result:
            wave_options = sorted({a.migration_wave for a in run.result.assessments})
            target_options = sorted({a.recommended_target for a in run.result.assessments})
            query = q.strip().casefold()
            for idx, wf in enumerate(run.result.assessments):
                if wave and wf.migration_wave != wave:
                    continue
                if target and wf.recommended_target != target:
                    continue
                if query:
                    haystack = " ".join(
                        [wf.path, wf.recommended_target, *[t.name for t in wf.tools]]
                    ).casefold()
                    if query not in haystack:
                        continue
                filtered.append((idx, wf))
        return templates.TemplateResponse(
            request,
            "partials/workflows.html",
            {
                "run": run,
                "workflows": filtered,
                "q": q,
                "wave": wave,
                "target": target,
                "wave_options": wave_options,
                "target_options": target_options,
            },
        )

    @router.get(
        "/ui/runs/{identity}/workflows/{workflow_index}/drawer",
        response_class=HTMLResponse,
    )
    def ui_workflow_drawer(
        request: Request,
        identity: str,
        workflow_index: int,
        tab: Annotated[
            Literal["summary", "source", "fabric", "risk", "validation", "deployment"],
            Query(),
        ] = "summary",
    ) -> HTMLResponse:
        service = get_run_service()
        run = service.get_run(identity)
        if run is None or run.result is None:
            raise HTTPException(status_code=404, detail="Completed assessment run not found")
        if workflow_index < 0 or workflow_index >= len(run.result.assessments):
            raise HTTPException(status_code=404, detail="Workflow index out of range")
        wf = run.result.assessments[workflow_index]
        evidence: WorkflowEvidence | None = None
        svg_markup = ""
        if tab in {"source", "risk"}:
            try:
                ev_response = service.evidence(identity, workflow_index)
                if ev_response.workflows:
                    evidence = ev_response.workflows[0]
                    if tab == "source":
                        svg_markup = render_workflow_svg(evidence)
            except (KeyError, OSError, ValueError):
                evidence = None
                svg_markup = '<div class="empty-inline">Workflow graph evidence unavailable.</div>'
        shape = compute_graph_shape(evidence)
        executable_stages: list[dict[str, str]] = []
        if evidence is not None:
            for node in evidence.nodes:
                if (
                    node.disabled
                    or node.support == "neutral"
                    or "container" in node.name.casefold()
                ):
                    continue
                target_lbl, _ = support_metadata(node.support)
                executable_stages.append(
                    {
                        "id": node.id,
                        "name": node.name,
                        "support": node.support,
                        "target": target_lbl,
                    }
                )
        mapping_bars = _tool_mapping_bars([wf])
        design_mappings = [
            {
                "name": t.name,
                "count": t.count,
                "support": t.support,
                "target": support_metadata(t.support)[0],
                "mapping": support_metadata(t.support)[1],
            }
            for t in wf.tools
        ]
        risk = {
            "performance": _review_priority(max(wf.tool_count, shape.depth), 20, 50),
            "operational": _review_priority(
                len(wf.dependencies) + shape.branches + shape.containers, 1, 8
            ),
            "compatibility": _review_priority(len(wf.unsupported_tools), 1, 5),
        }
        return templates.TemplateResponse(
            request,
            "partials/workflow_drawer.html",
            {
                "run": run,
                "wf": wf,
                "workflow_index": workflow_index,
                "tab": tab,
                "evidence": evidence,
                "svg_markup": svg_markup,
                "shape": shape,
                "executable_stages": executable_stages,
                "mapping_bars": mapping_bars,
                "design_mappings": design_mappings,
                "risk": risk,
            },
        )

    @router.get("/ui/technical", response_class=HTMLResponse)
    def ui_technical(
        request: Request,
        run_id: str | None = None,
        register: Annotated[
            Literal["tools", "expressions", "endpoints", "dependencies", "findings", "connections"],
            Query(),
        ] = "tools",
        q: str = "",
        workflow: str = "",
        page: Annotated[int, Query(ge=0)] = 0,
    ) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        assessments = run.result.assessments if (run and run.result) else []
        workflow_names = [a.path for a in assessments]
        tech_kpis = {
            "workflows": len(assessments),
            "tools": sum(a.tool_count for a in assessments),
            "connections": sum(a.connection_count for a in assessments),
            "dependencies": sum(len(a.dependencies) for a in assessments),
            "endpoints": sum(len(a.data_sources) for a in assessments),
            "findings": (run.result.failed_count if (run and run.result) else 0)
            + sum(len(a.unsupported_tools) for a in assessments),
        }
        mapping_bars = _tool_mapping_bars(assessments)

        ev_data: EvidenceResponse | None = None
        if run and run.status == "completed" and register in {
            "tools",
            "expressions",
            "findings",
            "connections",
        }:
            try:
                ev_data = service.evidence(run.id, None)
            except (KeyError, OSError, ValueError):
                ev_data = None

        columns: list[str]
        all_rows: list[list[str]] = []
        if register == "tools":
            columns = ["Workflow", "Tool ID", "Tool", "Mapping", "Fabric Target", "Disabled"]
            if ev_data:
                for ev_wf in ev_data.workflows:
                    for node in ev_wf.nodes:
                        target_lbl, map_lbl = support_metadata(node.support)
                        all_rows.append(
                            [
                                ev_wf.workflow_path,
                                node.id,
                                node.name,
                                map_lbl,
                                target_lbl,
                                "Yes" if node.disabled else "No",
                            ]
                        )
            else:
                for wf in assessments:
                    for t in wf.tools:
                        target_lbl, map_lbl = support_metadata(t.support)
                        all_rows.append(
                            [wf.path, "—", f"{t.name} (x{t.count})", map_lbl, target_lbl, "No"]
                        )
        elif register == "expressions":
            columns = ["Workflow", "Tool ID", "Tool", "Source Expression", "Mapping Guidance"]
            if ev_data:
                for ev_wf in ev_data.workflows:
                    for node in ev_wf.nodes:
                        _, map_lbl = support_metadata(node.support)
                        for expr in node.expressions:
                            all_rows.append(
                                [ev_wf.workflow_path, node.id, node.name, expr, map_lbl]
                            )
        elif register == "endpoints":
            columns = ["Workflow", "Direction", "Platform", "Location"]
            for wf in assessments:
                for ep in wf.data_sources:
                    all_rows.append([wf.path, "Referenced endpoint", wf.source_product, ep])
        elif register == "dependencies":
            columns = ["Workflow", "Type", "Reference"]
            for wf in assessments:
                for dep in wf.dependencies:
                    dep_type = (
                        "Macro reference" if dep.lower().endswith(".yxmc") else "Dependency"
                    )
                    all_rows.append([wf.path, dep_type, dep])
        elif register == "findings":
            columns = ["Workflow", "Tool / Source", "Severity", "Evidence / Action"]
            if run and run.result:
                for failure in run.result.failures:
                    all_rows.append(["Parse failure", "Parser", "High", failure])
            for wf in assessments:
                for u in wf.unsupported_tools:
                    all_rows.append([wf.path, u, "High", f"{u}: explicit redesign required"])
            if ev_data:
                for ev_wf in ev_data.workflows:
                    for finding in ev_wf.findings:
                        all_rows.append([ev_wf.workflow_path, "Workflow", "Medium", finding])
        else:
            columns = ["Workflow", "From Tool", "From Anchor", "To Tool", "To Anchor"]
            if ev_data:
                for ev_wf in ev_data.workflows:
                    for edge in ev_wf.edges:
                        all_rows.append(
                            [
                                ev_wf.workflow_path,
                                edge.source,
                                edge.source_anchor,
                                edge.target,
                                edge.target_anchor,
                            ]
                        )

        query = q.strip().casefold()
        wf_filter = workflow.strip()
        filtered_rows = [
            row
            for row in all_rows
            if (not wf_filter or row[0] == wf_filter)
            and (not query or query in " ".join(row).casefold())
        ]
        page_size = 50
        total_rows = len(filtered_rows)
        total_pages = max(1, (total_rows + page_size - 1) // page_size)
        safe_page = min(page, total_pages - 1)
        start = safe_page * page_size
        return templates.TemplateResponse(
            request,
            "partials/technical.html",
            {
                "run": run,
                "register": register,
                "q": q,
                "workflow_filter": wf_filter,
                "workflow_names": workflow_names,
                "tech_kpis": tech_kpis,
                "mapping_bars": mapping_bars,
                "columns": columns,
                "rows": filtered_rows[start : start + page_size],
                "total_rows": total_rows,
                "page": safe_page,
                "total_pages": total_pages,
            },
        )

    @router.get("/ui/capacity", response_class=HTMLResponse)
    def ui_capacity(request: Request, run_id: str | None = None) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        estimate: CapacityPortfolioEstimate | None = None
        error: str | None = None
        if run and run.status == "completed":
            try:
                estimate = service.get_capacity(run.id)
            except (KeyError, ValueError) as exc:
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "partials/capacity.html",
            {
                "run": run,
                "estimate": estimate,
                "optimized_sku": _optimized_sku(estimate),
                "error": error,
                "saved_notice": None,
            },
        )

    @router.post("/ui/capacity", response_class=HTMLResponse)
    async def ui_update_capacity(request: Request) -> HTMLResponse:
        form = await request.form()
        run_id = str(form.get("run_id", "")).strip()
        service = get_run_service()
        run = service.get_run(run_id)
        estimate: CapacityPortfolioEstimate | None = None
        error: str | None = None
        saved_notice: str | None = None
        if run is not None:
            try:
                current = service.get_capacity(run.id)
                users = max(0, int(str(form.get("active_users_per_day", "1") or "0")))
                days = int(str(form.get("days_per_month", current.settings.days_per_month)))
                util = float(
                    str(form.get("target_utilization", current.settings.target_utilization))
                )
                burst = int(
                    str(form.get("burst_window_minutes", current.settings.burst_window_minutes))
                )
                billing_raw = str(
                    form.get("spark_billing_mode", current.settings.spark_billing_mode)
                )
                billing: Literal["capacity", "autoscale"] = (
                    "autoscale" if billing_raw == "autoscale" else "capacity"
                )
                updated_settings = CapacitySettings(
                    **{
                        **current.settings.model_dump(),
                        "days_per_month": days,
                        "target_utilization": util,
                        "active_users_per_day": users,
                        "peak_concurrent_users": users,
                        "burst_window_minutes": burst,
                        "spark_billing_mode": billing,
                    }
                )
                wf_inputs: list[CapacityWorkflowInput] = []
                for idx in range(len(current.workflows)):
                    path_val = form.get(f"wf_path_{idx}")
                    if path_val is None:
                        continue
                    refresh_val = int(
                        str(
                            form.get(
                                f"wf_refresh_{idx}",
                                current.workflows[idx].refresh_interval_minutes,
                            )
                        )
                    )
                    rows_val = int(
                        str(
                            form.get(
                                f"wf_rows_{idx}",
                                current.workflows[idx].source_rows_per_run,
                            )
                        )
                    )
                    vcores_raw = form.get(f"wf_vcores_{idx}")
                    vcores_val = (
                        int(str(vcores_raw))
                        if vcores_raw is not None and str(vcores_raw).strip()
                        else current.workflows[idx].spark_vcores
                    )
                    rt_raw = form.get(f"wf_runtime_{idx}")
                    rt_val = (
                        int(str(rt_raw).strip())
                        if rt_raw is not None and str(rt_raw).strip()
                        else None
                    )
                    wf_inputs.append(
                        CapacityWorkflowInput(
                            workflow_path=str(path_val),
                            refresh_interval_minutes=refresh_val,
                            source_rows_per_run=rows_val,
                            spark_vcores=vcores_val,
                            notebook_runtime_seconds=rt_val,
                            confirmed=True,
                        )
                    )
                estimate = service.update_capacity(
                    run.id, CapacityUpdate(inputs=wf_inputs, settings=updated_settings)
                )
                saved_notice = (
                    f"Saved and recalculated capacity across {len(wf_inputs)} workflow(s)."
                    if wf_inputs
                    else "Updated portfolio capacity settings."
                )
            except (KeyError, ValueError) as exc:
                error = str(exc)
                estimate = _safe_capacity(service, run)
        return templates.TemplateResponse(
            request,
            "partials/capacity.html",
            {
                "run": run,
                "estimate": estimate,
                "optimized_sku": _optimized_sku(estimate),
                "error": error,
                "saved_notice": saved_notice,
            },
        )

    @router.get("/ui/deployment", response_class=HTMLResponse)
    def ui_deployment(
        request: Request,
        run_id: str | None = None,
        workflow: str = "",
    ) -> HTMLResponse:
        service = get_run_service()
        run = _resolve_active_run(service, run_id)
        if run and run.status == "completed" and run.routing_basis != "compiler_confirmed":
            try:
                service.ensure_fabric_fit(run.id)
                refreshed = service.get_run(run.id)
                if refreshed is not None:
                    run = refreshed
            except (KeyError, OSError, ValueError):
                pass
        targets = get_target_registry().list_providers()
        assessments = run.result.assessments if (run and run.result) else []
        selected_wf = next((wf for wf in assessments if wf.path == workflow), None)
        dep_counts = _deployment_counts(assessments)
        return templates.TemplateResponse(
            request,
            "partials/deployment.html",
            {
                "run": run,
                "targets": targets,
                "selected_workflow": workflow,
                "selected_wf": selected_wf,
                "dep_counts": dep_counts,
            },
        )

    @router.post("/ui/deployment/preview", response_class=HTMLResponse)
    def ui_deployment_preview(
        request: Request,
        run_id: Annotated[str, Form()],
        target: Annotated[str, Form()] = "recommended",
        workflow: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        service = get_run_service()
        error: str | None = None
        candidates: list[Candidate] = []
        valid_targets: tuple[Target, ...] = ("dataflow", "notebook", "pipeline")
        chosen = target if target in (*valid_targets, "recommended", "all") else "recommended"
        try:
            if chosen == "recommended":
                run = service.get_run(run_id)
                assessments = run.result.assessments if (run and run.result) else []
                scoped = [wf for wf in assessments if not workflow or wf.path == workflow]
                needed_targets: list[Target] = []
                wf_target_map: dict[str, set[Target]] = {}
                for wf in scoped:
                    rec = _recommended_targets_for(wf)
                    wf_target_map[wf.path] = set(rec)
                    for t in rec:
                        if t not in needed_targets:
                            needed_targets.append(t)
                by_target = {t: service.preview_candidates(run_id, t) for t in needed_targets}
                for wf in scoped:
                    for t in _recommended_targets_for(wf):
                        candidates.extend(
                            c for c in by_target.get(t, []) if c.workflow == wf.path
                        )
            elif chosen == "all":
                all_candidates = [
                    c
                    for t in valid_targets
                    for c in service.preview_candidates(run_id, t)
                ]
                candidates = (
                    [c for c in all_candidates if c.workflow == workflow]
                    if workflow
                    else all_candidates
                )
            else:
                all_candidates = service.preview_candidates(run_id, cast(Target, chosen))
                candidates = (
                    [c for c in all_candidates if c.workflow == workflow]
                    if workflow
                    else all_candidates
                )
        except (KeyError, OSError, ValueError) as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "partials/deployment_preview.html",
            {
                "candidates": candidates,
                "target": chosen,
                "error": error,
            },
        )

    @router.post("/ui/deployment/dry-run", response_class=HTMLResponse)
    def ui_deployment_dry_run(
        request: Request,
        run_id: Annotated[str, Form()],
        workspace_id: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        service = get_run_service()
        try:
            candidates = [
                c
                for t in cast(tuple[Target, ...], ("dataflow", "notebook", "pipeline"))
                for c in service.preview_candidates(run_id, t)
            ]
            result = dry_run(
                candidates,
                approved_workspace=os.getenv("ACCELERATOR_DEV_WORKSPACE"),
                requested_workspace=workspace_id.strip() or "dev",
            )
            return templates.TemplateResponse(
                request,
                "partials/deployment_result.html",
                {
                    "error": None,
                    "title": "Dry-Run Validation Result",
                    "status": "allowed" if result.allowed else "blocked",
                    "ok": result.allowed,
                    "message": (
                        f"Planned mutations: {len(result.planned_mutations)} · "
                        f"Blockers: {len(result.blockers)}"
                    ),
                    "details": json.dumps(result.model_dump(mode="json"), indent=2),
                },
            )
        except (KeyError, OSError, ValueError) as exc:
            return templates.TemplateResponse(
                request,
                "partials/deployment_result.html",
                {"error": str(exc)},
            )

    @router.post("/ui/deployment/dispatch", response_class=HTMLResponse)
    def ui_deployment_dispatch(
        request: Request,
        run_id: Annotated[str, Form()],
        provider: Annotated[Literal["github", "gitlab"], Form()],
        repository: Annotated[str, Form()],
        source_workflow: Annotated[str, Form()],
        target: Annotated[
            Literal["recommended", "notebook", "dataflow", "pipeline", "bundle"], Form()
        ] = "recommended",
        ref: Annotated[str, Form()] = "main",
    ) -> HTMLResponse:
        try:
            resolved_target: DeploymentTarget
            if target == "recommended":
                run = get_run_service().get_run(run_id)
                assessments = run.result.assessments if (run and run.result) else []
                matched = next(
                    (wf for wf in assessments if wf.path == source_workflow.strip()), None
                )
                resolved_target = (
                    _recommended_deployment_target(matched) if matched is not None else "notebook"
                )
            else:
                resolved_target = target
            if provider == "github":
                gh_res = dispatch_github(
                    run_id,
                    GitHubDeploymentSelection(
                        repository_url=repository.strip(),
                        source_workflow=source_workflow.strip(),
                        target=resolved_target,
                        ref=ref.strip() or None,
                    ),
                )
                payload = gh_res.model_dump(mode="json")
                title = "GitHub Actions Dispatch"
            else:
                gl_res = dispatch_gitlab(
                    run_id,
                    GitLabDeploymentSelection(
                        project_url=repository.strip(),
                        source_workflow=source_workflow.strip(),
                        target=resolved_target,
                        ref=ref.strip() or None,
                    ),
                )
                payload = gl_res.model_dump(mode="json")
                title = "GitLab CI Dispatch"
            return templates.TemplateResponse(
                request,
                "partials/deployment_result.html",
                {
                    "error": None,
                    "title": title,
                    "status": "dispatched",
                    "ok": True,
                    "message": "Workflow dispatch accepted.",
                    "details": json.dumps(payload, indent=2),
                },
            )
        except (HTTPException, KeyError, OSError, ValueError) as exc:
            detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            return templates.TemplateResponse(
                request,
                "partials/deployment_result.html",
                {"error": detail},
            )

    return router
