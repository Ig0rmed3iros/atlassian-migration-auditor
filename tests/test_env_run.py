import time, json
import httpx
from webapp.store import Store
from webapp.runs import RunEngine
from auditor.client import Connection, JiraClient
from auditor.confluence.client import ConfluenceClient
from auditor.connectors import get_connector


def _wait(store, rid, t=5):
    end = time.time() + t
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_env_run_workspace_has_no_target_dir(tmp_path):
    # An env audit is single-connection and read-only: it never writes to a
    # target. The run workspace must therefore not carry a spurious tgt/ dir.
    import os
    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    ws_root = str(tmp_path / "ws")
    env_stages = {
        "verify": lambda ctx: None, "scope": lambda ctx: None,
        "gather": lambda ctx: ctx.update(snapshot={"areas": {}}),
        "checks": lambda ctx: ctx.update(env_findings=[]),
        "analysis": lambda ctx: ctx.update(ai={"skipped": True}),
    }
    engine = RunEngine(store, ws_root, stages={}, env_stages=env_stages)
    rid = engine.start(mid, {}, kind="env_audit")
    _wait(store, rid)
    run_ws = os.path.join(ws_root, str(mid), str(rid))
    assert os.path.isdir(os.path.join(run_ws, "src"))
    assert not os.path.exists(os.path.join(run_ws, "tgt"))


def test_env_run_phases_and_finalize(tmp_path):
    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    seen = []
    env_stages = {
        "verify": lambda ctx: seen.append("verify"),
        "scope": lambda ctx: None,
        "gather": lambda ctx: ctx.update(snapshot={"areas": {}}),
        "checks": lambda ctx: ctx.update(env_findings=[
            {"area": "workflows", "name": "WF", "kind": "workflow_no_transitions",
             "severity": "high", "detail": {}}]),
        "analysis": lambda ctx: ctx.update(ai={"skipped": True, "health_score": None}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={}, env_stages=env_stages)
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done" and r["verdict"] == "CRITICAL" and "verify" in seen
    rows = store.query_config(rid, "workflows")
    assert rows and rows[0]["kind"] == "workflow_no_transitions"
    stats = json.loads(r["stats_json"])
    assert stats["high"] == 1
    assert stats["ai"]["skipped"] is True
    assert "headlines" in stats


def test_env_run_real_stages_end_to_end(tmp_path, monkeypatch):
    """The production env stages run through the engine against a mock source:
    one client is built (require_both=False), config is gathered, deterministic
    checks flag a transition-less workflow, and AI is skipped (no key) without
    blocking. A write to the source would fail the test loudly."""
    import webapp.env_stages as es

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})

    def handler(req):
        p = str(req.url.path)
        if req.method != "GET":
            raise AssertionError(
                f"env audit wrote to the SOURCE: {req.method} {p}")
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me",
                                             "emailAddress": "a@b.c",
                                             "displayName": "Igor"})
        if p == "/rest/api/3/project/search":
            return httpx.Response(200, json={
                "values": [{"key": "ACME", "name": "Acme", "id": "1"}],
                "isLast": True})
        if p.endswith("/workflow/search"):
            # one workflow with statuses but no transitions -> high finding
            return httpx.Response(200, json={"values": [
                {"id": {"name": "WF"}, "statuses": [{"name": "Open"}],
                 "transitions": []}], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("/status"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"values": [], "isLast": True})

    def fake_clients(store_, mid_, http=None, require_both=True):
        # An env audit must build only the SOURCE client.
        assert require_both is False
        conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, None, get_connector("jira")
    monkeypatch.setattr(es, "build_clients", fake_clients)
    # No AI provider configured -> AI is skipped, never blocks.
    monkeypatch.setattr(es, "ai_provider", lambda store_: None)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=es.build_env_stages())
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert r["verdict"] == "CRITICAL"
    rows = store.query_config(rid, "workflows")
    assert any(x["kind"] == "workflow_no_transitions" for x in rows)
    stats = json.loads(r["stats_json"])
    assert stats["ai"]["skipped"] is True


