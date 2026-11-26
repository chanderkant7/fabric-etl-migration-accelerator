"""Deterministic pure-Python SVG DAG renderer and graph-shape analyzer."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass
from html import escape

from etl_portfolio_assessment.evidence.extractor import EvidenceNode, WorkflowEvidence

_NODE_W = 136.0
_NODE_H = 48.0
_MIN_GAP_X = 24.0
_MIN_GAP_Y = 20.0
_PAD = 36.0

# Engine support values: dataflow, notebook, pipeline, manual, neutral
_SUPPORT_STYLE: dict[str, tuple[str, str, str, str, str]] = {
    "dataflow": ("#ecfdf3", "#15803d", "#14532d", "Dataflow Gen2", "Rewrite in Fabric"),
    "supported": ("#ecfdf3", "#15803d", "#14532d", "Dataflow Gen2", "Rewrite in Fabric"),
    "native": ("#ecfdf3", "#15803d", "#14532d", "Dataflow Gen2", "Rewrite in Fabric"),
    "notebook": ("#eff6ff", "#1d4ed8", "#1e3a8a", "Fabric Notebook", "Requires new implementation"),
    "pipeline": ("#f5f3ff", "#6d28d9", "#4c1d95", "Fabric Data Pipeline", "Rewrite in Fabric"),
    "partial": ("#fffbeb", "#b45309", "#78350f", "Fabric (review)", "Requires review"),
    "manual": ("#fef2f2", "#b91c1c", "#7f1d1d", "Manual redesign", "Requires new implementation"),
    "unsupported": (
        "#fef2f2",
        "#b91c1c",
        "#7f1d1d",
        "Manual redesign",
        "Requires new implementation",
    ),
    "neutral": ("#f8fafc", "#64748b", "#334155", "Documentation", "No equivalent needed"),
    "disabled": ("#f3f4f6", "#9ca3af", "#6b7280", "Disabled in source", "Disabled"),
}


@dataclass(frozen=True)
class NodeBox:
    id: str
    x: float
    y: float
    width: float
    height: float
    is_container: bool


@dataclass(frozen=True)
class GraphShape:
    branches: int
    depth: int
    containers: int
    cycles: bool


def support_metadata(support: str) -> tuple[str, str]:
    """Return (fabric_target, mapping_label) for a node support classification."""
    entry = _SUPPORT_STYLE.get(support.strip().casefold())
    if entry is None:
        return ("Target not determined", "Requires review")
    return (entry[3], entry[4])


def compute_graph_shape(evidence: WorkflowEvidence | None) -> GraphShape:
    """Compute structural graph metrics for risk and summary views."""
    if evidence is None or evidence.parse_status == "failed" or not evidence.nodes:
        return GraphShape(branches=0, depth=0, containers=0, cycles=False)
    node_ids = [n.id for n in evidence.nodes]
    id_set = set(node_ids)
    outgoing: dict[str, list[str]] = {nid: [] for nid in node_ids}
    indegree: dict[str, int] = dict.fromkeys(node_ids, 0)
    for edge in evidence.edges:
        if edge.source in id_set and edge.target in id_set and edge.source != edge.target:
            outgoing[edge.source].append(edge.target)
            indegree[edge.target] += 1

    queue = deque([nid for nid in node_ids if indegree[nid] == 0])
    depth: dict[str, int] = dict.fromkeys(node_ids, 1)
    processed = 0
    while queue:
        src = queue.popleft()
        processed += 1
        for dst in outgoing[src]:
            depth[dst] = max(depth[dst], depth[src] + 1)
            indegree[dst] -= 1
            if indegree[dst] == 0:
                queue.append(dst)

    return GraphShape(
        branches=sum(1 for targets in outgoing.values() if len(targets) > 1),
        depth=max(depth.values(), default=0),
        containers=sum(1 for n in evidence.nodes if "container" in n.name.casefold()),
        cycles=processed < len(node_ids),
    )


def _truncate(text: str, limit: int = 18) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[: limit - 1]}…"


def _anchor_ratio(anchor: str, side: str) -> float:
    norm = anchor.strip().casefold()
    if norm == "left":
        return 0.25 if side == "source" else 0.35
    if norm == "right":
        return 0.75 if side == "source" else 0.65
    if norm == "true":
        return 0.35
    if norm == "false":
        return 0.65
    if norm == "control":
        return 0.2
    return 0.5


def _edge_path(src: NodeBox, dst: NodeBox, src_anchor: str, dst_anchor: str) -> str:
    x1 = src.x + src.width
    y1 = src.y + src.height * _anchor_ratio(src_anchor, "source")
    x2 = dst.x
    y2 = dst.y + dst.height * _anchor_ratio(dst_anchor, "target")
    if x2 >= x1:
        ctrl = max(28.0, (x2 - x1) * 0.45)
        return (
            f"M {x1:.1f} {y1:.1f} C {x1 + ctrl:.1f} {y1:.1f}, "
            f"{x2 - ctrl:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
        )
    detour = max(x1, x2) + 52.0
    return (
        f"M {x1:.1f} {y1:.1f} C {detour:.1f} {y1:.1f}, "
        f"{detour:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
    )


def _deoverlap_non_containers(boxes: dict[str, NodeBox]) -> dict[str, NodeBox]:
    """Push apart overlapping tool nodes while preserving relative order."""
    mutable: dict[str, list[float]] = {
        nid: [b.x, b.y, b.width, b.height, 1.0 if b.is_container else 0.0]
        for nid, b in boxes.items()
    }
    tool_ids = sorted(
        [nid for nid, b in boxes.items() if not b.is_container],
        key=lambda nid: (boxes[nid].x, boxes[nid].y, nid),
    )
    for _ in range(10):
        moved = False
        for i, a_id in enumerate(tool_ids):
            ax, ay, aw, ah, _ = mutable[a_id]
            for b_id in tool_ids[i + 1 :]:
                bx, by, bw, bh, _ = mutable[b_id]
                if (
                    bx < ax + aw + _MIN_GAP_X
                    and ax < bx + bw + _MIN_GAP_X
                    and by < ay + ah + _MIN_GAP_Y
                    and ay < by + bh + _MIN_GAP_Y
                ):
                    if abs(bx - ax) >= abs(by - ay):
                        mutable[b_id][0] = ax + aw + _MIN_GAP_X
                    else:
                        mutable[b_id][1] = ay + ah + _MIN_GAP_Y
                    moved = True
        if not moved:
            break

    min_x = min((v[0] for v in mutable.values()), default=0.0)
    min_y = min((v[1] for v in mutable.values()), default=0.0)
    shift_x = _PAD - min_x if min_x < _PAD else 0.0
    shift_y = _PAD - min_y if min_y < _PAD else 0.0
    return {
        nid: NodeBox(
            id=nid,
            x=round(v[0] + shift_x, 1),
            y=round(v[1] + shift_y, 1),
            width=round(v[2], 1),
            height=round(v[3], 1),
            is_container=bool(v[4]),
        )
        for nid, v in mutable.items()
    }


def _compute_layout(evidence: WorkflowEvidence) -> dict[str, NodeBox]:
    """Compute non-overlapping coordinates, preserving source layout + containers when present."""
    nodes = evidence.nodes
    if not nodes:
        return {}

    if evidence.layout_source == "source" and any(
        n.x is not None and n.y is not None for n in nodes
    ):
        scale = 1.65
        raw: dict[str, NodeBox] = {}
        for node in nodes:
            is_container = (
                "toolcontainer" in node.name.casefold()
                and node.width is not None
                and node.height is not None
            )
            w = max(float(node.width or 0.0) * scale, _NODE_W + 40.0) if is_container else _NODE_W
            h = max(float(node.height or 0.0) * scale, _NODE_H + 40.0) if is_container else _NODE_H
            x = float(node.x or 0.0) * scale
            y = float(node.y or 0.0) * scale
            raw[node.id] = NodeBox(
                id=node.id,
                x=x if is_container else x - w / 2.0,
                y=y if is_container else y - h / 2.0,
                width=w,
                height=h,
                is_container=is_container,
            )
        min_x = min(b.x for b in raw.values())
        min_y = min(b.y for b in raw.values())
        shifted = {
            nid: NodeBox(
                id=nid,
                x=b.x - min_x + _PAD,
                y=b.y - min_y + _PAD,
                width=b.width,
                height=b.height,
                is_container=b.is_container,
            )
            for nid, b in raw.items()
        }
        return _deoverlap_non_containers(shifted)

    node_ids = [n.id for n in nodes]
    id_set = set(node_ids)
    level: dict[str, int] = dict.fromkeys(node_ids, 0)
    for _ in range(len(node_ids)):
        changed = False
        for edge in evidence.edges:
            if edge.source in id_set and edge.target in id_set and edge.source != edge.target:
                candidate = level[edge.source] + 1
                if candidate > level[edge.target]:
                    level[edge.target] = candidate
                    changed = True
        if not changed:
            break

    columns: dict[int, list[str]] = defaultdict(list)
    for nid in node_ids:
        columns[level[nid]].append(nid)

    boxes: dict[str, NodeBox] = {}
    col_step = _NODE_W + 64.0
    row_step = _NODE_H + 34.0
    for col_idx in sorted(columns):
        for row_idx, nid in enumerate(columns[col_idx]):
            boxes[nid] = NodeBox(
                id=nid,
                x=_PAD + col_idx * col_step,
                y=_PAD + row_idx * row_step,
                width=_NODE_W,
                height=_NODE_H,
                is_container=False,
            )
    return boxes


def _style_for(node: EvidenceNode) -> tuple[str, str, str, str, str]:
    if node.disabled:
        return _SUPPORT_STYLE["disabled"]
    key = node.support.strip().casefold()
    return _SUPPORT_STYLE.get(
        key,
        ("#f8fafc", "#475569", "#0f172a", "Target not determined", "Requires review"),
    )


def render_workflow_svg(evidence: WorkflowEvidence) -> str:
    """Render an interactive, non-overlapping inline SVG source workflow DAG."""
    if evidence.parse_status == "failed":
        detail = escape(" · ".join(evidence.findings) or "Parse failed")
        return (
            f'<div class="banner banner-err">'
            f"<span>Source graph unavailable: {detail}</span></div>"
        )
    if not evidence.nodes:
        return (
            '<div class="empty-inline">No graph nodes available for this workflow '
            "(static diagram unavailable).</div>"
        )

    boxes = _compute_layout(evidence)
    max_x = max(b.x + b.width for b in boxes.values()) + _PAD
    max_y = max(b.y + b.height for b in boxes.values()) + _PAD + 18.0
    width = max(int(max_x), 760)
    height = max(int(max_y), 320)

    parts: list[str] = [
        f'<svg class="dag-svg" id="source-dag-svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" data-base-width="{width}" '
        f'data-base-height="{height}" role="img" '
        f'aria-label="{escape(evidence.workflow_path)} source workflow diagram">',
        "<defs>"
        '<marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 1 L 10 5 L 0 9 z" fill="#64748b" />'
        "</marker>"
        "</defs>",
    ]

    for node in evidence.nodes:
        box = boxes.get(node.id)
        if box is None or not box.is_container:
            continue
        meta = json.dumps(
            {
                "id": node.id,
                "name": node.name,
                "label": node.label or node.name,
                "plugin": node.plugin,
                "support": node.support,
                "target": "Container / Query Group",
                "mapping": "Preserved as queryGroup",
                "disabled": node.disabled,
                "expressions": node.expressions[:5],
                "issues": node.issues[:5],
            }
        )
        search_txt = f"{node.id} {node.name} {node.label}".casefold()
        parts.append(
            f'<g class="dag-node dag-container" data-node-id="{escape(node.id)}" '
            f'data-search="{escape(search_txt)}" '
            f'data-meta="{escape(meta)}" tabindex="0" role="button" '
            f'onclick="selectDagNode(this)">'
            f'<rect class="dag-rect" x="{box.x:.1f}" y="{box.y:.1f}" '
            f'width="{box.width:.1f}" height="{box.height:.1f}" rx="10" ry="10" '
            'fill="rgba(148,163,184,0.08)" stroke="#94a3b8" stroke-width="1.4" '
            'stroke-dasharray="6 4" />'
            f'<text x="{box.x + 10:.1f}" y="{box.y + 18:.1f}" font-size="11.5" '
            f'font-weight="700" fill="#475569">'
            f"{escape(_truncate(node.label or node.name, 34))}</text>"
            "</g>"
        )

    for edge in evidence.edges:
        src = boxes.get(edge.source)
        dst = boxes.get(edge.target)
        if src is None or dst is None or src.is_container or dst.is_container:
            continue
        d = _edge_path(src, dst, edge.source_anchor, edge.target_anchor)
        parts.append(
            f'<path class="dag-edge" d="{d}" fill="none" stroke="#94a3b8" '
            'stroke-width="1.6" marker-end="url(#dag-arrow)" />'
        )

    for node in evidence.nodes:
        box = boxes.get(node.id)
        if box is None or box.is_container:
            continue
        fill, stroke, text_color, fabric_target, mapping_label = _style_for(node)
        title = _truncate(node.name or node.plugin.rsplit(".", 1)[-1], 18)
        sub = f"#{node.id} · {_truncate(fabric_target, 16)}"
        annotation = (
            _truncate(node.label, 22)
            if node.label and node.label.strip() != node.name.strip()
            else ""
        )
        tooltip = escape(
            f"Tool #{node.id}: {node.label or node.name} ({node.plugin}) "
            f"— {node.support} → {fabric_target}"
            + (" [disabled]" if node.disabled else "")
        )
        meta = json.dumps(
            {
                "id": node.id,
                "name": node.name,
                "label": node.label or node.name,
                "plugin": node.plugin,
                "support": node.support,
                "target": fabric_target,
                "mapping": mapping_label,
                "disabled": node.disabled,
                "expressions": node.expressions[:5],
                "issues": node.issues[:5],
            }
        )
        dash = ' stroke-dasharray="4 3"' if node.disabled else ""
        search_text = (
            f"{node.id} {node.name} {node.label} {node.support} {fabric_target}".casefold()
        )
        parts.append(
            f'<g class="dag-node" data-node-id="{escape(node.id)}" '
            f'data-search="{escape(search_text)}" data-meta="{escape(meta)}" '
            f'transform="translate({box.x:.1f},{box.y:.1f})" tabindex="0" role="button" '
            f'onclick="selectDagNode(this)">'
            f"<title>{tooltip}</title>"
            f'<rect class="dag-rect" width="{box.width:.1f}" height="{box.height:.1f}" '
            f'rx="7" ry="7" fill="{fill}" stroke="{stroke}" stroke-width="1.7"{dash} />'
            f'<text x="10" y="19" font-size="11.5" font-weight="700" fill="{text_color}">'
            f"{escape(title)}</text>"
            f'<text x="10" y="36" font-size="10" fill="#475569">{escape(sub)}</text>'
            + (
                f'<text x="{box.width / 2:.1f}" y="{box.height + 13:.1f}" '
                f'font-size="9.5" text-anchor="middle" fill="#64748b">{escape(annotation)}</text>'
                if annotation
                else ""
            )
            + "</g>"
        )

    parts.append("</svg>")
    return "".join(parts)
