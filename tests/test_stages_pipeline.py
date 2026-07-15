"""Stage-level pipeline tests: real Store + fake JiraClients.

Covers the four load-bearing seams:
  1. ERR string counts -> None in DB (stage_scope)
  2. Unmatched requested key emits warn event (stage_scope / Fix 1)
  3. No-overlap compare -> fidelity_pct=None written to DB (stage_compare)
  4. stage_verify raises loudly when both sides are unreachable (stage_verify)

Plus two full end-to-end smokes through create_app + the real RunEngine,
every production stage live against a MockTransport site pair:
  test 10 — Confluence Cloud→Cloud (acceptance #3)
  test 11 — Jira DC→Cloud (acceptance #2): wiki vs ADF prose fingerprints
            equal, DC auth/pagination/config gates all exercised.
"""
import dataclasses
import gzip
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from auditor.client import ClientError, Connection, JiraClient, h16
from auditor.connectors import JIRA
from webapp import stages as S
from webapp.config import Config
from webapp.main import create_app
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


class FakeClient:
    def __init__(self, projects=None, counts=None, me=None, deployment="cloud"):
        self._projects = projects or []
        self._counts = counts or {}
        self._me = me or {"displayName": "T", "emailAddress": "t@x.y",
                          "accountId": "acc"}
        # Stages read cl.conn.deployment for capability gates; a bare
        # namespace keeps these fakes free of real Connection plumbing.
        self.conn = SimpleNamespace(deployment=deployment)

    def myself(self):
        return self._me

    def all_projects(self):
        return self._projects, None

    def approx_count(self, jql):
        for k, v in self._counts.items():
            if f'"{k}"' in jql:
                return v
        return 0


def _ctx(store, mid, rid, tmp_path, **over):
    c = {
        "run_id": rid,
        "migration_id": mid,
        "params": {},
        "store": store,
        "workspace": str(tmp_path),
        "project_results": {},
        "issue_findings": [],
        "config_result": {"areas": {}, "findings": []},
        "blind_spots": [],
        "connector": JIRA,   # stage_verify sets this in production
    }
    c.update(over)
    return c


# ------------------------------------------------------------------ test 1
def test_stage_scope_stores_err_counts_as_none(store, tmp_path):
    """approx_count returning a non-int string (e.g. 'ERR500') must be
    normalised to None before writing to the DB (the schema column is INTEGER).
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    src = FakeClient(
        projects=[{"key": "AC", "name": "AC", "id": "1"}],
        counts={"AC": "ERR500"},          # simulates a broken count response
    )
    tgt = FakeClient(
        projects=[{"key": "AC", "name": "AC", "id": "9"}],
        counts={"AC": 5},
    )
    ctx = _ctx(store, mid, rid, tmp_path, src=src, tgt=tgt)
    S.stage_scope(ctx)
    rows = {r["key"]: r for r in store.get_run_projects(rid)}
    assert rows["AC"]["src_count"] is None      # ERR string -> None
    assert rows["AC"]["tgt_count"] == 5
    assert [m["key"] for m in ctx["selected"]] == ["AC"]


# ------------------------------------------------------------------ test 2
def test_stage_scope_warns_on_unmatched_requested_key(store, tmp_path):
    """A key in params['projects'] that is not matched on both sides must
    produce a warn event so the operator can see the typo / source-only project
    rather than it silently vanishing from scope.
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    src = FakeClient(projects=[{"key": "AC", "name": "AC", "id": "1"}])
    tgt = FakeClient(projects=[{"key": "AC", "name": "AC", "id": "9"}])
    ctx = _ctx(store, mid, rid, tmp_path, src=src, tgt=tgt,
               params={"projects": ["AC", "GHOST"]})
    S.stage_scope(ctx)
    assert [m["key"] for m in ctx["selected"]] == ["AC"]
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("GHOST" in m and "skipped" in m for m in msgs), (
        "expected a warn event mentioning 'GHOST' and 'skipped'; got: " + str(msgs)
    )


