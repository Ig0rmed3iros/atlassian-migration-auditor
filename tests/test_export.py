"""CSV / JSON findings export, for both env-audit and migration runs."""
from __future__ import annotations

import copy
import csv
import io

import httpx
from fastapi.testclient import TestClient

from webapp.config import Config
from webapp.main import create_app
from webapp.export import export_findings, rows_to_csv


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8487,
                 public_base_url="http://localhost:8487", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _env_finding(kind, name, area="schemes", severity="low"):
    from auditor.envaudit.fixes import _FIXES, category_for
    fix = copy.copy(_FIXES.get(kind, {"tier": "human", "title": kind}))
    return {"area": area, "name": name, "kind": kind, "severity": severity,
            "detail": {"fix": fix, "category": category_for(kind),
                       "severity": severity}}


def _seed_env(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Env", product="jira", audit_type="environment")
    run = store.create_run(mid, {}, kind="env_audit")
    store.insert_findings_config(run, [
        _env_finding("scheme_unused", "Old, Scheme"),   # comma -> CSV quoting
        _env_finding("orphaned_pages", "DOCS", "spaces", "high")])
    store.update_run(run, status="done", verdict="CRITICAL", stats={})
    return app, store, run


def _seed_migration(tmp_path):
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("Mig", product="jira", audit_type="migration")
    run = store.create_run(mid, {}, kind="audit")
    store.insert_findings_issue(run, [
        {"project": "ACME", "kind": "missing", "src_key": "ACME-1",
         "tgt_key": "", "field": "summary", "summary": "missing on target"}])
    store.update_run(run, status="done", verdict="GAPS_FOUND", stats={})
    return app, store, run


def test_export_env_findings_shape(tmp_path):
    app, store, run = _seed_env(tmp_path)
    fields, rows = export_findings(store, run)
    assert "kind" in fields and "fix_tier" in fields
    kinds = {r["kind"] for r in rows}
    assert {"scheme_unused", "orphaned_pages"} <= kinds
    orphan = next(r for r in rows if r["kind"] == "orphaned_pages")
    assert orphan["severity"] == "high"


def test_export_migration_findings_shape(tmp_path):
    app, store, run = _seed_migration(tmp_path)
    fields, rows = export_findings(store, run)
    assert "project" in fields and "src_key" in fields
    assert rows[0]["project"] == "ACME" and rows[0]["kind"] == "missing"


def test_csv_is_well_formed_and_escapes_commas(tmp_path):
    app, store, run = _seed_env(tmp_path)
    fields, rows = export_findings(store, run)
    parsed = list(csv.DictReader(io.StringIO(rows_to_csv(fields, rows))))
    assert parsed and set(parsed[0]) >= {"kind", "severity"}
    # "Old, Scheme" round-trips intact (proper quoting), not split on the comma
    assert any(r["name"] == "Old, Scheme" for r in parsed)


def test_findings_csv_route_downloads(tmp_path):
    app, store, run = _seed_env(tmp_path)
    r = TestClient(app).get(f"/runs/{run}/findings.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "scheme_unused" in r.text


def test_findings_json_route(tmp_path):
    app, store, run = _seed_env(tmp_path)
    data = TestClient(app).get(f"/runs/{run}/findings.json").json()
    assert data["count"] == 2
    assert any(f["kind"] == "orphaned_pages" for f in data["findings"])


def test_export_missing_run_redirects(tmp_path):
    app = _app(tmp_path)
    r = TestClient(app).get("/runs/999999/findings.csv", follow_redirects=False)
    assert r.status_code in (302, 303)


def test_analysis_page_links_to_exports(tmp_path):
    app, store, run = _seed_env(tmp_path)
    t = TestClient(app).get(f"/runs/{run}/analysis").text
    assert f"/runs/{run}/findings.csv" in t
    assert f"/runs/{run}/findings.json" in t
