import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webapp.analysis import make_router
from webapp.store import Store


@pytest.fixture()
def client(tmp_path):
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND", stats={
        "projects": 1, "issues_src_total": 10, "issues_tgt_total": 9,
        "holes": 1, "tails": 0, "collisions": 0, "issues_with_mismatches": 2,
        "config_missing": 1, "config_other": 0, "blind_spots": 0,
        "headlines": ["AC has 1 issue missing"],
        "project_stats": {"AC": {"src": 10, "tgt": 9, "fidelity_pct": 80.0}},
        "areas": {"statuses": {"src": 2, "tgt": 1}}})
    store.set_run_projects(rid, [{"key": "AC", "name": "AC Support",
                                  "src_count": 10, "tgt_count": 9, "missing": 1,
                                  "tail_count": 0, "fidelity_pct": 80.0,
                                  "blind_spot": 0, "status": "compared"}])
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-2",
         "tgt_key": None, "field": None, "summary": "lost issue", "detail": {}},
        {"project": "AC", "kind": "field_mismatch", "src_key": "AC-3",
         "tgt_key": "AC-3", "field": "status",
         "summary": "status differs", "detail": {"src": "Open", "tgt": "Done"}}])
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "On Hold", "kind": "missing_in_tgt",
         "detail": {}}])
    store.add_event(rid, "compare", "info", "AC compared")
    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    c = TestClient(app)
    c.rid = rid
    return c


def test_summary(client):
    d = client.get(f"/api/runs/{client.rid}/summary").json()
    assert d["verdict"] == "GAPS_FOUND"
    assert d["stats"]["holes"] == 1
    assert d["headlines"] == ["AC has 1 issue missing"]
    # product vocabulary passthrough: a jira migration reports jira labels
    assert d["product"] == "jira"
    assert d["container_label"] == "project" and d["item_label"] == "issue"


