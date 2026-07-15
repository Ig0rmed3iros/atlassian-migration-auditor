import json

import httpx
import pytest
from auditor.client import Connection, JiraClient
from auditor import cloneaccess as ca


def mk(handler):
    conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def test_looks_like_account_id():
    assert ca.looks_like_account_id("557058:1f2e3d4c-5b6a-7980-1234-567890abcdef")
    assert ca.looks_like_account_id("5b39d0d03aa72d2ded7dddd4")     # 24-hex legacy
    assert not ca.looks_like_account_id("someone@example.com")
    assert not ca.looks_like_account_id("Jane Doe")


def test_resolve_accountid_passthrough_no_http():
    # An accountId resolves without any HTTP call.
    def handler(req):
        raise AssertionError("should not call the API for an accountId")
    out = ca.resolve_identity(mk(handler), "5b39d0d03aa72d2ded7dddd4")
    assert out["status"] == "resolved"
    assert out["account_id"] == "5b39d0d03aa72d2ded7dddd4"


def test_resolve_email_hit():
    def handler(req):
        return httpx.Response(200, json=[{"accountId": "acc-9", "accountType":
            "atlassian", "active": True, "emailAddress": "a@b.c"}])
    out = ca.resolve_identity(mk(handler), "a@b.c")
    assert out["status"] == "resolved" and out["account_id"] == "acc-9"


def test_resolve_email_unresolved_and_ambiguous():
    def none_h(req): return httpx.Response(200, json=[])
    out = ca.resolve_identity(mk(none_h), "ghost@b.c")
    assert out["status"] == "unresolved" and out["account_id"] is None

    def two_h(req):
        return httpx.Response(200, json=[
            {"accountId": "a1", "accountType": "atlassian", "active": True},
            {"accountId": "a2", "accountType": "atlassian", "active": True}])
    out = ca.resolve_identity(mk(two_h), "dup@b.c")
    assert out["status"] == "ambiguous" and out["account_id"] is None


def _role_scan_handler():
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [
                {"key": "AC"}, {"key": "MS"}], "isLast": True, "total": 2})
        if p.endswith("/project/AC/role"):
            return httpx.Response(200, json={
                "Administrators": "https://x.atlassian.net/rest/api/3/project/AC/role/10002"})
        if p.endswith("/project/MS/role"):
            return httpx.Response(200, json={
                "Members": "https://x.atlassian.net/rest/api/3/project/MS/role/10001"})
        if p.endswith("/project/AC/role/10002"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "main-1"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "g1"}}]})
        if p.endswith("/project/MS/role/10001"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "main-1"}},
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "clone-1"}}]})
        return httpx.Response(404)
    return handler


def test_build_role_index_user_actors_only(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    idx = ca.build_role_index(mk(_role_scan_handler()))
    # main-1 is a direct user actor in AC/Administrators and MS/Members
    roles = sorted((r["project"], r["role"]) for r in idx["main-1"])
    assert roles == [("AC", "Administrators"), ("MS", "Members")]
    # group actor (g1) is NOT indexed; clone-1 only in MS/Members
    assert [(r["project"], r["role"]) for r in idx["clone-1"]] == [("MS", "Members")]


def test_build_role_index_seq_vs_parallel_identical(monkeypatch):
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    seq = ca.build_role_index(mk(_role_scan_handler()))
    monkeypatch.setenv("MA_GATHER_WORKERS", "8")
    par = ca.build_role_index(mk(_role_scan_handler()))
    assert seq == par


def _role_scan_handler_unordered():
    """Projects in non-alphabetical order (ZZ, AA, MM) with the same account
    appearing as a direct user actor in roles across all three, so that an
    input-order vs sorted-order bug would produce divergent results between
    sequential and parallel workers."""
    def handler(req):
        p = str(req.url.path)
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [
                {"key": "ZZ"}, {"key": "AA"}, {"key": "MM"}],
                "isLast": True, "total": 3})
        if p.endswith("/project/ZZ/role"):
            return httpx.Response(200, json={
                "Developers": "https://x.atlassian.net/rest/api/3/project/ZZ/role/10010"})
        if p.endswith("/project/AA/role"):
            return httpx.Response(200, json={
                "Administrators": "https://x.atlassian.net/rest/api/3/project/AA/role/10002"})
        if p.endswith("/project/MM/role"):
            return httpx.Response(200, json={
                "Members": "https://x.atlassian.net/rest/api/3/project/MM/role/10001"})
        if p.endswith("/project/ZZ/role/10010"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "shared-user"}},
                {"type": "atlassian-group-role-actor", "actorGroup": {"name": "devs"}}]})
        if p.endswith("/project/AA/role/10002"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "shared-user"}}]})
        if p.endswith("/project/MM/role/10001"):
            return httpx.Response(200, json={"actors": [
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "shared-user"}},
                {"type": "atlassian-user-role-actor", "actorUser": {"accountId": "other-user"}}]})
        return httpx.Response(404)
    return handler


