"""Tests for the CONFLUENCE env-fix apply path.

Mirrors tests/test_env_fix.py (the Jira apply contract) for the two app-tier
Confluence kinds — empty_space (archive) and confluence_empty_group (delete) —
exercising the SAME safety guards the Jira path has:

  - identity guard: mismatched expected_api_base raises before any write.
  - tier re-derivation: a human/unfixable Confluence finding selected by a
    crafted request is NOT applied.
  - TOCTOU re-verify: a space that gained pages / a group that gained members
    between audit and apply is NOT mutated.
  - name-collision safety: zero match -> already gone (closed); >1 -> ambiguous
    (still_open); single match but no id -> error (still_open, ok=False).
  - idempotency: already-archived space / already-gone group -> logged no-op,
    counted closed, not FAILED.
  - closure proven by RE-READING (space status==archived / group absent).
  - regression: the Confluence env AUDIT still performs zero writes.
"""
from __future__ import annotations

import copy
import time

import httpx
import pytest

from auditor.client import Connection
from auditor.confluence.client import ConfluenceClient
from auditor.connectors import get_connector
from auditor.envaudit.confluence_apply import apply_confluence_fixes
from webapp.runs import RunEngine
from webapp.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CLOUD_API_BASE = "https://acme.atlassian.net/wiki"


def _conf_client(handler, site="https://acme.atlassian.net", deployment="cloud"):
    if deployment == "cloud":
        conn = Connection(auth_type="pat", site_url=site, deployment="cloud",
                          email="a@b.c", api_token="x")
    else:
        conn = Connection(auth_type="pat", site_url=site, deployment="dc",
                          api_token="x")
    return ConfluenceClient(conn, http=httpx.Client(
        transport=httpx.MockTransport(handler)), sleeper=lambda s: None)


def _env_finding(kind, name, area="spaces"):
    """Build a finding dict as stored in findings_config (detail nested)."""
    from auditor.envaudit.fixes import _FIXES, category_for
    fix = copy.copy(_FIXES.get(kind, {
        "tier": "human", "tier_label": "Fixable by a human",
        "title": kind, "detail": "n/a", "api_hint": None,
        "risk": "low", "reversible": True, "caveat": None,
    }))
    if name and name not in fix.get("title", ""):
        fix["title"] = f"{fix['title']}: {name}"
    return {
        "area": area, "name": name, "kind": kind, "severity": "low",
        "detail": {"fix": fix, "category": category_for(kind),
                   "severity": "low"},
    }


