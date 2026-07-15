"""Tests for environment-audit route endpoints.

Covers:
  POST /migrations          with audit_type=environment
  POST /migrations/{id}/env-runs  (source-only requirement + engine.start delegation)
"""
import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def test_create_environment_audit(tmp_path):
    app = _app(tmp_path); c = TestClient(app)
    r = c.post("/migrations", data={"name": "Acme env", "product": "jira",
               "audit_type": "environment"}, follow_redirects=False)
    assert r.status_code == 303
    mid = int(r.headers["location"].split("/")[-1])
    assert app.state.store.get_migration(mid)["audit_type"] == "environment"


def test_create_confluence_environment_audit_and_run(tmp_path, monkeypatch):
    """A Confluence environment audit is creatable (product=confluence,
    audit_type=environment) and its env-run route starts an env_audit run."""
    app = _app(tmp_path); store = app.state.store
    c = TestClient(app)
    r = c.post("/migrations", data={"name": "Acme Confluence",
               "product": "confluence", "audit_type": "environment"},
               follow_redirects=False)
    assert r.status_code == 303
    mid = int(r.headers["location"].split("/")[-1])
    mig = store.get_migration(mid)
    assert mig["audit_type"] == "environment"
    assert mig["product"] == "confluence"

    # With a source connection, the env-run route starts an env_audit run.
    store.save_connection(mid, "source", "pat", "https://acme.example",
                          {"token": "t", "email": "a@b.c"})
    started = {"kind": None}
    monkeypatch.setattr(
        app.state.engine, "start",
        lambda mid_, params, **kw: (started.__setitem__("kind", kw.get("kind"))
                                    or 9))
    r2 = c.post(f"/migrations/{mid}/env-runs", follow_redirects=False)
    assert r2.status_code == 303 and "/runs/9" in r2.headers["location"]
    assert started["kind"] == "env_audit"


def test_environments_page_offers_confluence(tmp_path):
    """The /environments dashboard now offers a Jira | Confluence product
    selector and mentions Confluence in the empty-state copy."""
    app = _app(tmp_path)
    html = TestClient(app).get("/environments").text
    # The product select (allow_confluence) is rendered.
    assert 'value="confluence"' in html
    # The empty-state copy mentions Confluence (no env audits exist yet).
    assert "Confluence" in html


def test_env_run_requires_source_only(tmp_path, monkeypatch):
    import webapp.main as m
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    # no source connection -> error
    c = TestClient(app)
    r = c.post(f"/migrations/{mid}/env-runs", follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]
    store.save_connection(mid, "source", "pat", "https://acme.example",
                          {"token": "t", "email": "a@b.c"})
    started = {"n": 0}
    monkeypatch.setattr(app.state.engine, "start",
                        lambda mid_, params, **kw: (started.__setitem__("n", 1) or 7))
    r2 = c.post(f"/migrations/{mid}/env-runs", follow_redirects=False)
    assert r2.status_code == 303 and "/runs/7" in r2.headers["location"]
    assert started["n"] == 1


def test_dashboard_offers_both_audit_types(tmp_path):
    app = _app(tmp_path)
    html = TestClient(app).get("/").text
    assert "Migration audit" in html and "Environment audit" in html
    assert "workflow" in html.lower()   # the steps strip is present


def test_env_analysis_renders_health(tmp_path):
    app = _app(tmp_path); store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, status="done", verdict="NEEDS_ATTENTION", stats={
        "health_score": 72, "grade": "B", "findings": 3, "high": 0, "medium": 1,
        "low": 2, "capability_gaps": 0, "by_kind": {"duplicate_field": 1},
        "headlines": ["Field sprawl detected."],
        "ai": {"skipped": False, "themes": [{"title": "Field sprawl",
               "severity": "medium", "recommendation": "merge", "why": "x"}],
               "top_risks": ["r"], "quick_wins": ["w"]}})
    c = TestClient(app)
    html = c.get(f"/runs/{rid}/analysis").text
    # The analysis page is a JS shell — health_score 72 lives in the API summary,
    # not the server-rendered HTML.  Verify both: the page carries the env
    # audit-type marker, and the API endpoint returns the correct health score.
    assert 'data-audit-type="environment"' in html
    api = c.get(f"/api/runs/{rid}/summary").json()
    assert api.get("stats", {}).get("health_score") == 72


