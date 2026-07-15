"""Tests for the env-fix flow (Task 10).

Covers:
  - app-tier delete (unused scheme + empty group): apply issues DELETE, logs ok,
    closure reports closed after re-read shows object gone.
  - identity guard: mismatched expected_api_base raises/aborts without any write.
  - server-side tier re-derivation: a human/unfixable finding selected by a
    malicious client is NOT applied.
  - idempotency: object already gone -> logged no-op, verdict not FAILED.
  - the env-fix screen renders app-tier as checkboxes and human/unfixable as
    read-only.
  - the AUDIT itself still performs zero writes (regression).
  - end-to-end: env_fix run through RunEngine produces correct verdict.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from auditor.client import Connection, JiraClient
from auditor.connectors import get_connector
from auditor.envaudit.apply import apply_env_fixes
from webapp.config import Config
from webapp.main import create_app
from webapp.runs import RunEngine
from webapp.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn(site="https://acme.atlassian.net"):
    return Connection(auth_type="pat", site_url=site,
                      deployment="cloud", email="a@b.c", api_token="x")


def _client(handler, site="https://acme.atlassian.net"):
    conn = _conn(site)
    return JiraClient(conn, http=httpx.Client(
        transport=httpx.MockTransport(handler)), sleeper=lambda s: None)


def _wait(store, rid, t=5):
    end = time.time() + t
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def _app(tmp_path):
    cfg = Config(data_dir=str(tmp_path), bind_host="127.0.0.1", bind_port=8484,
                 public_base_url="http://localhost:8484", secret_key=None)
    return create_app(cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def _env_finding(kind, name, area="schemes"):
    """Build a finding dict as stored in findings_config (with detail nested)."""
    from auditor.envaudit.fixes import _FIXES, category_for
    import copy
    fix = copy.copy(_FIXES.get(kind, {
        "tier": "human", "tier_label": "Fixable by a human",
        "title": kind, "detail": "n/a", "api_hint": None,
        "risk": "low", "reversible": True, "caveat": None,
    }))
    if name and name not in fix.get("title", ""):
        fix["title"] = f"{fix['title']}: {name}"
    return {
        "area": area,
        "name": name,
        "kind": kind,
        "severity": "low",
        "detail": {
            "fix": fix,
            "category": category_for(kind),
            "severity": "low",
        },
    }


# ===========================================================================
# 1. apply_env_fixes — unit-level tests
# ===========================================================================

class TestApplyEnvFixes:

    def test_scheme_unused_delete_issues_delete_and_logs_ok(self):
        """A scheme_unused finding causes a DELETE call and logs ok=True."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "me"})
            # GET to resolve the scheme id by name
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "101", "name": "Old Scheme"}]})
            # DELETE the scheme
            if p == "/rest/api/3/workflowscheme/101" and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            # Re-check after delete: scheme gone
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                return httpx.Response(200, json={"values": []})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Old Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert deleted, "DELETE must have been called"
        assert any(r["ok"] for r in log), f"expected ok=True in log: {log}"
        assert any("DELETE" in r.get("method", "") for r in log)

    def test_empty_group_delete_issues_delete_and_logs_ok(self):
        """An empty_group finding causes a DELETE call and logs ok=True."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            q = dict(req.url.params)
            # member-count re-verify: still empty
            if req.method == "GET" and p == "/rest/api/3/group/member":
                return httpx.Response(200, json={"total": 0, "values": []})
            # permission-scheme reference check: no scheme grants the group
            if req.method == "GET" and p == "/rest/api/3/permissionscheme":
                return httpx.Response(200, json={"permissionSchemes": []})
            if req.method == "GET" and "/group/bulk" in p:
                return httpx.Response(200, json={"values": [
                    {"name": "old-group", "groupId": "gid-42"}]})
            if req.method == "DELETE" and "/group" in p and q.get("groupId") == "gid-42":
                deleted.append(True)
                return httpx.Response(200, json={})
            # Re-check: group gone
            if req.method == "GET" and "/group/bulk" in p:
                return httpx.Response(200, json={"values": []})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("empty_group", "old-group", area="groups")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert deleted, "DELETE for group must have been called"
        assert any(r["ok"] for r in log), f"expected ok=True in log: {log}"

    def test_empty_group_with_permission_scheme_grant_is_not_deleted(self):
        """REFERENCE CHECK (no-bias review): a memberless group that still holds a
        permission-scheme grant must NOT be deleted — that would silently revoke
        access. Reported still_open, no DELETE issued."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if req.method == "GET" and p == "/rest/api/3/group/member":
                return httpx.Response(200, json={"total": 0, "values": []})
            if req.method == "GET" and "/group/bulk" in p:
                return httpx.Response(200, json={"values": [
                    {"name": "old-group", "groupId": "gid-42"}]})
            if req.method == "GET" and p == "/rest/api/3/permissionscheme":
                return httpx.Response(200, json={"permissionSchemes": [
                    {"id": 1, "permissions": [
                        {"permission": "BROWSE_PROJECTS",
                         "holder": {"type": "group", "parameter": "old-group",
                                    "value": "gid-42"}}]}]})
            if req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        log = []
        closed, still_open = apply_env_fixes(
            cl, [_env_finding("empty_group", "old-group", area="groups")],
            log.append, expected_api_base="https://acme.atlassian.net")
        assert not deleted, "a group with a scheme grant must NOT be deleted"
        assert still_open == 1
        assert any("scheme" in (r.get("error") or "") for r in log)

    def test_empty_group_not_deleted_when_grants_did_not_expand(self):
        """Drift defence (no-bias review): schemes exist but carry NO permissions
        (expand failed/changed) -> unverifiable, so the delete is skipped rather
        than treating it as 'no grant'."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if req.method == "GET" and p == "/rest/api/3/group/member":
                return httpx.Response(200, json={"total": 0, "values": []})
            if req.method == "GET" and "/group/bulk" in p:
                return httpx.Response(200, json={"values": [
                    {"name": "old-group", "groupId": "gid-42"}]})
            if req.method == "GET" and p == "/rest/api/3/permissionscheme":
                return httpx.Response(200, json={"permissionSchemes": [
                    {"id": 1}]})        # scheme present, permissions NOT expanded
            if req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        log = []
        apply_env_fixes(
            cl, [_env_finding("empty_group", "old-group", area="groups")],
            log.append, expected_api_base="https://acme.atlassian.net")
        assert not deleted, "unverifiable scheme grants must NOT green-light delete"

    def test_identity_guard_raises_before_any_write(self):
        """A mismatched expected_api_base must raise ValueError before any HTTP write."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler, site="https://acme.atlassian.net")
        finding = _env_finding("scheme_unused", "Old Scheme")

        with pytest.raises(ValueError, match="api_base"):
            apply_env_fixes(
                cl, [finding], lambda r: None,
                expected_api_base="https://OTHER.atlassian.net")

        assert not writes, "No writes must occur when identity guard fails"

    def test_human_finding_is_not_applied(self):
        """A human-tier finding must be skipped even if selected by the client."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler)
        finding = _env_finding("duplicate_field", "Severity")  # human tier
        log = []
        apply_env_fixes(cl, [finding], log.append,
                        expected_api_base="https://acme.atlassian.net")

        assert not writes, "human-tier findings must NOT generate writes"
        # Log may record a skip entry but must not have ok=True for a write
        assert not any(r.get("method") in ("DELETE", "POST", "PUT")
                       for r in log)

    def test_unfixable_finding_is_not_applied(self):
        """An unfixable-tier finding must be skipped even if selected."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler)
        finding = _env_finding("migration_artifact", "WF (migrated)")
        log = []
        apply_env_fixes(cl, [finding], log.append,
                        expected_api_base="https://acme.atlassian.net")

        assert not writes
        assert not any(r.get("method") in ("DELETE", "POST", "PUT")
                       for r in log)

    def test_idempotent_scheme_already_absent(self):
        """If the scheme is already gone before apply, the result is a logged
        no-op and the verdict counts it as closed (not failed)."""
        def handler(req):
            p = str(req.url.path)
            # Scheme not present at lookup
            if "/workflowscheme" in p and req.method == "GET":
                return httpx.Response(200, json={"values": []})
            if req.method == "DELETE":
                raise AssertionError("DELETE must not be called when already absent")
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Old Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        # No DELETE happened; must be logged as no-op; closed counts as 1 (idempotent)
        assert not any(r.get("method") == "DELETE" for r in log)
        assert closed == 1  # already gone = already closed

    def test_idempotent_group_already_absent(self):
        """If the group is already gone, log no-op and count as closed."""
        def handler(req):
            if "/group/bulk" in str(req.url.path) and req.method == "GET":
                return httpx.Response(200, json={"values": []})
            if req.method == "DELETE":
                raise AssertionError("DELETE must not be called when already absent")
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("empty_group", "old-group", area="groups")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert not any(r.get("method") == "DELETE" for r in log)
        assert closed == 1

    def test_dry_run_runs_guards_but_issues_no_delete(self):
        """dry_run=True runs the full guard chain (resolve + TOCTOU re-verify)
        but stops at the DELETE: it logs a WOULD-DELETE record (ok=True) and
        counts the finding as a would-close — without a single write."""
        calls = {"delete": 0}

        def handler(req):
            p = str(req.url.path)
            if "/workflowscheme" in p and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "101", "name": "Old Scheme"}]})
            if "/workflowscheme/101" in p and req.method == "DELETE":
                calls["delete"] += 1
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Old Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net", dry_run=True)

        assert calls["delete"] == 0, "dry run must NOT issue any DELETE"
        assert not any(r.get("method") == "DELETE" for r in log)
        would = [r for r in log if r.get("method") == "WOULD-DELETE"]
        assert would and would[0]["ok"], f"expected a WOULD-DELETE record: {log}"
        assert closed == 1 and still_open == 0  # would-close counts as closed

    def test_dry_run_still_skips_an_object_that_fails_its_guard(self):
        """A preview must not claim it would delete an object the guards reject:
        an in-use scheme is skipped in dry_run exactly as in a real apply."""
        def handler(req):
            p = str(req.url.path)
            if "/workflowscheme" in p and req.method == "GET":
                # resolve: present, AND the inline projectIds shows live usage,
                # so the TOCTOU re-verify must block the (would-)delete.
                return httpx.Response(200, json={"values": [
                    {"id": "55", "name": "Busy Scheme",
                     "projectIds": ["10000", "10001"]}]})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Busy Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net", dry_run=True)

        assert not any(r.get("method") in ("DELETE", "WOULD-DELETE") for r in log)
        assert closed == 0 and still_open == 1

    def test_closure_counts_gone_object_as_closed(self):
        """After deletion, a re-read showing the scheme is gone increments closed."""
        calls = {"get": 0, "delete": 0}

        def handler(req):
            p = str(req.url.path)
            if "/workflowscheme" in p and req.method == "GET":
                calls["get"] += 1
                # First GET: scheme present. Second GET (closure check): gone.
                if calls["get"] == 1:
                    return httpx.Response(200, json={"values": [
                        {"id": "77", "name": "Scheme X"}]})
                return httpx.Response(200, json={"values": []})
            if "/workflowscheme/77" in p and req.method == "DELETE":
                calls["delete"] += 1
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Scheme X")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert calls["delete"] == 1
        assert closed == 1
        assert still_open == 0

    def test_all_log_records_have_required_shape(self):
        """Every log record must carry method, path, status, ok, object_name."""
        def handler(req):
            if "/workflowscheme" in str(req.url.path) and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "5", "name": "S"}]})
            if req.method == "DELETE":
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "S")
        log = []
        apply_env_fixes(cl, [finding], log.append,
                        expected_api_base="https://acme.atlassian.net")

        for r in log:
            assert "method" in r, f"missing method: {r}"
            assert "path" in r, f"missing path: {r}"
            assert "status" in r, f"missing status: {r}"
            assert "ok" in r, f"missing ok: {r}"
            assert "object_name" in r, f"missing object_name: {r}"

    def test_no_writes_when_no_app_tier_findings(self):
        """With no app-tier findings, apply makes no writes."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler)
        findings = [
            _env_finding("duplicate_field", "F"),
            _env_finding("workflow_no_transitions", "W"),
        ]
        log = []
        apply_env_fixes(cl, findings, log.append,
                        expected_api_base="https://acme.atlassian.net")

        assert not writes


# ===========================================================================
# 2. env-fix run through RunEngine (kind="env_fix")
# ===========================================================================

class TestEnvFixRun:

    def test_env_fix_run_uses_env_fix_phases(self, tmp_path):
        """A kind='env_fix' run dispatches to env_fix_stages and calls
        _finalize_fix for the FIXED_CLEAN / FIX_FAILED / etc. verdict."""
        store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
        mid = store.create_migration("env", audit_type="environment")
        audit = store.create_run(mid, {}, kind="env_audit")
        store.update_run(audit, status="done")

        seen = []
        env_fix_stages = {
            "verify": lambda ctx: seen.append("verify"),
            "apply": lambda ctx: ctx.update(
                fix_log=[{"fix_id": "x", "ok": True, "method": "DELETE",
                           "path": "/rest/api/3/workflowscheme/1",
                           "status": 204, "object_name": "Old",
                           "finding_ref": "r1", "created_id": None, "error": None}],
                fix_skipped=0),
            "reaudit": lambda ctx: ctx.update(
                closure={"closed": 1, "still_open": 0, "unchanged": 0,
                         "detail": []}),
        }
        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_fix_stages=env_fix_stages)
        rid = store.create_run(mid, {"finding_refs": ["r1"]},
                               kind="env_fix", source_run_id=audit)
        # Start directly via _execute to bypass the active-run guard
        # (audit run is already done, so start() would succeed)
        engine2 = RunEngine(store, str(tmp_path / "ws"),
                            env_fix_stages=env_fix_stages)
        # Use start() which manages the thread
        store.update_run(rid, status="running")  # pre-mark; engine start() creates new
        # Better: create a fresh run via engine.start
        store2 = Store(str(tmp_path / "d2.db"), str(tmp_path / "d2.key"))
        mid2 = store2.create_migration("env2", audit_type="environment")
        audit2 = store2.create_run(mid2, {}, kind="env_audit")
        store2.update_run(audit2, status="done")
        e2 = RunEngine(store2, str(tmp_path / "ws2"),
                       env_fix_stages=env_fix_stages)
        rid2 = e2.start(mid2, {"finding_refs": ["r1"]},
                        kind="env_fix", source_run_id=audit2)
        r = _wait(store2, rid2)
        assert r["status"] == "done"
        assert "verify" in seen
        assert r["verdict"] == "FIXED_CLEAN"

    def test_env_fix_run_nothing_applied_when_no_actions(self, tmp_path):
        """When no app-tier findings are selected, verdict is NOTHING_APPLIED."""
        store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
        mid = store.create_migration("env", audit_type="environment")
        audit = store.create_run(mid, {}, kind="env_audit")
        store.update_run(audit, status="done")

        env_fix_stages = {
            "verify": lambda ctx: None,
            "apply": lambda ctx: ctx.update(fix_log=[], fix_skipped=1),
            "reaudit": lambda ctx: ctx.update(
                closure={"closed": 0, "still_open": 0, "unchanged": 0,
                         "detail": []}),
        }
        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_fix_stages=env_fix_stages)
        rid = engine.start(mid, {}, kind="env_fix", source_run_id=audit)
        r = _wait(store, rid)
        assert r["status"] == "done"
        assert r["verdict"] == "NOTHING_APPLIED"

    def test_env_fix_run_fix_failed_when_action_fails(self, tmp_path):
        """When an action fails and not all closed, verdict is FIX_FAILED."""
        store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
        mid = store.create_migration("env", audit_type="environment")
        audit = store.create_run(mid, {}, kind="env_audit")
        store.update_run(audit, status="done")

        env_fix_stages = {
            "verify": lambda ctx: None,
            "apply": lambda ctx: ctx.update(
                fix_log=[{"fix_id": "e1", "ok": True}, {"fix_id": "e2", "ok": False}],
                fix_skipped=0),
            "reaudit": lambda ctx: ctx.update(
                closure={"closed": 1, "still_open": 1, "unchanged": 0, "detail": []}),
        }
        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_fix_stages=env_fix_stages)
        rid = engine.start(mid, {}, kind="env_fix", source_run_id=audit)
        r = _wait(store, rid)
        assert r["verdict"] == "FIX_FAILED"

    def test_env_fix_streamed_log_not_double_persisted(self, tmp_path):
        """When apply streams records durably (fix_log_streamed), _finalize_fix
        must NOT bulk-insert them again -> no duplicate fix_actions rows
        (review Bug 4: the streamed write-through is the single source)."""
        store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
        mid = store.create_migration("env", audit_type="environment")
        audit = store.create_run(mid, {}, kind="env_audit")
        store.update_run(audit, status="done")

        def streaming_apply(ctx):
            rec = {"fix_id": "scheme_unused", "ok": True, "method": "DELETE",
                   "object_name": "Old", "finding_ref": "r1",
                   "path": "/rest/api/3/workflowscheme/1", "status": 204,
                   "created_id": None, "error": None}
            store.append_fix_action(ctx["run_id"], rec)   # streamed during apply
            ctx.update(fix_log=[rec], fix_skipped=0, fix_log_streamed=True)

        env_fix_stages = {
            "verify": lambda ctx: None,
            "apply": streaming_apply,
            "reaudit": lambda ctx: ctx.update(
                closure={"closed": 1, "still_open": 0, "unchanged": 0,
                         "detail": []}),
        }
        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_fix_stages=env_fix_stages)
        rid = engine.start(mid, {"finding_refs": ["r1"]},
                           kind="env_fix", source_run_id=audit)
        r = _wait(store, rid)
        assert r["status"] == "done"
        acts = store.get_fix_actions(rid)
        assert len(acts) == 1            # streamed once, not doubled at finalize
        assert r["verdict"] == "FIXED_CLEAN"


# ===========================================================================
# 3. env-fix screen routes (GET / POST)
# ===========================================================================

class TestEnvFixScreen:

    def _setup(self, tmp_path):
        app = _app(tmp_path)
        store = app.state.store
        mid = store.create_migration("env", product="jira",
                                     audit_type="environment")
        store.save_connection(mid, "source", "pat",
                              "https://acme.atlassian.net",
                              {"token": "t", "email": "a@b.c"})
        audit = store.create_run(mid, {}, kind="env_audit")
        # Insert both app-tier and human-tier findings
        findings = [
            _env_finding("scheme_unused", "Old Scheme"),
            _env_finding("empty_group", "ghost-group", area="groups"),
            _env_finding("duplicate_field", "Severity"),
            _env_finding("migration_artifact", "WF (migrated)"),
        ]
        # Fold severity into detail (as _finalize_env does)
        for f in findings:
            f.setdefault("detail", {})["severity"] = f.get("severity")
        store.insert_findings_config(audit, findings)
        store.update_run(audit, status="done", verdict="NEEDS_ATTENTION",
                         stats={"health_score": 70, "grade": "C",
                                "findings": 4, "high": 0, "medium": 1,
                                "low": 3, "capability_gaps": 0,
                                "by_kind": {}, "headlines": [],
                                "ai": {"skipped": True}})
        return app, store, mid, audit

    def test_env_fix_screen_app_tier_rendered_as_checkboxes(self, tmp_path):
        """GET /runs/{id}/env-fix renders app-tier findings as checkboxes."""
        app, store, mid, audit = self._setup(tmp_path)
        c = TestClient(app)
        r = c.get(f"/runs/{audit}/env-fix")
        assert r.status_code == 200
        text = r.text
        # App-tier items must appear as checkbox inputs
        assert 'type="checkbox"' in text
        assert "Old Scheme" in text or "scheme_unused" in text.lower()

    def test_env_fix_screen_human_tier_rendered_as_readonly(self, tmp_path):
        """GET /runs/{id}/env-fix renders human/unfixable findings as read-only."""
        app, store, mid, audit = self._setup(tmp_path)
        c = TestClient(app)
        r = c.get(f"/runs/{audit}/env-fix")
        assert r.status_code == 200
        text = r.text
        # Severity should appear (human-tier findings listed)
        assert "Severity" in text or "duplicate_field" in text.lower()
        # Human-tier must NOT have a checkbox; re-use a marker class or text
        assert "human" in text.lower() or "guidance" in text.lower() or "read-only" in text.lower()

    def test_env_fix_screen_unfixable_rendered_as_readonly(self, tmp_path):
        """GET /runs/{id}/env-fix renders unfixable as read-only (re-migration)."""
        app, store, mid, audit = self._setup(tmp_path)
        c = TestClient(app)
        r = c.get(f"/runs/{audit}/env-fix")
        assert r.status_code == 200
        text = r.text
        assert "migrated" in text.lower() or "migration" in text.lower()

    def test_env_fix_post_starts_env_fix_run(self, tmp_path, monkeypatch):
        """POST /runs/{id}/env-fix with selected refs starts an env_fix run."""
        app, store, mid, audit = self._setup(tmp_path)
        started = {}
        monkeypatch.setattr(app.state.engine, "start",
                            lambda mid_, params, **kw:
                            (started.update(kw) or started.update({"mid": mid_}) or 99))
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix",
                   data={"finding_refs": "scheme_unused:Old Scheme",
                         "consent": "on"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert started.get("kind") == "env_fix"

    def test_env_fix_post_requires_selection(self, tmp_path):
        """POST /runs/{id}/env-fix with no selection redirects with error."""
        app, store, mid, audit = self._setup(tmp_path)
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix", data={}, follow_redirects=False)
        assert r.status_code == 303
        assert "error" in r.headers["location"]

    def test_env_fix_post_without_consent_starts_no_run(self, tmp_path,
                                                        monkeypatch):
        """H1 — consent is enforced server-side: a POST with selected refs but
        consent unchecked must start NO run (writes nothing) and redirect back
        with an error."""
        app, store, mid, audit = self._setup(tmp_path)
        started = {"called": False}
        monkeypatch.setattr(
            app.state.engine, "start",
            lambda *a, **k: started.update(called=True) or 99)
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix",
                   data={"finding_refs": "scheme_unused:Old Scheme"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert not started["called"], "no run may start without consent"
        assert "error" in r.headers["location"]

    def test_env_fix_post_with_consent_proceeds(self, tmp_path, monkeypatch):
        """H1 — consent=on lets the run start normally."""
        app, store, mid, audit = self._setup(tmp_path)
        started = {}
        monkeypatch.setattr(
            app.state.engine, "start",
            lambda mid_, params, **kw:
            (started.update(kw) or started.update({"mid": mid_}) or 99))
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix",
                   data={"finding_refs": "scheme_unused:Old Scheme",
                         "consent": "on"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert started.get("kind") == "env_fix"

    def test_preview_starts_a_run_without_consent(self, tmp_path, monkeypatch):
        """A dry_run preview issues no write, so it is exempt from the consent
        gate: a POST with dry_run=1 and NO consent must start a run, and the
        run's params must carry dry_run=True."""
        app, store, mid, audit = self._setup(tmp_path)
        started = {}
        monkeypatch.setattr(
            app.state.engine, "start",
            lambda mid_, params, **kw:
            (started.update(params=params) or started.update(kw) or 99))
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix",
                   data={"finding_refs": "scheme_unused:Old Scheme",
                         "dry_run": "1"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert "error" not in r.headers["location"]
        assert started.get("kind") == "env_fix"
        assert started["params"]["dry_run"] is True

    def test_live_apply_still_blocked_without_consent(self, tmp_path,
                                                      monkeypatch):
        """The preview exemption must NOT weaken the live-apply consent gate: a
        non-preview POST (no dry_run) without consent still starts no run."""
        app, store, mid, audit = self._setup(tmp_path)
        started = {"called": False}
        monkeypatch.setattr(
            app.state.engine, "start",
            lambda *a, **k: started.update(called=True) or 99)
        c = TestClient(app)
        r = c.post(f"/runs/{audit}/env-fix",
                   data={"finding_refs": "scheme_unused:Old Scheme"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert not started["called"]
        assert "error" in r.headers["location"]

    def test_env_fix_screen_not_shown_for_migration_run(self, tmp_path):
        """GET /runs/{id}/env-fix for a migration run redirects away."""
        app = _app(tmp_path)
        store = app.state.store
        mid = store.create_migration("mig")
        rid = store.create_run(mid, {}, kind="audit")
        store.update_run(rid, status="done")
        c = TestClient(app)
        r = c.get(f"/runs/{rid}/env-fix", follow_redirects=False)
        # Must redirect (not 200)
        assert r.status_code in (302, 303, 307, 308)


# ===========================================================================
# 4. analysis.html shows env-fix button for env_audit runs
# ===========================================================================

class TestAnalysisEnvFixButton:

    def test_analysis_page_shows_env_fix_button_for_env_audit(self, tmp_path):
        """GET /runs/{id}/analysis for an env_audit run shows an env-fix button."""
        app = _app(tmp_path)
        store = app.state.store
        mid = store.create_migration("env", audit_type="environment")
        rid = store.create_run(mid, {}, kind="env_audit")
        store.update_run(rid, status="done", verdict="NEEDS_ATTENTION",
                         stats={"health_score": 72, "grade": "B",
                                "findings": 3, "high": 0, "medium": 1,
                                "low": 2, "capability_gaps": 0,
                                "by_kind": {}, "headlines": [],
                                "ai": {"skipped": True}})
        c = TestClient(app)
        html = c.get(f"/runs/{rid}/analysis").text
        # Must have a link/button to /runs/{rid}/env-fix
        assert f"/runs/{rid}/env-fix" in html

    def test_analysis_page_migration_run_no_env_fix_button(self, tmp_path):
        """GET /runs/{id}/analysis for a migration audit run must NOT have env-fix."""
        app = _app(tmp_path)
        store = app.state.store
        mid = store.create_migration("mig")
        rid = store.create_run(mid, {}, kind="audit")
        store.update_run(rid, status="done", verdict="GAPS_FOUND")
        c = TestClient(app)
        html = c.get(f"/runs/{rid}/analysis").text
        assert f"/runs/{rid}/env-fix" not in html


# ===========================================================================
# 5. Audit regression: env audit still writes nothing
# ===========================================================================

class TestAuditWriteRegression:

    def test_env_audit_never_writes(self, tmp_path, monkeypatch):
        """An env_audit run (not env_fix) must issue zero MUTATING HTTP calls.

        The only non-GET call the audit may make is the read-only Cloud
        approximate-count endpoint (POST /rest/api/3/search/approximate-count),
        which the Section-3 issue_quality data-quality probes use to fetch a
        count. A count query reads nothing into the snapshot beyond an integer
        and mutates nothing, so it is explicitly allowed; any other non-GET call
        is a genuine write and fails the test."""
        import webapp.env_stages as es

        store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
        mid = store.create_migration("env", audit_type="environment")
        store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                              {"token": "x", "email": "a@b.c"})
        writes = []
        # Read-only non-GET endpoints the audit is permitted to call.
        _READONLY_POSTS = {"/rest/api/3/search/approximate-count"}

        def handler(req):
            if req.method != "GET" and \
                    str(req.url.path) not in _READONLY_POSTS:
                writes.append(f"{req.method} {req.url.path}")
            if str(req.url.path) == "/rest/api/3/search/approximate-count":
                return httpx.Response(200, json={"count": 0})
            if str(req.url.path) == "/rest/api/3/myself":
                return httpx.Response(200,
                    json={"accountId": "me", "emailAddress": "a@b.c",
                          "displayName": "Igor"})
            if str(req.url.path) == "/rest/api/3/project/search":
                return httpx.Response(200, json={"values": [], "isLast": True})
            if str(req.url.path).endswith("/field"):
                return httpx.Response(200, json=[])
            if str(req.url.path).endswith("/status"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"values": [], "isLast": True})

        def fake_clients(store_, mid_, http=None, require_both=True):
            assert require_both is False
            conn = Connection(auth_type="pat", site_url="https://s.atlassian.net",
                              deployment="cloud", email="a@b.c", api_token="x")
            cl = JiraClient(conn, http=httpx.Client(
                transport=httpx.MockTransport(handler)),
                sleeper=lambda s: None)
            return cl, None, get_connector("jira")

        monkeypatch.setattr(es, "build_clients", fake_clients)
        monkeypatch.setattr(es, "ai_provider", lambda store_: None)

        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_stages=es.build_env_stages())
        rid = engine.start(mid, {}, kind="env_audit")
        r = _wait(store, rid)
        assert r["status"] == "done"
        assert not writes, f"env_audit issued writes: {writes}"


