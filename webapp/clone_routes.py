"""Clone-access web routes: page, read-only preview, background apply, status."""
from __future__ import annotations

import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from webapp.clone_runner import run_preview


def _parse_form_pairs(main, clone, upload_bytes) -> list:
    """Pairs from a single main/clone OR an uploaded main,clone CSV."""
    if upload_bytes:
        text = upload_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        if "main" not in cols or "clone" not in cols:
            raise ValueError("CSV must have 'main' and 'clone' columns")
        out = []
        for row in reader:
            m = (row.get(cols["main"]) or "").strip()
            c = (row.get(cols["clone"]) or "").strip()
            if m or c:
                out.append((m, c))
        return out
    if main and clone:
        return [(main.strip(), clone.strip())]
    raise ValueError("provide a main and clone, or upload a CSV")


def make_clone_router(store, runner, http_getter, templates) -> APIRouter:
    router = APIRouter()

    def _render(request, **ctx):
        base = {"connections": store.list_saved_connections("jira"),
                "runs": store.list_clone_runs(20), "active_nav": "clone"}
        base.update(ctx)
        return templates.TemplateResponse(request, "clone.html", base)

    @router.get("/clone", response_class=HTMLResponse)
    def clone_page(request: Request):
        return _render(request)

    @router.post("/clone/preview", response_class=HTMLResponse)
    async def clone_preview(request: Request, conn_id: int = Form(...),
                            main: str = Form(""), clone: str = Form(""),
                            csv_file: UploadFile | None = File(None)):
        data = await csv_file.read() if csv_file is not None else b""
        try:
            pairs = _parse_form_pairs(main, clone, data)
            report = run_preview(store, conn_id, pairs, http_getter())
        except ValueError as e:
            return _render(request, error=str(e), sel_conn=conn_id)
        return _render(request, report=report, sel_conn=conn_id, is_preview=True)

    @router.post("/clone/apply")
    async def clone_apply(request: Request, conn_id: int = Form(...),
                          main: str = Form(""), clone: str = Form(""),
                          dry_run: Optional[str] = Form(None),
                          csv_file: UploadFile | None = File(None)):
        data = await csv_file.read() if csv_file is not None else b""
        try:
            pairs = _parse_form_pairs(main, clone, data)
        except ValueError as e:
            return _render(request, error=str(e), sel_conn=conn_id)
        is_dry = dry_run is not None
        run_id = runner.start(conn_id, pairs, dry_run=is_dry, scan_roles=True)
        return RedirectResponse(f"/clone/runs/{run_id}", status_code=303)

    @router.get("/clone/runs/{run_id}", response_class=HTMLResponse)
    def clone_run_page(request: Request, run_id: int):
        row = store.get_clone_run(run_id)
        if row is None:
            return RedirectResponse("/clone", status_code=303)
        report = json.loads(row["report_json"]) if row["report_json"] else None
        log = json.loads(row["log_json"]) if row["log_json"] else []
        return templates.TemplateResponse(request, "clone_run.html",
            {"run": row, "report": report, "log": log,
             "active_nav": "clone"})

    @router.get("/clone/runs/{run_id}/status")
    def clone_run_status(run_id: int):
        row = store.get_clone_run(run_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "status": row["status"], "phase": row["phase"],
            "log": json.loads(row["log_json"]) if row["log_json"] else [],
            "report": json.loads(row["report_json"]) if row["report_json"] else None})

    return router
