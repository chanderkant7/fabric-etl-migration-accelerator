(function () {
  "use strict";

  const rootEl = document.documentElement;
  const savedTheme = localStorage.getItem("accelerator-theme");
  if (savedTheme === "dark" || savedTheme === "light") {
    rootEl.setAttribute("data-theme", savedTheme);
  }

  document.addEventListener("htmx:configRequest", function (event) {
    const token = document.body.getAttribute("data-csrf-token");
    if (token) {
      event.detail.headers["x-accelerator-token"] = token;
    }
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail && event.detail.target && event.detail.target.id === "stage") {
      const marker = event.detail.target.querySelector("[data-page-nav]");
      if (marker) {
        window.setActiveNav(marker.getAttribute("data-page-nav"));
      }
    }
  });

  window.toggleTheme = function () {
    const next = rootEl.getAttribute("data-theme") === "dark" ? "light" : "dark";
    rootEl.setAttribute("data-theme", next);
    localStorage.setItem("accelerator-theme", next);
  };

  window.setActiveNav = function (section) {
    document.querySelectorAll(".nav-btn[data-nav]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-nav") === section);
    });
  };

  window.closeDrawer = function () {
    const host = document.getElementById("drawer-host");
    if (host) {
      host.innerHTML = "";
    }
  };

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      window.closeDrawer();
    }
  });

  window.browseLocalFolder = async function (inputId) {
    const token = document.body.getAttribute("data-csrf-token") || "";
    try {
      const resp = await fetch("/api/snapshots/browse-folder", {
        method: "POST",
        headers: { "x-accelerator-token": token },
      });
      if (!resp.ok) {
        return;
      }
      const data = await resp.json();
      if (data && data.path) {
        const input = document.getElementById(inputId);
        if (input) {
          input.value = data.path;
        }
      }
    } catch (_err) {
      // Folder picker unavailable; user can type path directly.
    }
  };

  window.copyCodeBlock = function (elementId, buttonEl) {
    const pre = document.getElementById(elementId);
    if (!pre || !navigator.clipboard) {
      return;
    }
    navigator.clipboard.writeText(pre.textContent || "").then(function () {
      if (buttonEl) {
        const orig = buttonEl.textContent;
        buttonEl.textContent = "Copied";
        setTimeout(function () {
          buttonEl.textContent = orig;
        }, 1200);
      }
    });
  };

  let dagZoom = 1.0;
  let dagMatchIndex = -1;

  function applyDagZoom(nextZoom) {
    const svg = document.getElementById("source-dag-svg");
    if (!svg) return;
    dagZoom = Math.max(0.25, Math.min(2.5, nextZoom));
    const baseW = Number(svg.getAttribute("data-base-width") || 800);
    const baseH = Number(svg.getAttribute("data-base-height") || 360);
    svg.setAttribute("width", String(Math.round(baseW * dagZoom)));
    svg.setAttribute("height", String(Math.round(baseH * dagZoom)));
    const label = document.getElementById("dag-zoom-label");
    if (label) {
      label.textContent = Math.round(dagZoom * 100) + "%";
    }
  }

  window.dagZoomBy = function (delta) {
    applyDagZoom(dagZoom + delta);
  };

  window.dagZoomReset = function () {
    applyDagZoom(1.0);
  };

  window.dagZoomFit = function () {
    const svg = document.getElementById("source-dag-svg");
    const wrap = document.getElementById("source-dag-viewport");
    if (!svg || !wrap) return;
    const baseW = Number(svg.getAttribute("data-base-width") || 800);
    const baseH = Number(svg.getAttribute("data-base-height") || 360);
    const fitW = Math.max(100, wrap.clientWidth - 28) / baseW;
    const fitH = Math.max(100, wrap.clientHeight - 28) / baseH;
    applyDagZoom(Math.min(1.2, fitW, fitH));
  };

  window.selectDagNode = function (nodeEl) {
    if (!nodeEl) return;
    document.querySelectorAll("#source-dag-svg .dag-node.dag-selected").forEach(function (el) {
      el.classList.remove("dag-selected");
    });
    nodeEl.classList.add("dag-selected");
    const inspector = document.getElementById("dag-inspector");
    if (!inspector) return;
    try {
      const meta = JSON.parse(nodeEl.getAttribute("data-meta") || "{}");
      let html =
        "<strong>Tool #" +
        escapeHtml(meta.id) +
        " · " +
        escapeHtml(meta.label || meta.name) +
        "</strong>" +
        ' <span class="badge">' +
        escapeHtml(meta.name) +
        "</span>" +
        ' <span class="badge badge-info">' +
        escapeHtml(meta.target || meta.support) +
        "</span>";
      if (meta.disabled) {
        html += ' <span class="badge badge-warn">Disabled</span>';
      }
      html +=
        '<div style="color:var(--muted);margin-top:4px;">Plugin: <code>' +
        escapeHtml(meta.plugin || "") +
        "</code> · Mapping: " +
        escapeHtml(meta.mapping || meta.support || "") +
        "</div>";
      if (meta.expressions && meta.expressions.length) {
        html +=
          '<div style="margin-top:6px;"><strong>Expressions:</strong> <code>' +
          meta.expressions.map(escapeHtml).join(" | ") +
          "</code></div>";
      }
      if (meta.issues && meta.issues.length) {
        html +=
          '<div style="margin-top:6px;color:var(--err-fg);"><strong>Issues:</strong> ' +
          meta.issues.map(escapeHtml).join(" · ") +
          "</div>";
      }
      inspector.innerHTML = html;
    } catch (_err) {
      inspector.textContent = "Tool metadata unavailable.";
    }
  };

  window.findDagNode = function () {
    const input = document.getElementById("dag-search-input");
    const svg = document.getElementById("source-dag-svg");
    if (!input || !svg) return;
    const query = (input.value || "").trim().toLowerCase();
    const nodes = Array.from(svg.querySelectorAll(".dag-node"));
    const matches = query
      ? nodes.filter(function (n) {
          return (n.getAttribute("data-search") || "").indexOf(query) !== -1;
        })
      : [];
    const status = document.getElementById("dag-search-status");
    if (!matches.length) {
      if (status) status.textContent = query ? "No matching tools" : "";
      return;
    }
    dagMatchIndex = (dagMatchIndex + 1) % matches.length;
    const target = matches[dagMatchIndex];
    window.selectDagNode(target);
    target.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    if (status) {
      status.textContent = "Match " + (dagMatchIndex + 1) + " of " + matches.length;
    }
  };

  window.updateDeliverySensitivity = function (totalEffortDays) {
    const teamEl = document.getElementById("plan-team-size");
    const allocEl = document.getElementById("plan-allocation");
    if (!teamEl || !allocEl) return;
    const team = Number(teamEl.value || 1);
    const alloc = Number(allocEl.value || 80);
    const fte = (team * alloc) / 100;
    const weeks = fte > 0 ? totalEffortDays / fte / 5 : 0;
    const teamVal = document.getElementById("plan-team-val");
    const allocVal = document.getElementById("plan-alloc-val");
    const fteVal = document.getElementById("plan-fte-val");
    const weeksVal = document.getElementById("plan-weeks-val");
    if (teamVal) teamVal.textContent = team + " people";
    if (allocVal) allocVal.textContent = alloc + "%";
    if (fteVal) fteVal.textContent = fte.toFixed(1) + " FTE";
    if (weeksVal) weeksVal.textContent = weeks.toFixed(1) + " weeks";
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }
})();