# ------------------------------------------------------------------ test 3
def test_stage_compare_writes_none_fidelity_row(store, tmp_path):
    """When src and tgt share no common issue keys, compare_project returns
    fidelity_pct=None.  stage_compare must write that None (not 0.0, not a
    string) to the DB so the UI can distinguish N/A from 0%.
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    def mk(key):
        return {
            "key": key,
            "id": key,
            "fields": {
                "summary": "s",
                "description": {"len": 1, "sha": h16("s"), "head": "s"},
                "issuetype": {"name": "Task"},
                "status": {"name": "Open"},
                "priority": None,
                "resolution": None,
                "resolutiondate": None,
                "created": "2026-01-01T00:00:00.000+0000",
                "duedate": None,
                "labels": [],
                "components": [],
                "fixVersions": [],
                "versions": [],
                "parent": None,
                "environment": None,
                "security": None,
                "assignee": None,
                "reporter": None,
                "creator": None,
                "comment": {"total": 0, "inline": 0, "items": []},
                "worklog": {"total": 0},
                "votes": {"votes": 0},
                "watches": {"watchCount": 0},
                "attachment": [],
                "issuelinks": [],
            },
        }

    os.makedirs(os.path.join(str(tmp_path), "src"))
    os.makedirs(os.path.join(str(tmp_path), "tgt"))
    with gzip.open(os.path.join(str(tmp_path), "src", "AC.core.jsonl.gz"), "wt") as fh:
        fh.write(json.dumps(mk("AC-1")) + "\n")
    with gzip.open(os.path.join(str(tmp_path), "tgt", "AC.core.jsonl.gz"), "wt") as fh:
        fh.write(json.dumps(mk("AC-99")) + "\n")   # no overlap with AC-1

    store.set_run_projects(rid, [{"key": "AC", "name": "AC",
                                  "src_count": 1, "tgt_count": 1,
                                  "status": "scoped"}])
    ctx = _ctx(store, mid, rid, tmp_path, src=FakeClient(), tgt=FakeClient(),
               selected=[{"key": "AC", "name": "AC",
                          "src_count": 1, "tgt_count": 1}])
    S.stage_compare(ctx)
    row = store.get_run_projects(rid)[0]
    assert row["fidelity_pct"] is None, (
        f"expected None fidelity_pct (no overlap), got {row['fidelity_pct']!r}"
    )
    assert ctx["project_results"]["AC"]["stats"]["fidelity_pct"] is None


# ------------------------------------------------------------------ test 4
def test_stage_verify_fails_loud_on_unreachable_side(store, tmp_path, monkeypatch):
    """stage_verify must propagate ClientError immediately so the run engine
    records a 'failed' status rather than swallowing the error.
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    store.save_connection(mid, "source", "pat", "https://s.atlassian.net",
                          secret={"email": "a@b.c", "token": "t"})
    store.save_connection(mid, "target", "pat", "https://t.atlassian.net",
                          secret={"email": "a@b.c", "token": "t"})

    class Boom:
        def myself(self):
            raise ClientError("auth failed", 401)

    monkeypatch.setattr(S, "build_clients",
                        lambda *a, **k: (Boom(), Boom(), JIRA))
    ctx = _ctx(store, mid, rid, tmp_path)
    with pytest.raises(ClientError):
        S.stage_verify(ctx)


def test_stage_verify_tolerates_missing_display_name(store, tmp_path,
                                                     monkeypatch):
    """DC identities may lack a display name entirely, and a future
    connector's verify may omit the key: the verify event must degrade to
    '?' instead of raising KeyError AFTER a successful authentication."""
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    for role in ("source", "target"):
        store.save_connection(mid, role, "pat", f"https://{role}.example",
                              secret={"token": "t"}, deployment="dc")
    bare = dataclasses.replace(
        JIRA, verify=lambda cl: {"email": None, "account_id": None})
    monkeypatch.setattr(S, "build_clients",
                        lambda *a, **k: (FakeClient(), FakeClient(), bare))
    ctx = _ctx(store, mid, rid, tmp_path)
    S.stage_verify(ctx)
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("authenticated as ?" in m for m in msgs), (
        "expected the verify event to fall back to '?'; got: " + str(msgs))


def test_stage_verify_sets_vocabulary_labels(store, tmp_path, monkeypatch):
    """The finalize headlines template on ctx labels, so stage_verify must
    copy them off the connector — proven with non-jira labels, not defaults."""
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    for role in ("source", "target"):
        store.save_connection(mid, role, "pat", f"https://{role}.example",
                              secret={"email": "igor@acme.example", "token": "t"})
    labeled = dataclasses.replace(JIRA, container_label="space",
                                  item_label="page")
    monkeypatch.setattr(S, "build_clients",
                        lambda *a, **k: (FakeClient(), FakeClient(), labeled))
    ctx = _ctx(store, mid, rid, tmp_path)
    S.stage_verify(ctx)
    assert ctx["item_label"] == "page"
    assert ctx["container_label"] == "space"


