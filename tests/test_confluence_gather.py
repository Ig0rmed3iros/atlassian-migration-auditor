"""Confluence environment-audit gather tests (spec R1, privacy I1).

Mirrors tests/test_env_gather.py's MockTransport style. Each area is exercised
for its Cloud shape, DC skip-or-behave, capping, error-preservation, and the
absolute privacy reductions: NO page title/body, NO user/member/admin identity,
NO accountId/email, and NEVER a personal-space key or name (personal spaces are
COUNTED only).
"""
import json
import httpx

from auditor.client import Connection
from auditor.confluence.client import ConfluenceClient
from auditor.envaudit.confluence_gather import gather_confluence


def mk(handler, deployment="cloud"):
    if deployment == "cloud":
        conn = Connection(auth_type="pat", site_url="https://acme.atlassian.net",
                          email="igor@acme.example", api_token="tok")
    else:
        conn = Connection(auth_type="pat", site_url="https://confluence.acme.example",
                          deployment="dc", api_token="tok-123")
    return ConfluenceClient(
        conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda s: None)


# ---------------------------------------------------------------------------
# Building-block handlers
# ---------------------------------------------------------------------------

def _empty(req):
    """Baseline: every v2/classic envelope empty, every CQL count 0."""
    p = str(req.url.path)
    if p.endswith("/rest/api/search"):
        return httpx.Response(200, json={"totalSize": 0, "results": []})
    return httpx.Response(200, json={"results": [], "_links": {}})


def _mk(*overrides):
    def handler(req):
        for pred, resp in overrides:
            if pred(req):
                return resp(req)
        return _empty(req)
    return handler


# ===========================================================================
# Outer shape
# ===========================================================================

def test_gather_outer_shape_cloud():
    snap = gather_confluence(mk(_empty), ["ENG", "HR"])
    assert snap["deployment"] == "cloud"
    assert snap["projects"] == ["ENG", "HR"]
    assert isinstance(snap["areas"], dict)
    # All documented areas present.
    for area in ("spaces", "space_permissions", "page_restrictions", "groups",
                 "templates", "labels", "content_quality"):
        assert area in snap["areas"], f"missing area {area}"


def test_gather_progress_optional():
    # Omitting progress must not raise.
    snap = gather_confluence(mk(_empty), ["ENG"])
    assert snap["deployment"] == "cloud"


# ===========================================================================
# spaces area
# ===========================================================================

def _spaces_cloud(req):
    if req.url.path == "/wiki/api/v2/spaces":
        return httpx.Response(200, json={"results": [
            {"id": "1", "key": "ENG", "name": "Engineering",
             "type": "global", "status": "current", "homepageId": "111"},
            {"id": "2", "key": "OLD", "name": "Old Space",
             "type": "global", "status": "archived"},
            {"id": "3", "key": "~jdoe", "name": "John Doe (Personal)",
             "type": "personal", "status": "current", "homepageId": "9"},
            {"id": "4", "key": "~asmith", "name": "Anna Smith",
             "type": "personal", "status": "archived"},
        ], "_links": {}})
    return None


def test_spaces_cloud_shape():
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    sp = snap["areas"]["spaces"]
    assert sp["error"] is None
    # by_space holds GLOBAL spaces only.
    assert set(sp["by_space"].keys()) == {"ENG", "OLD"}
    eng = sp["by_space"]["ENG"]
    assert eng["name"] == "Engineering"
    assert eng["type"] == "global"
    assert eng["status"] == "current"
    assert eng["has_homepage"] is True
    assert eng["page_count"] == 0          # CQL count from _empty
    # OLD: archived global, no homepage.
    assert sp["by_space"]["OLD"]["has_homepage"] is False
    assert sp["by_space"]["OLD"]["status"] == "archived"
    # Aggregates: 4 spaces total, 2 personal, 2 archived.
    assert sp["count"] == 4
    assert sp["personal_count"] == 2
    assert sp["archived_count"] == 2


def test_spaces_page_count_populated_from_cql():
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        if str(req.url.path).endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            # Only the per-space page count query mentions ENG.
            if 'space="ENG"' in cql and "type=page" in cql:
                return httpx.Response(200, json={"totalSize": 17})
            return httpx.Response(200, json={"totalSize": 0})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    assert snap["areas"]["spaces"]["by_space"]["ENG"]["page_count"] == 17