def _wait(store, rid, t=5):
    end = time.time() + t
    while time.time() < end:
        r = store.get_run(rid)
        if r["status"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


# ===========================================================================
# 1. empty_space — archive happy path + closure-by-reread
# ===========================================================================

class TestApplyEmptySpace:

    def test_still_empty_space_is_archived_and_closed(self):
        """A genuinely-still-empty space is archived; closure is proven by a
        re-read showing status==archived (not assumed from the 2xx)."""
        archived = []
        reread = {"n": 0}

        def handler(req):
            p = str(req.url.path)
            params = dict(req.url.params)
            # name->id resolution + closure re-read both hit /api/v2/spaces
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                reread["n"] += 1
                # After the archive, the re-read reports the space archived.
                status = "archived" if archived else "current"
                return httpx.Response(200, json={"results": [
                    {"id": "111", "key": "GHOST", "name": "Ghost Space",
                     "type": "global", "status": status}], "_links": {}})
            # TOCTOU page re-verify: still zero pages
            if p == "/wiki/rest/api/search" and req.method == "GET":
                assert params["cql"] == 'space="GHOST" and type=page'
                return httpx.Response(200, json={"totalSize": 0, "results": []})
            if p == "/wiki/api/v2/spaces/111" and req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={"id": "111",
                                                 "status": "archived"})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)

        assert archived, "PUT archive must have been called"
        assert closed == 1 and still_open == 0
        assert any(r["ok"] and r["method"] == "PUT" for r in log)
        assert reread["n"] >= 2, "closure must re-read the space list"

    def test_dry_run_previews_archive_without_writing(self):
        """dry_run=True runs the resolve + TOCTOU page re-verify but issues NO
        PUT: it logs a WOULD-ARCHIVE record and counts the space as would-close."""
        archived = []

        def handler(req):
            p = str(req.url.path)
            params = dict(req.url.params)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "111", "key": "GHOST", "name": "Ghost Space",
                     "type": "global", "status": "current"}], "_links": {}})
            if p == "/wiki/rest/api/search" and req.method == "GET":
                return httpx.Response(200, json={"totalSize": 0, "results": []})
            if p == "/wiki/api/v2/spaces/111" and req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={"id": "111",
                                                 "status": "archived"})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE,
            dry_run=True)

        assert not archived, "dry run must NOT issue a PUT archive"
        assert not any(r["method"] == "PUT" for r in log)
        would = [r for r in log if r["method"] == "WOULD-ARCHIVE"]
        assert would and would[0]["ok"], f"expected WOULD-ARCHIVE: {log}"
        assert closed == 1 and still_open == 0

    def test_empty_space_dc_archives_via_v1(self):
        """DC path: resolve by key from /rest/api/space then PUT the v1
        space-update endpoint; closure re-reads status."""
        archived = []

        def handler(req):
            p = str(req.url.path)
            if p == "/rest/api/space" and req.method == "GET":
                status = "archived" if archived else "current"
                return httpx.Response(200, json={"results": [
                    {"id": "9", "key": "OLD", "name": "Old",
                     "type": "global", "status": status}],
                    "start": 0, "limit": 50, "size": 1, "_links": {}})
            if p == "/rest/api/search" and req.method == "GET":
                return httpx.Response(200, json={"totalSize": 0, "results": []})
            if p == "/rest/api/space/OLD" and req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={"key": "OLD",
                                                 "status": "archived"})
            return httpx.Response(404, json={})

        cl = _conf_client(handler, site="https://conf.acme.example",
                          deployment="dc")
        finding = _env_finding("empty_space", "OLD")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append,
            expected_api_base="https://conf.acme.example")
        assert archived
        assert closed == 1 and still_open == 0


# ===========================================================================
# 2. confluence_empty_group — delete happy path + closure-by-reread
# ===========================================================================

