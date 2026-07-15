"""Fix options screen, fix-run launcher, fix-run results page."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auditor.remediation.registry import FIXES, fixes_for, get_fix
from auditor.remediation.guidance import guidance_for

_HERE = os.path.dirname(__file__)
_templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


def make_fix_router(store, engine) -> APIRouter:
    router = APIRouter()

    def _fixable(run_id, product):
        groups = {}
        for area in store.config_areas(run_id):
            for f in store.query_config(run_id, area):
                for fx in fixes_for(product, f):
                    groups.setdefault(fx.fix_id, {
                        "fix": fx,
                        "findings": [],       # payload-bearing (actionable)
                        "no_payload": [],     # I12: payload-less (disabled)
                    })
                    if f.get("fix_payload") is not None:
                        groups[fx.fix_id]["findings"].append(f)
                    else:
                        groups[fx.fix_id]["no_payload"].append(f)
        return groups

    def _guidance(run_id):
        issue_findings = store.all_issue_findings(run_id)
        out = []
        for kind in ("missing_issues", "user_gap"):
            src = (issue_findings if kind == "user_gap"
                   else [f for f in issue_findings if f.get("kind") == "missing_in_tgt"])
            g = guidance_for(kind, src)
            if g:
                out.append({"kind": kind, **g})

        # C3/I4: workflow_wire guidance — statuses created but not yet in any
        # workflow; sourced from config findings (statuses area).
        status_findings = store.query_config(run_id, "statuses")
        g = guidance_for("workflow_wire", status_findings)
        if g:
            out.append({"kind": "workflow_wire", **g})

        # I6: key_collision guidance — issue-level findings
        g = guidance_for("key_collision", issue_findings)
        if g:
            out.append({"kind": "key_collision", **g})

        # I6: workflow_structure_mismatch guidance — config workflow findings
        workflow_findings = store.query_config(run_id, "workflows")
        g = guidance_for("workflow_structure_mismatch", workflow_findings)
        if g:
            out.append({"kind": "workflow_structure_mismatch", **g})

        return out

    @router.get("/runs/{run_id}/fix", response_class=HTMLResponse)
    def fix_screen(request: Request, run_id: int, error: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        mig = store.get_migration(run["migration_id"])
        return _templates.TemplateResponse(request, "fix.html", {
            "run": run, "mig": mig, "product": mig["product"], "error": error,
            "groups": _fixable(run_id, mig["product"]),
            "guidance": _guidance(run_id)})

    @router.post("/runs/{run_id}/fix")
    def start_fix(run_id: int, fix_ids: str = Form(""),
                  confirm_workflow: str = Form("")):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        ids = [i.strip() for i in fix_ids.split(",") if i.strip()]
        if not ids:
            return RedirectResponse(
                f"/runs/{run_id}/fix?error=Select at least one fix",
                status_code=303)
        valid = {f.fix_id for f in FIXES}
        needs_confirm = any(get_fix(i).requires_confirm
                            for i in ids if i in valid)
        if needs_confirm and not confirm_workflow:
            return RedirectResponse(
                f"/runs/{run_id}/fix?error=Workflow wiring needs explicit "
                f"confirmation (confirm box)", status_code=303)
        try:
            rid = engine.start(run["migration_id"],
                               {"fix_ids": ids, "confirm_workflow": bool(confirm_workflow)},
                               kind="fix", source_run_id=run_id)
        except RuntimeError as exc:
            return RedirectResponse(f"/runs/{run_id}/fix?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/fix-runs/{rid}", status_code=303)

    @router.get("/fix-runs/{run_id}", response_class=HTMLResponse)
    def fix_run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        stats = json.loads(run.get("stats_json") or "{}")
        return _templates.TemplateResponse(request, "fix_run.html", {
            "run": run, "actions": store.get_fix_actions(run_id),
            "mig": store.get_migration(run["migration_id"]),
            "stats": stats or None})

    return router