def test_spaces_error_preserved():
    def handler(req):
        if req.url.path == "/wiki/api/v2/spaces":
            return httpx.Response(403, json={"_error": "denied"})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    sp = snap["areas"]["spaces"]
    assert sp["error"] is not None
    # Shape preserved even on error.
    assert "by_space" in sp


# ===========================================================================
# space_permissions area
# ===========================================================================

def test_space_permissions_cloud_shape():
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        p = req.url.path
        if p == "/wiki/api/v2/spaces/1/permissions":      # ENG
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "read", "targetType": "space"}},
                {"principal": {"type": "user", "id": "u1"},
                 "operation": {"key": "administer", "targetType": "space"}},
            ], "_links": {}})
        if p == "/wiki/api/v2/spaces/2/permissions":      # OLD
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "group", "id": "g2"},
                 "operation": {"key": "read", "targetType": "space"}},
            ], "_links": {}})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    perms = snap["areas"]["space_permissions"]
    assert perms["error"] is None
    bs = perms["by_space"]
    # Only GLOBAL spaces probed.
    assert set(bs.keys()) == {"ENG", "OLD"}
    eng = bs["ENG"]
    assert set(eng["principal_types"]) == {"group", "user"}
    assert set(eng["operations"]) == {"read", "administer"}
    assert eng["has_admin"] is True          # administer present
    # On Cloud the v2 permissions API has no anonymous principal type, so the
    # anonymous dimension is UNEVALUABLE (None) — never a concrete False, which
    # would read as "confirmed no public access" (review Bug 2).
    assert eng["anonymous"] is None
    # OLD has only read -> no admin.
    assert bs["OLD"]["has_admin"] is False


def test_space_permissions_partial_failure_stays_evaluable_and_discloses():
    # One space's permission probe fails (503) while another succeeds. A PARTIAL
    # failure must NOT error the whole area — that would gate off the checks and
    # silently drop real security findings on the spaces we DID read. The area
    # stays evaluable (error None), keeps the readable space, and records the
    # partial failure for disclosure.
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        p = req.url.path
        if p == "/wiki/api/v2/spaces/1/permissions":      # ENG ok
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "anonymous", "id": "anon"},
                 "operation": {"key": "create", "targetType": "space"}}],
                "_links": {}})
        if p == "/wiki/api/v2/spaces/2/permissions":      # OLD fails
            return httpx.Response(503, text="boom")
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    perms = snap["areas"]["space_permissions"]
    assert "ENG" in perms["by_space"], "the readable space must be kept"
    assert perms["error"] is None, "a partial failure must NOT error the area"
    assert perms.get("probe_error"), "the partial failure must be recorded for disclosure"


def test_space_permissions_total_failure_errors_the_area():
    # When EVERY probed space fails, there is nothing to evaluate -> the area
    # errors (loud), exactly as before.
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        if req.url.path.endswith("/permissions"):
            return httpx.Response(503, text="boom")
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    perms = snap["areas"]["space_permissions"]
    assert not perms["by_space"]
    assert perms["error"], "a total failure must still error the area"


def test_space_permissions_dc_anonymous_detected():
    def handler(req):
        p = req.url.path
        if p == "/rest/api/space":
            return httpx.Response(200, json={"results": [
                {"id": 1, "key": "PUB", "name": "Public Space",
                 "type": "global", "status": "current"},
            ], "_links": {}})
        if p == "/rest/api/space/PUB/permission":
            return httpx.Response(200, json=[
                {"operation": "read",
                 "subjects": {"group": {"results": [{"name": "confluence-users"}]}},
                 "anonymousAccess": True},
            ])
        return _empty(req)
    snap = gather_confluence(mk(handler, "dc"), ["PUB"])
    perms = snap["areas"]["space_permissions"]
    assert perms["error"] is None or "skipped" not in perms
    bs = perms["by_space"]
    assert bs["PUB"]["anonymous"] is True
    assert "anonymous" in bs["PUB"]["principal_types"]