def test_env_run_real_stages_duplicate_field_and_workflow(tmp_path, monkeypatch):
    """Full env audit through the REAL build_env_stages against a MockTransport
    Jira (cloud) seeded with BOTH a duplicate custom field and a transition-less
    workflow, with no Anthropic key. The run finishes with findings (both a
    medium duplicate_field and a high workflow_no_transitions), ai.skipped is
    true, and the verdict is CRITICAL (the high-severity workflow drives it) —
    consistent with the seeded data. A write to the source fails the test."""
    import webapp.env_stages as es

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})

    def handler(req):
        p = str(req.url.path)
        if req.method != "GET":
            raise AssertionError(
                f"env audit wrote to the SOURCE: {req.method} {p}")
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me",
                                             "emailAddress": "a@b.c",
                                             "displayName": "Igor"})
        if p == "/rest/api/3/project/search":
            return httpx.Response(200, json={
                "values": [{"key": "ACME", "name": "Acme", "id": "1"}],
                "isLast": True})
        if p.endswith("/workflow/search"):
            # one workflow with statuses but no transitions -> high finding
            return httpx.Response(200, json={"values": [
                {"id": {"name": "WF"}, "statuses": [{"name": "Open"}],
                 "transitions": []}], "isLast": True})
        if p.endswith("/field"):
            # two custom fields with the IDENTICAL name ("Severity") -> a real
            # duplicate_field. (A migration-suffix twin would instead surface as
            # migration_artifact and suppress the duplicate_field overlap, so an
            # exact duplicate is used here to exercise the duplicate_field path.)
            return httpx.Response(200, json=[
                {"id": "customfield_1", "name": "Severity", "custom": True,
                 "schema": {"custom": "...:select"}},
                {"id": "customfield_2", "name": "Severity",
                 "custom": True, "schema": {"custom": "...:select"}}])
        if p.endswith("/status"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"values": [], "isLast": True})

    def fake_clients(store_, mid_, http=None, require_both=True):
        assert require_both is False
        conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, None, get_connector("jira")
    monkeypatch.setattr(es, "build_clients", fake_clients)
    monkeypatch.setattr(es, "ai_provider", lambda store_: None)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=es.build_env_stages())
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert r["verdict"] == "CRITICAL"

    wf_rows = store.query_config(rid, "workflows")
    assert any(x["kind"] == "workflow_no_transitions" for x in wf_rows)
    cf_rows = store.query_config(rid, "custom_fields")
    dup = [x for x in cf_rows if x["kind"] == "duplicate_field"]
    assert dup and dup[0]["name"] == "Severity"

    stats = json.loads(r["stats_json"])
    assert stats["ai"]["skipped"] is True
    assert stats["high"] == 1
    assert stats["by_kind"]["workflow_no_transitions"] == 1
    assert stats["by_kind"]["duplicate_field"] == 1


def test_env_run_persists_ai_findings_end_to_end(tmp_path):
    """An env run whose analysis stage runs the real analyze() against a fake
    provider returning ai_findings must persist those ai_findings in stats.ai —
    proving the AI second-auditor output round-trips through _finalize_env."""
    from auditor.envaudit.analysis import analyze

    class _FakeProvider:
        def complete(self, system, user_content, *, model=None, effort="medium"):
            return {"text": (
                '{"health_score": 77, "grade": "B", "summary": "ok", '
                '"themes": [], "top_risks": [], "quick_wins": [], '
                '"ai_findings": [{"title": "Mixed key casing", '
                '"area": "projects", "severity": "low", '
                '"observation": "Some project keys are lowercase.", '
                '"recommendation": "Standardise project key casing."}]}'),
                "error": None, "refused": False, "model": "albert-heavy"}

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    env_stages = {
        "verify": lambda ctx: None, "scope": lambda ctx: None,
        "gather": lambda ctx: ctx.update(snapshot={"deployment": "cloud",
                                                    "projects": ["ACME"],
                                                    "areas": {}}),
        "checks": lambda ctx: ctx.update(env_findings=[]),
        "analysis": lambda ctx: ctx.update(
            ai=analyze(ctx["snapshot"], ctx.get("env_findings", []),
                       _FakeProvider(), product="jira")),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=env_stages)
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done"
    stats = json.loads(r["stats_json"])
    ai = stats["ai"]
    assert ai.get("skipped") is not True
    assert ai["health_score"] == 77
    fnd = ai["ai_findings"]
    assert isinstance(fnd, list) and len(fnd) == 1
    assert fnd[0]["area"] == "projects"
    assert fnd[0]["severity"] == "low"
    assert set(fnd[0].keys()) >= {"title", "area", "severity",
                                  "observation", "recommendation"}