# ===========================================================================
# 6. apply_env_fixes — unused_issue_type_scheme and unused_issue_type_screen_scheme
# ===========================================================================

class TestApplyEnvFixesSchemeVariants:

    def test_unused_issue_type_scheme_delete(self):
        """An unused_issue_type_scheme finding causes DELETE /issuetypescheme/{id}."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            # usage re-verify endpoint: no projects attached (still unused)
            if "/issuetypescheme/project" in p and req.method == "GET":
                return httpx.Response(200, json={"values": []})
            if p == "/rest/api/3/issuetypescheme" and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "200", "name": "ITS Unused"}]})
            if "/issuetypescheme/200" in p and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("unused_issue_type_scheme", "ITS Unused",
                               area="issuetype_schemes")
        log = []
        closed, _ = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")
        assert deleted

    def test_unused_issue_type_screen_scheme_delete(self):
        """An unused_issue_type_screen_scheme finding causes DELETE /issuetypescreenscheme/{id}."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if "/issuetypescreenscheme/project" in p and req.method == "GET":
                return httpx.Response(200, json={"values": []})
            if p == "/rest/api/3/issuetypescreenscheme" and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "300", "name": "ITSS Unused"}]})
            if "/issuetypescreenscheme/300" in p and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("unused_issue_type_screen_scheme", "ITSS Unused",
                               area="issuetype_screen_schemes")
        log = []
        closed, _ = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")
        assert deleted