def test_summary_includes_product_and_labels(tmp_path):
    """A confluence run's summary carries product + connector labels so the
    UI can relabel without a second round-trip (stats keys stay issue-named)."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m", product="confluence")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "headlines": [], "project_stats": {}, "areas": {}})
    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    d = TestClient(app).get(f"/api/runs/{rid}/summary").json()
    assert d["product"] == "confluence"
    assert d["container_label"] == "space" and d["item_label"] == "page"


def test_projects(client):
    d = client.get(f"/api/runs/{client.rid}/projects").json()
    assert d[0]["key"] == "AC" and d[0]["fidelity_pct"] == 80.0


def test_issues_filters_and_pagination(client):
    d = client.get(f"/api/runs/{client.rid}/issues",
                   params={"kind": "field_mismatch"}).json()
    assert d["total"] == 1 and d["rows"][0]["field"] == "status"
    assert isinstance(d["rows"][0]["detail"], dict)
    d2 = client.get(f"/api/runs/{client.rid}/issues",
                    params={"q": "lost"}).json()
    assert d2["total"] == 1 and d2["rows"][0]["src_key"] == "AC-2"
    d3 = client.get(f"/api/runs/{client.rid}/issues",
                    params={"page": 2, "size": 1}).json()
    assert d3["total"] == 2 and len(d3["rows"]) == 1


def test_kind_counts(client):
    d = client.get(f"/api/runs/{client.rid}/issues/kinds").json()
    assert d == {"missing_in_tgt": 1, "field_mismatch": 1}


def test_config_areas_and_rows(client):
    areas = client.get(f"/api/runs/{client.rid}/config").json()
    assert areas["areas"] == ["statuses"]
    assert areas["skipped"] == {}
    rows = client.get(f"/api/runs/{client.rid}/config",
                      params={"area": "statuses"}).json()
    assert rows["rows"][0]["name"] == "On Hold"


def test_config_lists_dc_skipped_areas(tmp_path):
    """R4 surfaced: a DC-skipped area emits zero findings_config rows BY
    DESIGN, so the Config tab's area list must carry it explicitly — an
    operator must never see a skipped (unaudited) area rendered as clean."""
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN", stats={
        "headlines": [], "project_stats": {},
        "areas": {
            "statuses": {"label": "statuses", "src": 3, "tgt": 3},
            "workflow_schemes": {
                "label": "workflow_schemes", "skipped": True,
                "reason": "no Data Center API — verify manually"}}})
    app = FastAPI()
    app.state.store = store
    app.include_router(make_router())
    c = TestClient(app)
    d = c.get(f"/api/runs/{rid}/config").json()
    assert d["areas"] == []          # no findings rows at all
    assert d["skipped"] == {
        "workflow_schemes": "no Data Center API — verify manually"}


def test_events_incremental(client):
    evs = client.get(f"/api/runs/{client.rid}/events").json()
    assert evs[-1]["message"] == "AC compared"
    none = client.get(f"/api/runs/{client.rid}/events",
                      params={"after": evs[-1]["id"]}).json()
    assert none == []


def test_unknown_run_404(client):
    assert client.get("/api/runs/999/summary").status_code == 404


def test_issues_size_capped_at_200(client):
    d = client.get(f"/api/runs/{client.rid}/issues", params={"size": 500}).json()
    assert d["size"] == 200


def test_issues_page_zero_echoes_one(client):
    d = client.get(f"/api/runs/{client.rid}/issues", params={"page": 0}).json()
    assert d["page"] == 1


def test_issues_page_beyond_end_is_empty_not_error(client):
    d = client.get(f"/api/runs/{client.rid}/issues", params={"page": 999}).json()
    assert d["rows"] == [] and d["total"] >= 0


def test_all_endpoints_404_on_unknown_run(client):
    for path in ("summary", "projects", "issues", "issues/kinds", "config", "events"):
        assert client.get(f"/api/runs/424242/{path}").status_code == 404


def test_summary_empty_stats_json_graceful(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webapp.analysis import make_router
    from webapp.store import Store
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN")   # stats_json stays '{}'
    app = FastAPI(); app.state.store = store
    app.include_router(make_router())
    d = TestClient(app).get(f"/api/runs/{rid}/summary").json()
    assert d["verdict"] == "CLEAN"
    assert d["headlines"] == [] and d["project_stats"] == {} and d["areas"] == {}


def test_summary_keeps_existing_fields_back_compat(client):
    """The summary still exposes every legacy field even with the new block."""
    d = client.get(f"/api/runs/{client.rid}/summary").json()
    assert d["verdict"] == "GAPS_FOUND"
    assert d["stats"]["holes"] == 1
    assert d["headlines"] == ["AC has 1 issue missing"]
    assert "AC" in d["project_stats"]
    assert d["areas"] == {"statuses": {"src": 2, "tgt": 1}}
    # new block present and shaped, never crashes on a thin fixture
    assert "systematic_gaps" in d
    assert "fidelity" in d and "core" in d["fidelity"]


def test_summary_derives_systematic_gap_and_core_fidelity(tmp_path):
    """End-to-end: a field empty-on-target across nearly all of a project's
    issues is reported under systematic_gaps and lifts fidelity.core above
    fidelity.raw. Synthetic data only (Acme)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webapp.analysis import make_router
    from webapp.store import Store

    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    common = 800
    n_gap = 300           # >= 200 floor AND >= 0.30*800 (=240)
    findings = []
    for i in range(n_gap):
        findings.append({
            "project": "ACME", "kind": "field_mismatch",
            "src_key": f"ACME-{i}", "tgt_key": f"ACME-{i}",
            "field": "environment",
            "summary": "environment differs",
            "detail": {"src": "linux/prod", "tgt": None, "sev": "med"}})
    # one genuine, non-systematic mismatch
    findings.append({
        "project": "ACME", "kind": "field_mismatch",
        "src_key": "ACME-999", "tgt_key": "ACME-999", "field": "status",
        "summary": "status differs",
        "detail": {"src": "Open", "tgt": "Done", "sev": "high"}})
    store.insert_findings_issue(rid, findings)

    mismatched = n_gap + 1
    store.update_run(rid, status="done", verdict="GAPS_FOUND", stats={
        "projects": 1, "issues_src_total": common, "issues_tgt_total": common,
        "holes": 0, "tails": 0, "collisions": 0,
        "issues_with_mismatches": mismatched,
        "headlines": [],
        "project_stats": {"ACME": {
            "project": "ACME", "src": common, "tgt": common, "common": common,
            "missing_in_tgt": 0, "tails": 0, "collisions": 0,
            "issues_with_mismatches": mismatched,
            "fidelity_pct": round(100.0 * (common - mismatched) / common, 2)}},
        "areas": {}})

    app = FastAPI(); app.state.store = store
    app.include_router(make_router())
    d = TestClient(app).get(f"/api/runs/{rid}/summary").json()

    gaps = d["systematic_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["project"] == "ACME" and gaps[0]["field"] == "environment"
    assert gaps[0]["affected_issues"] == n_gap
    assert gaps[0]["top_pattern"] == f"linux/prod -> (empty) x{n_gap}"

    # core counts only ACME-999; raw counts all 301.
    assert d["fidelity"]["core_mismatched_total"] == 1
    assert d["fidelity"]["core"] > d["fidelity"]["raw"]
    # per-project core fidelity is attached onto project_stats (back-compat add).
    assert d["project_stats"]["ACME"]["core_mismatched"] == 1
    assert d["project_stats"]["ACME"]["fidelity_core"] > \
        d["project_stats"]["ACME"]["fidelity_raw"]


def test_malformed_detail_json_does_not_500(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webapp.analysis import make_router
    from webapp.store import Store
    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "field_mismatch", "src_key": "AC-1",
         "tgt_key": "AC-1", "field": "status", "summary": "x", "detail": {}}])
    # corrupt the detail_json directly in the DB
    store._exec("UPDATE findings_issue SET detail_json='{bad json' WHERE run_id=?",
                (rid,))
    app = FastAPI(); app.state.store = store
    app.include_router(make_router())
    d = TestClient(app).get(f"/api/runs/{rid}/issues").json()
    assert d["total"] == 1
    assert d["rows"][0]["detail"]["_unparseable"].startswith("{bad")


