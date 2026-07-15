import httpx
from fastapi.testclient import TestClient
from webapp.main import create_app
from webapp.config import Config


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _seed_run(store):
    mid = store.create_migration("m", product="jira")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="GAPS_FOUND")
    # The route reconstructs query metadata from STORED findings (R7), so the
    # finding the tests ask about must actually exist in the run.
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}}])
    return rid


def test_solutions_requires_key(tmp_path):
    app = _app(tmp_path); rid = _seed_run(app.state.store)
    r = TestClient(app).post(f"/runs/{rid}/solutions",
                             data={"kind": "missing_in_tgt", "area": "statuses",
                                   "name": "Triage", "product": "jira"})
    assert r.status_code == 400 and "Settings" in r.json()["error"]


def test_solutions_returns_cached_then_fresh(tmp_path, monkeypatch):
    import webapp.main as m
    app = _app(tmp_path); store = app.state.store
    from webapp.anthropic_key import save_key
    save_key(store, "sk-ant-x")
    rid = _seed_run(store)
    calls = {"n": 0}

    def fake_find(finding, client, **kw):
        calls["n"] += 1
        return {"query": "q", "solutions": [{"title": "Sol"}], "error": None,
                "model": "claude-opus-4-8"}
    monkeypatch.setattr(m, "find_solutions", fake_find)
    monkeypatch.setattr(m, "anthropic_client", lambda store: object())

    data = {"kind": "missing_in_tgt", "area": "statuses", "name": "Triage",
            "product": "jira"}
    c = TestClient(app)
    r1 = c.post(f"/runs/{rid}/solutions", data=data)
    assert r1.status_code == 200 and r1.json()["solutions"][0]["title"] == "Sol"
    assert r1.json()["cached"] is False
    r2 = c.post(f"/runs/{rid}/solutions", data=data)
    assert r2.json()["cached"] is True and calls["n"] == 1  # served from cache
    r3 = c.post(f"/runs/{rid}/solutions", data={**data, "refresh": "1"})
    assert r3.json()["cached"] is False and calls["n"] == 2  # refresh re-queried


def test_solutions_error_result_not_cached(tmp_path, monkeypatch):
    """A transient API failure (RateLimitError/APIConnectionError) yields an
    error-dict from find_solutions. That must NOT be persisted: a later
    request with the same signature and no refresh must re-query (natural
    retry), and once the API recovers the good result is cached normally."""
    import webapp.main as m
    app = _app(tmp_path); store = app.state.store
    from webapp.anthropic_key import save_key
    save_key(store, "sk-ant-x")
    rid = _seed_run(store)
    calls = {"n": 0}

    def fake_find(finding, client, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"query": "q", "solutions": [],
                    "error": "solution search failed: rate limited",
                    "model": "claude-opus-4-8"}
        return {"query": "q", "solutions": [{"title": "Sol"}], "error": None,
                "model": "claude-opus-4-8"}
    monkeypatch.setattr(m, "find_solutions", fake_find)
    monkeypatch.setattr(m, "anthropic_client", lambda store: object())

    data = {"kind": "missing_in_tgt", "area": "statuses", "name": "Triage",
            "product": "jira"}
    c = TestClient(app)
    # First call fails transiently — error returned, but NOT cached.
    r1 = c.post(f"/runs/{rid}/solutions", data=data)
    assert r1.json()["error"] and r1.json()["cached"] is False
    # The transient error must NOT have been persisted to the cache. The route
    # rebuilds the finding from the stored row + migration product (jira) +
    # source-connection deployment (defaults to cloud), so compute the same sig.
    from auditor.solutions import finding_signature
    sig = finding_signature({"kind": "missing_in_tgt", "area": "statuses",
                             "name": "Triage", "product": "jira",
                             "deployment_from": "cloud"})
    assert store.get_solutions(rid, sig) is None
    # Second call (no refresh) must re-query rather than serve the cached error.
    r2 = c.post(f"/runs/{rid}/solutions", data=data)
    assert calls["n"] == 2                       # re-queried, not served from cache
    assert r2.json()["error"] is None
    assert r2.json()["solutions"][0]["title"] == "Sol"
    assert r2.json()["cached"] is False
    # The successful result IS cached: a third call serves it without re-query.
    r3 = c.post(f"/runs/{rid}/solutions", data=data)
    assert calls["n"] == 2 and r3.json()["cached"] is True


