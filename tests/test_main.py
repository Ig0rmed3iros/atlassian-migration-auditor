import os
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient
from webapp.config import Config
from webapp.main import create_app


def mk_app(tmp_path, handler=None):
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    http = httpx.Client(transport=httpx.MockTransport(handler)) if handler else None
    app = create_app(cfg, http=http)
    return app, TestClient(app, follow_redirects=False)


def ok_jira(req):
    p = str(req.url.path)
    if p.endswith("/myself"):
        return httpx.Response(200, json={"displayName": "Igor",
                                         "emailAddress": "i@x.y",
                                         "accountId": "acc-1"})
    if p.endswith("project/search"):
        return httpx.Response(200, json={"isLast": True, "values": [
            {"key": "AC", "name": "AC Support", "id": "1",
             "insight": {"totalIssueCount": 5}}]})
    if p.endswith("approximate-count"):
        return httpx.Response(200, json={"count": 5})
    if "/role" in p and req.method == "POST":
        return httpx.Response(200, json={})
    if "/role" in p and req.method == "DELETE":
        return httpx.Response(204)
    if p.endswith("/rest/api/3/role"):
        return httpx.Response(200, json=[{"name": "Administrators", "id": 9}])
    return httpx.Response(200, json={"values": [], "isLast": True})


def test_dashboard_renders(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "Atlassian Audit Platform" in r.text


def test_create_migration_and_page(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "acme->globex"})
    assert r.status_code == 303
    loc = r.headers["location"]
    page = c.get(loc)
    assert page.status_code == 200
    assert ("acme-&gt;globex" in page.text) or ("acme->globex" in page.text)


def test_pat_connection_save_and_verify(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "tok"})
    assert r.status_code == 303
    page = c.get("/migrations/1")
    assert "verified" in page.text
    row = app.state.store.get_connection(1, "source")
    assert row["status"] == "verified"
    assert b"tok" not in row["secret_enc"]


def test_pat_connection_bad_auth_shows_error(tmp_path):
    def deny(req):
        return httpx.Response(401, text="no")
    app, c = mk_app(tmp_path, handler=deny)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "bad"},
               follow_redirects=True)
    assert "could not authenticate" in r.text.lower()
    assert app.state.store.get_connection(1, "source") is None


def test_settings_roundtrip_encrypts_secret(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/settings", data={"oauth_client_id": "cid",
                                  "oauth_client_secret": "sek"})
    assert r.status_code == 303
    store = app.state.store
    assert store.settings_get("oauth_client_id") == "cid"
    enc = store.settings_get("oauth_client_secret_enc")
    assert "sek" not in enc
    assert store.decrypt(enc.encode())["secret"] == "sek"


