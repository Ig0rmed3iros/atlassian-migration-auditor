"""Connection Vault — saved-credentials library (spec 2026-06-13).

Reuses the mk_app / ok_jira helpers from tests/test_main.py so the live-verify
mock transport matches the manual-PAT path exactly.
"""
import httpx

from tests.test_main import mk_app, ok_jira
from webapp.config import Config
from webapp.store import Store


def mk_store(tmp_path):
    return Store(db_path=str(tmp_path / "data" / "ma.db"),
                 key_path=str(tmp_path / "data" / "ma.key"),
                 secret_key=None)


# ----------------------------------------------------------------- store layer
def test_store_create_encrypts_token_at_rest(tmp_path):
    s = mk_store(tmp_path)
    cid = s.create_saved_connection(
        name="Acme Prod (Cloud)", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y",
        token="super-secret-token")
    row = s.get_saved_connection(cid)
    assert row is not None
    assert row["status"] == "unverified"
    assert row["name"] == "Acme Prod (Cloud)"
    assert row["product"] == "jira" and row["deployment"] == "cloud"
    # encryption at rest: the raw token string is NOT present in secret_enc.
    assert "super-secret-token" not in row["secret_enc"]
    assert "i@x.y" not in row["secret_enc"]
    # the decrypt helper round-trips the secret for internal use only.
    secret = s.saved_connection_secret(row)
    assert secret["token"] == "super-secret-token" and secret["email"] == "i@x.y"


def test_store_list_filters_by_product(tmp_path):
    s = mk_store(tmp_path)
    s.create_saved_connection(name="J", product="jira", deployment="cloud",
                              site_url="https://j.atlassian.net",
                              email="i@x.y", token="t1")
    s.create_saved_connection(name="C", product="confluence", deployment="cloud",
                              site_url="https://c.atlassian.net",
                              email="i@x.y", token="t2")
    everything = s.list_saved_connections()
    assert {r["product"] for r in everything} == {"jira", "confluence"}
    jira_only = s.list_saved_connections(product="jira")
    assert [r["name"] for r in jira_only] == ["J"]
    conf_only = s.list_saved_connections(product="confluence")
    assert [r["name"] for r in conf_only] == ["C"]


def test_store_mark_verified(tmp_path):
    s = mk_store(tmp_path)
    cid = s.create_saved_connection(name="J", product="jira", deployment="cloud",
                                    site_url="https://j.atlassian.net",
                                    email="i@x.y", token="t")
    assert s.get_saved_connection(cid)["status"] == "unverified"
    s.mark_saved_connection_verified(cid, "verified@x.y")
    row = s.get_saved_connection(cid)
    assert row["status"] == "verified"
    assert row["account_email"] == "verified@x.y"


def test_store_delete(tmp_path):
    s = mk_store(tmp_path)
    cid = s.create_saved_connection(name="J", product="jira", deployment="cloud",
                                    site_url="https://j.atlassian.net",
                                    email="i@x.y", token="t")
    s.delete_saved_connection(cid)
    assert s.get_saved_connection(cid) is None
    assert s.list_saved_connections() == []


def test_store_validates_product_and_deployment(tmp_path):
    s = mk_store(tmp_path)
    for bad in ("bamboo", "bitbucket", ""):
        try:
            s.create_saved_connection(name="x", product=bad, deployment="cloud",
                                      site_url="https://x", email="i@x.y", token="t")
            assert False, f"expected ValueError for product {bad!r}"
        except ValueError:
            pass
    for bad in ("server", "datacenter", ""):
        try:
            s.create_saved_connection(name="x", product="jira", deployment=bad,
                                      site_url="https://x", email="i@x.y", token="t")
            assert False, f"expected ValueError for deployment {bad!r}"
        except ValueError:
            pass


# ----------------------------------------------------------------- /connections
def test_connections_page_renders(tmp_path):
    app, c = mk_app(tmp_path)
    app.state.store.create_saved_connection(
        name="Acme Prod", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="t")
    r = c.get("/connections")
    assert r.status_code == 200
    assert "Acme Prod" in r.text
    # token must NEVER be rendered back to the browser
    assert "<form" in r.text and 'name="api_token"' in r.text