# ===========================================================================
# 7. C1 (CRITICAL) — TOCTOU: scheme deletes re-verified as unused at apply time
# ===========================================================================

class TestApplySchemeTOCTOU:

    def test_scheme_used_at_apply_is_not_deleted(self):
        """A workflow scheme unused at audit time but now used by >=1 project at
        apply time must NOT be deleted; it is reported still_open with a clear
        'skipped — now in use' log (ok=True, not a failure)."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            # Live list resolves the scheme by name AND carries current usage.
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "101", "name": "Old Scheme",
                     "projectIds": ["10001"]}]})  # NOW used by a project
            if p == "/rest/api/3/workflowscheme/101" and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Old Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert not deleted, "scheme now in use must NOT be deleted (TOCTOU guard)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "in use" in (r.get("error") or "")]
        assert skip, f"expected a 'now in use' skip log: {log}"
        assert skip[0]["ok"] is True, "a precondition-skip is not a failure"
        assert not any(r.get("method") == "DELETE" for r in log)

    def test_issue_type_scheme_used_at_apply_is_not_deleted(self):
        """Same TOCTOU re-verify for unused_issue_type_scheme: usage read from a
        per-scheme project-usages endpoint shows it is now used → no delete."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/3/issuetypescheme" and req.method == "GET":
                # list does not carry inline usage for this kind
                return httpx.Response(200, json={"values": [
                    {"id": "200", "name": "ITS Unused"}]})
            # per-scheme usage endpoint: shows a project now attached
            if "/issuetypescheme/project" in p and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"issueTypeSchemeId": "200", "projectIds": ["55"]}]})
            if "/issuetypescheme/200" in p and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("unused_issue_type_scheme", "ITS Unused",
                               area="issuetype_schemes")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert not deleted, "issue-type scheme now in use must NOT be deleted"
        assert still_open == 1 and closed == 0

    def test_scheme_still_unused_at_apply_is_deleted(self):
        """Control: a scheme that is STILL unused at apply time IS deleted."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                # First GET resolves + re-verifies (no projects); closure GET
                # after delete shows it gone.
                if not deleted:
                    return httpx.Response(200, json={"values": [
                        {"id": "101", "name": "Free Scheme", "projectIds": []}]})
                return httpx.Response(200, json={"values": []})
            if p == "/rest/api/3/workflowscheme/101" and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Free Scheme")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert deleted, "a still-unused scheme must be deleted"
        assert closed == 1 and still_open == 0


# ===========================================================================
# 8. C2 (CRITICAL) — TOCTOU: empty_group re-checks members before delete
# ===========================================================================

class TestApplyGroupTOCTOU:

    def test_group_nonempty_at_apply_is_not_deleted(self):
        """A group empty at audit but with members at apply time must NOT be
        deleted; reported still_open with a 'now has N member(s)' log."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if req.method == "GET" and "/group/bulk" in p:
                return httpx.Response(200, json={"values": [
                    {"name": "old-group", "groupId": "gid-42"}]})
            # live member-count probe: NOW has members
            if req.method == "GET" and p == "/rest/api/3/group/member":
                return httpx.Response(200, json={"total": 3, "values": []})
            if req.method == "DELETE" and "/group" in p:
                deleted.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("empty_group", "old-group", area="groups")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert not deleted, "group with members must NOT be deleted (TOCTOU guard)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "member" in (r.get("error") or "")]
        assert skip, f"expected a 'now has N member(s)' skip log: {log}"
        assert skip[0]["ok"] is True

    def test_group_still_empty_at_apply_is_deleted(self):
        """Control: a group still empty at apply time IS deleted."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if req.method == "GET" and "/group/bulk" in p:
                if not deleted:
                    return httpx.Response(200, json={"values": [
                        {"name": "ghost", "groupId": "gid-9"}]})
                return httpx.Response(200, json={"values": []})
            if req.method == "GET" and p == "/rest/api/3/group/member":
                return httpx.Response(200, json={"total": 0, "values": []})
            if req.method == "GET" and p == "/rest/api/3/permissionscheme":
                return httpx.Response(200, json={"permissionSchemes": []})
            if req.method == "DELETE" and "/group" in p:
                deleted.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("empty_group", "ghost", area="groups")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert deleted, "a still-empty group must be deleted"
        assert closed == 1 and still_open == 0


# ===========================================================================
# 9. H2 (HIGH) — name-collision: ambiguous / missing-id objects
# ===========================================================================

class TestApplyNameCollision:

    def test_two_same_named_schemes_neither_deleted(self):
        """Two live schemes share the finding's name → ambiguous → no DELETE,
        counted still_open, logged 'name is ambiguous (N matches)'."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "Dup", "projectIds": []},
                    {"id": "2", "name": "Dup", "projectIds": []}]})
            if p.startswith("/rest/api/3/workflowscheme/") and req.method == "DELETE":
                deleted.append(p)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Dup")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert not deleted, "ambiguous name must delete NOTHING"
        assert still_open == 1 and closed == 0
        amb = [r for r in log if "ambiguous" in (r.get("error") or "")]
        assert amb, f"expected an 'ambiguous' skip log: {log}"

    def test_single_match_missing_id_is_error_not_closed(self):
        """A single entry matches by name but has no id key → ERROR (still_open,
        ok=False), NOT a false 'already absent / closed' (review finding M2)."""
        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/3/workflowscheme" and req.method == "GET":
                # matches by name but the id field is absent / differently shaped
                return httpx.Response(200, json={"values": [
                    {"name": "Shapeless", "projectIds": []}]})
            if req.method == "DELETE":
                raise AssertionError("must not DELETE when id cannot be resolved")
            return httpx.Response(404, json={})

        cl = _client(handler)
        finding = _env_finding("scheme_unused", "Shapeless")
        log = []
        closed, still_open = apply_env_fixes(
            cl, [finding], log.append,
            expected_api_base="https://acme.atlassian.net")

        assert still_open == 1 and closed == 0
        assert not any(r.get("ok") and r.get("method") == "GET"
                       and r.get("error") == "already absent" for r in log), \
            "missing id must NOT be reported as already absent/closed"
        assert any(r.get("ok") is False for r in log)