def test_settings_page_renders_provider_panel(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    assert "Environment-audit AI analysis" in r.text
    assert "OpenAI-compatible" in r.text
    # The active provider is shown (default anthropic, not configured).
    assert "not configured" in r.text


def test_settings_save_openai_provider_encrypts_key(tmp_path):
    # Placeholder values only — NO real base_url / api_key (synthetic repo).
    app, c = mk_app(tmp_path)
    r = c.post("/settings", data={"ai_provider_choice": "openai",
                                  "openai_base_url": "https://example.test/v1",
                                  "openai_model": "albert-heavy",
                                  "openai_api_key": "sk-test-xxx"})
    assert r.status_code == 303
    store = app.state.store
    from webapp.ai_provider import get_provider_choice, load_openai_config
    assert get_provider_choice(store) == "openai"
    cfg = load_openai_config(store)
    assert cfg["base_url"] == "https://example.test/v1"
    assert cfg["model"] == "albert-heavy"
    # The api key is encrypted at rest — never plaintext in settings.
    raw = store.settings_get("openai_api_key_enc")
    assert raw and "sk-test-xxx" not in raw
    # The settings page now reports the active provider as configured.
    page = c.get("/settings")
    assert "openai" in page.text and "configured" in page.text


def test_settings_save_openai_key_not_rendered_back(tmp_path):
    app, c = mk_app(tmp_path)
    c.post("/settings", data={"ai_provider_choice": "openai",
                              "openai_base_url": "https://example.test/v1",
                              "openai_model": "albert-heavy",
                              "openai_api_key": "sk-test-xxx"})
    page = c.get("/settings")
    # The write-only key must never be rendered back into the page.
    assert "sk-test-xxx" not in page.text


def test_settings_page_renders_claude_cli_option(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    # The provider selector offers the local Claude CLI as a third option, with
    # a note that it uses the local CLI and needs no API key.
    assert 'value="claude_cli"' in r.text
    assert "Claude CLI" in r.text
    assert "no API key" in r.text.lower() or "no api key" in r.text.lower()


def test_settings_save_claude_cli_provider(tmp_path):
    app, c = mk_app(tmp_path)
    # No API key supplied at all — the CLI provider uses local auth.
    r = c.post("/settings", data={"ai_provider_choice": "claude_cli",
                                  "claude_cli_model": "claude-opus-4-8",
                                  "claude_cli_binary": "claude"})
    assert r.status_code == 303
    store = app.state.store
    from webapp.ai_provider import get_provider_choice, ai_provider, \
        ClaudeCLIProvider
    assert get_provider_choice(store) == "claude_cli"
    assert store.settings_get("claude_cli_model") == "claude-opus-4-8"
    assert store.settings_get("claude_cli_binary") == "claude"
    # The factory builds the CLI provider with no API key configured.
    p = ai_provider(store)
    assert isinstance(p, ClaudeCLIProvider)
    assert p.model == "claude-opus-4-8"
    # The settings page reports the active CLI provider as configured.
    page = c.get("/settings")
    assert "claude_cli" in page.text and "configured" in page.text


def test_settings_save_claude_cli_without_model(tmp_path):
    # Model field blank = the CLI's default model; the provider is still active
    # and configured (no API key needed).
    app, c = mk_app(tmp_path)
    r = c.post("/settings", data={"ai_provider_choice": "claude_cli"})
    assert r.status_code == 303
    store = app.state.store
    from webapp.ai_provider import get_provider_choice, ai_provider, \
        ClaudeCLIProvider
    assert get_provider_choice(store) == "claude_cli"
    p = ai_provider(store)
    assert isinstance(p, ClaudeCLIProvider)
    assert p.model is None
    assert p.binary == "claude"


def test_run_start_requires_both_connections(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/runs", data={}, follow_redirects=True)
    assert "configure both connections" in r.text.lower()


def test_env_run_route_requires_source_connection(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    r = c.post(f"/migrations/{mid}/env-runs", data={}, follow_redirects=True)
    assert "configure" in r.text.lower() and "connection" in r.text.lower()
    assert store.active_run(mid) is None


def test_env_run_route_rejects_migration_audit_type(tmp_path):
    # The env-run route is only valid for an environment-audit migration; a
    # two-sided migration row must be redirected with an error, not run.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("mig")          # audit_type defaults to migration
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    r = c.post(f"/migrations/{mid}/env-runs", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/migrations/{mid}?error=")
    assert store.active_run(mid) is None


def test_env_run_route_starts_env_audit(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    # Stub the engine's env stages so the background thread completes without
    # any network — the route under test is the trigger, not the pipeline.
    app.state.engine.env_stages = {
        "verify": lambda ctx: None, "scope": lambda ctx: None,
        "gather": lambda ctx: ctx.update(snapshot={"areas": {}}),
        "checks": lambda ctx: ctx.update(env_findings=[]),
        "analysis": lambda ctx: ctx.update(ai={"skipped": True}),
    }
    mid = store.create_migration("env", audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    r = c.post(f"/migrations/{mid}/env-runs", data={}, follow_redirects=False)
    assert r.status_code == 303
    rid = int(r.headers["location"].rsplit("/", 1)[-1])
    run = store.get_run(rid)
    assert run["kind"] == "env_audit" and run["migration_id"] == mid


def test_sse_stream_for_finished_run(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.add_event(rid, "verify", "info", "hello-evt")
    store.update_run(rid, status="done")
    r = c.get(f"/runs/{rid}/stream")
    assert r.status_code == 200
    assert "hello-evt" in r.text and "event: done" in r.text


def test_sse_running_then_done_terminates(tmp_path):
    import threading, time
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})          # status='running'
    store.add_event(rid, "verify", "info", "started-evt")
    def finish():
        time.sleep(0.3)
        store.add_event(rid, "finalize", "info", "finishing-evt")
        store.update_run(rid, status="done")
    threading.Thread(target=finish, daemon=True).start()
    r = c.get(f"/runs/{rid}/stream")          # must return, not hang
    assert "started-evt" in r.text and "finishing-evt" in r.text
    assert "event: done" in r.text


def test_run_and_analysis_pages_render(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"headlines": [], "project_stats": {}, "areas": {}})
    assert c.get(f"/runs/{rid}").status_code == 200
    for view in ("", "/projects", "/config", "/issues", "/log"):
        resp = c.get(f"/runs/{rid}/analysis{view}")
        assert resp.status_code == 200, view
    assert c.get(f"/runs/{rid}/analysis/projects/AC").status_code == 200


def test_scope_preview_returns_matched_with_counts(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    for role in ("source", "target"):
        c.post("/migrations/1/connections",
               data={"role": role,
                     "site_url": f"https://{role}.atlassian.net",
                     "email": "i@x.y", "api_token": "t"})
    r = c.get("/migrations/1/scope.json")
    assert r.status_code == 200
    d = r.json()
    assert any(p["key"] == "AC" for p in d["matched"])
    ac = next(p for p in d["matched"] if p["key"] == "AC")
    assert ac["src_count"] == 5 and ac["tgt_count"] == 5
    assert "source_only" in d and "target_only" in d


def test_scope_preview_requires_both_connections(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    c.post("/migrations/1/connections",
           data={"role": "source", "site_url": "https://s.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    r = c.get("/migrations/1/scope.json")
    assert r.status_code == 400 and "both connections" in r.json()["error"]


def test_scope_preview_404_unknown_migration(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    assert c.get("/migrations/999/scope.json").status_code == 404


def test_create_migration_rejects_unregistered_product(tmp_path):
    # A product with no registered connector must be rejected at CREATION —
    # accepting the row and 500ing on the very next step (connections,
    # scope, run) is the broken half-state. (confluence is registered as of
    # Task 13, so it no longer belongs in this rejection list.)
    app, c = mk_app(tmp_path)
    for product in ("bamboo", "bitbucket"):
        r = c.post("/migrations", data={"name": "nope", "product": product})
        assert r.status_code == 303
        assert r.headers["location"].startswith("/?error=")
    assert app.state.store.list_migrations() == []


def test_connection_post_unknown_migration_redirects_not_500(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    r = c.post("/migrations/999/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "t"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_legacy_unregistered_product_row_degrades_gracefully(tmp_path):
    # A migration row may pre-date the registry gate (a product written by an
    # older build that this build no longer serves). Every endpoint that
    # resolves its connector must answer with a redirect/4xx error, never a
    # 500. (confluence joined the registry in Task 13, so the stand-in for a
    # genuinely unregistered product is now "bamboo".)
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    mid = store.create_migration("legacy")
    store._exec("UPDATE migrations SET product='bamboo' WHERE id=?", (mid,))
    r = c.post(f"/migrations/{mid}/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "t"})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    for role in ("source", "target"):
        store.save_connection(mid, role, "pat",
                              f"https://{role}.atlassian.net",
                              secret={"email": "i@x.y", "token": "t"})
    r2 = c.get(f"/migrations/{mid}/scope.json")
    assert r2.status_code == 400
    assert "unknown product" in r2.json()["error"]


def test_dc_connection_no_email_required(tmp_path):
    def bearer_dc(req):
        # DC PAT-as-Bearer: /rest/api/2 prefix, no Basic email:token pair.
        if (str(req.url.path) == "/rest/api/2/myself"
                and req.headers.get("authorization") == "Bearer tok"):
            return httpx.Response(200, json={"name": "igor",
                                             "displayName": "Igor Medeiros"})
        return httpx.Response(404, text="nope")
    app, c = mk_app(tmp_path, handler=bearer_dc)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://jira.acme.example",
                     "email": "", "api_token": "tok", "deployment": "dc"})
    assert r.status_code == 303
    assert r.headers["location"] == "/migrations/1"
    row = app.state.store.get_connection(1, "source")
    assert row["deployment"] == "dc" and row["status"] == "verified"
    secret = app.state.store.connection_secret(row)
    assert "email" not in secret and secret["token"] == "tok"


def test_cloud_connection_still_requires_email(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://acme.atlassian.net",
                     "email": "", "api_token": "tok", "deployment": "cloud"},
               follow_redirects=True)
    assert "email is required" in r.text.lower()
    assert app.state.store.get_connection(1, "source") is None


def test_elevate_blocked_for_dc(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "pat", "https://jira.acme.example",
                          secret={"token": "t"}, deployment="dc")
    store.save_connection(mid, "target", "pat", "https://globex.atlassian.net",
                          secret={"email": "igor@globex.example", "token": "t"},
                          deployment="cloud")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    r = c.get(f"/runs/{rid}/elevate")
    assert r.status_code == 303
    assert r.headers["location"] == f"/runs/{rid}"
    r2 = c.post(f"/runs/{rid}/elevate", data={"side": "source"})
    assert r2.status_code == 303
    assert r2.headers["location"] == f"/runs/{rid}"
    evts = store.get_events(rid)
    assert any("only supported for jira cloud" in e["message"].lower()
               for e in evts)


def test_scope_preview_includes_product_labels(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    c.post("/migrations", data={"name": "m"})
    for role in ("source", "target"):
        c.post("/migrations/1/connections",
               data={"role": role,
                     "site_url": f"https://{role}.atlassian.net",
                     "email": "igor@acme.example", "api_token": "t"})
    d = c.get("/migrations/1/scope.json").json()
    assert d["product"] == "jira"
    assert d["container_label"] == "project"
    assert d["item_label"] == "issue"


def test_elevation_apply_and_undo(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    c.post("/migrations", data={"name": "m"})
    c.post("/migrations/1/connections",
           data={"role": "source", "site_url": "https://s.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    c.post("/migrations/1/connections",
           data={"role": "target", "site_url": "https://t.atlassian.net",
                 "email": "i@x.y", "api_token": "t"})
    rid = store.create_run(1, {})
    store.update_run(rid, status="done")
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "src_count": 5,
                                  "tgt_count": 0, "missing": None,
                                  "tail_count": None, "fidelity_pct": None,
                                  "blind_spot": 1, "status": "scoped"}])
    page = c.get(f"/runs/{rid}/elevate")
    assert page.status_code == 200 and "AC" in page.text
    r = c.post(f"/runs/{rid}/elevate", data={"side": "target"})
    assert r.status_code == 303
    log = store.settings_get(f"elevation:{rid}:target")
    assert log and '"ok": true' in log
    r2 = c.post(f"/runs/{rid}/elevate/undo", data={"side": "target"})
    assert r2.status_code == 303
    assert store.settings_get(f"elevation:{rid}:target") is None


def test_unknown_deployment_rejected_before_live_verify(tmp_path):
    # An unknown deployment value must flash-redirect like every other
    # validation error — BEFORE the live verification request, not explode
    # in store.save_connection after a real HTTP call.
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"displayName": "Igor"})
    app, c = mk_app(tmp_path, handler=handler)
    c.post("/migrations", data={"name": "m"})
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://s.atlassian.net",
                     "email": "i@x.y", "api_token": "tok",
                     "deployment": "server"})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert calls["n"] == 0              # rejected before any live request
    assert app.state.store.get_connection(1, "source") is None


def test_index_has_product_select_and_chip(tmp_path):
    # The create-migration form offers a product choice (jira default) and
    # every dashboard card names its product so a mixed dashboard stays
    # readable at a glance.
    app, c = mk_app(tmp_path)
    c.post("/migrations", data={"name": "acme wiki move",
                                "product": "confluence"})
    r = c.get("/")
    assert r.status_code == 200
    assert '<select name="product"' in r.text
    assert 'value="jira"' in r.text and 'value="confluence"' in r.text
    # Card-specific: the global CSS rule and the create-form <option> also
    # contain "product-chip"/"confluence", so assert the rendered chip span.
    assert '<span class="product-chip mono">confluence</span>' in r.text


def test_dashboard_card_stats_use_product_vocabulary(tmp_path):
    # R8: labels adapt project→space, issue→page by product. A finished
    # confluence run's card must read '1 space · 54 pages', never the
    # hardcoded jira nouns — while a jira card on the SAME dashboard keeps
    # project/issues prose.
    app, c = mk_app(tmp_path)
    store = app.state.store
    jmid = store.create_migration("jira move", product="jira")
    jrid = store.create_run(jmid, {})
    store.update_run(jrid, status="done", verdict="CLEAN",
                     stats={"projects": 2, "issues_src_total": 1234,
                            "headlines": [], "project_stats": {}, "areas": {}})
    cmid = store.create_migration("wiki move", product="confluence")
    crid = store.create_run(cmid, {})
    store.update_run(crid, status="done", verdict="CLEAN",
                     stats={"projects": 1, "issues_src_total": 54,
                            "headlines": [], "project_stats": {}, "areas": {}})
    r = c.get("/")
    assert r.status_code == 200
    text = " ".join(r.text.split())          # collapse template whitespace
    assert "2 projects" in text and "1,234</span> issues" in text
    assert "1 space" in text and "54</span> pages" in text
    assert "1 project " not in text and "54</span> issues" not in text


def test_migration_page_has_deployment_select(tmp_path):
    app, c = mk_app(tmp_path)
    c.post("/migrations", data={"name": "m"})
    r = c.get("/migrations/1")
    assert r.status_code == 200
    assert '<select name="deployment"' in r.text
    assert 'value="cloud"' in r.text and 'value="dc"' in r.text


def test_analysis_page_carries_data_product(tmp_path):
    # app.js relabels and builds item links from these data attributes, so
    # the analysis shell must carry the product AND each side's deployment
    # (confluence DC links have no /wiki context path).
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("wiki", product="confluence")
    store.save_connection(mid, "source", "pat", "https://acme.atlassian.net",
                          secret={"email": "igor@acme.example", "token": "t"},
                          deployment="cloud")
    store.save_connection(mid, "target", "pat", "https://wiki.globex.example",
                          secret={"token": "t"}, deployment="dc")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"headlines": [], "project_stats": {}, "areas": {}})
    r = c.get(f"/runs/{rid}/analysis")
    assert r.status_code == 200
    assert 'data-product="confluence"' in r.text
    assert 'data-src-deployment="cloud"' in r.text
    assert 'data-tgt-deployment="dc"' in r.text


def test_run_page_phase_labels_follow_product(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("wiki", product="confluence")
    rid = store.create_run(mid, {})
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "Extract pages" in r.text and "Scope spaces" in r.text
    assert "Extract issues" not in r.text
    mid2 = store.create_migration("tracker")          # jira default
    rid2 = store.create_run(mid2, {})
    r2 = c.get(f"/runs/{rid2}")
    assert "Extract issues" in r2.text and "Scope projects" in r2.text


def test_appjs_tierlabel_guards_missing_fix():
    # M1: a finding without a `fix` object must not crash render(). tierLabel(f)
    # must read from a guarded local (f.fix || {}) the way the loop body does,
    # never dereference f.fix.tier_label / f.fix.tier directly (a missing fix
    # throws a TypeError inside render() and blanks the whole analysis view).
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    # locate the tierLabel definition + its body (up to the next blank line)
    idx = src.index("const tierLabel")
    body = src[idx:idx + 300]
    assert "f.fix || {}" in body, \
        "tierLabel must guard against a missing fix via `f.fix || {}`"
    assert "f.fix.tier_label" not in body, \
        "tierLabel must not dereference f.fix.tier_label without a guard"
    assert "f.fix.tier" not in body, \
        "tierLabel must not dereference f.fix.tier without a guard"


def test_appjs_collision_tile_sub_uses_vocab():
    # No JS runner exists, so guard at source level: the Key collisions KPI
    # tile sub must interpolate vocab.items (a confluence run with title
    # collisions must read pages, not the jira word issues), mirroring the
    # Orphans tile's `Target-only ${vocab.items}`.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Same key, two issues" not in src
    assert "Same key, two ${vocab.items}" in src


def test_run_page_env_audit_uses_env_phase_labels(tmp_path):
    # An env_audit run's phases are [verify, scope, gather, checks, analysis];
    # PHASE_KEYS must branch on run.kind so gather/checks/analysis map to real
    # indices (not -1 -> all-done) and the migration-only phase labels
    # (Permission check / Extract issues / Compare fidelity) never render.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, phase="gather")
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "Gather config" in r.text or "Gather" in r.text
    assert "Health checks" in r.text or "Checks" in r.text
    assert "AI assessment" in r.text or "AI analysis" in r.text or "Analysis" in r.text
    # migration-only phase labels must not appear on an env run
    assert "Permission check" not in r.text
    assert "Extract issues" not in r.text
    assert "Compare fidelity" not in r.text


def test_appjs_verdict_map_covers_env_audit_verdicts():
    # build_env_summary (auditor/envaudit/report.py) emits NEEDS_ATTENTION,
    # HEALTHY_WITH_NOTES and HEALTHY. The VERDICT map must carry an entry for
    # each so verdictMeta does not fall through to the generic teal tails banner
    # (wrong severity tone for NEEDS_ATTENTION) or render HEALTHY identically to
    # CLEAN_WITH_TAILS.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "NEEDS_ATTENTION:" in src
    assert "HEALTHY_WITH_NOTES:" in src
    assert "HEALTHY:" in src
    # NEEDS_ATTENTION is a medium-severity verdict: it must NOT use the teal
    # tails banner class. It should read as a gaps-style (amber) severity.
    na_line = next(l for l in src.splitlines() if "NEEDS_ATTENTION:" in l)
    assert "vb-gaps" in na_line and "vb-tails" not in na_line


def test_run_html_pill_covers_env_audit_verdicts():
    # run.html's done-state verdict pill only handled the four migration
    # verdicts; env_audit done runs fell through to the generic DONE pill.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "templates" / "run.html").read_text(encoding="utf-8")
    assert "NEEDS_ATTENTION" in src
    assert "HEALTHY_WITH_NOTES" in src
    assert "'HEALTHY'" in src or '"HEALTHY"' in src


def test_run_html_appendlog_escapes_event_message(tmp_path):
    # No JS runner exists, so guard at source level. appendLog() builds the
    # terminal line via innerHTML; env_stages routes raw external-API prose
    # (AI error strings) and user-controlled values (display_name from /myself)
    # through add_event -> SSE -> appendLog. A message containing <, >, or &
    # must be HTML-escaped before concatenation or it injects arbitrary HTML.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "templates" / "run.html").read_text(encoding="utf-8")
    # An esc() helper must exist and the raw `msg` must never be concatenated
    # into innerHTML without it.
    assert "function esc(" in src
    assert "+ msg +" not in src and "+ msg + '</span>'" not in src
    assert "<span class=\"wn\">' + msg" not in src


def test_elevation_follows_connector_capability(tmp_path, monkeypatch):
    """connector.supports_elevation is the single source of truth: a
    registered product whose connector declares the capability reaches the
    confirm page even though it is not literally named jira; a connector
    without it stays blocked; an unregistered legacy product row redirects
    instead of raising."""
    import dataclasses
    from auditor import connectors as C
    fake = dataclasses.replace(C.JIRA, product="fakejira")
    monkeypatch.setitem(C._REGISTRY, "fakejira", fake)
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store

    mid = store.create_migration("cap-yes", product="fakejira")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    assert c.get(f"/runs/{rid}/elevate").status_code == 200

    mid2 = store.create_migration("cap-no", product="confluence")
    rid2 = store.create_run(mid2, {})
    store.update_run(rid2, status="done")
    r2 = c.get(f"/runs/{rid2}/elevate")
    assert r2.status_code == 303
    assert r2.headers["location"] == f"/runs/{rid2}"

    mid3 = store.create_migration("legacy")
    store._exec("UPDATE migrations SET product='bamboo' WHERE id=?", (mid3,))
    rid3 = store.create_run(mid3, {})
    store.update_run(rid3, status="done")
    r3 = c.get(f"/runs/{rid3}/elevate")
    assert r3.status_code == 303
    assert r3.headers["location"] == f"/runs/{rid3}"


def test_run_html_env_audit_has_report_phase_label(tmp_path):
    # Spec phase labels: Connect / Gather config / Health checks / AI analysis / Report.
    # 'Scope spaces|projects' is not in the spec list; 'Report' must be the 5th label.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, phase="gather")
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "Report" in r.text
    assert "Scope spaces" not in r.text and "Scope projects" not in r.text


def test_appjs_env_ai_section_renders_summary():
    # The EnvAnalysis.aiSection() must render ai.summary as a narrative paragraph
    # before the themes list. The spec explicitly lists 'summary' as a required
    # element of the AI assessment section; the engine populates it in ai['summary'].
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    # summaryHtml must be constructed from ai.summary and placed in the return block
    assert "ai.summary" in src
    assert "summaryHtml" in src
    # The summaryHtml must be interpolated into the returned card HTML
    ai_section_body = src[src.index("function aiSection(ai)"):
                          src.index("/* Verdict pill banner")]
    assert "${summaryHtml}" in ai_section_body


def test_left_nav_split_into_two_independent_sections(tmp_path):
    # The sidebar must offer Migration audits and Environment audits as two
    # separate entries, and the active highlight must follow the section you
    # are in (independent functions, not a single Migrations link).
    _, c = mk_app(tmp_path)
    mig = c.get("/").text
    env = c.get("/environments").text
    for html in (mig, env):
        assert 'href="/"' in html and "Migration audits" in html
        assert 'href="/environments"' in html and "Environment audits" in html
    assert 'href="/" class="on"' in mig
    assert 'href="/environments" class="on"' in env
    assert 'href="/environments" class="on"' not in mig


def test_environments_list_route_renders(tmp_path):
    _, c = mk_app(tmp_path)
    r = c.get("/environments")
    assert r.status_code == 200
    assert "Environment audits" in r.text


def test_audit_lists_are_filtered_by_type(tmp_path):
    # Each section lists ONLY its own audit_type — the two functions are
    # independent and never bleed into each other's list.
    app, c = mk_app(tmp_path)
    store = app.state.store
    store.create_migration("a migration job", audit_type="migration")
    store.create_migration("an environment job", audit_type="environment")
    mig_list = c.get("/").text
    env_list = c.get("/environments").text
    assert "a migration job" in mig_list and "an environment job" not in mig_list
    assert "an environment job" in env_list and "a migration job" not in env_list


def test_create_from_environment_section_creates_environment_audit(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "prod jira",
                                    "audit_type": "environment"})
    assert r.status_code == 303
    mid = int(r.headers["location"].rsplit("/", 1)[-1])
    assert app.state.store.get_migration(mid)["audit_type"] == "environment"


def test_environment_create_error_returns_to_environment_list(tmp_path):
    # A failed environment create bounces to /environments, NOT the migration
    # dashboard — the error stays in the section the user was working in.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "x", "product": "bamboo",
                                    "audit_type": "environment"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/environments?error=")
    assert app.state.store.list_migrations() == []


def test_environment_audit_detail_is_environment_section(tmp_path):
    # An environment audit's shared /migrations/{id} detail must present as the
    # environment section: breadcrumb back to /environments and the sidebar
    # Environment-audits entry active (NOT the migration entry).
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env detail", audit_type="environment")
    html = c.get(f"/migrations/{mid}").text
    assert 'href="/environments">Environment audits</a>' in html
    assert 'href="/environments" class="on"' in html
    assert 'href="/" class="on"' not in html


def test_migration_audit_detail_is_migration_section(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("mig detail")          # migration default
    html = c.get(f"/migrations/{mid}").text
    assert 'href="/">Migration audits</a>' in html
    assert 'href="/" class="on"' in html
    assert 'href="/environments" class="on"' not in html


def test_environment_audit_analysis_breadcrumb_points_to_environments(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env ana", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, status="done", verdict="HEALTHY",
                     stats={"headlines": [], "ai": {"skipped": True}})
    html = c.get(f"/runs/{rid}/analysis").text
    assert 'href="/environments">Environment audits</a>' in html
    assert 'href="/environments" class="on"' in html


def test_environment_audit_run_page_breadcrumb_points_to_environments(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env run", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    html = c.get(f"/runs/{rid}").text
    assert 'href="/environments">Environment audits</a>' in html
    assert 'href="/environments" class="on"' in html
    assert 'href="/">Migration audits</a>' not in html


def test_appjs_findings_list_no_aggregate_badge_for_all():
    # findingsList() must not assign the single aggregate kindKey badge to every
    # finding. Per-finding severity must be derived independently (e.g. from
    # headline text), not from the first non-zero key in the shared byKind map.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    findings_fn = src[src.index("function findingsList("):
                      src.index("/* AI assessment section")]
    # The old pattern: a single kindKey taken from Object.keys(byKind) and then
    # applied identically to every headline in the .map(). It must not appear.
    assert 'Object.keys(byKind || {}).find(' not in findings_fn
    # A per-finding severity helper or inline derivation must be present instead.
    assert "_headlineSev" in findings_fn or "sev" in findings_fn


def test_appjs_env_findings_by_category_reads_findings_array():
    # findingsByCategory must read R.findings (the array from the summary API).
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    # The function must exist and reference .findings
    assert "function findingsByCategory(" in src
    # Must access the findings array from the summary object
    assert "R.findings" in src or ".findings" in src


def test_appjs_env_findings_grouped_by_category_in_fixed_order():
    # Findings must be grouped by category in the fixed order:
    # Performance, Security, Structure, Hygiene, Coverage.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    fn_body = src[src.index("function findingsByCategory("):
                  src.index("function aiSection(")]
    # The fixed order list must appear in the function body
    order_present = (
        '"Performance"' in fn_body and
        '"Security"' in fn_body and
        '"Structure"' in fn_body and
        '"Hygiene"' in fn_body and
        '"Coverage"' in fn_body
    )
    assert order_present, "Category order list missing from findingsByCategory"
    # Grouping must iterate over category (not emit all flat)
    assert "category" in fn_body


def test_appjs_env_findings_tier_badge_classes_and_labels():
    # The three fix tiers must map to distinct badge classes and label strings.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    fn_body = src[src.index("function findingsByCategory("):
                  src.index("function aiSection(")]
    # tier → class mapping must cover all three tiers
    assert "tier-app" in fn_body or "t-app" in src
    assert "tier-human" in fn_body or "t-human" in src
    assert "tier-unfixable" in fn_body or "t-unfixable" in src
    # The three tier labels must be present (they come from fix.tier_label or
    # hard-coded mapping so the labels always render correctly)
    assert "Fixable by the app" in fn_body
    assert "Fixable by a human" in fn_body
    assert "Re-migration suggested" in fn_body


def test_appjs_env_findings_fix_text_is_escaped():
    # All fix text fields (title, detail, caveat) and finding fields (area, name)
    # MUST go through esc() before insertion into innerHTML.
    # A raw template literal like `${f.fix.title}` without esc() is a XSS vector.
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    fn_body = src[src.index("function findingsByCategory("):
                  src.index("function aiSection(")]
    # esc() must be called on fix fields — look for esc(f.fix. or esc(fix.
    assert "esc(f.fix." in fn_body or "esc(fix." in fn_body
    # Raw unescaped fix.title must NOT appear as a bare template interpolation
    assert "${f.fix.title}" not in fn_body
    assert "${f.fix.detail}" not in fn_body
    # area and name must also be escaped
    assert "esc(f.area" in fn_body or "esc(f.name" in fn_body or "esc(f." in fn_body


def _appjs_src():
    return (pathlib.Path(__file__).resolve().parents[1]
            / "webapp" / "static" / "app.js").read_text(encoding="utf-8")


def _findings_by_cat_body(src):
    return src[src.index("function findingsByCategory("):
               src.index("function aiSection(")]


def test_appjs_env_findings_grouped_by_problem_kind():
    # The redesigned results view groups findings BY PROBLEM TYPE (kind) into one
    # problem card per kind, NOT a flat per-finding row list. On a real instance
    # this view had 1600 findings; flat rows were unusable. The grouping loop must
    # bucket by f.kind and reference the generic problem label (fix.label).
    fn = _findings_by_cat_body(_appjs_src())
    # builds a per-kind grouping (a dict keyed by f.kind)
    assert "f.kind" in fn, "findingsByCategory must group by f.kind"
    # the problem card uses the generic fix.label for its header
    assert "fix.label" in fn, "problem card header must use the generic fix.label"
    # a fallback for a kind with no registered label (prettified kind)
    assert "prettyKind" in fn or "fallback" in fn.lower()
    # the affected COUNT is rendered in the header (in parens)
    assert ".count" in fn or "items.length" in fn
    assert "(${" in fn, "problem count must render in parens"


def test_appjs_env_affected_list_capped_and_collapsible():
    # The affected-object list must be capped + collapsible: show the first N
    # inline, then a <details>/"show all" affordance for the rest (no JS
    # framework). A cap constant must exist so the cap is a single source of truth.
    src = _appjs_src()
    fn = _findings_by_cat_body(src)
    # a named cap constant (single source of truth for the inline cap)
    assert "AFFECTED_CAP" in src, "a cap constant must gate the affected-object list"
    # collapsible affordance: a <details>/<summary> "show all N" pattern
    assert "<details" in fn and "<summary" in fn
    assert "show all" in fn
    # the cap is actually applied via slice(0, AFFECTED_CAP)
    assert "slice(0, AFFECTED_CAP)" in fn or "slice(0,AFFECTED_CAP)" in fn


def test_appjs_env_affected_names_are_escaped():
    # Affected-object names are live Jira/Confluence object names — every one MUST
    # go through esc(). No raw ${...name} / ${...label} interpolation may appear.
    fn = _findings_by_cat_body(_appjs_src())
    # names escaped (the affected list maps f.name through esc())
    assert "esc(f.name" in fn
    # the generic label is escaped in the header
    assert "esc(label)" in fn or "esc(fix.label" in fn
    # no raw unescaped name/label interpolation
    assert "${f.name}" not in fn
    assert "${fix.label}" not in fn
    assert "${f.label}" not in fn


def test_appjs_env_problem_cards_sorted_by_severity_then_count():
    # Problem cards within a category sort by severity (high→low) then count
    # (desc) so the biggest / most severe problems come first.
    fn = _findings_by_cat_body(_appjs_src())
    # a severity-rank table drives the ordering (high=0 sorts first)
    assert "SEV_RANK" in fn
    assert "high: 0" in fn or "high:0" in fn
    # an explicit sort by rank then count is present
    assert ".sort(" in fn
    assert "rank" in fn and "count" in fn


def test_appjs_env_six_category_order_preserved():
    # The 6 top-level category sections stay in the fixed orienting order.
    fn = _findings_by_cat_body(_appjs_src())
    order = ["Performance", "Security", "Structure", "DataQuality",
             "Hygiene", "Coverage"]
    # all six appear in the body, in order, inside the ORDER array
    positions = [fn.find(f'"{c}"') for c in order]
    assert all(p >= 0 for p in positions), \
        f"missing category in findingsByCategory: {dict(zip(order, positions))}"
    assert positions == sorted(positions), \
        "the 6 categories must appear in the fixed orienting order"


def test_appjs_env_app_tier_cards_hint_fix_options():
    # app-tier problem cards must hint that they can be auto-fixed on the Fix
    # options screen (the page already has the Fix options button; we only hint).
    fn = _findings_by_cat_body(_appjs_src())
    assert 'tier === "app"' in fn
    assert "Fix options" in fn


# ---------------------------------------------------------------------------
# AI-discovered issues section (the AI acting as a complementary 2nd auditor).
# The EnvAnalysis renderer must read ai.ai_findings and render a distinct
# "AI-discovered issues" section: one card per finding with a severity badge,
# title, area, observation, and recommendation — all esc()'d, visually marked
# as AI (distinct from the deterministic problem-type cards), and degrading
# gracefully (rendering nothing) when ai_findings is empty or AI was skipped.
# ---------------------------------------------------------------------------

def _ai_findings_section_body(src):
    """The aiFindingsSection() renderer lives between aiSection() and the
    Verdict pill banner comment."""
    start = src.index("function aiFindingsSection(")
    end = src.index("/* Verdict pill banner", start)
    return src[start:end]


def test_appjs_env_ai_findings_section_exists_and_reads_ai_findings():
    src = _appjs_src()
    assert "function aiFindingsSection(" in src, \
        "a dedicated AI-discovered-issues renderer must exist"
    # It must read the ai.ai_findings array from the ai dict.
    assert "ai.ai_findings" in src or "ai_findings" in src
    body = _ai_findings_section_body(src)
    assert "ai_findings" in body


def test_appjs_env_ai_findings_section_rendered_from_render():
    # render() must call aiFindingsSection so the section actually reaches the DOM.
    src = _appjs_src()
    render_body = src[src.index("async render(el, runId)"):]
    assert "aiFindingsSection(" in render_body


def test_appjs_env_ai_findings_fields_rendered_and_escaped():
    # Each AI finding card renders the RICH fields (title, area, severity, root
    # cause, risk, remediation steps, priority, effort — plus legacy observation/
    # recommendation as fallback) and escapes EVERY text field.
    body = _ai_findings_section_body(_appjs_src())
    for field in ("title", "area", "severity", "root_cause", "risk",
                  "remediation_steps", "priority", "effort", "observation",
                  "recommendation"):
        assert field in body, f"ai finding {field} not rendered"
    # title/area escaped directly off the finding.
    assert "esc(f.title" in body and "esc(f.area" in body
    # severity into `sev`; priority/effort escaped; root_cause/risk through the
    # block() helper (esc(text)); remediation steps esc()'d per item.
    assert "esc(sev" in body
    assert "esc(String(f.priority" in body and "esc(f.effort" in body
    assert "esc(text)" in body and "esc(s)" in body
    # No raw unescaped interpolation of any scalar AI-finding field.
    for field in ("title", "area", "severity", "priority", "effort"):
        assert "${f." + field + "}" not in body, \
            f"ai finding {field} interpolated without esc()"


def test_appjs_env_ai_findings_visually_distinct_ai_tag():
    # The AI-discovered section must be visually distinct from the deterministic
    # problem-type cards — an explicit "AI" tag / accent marks it as model output.
    body = _ai_findings_section_body(_appjs_src())
    assert "AI-discovered" in body or "AI discovered" in body or \
        "AI-identified" in body, "section needs an AI-discovered heading"
    # An explicit AI tag/badge class marks the cards as AI output.
    assert "ai-finding" in body or "ai-tag" in body or "ai-badge" in body, \
        "AI cards need a distinct ai-* class so they read as AI, not rules"


def test_appjs_env_ai_findings_severity_badge_uses_sevclass():
    # The per-card severity badge reuses the shared sevClass() palette so high/
    # medium/low render consistently with the rest of the env analysis UI.
    body = _ai_findings_section_body(_appjs_src())
    assert "sevClass(" in body


def test_appjs_env_ai_findings_graceful_when_empty_or_skipped():
    # Empty ai_findings (or skipped AI) must render NOTHING (no empty card shell).
    body = _ai_findings_section_body(_appjs_src())
    # A guard returning "" for the empty/skipped case.
    assert 'return ""' in body or "return ''" in body, \
        "aiFindingsSection must return empty for no findings / skipped AI"
    # It must check length and/or skipped before rendering cards.
    assert ".length" in body
    assert "skipped" in body


def test_appjs_env_ai_findings_does_not_drive_verdict():
    # The verdict banner is rule-driven; ai_findings must not feed verdictBanner.
    src = _appjs_src()
    vb_start = src.index("function verdictBanner(")
    vb_end = src.index("return {", vb_start)
    vb_body = src[vb_start:vb_end]
    assert "ai_findings" not in vb_body, \
        "ai_findings must NOT influence the rule-driven verdict banner"


# --------------------------------------------------------------- delete: store


def _seed_run_with_children(store, mid, kind="audit"):
    """Create a FINISHED run and populate a row in every run-scoped child table
    so the cascade can be asserted exhaustively. Marked done so it is not the
    migration's active run (active-run refusal is exercised separately).
    Returns the run id."""
    rid = store.create_run(mid, {}, kind=kind)
    store.update_run(rid, status="done")
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "src_count": 1,
                                  "tgt_count": 1, "missing": 0,
                                  "tail_count": 0, "fidelity_pct": 100.0,
                                  "blind_spot": 0, "status": "scoped"}])
    store.insert_findings_issue(rid, [{"project": "AC", "kind": "missing",
                                       "src_key": "AC-1"}])
    store.insert_findings_config(rid, [{"area": "workflows", "name": "wf",
                                        "kind": "extra"}])
    store.insert_fix_actions(rid, [{"fix_id": "f1", "ok": True}])
    store.add_event(rid, "verify", "info", "evt")
    store.save_solutions(rid, "sig-1", {"links": []})
    return rid