# ------------------------------------------------------------------ test 5
def test_undo_migration_elevations_clears_grants(store):
    from webapp.stages import undo_migration_elevations
    import json
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.settings_set(f"elevation:{rid}:target", json.dumps(
        {"role_id": 9, "account_id": "acc-1",
         "grants": [{"project_id": "100", "ok": True, "added": True}]}))
    deletes = []
    class FakeClient:
        def req(self, path, method="GET", body=None, params=None, tries=6):
            if method == "DELETE":
                deletes.append(path)
            return 204, {}
    undo = []
    undo_migration_elevations(store, mid, None, FakeClient(),
                              log=lambda side, frm: undo.append((side, frm)))
    assert deletes and "/project/100/role/9" in deletes[0]
    assert store.settings_get(f"elevation:{rid}:target") is None
    assert undo == [("target", rid)]


def test_undo_migration_elevations_keeps_record_on_partial_failure(store):
    # No-bias review: the local elevation record was deleted even when the
    # server-side DELETE failed (500), leaving a LIVE grant that no later sweep
    # could find -> a permanent privilege leak. On any failed de-grant the record
    # MUST be kept so a retry re-attempts.
    from webapp.stages import undo_migration_elevations
    import json
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.settings_set(f"elevation:{rid}:target", json.dumps(
        {"role_id": 9, "account_id": "acc-1",
         "grants": [{"project_id": "100", "ok": True, "added": True}]}))

    class FailClient:
        def req(self, path, method="GET", body=None, params=None, tries=6):
            return 500, {"_error": "boom"}      # de-grant fails server-side
    undo_migration_elevations(store, mid, None, FailClient())
    assert store.settings_get(f"elevation:{rid}:target") is not None


# ------------------------------------------------------------------ test 6
def test_stage_permissions_warns_on_indeterminate(store, tmp_path):
    """A project that detect_blind_spots flags as indeterminate (count lookup
    errored but insight says issues exist) must emit a warn event so the
    operator knows the count is unverified rather than seeing it pass silently.
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})

    def fake_detect(client, keys):
        return [{"key": "AC", "search_count": None, "insight_count": 42,
                 "blind_spot": False, "indeterminate": True}]

    connector = dataclasses.replace(JIRA, detect_blind_spots=fake_detect)
    src = FakeClient(projects=[{"key": "AC", "name": "AC", "id": "1"}])
    tgt = FakeClient(projects=[{"key": "AC", "name": "AC", "id": "9"}])
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "status": "scoped"}])
    ctx = _ctx(store, mid, rid, tmp_path, src=src, tgt=tgt,
               connector=connector, selected=[{"key": "AC", "name": "AC"}])
    S.stage_permissions(ctx)
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("COULD NOT VERIFY" in m and "AC" in m for m in msgs), (
        "expected a warn event for the indeterminate project; got: " + str(msgs)
    )


# ------------------------------------------------------------------ test 7
def test_stage_permissions_dc_side_skips_with_warning(store, tmp_path):
    """Blind-spot detection needs Jira Cloud's insight counts (R9): a dc side
    must be skipped with ONE explicit warn event — never probed, never
    silently reported as zero blind spots — while the cloud side is still
    checked normally.
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    seen = []

    def fake_detect(client, keys):
        seen.append(client)
        return [{"key": "AC", "search_count": 5, "insight_count": 5,
                 "blind_spot": False}]

    connector = dataclasses.replace(JIRA, detect_blind_spots=fake_detect)
    src = FakeClient(deployment="dc")
    tgt = FakeClient(deployment="cloud")
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "status": "scoped"}])
    ctx = _ctx(store, mid, rid, tmp_path, src=src, tgt=tgt,
               connector=connector, selected=[{"key": "AC", "name": "AC"}])
    S.stage_permissions(ctx)
    assert seen == [tgt], "only the cloud side may be probed"
    warns = [e["message"] for e in store.get_events(rid)
             if e["level"] == "warn"]
    assert len(warns) == 1
    assert "not supported" in warns[0] and "source" in warns[0]


# ------------------------------------------------------------------ test 8
def test_stage_compare_passes_cross_dialect_for_mixed_deployments(store, tmp_path):
    """A jira dc→cloud pair authors wiki markup on one side and ADF on the
    other: stage_compare must flag the comparison cross_dialect=True so
    content findings carry the representation-sensitive badge. Same-deployment
    pairs stay False (byte-identical existing behavior).
    """
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    seen = []

    def spy_compare(key, src_path, tgt_path, cross_dialect=False):
        seen.append(cross_dialect)
        return {"stats": {"common": 0, "missing_in_tgt": 0, "tails": 0,
                          "issues_with_mismatches": 0, "fidelity_pct": None},
                "findings": []}

    connector = dataclasses.replace(JIRA, compare=spy_compare)
    store.set_run_projects(rid, [{"key": "AC", "name": "AC", "status": "scoped"}])
    ctx = _ctx(store, mid, rid, tmp_path,
               src=FakeClient(deployment="dc"),
               tgt=FakeClient(deployment="cloud"),
               connector=connector, selected=[{"key": "AC", "name": "AC"}])
    S.stage_compare(ctx)
    assert seen == [True]

    seen.clear()
    ctx2 = _ctx(store, mid, rid, tmp_path,
                src=FakeClient(), tgt=FakeClient(),
                connector=connector, selected=[{"key": "AC", "name": "AC"}])
    S.stage_compare(ctx2)
    assert seen == [False]


