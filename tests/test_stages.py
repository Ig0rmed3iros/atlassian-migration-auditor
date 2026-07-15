import httpx
import pytest
from webapp.stages import build_clients, build_stages
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


class _NullStore:
    def add_event(self, *a, **k):
        pass


def test_build_clients_pat_and_oauth(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          secret={"email": "a@b.c", "token": "tok"})
    store.save_connection(mid, "target", "oauth", "https://t.atlassian.net",
                          cloud_id="cid-9",
                          secret={"access_token": "at", "refresh_token": "rt",
                                  "expires_at": 9e12})
    src, tgt, _conn = build_clients(store, mid, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert src.conn.auth_type == "pat" and src.conn.email == "a@b.c"
    assert tgt.conn.auth_type == "oauth" and tgt.conn.cloud_id == "cid-9"
    assert src.conn.api_base == "https://s.atlassian.net"
    assert tgt.conn.api_base == "https://api.atlassian.com/ex/jira/cid-9"


def test_oauth_refresh_persists_back_to_store(store):
    mid = store.create_migration("m")
    store.save_connection(mid, "source", "oauth", "https://s.atlassian.net",
                          cloud_id="c1",
                          secret={"access_token": "old", "refresh_token": "rt1",
                                  "expires_at": 1})   # expired -> proactive refresh
    calls = {"n": 0}
    def handler(req):
        if "auth.atlassian.com" in str(req.url):
            calls["n"] += 1
            return httpx.Response(200, json={"access_token": "new",
                                             "refresh_token": "rt2",
                                             "expires_in": 3600})
        return httpx.Response(200, json={"ok": 1})
    store.settings_set("oauth_client_id", "cid")
    store.settings_set("oauth_client_secret_enc",
                       store.encrypt({"secret": "sec"}).decode())
    src, _tgt_missing, _conn = build_clients(
        store, mid,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        require_both=False)
    st, _ = src.req("/rest/api/3/myself")
    assert st == 200 and calls["n"] == 1
    row = store.get_connection(mid, "source")
    sec = store.connection_secret(row)
    assert sec["refresh_token"] == "rt2" and sec["access_token"] == "new"


def test_build_stages_returns_all_engine_phases():
    from webapp.runs import PHASES
    stages = build_stages()
    for p in PHASES:
        if p == "finalize":
            continue
        assert p in stages and callable(stages[p])


def test_build_clients_returns_connector_for_product(store, monkeypatch):
    import dataclasses

    from auditor.connectors import JIRA
    from webapp import stages as S

    mid = store.create_migration("m")           # product defaults to jira
    store.save_connection(mid, "source", "pat", "https://acme.atlassian.net",
                          secret={"email": "igor@acme.example", "token": "t"})
    store.save_connection(mid, "target", "pat", "https://jira.globex.example",
                          secret={"token": "t"}, deployment="dc")
    src, tgt, connector = build_clients(store, mid)
    assert connector.product == "jira"
    assert src.conn.deployment == "cloud" and tgt.conn.deployment == "dc"
    assert tgt.conn.email is None               # dc secret carries no email key

    # oauth is wired through the Jira Cloud gateway only: a (hypothetical)
    # non-jira connector with an oauth connection must refuse loudly instead
    # of minting a gateway client against the wrong product API. The store
    # refuses unregistered products at creation, so simulate the legacy row
    # directly (get_connector is monkeypatched to serve it below).
    mid2 = store.create_migration("m2")
    store._exec("UPDATE migrations SET product='confluence' WHERE id=?",
                (mid2,))
    store.save_connection(mid2, "source", "oauth", "https://acme.atlassian.net",
                          cloud_id="c1",
                          secret={"access_token": "at", "refresh_token": "rt",
                                  "expires_at": 9e12})
    store.save_connection(mid2, "target", "pat", "https://wiki.globex.example",
                          secret={"token": "t"}, deployment="dc")
    monkeypatch.setattr(S, "get_connector",
                        lambda p: dataclasses.replace(JIRA, product="confluence"))
    with pytest.raises(RuntimeError, match="oauth is only supported for Jira Cloud"):
        build_clients(store, mid2)


def test_stage_config_attaches_payload_when_capture_enabled(monkeypatch):
    import webapp.stages as st

    class FakeConn:
        product = "jira"
        def audit_config(self, src, tgt, containers, workspace, progress):
            return {"areas": {}, "findings": [
                {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
                 "detail": {}}]}
    captured = {}
    monkeypatch.setattr(st, "capture_config_payload",
                        lambda client, f: {"name": "Triage", "category": "TODO"})
    ctx = {"connector": FakeConn(), "src": object(), "tgt": object(),
           "params": {"capture_remediation": True}, "selected": [{"key": "P"}],
           "run_id": 1, "store": _NullStore(), "workspace": "/tmp"}
    st.stage_config(ctx)
    assert ctx["config_result"]["findings"][0]["fix_payload"]["category"] == "TODO"


def test_stage_config_skips_payload_when_disabled(monkeypatch):
    import webapp.stages as st
    class FakeConn:
        product = "jira"
        def audit_config(self, *a, **k):
            return {"areas": {}, "findings": [
                {"area": "statuses", "name": "T", "kind": "missing_in_tgt",
                 "detail": {}}]}
    ctx = {"connector": FakeConn(), "src": object(), "tgt": object(),
           "params": {"capture_remediation": False}, "selected": [{"key": "P"}],
           "run_id": 1, "store": _NullStore(), "workspace": "/tmp"}
    st.stage_config(ctx)
    assert "fix_payload" not in ctx["config_result"]["findings"][0]


def test_stage_capture_values_noop_for_non_jira():
    """stage_capture_values is a no-op when connector.product != 'jira'."""
    import webapp.stages as st

    class FakeConn:
        product = "confluence"

    ctx = {
        "connector": FakeConn(),
        "params": {"capture_remediation": True},
        "config_result": {"findings": [
            {"area": "custom_fields", "kind": "missing_in_tgt", "name": "Severity",
             "fix_payload": {"field_id": "customfield_10200"}}]},
        "selected": [{"key": "P"}],
        "run_id": 1,
        "store": _NullStore(),
        "workspace": "/tmp",
        "src": object(),
    }
    st.stage_capture_values(ctx)
    # values_file must NOT have been written (stage exited early)
    assert "values_file" not in ctx["config_result"]["findings"][0]["fix_payload"]


def test_stage_capture_values_writes_values_file(tmp_path, monkeypatch):
    """stage_capture_values populates values_file + values_count on each matching finding."""
    import webapp.stages as st

    class FakeConn:
        product = "jira"

    monkeypatch.setattr(st, "capture_fields_values",
                        lambda client, keys, field_ids, out_dir: {fid: 3 for fid in field_ids})

    finding = {"area": "custom_fields", "kind": "missing_in_tgt", "name": "My Field",
               "fix_payload": {"field_id": "customfield_10300"}}
    ctx = {
        "connector": FakeConn(),
        "params": {"capture_remediation": True},
        "config_result": {"findings": [finding]},
        "selected": [{"key": "PROJ"}],
        "run_id": 1,
        "store": _NullStore(),
        "workspace": str(tmp_path),
        "src": object(),
    }
    st.stage_capture_values(ctx)
    assert finding["fix_payload"]["values_count"] == 3
    assert "values_file" in finding["fix_payload"]


def test_stage_capture_values_uses_field_id_as_filename(tmp_path, monkeypatch):
    """Filename stem must be field_id, not the human name, so same-slug collisions
    ('My-Field' vs 'My Field') never silently overwrite each other."""
    import webapp.stages as st

    class FakeConn:
        product = "jira"

    captured_field_ids = []
    def fake_capture(client, keys, field_ids, out_dir):
        captured_field_ids.extend(field_ids)
        return {fid: 1 for fid in field_ids}

    monkeypatch.setattr(st, "capture_fields_values", fake_capture)

    # Two fields whose names would both collapse to 'My_Field' under re.sub
    findings = [
        {"area": "custom_fields", "kind": "missing_in_tgt", "name": "My-Field",
         "fix_payload": {"field_id": "customfield_10100"}},
        {"area": "custom_fields", "kind": "missing_in_tgt", "name": "My Field",
         "fix_payload": {"field_id": "customfield_10200"}},
    ]
    ctx = {
        "connector": FakeConn(),
        "params": {"capture_remediation": True},
        "config_result": {"findings": findings},
        "selected": [{"key": "PROJ"}],
        "run_id": 1,
        "store": _NullStore(),
        "workspace": str(tmp_path),
        "src": object(),
    }
    st.stage_capture_values(ctx)
    assert len(captured_field_ids) == 2
    # Both field ids must be distinct (no collision)
    assert captured_field_ids[0] != captured_field_ids[1]
    # Field ids must be the raw field_id, not the human name
    assert "customfield_10100" in captured_field_ids
    assert "customfield_10200" in captured_field_ids
    # values_file paths for each finding must be distinct and contain field_id
    paths = [f["fix_payload"]["values_file"] for f in findings]
    assert paths[0] != paths[1]
    assert "customfield_10100" in paths[0]
    assert "customfield_10200" in paths[1]


def test_stage_capture_values_emits_capture_values_phase_events(monkeypatch):
    """_say calls inside stage_capture_values must use phase='capture_values'."""
    import webapp.stages as st

    class FakeConn:
        product = "jira"

    monkeypatch.setattr(st, "capture_fields_values",
                        lambda client, keys, field_ids, out_dir: {fid: 5 for fid in field_ids})

    events = []

    class RecordingStore:
        def add_event(self, run_id, phase, level, msg):
            events.append({"phase": phase, "msg": msg})

    finding = {"area": "custom_fields", "kind": "missing_in_tgt", "name": "Severity",
               "fix_payload": {"field_id": "customfield_99"}}
    ctx = {
        "connector": FakeConn(),
        "params": {"capture_remediation": True},
        "config_result": {"findings": [finding]},
        "selected": [{"key": "P"}],
        "run_id": 1,
        "store": RecordingStore(),
        "workspace": "/tmp",
        "src": object(),
    }
    st.stage_capture_values(ctx)
    assert events, "expected at least one _say call"
    for ev in events:
        assert ev["phase"] == "capture_values", (
            f"expected phase 'capture_values', got {ev['phase']!r}"
        )
