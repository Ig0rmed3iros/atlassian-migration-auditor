import httpx
import pytest

from auditor.client import ClientError, Connection
from auditor.confluence.client import ConfluenceClient


def mk_conf(handler, deployment="cloud"):
    if deployment == "cloud":
        conn = Connection(auth_type="pat", site_url="https://acme.atlassian.net",
                          email="igor@acme.example", api_token="tok")
    else:
        conn = Connection(auth_type="pat", site_url="https://confluence.acme.example",
                          deployment="dc", api_token="tok-123")
    return ConfluenceClient(
        conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda s: None)


def test_cloud_base_has_wiki_prefix_dc_does_not():
    seen = []
    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"displayName": "Igor Medeiros"})
    mk_conf(handler).myself()
    mk_conf(handler, deployment="dc").myself()
    assert seen[0] == "https://acme.atlassian.net/wiki/rest/api/user/current"
    assert seen[1] == "https://confluence.acme.example/rest/api/user/current"


def test_cloud_myself_falls_back_to_v2_auth_check():
    def handler(req):
        if req.url.path == "/wiki/rest/api/user/current":
            return httpx.Response(410, text="gone")
        assert req.url.path == "/wiki/api/v2/spaces"
        assert dict(req.url.params)["limit"] == "1"
        return httpx.Response(200, json={"results": []})
    me = mk_conf(handler).myself()
    assert me == {"display_name": "verified (identity API unavailable)",
                  "email": None, "account_id": None}

    # Auth failures stay loud: a 401 never reaches the fallback.
    with pytest.raises(ClientError) as ei:
        mk_conf(lambda r: httpx.Response(401, text="no")).myself()
    assert ei.value.status == 401

    # The fallback itself failing auth is loud too — a 410 on the identity
    # API must never mask bad credentials as a verified connection.
    def handler_bad(req):
        if req.url.path == "/wiki/rest/api/user/current":
            return httpx.Response(410, text="gone")
        return httpx.Response(401, text="no")
    with pytest.raises(ClientError):
        mk_conf(handler_bad).myself()


def test_dc_myself_no_v2_fallback():
    # The v2 fallback is cloud-only: a DC 404 means the site is not
    # Confluence (or the path is wrong), not an API sunset.
    with pytest.raises(ClientError) as ei:
        mk_conf(lambda r: httpx.Response(404, text="nope"), "dc").myself()
    assert ei.value.status == 404


def test_all_spaces_cloud_v2_cursor():
    def handler(req):
        assert req.url.path == "/wiki/api/v2/spaces"
        params = dict(req.url.params)
        if params.get("cursor") == "c2":
            return httpx.Response(200, json={
                "results": [{"id": "3", "key": "OPS", "name": "Globex Ops"}],
                "_links": {}})
        assert params["limit"] == "250"
        return httpx.Response(200, json={
            "results": [{"id": "1", "key": "ENG", "name": "Acme Engineering"},
                        {"id": "2", "key": "HR", "name": "Acme People"}],
            "_links": {"next": "/wiki/api/v2/spaces?cursor=c2&limit=250"}})
    rows, err = mk_conf(handler).all_spaces()
    assert err is None
    assert rows == [{"key": "ENG", "name": "Acme Engineering", "id": "1"},
                    {"key": "HR", "name": "Acme People", "id": "2"},
                    {"key": "OPS", "name": "Globex Ops", "id": "3"}]


def test_all_spaces_dc_v1_start_limit():
    def handler(req):
        assert req.url.path == "/rest/api/space"
        start = int(dict(req.url.params).get("start", 0))
        if start == 0:
            return httpx.Response(200, json={
                "results": [{"key": f"SP{i}", "name": f"Acme Space {i}", "id": i}
                            for i in range(50)],
                "start": 0, "limit": 50, "size": 50,
                "_links": {"next": "/rest/api/space?start=50&limit=50"}})
        return httpx.Response(200, json={
            "results": [{"key": "LAST", "name": "Globex Last", "id": 99}],
            "start": 50, "limit": 50, "size": 1, "_links": {}})
    rows, err = mk_conf(handler, deployment="dc").all_spaces()
    assert err is None and len(rows) == 51
    assert rows[0] == {"key": "SP0", "name": "Acme Space 0", "id": 0}
    assert rows[-1] == {"key": "LAST", "name": "Globex Last", "id": 99}