def test_route_never_sends_issue_summary(tmp_path, monkeypatch):
    """End-to-end privacy boundary: an issue finding's summary (the customer's
    issue title) must never reach the outbound query, even when the client
    maliciously posts it as `name`. The route rebuilds metadata from the stored
    row by key (R7), so the summary is dropped and only the key survives."""
    import webapp.main as m
    from auditor.solutions import build_query
    app = _app(tmp_path); store = app.state.store
    from webapp.anthropic_key import save_key
    save_key(store, "sk-ant-x")
    mid = store.create_migration("m", product="jira")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    SECRET = "ACME payroll breach Q3 incident"
    store.insert_findings_issue(rid, [
        {"project": "ACME", "kind": "missing_in_src", "src_key": None,
         "tgt_key": "ACME-9", "field": None, "summary": SECRET, "detail": {}}])
    captured = {}

    def fake_find(finding, client, **kw):
        captured["finding"] = finding
        return {"query": "q", "solutions": [], "error": None, "model": "m"}
    monkeypatch.setattr(m, "find_solutions", fake_find)
    monkeypatch.setattr(m, "anthropic_client", lambda store: object())

    c = TestClient(app)
    r = c.post(f"/runs/{rid}/solutions", data={
        "kind": "missing_in_src", "project": "ACME", "tgt_key": "ACME-9",
        "name": SECRET})   # client maliciously sends the issue title as name
    assert r.status_code == 200
    f = captured["finding"]
    assert (f.get("name") or "") == ""          # summary dropped
    assert SECRET not in build_query(f)          # never reaches the query
    assert "ACME-9" in build_query(f)            # the safe key is used instead


def test_analysis_page_loads_solutions_js(tmp_path):
    app = _app(tmp_path); rid = _seed_run(app.state.store)
    from fastapi.testclient import TestClient
    html = TestClient(app).get(f"/runs/{rid}/analysis").text
    assert "/static/solutions.js" in html


def test_analysis_page_find_solutions_buttons(tmp_path):
    """The analysis page must load solutions.js, and app.js must contain
    .find-solutions button templates with the correct kebab-case attributes.

    The analysis view is client-rendered: the server returns an HTML shell that
    loads app.js, which builds the DOM in the browser.  Regression coverage
    therefore spans two responses: the page (script tags) and the static bundle
    (button template strings).

    Spec prescribes data-deployment-from (kebab-case, HTML standard for
    multi-word data attributes) on the Find Solutions button.  solutions.js
    reads it via getAttribute so the attribute name must match exactly."""
    app = _app(tmp_path); rid = _seed_run(app.state.store)
    from fastapi.testclient import TestClient
    c = TestClient(app)

    # 1. The analysis page loads both JS files.
    html = c.get(f"/runs/{rid}/analysis").text
    assert "/static/solutions.js" in html
    assert "/static/app.js" in html

    # 2. app.js contains the .find-solutions button class and the correct
    #    spec-prescribed kebab-case data-deployment-from attribute (not the
    #    all-lowercase data-deploymentfrom that was previously emitted).
    appjs = c.get("/static/app.js").text
    assert "find-solutions" in appjs
    assert "data-deployment-from=" in appjs
    assert "data-deploymentfrom=" not in appjs

    # 3. Every button template carries data-kind and data-run so that
    #    solutions.js can POST a complete finding to /runs/{id}/solutions.
    assert "data-kind=" in appjs
    assert "data-run=" in appjs