def test_space_permissions_dc_group_grants_named():
    """DC: the reduced per-space dict aggregates the granted group NAMES into
    group_grants (config identifiers used by the empty-group cross-reference)."""
    def handler(req):
        p = req.url.path
        if p == "/rest/api/space":
            return httpx.Response(200, json={"results": [
                {"id": 1, "key": "ENG", "name": "Engineering",
                 "type": "global", "status": "current"}], "_links": {}})
        if p == "/rest/api/space/ENG/permission":
            return httpx.Response(200, json=[
                {"operation": "read",
                 "subjects": {"group": {"results": [{"name": "ghost-team"}]}},
                 "anonymousAccess": False},
                {"operation": "create",
                 "subjects": {"group": {"results": [{"name": "ghost-team"}]},
                              "user": {"results": [{"accountId": "x"}]}},
                 "anonymousAccess": False}])
        return _empty(req)
    snap = gather_confluence(mk(handler, "dc"), ["ENG"])
    bs = snap["areas"]["space_permissions"]["by_space"]
    # de-duplicated, sorted group names — never a user identity.
    assert bs["ENG"]["group_grants"] == ["ghost-team"]


def test_space_permissions_cloud_group_grants_none_when_id_only():
    """Cloud: a group grant whose principal is an opaque id (no name) reduces to
    group_grants == None — the signal the checks layer turns into a
    capability_gap (group grants exist but can't be cross-referenced)."""
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        p = req.url.path
        if p == "/wiki/api/v2/spaces/1/permissions":      # ENG
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "read", "targetType": "space"}}],
                "_links": {}})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    bs = snap["areas"]["space_permissions"]["by_space"]
    assert bs["ENG"]["group_grants"] is None


def test_space_permissions_cloud_group_grants_empty_when_no_group():
    """Cloud: a space with only user/anonymous grants (no group principal) has
    group_grants == [] — nothing to cross-reference, and NOT a capability_gap."""
    def handler(req):
        r = _spaces_cloud(req)
        if r is not None:
            return r
        p = req.url.path
        if p == "/wiki/api/v2/spaces/1/permissions":      # ENG
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "user", "id": "u1"},
                 "operation": {"key": "read", "targetType": "space"}}],
                "_links": {}})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    bs = snap["areas"]["space_permissions"]["by_space"]
    assert bs["ENG"]["group_grants"] == []


# ===========================================================================
# groups area
# ===========================================================================

def test_groups_cloud_shape():
    def handler(req):
        p = req.url.path
        if p.endswith("/rest/api/group"):
            return httpx.Response(200, json={"results": [
                {"name": "confluence-users", "id": "g1"},
                {"name": "site-admins", "id": "g2"},
            ], "_links": {}})
        if "/member" in p:
            return httpx.Response(200, json={"results": [{}, {}], "size": 2})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    g = snap["areas"]["groups"]
    assert g["error"] is None
    assert set(g["names"]) == {"confluence-users", "site-admins"}
    assert g["count"] == 2
    assert g["capped"] is False
    assert g["member_counts"]["confluence-users"] == 2


def test_groups_error_preserved():
    def handler(req):
        if req.url.path.endswith("/rest/api/group"):
            return httpx.Response(500, json={"_error": "down"})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    g = snap["areas"]["groups"]
    assert g["error"] is not None
    assert "names" in g and "member_counts" in g


# ===========================================================================
# templates + labels areas
# ===========================================================================

def test_templates_and_labels_counts():
    def handler(req):
        p = req.url.path
        if p.endswith("/template/page"):
            return httpx.Response(200, json={"results": [{}, {}, {}], "_links": {}})
        if p.endswith("/template/blueprint"):
            return httpx.Response(200, json={"results": [{}], "_links": {}})
        if p.endswith("/label"):
            return httpx.Response(200, json={"results": [{}, {}], "_links": {}})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    t = snap["areas"]["templates"]
    assert t["global_count"] == 3
    assert t["blueprint_count"] == 1
    assert t["error"] is None
    assert snap["areas"]["labels"]["global_count"] == 2


# ===========================================================================
# content_quality area
# ===========================================================================

def test_content_quality_cql_counts():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            if "status=draft" in cql:
                return httpx.Response(200, json={"totalSize": 5})
            if "lastmodified" in cql:
                return httpx.Response(200, json={"totalSize": 30})
            if "type=page" in cql:
                return httpx.Response(200, json={"totalSize": 100})
            return httpx.Response(200, json={"totalSize": 0})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    cq = snap["areas"]["content_quality"]
    assert cq["error"] is None
    assert cq["pages_total"] == 100
    assert cq["stale_pages"] == 30
    assert cq["drafts"] == 5