def test_all_spaces_error_is_loud():
    rows, err = mk_conf(lambda r: httpx.Response(403, text="no")).all_spaces()
    assert rows == [] and err is not None and "ERR403" in err
    rows2, err2 = mk_conf(lambda r: httpx.Response(403, text="no"),
                          deployment="dc").all_spaces()
    assert rows2 == [] and err2 is not None and "ERR403" in err2


def test_count_pages_total_size():
    seen = []
    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"totalSize": 7, "results": []})
    assert mk_conf(handler).count_pages("ENG") == 7
    assert mk_conf(handler, deployment="dc").count_pages("ENG") == 7
    assert seen[0][0] == "/wiki/rest/api/search"
    assert seen[1][0] == "/rest/api/search"
    for _, params in seen:
        assert params["cql"] == 'space="ENG" and type=page'
        assert params["limit"] == "1"
    assert mk_conf(lambda r: httpx.Response(400, text="bad cql")) \
        .count_pages("ENG") == "ERR400"


def test_space_content_cloud_covers_pages_and_blogs_in_one_cql():
    reqs = []
    def handler(req):
        reqs.append((req.url.path, dict(req.url.params)))
        if dict(req.url.params).get("cursor") == "n2":
            return httpx.Response(200, json={
                "results": [{"id": "3", "title": "Runbook", "type": "page"}],
                "_links": {}})
        return httpx.Response(200, json={
            "results": [{"id": "1", "title": "Home", "type": "page"},
                        {"id": "2", "title": "Launch", "type": "blogpost"}],
            "_links": {"next": "/rest/api/content/search?cursor=n2&limit=50"}})
    rows = list(mk_conf(handler).space_content("ENG"))
    assert [p["title"] for p in rows] == ["Home", "Launch", "Runbook"]
    path0, params0 = reqs[0]
    assert path0 == "/wiki/rest/api/content/search"
    assert params0["cql"] == 'space="ENG" and type in (page, blogpost)'
    for part in ("body.storage", "version", "history", "ancestors",
                 "metadata.labels", "children.comment", "children.attachment"):
        assert part in params0["expand"]
    assert reqs[1][1]["cursor"] == "n2"


def test_space_content_dc_enumerates_page_then_blogpost():
    seen_types = []
    def handler(req):
        params = dict(req.url.params)
        seen_types.append(params.get("type"))
        title = "Home" if params.get("type") == "page" else "Launch"
        return httpx.Response(200, json={
            "results": [{"id": "1", "title": title, "type": params.get("type")}],
            "_links": {}})
    rows = list(mk_conf(handler, "dc").space_content("ENG"))
    # DC's v1 content endpoint takes a single type -> two passes cover both.
    assert seen_types == ["page", "blogpost"]
    assert [p["title"] for p in rows] == ["Home", "Launch"]


def test_count_content_counts_pages_and_blogs():
    def handler(req):
        assert "type in (page, blogpost)" in dict(req.url.params)["cql"]
        return httpx.Response(200, json={"totalSize": 9})
    assert mk_conf(handler).count_content("ENG") == 9


def test_space_content_next_link_wiki_dedup():
    urls = []
    def handler(req):
        urls.append(str(req.url))
        if "cursor" in dict(req.url.params):
            return httpx.Response(200, json={"results": [], "_links": {}})
        return httpx.Response(200, json={
            "results": [{"id": "1", "title": "Home", "type": "page"}],
            "_links": {"next": "/wiki/rest/api/content/search?cursor=x"}})
    list(mk_conf(handler).space_content("ENG"))
    assert len(urls) == 2
    assert "/wiki/wiki/" not in urls[1]
    assert urls[1].startswith(
        "https://acme.atlassian.net/wiki/rest/api/content/search?cursor=x")


def test_space_content_raises_mid_loop():
    def handler(req):
        if "cursor" in dict(req.url.params):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={
            "results": [{"id": "1", "title": "Home", "type": "page"}],
            "_links": {"next": "/rest/api/content/search?cursor=x"}})
    it = mk_conf(handler).space_content("ENG")
    assert next(it)["title"] == "Home"
    with pytest.raises(ClientError) as ei:
        list(it)
    assert ei.value.status == 500