# ===========================================================================
# 10. EXPANDED app-tier deletes — empty_screen
# ===========================================================================

_BASE = "https://acme.atlassian.net"


def _apply_one(cl, finding):
    log = []
    closed, still_open = apply_env_fixes(
        cl, [finding], log.append, expected_api_base=_BASE)
    return closed, still_open, log


def _deletes(log):
    return [r for r in log if r.get("method") == "DELETE"]


class TestApplyEmptyScreen:

    def test_empty_screen_deleted_when_still_empty(self):
        """A still-empty screen (no tabs/fields) is deleted; closure confirms gone."""
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/screens" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json={"values": []})
                return httpx.Response(200, json={"values": [
                    {"id": "10025", "name": "Stray Screen"}]})
            # TOCTOU re-fetch: one empty tab, no fields
            if p == "/rest/api/3/screens/10025/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Field Tab"}])
            if p == "/rest/api/3/screens/10025/tabs/1/fields" and m == "GET":
                return httpx.Response(200, json=[])
            if p == "/rest/api/3/screens/10025" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_screen", "Stray Screen", area="screens"))
        assert state["deleted"], "still-empty screen must be deleted"
        assert closed == 1 and still_open == 0
        assert _deletes(log)[0]["path"] == "/rest/api/3/screens/10025"

    def test_empty_screen_regained_field_is_not_deleted(self):
        """TOCTOU: a screen that has regained a field since the audit is NOT
        deleted; reported still_open with a clear skip log."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10025", "name": "Stray Screen"}]})
            if p == "/rest/api/3/screens/10025/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if p == "/rest/api/3/screens/10025/tabs/1/fields" and m == "GET":
                # NOW carries a field
                return httpx.Response(200, json=[{"id": "summary", "name": "Summary"}])
            if p == "/rest/api/3/screens/10025" and m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_screen", "Stray Screen", area="screens"))
        assert not deleted, "screen with a field must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "field" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_empty_screen_builtin_low_id_is_protected(self):
        """A screen with a low (<=10000) id AND a default-ish name is protected:
        SKIPPED, never deleted, logged as built-in/default, ok=True."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "Default Screen"}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_screen", "Default Screen", area="screens"))
        assert not deleted, "built-in screen must never be deleted"
        assert still_open == 1 and closed == 0
        prot = [r for r in log if "built-in" in (r.get("error") or "").lower()
                or "default" in (r.get("error") or "").lower()]
        assert prot and prot[0]["ok"] is True