def test_issues_project_filter(client):
    """The paginated issues endpoint accepts an OPTIONAL `project` param that
    scopes findings to a single project; absent, behaviour is unchanged."""
    # Two more projects' findings to prove the filter actually narrows.
    store = client.app.state.store
    store.insert_findings_issue(client.rid, [
        {"project": "GLOBEX", "kind": "field_mismatch", "src_key": "GLOBEX-1",
         "tgt_key": "GLOBEX-1", "field": "priority", "summary": "priority differs",
         "detail": {"src": "High", "tgt": "Low"}}])
    # filter to GLOBEX -> only its one finding
    d = client.get(f"/api/runs/{client.rid}/issues",
                   params={"project": "GLOBEX"}).json()
    assert d["total"] == 1 and d["rows"][0]["src_key"] == "GLOBEX-1"
    # filter to AC -> only the two AC findings, not GLOBEX
    d2 = client.get(f"/api/runs/{client.rid}/issues",
                    params={"project": "AC"}).json()
    assert d2["total"] == 2
    assert all(r["project"] == "AC" for r in d2["rows"])
    # no project param -> all three findings (unchanged behaviour)
    d3 = client.get(f"/api/runs/{client.rid}/issues").json()
    assert d3["total"] == 3


# ----------------------------------------------------------------------------
# Per-project endpoint: GET /api/runs/{id}/projects/{key}
# ----------------------------------------------------------------------------
def _gap_project_app(tmp_path):
    """Build an app whose single project ACME carries one systematic gap
    (environment empty on target across most issues) plus one genuine mismatch
    and a couple of other-kind findings. Synthetic data only."""
    from fastapi import FastAPI
    from webapp.analysis import make_router
    from webapp.store import Store

    store = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    common = 800
    n_gap = 300                       # >= 200 floor AND >= 0.30*800 (=240)
    findings = []
    for i in range(n_gap):
        findings.append({
            "project": "ACME", "kind": "field_mismatch",
            "src_key": f"ACME-{i}", "tgt_key": f"ACME-{i}",
            "field": "environment", "summary": "environment differs",
            "detail": {"src": "linux/prod", "tgt": None, "sev": "med"}})
    # one genuine, non-systematic field mismatch
    findings.append({
        "project": "ACME", "kind": "field_mismatch",
        "src_key": "ACME-900", "tgt_key": "ACME-900", "field": "status",
        "summary": "status differs",
        "detail": {"src": "Open", "tgt": "Done", "sev": "high"}})
    # one comment mismatch and one attachment mismatch (always core kinds)
    findings.append({
        "project": "ACME", "kind": "comment_mismatch",
        "src_key": "ACME-901", "tgt_key": "ACME-901", "field": None,
        "summary": "comments differ", "detail": {"src_total": 3, "tgt_total": 2}})
    findings.append({
        "project": "ACME", "kind": "attachment_mismatch",
        "src_key": "ACME-902", "tgt_key": "ACME-902", "field": None,
        "summary": "attachments differ", "detail": {"missing_in_tgt": ["a.png"]}})
    store.insert_findings_issue(rid, findings)

    mismatched = n_gap + 3
    store.update_run(rid, status="done", verdict="GAPS_FOUND", stats={
        "projects": 1, "issues_src_total": common, "issues_tgt_total": common,
        "holes": 0, "tails": 0, "collisions": 0,
        "issues_with_mismatches": mismatched, "headlines": [],
        "project_stats": {"ACME": {
            "project": "ACME", "src": common, "tgt": common, "common": common,
            "missing_in_tgt": 0, "tails": 0, "collisions": 0,
            "issues_with_mismatches": mismatched,
            "fidelity_pct": round(100.0 * (common - mismatched) / common, 2)}},
        "areas": {}})
    store.set_run_projects(rid, [{
        "key": "ACME", "name": "Acme Support", "src_count": common,
        "tgt_count": common, "missing": 0, "tail_count": 0,
        "fidelity_pct": round(100.0 * (common - mismatched) / common, 2),
        "blind_spot": 0, "status": "compared"}])

    app = FastAPI(); app.state.store = store
    app.include_router(make_router())
    return app, rid, common, n_gap