# ------------------------------------------------------------------ test 9
def test_stage_extract_refuses_cached_extract_with_old_format(store, tmp_path):
    """reuse_extracts_from points the run at a PRIOR run's workspace. A cached
    side written by a pre-upgrade build (different sha scheme / raw ISO
    timestamps) mixed with a freshly extracted side would flag every common
    issue as drifted — confidently wrong. stage_extract must re-extract any
    cached file whose format stamp is not current, with a warn event, and
    keep reusing current-format files."""
    from auditor.extract import EXTRACT_FORMAT
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})
    os.makedirs(os.path.join(str(tmp_path), "src"))
    os.makedirs(os.path.join(str(tmp_path), "tgt"))
    src_path = os.path.join(str(tmp_path), "src", "AC.core.jsonl.gz")
    tgt_path = os.path.join(str(tmp_path), "tgt", "AC.core.jsonl.gz")
    with gzip.open(src_path, "wt") as fh:          # legacy: no header line
        fh.write(json.dumps({"key": "AC-1", "fields": {}}) + "\n")
    with gzip.open(tgt_path, "wt") as fh:          # current format
        fh.write(json.dumps({"_extract_format": EXTRACT_FORMAT}) + "\n")

    extracted = []

    def fake_extract(cl, key, out_path, progress=None):
        extracted.append(out_path)
        with gzip.open(out_path, "wt") as fh:
            fh.write(json.dumps({"_extract_format": EXTRACT_FORMAT}) + "\n")
        return {"extracted": 0, "approx": 0, "verified": True}

    connector = dataclasses.replace(JIRA, extract=fake_extract)
    ctx = _ctx(store, mid, rid, tmp_path, src=FakeClient(), tgt=FakeClient(),
               connector=connector, params={"reuse_extracts_from": 7},
               selected=[{"key": "AC", "name": "AC",
                          "src_count": 0, "tgt_count": 0}])
    S.stage_extract(ctx)
    assert extracted == [src_path], (
        "only the legacy-format side may be re-extracted; got: "
        + str(extracted))
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("incompatible" in m and "src" in m for m in msgs)
    assert any("reusing cached extract" in m and "tgt" in m for m in msgs)


# --------------------------------------------------------- compose wrappers
def test_build_stages_compare_reaches_usergap(store, tmp_path, monkeypatch):
    """build_stages()['compare'] is the _compose wrapper that wires
    stage_usergap into the compare phase. callable() alone passes for any
    lambda, so drive the real returned callable end-to-end and prove BOTH
    sub-stages run (stage_compare then stage_usergap)."""
    calls = []
    monkeypatch.setattr(S, "stage_compare", lambda ctx: calls.append("compare"))
    monkeypatch.setattr(S, "stage_usergap", lambda ctx: calls.append("usergap"))
    compare = S.build_stages()["compare"]
    compare({})
    assert calls == ["compare", "usergap"]


def test_build_stages_config_reaches_capture_values(store, tmp_path, monkeypatch):
    """build_stages()['config'] wires stage_capture_values into the config
    phase the same way — drive the real callable and prove both sub-stages run."""
    calls = []
    monkeypatch.setattr(S, "stage_config", lambda ctx: calls.append("config"))
    monkeypatch.setattr(S, "stage_capture_values",
                        lambda ctx: calls.append("capture_values"))
    config = S.build_stages()["config"]
    config({})
    assert calls == ["config", "capture_values"]


# ------------------------------------------------------------------ test 10
# Full confluence smoke (Task 16 / acceptance #3): a MockTransport site pair
# through create_app + the REAL RunEngine — wizard POSTs, every production
# stage, real store, real workspace files, analysis API. One space (DOCS),
# two pages of identical prose; the source Welcome page carries a structured
# macro the target lost. storage_text drops ac:* markup, so the bodies
# fingerprint EQUAL (no false content_mismatch for the dropped macro) and the
# run's only gap is the R7 macro-inventory finding -> verdict GAPS_FOUND.

_CONF_MACRO = '<ac:structured-macro ac:name="toc" ac:schema-version="1"/>'
_CONF_PROSE = {
    "Welcome": "<p>Welcome to the Acme documentation space.</p>",
    "Runbook": "<p>Operational runbook for the Globex cutover.</p>",
}