# ===========================================================================
# 11. EXPANDED app-tier deletes — screen_not_in_scheme (orphaned screen)
# ===========================================================================

class TestApplyScreenNotInScheme:

    def test_orphaned_screen_deleted_when_still_orphaned(self):
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/screens" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json={"values": []})
                return httpx.Response(200, json={"values": [
                    {"id": "10030", "name": "Orphan Screen"}]})
            # screen-scheme membership: no scheme references screen 10030
            if p == "/rest/api/3/screenscheme" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "SS", "screens": {"default": "10001"}}]})
            if p == "/rest/api/3/screens/10030" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("screen_not_in_scheme", "Orphan Screen",
                             area="screens"))
        assert state["deleted"]
        assert closed == 1 and still_open == 0

    def test_screen_now_in_scheme_is_not_deleted(self):
        """TOCTOU: a screen now referenced by a screen scheme is NOT deleted."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10030", "name": "Orphan Screen"}]})
            if p == "/rest/api/3/screenscheme" and m == "GET":
                # NOW used: scheme references screen 10030
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "SS",
                     "screens": {"default": "10030", "view": "10001"}}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("screen_not_in_scheme", "Orphan Screen",
                             area="screens"))
        assert not deleted, "screen now in a scheme must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "scheme" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True


# ===========================================================================
# 12. EXPANDED app-tier deletes — workflow_unreferenced
# ===========================================================================

class TestApplyWorkflowUnreferenced:

    def test_unreferenced_workflow_deleted_by_entity_id(self):
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/workflow/search" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json={"values": []})
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "Stale WF", "entityId": "uuid-77"}}]})
            # workflow-scheme usage: no scheme references "Stale WF"
            if p == "/rest/api/3/workflowscheme" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "WS", "defaultWorkflow": "jira",
                     "issueTypeMappings": {}}]})
            if p == "/rest/api/3/workflow/uuid-77" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("workflow_unreferenced", "Stale WF",
                             area="workflows"))
        assert state["deleted"], "unreferenced workflow must be deleted by entityId"
        assert closed == 1 and still_open == 0
        assert _deletes(log)[0]["path"] == "/rest/api/3/workflow/uuid-77"

    def test_workflow_now_referenced_is_not_deleted(self):
        """TOCTOU: a workflow now referenced by a workflow scheme is NOT deleted."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "Stale WF", "entityId": "uuid-77"}}]})
            if p == "/rest/api/3/workflowscheme" and m == "GET":
                # NOW referenced via issueTypeMappings
                return httpx.Response(200, json={"values": [
                    {"id": "1", "name": "WS", "defaultWorkflow": "jira",
                     "issueTypeMappings": {"10001": "Stale WF"}}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("workflow_unreferenced", "Stale WF",
                             area="workflows"))
        assert not deleted, "referenced workflow must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "scheme" in (r.get("error") or "").lower()
                or "referenc" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_builtin_jira_workflow_is_protected(self):
        """The built-in 'jira' workflow is protected by name even with a high id."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "jira", "entityId": "uuid-builtin"}}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("workflow_unreferenced", "jira", area="workflows"))
        assert not deleted, "built-in jira workflow must never be deleted"
        assert still_open == 1 and closed == 0
        prot = [r for r in log if "built-in" in (r.get("error") or "").lower()
                or "default" in (r.get("error") or "").lower()]
        assert prot and prot[0]["ok"] is True


# ===========================================================================
# 13. EXPANDED app-tier deletes — unused_custom_field (riskiest; value-check)
# ===========================================================================

class TestApplyUnusedCustomField:

    def _field_list(self, fid="customfield_10500", name="Migrated Notes"):
        return {"id": fid, "name": name, "custom": True,
                "schema": {"custom": "com.x:textarea"}}

    def test_unused_field_deleted_when_no_screen_and_zero_values(self):
        """A custom field on no screen AND with zero values IS deleted."""
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[self._field_list()])
            # screens list for the on-no-screen re-verify
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10001", "name": "Default Screen"}]})
            if p == "/rest/api/3/screens/10001/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if p == "/rest/api/3/screens/10001/tabs/1/fields" and m == "GET":
                return httpx.Response(200, json=[{"id": "summary", "name": "Summary"}])
            # VALUE CHECK: zero issues hold the field
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            # FILTER REFERENCE CHECK: no saved filter references the field
            if p == "/rest/api/3/filter/search" and m == "GET":
                return httpx.Response(200, json={"values": [], "isLast": True})
            if p == "/rest/api/3/field/customfield_10500" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("unused_custom_field", "Migrated Notes",
                             area="custom_fields"))
        assert state["deleted"], "no-screen + zero-value field must be deleted"
        assert closed == 1 and still_open == 0
        assert _deletes(log)[0]["path"] == "/rest/api/3/field/customfield_10500"

    def test_field_referenced_by_a_saved_filter_is_not_deleted(self):
        """REFERENCE CHECK (no-bias review): a field on no screen with zero values
        but used in a saved filter's JQL must NOT be deleted — that would break
        the filter. Reported still_open, no DELETE issued."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                return httpx.Response(200, json=[self._field_list()])
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10001", "name": "Default Screen"}]})
            if p == "/rest/api/3/screens/10001/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if p == "/rest/api/3/screens/10001/tabs/1/fields" and m == "GET":
                return httpx.Response(200, json=[])           # not on any screen
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})  # zero values
            if p == "/rest/api/3/filter/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "9", "name": "Open Notes",
                     "jql": "cf[10500] is not EMPTY ORDER BY created"}],
                    "isLast": True})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("unused_custom_field", "Migrated Notes",
                             area="custom_fields"))
        assert not deleted, "a field referenced by a filter must NOT be deleted"
        assert still_open == 1
        assert any("filter" in (r.get("error") or "") for r in log)

    def test_field_with_values_is_not_deleted(self):
        """VALUE CHECK: a field that holds data on >=1 issue is NOT deleted —
        deleting would destroy data; reported still_open with an N-issue skip."""
        deleted = []
        counted = {}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                return httpx.Response(200, json=[self._field_list()])
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10001", "name": "Default Screen"}]})
            if p == "/rest/api/3/screens/10001/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if p == "/rest/api/3/screens/10001/tabs/1/fields" and m == "GET":
                return httpx.Response(200, json=[])
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                counted["jql"] = json.loads(req.content).get("jql", "")
                return httpx.Response(200, json={"count": 12})  # holds data!
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("unused_custom_field", "Migrated Notes",
                             area="custom_fields"))
        assert not deleted, "a field with values must NOT be deleted (value-check)"
        assert still_open == 1 and closed == 0
        # the JQL is the numeric cf[NNNNN] is not EMPTY form
        assert "cf[10500]" in counted.get("jql", "")
        skip = [r for r in log if "value" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_field_now_on_screen_is_not_deleted(self):
        """TOCTOU: a field that is now on a screen is NOT deleted (no value-check
        needed — it is in use)."""
        deleted = []
        value_checked = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                return httpx.Response(200, json=[self._field_list()])
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10001", "name": "Default Screen"}]})
            if p == "/rest/api/3/screens/10001/tabs" and m == "GET":
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if p == "/rest/api/3/screens/10001/tabs/1/fields" and m == "GET":
                # field NOW on this screen (matched by id)
                return httpx.Response(200, json=[
                    {"id": "customfield_10500", "name": "Migrated Notes"}])
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                value_checked.append(True)
                return httpx.Response(200, json={"count": 0})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("unused_custom_field", "Migrated Notes",
                             area="custom_fields"))
        assert not deleted, "field now on a screen must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "screen" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_low_id_custom_field_is_protected(self):
        """A customfield_NNNNN whose numeric part <=10000 is protected (built-in
        / default) and never deleted."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                return httpx.Response(200, json=[
                    self._field_list(fid="customfield_10000", name="System Field")])
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("unused_custom_field", "System Field",
                             area="custom_fields"))
        assert not deleted, "low-id custom field must be protected"
        assert still_open == 1 and closed == 0
        prot = [r for r in log if "built-in" in (r.get("error") or "").lower()
                or "default" in (r.get("error") or "").lower()]
        assert prot and prot[0]["ok"] is True


# ===========================================================================
# 14. EXPANDED app-tier deletes — empty_project (Cloud trash, recoverable)
# ===========================================================================

class TestApplyEmptyProject:

    def test_empty_project_deleted_when_still_zero_issues(self):
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/project/search" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json={"values": []})
                return httpx.Response(200, json={"values": [
                    {"id": "10500", "key": "DEAD", "name": "Dead Project"}]})
            # TOCTOU: project still has zero issues
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/project/DEAD" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_project", "DEAD", area="projects"))
        assert state["deleted"], "empty project must be deleted by key"
        assert closed == 1 and still_open == 0
        assert _deletes(log)[0]["path"] == "/rest/api/3/project/DEAD"

    def test_project_now_has_issues_is_not_deleted(self):
        """TOCTOU: a project that now holds issues is NOT deleted."""
        deleted = []
        counted = {}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/project/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10500", "key": "DEAD", "name": "Dead Project"}]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                counted["jql"] = json.loads(req.content).get("jql", "")
                return httpx.Response(200, json={"count": 4})  # NOW has issues
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_project", "DEAD", area="projects"))
        assert not deleted, "project with issues must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        assert "DEAD" in counted.get("jql", "")
        skip = [r for r in log if "issue" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_low_id_project_is_protected(self):
        """A project whose numeric id <=10000 is protected (built-in/default)."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/project/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": "10000", "key": "SYS", "name": "System Project"}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("empty_project", "SYS", area="projects"))
        assert not deleted, "low-id project must be protected"
        assert still_open == 1 and closed == 0