class TestApplyEmptyGroup:

    def test_still_empty_group_is_deleted_and_closed(self):
        """A still-empty group is deleted; closure proven by absence on re-read."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            params = dict(req.url.params)
            if p == "/wiki/rest/api/group" and req.method == "GET":
                if deleted:
                    return httpx.Response(200, json={"results": [],
                                                     "_links": {}})
                return httpx.Response(200, json={"results": [
                    {"name": "ghost-group", "id": "gid-9"}], "_links": {}})
            # member-count re-verify: still empty
            if "/member" in p and req.method == "GET":
                return httpx.Response(200, json={"results": [], "size": 0,
                                                 "_links": {}})
            if p == "/wiki/rest/api/group" and req.method == "DELETE":
                assert params.get("name") == "ghost-group"
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("confluence_empty_group", "ghost-group",
                               area="groups")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)

        assert deleted, "DELETE group must have been called"
        assert closed == 1 and still_open == 0
        assert any(r["ok"] and r["method"] == "DELETE" for r in log)


# ===========================================================================
# 3. TOCTOU — space gained pages / group gained members between audit & apply
# ===========================================================================

class TestApplyTOCTOU:

    def test_space_with_pages_now_is_not_archived(self):
        """A space empty at audit but with pages NOW must NOT be archived;
        reported still_open with a 'now has N page(s)' skip (ok=True)."""
        archived = []

        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "111", "key": "GHOST", "name": "Ghost",
                     "type": "global", "status": "current"}], "_links": {}})
            # live page re-verify: NOW has pages
            if p == "/wiki/rest/api/search" and req.method == "GET":
                return httpx.Response(200, json={"totalSize": 4, "results": []})
            if p == "/wiki/api/v2/spaces/111" and req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)

        assert not archived, "space with pages must NOT be archived (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "page" in (r.get("error") or "")]
        assert skip, f"expected a 'now has N page(s)' skip log: {log}"
        assert skip[0]["ok"] is True
        assert not any(r.get("method") == "PUT" for r in log)

    def test_space_count_unreadable_blocks_archive(self):
        """If the page count cannot be read (ERR), do NOT archive — an
        unverifiable precondition is treated conservatively as non-empty."""
        archived = []

        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "111", "key": "GHOST", "name": "Ghost",
                     "type": "global", "status": "current"}], "_links": {}})
            if p == "/wiki/rest/api/search" and req.method == "GET":
                return httpx.Response(400, text="bad cql")
            if p == "/wiki/api/v2/spaces/111" and req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert not archived
        assert still_open == 1 and closed == 0

    def test_group_with_members_now_is_not_deleted(self):
        """A group empty at audit but with members NOW must NOT be deleted."""
        deleted = []

        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/rest/api/group" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"name": "old-group", "id": "gid-42"}], "_links": {}})
            if "/member" in p and req.method == "GET":
                return httpx.Response(200, json={"results": [{}, {}, {}],
                                                 "size": 3, "_links": {}})
            if p == "/wiki/rest/api/group" and req.method == "DELETE":
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("confluence_empty_group", "old-group",
                               area="groups")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)

        assert not deleted, "group with members must NOT be deleted (TOCTOU)"
        assert still_open == 1 and closed == 0
        skip = [r for r in log if "member" in (r.get("error") or "")]
        assert skip, f"expected a 'now has N member(s)' skip log: {log}"
        assert skip[0]["ok"] is True


# ===========================================================================
# 4. Identity guard — mismatched expected_api_base raises, no write
# ===========================================================================

class TestIdentityGuard:

    def test_mismatched_api_base_raises_before_any_write(self):
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"results": [], "_links": {}})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        with pytest.raises(ValueError, match="api_base"):
            apply_confluence_fixes(
                cl, [finding], lambda r: None,
                expected_api_base="https://OTHER.atlassian.net/wiki")
        assert not writes, "no write may occur when identity guard fails"

    def test_matching_api_base_does_not_raise(self):
        """The audited client's own api_base must pass the guard."""
        def handler(req):
            return httpx.Response(200, json={"results": [], "_links": {}})
        cl = _conf_client(handler)
        # Empty findings → no-op, but must not raise on the correct base.
        closed, still_open = apply_confluence_fixes(
            cl, [], lambda r: None, expected_api_base=cl.api_base)
        assert (closed, still_open) == (0, 0)


# ===========================================================================
# 5. Tier guard — human / unfixable Confluence findings are NOT applied
# ===========================================================================

class TestTierGuard:

    def test_human_confluence_finding_not_applied(self):
        """A human-tier Confluence finding (large_space) selected by a crafted
        request must generate NO write."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"results": [], "_links": {}})

        cl = _conf_client(handler)
        finding = _env_finding("large_space", "ENG")  # human tier
        log = []
        apply_confluence_fixes(cl, [finding], log.append,
                               expected_api_base=CLOUD_API_BASE)
        assert not writes
        assert not any(r.get("method") in ("PUT", "DELETE", "POST")
                       for r in log)

    def test_unfixable_confluence_finding_not_applied(self):
        """archived_space_clutter is an unfixable Confluence kind — refused."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"results": [], "_links": {}})

        cl = _conf_client(handler)
        finding = _env_finding("archived_space_clutter", "n/a")
        log = []
        apply_confluence_fixes(cl, [finding], log.append,
                               expected_api_base=CLOUD_API_BASE)
        assert not writes

    def test_jira_app_kind_not_applied_by_confluence_path(self):
        """A Jira app-tier kind (empty_group) must NOT be applied by the
        Confluence path — scope is Confluence kinds only (belt and braces)."""
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(req)
            return httpx.Response(200, json={"results": [], "_links": {}})

        cl = _conf_client(handler)
        finding = _env_finding("empty_group", "old-group", area="groups")
        log = []
        apply_confluence_fixes(cl, [finding], log.append,
                               expected_api_base=CLOUD_API_BASE)
        assert not writes


# ===========================================================================
# 6. Idempotency — already-archived space / already-gone group
# ===========================================================================