def test_env_run_with_openai_provider_produces_ai_assessment(tmp_path, monkeypatch):
    """An env audit with an OpenAI-compatible provider configured (fake client)
    runs through the real env stages and produces an AI assessment — proving the
    provider abstraction is wired end-to-end and is not Anthropic-only. No live
    network call: the openai client is faked. NO real base_url/api_key are used."""
    import webapp.env_stages as es
    import webapp.ai_provider as ap
    from webapp.ai_provider import set_provider_choice, save_openai_config

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})

    # Configure the OpenAI-compatible provider with placeholder values only.
    set_provider_choice(store, "openai")
    save_openai_config(store, "https://example.test/v1", "albert-heavy",
                       "sk-test-xxx")

    def handler(req):
        p = str(req.url.path)
        if req.method != "GET":
            raise AssertionError(
                f"env audit wrote to the SOURCE: {req.method} {p}")
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me",
                                             "emailAddress": "a@b.c",
                                             "displayName": "Igor"})
        if p == "/rest/api/3/project/search":
            return httpx.Response(200, json={
                "values": [{"key": "ACME", "name": "Acme", "id": "1"}],
                "isLast": True})
        if p.endswith("/workflow/search"):
            return httpx.Response(200, json={"values": [
                {"id": {"name": "WF"}, "statuses": [{"name": "Open"}],
                 "transitions": [{"name": "Go"}]}], "isLast": True})
        if p.endswith("/field"):
            return httpx.Response(200, json=[])
        if p.endswith("/status"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"values": [], "isLast": True})

    def fake_clients(store_, mid_, http=None, require_both=True):
        assert require_both is False
        conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, None, get_connector("jira")
    monkeypatch.setattr(es, "build_clients", fake_clients)

    # Fake openai module: OpenAI(base_url, api_key) + chat.completions.create.
    captured = {}

    class _OAMsg:
        content = ('{"health_score": 91, "grade": "A", "summary": "ok", '
                   '"themes": [], "top_risks": [], "quick_wins": []}')

    class _OAChoice:
        message = _OAMsg()

    class _OAResp:
        choices = [_OAChoice()]
        model = "albert-heavy"

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            return _OAResp()

    class _Chat:
        completions = _Completions()

    class _FakeOpenAIModule:
        class OpenAI:
            def __init__(self, *, base_url, api_key, **kw):
                captured["base_url"] = base_url
                captured["api_key"] = api_key
                captured.update(kw)
            chat = _Chat()

    monkeypatch.setattr(ap, "_import_openai", lambda: _FakeOpenAIModule)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=es.build_env_stages())
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done"

    stats = json.loads(r["stats_json"])
    ai = stats["ai"]
    assert ai.get("skipped") is not True
    assert ai["health_score"] == 91 and ai["grade"] == "A"
    assert ai["model"] == "albert-heavy"
    # The fake OpenAI client was actually used with the placeholder config.
    assert captured["base_url"] == "https://example.test/v1"
    assert captured["model"] == "albert-heavy"
    assert captured["user"] == "igor"


def test_env_run_openai_package_missing_degrades_not_crashes(tmp_path, monkeypatch):
    """Regression: the OpenAI provider is selected + configured but the optional
    `openai` package is NOT installed. Building the provider raises ImportError
    INSIDE the analysis stage. The audit must still COMPLETE (deterministic
    findings + verdict stand) with the AI step degraded to skipped + a visible
    reason — it must NOT fail the whole run (the bug the user reported)."""
    import webapp.env_stages as es
    import webapp.ai_provider as ap
    from webapp.ai_provider import set_provider_choice, save_openai_config

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("env", audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    set_provider_choice(store, "openai")
    save_openai_config(store, "https://example.test/v1", "albert-heavy",
                       "sk-test-xxx")

    def handler(req):
        p = str(req.url.path)
        if req.method != "GET":
            raise AssertionError(f"env audit wrote to SOURCE: {req.method} {p}")
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me",
                                             "emailAddress": "a@b.c",
                                             "displayName": "Igor"})
        if p == "/rest/api/3/project/search":
            return httpx.Response(200, json={"values": [
                {"key": "ACME", "name": "Acme", "id": "1"}], "isLast": True})
        return httpx.Response(200, json={"values": [], "isLast": True})

    def fake_clients(store_, mid_, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, None, get_connector("jira")
    monkeypatch.setattr(es, "build_clients", fake_clients)

    # Simulate the optional package being absent: the lazy import raises.
    def _no_openai():
        raise ImportError("The 'openai' package is required ... pip install openai")
    monkeypatch.setattr(ap, "_import_openai", _no_openai)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=es.build_env_stages())
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)

    # The run COMPLETED (did not fail) and the AI step degraded gracefully.
    assert r["status"] == "done"
    stats = json.loads(r["stats_json"])
    assert stats["ai"].get("skipped") is True
    assert "openai" in (stats["ai"].get("error") or "").lower()
    # The run log carries the actionable reason.
    msgs = " ".join(e["message"] for e in store.get_events(rid))
    assert "AI analysis skipped" in msgs