def test_dc_pat_bearer_header():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json={"displayName": "Igor Medeiros",
                                         "email": "igor@acme.example"})
    me = mk_conf(handler, deployment="dc").myself()
    assert me["display_name"] == "Igor Medeiros"
    assert me["email"] == "igor@acme.example"
    assert me["account_id"] is None          # DC has no accountId
    assert seen["auth"] == "Bearer tok-123"


def test_add_page_label_uses_v1_label_path_on_both_deployments():
    # /rest/api/content/{id}/label was NOT removed in the 2024-25 Cloud
    # deprecations; only enumeration endpoints are 410 Gone. Both Cloud and DC
    # should POST to the same path with a global-prefixed body.
    for deployment in ("cloud", "dc"):
        seen = {}
        def handler(req, _d=deployment):
            seen["path"] = req.url.path
            seen["body"] = req.content.decode()
            return httpx.Response(200, json={})
        mk_conf(handler, deployment=deployment).add_page_label("12345", "migrated")
        expected_suffix = "/rest/api/content/12345/label"
        assert seen["path"].endswith(expected_suffix), (
            f"deployment={deployment}: got {seen['path']!r}, "
            f"expected suffix {expected_suffix!r}")
        assert '"migrated"' in seen["body"]
        assert '"global"' in seen["body"]


def test_cql_escapes_space_key():
    # Defense in depth: a space key carrying a double quote must reach every CQL
    # site escaped, not break out of the literal — for both the page-only env
    # path (_space_cql) and the page+blog migration path (_space_content_cql).
    seen = []
    def handler(req):
        seen.append(dict(req.url.params)["cql"])
        return httpx.Response(200, json={"totalSize": 0, "results": []})
    assert mk_conf(handler).count_pages('EN"G') == 0          # env, page-only
    assert mk_conf(handler).count_content('EN"G') == 0        # migration, +blogs
    list(mk_conf(handler).space_content('EN"G'))              # enumeration
    assert seen == ['space="EN\\"G" and type=page',
                    'space="EN\\"G" and type in (page, blogpost)',
                    'space="EN\\"G" and type in (page, blogpost)']


# ===========================================================================
# Environment-audit additions: spaces_detailed, cql_count, space_permissions,
# groups_with_counts, global templates/blueprints, global labels.
# ===========================================================================


def test_cql_count_generalizes_count_pages():
    """cql_count issues an arbitrary CQL via /rest/api/search?...&limit=1 and
    returns totalSize on both deployments; ERR<status> on failure."""
    seen = []
    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"totalSize": 42, "results": []})
    assert mk_conf(handler).cql_count("type=page and space=ENG") == 42
    assert mk_conf(handler, "dc").cql_count("type=page") == 42
    assert seen[0][0] == "/wiki/rest/api/search"
    assert seen[1][0] == "/rest/api/search"
    for _, params in seen:
        assert params["limit"] == "1"
    assert seen[0][1]["cql"] == "type=page and space=ENG"
    # Error surfaces as ERR<status> (e.g. a 400 from an unsupported field).
    assert mk_conf(lambda r: httpx.Response(400, text="bad")) \
        .cql_count("type=page and status=draft") == "ERR400"


def test_count_pages_delegates_to_cql_count():
    """count_pages must keep its exact contract while delegating to cql_count."""
    seen = []
    def handler(req):
        seen.append(dict(req.url.params)["cql"])
        return httpx.Response(200, json={"totalSize": 3, "results": []})
    assert mk_conf(handler).count_pages("ENG") == 3
    assert seen == ['space="ENG" and type=page']