def test_connections_post_creates_and_verifies(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    r = c.post("/connections",
               data={"name": "Acme", "product": "jira", "deployment": "cloud",
                     "site_url": "https://acme.atlassian.net",
                     "email": "i@x.y", "api_token": "tok"})
    assert r.status_code == 303
    rows = app.state.store.list_saved_connections()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "verified"
    assert row["account_email"] == "i@x.y"
    # encryption at rest
    assert "tok" not in row["secret_enc"]


def test_connections_post_bad_auth_keeps_unverified(tmp_path):
    def deny(req):
        return httpx.Response(401, text="no")
    app, c = mk_app(tmp_path, handler=deny)
    r = c.post("/connections",
               data={"name": "Bad", "product": "jira", "deployment": "cloud",
                     "site_url": "https://bad.atlassian.net",
                     "email": "i@x.y", "api_token": "bad"})
    # the create still succeeds (best-effort verify); status stays unverified.
    assert r.status_code == 303
    rows = app.state.store.list_saved_connections()
    assert len(rows) == 1 and rows[0]["status"] == "unverified"


def test_connections_verify_route_marks_status(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    cid = app.state.store.create_saved_connection(
        name="Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="tok")
    assert app.state.store.get_saved_connection(cid)["status"] == "unverified"
    r = c.post(f"/connections/{cid}/verify")
    assert r.status_code == 303
    assert app.state.store.get_saved_connection(cid)["status"] == "verified"


def test_connections_delete_route(tmp_path):
    app, c = mk_app(tmp_path)
    cid = app.state.store.create_saved_connection(
        name="Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="tok")
    r = c.post(f"/connections/{cid}/delete")
    assert r.status_code == 303
    assert app.state.store.get_saved_connection(cid) is None


# ----------------------------------------------------------- from-saved in audit
def test_from_saved_copies_into_migration_and_verifies(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    cid = store.create_saved_connection(
        name="Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="tok")
    r = c.post(f"/migrations/{mid}/connections/from-saved",
               data={"role": "source", "saved_id": str(cid)})
    assert r.status_code == 303
    conn = store.get_connection(mid, "source")
    assert conn is not None
    assert conn["status"] == "verified"
    assert conn["site_url"] == "https://acme.atlassian.net"
    assert conn["deployment"] == "cloud"
    # the secret was copied (decryptable) — token present, not in ciphertext.
    secret = store.connection_secret(conn)
    assert secret["token"] == "tok" and secret["email"] == "i@x.y"
    assert b"tok" not in conn["secret_enc"]


def test_from_saved_with_denying_verify_does_not_save(tmp_path):
    def deny(req):
        return httpx.Response(401, text="no")
    app, c = mk_app(tmp_path, handler=deny)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    cid = store.create_saved_connection(
        name="Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="rotated")
    r = c.post(f"/migrations/{mid}/connections/from-saved",
               data={"role": "source", "saved_id": str(cid)},
               follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    # a dead/rotated credential must NOT silently land in the migration.
    assert store.get_connection(mid, "source") is None


def test_from_saved_dc_omits_email_in_secret(tmp_path):
    def bearer_dc(req):
        if (str(req.url.path) == "/rest/api/2/myself"
                and req.headers.get("authorization") == "Bearer tok"):
            return httpx.Response(200, json={"name": "igor",
                                             "displayName": "Igor"})
        return httpx.Response(404, text="nope")
    app, c = mk_app(tmp_path, handler=bearer_dc)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    cid = store.create_saved_connection(
        name="DC", product="jira", deployment="dc",
        site_url="https://jira.acme.example", email="", token="tok")
    r = c.post(f"/migrations/{mid}/connections/from-saved",
               data={"role": "source", "saved_id": str(cid)})
    assert r.status_code == 303
    conn = store.get_connection(mid, "source")
    assert conn is not None and conn["deployment"] == "dc"
    secret = store.connection_secret(conn)
    assert "email" not in secret and secret["token"] == "tok"


# ---------------------------------------------------- dropdown product filtering
def test_dropdown_only_lists_product_matching_connections(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("jira mig", product="jira")
    store.create_saved_connection(
        name="Jira Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="t")
    store.create_saved_connection(
        name="Conf Other", product="confluence", deployment="cloud",
        site_url="https://other.atlassian.net", email="i@x.y", token="t")
    html = c.get(f"/migrations/{mid}").text
    assert "Jira Acme" in html
    assert "Conf Other" not in html


def test_dropdown_hidden_when_no_saved_connections(tmp_path):
    app, c = mk_app(tmp_path)
    store = app.state.store
    mid = store.create_migration("jira mig", product="jira")
    html = c.get(f"/migrations/{mid}").text
    # no from-saved form when there is nothing to pick.
    assert "connections/from-saved" not in html


# ----------------------------------------------------- copy semantics on delete
def test_deleting_saved_connection_leaves_migration_connection_intact(tmp_path):
    app, c = mk_app(tmp_path, handler=ok_jira)
    store = app.state.store
    mid = store.create_migration("m", product="jira")
    cid = store.create_saved_connection(
        name="Acme", product="jira", deployment="cloud",
        site_url="https://acme.atlassian.net", email="i@x.y", token="tok")
    c.post(f"/migrations/{mid}/connections/from-saved",
           data={"role": "source", "saved_id": str(cid)})
    assert store.get_connection(mid, "source") is not None
    # delete the saved connection — the migration's copy must survive.
    c.post(f"/connections/{cid}/delete")
    assert store.get_saved_connection(cid) is None
    surviving = store.get_connection(mid, "source")
    assert surviving is not None and surviving["status"] == "verified"
    assert surviving["site_url"] == "https://acme.atlassian.net"