def _conf_page(pid, title, body, created):
    """Expanded v1 content row, exactly what _EXPAND asks for."""
    return {
        "id": pid, "title": title,
        "body": {"storage": {"value": body}},
        "version": {"number": 2},
        "history": {"createdDate": created,
                    "createdBy": {"displayName": "Igor Medeiros"}},
        "ancestors": [],
        "metadata": {"labels": {"results": [{"name": "docs"}]}},
        "children": {"comment": {"results": [], "size": 0},
                     "attachment": {"results": [], "size": 0}},
    }


def _conf_pair_handler(req):
    """Two cloud Confluence sites told apart by host: src.* carries the toc
    macro on its Welcome page, tgt.* lost it."""
    is_src = req.url.host.startswith("src")
    path = str(req.url.path)
    if path == "/wiki/rest/api/user/current":
        return httpx.Response(200, json={"displayName": "Igor Medeiros",
                                         "email": "igor@acme.example",
                                         "accountId": "acc-igor"})
    if path == "/wiki/api/v2/spaces":
        return httpx.Response(200, json={
            "results": [{"key": "DOCS", "name": "Acme Docs", "id": "111"}],
            "_links": {}})
    if path == "/wiki/rest/api/search":            # CQL count envelope (R11)
        return httpx.Response(200, json={"totalSize": 2})
    if path == "/wiki/rest/api/content/search":    # cloud page enumeration
        welcome = _CONF_PROSE["Welcome"] + (_CONF_MACRO if is_src else "")
        return httpx.Response(200, json={"results": [
            _conf_page("p1", "Welcome", welcome, "2026-01-05T10:00:00.000Z"),
            _conf_page("p2", "Runbook", _CONF_PROSE["Runbook"],
                       "2026-02-01T09:30:00.000Z"),
        ], "_links": {}})
    return httpx.Response(404, text="unmocked path: " + path)


def test_confluence_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    http = httpx.Client(transport=httpx.MockTransport(_conf_pair_handler))
    app = create_app(cfg, http=http)
    c = TestClient(app, follow_redirects=False)
    store = app.state.store

    # stage_verify mints clients without an http handle (prod hits the real
    # network); route the engine's clients through the SAME MockTransport
    # while keeping build_clients itself fully real (secret decryption,
    # connector resolution, make_client).
    real_build = S.build_clients
    monkeypatch.setattr(
        S, "build_clients",
        lambda store, mid, http=None, require_both=True:
            real_build(store, mid, http=app.state.http,
                       require_both=require_both))

    r = c.post("/migrations", data={"name": "acme wiki -> globex",
                                    "product": "confluence"})
    assert r.status_code == 303
    for role, site in (("source", "https://src.acme.example"),
                       ("target", "https://tgt.globex.example")):
        r = c.post("/migrations/1/connections",
                   data={"role": role, "site_url": site,
                         "email": "igor@acme.example", "api_token": "tok"})
        assert r.status_code == 303
        assert "error" not in r.headers["location"], r.headers["location"]

    r = c.post("/migrations/1/runs", data={})
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    rid = int(r.headers["location"].rsplit("/", 1)[1])

    deadline = time.time() + 15
    while time.time() < deadline:
        run = store.get_run(rid)
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "done", (
        "run did not finish clean; events: "
        + str([e["message"] for e in store.get_events(rid)]))

    # The macro gap is the ONLY finding: equal prose around the dropped macro
    # must not read as content drift, so the verdict comes from config.
    assert run["verdict"] == "GAPS_FOUND"
    rows = store.get_run_projects(rid)
    assert [row["key"] for row in rows] == ["DOCS"]
    assert rows[0]["src_count"] == 2 and rows[0]["tgt_count"] == 2
    assert rows[0]["status"] == "compared"
    assert rows[0]["fidelity_pct"] == 100.0

    s = c.get(f"/api/runs/{rid}/summary").json()
    assert s["product"] == "confluence"
    assert s["container_label"] == "space" and s["item_label"] == "page"
    assert s["verdict"] == "GAPS_FOUND"
    assert s["stats"]["config_missing"] == 1
    assert s["stats"]["issues_with_mismatches"] == 0

    cfg_rows = c.get(f"/api/runs/{rid}/config",
                     params={"area": "macros"}).json()["rows"]
    assert [(row["name"], row["kind"]) for row in cfg_rows] == \
        [("toc", "missing_in_tgt")]


# ------------------------------------------------------------------ test 11
# Full jira DC→Cloud smoke (acceptance #2): a MockTransport pair — DC source
# speaking /rest/api/2 with Bearer-PAT auth, keyset search and wiki-markup
# bodies; Cloud target speaking /rest/api/3 with Basic auth, cursor search
# and ADF bodies — through create_app + the REAL RunEngine. The same authored
# prose lives on both sides (different dialects, different timestamp
# spellings), so the run must emit ZERO issue-level findings — no false
# content_mismatch — while still catching the one REAL planted gap: a source
# status the target lacks (config missing_in_tgt) -> verdict GAPS_FOUND.