def test_project_endpoint_systematic_gap_and_core_fidelity(tmp_path):
    from fastapi.testclient import TestClient
    app, rid, common, n_gap = _gap_project_app(tmp_path)
    d = TestClient(app).get(f"/api/runs/{rid}/projects/ACME").json()

    proj = d["project"]
    assert proj["key"] == "ACME" and proj["name"] == "Acme Support"
    assert proj["common"] == common
    assert proj["src"] == common and proj["tgt"] == common
    assert proj["status"] == "compared" and proj["blind_spot"] is False
    # the systematic gap lifts core above raw for this project
    assert proj["fidelity_core"] > proj["fidelity_raw"]

    # field_breakdown sorted desc by affected issues; environment marked systematic
    fb = d["field_breakdown"]
    assert fb == sorted(fb, key=lambda x: x["issues"], reverse=True)
    env = next(f for f in fb if f["field"] == "environment")
    assert env["issues"] == n_gap and env["systematic"] is True
    status = next(f for f in fb if f["field"] == "status")
    assert status["systematic"] is False

    # kind_breakdown carries every fidelity kind with a numeric count
    kb = d["kind_breakdown"]
    for k in ("field_mismatch", "comment_mismatch", "link_mismatch",
              "content_mismatch", "attachment_mismatch"):
        assert k in kb
    assert kb["field_mismatch"] == n_gap + 1
    assert kb["comment_mismatch"] == 1 and kb["attachment_mismatch"] == 1

    # worst-field value pairs surfaced
    assert d["top_value_pairs"]
    top = d["top_value_pairs"][0]
    assert top["field"] == "environment" and top["count"] == n_gap

    # this project's systematic gaps echoed
    assert len(d["systematic_gaps"]) == 1
    assert d["systematic_gaps"][0]["field"] == "environment"


def test_project_endpoint_unknown_project_404(tmp_path):
    from fastapi.testclient import TestClient
    app, rid, _, _ = _gap_project_app(tmp_path)
    assert TestClient(app).get(
        f"/api/runs/{rid}/projects/NOPE").status_code == 404


def test_project_endpoint_unknown_run_404(tmp_path):
    from fastapi.testclient import TestClient
    app, _, _, _ = _gap_project_app(tmp_path)
    assert TestClient(app).get(
        "/api/runs/424242/projects/ACME").status_code == 404


def test_env_audit_summary_surfaces_admin_link(tmp_path):
    """The env_audit summary payload carries each finding's admin_link through
    to the client (where app.js renders the deep-link)."""
    import httpx
    from fastapi.testclient import TestClient
    from webapp.config import Config
    from webapp.main import create_app

    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8487,
                 public_base_url="http://localhost:8487", secret_key=None)
    app = create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    store = app.state.store
    mid = store.create_migration("env", product="jira", audit_type="environment")
    audit = store.create_run(mid, {}, kind="env_audit")
    store.insert_findings_config(audit, [{
        "area": "plugins", "name": "", "kind": "apps_to_assess_for_cloud",
        "severity": "medium",
        "detail": {"severity": "medium", "category": "Structure",
                   "fix": {"tier": "human", "title": "x", "detail": "y"},
                   "admin_link": {"url": "https://acme.atlassian.net/plugins/servlet/upm",
                                  "label": "Manage apps (UPM)"}}}])
    store.update_run(audit, status="done", verdict="NEEDS_ATTENTION",
                     stats={"health_score": 70, "grade": "C"})

    d = TestClient(app).get(f"/api/runs/{audit}/summary").json()
    fa = [f for f in d["findings"] if f["kind"] == "apps_to_assess_for_cloud"]
    assert fa and fa[0]["admin_link"]["url"].endswith("/plugins/servlet/upm")
    assert fa[0]["admin_link"]["label"] == "Manage apps (UPM)"