def test_content_quality_drafts_400_yields_none():
    """A draft CQL that 400s (dialect mismatch) -> drafts None, not an abort."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            if "status=draft" in cql:
                return httpx.Response(400, json={"_error": "bad field"})
            if "type=page" in cql:
                return httpx.Response(200, json={"totalSize": 10})
            return httpx.Response(200, json={"totalSize": 0})
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    cq = snap["areas"]["content_quality"]
    assert cq["drafts"] is None
    assert cq["pages_total"] == 10
    # The whole area did not abort.
    assert cq["error"] is None


# ===========================================================================
# Per-area isolation: one area failing never aborts the gather
# ===========================================================================

def test_one_area_failure_does_not_abort_gather():
    def handler(req):
        if req.url.path.endswith("/rest/api/group"):
            raise httpx.ConnectError("boom")
        return _empty(req)
    snap = gather_confluence(mk(handler), ["ENG"])
    # groups area carries an error...
    assert snap["areas"]["groups"]["error"] is not None
    # ...but other areas still resolved.
    assert "spaces" in snap["areas"]
    assert snap["areas"]["spaces"]["error"] is None


# ===========================================================================
# PRIVACY (I1) — leak tests
# ===========================================================================

def test_privacy_no_page_title_no_personal_key_no_identity_cloud():
    """Inject a page title, a space-admin/user identity, a group member list,
    a personal-space key/name, and an accountId into the raw responses; assert
    NONE appear in the snapshot, and personal spaces feed only personal_count."""
    def handler(req):
        p = req.url.path
        if p == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [
                {"id": "1", "key": "ENG", "name": "Engineering",
                 "type": "global", "status": "current", "homepageId": "111"},
                # PERSONAL space: key + name embed a username -> must NEVER appear.
                {"id": "9", "key": "~secretuser",
                 "name": "Secret User Personal Space",
                 "type": "personal", "status": "current",
                 "authorId": "acc-personal-OWNER"},
            ], "_links": {}})
        if p == "/wiki/api/v2/spaces/1/permissions":
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "user", "id": "acc-ADMIN-77"},
                 "operation": {"key": "administer", "targetType": "space"}},
            ], "_links": {}})
        if p.endswith("/rest/api/group"):
            return httpx.Response(200, json={"results": [
                {"name": "confluence-users", "id": "g1"}], "_links": {}})
        if "/member" in p:
            return httpx.Response(200, json={"results": [
                {"accountId": "acc-MEMBER-55", "displayName": "Mallory Member"}],
                "size": 1})
        if p.endswith("/rest/api/search"):
            # Hostile: stuff a page title into the count envelope.
            return httpx.Response(200, json={
                "totalSize": 3,
                "results": [{"title": "LEAKED SECRET PAGE TITLE",
                             "body": "secret body text"}]})
        return _empty(req)

    snap = gather_confluence(mk(handler), ["ENG"])
    blob = json.dumps(snap)
    # Page content
    assert "LEAKED SECRET PAGE TITLE" not in blob
    assert "secret body text" not in blob
    # Personal-space key + name (username-bearing)
    assert "~secretuser" not in blob
    assert "Secret User Personal Space" not in blob
    assert "acc-personal-OWNER" not in blob
    # Space-admin / user identity
    assert "acc-ADMIN-77" not in blob
    # Group member identities
    assert "acc-MEMBER-55" not in blob
    assert "Mallory Member" not in blob

    sp = snap["areas"]["spaces"]
    # The personal space contributed to personal_count ONLY.
    assert sp["personal_count"] == 1
    assert "~secretuser" not in sp["by_space"]
    assert set(sp["by_space"].keys()) == {"ENG"}
    # Global space name is allowed (config identifier).
    assert "Engineering" in blob
    assert "ENG" in blob


def test_privacy_dc_admin_name_absent():
    """DC permission subjects carry admin/user names; the snapshot stores only
    principal TYPES, never the subject names."""
    def handler(req):
        p = req.url.path
        if p == "/rest/api/space":
            return httpx.Response(200, json={"results": [
                {"id": 1, "key": "ENG", "name": "Engineering",
                 "type": "global", "status": "current"}], "_links": {}})
        if p == "/rest/api/space/ENG/permission":
            return httpx.Response(200, json=[
                {"operation": "administer",
                 "subjects": {"user": {"results": [
                     {"accountId": "acc-DC-ADMIN", "displayName": "Adam Admin"}]}},
                 "anonymousAccess": False},
            ])
        return _empty(req)
    snap = gather_confluence(mk(handler, "dc"), ["ENG"])
    blob = json.dumps(snap)
    assert "acc-DC-ADMIN" not in blob
    assert "Adam Admin" not in blob
    # But the capability (has_admin) is recorded.
    assert snap["areas"]["space_permissions"]["by_space"]["ENG"]["has_admin"] is True


# ===========================================================================
# PARALLELISM — bounded thread pool (perf) with byte-identical output.
# The Confluence gather runs its independent per-space page counts, per-space
# permission probes, content_quality CQL counts, and top-level area fetches
# concurrently. These tests pin the hard invariant: identical snapshot
# regardless of worker count, per-object error still captured (no false clean),
# and a bounded/configurable pool width via MA_GATHER_WORKERS.
# ===========================================================================

import threading
from auditor.envaudit import _pool


def _rich_cloud_handler():
    """A Cloud handler with several global spaces (each with its own page count
    + permission set), groups with member probes, templates/labels, and
    content_quality CQL counts — enough per-object reads to exercise the
    parallel merge paths."""
    spaces = [
        {"id": "1", "key": "ENG", "name": "Engineering",
         "type": "global", "status": "current", "homepageId": "111"},
        {"id": "2", "key": "HR", "name": "Human Resources",
         "type": "global", "status": "current", "homepageId": "222"},
        {"id": "3", "key": "OPS", "name": "Operations",
         "type": "global", "status": "archived"},
        {"id": "9", "key": "~personal", "name": "Personal",
         "type": "personal", "status": "current"},
    ]
    page_counts = {"ENG": 17, "HR": 4, "OPS": 0}

    def handler(req):
        p = req.url.path
        if p == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": spaces, "_links": {}})
        if p == "/wiki/api/v2/spaces/1/permissions":
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "group", "id": "g1"},
                 "operation": {"key": "read"}},
                {"principal": {"type": "user", "id": "u1"},
                 "operation": {"key": "administer"}}], "_links": {}})
        if p == "/wiki/api/v2/spaces/2/permissions":
            return httpx.Response(200, json={"results": [
                {"principal": {"type": "group", "id": "g2"},
                 "operation": {"key": "read"}}], "_links": {}})
        if p == "/wiki/api/v2/spaces/3/permissions":
            return httpx.Response(200, json={"results": [], "_links": {}})
        if p.endswith("/rest/api/group"):
            return httpx.Response(200, json={"results": [
                {"name": "confluence-users", "id": "g1"},
                {"name": "site-admins", "id": "g2"}], "_links": {}})
        if "/member" in p:
            return httpx.Response(200, json={"results": [{}], "size": 1})
        if p.endswith("/template/page"):
            return httpx.Response(200, json={"results": [{}, {}], "_links": {}})
        if p.endswith("/template/blueprint"):
            return httpx.Response(200, json={"results": [{}], "_links": {}})
        if p.endswith("/label"):
            return httpx.Response(200, json={"results": [{}, {}, {}], "_links": {}})
        if str(p).endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            for key, n in page_counts.items():
                if f'space="{key}"' in cql and "type=page" in cql:
                    return httpx.Response(200, json={"totalSize": n})
            if "status=draft" in cql:
                return httpx.Response(200, json={"totalSize": 6})
            if "lastmodified" in cql:
                return httpx.Response(200, json={"totalSize": 12})
            if "type=page" in cql:
                return httpx.Response(200, json={"totalSize": 99})
            return httpx.Response(200, json={"totalSize": 0})
        return _empty(req)

    return handler


def test_confluence_gather_equivalence_seq_vs_parallel(monkeypatch):
    """HARD INVARIANT: the Confluence snapshot is byte-for-byte identical with
    1 worker (sequential) and 10 workers against the SAME handler."""
    handler = _rich_cloud_handler()

    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    snap_seq = gather_confluence(mk(handler), ["ENG", "HR"])

    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    snap_par = gather_confluence(mk(handler), ["ENG", "HR"])

    assert json.dumps(snap_seq, sort_keys=True) == \
        json.dumps(snap_par, sort_keys=True)
    # Sanity: the parallel paths actually ran with real per-space data.
    bs = snap_par["areas"]["spaces"]["by_space"]
    assert set(bs.keys()) == {"ENG", "HR", "OPS"}
    assert bs["ENG"]["page_count"] == 17


def test_confluence_per_space_perm_error_captured_under_concurrency(monkeypatch):
    """One space's permission probe failing under concurrency must be CAPTURED
    (no false clean) while the OTHER spaces still merge in AND stay evaluable.
    A partial failure records `probe_error` (disclosed downstream) but does NOT
    error the whole area, which would silently drop findings on the read spaces."""
    handler = _rich_cloud_handler()

    def failing(req):
        if req.url.path == "/wiki/api/v2/spaces/2/permissions":   # HR fails
            return httpx.Response(500, json={"_error": "boom"})
        return handler(req)

    monkeypatch.setenv("MA_GATHER_WORKERS", "10")
    snap = gather_confluence(mk(failing), ["ENG", "HR"])
    perms = snap["areas"]["space_permissions"]
    assert perms["probe_error"] is not None   # failure captured, not swallowed
    assert perms["error"] is None             # ...but the area stays evaluable
    bs = perms["by_space"]
    assert "ENG" in bs                        # sibling still merged + checked
    assert "HR" not in bs                     # the failed space is absent


def test_confluence_gather_determinism_keys(monkeypatch):
    """by_space dicts carry the same keys and sorted type lists regardless of
    worker count."""
    handler = _rich_cloud_handler()
    monkeypatch.setenv("MA_GATHER_WORKERS", "6")
    snap = gather_confluence(mk(handler), ["ENG"])
    assert set(snap["areas"]["spaces"]["by_space"].keys()) == {"ENG", "HR", "OPS"}
    eng = snap["areas"]["space_permissions"]["by_space"]["ENG"]
    assert eng["principal_types"] == sorted(eng["principal_types"])
    assert eng["operations"] == sorted(eng["operations"])


def test_confluence_gather_worker_count_env_override(monkeypatch):
    """MA_GATHER_WORKERS honored + clamped to >= 1 (shared _pool helper)."""
    monkeypatch.delenv("MA_GATHER_WORKERS", raising=False)
    assert _pool.worker_count() == _pool.MAX_WORKERS
    monkeypatch.setenv("MA_GATHER_WORKERS", "5")
    assert _pool.worker_count() == 5
    monkeypatch.setenv("MA_GATHER_WORKERS", "0")
    assert _pool.worker_count() == 1


def test_confluence_gather_pool_is_bounded(monkeypatch):
    """The Confluence pool never exceeds the configured width: with a cap of 2
    workers, no more than 2 per-space page-count reads overlap at once."""
    handler = _rich_cloud_handler()
    in_flight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def counting(req):
        is_page_count = (str(req.url.path).endswith("/rest/api/search")
                         and "type=page" in dict(req.url.params).get("cql", "")
                         and 'space=' in dict(req.url.params).get("cql", ""))
        if is_page_count:
            with lock:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
            try:
                import time as _t
                _t.sleep(0.02)
                return handler(req)
            finally:
                with lock:
                    in_flight["now"] -= 1
        return handler(req)

    monkeypatch.setenv("MA_GATHER_WORKERS", "2")
    gather_confluence(mk(counting), ["ENG"])
    assert in_flight["max"] <= 2, f"peak in-flight {in_flight['max']} exceeded cap 2"
    assert in_flight["max"] >= 2   # actually parallelized


# ===========================================================================
# MIGRATION: orphaned-page hierarchy + risky-macro areas
# ===========================================================================

def test_orphan_pages_computed_from_homepage_subtree():
    """orphan_pages = total pages − descendants of the homepage − the homepage.
    The homepage CONTENT ID is used transiently and must never reach the snapshot."""
    def h(req):
        p = str(req.url.path)
        if p == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [
                {"key": "DOCS", "name": "Docs", "id": "1", "type": "global",
                 "status": "current", "homepageId": "500"}], "_links": {}})
        if p.endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            if "ancestor=500" in cql:
                return httpx.Response(200, json={"totalSize": 70, "results": []})
            if 'space="DOCS"' in cql and "type=page" in cql:
                return httpx.Response(200, json={"totalSize": 100, "results": []})
            return httpx.Response(200, json={"totalSize": 0, "results": []})
        return httpx.Response(200, json={"results": [], "_links": {}})

    snap = gather_confluence(mk(h), ["DOCS"])
    s = snap["areas"]["spaces"]["by_space"]["DOCS"]
    assert s["page_count"] == 100
    assert s["orphan_pages"] == 29           # 100 − 70 − 1
    # PRIVACY: the homepage content id is transient — never stored in by_space
    # nor anywhere in the serialized snapshot.
    assert "homepage_id" not in s
    assert "500" not in json.dumps(snap)


def test_orphan_pages_none_without_homepage():
    def h(req):
        p = str(req.url.path)
        if p == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [
                {"key": "NOHP", "name": "NoHome", "id": "1", "type": "global",
                 "status": "current"}], "_links": {}})   # no homepageId
        if p.endswith("/rest/api/search"):
            return httpx.Response(200, json={"totalSize": 50, "results": []})
        return httpx.Response(200, json={"results": [], "_links": {}})

    s = gather_confluence(mk(h), ["NOHP"])["areas"]["spaces"]["by_space"]["NOHP"]
    assert s["has_homepage"] is False
    assert s["orphan_pages"] is None         # no homepage → unevaluable


def test_macros_area_counts_risky_macros():
    def h(req):
        p = str(req.url.path)
        if p.endswith("/rest/api/search"):
            cql = dict(req.url.params).get("cql", "")
            if 'macro="gliffy"' in cql:
                return httpx.Response(200, json={"totalSize": 12, "results": []})
            if 'macro="chart"' in cql:
                return httpx.Response(200, json={"totalSize": 3, "results": []})
            return httpx.Response(200, json={"totalSize": 0, "results": []})
        return httpx.Response(200, json={"results": [], "_links": {}})

    m = gather_confluence(mk(h), ["ENG"])["areas"]["macros"]
    assert m["error"] is None
    assert m["by_macro"]["gliffy"] == 12
    assert m["by_macro"]["chart"] == 3
    assert m["by_macro"]["drawio"] == 0       # probed, zero usage


def test_macros_area_error_when_all_probes_fail():
    def h(req):
        if str(req.url.path).endswith("/rest/api/search"):
            return httpx.Response(400, json={"message": "bad cql"})
        return httpx.Response(200, json={"results": [], "_links": {}})

    m = gather_confluence(mk(h), ["ENG"])["areas"]["macros"]
    assert m["error"] is not None              # no false clean
    assert all(v is None for v in m["by_macro"].values())


# --- page_restrictions probe (counts only, never identities) ----------------
from auditor.envaudit.confluence_gather import (   # noqa: E402
    _gather_restrictions, _SPACE_PERM_CAP, _RESTRICTION_PAGE_CAP)


class _FakeRestrClient:
    def __init__(self, results):
        self._r = results  # key -> (probed, restricted, evaluable, truncated, err)

    def restricted_page_sample(self, key, cap=100):
        return self._r.get(key, (0, 0, False, False, None))


def test_gather_restrictions_reduces_to_counts():
    client = _FakeRestrClient({
        "ENG": (50, 3, True, True, None),    # more pages remained -> page_capped
        "HR": (10, 0, True, False, None)})   # fully drained
    area = _gather_restrictions(
        client, dc=False, global_spaces=[{"key": "ENG"}, {"key": "HR"}])
    assert area["by_space"]["ENG"] == {
        "restricted": 3, "probed": 50, "evaluable": True, "page_capped": True}
    assert area["by_space"]["HR"] == {
        "restricted": 0, "probed": 10, "evaluable": True, "page_capped": False}
    assert area["error"] is None and "capped" not in area


def test_gather_restrictions_space_cap_flagged():
    spaces = [{"key": f"S{i}"} for i in range(_SPACE_PERM_CAP + 5)]
    client = _FakeRestrClient({})
    area = _gather_restrictions(client, dc=False, global_spaces=spaces)
    assert area["capped"] is True
    assert len(area["by_space"]) == _SPACE_PERM_CAP   # only the first cap probed


def test_gather_restrictions_probe_error_marks_area_not_space():
    client = _FakeRestrClient({"ENG": (0, 0, False, False, "ERR503:down")})
    area = _gather_restrictions(client, dc=False, global_spaces=[{"key": "ENG"}])
    assert area["error"] == "ERR503:down"
    assert "ENG" not in area["by_space"]      # errored space omitted, not clean
