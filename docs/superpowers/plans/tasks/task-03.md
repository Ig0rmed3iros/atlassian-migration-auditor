### Task 3: `webapp/store.py` — SQLite store + encrypted secrets

**Files:**
- Create: `webapp/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:
```python
import json
import pytest
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_store.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.store'`.

- [ ] **Step 3: Write the implementation**

`webapp/store.py`:
```python
"""SQLite persistence + Fernet-encrypted secrets.

Single-file DB under MA_DATA_DIR. All methods synchronous; the connection is
created with check_same_thread=False because the run engine thread and the
request threads share the Store (sqlite serializes writes itself; our writes
are short transactions).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from cryptography.fernet import Fernet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
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
CREATE INDEX IF NOT EXISTS ix_fi ON findings_issue (run_id, project, kind);
CREATE TABLE IF NOT EXISTS findings_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  area TEXT NOT NULL, name TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS ix_fc ON findings_config (run_id, area);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts REAL NOT NULL, phase TEXT, level TEXT DEFAULT 'info', message TEXT);
CREATE INDEX IF NOT EXISTS ix_ev ON events (run_id, id);
"""


class Store:
    def __init__(self, db_path: str, key_path: str, secret_key: str | None = None):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        if secret_key:
            key = secret_key.encode()
        elif os.path.exists(key_path):
            key = open(key_path, "rb").read()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as fh:
                fh.write(key)
            os.chmod(key_path, 0o600)
        self._fernet = Fernet(key)

    # ------------------------------------------------------------- secrets
    def encrypt(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def decrypt(self, blob: bytes) -> dict:
        return json.loads(self._fernet.decrypt(bytes(blob)))

    # -------------------------------------------------------------- helpers
    def _exec(self, sql, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _row(self, sql, args=()):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------- settings
    def settings_get(self, key: str) -> str | None:
        r = self._row("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else None

    def settings_set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO settings(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    def settings_delete(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key=?", (key,))

    # ----------------------------------------------------------- migrations
    def create_migration(self, name: str) -> int:
        return self._exec("INSERT INTO migrations(name,created_at) VALUES(?,?)",
                          (name, time.time())).lastrowid

    def list_migrations(self) -> list[dict]:
        return self._rows("SELECT * FROM migrations ORDER BY id DESC")

    def get_migration(self, mid: int) -> dict | None:
        return self._row("SELECT * FROM migrations WHERE id=?", (mid,))

    # ----------------------------------------------------------- connections
    def save_connection(self, migration_id: int, role: str, auth_type: str,
                        site_url: str, secret: dict, cloud_id: str | None = None,
                        account_email: str | None = None) -> int:
        return self._exec(
            "INSERT INTO connections(migration_id,role,auth_type,site_url,"
            "cloud_id,account_email,secret_enc) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(migration_id,role) DO UPDATE SET auth_type=excluded.auth_type,"
            "site_url=excluded.site_url,cloud_id=excluded.cloud_id,"
            "account_email=excluded.account_email,secret_enc=excluded.secret_enc,"
            "status='new',verified_at=NULL",
            (migration_id, role, auth_type, site_url, cloud_id, account_email,
             self.encrypt(secret))).lastrowid

    def get_connection(self, migration_id: int, role: str) -> dict | None:
        return self._row("SELECT * FROM connections WHERE migration_id=? AND role=?",
                         (migration_id, role))

    def connection_secret(self, conn_row: dict) -> dict:
        return self.decrypt(conn_row["secret_enc"])

    def update_connection_secret(self, conn_id: int, secret: dict) -> None:
        self._exec("UPDATE connections SET secret_enc=? WHERE id=?",
                   (self.encrypt(secret), conn_id))

    def mark_connection_verified(self, conn_id: int, account_email: str) -> None:
        self._exec("UPDATE connections SET status='verified',verified_at=?,"
                   "account_email=? WHERE id=?",
                   (time.time(), account_email, conn_id))

    # ----------------------------------------------------------------- runs
    def create_run(self, migration_id: int, params: dict) -> int:
        return self._exec(
            "INSERT INTO runs(migration_id,started_at,params_json) VALUES(?,?,?)",
            (migration_id, time.time(), json.dumps(params))).lastrowid

    def update_run(self, run_id: int, *, status=None, phase=None, verdict=None,
                   stats: dict | None = None, finished: bool = False) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if phase is not None:
            sets.append("phase=?"); args.append(phase)
        if verdict is not None:
            sets.append("verdict=?"); args.append(verdict)
        if stats is not None:
            sets.append("stats_json=?"); args.append(json.dumps(stats, default=str))
        if finished or status in ("done", "failed", "cancelled"):
            sets.append("finished_at=?"); args.append(time.time())
        if sets:
            args.append(run_id)
            self._exec(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)

    def get_run(self, run_id: int) -> dict | None:
        return self._row("SELECT * FROM runs WHERE id=?", (run_id,))

    def list_runs(self, migration_id: int) -> list[dict]:
        return self._rows("SELECT * FROM runs WHERE migration_id=? ORDER BY id DESC",
                          (migration_id,))

    def active_run(self, migration_id: int) -> dict | None:
        return self._row("SELECT * FROM runs WHERE migration_id=? AND "
                         "status='running' ORDER BY id DESC LIMIT 1",
                         (migration_id,))

    def stale_running(self) -> list[dict]:
        return self._rows("SELECT * FROM runs WHERE status='running'")

    # --------------------------------------------------------- run projects
    def set_run_projects(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            for r in rows:
                self.db.execute(
                    "INSERT INTO run_projects(run_id,key,name,src_count,tgt_count,"
                    "missing,tail_count,fidelity_pct,blind_spot,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,key) DO UPDATE SET "
                    "name=excluded.name,src_count=excluded.src_count,"
                    "tgt_count=excluded.tgt_count,missing=excluded.missing,"
                    "tail_count=excluded.tail_count,fidelity_pct=excluded.fidelity_pct,"
                    "blind_spot=excluded.blind_spot,status=excluded.status",
                    (run_id, r["key"], r.get("name"), r.get("src_count"),
                     r.get("tgt_count"), r.get("missing"), r.get("tail_count"),
                     r.get("fidelity_pct"), int(r.get("blind_spot") or 0),
                     r.get("status")))
            self.db.commit()

    def get_run_projects(self, run_id: int) -> list[dict]:
        return self._rows("SELECT * FROM run_projects WHERE run_id=? ORDER BY key",
                          (run_id,))

    # ------------------------------------------------------------- findings
    def insert_findings_issue(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO findings_issue(run_id,project,kind,src_key,tgt_key,"
                "field,summary,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                [(run_id, r["project"], r["kind"], r.get("src_key"),
                  r.get("tgt_key"), r.get("field"), r.get("summary"),
                  json.dumps(r.get("detail") or {}, default=str)) for r in rows])
            self.db.commit()

    def query_issues(self, run_id: int, project=None, kind=None, q=None,
                     page: int = 1, size: int = 50) -> tuple[list[dict], int]:
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        if kind:
            where.append("kind=?"); args.append(kind)
        if q:
            where.append("(summary LIKE ? OR src_key LIKE ? OR tgt_key LIKE ? "
                         "OR field LIKE ?)")
            like = f"%{q}%"
            args += [like, like, like, like]
        w = " AND ".join(where)
        total = self.db.execute(
            f"SELECT COUNT(*) c FROM findings_issue WHERE {w}", args).fetchone()["c"]
        rows = self._rows(
            f"SELECT * FROM findings_issue WHERE {w} ORDER BY id "
            f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
        return rows, total

    def issue_kind_counts(self, run_id: int, project=None) -> dict:
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        rows = self._rows(
            f"SELECT kind, COUNT(*) c FROM findings_issue WHERE "
            f"{' AND '.join(where)} GROUP BY kind", args)
        return {r["kind"]: r["c"] for r in rows}

    def insert_findings_config(self, run_id: int, rows: list[dict]) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT INTO findings_config(run_id,area,name,kind,detail_json) "
                "VALUES(?,?,?,?,?)",
                [(run_id, r["area"], r.get("name"), r["kind"],
                  json.dumps(r.get("detail") or {}, default=str)) for r in rows])
            self.db.commit()

    def config_areas(self, run_id: int) -> list[str]:
        return [r["area"] for r in self._rows(
            "SELECT DISTINCT area FROM findings_config WHERE run_id=? ORDER BY area",
            (run_id,))]

    def query_config(self, run_id: int, area: str) -> list[dict]:
        return self._rows("SELECT * FROM findings_config WHERE run_id=? AND area=? "
                          "ORDER BY id", (run_id, area))

    # --------------------------------------------------------------- events
    def add_event(self, run_id: int, phase: str, level: str, message: str) -> None:
        self._exec("INSERT INTO events(run_id,ts,phase,level,message) "
                   "VALUES(?,?,?,?,?)", (run_id, time.time(), phase, level, message))

    def get_events(self, run_id: int, after_id: int = 0) -> list[dict]:
        return self._rows("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
                          (run_id, after_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_store.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/store.py tests/test_store.py
git commit -m "feat: SQLite store with Fernet-encrypted secrets, findings queries, event log"
```

---

## Post-review amendments (applied)

RLock guards every statement (reads and writes; unguarded reads on a shared sqlite3 connection crash under a concurrent writer — reproduced); PRAGMA foreign_keys=ON; key_path dir created; 2 new tests (threaded stress + FK enforcement).