# ===========================================================================
# 14b. EXPANDED app-tier deletes — status_not_in_workflow (orphan status)
#
# The status is in NO workflow, so the delete is clean. Gates: built-in status
# protection (low id + system names), TOCTOU workflow re-read (abort if now in a
# workflow), and an issues-in-status value-check (abort if any issue sits in it).
# Cloud delete is keyed by id via DELETE /rest/api/3/statuses?id={id}.
# ===========================================================================

class TestApplyStatusNotInWorkflow:

    def test_orphan_status_deleted_when_still_unused_and_empty(self):
        """A status in no workflow AND holding zero issues IS deleted; closure
        confirms it is gone from the live status list."""
        state = {"deleted": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            # live status list (resolve id by name + closure re-read)
            if p == "/rest/api/3/statuses" and m == "GET":
                if state["deleted"]:
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Stray Status"}])
            # TOCTOU: no workflow references the status
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"},
                     "statuses": [{"name": "To Do"}, {"name": "Done"}]}]})
            # VALUE CHECK: zero issues currently sit in the status
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/statuses" and m == "DELETE":
                state["deleted"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Stray Status",
                             area="statuses"))
        assert state["deleted"], "orphan, empty status must be deleted"
        assert closed == 1 and still_open == 0
        d = _deletes(log)[0]
        assert d["path"] == "/rest/api/3/statuses"

    def test_status_deleted_via_id_query_param(self):
        """The DELETE targets the bulk /statuses endpoint with ?id={id}."""
        seen = {}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Stray Status"}])
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"}, "statuses": [{"name": "Done"}]}]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/statuses" and m == "DELETE":
                seen["params"] = dict(req.url.params)
                # second GET (closure) shows it gone
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        # one closure read must show the status gone; flip the list after delete
        deleted = {"yes": False}

        def handler2(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=([] if deleted["yes"]
                    else [{"id": "10042", "name": "Stray Status"}]))
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"}, "statuses": [{"name": "Done"}]}]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/statuses" and m == "DELETE":
                seen["params"] = dict(req.url.params)
                deleted["yes"] = True
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler2)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Stray Status",
                             area="statuses"))
        assert seen.get("params", {}).get("id") == "10042"
        assert closed == 1 and still_open == 0

    def test_status_now_in_a_workflow_is_not_deleted(self):
        """TOCTOU: a status that is now used by a workflow is NOT deleted."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Stray Status"}])
            if p == "/rest/api/3/workflow/search" and m == "GET":
                # NOW referenced by a workflow's status set
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"},
                     "statuses": [{"name": "To Do"}, {"name": "Stray Status"}]}]})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Stray Status",
                             area="statuses"))
        assert not deleted, "status now in a workflow must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "workflow" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True
        # a DELETE must never have been attempted
        assert not _deletes(log)

    def test_status_with_issues_is_not_deleted(self):
        """VALUE CHECK: a status that currently holds issues is NOT deleted —
        deleting it would lose issue state."""
        deleted = []
        counted = {}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Stray Status"}])
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"}, "statuses": [{"name": "Done"}]}]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                counted["jql"] = json.loads(req.content).get("jql", "")
                return httpx.Response(200, json={"count": 7})  # issues sit here!
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Stray Status",
                             area="statuses"))
        assert not deleted, "status holding issues must NOT be deleted (value-check)"
        assert still_open == 1 and closed == 0
        assert "Stray Status" in counted.get("jql", "")
        skip = [r for r in log if "issue" in (r.get("error") or "").lower()]
        assert skip and skip[0]["ok"] is True

    def test_status_value_check_errors_aborts(self):
        """If the issues-in-status count errors (None / ERR), ABORT — never
        delete on an unverifiable precondition."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Stray Status"}])
            if p == "/rest/api/3/workflow/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": {"name": "WF"}, "statuses": [{"name": "Done"}]}]})
            # value check ERRORS
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(500, json={})
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Stray Status",
                             area="statuses"))
        assert not deleted, "an unverifiable value-check must abort the delete"
        assert still_open == 1 and closed == 0

    def test_low_id_status_is_protected(self):
        """A status whose id <=10000 is a built-in/default and is never deleted."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10000", "name": "Custom Looking Name"}])
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Custom Looking Name",
                             area="statuses"))
        assert not deleted, "low-id status must be protected"
        assert still_open == 1 and closed == 0
        prot = [r for r in log if "built-in" in (r.get("error") or "").lower()
                or "default" in (r.get("error") or "").lower()]
        assert prot and prot[0]["ok"] is True

    def test_system_named_status_is_protected_even_with_high_id(self):
        """A well-known system status name (e.g. 'In Progress') is protected by
        name even if Jira gives it a high id."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "99999", "name": "In Progress"}])
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "In Progress",
                             area="statuses"))
        assert not deleted, "system status name must be protected"
        assert still_open == 1 and closed == 0
        prot = [r for r in log if "built-in" in (r.get("error") or "").lower()
                or "default" in (r.get("error") or "").lower()]
        assert prot and prot[0]["ok"] is True

    def test_status_already_absent_is_idempotent_closed(self):
        """A status already gone at apply time is a no-op closed (idempotent)."""
        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[])
            if m == "DELETE":
                raise AssertionError("must not DELETE when already absent")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Gone Status",
                             area="statuses"))
        assert closed == 1 and still_open == 0
        assert not _deletes(log)

    def test_ambiguous_status_name_deletes_nothing(self):
        """Two live statuses share the finding's name → ambiguous → no DELETE."""
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/statuses" and m == "GET":
                return httpx.Response(200, json=[
                    {"id": "10042", "name": "Dup Status"},
                    {"id": "10043", "name": "Dup Status"}])
            if m == "DELETE":
                deleted.append(True)
                return httpx.Response(204, content=b"")
            return httpx.Response(404, json={})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("status_not_in_workflow", "Dup Status",
                             area="statuses"))
        assert not deleted, "ambiguous status name must delete NOTHING"
        assert still_open == 1 and closed == 0
        amb = [r for r in log if "ambiguous" in (r.get("error") or "").lower()]
        assert amb