def test_spaces_detailed_cloud_keeps_type_status_homepage():
    """spaces_detailed (Cloud v2) preserves type, status, and homepage presence
    (homepageId), paginating via the v2 cursor like all_spaces."""
    def handler(req):
        assert req.url.path == "/wiki/api/v2/spaces"
        params = dict(req.url.params)
        if params.get("cursor") == "c2":
            return httpx.Response(200, json={
                "results": [{"id": "3", "key": "JANE", "name": "Jane Personal",
                             "type": "personal", "status": "current",
                             "homepageId": None}],
                "_links": {}})
        assert params["limit"] == "250"
        return httpx.Response(200, json={
            "results": [
                {"id": "1", "key": "ENG", "name": "Engineering",
                 "type": "global", "status": "current", "homepageId": "111"},
                {"id": "2", "key": "OLD", "name": "Old Space",
                 "type": "global", "status": "archived"},
            ],
            "_links": {"next": "/wiki/api/v2/spaces?cursor=c2&limit=250"}})
    rows, err = mk_conf(handler).spaces_detailed()
    assert err is None
    by_key = {r["key"]: r for r in rows}
    assert by_key["ENG"] == {"key": "ENG", "name": "Engineering", "id": "1",
                             "type": "global", "status": "current",
                             "has_homepage": True, "homepage_id": "111"}
    # archived global with no homepageId -> has_homepage False + homepage_id None
    assert by_key["OLD"]["type"] == "global"
    assert by_key["OLD"]["status"] == "archived"
    assert by_key["OLD"]["has_homepage"] is False
    assert by_key["OLD"]["homepage_id"] is None
    assert by_key["JANE"]["type"] == "personal"
    assert by_key["JANE"]["has_homepage"] is False


def test_spaces_detailed_dc_expand_metadata():
    """spaces_detailed (DC v1) reads type/status from the classic /rest/api/space
    response (type: global|personal, status: current|archived) and homepage from
    the expanded homepage object, with start/limit pagination."""
    def handler(req):
        assert req.url.path == "/rest/api/space"
        params = dict(req.url.params)
        assert "expand" in params
        start = int(params.get("start", 0))
        if start == 0:
            # A FULL first page (== limit) so pagination continues; the short
            # final page terminates (matches all_spaces DC semantics).
            results = [{"key": f"SP{i}", "name": f"Space {i}", "id": i,
                        "type": "global", "status": "current"}
                       for i in range(49)]
            results.append({"key": "ENG", "name": "Engineering", "id": 100,
                            "type": "global", "status": "current",
                            "homepage": {"id": "55"}})
            return httpx.Response(200, json={
                "results": results, "start": 0, "limit": 50, "size": 50,
                "_links": {"next": "/rest/api/space?start=50&limit=50"}})
        return httpx.Response(200, json={
            "results": [
                {"key": "HR", "name": "People", "id": 2,
                 "type": "global", "status": "current"},
                {"key": "ZZ", "name": "Archived", "id": 9,
                 "type": "global", "status": "archived"}],
            "start": 50, "limit": 50, "size": 2, "_links": {}})
    rows, err = mk_conf(handler, "dc").spaces_detailed()
    assert err is None
    by_key = {r["key"]: r for r in rows}
    assert by_key["ENG"]["has_homepage"] is True
    assert by_key["ENG"]["type"] == "global"
    assert by_key["HR"]["has_homepage"] is False
    assert by_key["ZZ"]["status"] == "archived"


def test_spaces_detailed_error_is_loud():
    rows, err = mk_conf(lambda r: httpx.Response(403, text="no")).spaces_detailed()
    assert rows == [] and err is not None and "ERR403" in err
    rows2, err2 = mk_conf(lambda r: httpx.Response(500, text="no"),
                          "dc").spaces_detailed()
    # 500 is retried then surfaces; first-page failure -> empty + error.
    assert rows2 == [] and err2 is not None


def test_space_permissions_cloud_reduces_to_types():
    """space_permissions (Cloud v2) returns the principal types and operation
    keys for a space's permissions, cursor-paginated, NEVER principal values."""
    def handler(req):
        assert req.url.path == "/wiki/api/v2/spaces/111/permissions"
        params = dict(req.url.params)
        if params.get("cursor") == "p2":
            return httpx.Response(200, json={
                "results": [{"id": "9",
                             "principal": {"type": "user", "id": "acc-secret"},
                             "operation": {"key": "administer",
                                           "targetType": "space"}}],
                "_links": {}})
        return httpx.Response(200, json={
            "results": [
                {"id": "1", "principal": {"type": "group", "id": "grp-secret"},
                 "operation": {"key": "read", "targetType": "space"}},
            ],
            "_links": {"next":
                       "/wiki/api/v2/spaces/111/permissions?cursor=p2"}})
    space = {"key": "ENG", "name": "Engineering", "id": "111"}
    perms, err = mk_conf(handler).space_permissions(space)
    assert err is None
    # Reduced to principal type + operation key pairs only.
    ptypes = {p["principal_type"] for p in perms}
    ops = {p["operation"] for p in perms}
    assert ptypes == {"group", "user"}
    assert ops == {"read", "administer"}
    # No identity (principal id) leaks.
    import json as _json
    assert "acc-secret" not in _json.dumps(perms)
    assert "grp-secret" not in _json.dumps(perms)