def test_build_role_index_seq_vs_parallel_identical_unordered(monkeypatch):
    """Non-alphabetical project key ordering (ZZ, AA, MM) must produce the
    same index regardless of worker count — catches input-order vs sort-order bugs."""
    monkeypatch.setenv("MA_GATHER_WORKERS", "1")
    seq = ca.build_role_index(mk(_role_scan_handler_unordered()))
    monkeypatch.setenv("MA_GATHER_WORKERS", "8")
    par = ca.build_role_index(mk(_role_scan_handler_unordered()))
    assert seq == par
    # shared-user appears in all three projects (ZZ/Developers, AA/Administrators, MM/Members)
    roles = [(r["project"], r["role"]) for r in seq["shared-user"]]
    assert roles == [("AA", "Administrators"), ("MM", "Members"), ("ZZ", "Developers")]
    # other-user only appears in MM/Members
    assert [(r["project"], r["role"]) for r in seq["other-user"]] == [("MM", "Members")]


def test_plan_pair_groups_additive_diff():
    def handler(req):
        aid = req.url.params.get("accountId")
        if aid == "main-1":
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"},
                                             {"name": "g2", "groupId": "gid2"}])
        if aid == "clone-1":
            return httpx.Response(200, json=[{"name": "g2", "groupId": "gid2"}])
        return httpx.Response(404)
    role_index = {"main-1": [{"project": "AC", "role_id": "10002", "role": "Administrators"},
                             {"project": "MS", "role_id": "10001", "role": "Members"}],
                  "clone-1": [{"project": "MS", "role_id": "10001", "role": "Members"}]}
    plan = ca.plan_pair(mk(handler), "main-1", "clone-1", role_index)
    assert [g["groupId"] for g in plan["groups_add"]] == ["gid1"]   # g2 already
    assert plan["groups_already"] == ["g2"]
    assert [r["role"] for r in plan["roles_add"]] == ["Administrators"]  # MS already
    assert plan["roles_already"] == ["MS/Members"]
    assert plan["roles_scanned"] is True


def test_plan_pair_without_role_index_skips_roles():
    def handler(req):
        return httpx.Response(200, json=[])    # both users have no groups
    plan = ca.plan_pair(mk(handler), "main-1", "clone-1", role_index=None)
    assert plan["groups_add"] == [] and plan["roles_add"] == []
    assert plan["roles_scanned"] is False


def _apply_handler(writes):
    """Records POST writes; serves groups so main has g1 (clone lacks it)."""
    def handler(req):
        p = str(req.url.path); m = req.method
        if p.endswith("/user/search"):
            # emails resolve to <local>-id
            q = req.url.params.get("query")
            return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
                "accountType": "atlassian", "active": True, "emailAddress": q}])
        if p.endswith("/user/groups"):
            aid = req.url.params.get("accountId")
            return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"}]
                                  if aid == "main-id" else [])
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [], "isLast": True, "total": 0})
        if m == "POST" and p.endswith("/group/user"):
            writes.append(("group", req.url.params.get("groupId"),
                           json.loads(req.content)["accountId"]))
            return httpx.Response(201, json={})
        return httpx.Response(200, json={})
    return handler