# ===========================================================================
# 15. Cross-cutting guards for the EXPANDED kinds
# ===========================================================================

class TestExpandedKindsGuards:

    def test_identity_guard_blocks_expanded_kinds(self):
        """Identity guard fires before any write for an expanded kind too."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler, site=_BASE)
        for kind, name, area in (
            ("empty_screen", "S", "screens"),
            ("workflow_unreferenced", "W", "workflows"),
            ("unused_custom_field", "F", "custom_fields"),
            ("empty_project", "P", "projects"),
            ("screen_not_in_scheme", "O", "screens"),
            ("status_not_in_workflow", "St", "statuses"),
        ):
            with pytest.raises(ValueError, match="api_base"):
                apply_env_fixes(cl, [_env_finding(kind, name, area=area)],
                                lambda r: None,
                                expected_api_base="https://OTHER.atlassian.net")
        assert not writes

    def test_idempotent_expanded_kinds_already_absent(self):
        """Already-gone objects for the expanded kinds are no-op closed."""
        def handler(req):
            # every list comes back empty → already absent. /field and /statuses
            # are plain lists on Cloud; the paginated lists use {values:[]}.
            if req.method == "GET":
                if str(req.url.path) in ("/rest/api/3/field",
                                         "/rest/api/3/statuses"):
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json={"values": []})
            if req.method == "DELETE":
                raise AssertionError("must not DELETE when already absent")
            return httpx.Response(404, json={})

        cl = _client(handler)
        for kind, name, area in (
            ("empty_screen", "Gone Screen", "screens"),
            ("screen_not_in_scheme", "Gone Orphan", "screens"),
            ("workflow_unreferenced", "Gone WF", "workflows"),
            ("unused_custom_field", "Gone Field", "custom_fields"),
            ("empty_project", "GONE", "projects"),
            ("status_not_in_workflow", "Gone Status", "statuses"),
        ):
            closed, still_open, log = _apply_one(
                cl, _env_finding(kind, name, area=area))
            assert closed == 1 and still_open == 0, f"{kind} idempotency"
            assert not _deletes(log), f"{kind} must not DELETE when absent"

    def test_expanded_kinds_not_in_scope_are_refused_if_human(self):
        """A duplicate_field (still human) is never applied even though the
        custom-field sibling is now app-tier."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"values": []})

        cl = _client(handler)
        closed, still_open, log = _apply_one(
            cl, _env_finding("duplicate_field", "Severity", area="custom_fields"))
        assert not writes
        assert not any(r.get("method") in ("DELETE", "POST", "PUT") for r in log)


def test_env_fix_run_page_shows_failure_reason(tmp_path):
    """A failed env-fix run (e.g. the destructive-ops cap aborted the whole batch
    before any action) must surface the reason on the result page, not a bare
    FAILED page with 'No actions recorded yet'."""
    app = _app(tmp_path)
    store = app.state.store
    mid = store.create_migration("env", audit_type="environment")
    rid = store.create_run(mid, {}, kind="env_fix")
    store.add_event(rid, "apply", "error",
                    "refusing to apply 200 destructive operation(s) in one "
                    "batch — the cap is 50. Trim the selection.")
    store.update_run(rid, status="failed")
    t = TestClient(app).get(f"/env-fix-runs/{rid}").text
    assert "the cap is 50" in t
    assert "Run failed" in t