class TestIdempotency:

    def test_already_archived_space_is_noop_closed(self):
        """A space already archived → no PUT, logged no-op, counted closed."""
        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "111", "key": "GHOST", "name": "Ghost",
                     "type": "global", "status": "archived"}], "_links": {}})
            if req.method == "PUT":
                raise AssertionError("must not PUT an already-archived space")
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert not any(r.get("method") == "PUT" for r in log)
        assert closed == 1 and still_open == 0

    def test_space_already_gone_is_noop_closed(self):
        """Zero name matches → already gone → closed no-op, no write."""
        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [], "_links": {}})
            if req.method == "PUT":
                raise AssertionError("must not PUT when space absent")
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert closed == 1 and still_open == 0

    def test_group_already_gone_is_noop_closed(self):
        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/rest/api/group" and req.method == "GET":
                return httpx.Response(200, json={"results": [], "_links": {}})
            if req.method == "DELETE":
                raise AssertionError("must not DELETE an absent group")
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("confluence_empty_group", "ghost", area="groups")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert closed == 1 and still_open == 0


# ===========================================================================
# 7. Name-collision safety — ambiguous / missing id
# ===========================================================================

class TestNameCollision:

    def test_two_same_keyed_spaces_neither_archived(self):
        """Two live rows share the finding's space KEY → ambiguous → no write."""
        archived = []

        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "1", "key": "DUP", "name": "Dup A",
                     "type": "global", "status": "current"},
                    {"id": "2", "key": "DUP", "name": "Dup B",
                     "type": "global", "status": "current"}], "_links": {}})
            if req.method == "PUT":
                archived.append(True)
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "DUP")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert not archived
        assert still_open == 1 and closed == 0
        assert any("ambiguous" in (r.get("error") or "") for r in log)

    def test_single_space_missing_id_is_error_not_closed(self):
        """A single space matches by key but carries no id (Cloud) → ERROR
        (still_open, ok=False), NOT a false 'already gone / closed'."""
        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"key": "GHOST", "name": "Ghost", "type": "global",
                     "status": "current"}], "_links": {}})  # no id
            if req.method == "PUT":
                raise AssertionError("must not PUT when id unresolved")
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "GHOST")
        log = []
        closed, still_open = apply_confluence_fixes(
            cl, [finding], log.append, expected_api_base=CLOUD_API_BASE)
        assert still_open == 1 and closed == 0
        assert any(r.get("ok") is False for r in log)


# ===========================================================================
# 8. Log record shape
# ===========================================================================

class TestLogShape:

    def test_log_records_have_required_shape(self):
        def handler(req):
            p = str(req.url.path)
            if p == "/wiki/api/v2/spaces" and req.method == "GET":
                return httpx.Response(200, json={"results": [
                    {"id": "1", "key": "S", "name": "S", "type": "global",
                     "status": "archived" if False else "current"}],
                    "_links": {}})
            if p == "/wiki/rest/api/search" and req.method == "GET":
                return httpx.Response(200, json={"totalSize": 0, "results": []})
            if req.method == "PUT":
                return httpx.Response(200, json={"status": "archived"})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        finding = _env_finding("empty_space", "S")
        log = []
        apply_confluence_fixes(cl, [finding], log.append,
                               expected_api_base=CLOUD_API_BASE)
        for r in log:
            for k in ("object_name", "method", "path", "status", "ok",
                      "finding_ref", "fix_id", "created_id", "error"):
                assert k in r, f"missing {k}: {r}"


# ===========================================================================
# 9. Dispatch — env_fix_apply routes a Confluence migration to the Confluence
#    apply path with the Confluence api_base as the identity guard.
# ===========================================================================