def _child_counts(store, rid):
    return {t: store._row(f"SELECT COUNT(*) c FROM {t} WHERE run_id=?",
                          (rid,))["c"]
            for t in ("run_projects", "findings_issue", "findings_config",
                      "fix_actions", "events", "finding_solutions")}


def test_delete_run_cascades_and_leaves_others_intact(tmp_path):
    app, _ = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    keep = _seed_run_with_children(store, mid)
    drop = _seed_run_with_children(store, mid)
    # both runs start with one row in every child table
    assert all(v == 1 for v in _child_counts(store, drop).values())
    assert all(v == 1 for v in _child_counts(store, keep).values())

    store.delete_run(drop)

    assert store.get_run(drop) is None
    assert all(v == 0 for v in _child_counts(store, drop).values()), \
        "every run-scoped child table must be emptied for the deleted run"
    # the other run and ALL of its children survive untouched
    assert store.get_run(keep) is not None
    assert all(v == 1 for v in _child_counts(store, keep).values()), \
        "a sibling run's children must not be touched"


def test_delete_migration_cascades_runs_connections_keeps_others(tmp_path):
    app, _ = mk_app(tmp_path)
    store = app.state.store
    # vault row is independent of any migration — must survive a delete
    vault = store.create_saved_connection("v", "jira", "cloud",
                                          "https://v.example", "i@x.y", "tok")
    drop = store.create_migration("drop")
    store.save_connection(drop, "source", "pat", "https://s.example",
                          {"token": "t", "email": "i@x.y"})
    r1 = _seed_run_with_children(store, drop)
    r2 = _seed_run_with_children(store, drop)
    keep = store.create_migration("keep")
    store.save_connection(keep, "source", "pat", "https://k.example",
                          {"token": "t", "email": "i@x.y"})
    kr = _seed_run_with_children(store, keep)

    store.delete_migration(drop)

    assert store.get_migration(drop) is None
    assert store.list_runs(drop) == []
    assert store.get_connection(drop, "source") is None
    for rid in (r1, r2):
        assert all(v == 0 for v in _child_counts(store, rid).values())
    # the other migration, its run + children, its connection all survive
    assert store.get_migration(keep) is not None
    assert store.get_connection(keep, "source") is not None
    assert all(v == 1 for v in _child_counts(store, kr).values())
    # the independent vault entry is never touched
    assert store.get_saved_connection(vault) is not None