def test_run_clone_dry_run_writes_nothing():
    writes = []
    rep = ca.run_clone(mk(_apply_handler(writes)),
                       [("main@x.y", "clone@x.y")],
                       dry_run=True, scan_roles=False)
    assert writes == []
    pair = rep["pairs"][0]
    assert pair["status"] == "ok"
    assert [g for g in pair["groups"]["added"]] == ["g1"]   # planned, not written
    assert rep["summary"]["groups_added"] == 1


def test_run_clone_apply_writes_missing_group_idempotently():
    writes = []
    rep = ca.run_clone(mk(_apply_handler(writes)),
                       [("main@x.y", "clone@x.y")],
                       dry_run=False, scan_roles=False)
    assert writes == [("group", "gid1", "clone-id")]
    assert rep["pairs"][0]["groups"]["added"] == ["g1"]
    assert rep["summary"]["failed"] == 0


def test_run_clone_blocks_unresolved_and_noops_self():
    def handler(req):
        if str(req.url.path).endswith("/user/search"):
            return httpx.Response(200, json=[])       # nothing resolves
        return httpx.Response(200, json=[])
    rep = ca.run_clone(mk(handler), [("ghost@x.y", "who@x.y")],
                       dry_run=True, scan_roles=False)
    assert rep["pairs"][0]["status"] == "blocked"
    assert rep["summary"]["blocked"] == 1

    # self-clone (same accountId both sides) -> noop, no writes
    rep2 = ca.run_clone(mk(handler), [("5b39d0d03aa72d2ded7dddd4",
                                       "5b39d0d03aa72d2ded7dddd4")],
                        dry_run=False, scan_roles=False)
    assert rep2["pairs"][0]["status"] == "noop"


def test_breaker_trips_and_attaches_partial(monkeypatch):
    """Repeated 500s on POST /group/user trip the circuit breaker; partial report attached."""
    monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")

    def handler(req):
        p = str(req.url.path)
        m = req.method
        if p.endswith("/user/search"):
            q = req.url.params.get("query")
            return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
                "accountType": "atlassian", "active": True, "emailAddress": q}])
        if p.endswith("/user/groups"):
            aid = req.url.params.get("accountId")
            if aid == "main-id":
                return httpx.Response(200, json=[
                    {"name": "g1", "groupId": "gid1"},
                    {"name": "g2", "groupId": "gid2"},
                    {"name": "g3", "groupId": "gid3"},
                ])
            return httpx.Response(200, json=[])
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [], "isLast": True, "total": 0})
        if m == "POST" and p.endswith("/group/user"):
            return httpx.Response(500, json={"_error": "internal server error"})
        return httpx.Response(200, json={})

    with pytest.raises(ca.CloneAborted) as exc_info:
        ca.run_clone(mk(handler), [("main@x.y", "clone@x.y")],
                     dry_run=False, scan_roles=False)

    partial = exc_info.value.partial
    assert isinstance(partial, dict)
    assert "summary" in partial
    assert partial["summary"]["pairs"] >= 1


def test_already_member_not_counted_as_failure():
    """A 400 'already a member' response lands in groups['already'], not ['failed']."""

    def handler(req):
        p = str(req.url.path)
        m = req.method
        if p.endswith("/user/search"):
            q = req.url.params.get("query")
            return httpx.Response(200, json=[{"accountId": q.split("@")[0] + "-id",
                "accountType": "atlassian", "active": True, "emailAddress": q}])
        if p.endswith("/user/groups"):
            aid = req.url.params.get("accountId")
            if aid == "main-id":
                return httpx.Response(200, json=[{"name": "g1", "groupId": "gid1"}])
            return httpx.Response(200, json=[])
        if p.endswith("/project/search"):
            return httpx.Response(200, json={"values": [], "isLast": True, "total": 0})
        if m == "POST" and p.endswith("/group/user"):
            return httpx.Response(400, json={"_error": "user is already a member of the group"})
        return httpx.Response(200, json={})

    rep = ca.run_clone(mk(handler), [("main@x.y", "clone@x.y")],
                       dry_run=False, scan_roles=False)
    pair = rep["pairs"][0]
    assert "g1" in pair["groups"]["already"]
    assert pair["groups"]["failed"] == []
    assert rep["summary"]["failed"] == 0