class TestDispatch:

    def test_env_fix_apply_dispatches_to_confluence_path(self, tmp_path,
                                                         monkeypatch):
        """A Confluence env_fix run must call apply_confluence_fixes (not the
        Jira apply_env_fixes), passing the audited Confluence api_base."""
        import webapp.env_fix_stages as efs

        called = {}

        def fake_conf_apply(client, findings, log, expected_api_base=None,
                            dry_run=False, record_sink=None):
            called["client"] = client
            called["expected_api_base"] = expected_api_base
            called["dry_run"] = dry_run
            called["record_sink"] = record_sink
            called["kinds"] = [f.get("kind") for f in findings]
            log({"object_name": "GHOST", "method": "PUT",
                 "path": "/wiki/api/v2/spaces/1", "status": 200, "ok": True,
                 "finding_ref": None, "fix_id": None, "created_id": None,
                 "error": None})
            return 1, 0

        def boom(*a, **k):
            raise AssertionError("Jira apply must not run for a Confluence fix")

        monkeypatch.setattr(efs, "apply_confluence_fixes", fake_conf_apply)
        monkeypatch.setattr(efs, "apply_env_fixes", boom)

        conf_client = _conf_client(lambda r: httpx.Response(404))
        connector = get_connector("confluence")

        store = Store(str(tmp_path / "d.db"), str(tmp_path / "d.key"))
        mid = store.create_migration("env", product="confluence",
                                     audit_type="environment")
        audit = store.create_run(mid, {}, kind="env_audit")
        store.update_run(audit, status="done")
        store.insert_findings_config(
            audit, [_env_finding("empty_space", "GHOST")])
        fix_run = store.create_run(mid, {"finding_refs":
                                         ["empty_space:GHOST"]},
                                   kind="env_fix", source_run_id=audit)

        ctx = {
            "store": store, "run_id": fix_run, "migration_id": mid,
            "src": conf_client, "connector": connector,
            "expected_api_base": conf_client.api_base,
            "params": {"finding_refs": ["empty_space:GHOST"]},
        }
        efs.env_fix_apply(ctx)

        assert called.get("client") is conf_client
        assert called.get("expected_api_base") == conf_client.api_base
        assert called.get("kinds") == ["empty_space"]
        assert ctx["closure"]["closed"] == 1


# ===========================================================================
# 10. Regression — the Confluence env AUDIT still performs zero writes
# ===========================================================================

class TestConfluenceAuditWriteRegression:

    def test_confluence_env_audit_never_writes(self, tmp_path, monkeypatch):
        """An env_audit run for a Confluence migration must issue zero
        mutating HTTP calls (GET-only audit)."""
        import webapp.env_stages as es

        store = Store(str(tmp_path / "e.db"), str(tmp_path / "e.key"))
        mid = store.create_migration("env", product="confluence",
                                     audit_type="environment")
        store.save_connection(mid, "source", "pat",
                              "https://acme.atlassian.net",
                              {"token": "x", "email": "a@b.c"})
        writes = []

        def handler(req):
            if req.method != "GET":
                writes.append(f"{req.method} {req.url.path}")
            p = str(req.url.path)
            if p.endswith("/rest/api/user/current"):
                return httpx.Response(200, json={"displayName": "Igor"})
            if "/api/v2/spaces" in p:
                return httpx.Response(200, json={"results": [], "_links": {}})
            if p.endswith("/rest/api/search"):
                return httpx.Response(200, json={"totalSize": 0, "results": []})
            if p.endswith("/rest/api/group"):
                return httpx.Response(200, json={"results": [], "_links": {}})
            return httpx.Response(200, json={"results": [], "_links": {}})

        def fake_clients(store_, mid_, http=None, require_both=True):
            assert require_both is False
            conn = Connection(auth_type="pat",
                              site_url="https://acme.atlassian.net",
                              deployment="cloud", email="a@b.c", api_token="x")
            cl = ConfluenceClient(conn, http=httpx.Client(
                transport=httpx.MockTransport(handler)), sleeper=lambda s: None)
            return cl, None, get_connector("confluence")

        monkeypatch.setattr(es, "build_clients", fake_clients)
        monkeypatch.setattr(es, "ai_provider", lambda store_: None)

        engine = RunEngine(store, str(tmp_path / "ws"),
                           env_stages=es.build_env_stages())
        rid = engine.start(mid, {}, kind="env_audit")
        r = _wait(store, rid)
        assert r["status"] == "done"
        assert not writes, f"confluence env_audit issued writes: {writes}"