def test_space_permissions_dc_reads_subjects_and_anonymous():
    """space_permissions (DC v1) reads /rest/api/space/{key}/permission and
    reduces subjects to principal types + operations, plus anonymousAccess."""
    def handler(req):
        assert req.url.path == "/rest/api/space/ENG/permission"
        return httpx.Response(200, json=[
            {"operation": "read",
             "subjects": {"user": {"results": [{"accountId": "acc-x"}]},
                          "group": {"results": [{"name": "confluence-users"}]}},
             "anonymousAccess": True},
            {"operation": "administer",
             "subjects": {"group": {"results": [{"name": "site-admins"}]}},
             "anonymousAccess": False},
        ])
    space = {"key": "ENG", "name": "Engineering", "id": "1"}
    perms, err = mk_conf(handler, "dc").space_permissions(space)
    assert err is None
    ptypes = {p["principal_type"] for p in perms}
    ops = {p["operation"] for p in perms}
    assert "user" in ptypes and "group" in ptypes
    assert "anonymous" in ptypes        # anonymousAccess True surfaces as a type
    assert "read" in ops and "administer" in ops
    import json as _json
    assert "acc-x" not in _json.dumps(perms)


def test_space_permissions_dc_captures_group_names_not_user_names():
    """DC group grants carry the granted group NAMES (config identifiers, used
    by the empty-group cross-reference) while USER subjects stay reduced to the
    type only — a user accountId/name must never leak (privacy I1)."""
    def handler(req):
        return httpx.Response(200, json=[
            {"operation": "read",
             "subjects": {"user": {"results": [{"accountId": "acc-x"}]},
                          "group": {"results": [{"name": "ghost-team"}]}},
             "anonymousAccess": False},
        ])
    space = {"key": "ENG", "name": "Engineering", "id": "1"}
    perms, err = mk_conf(handler, "dc").space_permissions(space)
    assert err is None
    group_rows = [p for p in perms if p["principal_type"] == "group"]
    assert group_rows and group_rows[0].get("group_names") == ["ghost-team"]
    user_rows = [p for p in perms if p["principal_type"] == "user"]
    assert user_rows and "group_names" not in user_rows[0]
    import json as _json
    assert "acc-x" not in _json.dumps(perms)


def test_space_permissions_cloud_has_no_group_names():
    """Cloud v2 exposes a group id, not a name — group rows must NOT carry a
    group_names key (the gather turns that absence into a capability_gap)."""
    def handler(req):
        return httpx.Response(200, json={"results": [
            {"principal": {"type": "group", "id": "grp-123"},
             "operation": {"key": "read"}}]})
    space = {"key": "ENG", "name": "Engineering", "id": "1"}
    perms, err = mk_conf(handler).space_permissions(space)
    assert err is None
    assert perms and all("group_names" not in p for p in perms)
    import json as _json
    assert "grp-123" not in _json.dumps(perms)


def test_space_permissions_error():
    space = {"key": "ENG", "name": "Engineering", "id": "111"}
    perms, err = mk_conf(lambda r: httpx.Response(404, text="no")) \
        .space_permissions(space)
    assert perms == [] and err is not None and "ERR404" in err


def test_groups_with_counts_caps_and_counts():
    """groups_with_counts returns (names, member_counts, capped) reading
    /rest/api/group, probing each group's member count up to the cap."""
    groups = [{"name": f"grp-{i}", "id": f"gid-{i}"} for i in range(3)]
    def handler(req):
        p = req.url.path
        if p == "/wiki/rest/api/group" or p == "/rest/api/group":
            return httpx.Response(200, json={"results": groups,
                                             "size": 3, "_links": {}})
        # member probe: /rest/api/group/{id}/membersByGroupId or by name
        if "/member" in p:
            return httpx.Response(200, json={"results": [{}, {}], "size": 2,
                                             "_links": {}})
        return httpx.Response(200, json={"results": [], "_links": {}})
    names, counts, capped, err = mk_conf(handler).groups_with_counts(cap=10)
    assert err is None
    assert set(names) == {"grp-0", "grp-1", "grp-2"}
    assert capped is False
    # member_counts present for probed groups
    assert all(v == 2 for v in counts.values())


