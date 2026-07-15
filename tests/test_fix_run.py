import time
import httpx
from webapp.store import Store
from webapp.runs import RunEngine
from auditor.connectors import get_connector


def _wait(store, rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_fix_run_uses_fix_phases_and_fix_finalize(tmp_path):
    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    # The audit run defaults to status='running'; finish it so the active-run
    # guard lets the follow-on fix run start.
    store.update_run(audit, status="done")
    seen = []
    fix_stages = {
        "verify": lambda ctx: seen.append("verify"),
        "apply": lambda ctx: ctx.update(
            fix_log=[{"fix_id": "x", "ok": True}], touched_areas={"statuses"}),
        "reaudit": lambda ctx: ctx.update(
            closure={"closed": 1, "still_open": 0, "unchanged": 0, "detail": []}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fix_stages)
    rid = engine.start(mid, {"fix_ids": ["x"]}, kind="fix", source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert "verify" in seen
    stats = __import__("json").loads(r["stats_json"])
    assert stats["closed"] == 1
    # Inputs are fully controlled (closed=1, still_open=0, failed=0), so the
    # verdict is deterministic — pin it so a mis-route to the wrong branch fails.
    assert r["verdict"] == "FIXED_CLEAN"


def test_fix_finalize_invokes_elevation_undo(tmp_path):
    """A fix run may hold an elevated grant (it can run verify/apply under
    elevation). The fix finalize path must de-revoke it on the way out, exactly
    like the audit finalize path — otherwise the grant outlives the run."""
    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    store.update_run(audit, status="done")
    fix_stages = {
        "verify": lambda ctx: ctx.update(src="SRC", tgt="TGT"),
        "apply": lambda ctx: ctx.update(fix_log=[{"fix_id": "x", "ok": True}]),
        "reaudit": lambda ctx: ctx.update(
            closure={"closed": 1, "still_open": 0, "unchanged": 0, "detail": []}),
    }
    calls = []
    engine = RunEngine(
        store, str(tmp_path / "ws"), stages={}, fix_stages=fix_stages,
        elevation_undo=lambda src, tgt, mid, rid: calls.append((src, tgt, mid, rid)))
    rid = engine.start(mid, {"fix_ids": ["x"]}, kind="fix", source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert calls == [("SRC", "TGT", mid, rid)]


def test_fix_verdict_is_fix_failed_when_any_action_failed(tmp_path):
    """Edge case: a failed action alongside a partial close. If any action
    failed and not every finding closed, the verdict is FIX_FAILED — a partial
    close must not mask a failed action."""
    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    store.update_run(audit, status="done")
    fix_stages = {
        "verify": lambda ctx: None,
        "apply": lambda ctx: ctx.update(
            fix_log=[{"fix_id": "x", "ok": True}, {"fix_id": "y", "ok": False}]),
        "reaudit": lambda ctx: ctx.update(
            closure={"closed": 1, "still_open": 1, "unchanged": 0, "detail": []}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fix_stages)
    rid = engine.start(mid, {"fix_ids": ["x", "y"]}, kind="fix",
                       source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert r["verdict"] == "FIX_FAILED"


def test_fix_stages_create_status_end_to_end(tmp_path, monkeypatch):
    import webapp.fix_stages as fs
    from auditor.client import Connection, JiraClient

    # one missing-status finding with a payload, persisted on the audit run
    store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
    mid = store.create_migration("m")
    store.save_connection(mid, "target", "pat", "https://t.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    audit = store.create_run(mid, {})
    store.insert_findings_config(audit, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}, "fix_payload": {"name": "Triage", "category": "TODO"}}])

    posted = {"created": False}

    def handler(req):
        p = str(req.url.path)
        m = req.method
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me"})
        if p == "/rest/api/3/status" and m == "GET":
            return httpx.Response(200,
                json=[{"name": "Triage"}] if posted["created"] else [])
        if p == "/rest/api/3/statuses" and m == "POST":
            posted["created"] = True
            return httpx.Response(200, json=[{"id": "10010", "name": "Triage"}])
        return httpx.Response(404, json={})

    def src_handler(req):
        # The source is read-only during a fix run: any write to it is a
        # target-only-invariant violation that must fail the test loudly.
        if req.method != "GET":
            raise AssertionError(
                f"fix run wrote to the SOURCE: {req.method} {req.url.path}")
        return httpx.Response(404, json={})

    def fake_clients(store_, mid_, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")

        def jc(h):
            return JiraClient(conn, http=httpx.Client(
                transport=httpx.MockTransport(h)), sleeper=lambda s: None)
        return jc(src_handler), jc(handler), get_connector("jira")
    monkeypatch.setattr(fs, "build_clients", fake_clients)

    ctx = {"store": store, "run_id": store.create_run(
                mid, {"fix_ids": ["jira.status.create"]}, kind="fix",
                source_run_id=audit),
           "migration_id": mid, "params": {"fix_ids": ["jira.status.create"]},
           "source_run_id": audit, "workspace": str(tmp_path)}
    fs.fix_verify(ctx); fs.fix_apply(ctx); fs.fix_reaudit(ctx)
    assert ctx["fix_log"][0]["ok"] and ctx["fix_log"][0]["created_id"] == "10010"
    assert ctx["closure"]["closed"] == 1


def test_nothing_applied_verdict_when_all_skipped(tmp_path):
    """I10: when every selected finding has no fix_payload, plan.skipped is
    non-empty and plan.actions is empty. _finalize_fix must emit NOTHING_APPLIED,
    not FIXED_CLEAN."""
    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    store.update_run(audit, status="done")
    fix_stages = {
        "verify": lambda ctx: None,
        # apply produces no actions and non-empty skipped list (payload-less run)
        "apply": lambda ctx: ctx.update(
            fix_log=[],
            touched_areas=set(),
            fix_skipped=2),
        "reaudit": lambda ctx: ctx.update(
            closure={"closed": 0, "still_open": 0, "unchanged": 0, "detail": []}),
    }
    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fix_stages)
    rid = engine.start(mid, {"fix_ids": ["jira.status.create"]}, kind="fix",
                       source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    assert r["verdict"] == "NOTHING_APPLIED"
    import json
    stats = json.loads(r["stats_json"])
    headline = " ".join(stats["headlines"])
    assert "re-run" in headline.lower() or "nothing" in headline.lower() or "capture" in headline.lower()


def test_server_side_confirm_refusal(tmp_path):
    """I3: requires_confirm is enforced in fix_apply/build_plan even when the
    HTTP layer is bypassed. If a selected fix has requires_confirm and the param
    confirm_workflow is falsy, those actions must be dropped (fix_skipped > 0)
    and a refusal logged."""
    from unittest.mock import patch
    from auditor.remediation.registry import Fix

    store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
    mid = store.create_migration("m")
    store.save_connection(mid, "target", "pat", "https://t.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    audit = store.create_run(mid, {})
    store.update_run(audit, status="done")
    # Insert a finding for the confirm-required fix
    store.insert_findings_config(audit, [
        {"area": "statuses", "name": "Open", "kind": "missing_in_tgt",
         "detail": {}, "fix_payload": {"name": "Open", "category": "TODO"}}])

    import webapp.fix_stages as fs
    import httpx
    from auditor.client import Connection, JiraClient

    def fake_clients(store_, mid_, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")

        def handler(req):
            if str(req.url.path) == "/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "me"})
            return httpx.Response(404, json={})

        cl = JiraClient(conn,
                        http=httpx.Client(transport=httpx.MockTransport(handler)),
                        sleeper=lambda s: None)
        from auditor.connectors import get_connector
        return cl, cl, get_connector("jira")

    # Patch a fix that requires_confirm into the registry temporarily
    confirm_fix = Fix(
        "jira.status.create_confirm_test", "jira", "statuses",
        ("missing_in_tgt",), "create", "low",
        "Confirm-required fix", "Needs explicit consent.",
        requires_confirm=True)

    import auditor.remediation.registry as reg
    import auditor.remediation.plan as plan_mod

    orig_fixes = reg.FIXES[:]
    orig_by_id = dict(reg._BY_ID)
    try:
        reg.FIXES.append(confirm_fix)
        reg._BY_ID[confirm_fix.fix_id] = confirm_fix

        ctx = {
            "store": store,
            "run_id": store.create_run(mid, {"fix_ids": [confirm_fix.fix_id],
                                             "confirm_workflow": False},
                                       kind="fix", source_run_id=audit),
            "migration_id": mid,
            "params": {"fix_ids": [confirm_fix.fix_id], "confirm_workflow": False},
            "workspace": str(tmp_path),
        }
        with patch.object(fs, "build_clients", fake_clients):
            fs.fix_verify(ctx)
            fs.fix_apply(ctx)

        # No actions should have been applied for the confirm-required fix
        assert ctx.get("fix_log") == [] or all(
            a.get("fix_id") != confirm_fix.fix_id for a in ctx.get("fix_log", []))
        # fix_skipped must be set to reflect the refused actions
        assert ctx.get("fix_skipped", 0) > 0
    finally:
        reg.FIXES[:] = orig_fixes
        reg._BY_ID.clear()
        reg._BY_ID.update(orig_by_id)


def test_full_fix_run_closes_a_missing_field(tmp_path, monkeypatch):
    """End-to-end: a missing custom-field finding is created and confirmed closed
    by reaudit — the MockTransport dispatches by both path AND method so the POST
    /field marks the field as created, and the subsequent GET /field precheck
    (used by reaudit) returns it, flipping the closure to closed=1."""
    import webapp.fix_stages as fs
    from auditor.client import Connection, JiraClient

    store = Store(str(tmp_path / "f.db"), str(tmp_path / "f.key"))
    mid = store.create_migration("m")
    store.save_connection(mid, "target", "pat", "https://t.atlassian.net",
                          {"token": "x", "email": "a@b.c"})
    audit = store.create_run(mid, {})
    # Mark the audit run done so the active-run guard lets the fix run start.
    store.update_run(audit, status="done")
    store.insert_findings_config(audit, [
        {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
         "detail": {"type": "select"},
         "fix_payload": {"type": "select", "field_id": "customfield_1",
                         "contexts": [{"name": "Default", "options": ["High"]}]}}])

    # Single handler dispatches by BOTH path and method — the two /field
    # branches are separated by method so neither is dead code.
    state = {"made": False}

    def handler(req):
        p = str(req.url.path)
        m = req.method
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me"})
        # GET /field — precheck (apply) and reaudit both call this; return the
        # field only after the POST has run so the reaudit flip is observable.
        if p == "/rest/api/3/field" and m == "GET":
            items = [{"name": "Severity", "custom": True}] if state["made"] else []
            return httpx.Response(200, json=items)
        # POST /field — creates the field and marks it as existing.
        if p == "/rest/api/3/field" and m == "POST":
            state["made"] = True
            return httpx.Response(201, json={"id": "customfield_9"})
        # POST /context — returns a context id used for option creation.
        if p.endswith("/context") and m == "POST":
            return httpx.Response(201, json={"id": "ctx1"})
        # POST /option — options added to the new context.
        if "/context/" in p and p.endswith("/option") and m == "POST":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    def fake_clients(s, m, http=None, require_both=True):
        conn = Connection(auth_type="pat", site_url="https://t.atlassian.net",
                          deployment="cloud", email="a@b.c", api_token="x")
        cl = JiraClient(conn, http=httpx.Client(
            transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
        return cl, cl, get_connector("jira")
    monkeypatch.setattr(fs, "build_clients", fake_clients)

    engine = RunEngine(store, str(tmp_path / "ws"), stages={},
                       fix_stages=fs.build_fix_stages())
    rid = engine.start(mid, {"fix_ids": ["jira.custom_field.create"]},
                       kind="fix", source_run_id=audit)
    r = _wait(store, rid)
    assert r["status"] == "done"
    acts = store.get_fix_actions(rid)
    assert any(a["created_id"] == "customfield_9" for a in acts)
    import json
    stats = json.loads(r["stats_json"])
    assert stats["closed"] == 1, "reaudit must flip the finding to closed"
    assert r["verdict"] == "FIXED_CLEAN"