# --------------------------------------------------------------- delete: routes


def test_delete_run_route_redirects_to_migration_and_removes(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = _seed_run_with_children(store, mid)
    store.update_run(rid, status="done")
    r = c.post(f"/runs/{rid}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == f"/migrations/{mid}"
    assert store.get_run(rid) is None
    assert all(v == 0 for v in _child_counts(store, rid).values())


def test_delete_run_route_refuses_active_running_run(tmp_path):
    # A run that is both status='running' AND the engine's active run for its
    # migration must be refused: redirect back to the run with an error, and
    # the run (and its children) stay put.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})          # status defaults to 'running'
    store.add_event(rid, "verify", "info", "evt")
    # sanity: this is the active run for the migration
    assert store.active_run(mid)["id"] == rid
    r = c.post(f"/runs/{rid}/delete")
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert store.get_run(rid) is not None     # still present — refused
    assert store.get_events(rid)              # children intact


def test_delete_run_route_unknown_redirects_home(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/runs/999/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_delete_run_get_does_not_delete(tmp_path):
    # Only POST is wired; a GET (crawl/prefetch) must NOT delete.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.update_run(rid, status="done")
    r = c.get(f"/runs/{rid}/delete")
    assert r.status_code in (404, 405)
    assert store.get_run(rid) is not None


def test_delete_migration_route_redirects_home_for_migration(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")          # migration default
    _seed_run_with_children(store, mid)
    r = c.post(f"/migrations/{mid}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert store.get_migration(mid) is None


def test_delete_migration_route_redirects_environments_for_env(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    _seed_run_with_children(store, mid, kind="env_audit")
    r = c.post(f"/migrations/{mid}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/environments"
    assert store.get_migration(mid) is None


def test_delete_migration_route_refuses_active_run(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})          # running -> active
    assert store.active_run(mid) is not None
    r = c.post(f"/migrations/{mid}/delete")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(f"/migrations/{mid}?error=")
    assert "Cancel" in loc and "active%20run" in loc
    assert store.get_migration(mid) is not None   # refused, still present


def test_delete_migration_route_unknown_redirects_home(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations/999/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_delete_migration_get_does_not_delete(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("m")
    r = c.get(f"/migrations/{mid}/delete")
    assert r.status_code in (404, 405)
    assert store.get_migration(mid) is not None


def test_migration_page_renders_delete_audit_control(tmp_path):
    # The delete-audit form (topbar) must appear on the shared detail page for
    # BOTH a migration and an environment audit, in every wizard state.
    app, c = mk_app(tmp_path)
    store = app.state.store
    for atype in ("migration", "environment"):
        mid = store.create_migration(f"{atype} job", audit_type=atype)
        html = c.get(f"/migrations/{mid}").text
        assert f'action="/migrations/{mid}/delete"' in html
        assert "cannot be undone" in html


def test_env_page_renders_per_run_delete_button(tmp_path):
    # The environment-audit run history renders without needing connections, so
    # the per-run Delete button is assertable directly there.
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env job", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_audit")
    store.update_run(rid, status="done")
    html = c.get(f"/migrations/{mid}").text
    assert f'action="/runs/{rid}/delete"' in html
    assert "cannot be undone" in html


def test_migration_detail_renders_per_run_delete_button(tmp_path):
    # For a migration audit the run history only renders once BOTH sides verify,
    # so connect them, then assert the per-run Delete button is present.
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    c.post("/migrations", data={"name": "m"})
    for role in ("source", "target"):
        c.post("/migrations/1/connections",
               data={"role": role, "site_url": f"https://{role}.atlassian.net",
                     "email": "i@x.y", "api_token": "t"})
    rid = store.create_run(1, {})
    store.update_run(rid, status="done")
    html = c.get("/migrations/1").text
    assert f'action="/runs/{rid}/delete"' in html


# ----------------------------------------------------- security: CSRF origin


def test_csrf_cross_origin_post_blocked(tmp_path):
    # A state-changing POST carrying a cross-origin Origin header (a browser
    # CSRF submit from a malicious page) must be rejected with 403 before the
    # route runs — nothing is created.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "evil"},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert app.state.store.list_migrations() == []


def test_csrf_cross_origin_referer_blocked(tmp_path):
    # Same rule via Referer when Origin is absent: a cross-origin Referer is a
    # browser request and must be blocked.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "evil"},
               headers={"Referer": "http://evil.example/page"})
    assert r.status_code == 403
    assert app.state.store.list_migrations() == []


def test_csrf_same_origin_post_proceeds(tmp_path):
    # A same-origin Origin (the app's own forms) passes through and the POST
    # runs normally. TestClient's own origin is http://testserver.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "ok"},
               headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    assert len(app.state.store.list_migrations()) == 1


def test_csrf_no_origin_no_referer_proceeds(tmp_path):
    # Non-browser callers (curl, the test client default) send neither header;
    # CSRF is browser-only, so these are allowed. This is also what every other
    # test in this file relies on.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "cli"})
    assert r.status_code == 303
    assert len(app.state.store.list_migrations()) == 1


def test_csrf_cross_site_blocked_via_sec_fetch_when_no_origin(tmp_path):
    # Review: a no-Origin/no-Referer state-changing request previously bypassed
    # CSRF. A modern browser still sends Sec-Fetch-Site even without Origin, so a
    # cross-site submit is now blocked.
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "x"},
               headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert app.state.store.list_migrations() == []


def test_csrf_same_origin_sec_fetch_proceeds(tmp_path):
    app, c = mk_app(tmp_path)
    r = c.post("/migrations", data={"name": "ok2"},
               headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303


def test_csrf_get_never_blocked_even_cross_origin(tmp_path):
    # GET is a safe method: a cross-origin Origin header must NOT block it.
    app, c = mk_app(tmp_path)
    r = c.get("/", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


# ------------------------------------------------- security: DB file perms


def test_store_init_hardens_db_file_perms(tmp_path, monkeypatch):
    # The SQLite DB holds the Fernet-encrypted secrets; Store() must chmod it
    # 0600 (and its data dir 0700). On a no-op filesystem (WSL drvfs) the mode
    # never sticks, so assert the chmod was ATTEMPTED for the db path rather
    # than reading the resulting mode.
    import webapp.store as store_mod
    calls = []
    real_chmod = os.chmod

    def spy(path, mode):
        calls.append((str(path), mode))
        try:
            real_chmod(path, mode)
        except OSError:
            pass
    monkeypatch.setattr(store_mod.os, "chmod", spy)
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    store_mod.Store(db_path=cfg.db_path, key_path=cfg.key_path,
                    secret_key=cfg.secret_key)
    db_chmods = [m for (p, m) in calls if p == cfg.db_path]
    assert 0o600 in db_chmods, "Store must chmod the db file 0600"
    dir_chmods = [m for (p, m) in calls
                  if p == (os.path.dirname(cfg.db_path) or ".")]
    assert 0o700 in dir_chmods, "Store must chmod the data dir 0700"


def test_store_init_survives_chmod_oserror(tmp_path, monkeypatch):
    # A filesystem that rejects chmod must not crash Store() construction.
    import webapp.store as store_mod

    def boom(path, mode):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(store_mod.os, "chmod", boom)
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    # Must not raise (covers both the db/data-dir and the .key chmod paths).
    store_mod.Store(db_path=cfg.db_path, key_path=cfg.key_path,
                    secret_key=None)


# --------------------------------------------- security: path-traversal keys


def test_scope_rejects_traversal_container_key():
    from auditor.scope import match_projects, is_safe_container_key
    src = [{"key": "ACME", "name": "Acme"},
           {"key": "../../etc/x", "name": "evil"}]
    tgt = [{"key": "ACME", "name": "Acme"},
           {"key": "../../etc/x", "name": "evil"}]
    out = match_projects(src, tgt)
    keys = [m["key"] for m in out["matched"]]
    assert "ACME" in keys
    assert "../../etc/x" not in keys
    # source_only / target_only must also be clean of the unsafe key
    all_keys = (keys + [m["key"] for m in out["source_only"]]
                + [m["key"] for m in out["target_only"]])
    assert all("/" not in k and ".." not in k for k in all_keys)
    # the validator itself accepts real keys/ids and rejects traversal
    assert is_safe_container_key("ACME")
    assert is_safe_container_key("AC-1")
    assert is_safe_container_key("customfield_10001")
    assert not is_safe_container_key("../../etc/x")
    assert not is_safe_container_key("a/b")
    assert not is_safe_container_key("")
    assert not is_safe_container_key(None)


def test_payload_skips_unsafe_field_id():
    # _capture_custom_field must skip a field whose audited-instance id is not
    # filesystem-safe (it becomes a {field_id}.jsonl.gz filename downstream).
    from auditor.remediation.payload import _capture_custom_field

    class FakeClient:
        api_prefix = "/rest/api/3"

        def __init__(self, fid):
            self._fid = fid

        def paginate_start_at(self, path):
            if path.endswith("/field"):
                return ([{"custom": True, "name": "Sprint",
                          "id": self._fid,
                          "schema": {"custom": "x:select"}}], None)
            return ([], None)

    # A safe id is captured.
    ok = _capture_custom_field(FakeClient("customfield_10001"), "Sprint")
    assert ok is not None and ok["field_id"] == "customfield_10001"
    # A traversal id is rejected (None, never used as a path).
    bad = _capture_custom_field(FakeClient("../../../etc/passwd"), "Sprint")
    assert bad is None


# ------------------------------------------------ security: oauth callback


def test_oauth_callback_handles_exchange_failure(tmp_path):
    # An Atlassian 400 on the token exchange must degrade to the OAuth-failed
    # flash on the index, NOT raise a 500.
    def deny_token(req):
        # /oauth/token 400s; anything else would be unexpected here.
        return httpx.Response(400, text="invalid_grant")
    app, c = mk_app(tmp_path, handler=deny_token)
    store = app.state.store
    store.settings_set("oauth_client_id", "cid")
    store.settings_set("oauth_client_secret_enc",
                       store.encrypt({"secret": "sek"}).decode())
    mid = store.create_migration("m")
    # Seed a valid pending-state so the callback reaches the token exchange.
    state = "test-state"
    app.state.oauth_pending[state] = {"migration_id": mid, "role": "source"}
    r = c.get(f"/oauth/callback?state={state}&code=abc")
    assert r.status_code == 200
    assert "OAuth failed" in r.text


def test_healthz_ok(tmp_path):
    from webapp.store import SCHEMA_VERSION
    _, c = mk_app(tmp_path)
    r = c.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] is True
    assert body["schema_version"] == SCHEMA_VERSION
    assert "version" in body


def test_cli_backup_creates_snapshot(tmp_path, monkeypatch):
    import sys
    from webapp import main as m
    monkeypatch.setenv("MA_DATA_DIR", str(tmp_path / "d"))
    for v in ("MA_BIND", "MA_ALLOW_PUBLIC_BIND"):
        monkeypatch.delenv(v, raising=False)
    dest = str(tmp_path / "snap.db")
    monkeypatch.setattr(sys, "argv", ["migration-auditor", "backup", dest])
    m.cli()
    assert os.path.exists(dest)