def test_groups_with_counts_capped_flag():
    groups = [{"name": f"grp-{i}", "id": f"gid-{i}"} for i in range(5)]
    def handler(req):
        p = req.url.path
        if p.endswith("/rest/api/group"):
            return httpx.Response(200, json={"results": groups, "_links": {}})
        if "/member" in p:
            return httpx.Response(200, json={"results": [{}], "_links": {}})
        return httpx.Response(200, json={"results": [], "_links": {}})
    names, counts, capped, err = mk_conf(handler).groups_with_counts(cap=2)
    assert len(names) == 5
    assert capped is True
    assert len(counts) <= 2


def test_global_templates_and_blueprints_counts():
    def handler(req):
        p = req.url.path
        if p.endswith("/template/page"):
            return httpx.Response(200, json={"results": [{}, {}, {}],
                                             "_links": {}})
        if p.endswith("/template/blueprint"):
            return httpx.Response(200, json={"results": [{}],
                                             "_links": {}})
        return httpx.Response(200, json={"results": [], "_links": {}})
    c = mk_conf(handler)
    assert c.global_templates() == (3, None)
    assert c.blueprints() == (1, None)


def test_global_templates_error():
    cnt, err = mk_conf(lambda r: httpx.Response(404, text="no")) \
        .global_templates()
    assert cnt is None and err is not None


def test_global_labels_count():
    def handler(req):
        assert "/label" in req.url.path
        assert dict(req.url.params).get("type") == "global"
        return httpx.Response(200, json={"results": [{}, {}], "_links": {}})
    cnt, err = mk_conf(handler).global_labels()
    assert cnt == 2 and err is None


# ===========================================================================
# Env-fix write methods: archive_space + delete_group.
# These are LIVE writes the Confluence env-fix apply path calls; they must
# return (status, payload), never raise on a 4xx (the caller logs the status).
# ===========================================================================


def test_archive_space_cloud_puts_status_archived_by_id():
    """Cloud archive: PUT /api/v2/spaces/{id} with status=archived. The id
    (not the key) is the v2 path segment, so the caller must resolve it first."""
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "111", "status": "archived"})
    st, d = mk_conf(handler).archive_space("111")
    assert st == 200
    assert seen["method"] == "PUT"
    assert seen["path"] == "/wiki/api/v2/spaces/111"
    assert '"archived"' in seen["body"]
    assert d.get("status") == "archived"


def test_archive_space_dc_puts_space_key_status_archived():
    """DC archive: PUT /rest/api/space/{key} sets status=archived (the classic
    v1 space-update endpoint; DC has no v2 spaces API)."""
    seen = {}
    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"key": "ENG", "status": "archived"})
    st, d = mk_conf(handler, "dc").archive_space("ENG")
    assert st == 200
    assert seen["method"] == "PUT"
    assert seen["path"] == "/rest/api/space/ENG"
    assert '"archived"' in seen["body"]


def test_archive_space_does_not_raise_on_4xx():
    """A 4xx must come back as a status, not an exception (the caller logs it)."""
    st, d = mk_conf(lambda r: httpx.Response(404, text="no")).archive_space("999")
    assert st == 404
    st2, d2 = mk_conf(lambda r: httpx.Response(403, text="no"),
                      "dc").archive_space("X")
    assert st2 == 403


def test_delete_group_by_name_both_deployments():
    """delete_group issues DELETE /rest/api/group?name=... on both deployments
    (Cloud serves it under /wiki via api_base; DC at the bare root)."""
    for deployment, expect_path in (("cloud", "/wiki/rest/api/group"),
                                    ("dc", "/rest/api/group")):
        seen = {}
        def handler(req, _seen=seen):
            _seen["method"] = req.method
            _seen["path"] = req.url.path
            _seen["params"] = dict(req.url.params)
            return httpx.Response(204, json={})
        st, d = mk_conf(handler, deployment).delete_group("ghost-group")
        assert st == 204
        assert seen["method"] == "DELETE"
        assert seen["path"] == expect_path
        assert seen["params"].get("name") == "ghost-group"