def test_env_run_confluence_end_to_end(tmp_path, monkeypatch):
    """A full CONFLUENCE environment audit through the REAL build_env_stages
    against a MockTransport Confluence Cloud site. The migration row is
    product='confluence', audit_type='environment'. The run flows
    gather_confluence -> run_checks_confluence -> annotate_fixes -> finalize:

      - a current global space with zero pages          -> empty_space (low)
      - a current global space missing its homepage     -> space_no_homepage (med)
      - a space with no admin grant                     -> space_no_admin (high)

    The high finding drives a CRITICAL verdict. Findings persist with a fix tier
    + category, AI is skipped (no provider), and ZERO writes hit the source (a
    non-GET request fails the test loudly)."""
    import webapp.env_stages as es

    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("conf-env", product="confluence",
                                 audit_type="environment")
    store.save_connection(mid, "source", "pat", "https://c.atlassian.net",
                          {"token": "x", "email": "a@b.c"})

    # Two GLOBAL current spaces: ENG (0 pages, has homepage) and OPS (5 pages,
    # NO homepage); plus one personal space (counted only).
    _SPACES = [
        {"key": "ENG", "name": "Engineering", "id": "100", "type": "global",
         "status": "current", "homepageId": "h1"},
        {"key": "OPS", "name": "Operations", "id": "200", "type": "global",
         "status": "current"},  # no homepageId -> has_homepage False
        {"key": "~personal", "name": "Personal", "id": "300",
         "type": "personal", "status": "current", "homepageId": "h3"},
    ]
    # Per-space permissions: ENG has an admin grant; OPS has NONE (space_no_admin).
    _PERMS = {
        "100": [{"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "administer", "targetType": "space"}},
                {"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "read", "targetType": "space"}}],
        "200": [{"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "read", "targetType": "space"}}],
    }

    def _cql_count(cql):
        # ENG has 0 pages, OPS has 5; instance-wide pages_total small.
        if cql.startswith("macro="):
            return 0   # no risky macros used (keep the controlled finding set)
        if 'space="ENG"' in cql:
            return 0
        if 'space="OPS"' in cql:
            return 5
        return 5  # content_quality probes (small instance)

    def handler(req):
        p = str(req.url.path)
        if req.method != "GET":
            raise AssertionError(
                f"env audit wrote to the SOURCE: {req.method} {p}")
        # verify
        if p == "/wiki/rest/api/user/current":
            return httpx.Response(200, json={"displayName": "Igor",
                                             "email": "a@b.c",
                                             "accountId": "me"})
        # spaces (list_containers + spaces_detailed) — single page, no next.
        if p == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": _SPACES, "_links": {}})
        # per-space permissions
        if p.startswith("/wiki/api/v2/spaces/") and p.endswith("/permissions"):
            sid = p.split("/")[-2]
            return httpx.Response(200, json={"results": _PERMS.get(sid, []),
                                             "_links": {}})
        # CQL search (count envelope) — page counts + content_quality.
        if p == "/wiki/rest/api/search":
            cql = req.url.params.get("cql", "")
            return httpx.Response(200, json={"totalSize": _cql_count(cql)})
        # groups
        if p == "/wiki/rest/api/group":
            return httpx.Response(200, json={"results": [], "_links": {}})
        # templates / labels
        if p in ("/wiki/rest/api/template/page",
                 "/wiki/rest/api/template/blueprint",
                 "/wiki/rest/api/label"):
            return httpx.Response(200, json={"results": [], "_links": {}})
        return httpx.Response(200, json={"results": [], "_links": {}})

    def fake_clients(store_, mid_, http=None, require_both=True):
        assert require_both is False
        conn = Connection(auth_type="pat", site_url="https://c.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = ConfluenceClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, None, get_connector("confluence")
    monkeypatch.setattr(es, "build_clients", fake_clients)
    monkeypatch.setattr(es, "ai_provider", lambda store_: None)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       env_stages=es.build_env_stages())
    rid = engine.start(mid, {}, kind="env_audit")
    r = _wait(store, rid)
    assert r["status"] == "done"
    # The high-severity space_no_admin drives a CRITICAL verdict.
    assert r["verdict"] == "CRITICAL"

    # Findings persisted under their Confluence areas with kind + fix + category.
    space_rows = store.query_config(rid, "spaces")
    kinds = {x["kind"] for x in space_rows}
    assert "empty_space" in kinds
    assert "space_no_homepage" in kinds
    perm_rows = store.query_config(rid, "space_permissions")
    assert any(x["kind"] == "space_no_admin" for x in perm_rows)

    # Each persisted finding carries the folded fix tier + category in detail.
    detail = json.loads(space_rows[0]["detail_json"])
    assert "fix" in detail and "tier" in detail["fix"]
    assert "category" in detail

    stats = json.loads(r["stats_json"])
    assert stats["ai"]["skipped"] is True
    assert stats["high"] == 1
    assert stats["by_kind"]["space_no_admin"] == 1
    assert stats["by_kind"]["empty_space"] == 1