def test_env_summary_has_findings_array(tmp_path):
    """GET /api/runs/{rid}/summary for an env_audit run includes a findings array
    with items carrying kind, severity, category, and fix.tier."""
    from webapp.store import Store

    app = _app(tmp_path)
    store = app.state.store

    mid = store.create_migration("env-test", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")

    # Build a finding with detail already containing fix+category+severity (as Phase C would produce)
    findings = [{
        "area": "custom_fields",
        "name": "Old Field",
        "kind": "unused_custom_field",
        "severity": "low",
        "detail": {
            "fix": {
                "tier": "human",
                "tier_label": "Fixable by a human",
                "title": "Remove or add field to a screen: Old Field",
                "detail": "...",
                "api_hint": None,
                "risk": "low",
                "reversible": True,
                "caveat": None,
            },
            "category": "Hygiene",
            # severity is NOT in detail yet — A1 will fold it in at finalize time
        },
    }]

    # Simulate A1: fold severity into detail before insertion
    for f in findings:
        f.setdefault("detail", {})["severity"] = f.get("severity")

    store.insert_findings_config(rid, findings)
    store.update_run(rid, status="done", verdict="HEALTHY_WITH_NOTES", stats={
        "health_score": 88, "grade": "A", "findings": 1, "high": 0, "medium": 0,
        "low": 1, "capability_gaps": 0, "by_kind": {"unused_custom_field": 1},
        "headlines": ["1 low-severity finding."],
        "ai": {"skipped": True},
    })

    c = TestClient(app)
    r = c.get(f"/api/runs/{rid}/summary")
    assert r.status_code == 200
    body = r.json()

    # Must have findings array
    assert "findings" in body, "env summary must include findings array"
    assert len(body["findings"]) == 1

    item = body["findings"][0]
    assert item["kind"] == "unused_custom_field"
    assert item["severity"] == "low"
    assert item["category"] == "Hygiene"
    assert isinstance(item["fix"], dict)
    assert item["fix"]["tier"] == "human"

    # Raw detail blob must NOT be leaked
    assert "detail_json" not in item
    assert "detail" not in item
    assert "fix_payload" not in item


def test_env_summary_surfaces_ai_findings(tmp_path):
    """The whole ai dict (including the new ai_findings list emitted by the AI
    second-auditor) nests in stats.ai and must flow through /summary unchanged —
    no server-side stripping of the new key."""
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env-ai", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    ai_findings = [
        {"title": "Inconsistent key casing", "area": "projects",
         "severity": "low", "observation": "Mixed-case project keys.",
         "recommendation": "Standardise casing."},
        {"title": "Admin to all-logged-in", "area": "permission_scheme_grants",
         "severity": "high", "observation": "ADMINISTER held broadly.",
         "recommendation": "Restrict to a named admin group."},
    ]
    store.update_run(rid, status="done", verdict="HEALTHY_WITH_NOTES", stats={
        "health_score": 81, "grade": "B", "findings": 0, "high": 0, "medium": 0,
        "low": 0, "capability_gaps": 0, "by_kind": {}, "headlines": [],
        "ai": {"skipped": False, "health_score": 81, "grade": "B",
               "summary": "ok", "themes": [], "top_risks": [], "quick_wins": [],
               "model": "m", "ai_findings": ai_findings}})

    c = TestClient(app)
    body = c.get(f"/api/runs/{rid}/summary").json()
    ai = body.get("stats", {}).get("ai") or {}
    assert ai.get("ai_findings") == ai_findings
    assert ai["ai_findings"][1]["severity"] == "high"
    assert ai["ai_findings"][0]["area"] == "projects"


def test_migration_summary_unchanged(tmp_path):
    """GET /api/runs/{rid}/summary for a regular migration run must NOT have a findings array."""
    app = _app(tmp_path)
    store = app.state.store

    mid = store.create_migration("mig-test", audit_type="migration")
    rid = store.create_run(mid, {}, kind="audit")
    store.update_run(rid, status="done", verdict="HEALTHY", stats={
        "issues_src_total": 0,
        "headlines": [],
    })

    c = TestClient(app)
    r = c.get(f"/api/runs/{rid}/summary")
    assert r.status_code == 200
    body = r.json()

    # Migration runs must not have a findings key (or it must be absent)
    # The key point: migration contract is unchanged
    assert "findings" not in body or body["findings"] is None, (
        "migration summary must not include an env findings array"
    )