def _adf(*nodes):
    return {"type": "doc", "version": 1, "content": list(nodes)}


def _p(*content):
    return {"type": "paragraph", "content": list(content)}


def _t(text):
    return {"type": "text", "text": text}


def _jira_issue(key, iid, summary, desc, created, comments=()):
    return {"key": key, "id": iid, "fields": {
        "summary": summary, "description": desc,
        "issuetype": {"name": "Task"}, "status": {"name": "Open"},
        "created": created, "labels": ["migration"],
        "reporter": {"displayName": "Igor Medeiros"},
        "comment": {"total": len(comments), "comments": list(comments)},
    }}


def _comment(body, created):
    return {"author": {"displayName": "Igor Medeiros"},
            "created": created, "updated": created, "body": body}


# One instant, two platform spellings: DC emits +0000, Cloud +00:00 — they
# must normalize equal (norm_ts) or every common issue reads as drifted.
_TS1_DC, _TS1_CLOUD = "2026-01-05T10:00:00.000+0000", "2026-01-05T10:00:00.000+00:00"
_TS2_DC, _TS2_CLOUD = "2026-02-01T09:30:00.000+0000", "2026-02-01T09:30:00.000+00:00"

_DC_ISSUES = [
    _jira_issue("AC-1", "10001", "Login fails",
                "Hello *World*,\nline two [~imedeiros]", _TS1_DC),
    _jira_issue("AC-2", "10002", "Cutover runbook",
                "h1. Runbook\nOperational runbook for the Globex cutover.",
                _TS2_DC,
                comments=[_comment("Restart the *Globex* feed nightly",
                                   _TS2_DC)]),
]
_CLOUD_ISSUES = [
    _jira_issue("AC-1", "20001", "Login fails",
                _adf(_p(_t("Hello World,"), {"type": "hardBreak"},
                        _t("line two "),
                        {"type": "mention",
                         "attrs": {"id": "acc-igor", "text": "@Igor Medeiros"}})),
                _TS1_CLOUD),
    _jira_issue("AC-2", "20002", "Cutover runbook",
                _adf({"type": "heading", "attrs": {"level": 1},
                      "content": [_t("Runbook")]},
                     _p(_t("Operational runbook for the Globex cutover."))),
                _TS2_CLOUD,
                comments=[_comment(
                    _adf(_p(_t("Restart the Globex feed nightly"))),
                    _TS2_CLOUD)]),
]

# Config surface, R4-honest: areas with no DC list API are never requested
# (CLOUD_ONLY -> skipped); the rest answer in each deployment's envelope.
# The planted gap: source status "Escalated" missing on the target.
_DC_CONFIG = {
    "/rest/api/2/status": [{"name": "Open"}, {"name": "Escalated"}],
    "/rest/api/2/issuetype": [{"name": "Task"}],
    "/rest/api/2/priority": [],
    "/rest/api/2/resolution": [],
    "/rest/api/2/issueLinkType": {"issueLinkTypes": []},
    "/rest/api/2/role": [],
    "/rest/api/2/screens": [],                       # plain array slices
    "/rest/api/2/issuetypescheme": {"schemes": []},  # DC envelope key
    "/rest/api/2/permissionscheme": {"permissionSchemes": []},
    "/rest/api/2/notificationscheme": {"values": []},
    "/rest/api/2/field": [],
    "/rest/api/2/workflow": [],                      # plain array, no detail
}
_CLOUD_CONFIG = {
    "/rest/api/3/status": [{"name": "Open"}],        # Escalated is MISSING
    "/rest/api/3/issuetype": [{"name": "Task"}],
    "/rest/api/3/priority": [],
    "/rest/api/3/resolution": [],
    "/rest/api/3/issueLinkType": {"issueLinkTypes": []},
    "/rest/api/3/role": [],
    "/rest/api/3/screens": {"values": [], "isLast": True},
    "/rest/api/3/issuetypescheme": {"values": [], "isLast": True},
    "/rest/api/3/permissionscheme": {"permissionSchemes": []},
    "/rest/api/3/notificationscheme": {"values": [], "isLast": True},
    "/rest/api/3/field": [],
    "/rest/api/3/workflow/search": {"values": [], "isLast": True},
}


