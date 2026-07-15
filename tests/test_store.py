import json
import os
import pytest
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_wal_mode_and_migration_index(store):
    # Review (data layer): WAL so readers don't serialize behind the writer, and
    # an index on the runs.migration_id hot path.
    mode = store.db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() in ("wal", "memory")
    idx = {r[1] for r in store.db.execute("PRAGMA index_list(runs)")}
    assert "ix_runs_mig" in idx


def test_wal_sidecars_are_owner_only(tmp_path):
    # No-bias review: WAL adds -wal/-shm sidecars holding recently-written pages
    # (encrypted secrets + clear findings); they must be hardened like the DB.
    import os, stat
    Store(db_path=str(tmp_path / "w.db"), key_path=str(tmp_path / "w.key"))
    for side in ("w.db-wal", "w.db-shm"):
        p = tmp_path / side
        if p.exists():        # WAL active on this filesystem
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, side


def test_prune_events_bounds_the_log(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    for i in range(50):
        store.add_event(rid, "p", "info", f"event {i}")
    deleted = store.prune_events(keep_per_run=10)
    assert deleted == 40
    rows = store.db.execute("SELECT COUNT(*) FROM events WHERE run_id=?",
                            (rid,)).fetchone()[0]
    assert rows == 10
    msgs = [r["message"] for r in store._rows(
        "SELECT message FROM events WHERE run_id=? ORDER BY id", (rid,))]
    assert msgs[-1] == "event 49"          # the most recent events are kept


def test_vacuum_runs_without_error(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.delete_run(rid)
    store.vacuum()                          # must not raise


def test_secret_roundtrip_and_keyfile_created(tmp_path):
    s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
    blob = s.encrypt({"email": "a@b.c", "token": "shh"})
    assert b"shh" not in blob
    assert s.decrypt(blob) == {"email": "a@b.c", "token": "shh"}
    assert (tmp_path / ".key").exists()
    # a second Store with the same keyfile can decrypt
    s2 = Store(db_path=str(tmp_path / "t2.db"), key_path=str(tmp_path / ".key"))
    assert s2.decrypt(blob)["token"] == "shh"


def test_explicit_secret_key_skips_keyfile(tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"),
              secret_key=key)
    assert s.decrypt(s.encrypt({"x": 1})) == {"x": 1}
    assert not (tmp_path / ".key").exists()


def test_settings_roundtrip(store):
    assert store.settings_get("oauth_client_id") is None
    store.settings_set("oauth_client_id", "abc")
    store.settings_set("oauth_client_id", "abc2")
    assert store.settings_get("oauth_client_id") == "abc2"


def test_migration_and_connection_crud(store):
    mid = store.create_migration("acme->globex")
    assert store.get_migration(mid)["name"] == "acme->globex"
    assert [m["id"] for m in store.list_migrations()] == [mid]
    store.save_connection(mid, "source", "pat", "https://src.atlassian.net",
                          secret={"email": "a@b.c", "token": "t1"},
                          account_email="a@b.c")
    store.save_connection(mid, "source", "pat", "https://src2.atlassian.net",
                          secret={"email": "a@b.c", "token": "t2"},
                          account_email="a@b.c")          # upsert by (mig, role)
    row = store.get_connection(mid, "source")
    assert row["site_url"] == "https://src2.atlassian.net"
    assert store.connection_secret(row)["token"] == "t2"
    assert store.get_connection(mid, "target") is None


def test_migration_product_must_have_a_registered_connector(store):
    a = store.create_migration("A")
    assert store.get_migration(a)["product"] == "jira"
    # The connector registry is the single source of truth: a migration row
    # whose product no connector can serve would 500 on every follow-up step
    # (connections, scope, runs). "bamboo" raises forever; registering a
    # connector (e.g. confluence in Task 13) auto-enables creation via the
    # known_products() loop below — no store change needed.
    with pytest.raises(ValueError):
        store.create_migration("C", product="bamboo")
    from auditor.connectors import known_products
    assert "confluence" in known_products()
    for product in known_products():
        mid = store.create_migration(f"ok-{product}", product=product)
        assert store.get_migration(mid)["product"] == product


def test_connection_deployment_persisted(store):
    mid = store.create_migration("acme->globex")
    store.save_connection(mid, "source", "pat", "https://jira.acme.example",
                          secret={"token": "t1"}, deployment="dc")
    assert store.get_connection(mid, "source")["deployment"] == "dc"
    store.save_connection(mid, "target", "pat", "https://globex.atlassian.net",
                          secret={"email": "igor@globex.example", "token": "t2"},
                          account_email="igor@globex.example")
    assert store.get_connection(mid, "target")["deployment"] == "cloud"
    # upsert by (migration, role) must also update deployment
    store.save_connection(mid, "source", "pat", "https://acme.atlassian.net",
                          secret={"email": "igor@acme.example", "token": "t3"},
                          account_email="igor@acme.example", deployment="cloud")
    assert store.get_connection(mid, "source")["deployment"] == "cloud"
    with pytest.raises(ValueError):
        store.save_connection(mid, "source", "pat", "https://jira.acme.example",
                              secret={"token": "t"}, deployment="server")


# The pre-multi-product schema, verbatim minus product/deployment: upgrades of
# live auditor.db files must backfill via _migrate, not require a fresh DB.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id INTEGER NOT NULL REFERENCES migrations(id),
  role TEXT NOT NULL CHECK(role IN ('source','target')),
  auth_type TEXT NOT NULL CHECK(auth_type IN ('oauth','pat')),
  site_url TEXT NOT NULL, cloud_id TEXT, account_email TEXT,
  secret_enc BLOB NOT NULL, status TEXT DEFAULT 'new', verified_at REAL,
  UNIQUE(migration_id, role));
"""


def test_schema_upgrade_in_place(tmp_path):
    import sqlite3
    import time
    path = str(tmp_path / "v1.db")
    db = sqlite3.connect(path)
    db.executescript(_V1_SCHEMA)
    db.execute("INSERT INTO migrations(name,created_at) VALUES(?,?)",
               ("acme legacy", time.time()))
    db.commit()
    db.close()

    s = Store(db_path=path, key_path=str(tmp_path / ".key"))
    assert s.get_migration(1)["product"] == "jira"
    s.save_connection(1, "source", "pat", "https://jira.acme.example",
                      secret={"token": "t"}, deployment="dc")
    assert s.get_connection(1, "source")["deployment"] == "dc"

    # _migrate must also add runs.kind (default 'audit') and
    # findings_config/findings_issue.fix_payload (NULL on existing rows).
    rid = s.create_run(1, {})
    assert s.get_run(rid)["kind"] == "audit"
    s.insert_findings_config(rid, [
        {"area": "statuses", "name": "Open", "kind": "missing_in_tgt", "detail": {}}])
    assert s.query_config(rid, "statuses")[0]["fix_payload"] is None
    s.insert_findings_issue(rid, [
        {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-1",
         "tgt_key": None, "field": None, "summary": "gone", "detail": {}}])
    rows, _ = s.query_issues(rid)
    assert rows[0]["fix_payload"] is None
    s.db.close()

    # reopening the upgraded DB is a no-op (idempotent _migrate)
    s2 = Store(db_path=path, key_path=str(tmp_path / ".key"))
    assert s2.get_migration(1)["product"] == "jira"
    assert s2.get_connection(1, "source")["deployment"] == "dc"


# Schema that already has a 'runs' table but predates the kind/source_run_id
# columns and findings tables that predate fix_payload.  This is the critical
# gap: the existing test_schema_upgrade_in_place omits 'runs' entirely so the
# table is created fresh from _SCHEMA (all columns present) — _migrate's ALTER
# TABLE branches for kind/source_run_id are never exercised on an existing row.
_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  product TEXT NOT NULL DEFAULT 'jira', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id INTEGER NOT NULL REFERENCES migrations(id),
  role TEXT NOT NULL CHECK(role IN ('source','target')),
  auth_type TEXT NOT NULL CHECK(auth_type IN ('oauth','pat')),
  site_url TEXT NOT NULL, deployment TEXT NOT NULL DEFAULT 'cloud',
  cloud_id TEXT, account_email TEXT,
  secret_enc BLOB NOT NULL, status TEXT DEFAULT 'new', verified_at REAL,
  UNIQUE(migration_id, role));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id INTEGER NOT NULL REFERENCES migrations(id),
  status TEXT NOT NULL DEFAULT 'running',
  phase TEXT DEFAULT 'verify', verdict TEXT,
  started_at REAL NOT NULL, finished_at REAL,
  params_json TEXT NOT NULL DEFAULT '{}',
  stats_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS run_projects (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  key TEXT NOT NULL, name TEXT, src_count INTEGER, tgt_count INTEGER,
  missing INTEGER, tail_count INTEGER, fidelity_pct REAL,
  blind_spot INTEGER DEFAULT 0, status TEXT,
  PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS findings_issue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  project TEXT NOT NULL, kind TEXT NOT NULL,
  src_key TEXT, tgt_key TEXT, field TEXT, summary TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS findings_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  area TEXT NOT NULL, name TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS fix_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  finding_ref TEXT, fix_id TEXT NOT NULL, object_name TEXT,
  method TEXT, path TEXT, status INTEGER, ok INTEGER DEFAULT 0,
  created_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts REAL NOT NULL, phase TEXT, level TEXT DEFAULT 'info', message TEXT);
"""


def test_schema_upgrade_legacy_runs_table(tmp_path):
    """I9: _migrate must add kind/source_run_id to an EXISTING runs table and
    fix_payload to existing findings tables, then backfill old rows with the
    correct defaults (kind='audit', fix_payload=NULL).

    The original test_schema_upgrade_in_place uses _V1_SCHEMA which has NO
    runs table, so _SCHEMA creates it fresh with every column already present —
    the ALTER TABLE branches in _migrate are never hit.  This test uses
    _V2_SCHEMA which includes a runs table without those columns."""
    import sqlite3
    import time

    path = str(tmp_path / "v2.db")
    db = sqlite3.connect(path)
    db.executescript(_V2_SCHEMA)
    # Insert a pre-existing run row that will be backfilled by _migrate.
    db.execute("INSERT INTO migrations(name,created_at) VALUES(?,?)",
               ("acme legacy", time.time()))
    db.execute("INSERT INTO runs(migration_id,started_at) VALUES(?,?)",
               (1, time.time()))
    db.commit()
    db.close()

    # Opening through Store must not raise — _migrate upgrades the schema in
    # place and the pre-existing run row gets kind='audit' by DEFAULT.
    s = Store(db_path=path, key_path=str(tmp_path / ".key"))

    # Pre-existing run gets kind='audit' backfilled by ALTER TABLE DEFAULT.
    existing_run = s.get_run(1)
    assert existing_run is not None
    assert existing_run["kind"] == "audit", (
        "_migrate must backfill kind='audit' on rows that pre-date the column")
    assert existing_run["source_run_id"] is None, (
        "_migrate must add source_run_id (nullable, defaults NULL)")

    # The columns must be present on the findings tables too so fix_payload
    # writes (None) work without raising.  Insert a config finding and an issue
    # finding with no payload — both should come back as None.
    s.insert_findings_config(1, [
        {"area": "statuses", "name": "Triage", "kind": "missing_in_tgt",
         "detail": {}}])
    assert s.query_config(1, "statuses")[0]["fix_payload"] is None, (
        "_migrate must add fix_payload TEXT to findings_config")

    s.insert_findings_issue(1, [
        {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-1",
         "tgt_key": None, "field": None, "summary": "gone", "detail": {}}])
    rows, _ = s.query_issues(1)
    assert rows[0]["fix_payload"] is None, (
        "_migrate must add fix_payload TEXT to findings_issue")

    # create_run and get_fix_actions must work end-to-end on the upgraded DB.
    new_rid = s.create_run(1, {"projects": ["AC"]})
    assert s.get_run(new_rid)["kind"] == "audit"

    # A fix run linked to the audit run must also persist correctly.
    fix_rid = s.create_run(1, {"fix_ids": ["jira.custom_field.create"]},
                           kind="fix", source_run_id=new_rid)
    fix_row = s.get_run(fix_rid)
    assert fix_row["kind"] == "fix"
    assert fix_row["source_run_id"] == new_rid

    s.insert_fix_actions(fix_rid, [
        {"finding_ref": "statuses/Triage", "fix_id": "jira.status.create",
         "object_name": "Triage", "method": "POST", "path": "/rest/api/3/statuses",
         "status": 200, "ok": True, "created_id": "10020", "error": None}])
    actions = s.get_fix_actions(fix_rid)
    assert len(actions) == 1 and actions[0]["ok"] == 1

    s.db.close()

    # Reopening the already-upgraded DB is a no-op (idempotent _migrate).
    s2 = Store(db_path=path, key_path=str(tmp_path / ".key"))
    assert s2.get_run(1)["kind"] == "audit"
    s2.db.close()


def test_run_lifecycle_and_active_guard(store):
    mid = store.create_migration("m")
    rid = store.create_run(mid, {"projects": ["AC"]})
    assert store.active_run(mid)["id"] == rid
    store.update_run(rid, status="done", verdict="CLEAN",
                     stats={"issues": 5}, phase="finalize")
    r = store.get_run(rid)
    assert r["status"] == "done" and r["verdict"] == "CLEAN"
    assert json.loads(r["stats_json"])["issues"] == 5
    assert store.active_run(mid) is None
    assert len(store.list_runs(mid)) == 1


def test_run_projects_roundtrip(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.set_run_projects(rid, [
        {"key": "AC", "name": "AC Support", "src_count": 100, "tgt_count": 95,
         "missing": 5, "tail_count": 5, "fidelity_pct": 99.0,
         "blind_spot": 0, "status": "done"}])
    rows = store.get_run_projects(rid)
    assert rows[0]["key"] == "AC" and rows[0]["tgt_count"] == 95


def test_findings_issue_pagination_and_filters(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    rows = []
    for i in range(120):
        rows.append({"project": "AC", "kind": "missing_in_tgt",
                     "src_key": f"AC-{i}", "tgt_key": None, "field": None,
                     "summary": f"issue {i}", "detail": {"n": i}})
    rows.append({"project": "MS", "kind": "field_mismatch", "src_key": "MS-1",
                 "tgt_key": "MS-1", "field": "status",
                 "summary": "status differs", "detail": {}})
    store.insert_findings_issue(rid, rows)
    page1, total = store.query_issues(rid, page=1, size=50)
    assert total == 121 and len(page1) == 50
    only_ms, t2 = store.query_issues(rid, project="MS")
    assert t2 == 1 and only_ms[0]["field"] == "status"
    hits, t3 = store.query_issues(rid, q="issue 11")
    assert t3 >= 1 and all("issue 11" in h["summary"] for h in hits)
    byk, t4 = store.query_issues(rid, kind="field_mismatch")
    assert t4 == 1


def test_all_issue_findings_bulk_read(store):
    # The display-time fidelity derivation reads EVERY finding in one pass
    # (not the O(n^2) page-by-page loop). Bulk read must return the whole set
    # and honor a project scope, with detail_json still present for parsing.
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    rows = []
    for i in range(250):
        rows.append({"project": "AC", "kind": "field_mismatch",
                     "src_key": f"AC-{i}", "tgt_key": f"AC-{i}",
                     "field": "priority", "summary": f"prio {i}",
                     "detail": {"src": "Major", "tgt": None}})
    rows.append({"project": "MS", "kind": "field_mismatch", "src_key": "MS-1",
                 "tgt_key": "MS-1", "field": "status", "summary": "s",
                 "detail": {"src": "Open", "tgt": "Done"}})
    store.insert_findings_issue(rid, rows)

    all_rows = store.all_issue_findings(rid)
    _page, total = store.query_issues(rid, page=1, size=50)
    assert len(all_rows) == total == 251
    assert "detail_json" in all_rows[0]

    ac_only = store.all_issue_findings(rid, project="AC")
    assert len(ac_only) == 250 and all(r["project"] == "AC" for r in ac_only)


def test_findings_config_and_areas(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.insert_findings_config(rid, [
        {"area": "statuses", "name": "On Hold", "kind": "missing_in_tgt", "detail": {}},
        {"area": "custom_fields", "name": "Squad", "kind": "missing_in_tgt", "detail": {}},
    ])
    assert set(store.config_areas(rid)) == {"statuses", "custom_fields"}
    rows = store.query_config(rid, "statuses")
    assert rows[0]["name"] == "On Hold"


def test_events_stream(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.add_event(rid, "extract", "info", "AC 100/200")
    store.add_event(rid, "extract", "info", "AC 200/200")
    evs = store.get_events(rid)
    assert len(evs) == 2
    later = store.get_events(rid, after_id=evs[0]["id"])
    assert len(later) == 1 and later[0]["message"] == "AC 200/200"


def test_concurrent_writer_and_readers_do_not_crash(store):
    import threading
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            store.insert_findings_issue(rid, [
                {"project": "AC", "kind": "missing_in_tgt",
                 "src_key": f"AC-{i}", "tgt_key": None, "field": None,
                 "summary": "x", "detail": {}}])
            store.add_event(rid, "extract", "info", f"n={i}")
            i += 1

    def reader():
        while not stop.is_set():
            try:
                store.get_events(rid)
                store.query_issues(rid, page=1, size=20)
                store.issue_kind_counts(rid)
            except Exception as exc:   # noqa: BLE001 - the test asserts none occur
                errors.append(exc)
                stop.set()

    threads = [threading.Thread(target=writer)] + \
        [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    import time as _t
    _t.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert errors == [], f"concurrent access crashed: {errors[:3]}"


def test_foreign_keys_enforced(store):
    import sqlite3, pytest
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_findings_issue(99999, [
            {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-1",
             "tgt_key": None, "field": None, "summary": "orphan", "detail": {}}])


def test_create_fix_run_carries_kind_and_source(store):
    mid = store.create_migration("m")
    audit = store.create_run(mid, {})
    fix = store.create_run(mid, {"fix_ids": ["jira.custom_field.create"]},
                           kind="fix", source_run_id=audit)
    row = store.get_run(fix)
    assert row["kind"] == "fix" and row["source_run_id"] == audit
    assert store.get_run(audit)["kind"] == "audit"   # default stored


def test_create_run_rejects_unknown_kind(store):
    mid = store.create_migration("m")
    with pytest.raises(ValueError):
        store.create_run(mid, {}, kind="dry_run")
    with pytest.raises(ValueError):
        store.create_run(mid, {}, kind="Fix")


def test_fix_actions_roundtrip(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {}, kind="fix")
    store.insert_fix_actions(rid, [
        {"finding_ref": "custom_fields/Severity", "fix_id": "jira.custom_field.create",
         "object_name": "Severity", "method": "POST", "path": "/rest/api/3/field",
         "status": 201, "ok": True, "created_id": "customfield_10099", "error": None},
        {"finding_ref": "statuses/Triage", "fix_id": "jira.status.create",
         "object_name": "Triage", "method": "POST", "path": "/rest/api/3/statuses",
         "status": 400, "ok": False, "created_id": None, "error": "exists"}])
    acts = store.get_fix_actions(rid)
    assert len(acts) == 2 and acts[0]["created_id"] == "customfield_10099"
    assert acts[1]["ok"] == 0


def test_warns_when_secret_perms_silently_noop(tmp_path, monkeypatch, caplog):
    # On a filesystem that ignores chmod (WSL drvfs / network mounts) the DB
    # file and the data DIRECTORY stay readable by other local users. The Store
    # must WARN about BOTH so the operator isn't falsely assured (no-bias review:
    # the data dir was never re-checked).
    import os
    import logging
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)   # simulate no-op FS
    old_umask = os.umask(0)                                  # files+dir created loose
    try:
        # Nested data dir so makedirs creates it (loosely, under umask 0) — the
        # pytest tmp_path itself is already 0700 and would mask the dir gap.
        d = tmp_path / "data"
        with caplog.at_level(logging.WARNING):
            Store(db_path=str(d / "t.db"), key_path=str(d / ".key"))
    finally:
        os.umask(old_umask)
    msgs = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "owner-only" in msgs
    assert "database" in msgs      # the DB file is flagged
    assert "directory" in msgs     # AND the data directory (the review gap)


def test_key_file_created_atomically_owner_only(tmp_path, monkeypatch):
    # The master Fernet key must be created owner-only ATOMICALLY, never written
    # world-readable then chmod'd (no-bias review: TOCTOU on a working FS). Proof:
    # even with chmod neutralised, the freshly generated key is 0600.
    import os
    import stat
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    kp = tmp_path / "k.key"
    Store(db_path=str(tmp_path / "k.db"), key_path=str(kp))
    assert stat.S_IMODE(os.stat(kp).st_mode) == 0o600


def test_strict_perms_fails_closed(tmp_path, monkeypatch):
    # MA_STRICT_PERMS makes a deployment fail-closed: if at-rest hardening can't
    # be enforced (chmod no-ops), construction RAISES rather than logging a line
    # nobody reads (no-bias review: the warning was only a log).
    import os
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setenv("MA_STRICT_PERMS", "1")
    old_umask = os.umask(0)
    try:
        with pytest.raises((PermissionError, RuntimeError)):
            Store(db_path=str(tmp_path / "s.db"), key_path=str(tmp_path / "s.key"))
    finally:
        os.umask(old_umask)


def test_owner_only_perms_emit_no_warning(tmp_path, caplog):
    # A normal POSIX filesystem where chmod works must NOT warn.
    import logging
    with caplog.at_level(logging.WARNING):
        Store(db_path=str(tmp_path / "ok.db"), key_path=str(tmp_path / "ok.key"))
    msgs = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "owner-only" not in msgs


def test_failed_batch_write_rolls_back_no_partial_leak(store):
    # A write that fails partway (NOT NULL violation on row 2 of an executemany)
    # must roll back row 1, not leave it pending for the next unrelated commit()
    # to silently flush (review: 'no rollback' critical).
    import sqlite3
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    good = {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-1"}
    bad = {"project": None, "kind": "x"}     # project is NOT NULL -> fails mid-batch
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_findings_issue(rid, [good, bad])
    n = store.db.execute("SELECT COUNT(*) FROM findings_issue WHERE run_id=?",
                         (rid,)).fetchone()[0]
    assert n == 0, "row 1 must be rolled back, not left pending"
    # A later committing write must not flush an orphaned pending row.
    store.create_run(mid, {})
    n2 = store.db.execute("SELECT COUNT(*) FROM findings_issue WHERE run_id=?",
                          (rid,)).fetchone()[0]
    assert n2 == 0


def test_txn_rolls_back_when_commit_itself_raises(store):
    # If commit() raises (disk full / SQLITE_BUSY), the transaction must be
    # rolled back and the connection left clean — otherwise the pending rows
    # leak into the NEXT unrelated commit (no-bias review: critical, the commit
    # path was uncovered by the original rollback fix).
    import sqlite3
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    real = store.db
    fail = [True]

    class _CommitBoom:
        def __getattr__(self, n):
            return getattr(real, n)

        def commit(self):
            if fail[0]:
                raise sqlite3.OperationalError("disk I/O error")
            return real.commit()

    store.db = _CommitBoom()
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.insert_findings_issue(
                rid, [{"project": "AC", "kind": "missing_in_tgt"}])
        # The connection must NOT be left mid-transaction.
        assert real.in_transaction is False
    finally:
        fail[0] = False
        store.db = real
    # The failed row must not be flushed by a later unrelated write.
    store.create_run(mid, {})
    n = store.db.execute("SELECT COUNT(*) FROM findings_issue WHERE run_id=?",
                         (rid,)).fetchone()[0]
    assert n == 0


def test_delete_run_referenced_by_fix_run_does_not_raise(store):
    # runs.source_run_id REFERENCES runs(id); deleting an audit run that a fix
    # run points at must NOT raise a FK IntegrityError (review: delete_run high).
    mid = store.create_migration("m")
    audit = store.create_run(mid, {}, kind="env_audit")
    fix = store.create_run(mid, {}, kind="env_fix", source_run_id=audit)
    store.delete_run(audit)                       # must not raise
    assert store.get_run(audit) is None
    # The fix run survives with its now-dangling source nulled out.
    assert store.get_run(fix) is not None
    assert store.get_run(fix)["source_run_id"] is None


def test_delete_migration_with_cross_referencing_runs(store):
    # delete_migration bulk-deletes every run of a migration; a fix run pointing
    # at an audit run in the same migration must not trip the self-FK.
    mid = store.create_migration("m")
    audit = store.create_run(mid, {}, kind="env_audit")
    store.create_run(mid, {}, kind="env_fix", source_run_id=audit)
    store.delete_migration(mid)                   # must not raise
    assert store.get_migration(mid) is None


def test_append_fix_action_write_through(store):
    # Durability (review Bug 4): each live-write action must be persistable the
    # instant it fires, so a crash mid-apply can't erase the record of DELETEs
    # already sent to production. append_fix_action commits one row immediately.
    mid = store.create_migration("m"); rid = store.create_run(mid, {}, kind="env_fix")
    store.append_fix_action(rid, {
        "finding_ref": "schemes/Old", "fix_id": "scheme_unused",
        "object_name": "Old", "method": "DELETE",
        "path": "/rest/api/3/workflowscheme/101", "status": 204, "ok": True,
        "created_id": None, "error": None})
    store.append_fix_action(rid, {
        "finding_ref": "schemes/Gone", "fix_id": "scheme_unused",
        "object_name": "Gone", "method": "DELETE",
        "path": "/rest/api/3/workflowscheme/102", "status": 204, "ok": True})
    acts = store.get_fix_actions(rid)
    assert [a["object_name"] for a in acts] == ["Old", "Gone"]   # insertion order
    assert acts[0]["method"] == "DELETE" and acts[0]["ok"] == 1
    # fix_id NOT NULL net (missing -> safe fallback), same as the bulk insert.
    store.append_fix_action(rid, {"object_name": "x", "method": "GET"})
    assert store.get_fix_actions(rid)[-1]["fix_id"] == "env_fix"


def test_config_findings_persist_fix_payload(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.insert_findings_config(rid, [
        {"area": "custom_fields", "name": "Severity", "kind": "missing_in_tgt",
         "detail": {"type": "select"},
         "fix_payload": {"type": "select", "contexts": [{"name": "Default",
                         "options": ["High", "Low"]}]}}])
    rows = store.query_config(rid, "custom_fields")
    assert rows[0]["fix_payload"]["contexts"][0]["options"] == ["High", "Low"]


def test_issue_findings_persist_fix_payload(store):
    mid = store.create_migration("m"); rid = store.create_run(mid, {})
    store.insert_findings_issue(rid, [
        {"project": "AC", "kind": "missing_in_tgt", "src_key": "AC-1",
         "tgt_key": None, "field": None, "summary": "gone",
         "detail": {"n": 1},
         "fix_payload": {"action": "create", "issue_key": "AC-1"}},
        {"project": "AC", "kind": "field_mismatch", "src_key": "AC-2",
         "tgt_key": "AC-2", "field": "priority", "summary": "prio drift",
         "detail": {}, "fix_payload": None}])
    rows, total = store.query_issues(rid)
    assert total == 2
    fp = rows[0]["fix_payload"]
    assert fp["action"] == "create" and fp["issue_key"] == "AC-1"
    assert rows[1]["fix_payload"] is None


def test_finding_solutions_roundtrip(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "sol.db"), str(tmp_path / "sol.key"))
    mid = s.create_migration("m"); rid = s.create_run(mid, {})
    assert s.get_solutions(rid, "sig1") is None
    s.save_solutions(rid, "sig1", {"solutions": [{"title": "x"}], "model": "m"})
    got = s.get_solutions(rid, "sig1")
    assert got["payload"]["solutions"][0]["title"] == "x"
    assert isinstance(got["created_at"], float)
    # overwrite (refresh)
    s.save_solutions(rid, "sig1", {"solutions": [], "model": "m"})
    assert s.get_solutions(rid, "sig1")["payload"]["solutions"] == []


def test_audit_type_column(tmp_path):
    from webapp.store import Store
    s = Store(str(tmp_path / "at.db"), str(tmp_path / "at.key"))
    mid = s.create_migration("Acme env", product="jira", audit_type="environment")
    assert s.get_migration(mid)["audit_type"] == "environment"
    mid2 = s.create_migration("Acme mig")
    assert s.get_migration(mid2)["audit_type"] == "migration"   # default


def test_schema_version_is_stamped(store):
    from webapp.store import SCHEMA_VERSION
    assert store.schema_version() == SCHEMA_VERSION >= 1


def test_db_ping_ok(store):
    assert store.db_ping() is True


def test_backup_writes_consistent_snapshot(store, tmp_path):
    store.settings_set("provider", "anthropic")     # some state to copy
    dest = str(tmp_path / "snap.db")
    out = store.backup(dest)
    assert out == dest and os.path.exists(dest)
    # The snapshot is a real, openable DB carrying the same data + schema stamp.
    snap = Store(db_path=dest, key_path=str(tmp_path / "snap.key"))
    assert snap.settings_get("provider") == "anthropic"
    assert snap.schema_version() == store.schema_version()


def test_backup_refuses_to_overwrite(store, tmp_path):
    import pytest
    # Refuses for ANY existing destination — including a 0-byte file, which
    # VACUUM INTO would silently overwrite (the explicit exists-check guards it).
    for content in (b"prior-valid-or-garbage", b""):
        dest = tmp_path / "exists.db"
        dest.write_bytes(content)
        with pytest.raises(FileExistsError):
            store.backup(str(dest))
        assert dest.read_bytes() == content          # never clobbered