def test_delete_group_does_not_raise_on_4xx():
    st, d = mk_conf(lambda r: httpx.Response(404, text="no")).delete_group("x")
    assert st == 404


def _restr(read_users=(), read_groups=(), upd_users=(), upd_groups=()):
    return {"read": {"restrictions": {
                "user": {"results": list(read_users)},
                "group": {"results": list(read_groups)}}},
            "update": {"restrictions": {
                "user": {"results": list(upd_users)},
                "group": {"results": list(upd_groups)}}}}


def test_restricted_page_sample_counts_restricted_not_identities():
    rows = [
        {"id": "1", "restrictions": _restr(read_users=[{"accountId": "x"}])},
        {"id": "2", "restrictions": _restr(upd_groups=[{"name": "admins"}])},
        {"id": "3", "restrictions": _restr()},          # no restriction
    ]
    def handler(req):
        assert req.url.path == "/wiki/rest/api/content/search"
        assert "restrictions.read" in dict(req.url.params)["expand"]
        return httpx.Response(200, json={"results": rows, "_links": {}})
    probed, restricted, evaluable, truncated, err = \
        mk_conf(handler).restricted_page_sample("ENG")
    # _links has no `next` -> the space was fully drained -> not truncated.
    assert (probed, restricted, evaluable, truncated, err) == \
        (3, 2, True, False, None)


def test_restricted_page_sample_short_page_with_next_is_truncated():
    # The CQL endpoint may return a SHORT page yet advertise more via _links.next.
    # `truncated` must follow the next-link, NOT "did we hit cap" — otherwise a
    # restricted page on a later page is silently missed (false clean).
    rows = [{"id": "1", "restrictions": _restr()}]   # 1 clean row, but MORE exist
    def handler(req):
        return httpx.Response(200, json={
            "results": rows, "_links": {"next": "/more"}})
    probed, restricted, evaluable, truncated, err = \
        mk_conf(handler).restricted_page_sample("ENG")
    assert probed == 1 and restricted == 0 and evaluable is True
    assert truncated is True                          # disclose: not fully seen


def test_restricted_page_sample_not_evaluable_when_no_block():
    # API did not honor the expand (no restrictions block on any row) -> the
    # probe is NOT evaluable, so the caller discloses rather than reads as clean.
    rows = [{"id": "1"}, {"id": "2"}]
    def handler(req):
        return httpx.Response(200, json={"results": rows, "_links": {}})
    probed, restricted, evaluable, truncated, err = \
        mk_conf(handler).restricted_page_sample("ENG")
    assert probed == 2 and restricted == 0 and evaluable is False and err is None


def test_restricted_page_sample_dc_path_and_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"results": [
            {"id": "1", "restrictions": _restr(read_groups=[{"name": "x"}])}],
            "_links": {}})
    out = mk_conf(handler, deployment="dc").restricted_page_sample("ENG")
    assert seen["path"] == "/rest/api/content"
    assert seen["params"]["spaceKey"] == "ENG" and seen["params"]["type"] == "page"
    assert "restrictions.read" in seen["params"]["expand"]
    assert out[:4] == (1, 1, True, False)             # counted, fully drained


def test_restricted_page_sample_error_is_not_evaluable():
    probed, restricted, evaluable, truncated, err = mk_conf(
        lambda r: httpx.Response(503, text="down")).restricted_page_sample("ENG")
    assert evaluable is False and err and restricted == 0 and truncated is False


def test_space_content_dc_second_pass_failure_raises():
    # DC enumerates page then blogpost. A failure in the SECOND (blogpost) pass
    # must RAISE — never silently return only the pages (a partial enumeration
    # that would under-count and risk a truncated extract).
    def handler(req):
        if dict(req.url.params).get("type") == "blogpost":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={
            "results": [{"id": "1", "title": "Home", "type": "page"}],
            "_links": {}})
    it = mk_conf(handler, "dc").space_content("ENG")
    assert next(it)["title"] == "Home"          # the page pass yields first
    with pytest.raises(ClientError) as ei:
        list(it)                                 # the blogpost pass raises
    assert ei.value.status == 503