def _dc_cloud_pair_handler(req):
    path = str(req.url.path)
    auth = req.headers.get("Authorization", "")
    if req.url.host.startswith("dc."):
        # R2: a DC PAT is a first-class bearer token — Basic would 401 live.
        if auth != "Bearer dc-tok":
            return httpx.Response(401, text="DC expects Bearer PAT, got: " + auth[:12])
        if path == "/rest/api/2/myself":
            # DC realism: no accountId, no emailAddress.
            return httpx.Response(200, json={"displayName": "Igor Medeiros",
                                             "name": "imedeiros"})
        if path == "/rest/api/2/project":
            return httpx.Response(200, json=[
                {"key": "AC", "name": "AC Support", "id": "10000"}])
        if path == "/rest/api/2/search":
            if req.url.params.get("maxResults") == "0":   # count probe (R11)
                return httpx.Response(200, json={"total": 2})
            if "id >" in req.url.params.get("jql", ""):   # keyset page 2
                return httpx.Response(200, json={"issues": []})
            return httpx.Response(200, json={"issues": _DC_ISSUES, "total": 2})
        if path in _DC_CONFIG:
            return httpx.Response(200, json=_DC_CONFIG[path])
        if path == "/rest/servicedeskapi/servicedesk":
            return httpx.Response(200, json={"values": [], "isLastPage": True})
        return httpx.Response(404, text="unmocked DC path: " + path)
    if not auth.startswith("Basic "):
        return httpx.Response(401, text="Cloud expects Basic email:token")
    if path == "/rest/api/3/myself":
        return httpx.Response(200, json={"displayName": "Igor Medeiros",
                                         "emailAddress": "igor@globex.example",
                                         "accountId": "acc-igor"})
    if path == "/rest/api/3/project/search":
        return httpx.Response(200, json={"isLast": True, "values": [
            {"key": "AC", "name": "AC Support", "id": "20000",
             "insight": {"totalIssueCount": 2}}]})
    if path == "/rest/api/3/search/approximate-count":
        return httpx.Response(200, json={"count": 2})
    if path == "/rest/api/3/search/jql":
        return httpx.Response(200, json={"issues": _CLOUD_ISSUES,
                                         "isLast": True})
    if path in _CLOUD_CONFIG:
        return httpx.Response(200, json=_CLOUD_CONFIG[path])
    if path == "/rest/servicedeskapi/servicedesk":
        return httpx.Response(200, json={"values": [], "isLastPage": True})
    return httpx.Response(404, text="unmocked cloud path: " + path)


def test_jira_dc_to_cloud_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg = Config(data_dir=str(tmp_path / "data"), bind_host="127.0.0.1",
                 bind_port=8484, public_base_url="http://localhost:8484",
                 secret_key=None)
    http = httpx.Client(transport=httpx.MockTransport(_dc_cloud_pair_handler))
    app = create_app(cfg, http=http)
    c = TestClient(app, follow_redirects=False)
    store = app.state.store

    real_build = S.build_clients
    monkeypatch.setattr(
        S, "build_clients",
        lambda store, mid, http=None, require_both=True:
            real_build(store, mid, http=app.state.http,
                       require_both=require_both))

    r = c.post("/migrations", data={"name": "acme dc -> globex cloud",
                                    "product": "jira"})
    assert r.status_code == 303
    # DC source: token only, NO email (the wizard's dc contract).
    r = c.post("/migrations/1/connections",
               data={"role": "source", "site_url": "https://dc.acme.example",
                     "api_token": "dc-tok", "deployment": "dc"})
    assert r.status_code == 303
    assert "error" not in r.headers["location"], r.headers["location"]
    r = c.post("/migrations/1/connections",
               data={"role": "target",
                     "site_url": "https://globex.atlassian.net",
                     "email": "igor@globex.example", "api_token": "tok",
                     "deployment": "cloud"})
    assert r.status_code == 303
    assert "error" not in r.headers["location"], r.headers["location"]

    r = c.post("/migrations/1/runs", data={})
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    rid = int(r.headers["location"].rsplit("/", 1)[1])

    deadline = time.time() + 15
    while time.time() < deadline:
        run = store.get_run(rid)
        if run["status"] != "running":
            break
        time.sleep(0.05)
    assert run["status"] == "done", (
        "run did not finish clean; events: "
        + str([e["message"] for e in store.get_events(rid)]))

    # The planted status gap is the ONLY finding: same prose in wiki vs ADF
    # (and +0000 vs +00:00 created stamps) must NOT read as drift, so the
    # verdict comes from config, exactly one rung up the ladder.
    assert run["verdict"] == "GAPS_FOUND"
    rows = store.get_run_projects(rid)
    assert [row["key"] for row in rows] == ["AC"]
    assert rows[0]["src_count"] == 2 and rows[0]["tgt_count"] == 2
    assert rows[0]["status"] == "compared"
    assert rows[0]["fidelity_pct"] == 100.0

    s = c.get(f"/api/runs/{rid}/summary").json()
    assert s["product"] == "jira"
    assert s["container_label"] == "project" and s["item_label"] == "issue"
    assert s["verdict"] == "GAPS_FOUND"
    assert s["stats"]["issues_with_mismatches"] == 0
    assert s["stats"]["holes"] == 0 and s["stats"]["collisions"] == 0
    assert s["stats"]["config_missing"] == 1
    # ZERO issue-level findings of any kind — the no-false-content-mismatch
    # acceptance check, asserted at the persisted-findings level.
    assert c.get(f"/api/runs/{rid}/issues").json()["total"] == 0

    cfg_rows = c.get(f"/api/runs/{rid}/config",
                     params={"area": "statuses"}).json()["rows"]
    assert [(row["name"], row["kind"]) for row in cfg_rows] == \
        [("Escalated", "missing_in_tgt")]
    # R4 honesty: the five no-DC-API areas are skipped loudly, never silent.
    skipped = c.get(f"/api/runs/{rid}/config").json()["skipped"]
    assert set(skipped) == {"screen_schemes", "issuetype_screen_schemes",
                            "workflow_schemes", "field_configurations",
                            "field_config_schemes"}
    assert s["stats"]["config_skipped"] == 5

    # R9 honesty: the dc SOURCE side gets one explicit blind-spot warn while
    # the cloud target is probed normally (insight 2 vs search 2 -> clean).
    warns = [e["message"] for e in store.get_events(rid)
             if e["level"] == "warn"]
    assert [w for w in warns if "not supported" in w and "source" in w]
    assert s["stats"]["blind_spots"] == 0


