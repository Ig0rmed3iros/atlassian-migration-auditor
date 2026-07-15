"""Env-fix screen: list app-tier findings as checkboxes, human/unfixable as
read-only guidance, and launch a consent-gated env_fix run.

Routes:
  GET  /runs/{id}/env-fix   — show the fix screen for an env_audit run
  POST /runs/{id}/env-fix   — start an env_fix run with selected findings
  GET  /env-fix-runs/{id}   — result page (reuses fix_run.html)
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auditor.envaudit.fixes import _FIXES

_HERE = os.path.dirname(__file__)
_templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

# App-tier kinds that can be selected and applied.
_APP_TIER_KINDS = frozenset(
    k for k, v in _FIXES.items() if v.get("tier") == "app"
)


# Worst-first severity ordering for the groups (so the biggest problems lead).
_SEV_RANK = {"high": 0, "critical": 0, "medium": 1, "med": 1, "low": 2}


def _group_findings(items: list) -> list:
    """Group findings BY PROBLEM TYPE (kind) for the fix screen — used for ALL
    three tiers (app / human / unfixable).

    Returns one group per kind, each carrying the shared fix metadata (shown
    once) + the individual items (for the expander). Sorted worst-severity-first
    then by count desc, so the largest/most-severe problems lead. A flat list of
    hundreds of rows is unusable (component_no_lead alone can be 700+); this
    collapses each kind to a single expandable group."""
    groups: dict[str, dict] = {}
    for it in items:
        kind = it.get("kind") or ""
        g = groups.get(kind)
        if g is None:
            fix = it.get("fix") or {}
            g = {
                "kind": kind,
                "label": fix.get("label") or fix.get("title") or kind,
                "risk": fix.get("risk") or "low",
                "detail": fix.get("detail") or "",
                "caveat": fix.get("caveat") or "",
                "api_hint": fix.get("api_hint") or "",
                "items": [],
                "_sev": 3,
            }
            groups[kind] = g
        g["items"].append({"name": it.get("name") or "", "ref": it.get("ref") or ""})
        g["_sev"] = min(g["_sev"],
                        _SEV_RANK.get((it.get("severity") or "").lower(), 3))
    out = list(groups.values())
    for g in out:
        g["count"] = len(g["items"])
        g["items"].sort(key=lambda x: x["name"])
    out.sort(key=lambda g: (g["_sev"], -g["count"], g["label"].lower()))
    return out


def _env_findings_for_screen(store, run_id: int) -> tuple[list, list, list]:
    """Return (app_items, human_items, unfixable_items) for the fix screen.

    Each item: dict with name, kind, severity, fix, category.
    Server re-derives tier from the stored kind (I4).
    """
    app_items, human_items, unfixable_items = [], [], []
    for area in store.config_areas(run_id):
        for row in store.query_config(run_id, area):
            kind = row.get("kind") or ""
            detail = row.get("detail") or {}
            fix = detail.get("fix") or _FIXES.get(kind) or {}
            item = {
                "name": row.get("name") or "",
                "kind": kind,
                "area": row.get("area") or "",
                "severity": detail.get("severity") or "",
                "category": detail.get("category") or "",
                "fix": fix,
                # Ref used in the form: "kind:name"
                "ref": f"{kind}:{row.get('name') or ''}",
            }
            # Re-derive tier from registry (I4)
            fix_entry = _FIXES.get(kind)
            tier = fix_entry.get("tier") if fix_entry else "human"
            if tier == "app":
                app_items.append(item)
            elif tier == "unfixable":
                unfixable_items.append(item)
            else:
                human_items.append(item)
    return app_items, human_items, unfixable_items


def make_env_fix_router(store, engine) -> APIRouter:
    router = APIRouter()

    @router.get("/runs/{run_id}/env-fix", response_class=HTMLResponse)
    def env_fix_screen(request: Request, run_id: int, error: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        # Only valid for env_audit source runs
        if run.get("kind") != "env_audit":
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        mig = store.get_migration(run["migration_id"])
        app_items, human_items, unfixable_items = _env_findings_for_screen(
            store, run_id)
        return _templates.TemplateResponse(request, "env_fix.html", {
            "run": run, "mig": mig, "error": error,
            "app_groups": _group_findings(app_items),
            "human_groups": _group_findings(human_items),
            "unfixable_groups": _group_findings(unfixable_items),
        })

    @router.post("/runs/{run_id}/env-fix")
    def start_env_fix(run_id: int,
                      finding_refs: str = Form(""),
                      consent: str = Form(""),
                      dry_run: str = Form("")):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if run.get("kind") != "env_audit":
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        refs = [r.strip() for r in finding_refs.split(",") if r.strip()]
        if not refs:
            return RedirectResponse(
                f"/runs/{run_id}/env-fix?error=Select at least one finding",
                status_code=303)
        is_preview = bool(dry_run)
        # H1: consent is enforced server-side for a LIVE write run (it deletes
        # schemes/groups on the audited instance). A dry-run PREVIEW issues no
        # write — every apply guard runs but stops before the DELETE/PUT — so it
        # is exempt from the consent gate. A live run with absent/falsey consent
        # is refused (no engine.start, nothing written).
        if not is_preview and not consent:
            return RedirectResponse(
                f"/runs/{run_id}/env-fix?error="
                f"You must confirm consent before applying fixes",
                status_code=303)
        try:
            rid = engine.start(
                run["migration_id"],
                {"finding_refs": refs, "consent": bool(consent),
                 "dry_run": is_preview},
                kind="env_fix",
                source_run_id=run_id)
        except RuntimeError as exc:
            return RedirectResponse(
                f"/runs/{run_id}/env-fix?error={exc}", status_code=303)
        return RedirectResponse(f"/env-fix-runs/{rid}", status_code=303)

    @router.get("/env-fix-runs/{run_id}", response_class=HTMLResponse)
    def env_fix_run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        stats = json.loads(run.get("stats_json") or "{}")
        # Surface warn/error events so a run that FAILED before applying anything
        # (e.g. the destructive-ops cap aborted the whole batch) shows the actual
        # reason instead of a bare FAILED page with "No actions recorded yet".
        notes = [e for e in store.get_events(run_id)
                 if e.get("level") in ("warn", "error")]
        return _templates.TemplateResponse(request, "fix_run.html", {
            "run": run, "actions": store.get_fix_actions(run_id),
            "mig": store.get_migration(run["migration_id"]),
            "notes": notes,
            "stats": stats or None})

    return router