# ------------------------------------------------------------------ test 12+13
# Overlap source+target extraction per project (Task 2).

def _issues_client(n):
    issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": f"s{i}"}}
              for i in range(n)]
    def handler(req):
        p = str(req.url.path)
        if p.endswith("approximate-count"):
            return httpx.Response(200, json={"count": n})
        if p.endswith("search/jql"):
            return httpx.Response(200, json={"issues": issues, "isLast": True})
        return httpx.Response(404)          # /field -> handled as no custom fields
    conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                      email="e", api_token="t")
    return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleeper=lambda s: None)


def _lines(path):
    with gzip.open(path, "rt") as f:
        return [ln for ln in f]


def test_stage_extract_seq_vs_parallel_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_say", lambda *a, **k: None)
    def run(workers, sub):
        monkeypatch.setenv("MA_EXTRACT_WORKERS", str(workers))
        ws = str(tmp_path / sub)
        os.makedirs(os.path.join(ws, "src")); os.makedirs(os.path.join(ws, "tgt"))
        ctx = {"connector": JIRA, "src": _issues_client(3), "tgt": _issues_client(3),
               "selected": [{"key": "AC", "name": "AC", "src_count": 3, "tgt_count": 3}],
               "workspace": ws, "params": {}}
        S.stage_extract(ctx)
        return ws
    seq, par = run(1, "seq"), run(2, "par")
    for side in ("src", "tgt"):
        a = _lines(os.path.join(seq, side, "AC.core.jsonl.gz"))
        b = _lines(os.path.join(par, side, "AC.core.jsonl.gz"))
        assert a == b
        assert sum(1 for ln in b if '"AC-' in ln) == 3


def test_stage_extract_gating_raises_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_say", lambda *a, **k: None)
    monkeypatch.setenv("MA_EXTRACT_WORKERS", "2")
    def client(n_issues, n_count):
        issues = [{"key": f"AC-{i}", "id": str(i), "fields": {"summary": "s"}}
                  for i in range(n_issues)]
        def h(req):
            p = str(req.url.path)
            if p.endswith("approximate-count"):
                return httpx.Response(200, json={"count": n_count})
            if p.endswith("search/jql"):
                return httpx.Response(200, json={"issues": issues, "isLast": True})
            return httpx.Response(404)
        conn = Connection(auth_type="pat", site_url="https://x.atlassian.net",
                          email="e", api_token="t")
        return JiraClient(conn, http=httpx.Client(transport=httpx.MockTransport(h)),
                          sleeper=lambda s: None)
    ws = str(tmp_path)
    os.makedirs(os.path.join(ws, "src")); os.makedirs(os.path.join(ws, "tgt"))
    ctx = {"connector": JIRA, "src": client(3, 3), "tgt": client(2, 5),
           "selected": [{"key": "AC", "name": "AC", "src_count": 3, "tgt_count": 5}],
           "workspace": ws, "params": {}}
    with pytest.raises(RuntimeError, match="refusing to compare"):
        S.stage_extract(ctx)
