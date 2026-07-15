"""SQLite persistence + Fernet-encrypted secrets.

Single-file DB under MA_DATA_DIR. All methods synchronous; the connection is
created with check_same_thread=False because the run engine thread and the
request threads share the Store. One shared connection guarded by an RLock on
every statement (reads AND writes): check_same_thread=False does not make
sqlite3 connections thread-safe, and unguarded reads crash under a concurrent
writer.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from cryptography.fernet import Fernet

log = logging.getLogger("migration_auditor.store")

from auditor.connectors import known_products

# Logical schema revision stamped into PRAGMA user_version by _migrate. Bump it
# when the schema changes so a health probe / future migration can gate on it.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  product TEXT NOT NULL DEFAULT 'jira',
  audit_type TEXT NOT NULL DEFAULT 'migration',
  created_at REAL NOT NULL);
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
  stats_json TEXT NOT NULL DEFAULT '{}',
  kind TEXT NOT NULL DEFAULT 'audit' CHECK(kind IN ('audit','fix','env_audit','env_fix')),
  source_run_id INTEGER REFERENCES runs(id));
CREATE INDEX IF NOT EXISTS ix_runs_mig ON runs (migration_id);
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
  detail_json TEXT NOT NULL DEFAULT '{}',
  fix_payload TEXT);
CREATE INDEX IF NOT EXISTS ix_fi ON findings_issue (run_id, project, kind);
CREATE TABLE IF NOT EXISTS findings_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  area TEXT NOT NULL, name TEXT, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  fix_payload TEXT);
CREATE INDEX IF NOT EXISTS ix_fc ON findings_config (run_id, area);
CREATE TABLE IF NOT EXISTS fix_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  finding_ref TEXT, fix_id TEXT NOT NULL, object_name TEXT,
  method TEXT, path TEXT, status INTEGER, ok INTEGER DEFAULT 0,
  created_id TEXT, error TEXT, snapshot_json TEXT);
CREATE INDEX IF NOT EXISTS ix_fa ON fix_actions (run_id);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts REAL NOT NULL, phase TEXT, level TEXT DEFAULT 'info', message TEXT);
CREATE INDEX IF NOT EXISTS ix_ev ON events (run_id, id);
CREATE TABLE IF NOT EXISTS finding_solutions (
  run_id INTEGER NOT NULL REFERENCES runs(id),
  finding_sig TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY (run_id, finding_sig));
CREATE TABLE IF NOT EXISTS saved_connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  product TEXT NOT NULL,
  deployment TEXT NOT NULL,
  site_url TEXT NOT NULL,
  account_email TEXT,
  secret_enc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unverified',
  created_at REAL NOT NULL);
"""


def _warn_if_not_owner_only(path: str, what: str) -> None:
    """After a best-effort chmod, verify the path is ACTUALLY owner-only. A
    filesystem that ignores chmod (WSL drvfs, some network mounts) leaves the
    secret group/world-readable while silently succeeding, so the only way to
    know hardening failed is to re-stat. Warn loudly so the operator isn't
    falsely assured the secrets are protected at rest. When MA_STRICT_PERMS is
    set, RAISE instead (fail-closed) so a deployment can refuse to run with
    unprotected secrets rather than rely on a log line nobody reads."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & 0o077:
        msg = (f"{what} at {path} is not owner-only (mode {mode & 0o777:03o}): "
               f"this filesystem ignored chmod (e.g. WSL drvfs or a network "
               f"mount), so the Fernet-encrypted secrets are readable by other "
               f"local users. Use a POSIX filesystem for the data dir, or supply "
               f"the key out-of-band via MA_SECRET_KEY.")
        if os.environ.get("MA_STRICT_PERMS"):
            raise PermissionError(msg)
        log.warning("%s", msg)


def _harden_perms(db_path: str) -> None:
    """Best-effort: 0600 the DB file and 0700 its containing data dir so the
    Fernet-encrypted secrets at rest are owner-only. A filesystem that ignores
    chmod (WSL drvfs, some network mounts) raises OSError OR silently no-ops;
    swallow the error so a local-first install still starts, then verify BOTH
    the file and the directory and warn (or fail-closed) if either stayed
    readable — the directory holds the key + DB and was the easy thing to miss."""
    data_dir = os.path.dirname(db_path) or "."
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    _warn_if_not_owner_only(data_dir, "data directory")
    # The DB file AND its WAL sidecars (-wal/-shm), which hold recently-written
    # pages — encrypted secrets + clear findings — must all be owner-only. WAL
    # widened the at-rest surface from one file to three; harden all of them so
    # the MA_STRICT_PERMS fail-closed check can't be slipped by a 0644 sidecar.
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if suffix and not os.path.exists(p):
            continue
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        _warn_if_not_owner_only(
            p, "secrets database" if not suffix else f"WAL sidecar {suffix}")


class Store:
    def __init__(self, db_path: str, key_path: str, secret_key: str | None = None):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._txn_depth = 0          # reentrancy guard for _txn (lock-protected)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # WAL: readers no longer block behind the in-flight writer (the rollback
        # journal serialized every read across all migrations). Best-effort —
        # a filesystem that can't do WAL (some network mounts) keeps the prior
        # mode rather than erroring; busy_timeout already set below.
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            pass
        self.db.executescript(_SCHEMA)
        self._migrate()
        # The DB holds the Fernet-encrypted secrets (OAuth client secret, PAT
        # tokens, AI keys); lock it down to the owner like the .key file. The
        # data dir is tightened too so a sibling can't list/replace files. Both
        # are best-effort: a no-op filesystem (e.g. WSL drvfs) ignores chmod and
        # raises/no-ops — never let that crash startup.
        _harden_perms(db_path)
        if secret_key:
            key = secret_key.encode()        # in-memory key: no file to harden
        elif os.path.exists(key_path):
            key = open(key_path, "rb").read()
            _warn_if_not_owner_only(key_path, "Fernet key file")
        else:
            key = Fernet.generate_key()
            # Create the master key ATOMICALLY owner-only (O_EXCL + 0600) so the
            # bytes are never momentarily world-readable between write and chmod
            # (TOCTOU). umask can only REMOVE bits, so 0600 stays 0600; the chmod
            # below is belt-and-braces. If we lose a create race, read the winner.
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = open(key_path, "rb").read()
            else:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
                try:
                    os.chmod(key_path, 0o600)
                except OSError:
                    pass
            _warn_if_not_owner_only(key_path, "Fernet key file")
        self._fernet = Fernet(key)

    def _migrate(self) -> None:
        """Idempotent in-place upgrades for pre-existing DB files. SQLite ALTER
        TABLE ADD COLUMN with a constant default backfills old rows atomically."""
        def cols(t):
            return {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}
        if "product" not in cols("migrations"):
            self.db.execute("ALTER TABLE migrations ADD COLUMN product TEXT "
                            "NOT NULL DEFAULT 'jira'")
        if "audit_type" not in cols("migrations"):
            self.db.execute("ALTER TABLE migrations ADD COLUMN audit_type TEXT "
                            "NOT NULL DEFAULT 'migration'")
        if "deployment" not in cols("connections"):
            self.db.execute("ALTER TABLE connections ADD COLUMN deployment TEXT "
                            "NOT NULL DEFAULT 'cloud'")
        if "kind" not in cols("runs"):
            # SQLite silently drops a CHECK on ALTER TABLE ADD COLUMN, so an
            # upgraded DB's runs.kind has no CHECK(kind IN
            # ('audit','fix','env_audit')) — the create_run() guard enforces the
            # domain instead.
            self.db.execute("ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL "
                            "DEFAULT 'audit'")
        if "source_run_id" not in cols("runs"):
            self.db.execute("ALTER TABLE runs ADD COLUMN source_run_id INTEGER")
        for t in ("findings_config", "findings_issue"):
            if "fix_payload" not in cols(t):
                self.db.execute(f"ALTER TABLE {t} ADD COLUMN fix_payload TEXT")
        if "snapshot_json" not in cols("fix_actions"):
            # L3 pre-delete snapshot: the live object JSON captured before a
            # destructive op, stored LOCALLY for the operator's audit/restore
            # trail (never transmitted externally).
            self.db.execute("ALTER TABLE fix_actions ADD COLUMN snapshot_json TEXT")
        # Connection Vault (spec 2026-06-13): the saved_connections table is in
        # _SCHEMA so fresh DBs get it from executescript; this guarded CREATE
        # backfills any DB file that predates it (executescript ran before the
        # table existed in _SCHEMA), keeping the upgrade path idempotent.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS saved_connections ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
            "product TEXT NOT NULL, deployment TEXT NOT NULL, "
            "site_url TEXT NOT NULL, account_email TEXT, secret_enc TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'unverified', created_at REAL NOT NULL)")
        # Clone-access runs (spec 2026-06-24): persists background clone
        # execution state, progress log and the final report.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS clone_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, conn_id INTEGER, "
            "status TEXT DEFAULT 'running', phase TEXT, params_json TEXT, "
            "report_json TEXT, log_json TEXT DEFAULT '[]', "
            "created_at REAL, finished_at REAL)")
        # Refuse to open a DB written by a NEWER build (its user_version exceeds
        # what this build understands) — silently re-stamping it down to our
        # version could corrupt data a newer schema relies on. Fail loud.
        cur = self.db.execute("PRAGMA user_version").fetchone()
        existing = int(cur[0]) if cur else 0
        if existing > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {existing} is newer than this build "
                f"supports ({SCHEMA_VERSION}); upgrade the app or restore a "
                "compatible backup")
        # Stamp the logical schema revision so an operator / health probe can
        # see what shape the DB is, and a future _migrate can gate on it.
        # (PRAGMA can't be parameterized; SCHEMA_VERSION is an int literal.)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    # ------------------------------------------------------------- secrets
    def encrypt(self, data: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def decrypt(self, blob: bytes) -> dict:
        return json.loads(self._fernet.decrypt(bytes(blob)))

    # -------------------------------------------------------------- helpers
    @contextlib.contextmanager
    def _txn(self):
        """Lock + atomic transaction: COMMIT on success, ROLLBACK on ANY
        exception, so a write that fails partway (e.g. a bad row mid-executemany
        or a later statement in a multi-step write) can never leave pending rows
        that the next unrelated commit() would silently flush. Reentrant: nested
        _txn calls join the outer transaction and only the outermost commits."""
        with self._lock:
            self._txn_depth += 1
            outer = self._txn_depth == 1
            try:
                yield self.db
            except BaseException:
                if outer:
                    self.db.rollback()
                raise
            else:
                if outer:
                    # commit() can itself raise (disk full, SQLITE_BUSY, I/O):
                    # roll back so the failed rows can't leak into the next
                    # unrelated transaction on this shared connection.
                    try:
                        self.db.commit()
                    except BaseException:
                        self.db.rollback()
                        raise
            finally:
                self._txn_depth -= 1

    def _exec(self, sql, args=()):
        with self._txn() as db:
            return db.execute(sql, args)

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _row(self, sql, args=()):
        with self._lock:
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None

    # --------------------------------------------------------- maintenance
    # The DB file only grows: deleted runs leave free pages, and the events log
    # accrues forever. These give an operator/scheduler a way to bound it.
    def prune_events(self, keep_per_run: int = 2000) -> int:
        """Keep only the most recent `keep_per_run` events per run; delete older
        ones so a long/looping run can't grow the events table unbounded.
        Returns the number deleted."""
        with self._txn() as db:
            cur = db.execute(
                "DELETE FROM events WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER "
                "      (PARTITION BY run_id ORDER BY id DESC) AS rn FROM events"
                "  ) WHERE rn > ?)", (keep_per_run,))
            return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim free pages left by deleted runs/findings. Standalone (VACUUM
        cannot run inside a transaction); cheap on a small DB so it is safe to
        call opportunistically from a maintenance task."""
        with self._lock:
            self.db.execute("VACUUM")

    def db_ping(self, timeout: float = 5.0) -> bool:
        """Liveness probe: True if the DB answers a trivial query. Acquires the
        connection lock with a TIMEOUT so a probe never HANGS behind a long
        write / VACUUM / backup — it raises TimeoutError, which /healthz turns
        into a 503 (a hung probe defeats supervision). Also raises on a
        genuinely unusable connection."""
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError("db lock not acquired within probe timeout")
        try:
            self.db.execute("SELECT 1").fetchone()
        finally:
            self._lock.release()
        return True

    def schema_version(self) -> int:
        """The DB's PRAGMA user_version — the logical schema revision _migrate
        last stamped. 0 on a pre-versioning DB."""
        with self._lock:
            row = self.db.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def backup(self, dest_path: str) -> str:
        """Write a consistent snapshot of the live DB to `dest_path` via
        `VACUUM INTO` (a single statement that also defragments). Holds the
        store lock for the copy, so it is consistent but NOT concurrent — on a
        large DB it briefly freezes the app. REFUSES to overwrite an existing
        destination (an explicit check — VACUUM INTO silently overwrites a
        0-byte file, so a backup must never clobber a prior one). The snapshot
        file is chmod'd owner-only (just the file — never its parent dir, which
        may be an operator-chosen shared path). Returns the destination."""
        if os.path.exists(dest_path):
            raise FileExistsError(
                f"backup destination already exists, refusing to overwrite: "
                f"{dest_path}")
        with self._lock:
            self.db.execute("VACUUM INTO ?", (dest_path,))
        try:                                  # best-effort; never fail a backup
            os.chmod(dest_path, 0o600)
        except OSError:
            pass
        return dest_path

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
    def create_migration(self, name: str, product: str = "jira",
                         audit_type: str = "migration") -> int:
        # Validated against the connector REGISTRY, not a name whitelist: a
        # product is only creatable once a connector can actually audit it,
        # otherwise the row would 500 on every follow-up step.
        if product not in known_products():
            raise ValueError(f"unknown product {product!r}")
        if audit_type not in ("migration", "environment"):
            raise ValueError(f"unknown audit_type {audit_type!r}")
        return self._exec(
            "INSERT INTO migrations(name,product,audit_type,created_at) "
            "VALUES(?,?,?,?)",
            (name, product, audit_type, time.time())).lastrowid

    def list_migrations(self) -> list[dict]:
        return self._rows("SELECT * FROM migrations ORDER BY id DESC")

    def get_migration(self, mid: int) -> dict | None:
        return self._row("SELECT * FROM migrations WHERE id=?", (mid,))

    # ----------------------------------------------------------- connections
    def save_connection(self, migration_id: int, role: str, auth_type: str,
                        site_url: str, secret: dict, cloud_id: str | None = None,
                        account_email: str | None = None,
                        deployment: str = "cloud") -> int:
        if deployment not in ("cloud", "dc"):
            raise ValueError(f"unknown deployment {deployment!r}")
        return self._exec(
            "INSERT INTO connections(migration_id,role,auth_type,site_url,"
            "deployment,cloud_id,account_email,secret_enc) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(migration_id,role) DO UPDATE SET auth_type=excluded.auth_type,"
            "site_url=excluded.site_url,deployment=excluded.deployment,"
            "cloud_id=excluded.cloud_id,"
            "account_email=excluded.account_email,secret_enc=excluded.secret_enc,"
            "status='new',verified_at=NULL",
            (migration_id, role, auth_type, site_url, deployment, cloud_id,
             account_email, self.encrypt(secret))).lastrowid

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

    # ----------------------------------------------------- saved connections
    # The Connection Vault: reusable PAT credentials stored once, encrypted,
    # then COPIED into a migration's role connection on use (copy semantics,
    # not a foreign key). Secrets live in secret_enc as Fernet({"token","email"})
    # and are only ever returned via saved_connection_secret for a live verify
    # or a copy — never rendered back to the browser.
    def list_saved_connections(self, product: str | None = None) -> list[dict]:
        if product is not None:
            return self._rows("SELECT * FROM saved_connections WHERE product=? "
                              "ORDER BY id DESC", (product,))
        return self._rows("SELECT * FROM saved_connections ORDER BY id DESC")

    def get_saved_connection(self, conn_id: int) -> dict | None:
        return self._row("SELECT * FROM saved_connections WHERE id=?", (conn_id,))

    def create_saved_connection(self, name: str, product: str, deployment: str,
                                site_url: str, email: str | None,
                                token: str) -> int:
        if product not in ("jira", "confluence"):
            raise ValueError(f"unknown product {product!r}")
        if deployment not in ("cloud", "dc"):
            raise ValueError(f"unknown deployment {deployment!r}")
        secret = {"token": token, "email": email}
        return self._exec(
            "INSERT INTO saved_connections(name,product,deployment,site_url,"
            "secret_enc,status,created_at) VALUES(?,?,?,?,?,'unverified',?)",
            (name, product, deployment, site_url,
             self.encrypt(secret).decode(), time.time())).lastrowid

    def mark_saved_connection_verified(self, conn_id: int,
                                       account_email: str | None) -> None:
        self._exec("UPDATE saved_connections SET status='verified',"
                   "account_email=? WHERE id=?", (account_email, conn_id))

    def delete_saved_connection(self, conn_id: int) -> None:
        self._exec("DELETE FROM saved_connections WHERE id=?", (conn_id,))

    def saved_connection_secret(self, conn_row: dict) -> dict:
        return self.decrypt(conn_row["secret_enc"].encode())

    # ---------------------------------------------------------- clone runs
    def create_clone_run(self, conn_id: int, params: dict) -> int:
        return self._exec(
            "INSERT INTO clone_runs(conn_id,status,params_json,log_json,created_at)"
            " VALUES(?,?,?,?,?)",
            (conn_id, "running", json.dumps(params), "[]", time.time())).lastrowid

    def get_clone_run(self, run_id: int) -> dict | None:
        return self._row("SELECT * FROM clone_runs WHERE id=?", (run_id,))

    def list_clone_runs(self, limit: int = 50) -> list:
        return self._rows("SELECT * FROM clone_runs ORDER BY id DESC LIMIT ?",
                          (limit,))

    def update_clone_run(self, run_id: int, *, status=None, phase=None,
                         report: dict | None = None, finished: bool = False) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?"); args.append(status)
        if phase is not None:
            sets.append("phase=?"); args.append(phase)
        if report is not None:
            sets.append("report_json=?"); args.append(json.dumps(report, default=str))
        if finished or status in ("done", "failed"):
            sets.append("finished_at=?"); args.append(time.time())
        if sets:
            args.append(run_id)
            self._exec(f"UPDATE clone_runs SET {','.join(sets)} WHERE id=?", args)

    def append_clone_log(self, run_id: int, line: str) -> None:
        with self._txn() as db:
            row = db.execute("SELECT log_json FROM clone_runs WHERE id=?",
                             (run_id,)).fetchone()
            log = json.loads(row["log_json"]) if row and row["log_json"] else []
            log.append(line)
            db.execute("UPDATE clone_runs SET log_json=? WHERE id=?",
                       (json.dumps(log), run_id))

    # ----------------------------------------------------------------- runs
    def create_run(self, migration_id: int, params: dict, kind: str = "audit",
                   source_run_id: int | None = None) -> int:
        if kind not in ("audit", "fix", "env_audit", "env_fix"):
            raise ValueError(f"unknown run kind {kind!r}")
        return self._exec(
            "INSERT INTO runs(migration_id,started_at,params_json,kind,source_run_id)"
            " VALUES(?,?,?,?,?)",
            (migration_id, time.time(), json.dumps(params), kind,
             source_run_id)).lastrowid

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

    # ----------------------------------------------------------- delete runs
    # The six run-scoped child tables that a run owns. Centralised so
    # delete_run and delete_migration share one cascade list (drift between
    # the two would orphan rows).
    _RUN_CHILDREN = ("run_projects", "findings_issue", "findings_config",
                     "fix_actions", "events", "finding_solutions")

    def _delete_run_children(self, run_id: int) -> None:
        """Delete every run-scoped child row for one run. Caller holds _lock
        and commits. Does NOT delete the runs row itself."""
        for t in self._RUN_CHILDREN:
            self.db.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))

    def delete_run(self, run_id: int) -> None:
        """Delete one run and all of its run-scoped child rows.

        A fix/re-audit run's source_run_id may point HERE. With foreign_keys=ON
        that reference would make the DELETE raise, so NULL it first (the fix run
        survives with a nulled source). Atomic: the NULL + child + run deletes
        all roll back together on any failure."""
        with self._txn() as db:
            db.execute("UPDATE runs SET source_run_id=NULL WHERE source_run_id=?",
                       (run_id,))
            self._delete_run_children(run_id)
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def delete_migration(self, mid: int) -> None:
        """Delete a migration and everything beneath it: every run (+ its child
        rows) and the migration's connections. The Connection Vault
        (saved_connections) is independent and is never touched."""
        with self._txn() as db:
            run_ids = [r["id"] for r in db.execute(
                "SELECT id FROM runs WHERE migration_id=?", (mid,)).fetchall()]
            # NULL any source_run_id (even from runs in OTHER migrations) that
            # points at a run we are about to delete, so the self-FK never trips.
            db.execute("UPDATE runs SET source_run_id=NULL WHERE source_run_id IN "
                       "(SELECT id FROM runs WHERE migration_id=?)", (mid,))
            for rid in run_ids:
                self._delete_run_children(rid)
            db.execute("DELETE FROM runs WHERE migration_id=?", (mid,))
            db.execute("DELETE FROM connections WHERE migration_id=?", (mid,))
            db.execute("DELETE FROM migrations WHERE id=?", (mid,))

    # --------------------------------------------------------- run projects
    def set_run_projects(self, run_id: int, rows: list[dict]) -> None:
        with self._txn() as db:
            for r in rows:
                db.execute(
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

    def get_run_projects(self, run_id: int) -> list[dict]:
        return self._rows("SELECT * FROM run_projects WHERE run_id=? ORDER BY key",
                          (run_id,))

    # ---------------------------------------------------------- fix actions
    _FIX_ACTION_SQL = (
        "INSERT INTO fix_actions(run_id,finding_ref,fix_id,object_name,"
        "method,path,status,ok,created_id,error,snapshot_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)")

    @staticmethod
    def _fix_action_tuple(run_id: int, r: dict) -> tuple:
        # fix_id is NOT NULL: coerce a missing/empty id to a safe fallback so a
        # stray record can never crash the persist AFTER the live writes already
        # happened (which would lose the fix log + snapshots). Callers set a
        # meaningful fix_id; this is the net.
        return (run_id, r.get("finding_ref"), r.get("fix_id") or "env_fix",
                r.get("object_name"), r.get("method"), r.get("path"),
                r.get("status"), int(bool(r.get("ok"))), r.get("created_id"),
                r.get("error"), r.get("snapshot_json"))

    def insert_fix_actions(self, run_id: int, rows: list[dict]) -> None:
        with self._txn() as db:
            db.executemany(
                self._FIX_ACTION_SQL,
                [self._fix_action_tuple(run_id, r) for r in rows])

    def append_fix_action(self, run_id: int, row: dict) -> None:
        """Persist ONE fix-action row and commit immediately — the write-through
        path the live-apply loop uses so a DELETE's record survives a hard crash
        that happens right after the DELETE fired (review Bug 4). Thread-safe so
        the parallel apply workers can stream concurrently."""
        with self._txn() as db:
            db.execute(self._FIX_ACTION_SQL,
                       self._fix_action_tuple(run_id, row))

    def get_fix_actions(self, run_id: int) -> list[dict]:
        return self._rows("SELECT * FROM fix_actions WHERE run_id=? ORDER BY id",
                          (run_id,))

    # ------------------------------------------------------------- findings
    def insert_findings_issue(self, run_id: int, rows: list[dict]) -> None:
        with self._txn() as db:
            db.executemany(
                "INSERT INTO findings_issue(run_id,project,kind,src_key,tgt_key,"
                "field,summary,detail_json,fix_payload) VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, r["project"], r["kind"], r.get("src_key"),
                  r.get("tgt_key"), r.get("field"), r.get("summary"),
                  json.dumps(r.get("detail") or {}, default=str),
                  json.dumps(r["fix_payload"], default=str)
                  if r.get("fix_payload") is not None else None) for r in rows])

    @staticmethod
    def _decode_fix_payload(rows: list[dict]) -> list[dict]:
        """Deserialize the fix_payload JSON column in place so every finding
        read path returns it as a dict (or None), never a raw string — callers
        must not have to json.loads it themselves."""
        for r in rows:
            r["fix_payload"] = (json.loads(r["fix_payload"])
                                if r.get("fix_payload") else None)
        return rows

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
        with self._lock:
            total = self.db.execute(
                f"SELECT COUNT(*) c FROM findings_issue WHERE {w}", args).fetchone()["c"]
            rows = self._rows(
                f"SELECT * FROM findings_issue WHERE {w} ORDER BY id "
                f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
        return self._decode_fix_payload(rows), total

    def all_issue_findings(self, run_id: int, project=None) -> list[dict]:
        """Every per-issue finding for a run in one statement (no pagination).

        Feeds the pure fidelity derivation, which must see the whole set. The
        UI's paged views still use query_issues; this is the bulk read path so
        the derivation does not page through tens of thousands of rows with
        O(n^2) OFFSET scans."""
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        w = " AND ".join(where)
        return self._decode_fix_payload(self._rows(
            f"SELECT * FROM findings_issue WHERE {w} ORDER BY id", args))

    def issue_finding_counts(self, run_id: int) -> dict:
        """Aggregate counts for derive_fidelity_from_counts — never loads full rows.

        Returns a dict with four keys:
          holes       int   — count of missing_in_tgt findings
          src_tails   int   — count of source-direction tail_post_cutover findings
          field_agg   list  — per-(project, kind, field) dicts with
                              {project, kind, field, affected_issues, empty_issues}
                              for field-like kinds (field_mismatch, link_mismatch)
          core_by_project  dict  — {project: core_mismatched_count} where
                              core_mismatched counts distinct issues with at
                              least one NON-systematic mismatch finding.
                              NOTE: requires systematic_fields already computed
                              from field_agg; computed internally via a second
                              SQL pass after the caller-supplied threshold logic
                              cannot run here. See derive_fidelity_from_counts.

        This is the performance-critical path: it issues a small number of
        aggregate SQL queries instead of loading all N findings into Python.
        """
        # ── holes ──────────────────────────────────────────────────────────
        holes = self.db.execute(
            "SELECT COUNT(*) c FROM findings_issue "
            "WHERE run_id=? AND kind='missing_in_tgt'", (run_id,)
        ).fetchone()["c"]

        # ── source-direction tails ──────────────────────────────────────────
        src_tails = self.db.execute(
            "SELECT COUNT(*) c FROM findings_issue "
            "WHERE run_id=? AND kind='tail_post_cutover' "
            "AND (json_extract(detail_json,'$.direction')='source' "
            "     OR (json_extract(detail_json,'$.direction') IS NULL "
            "         AND src_key IS NOT NULL))",
            (run_id,)
        ).fetchone()["c"]

        # ── field-like kinds: per-(project, kind, field) distinct issue counts
        # Counts affected_issues (distinct issue keys for this field) and
        # empty_issues (distinct issue keys where target value is empty).
        # _is_empty covers: null, empty/whitespace string, empty list/dict.
        # JSON_EXTRACT returns: NULL for null, the bare value for scalars,
        # JSON text for arrays/objects. Empty string -> '', empty array -> '[]',
        # empty object -> '{}'. TRIM handles whitespace-only strings.
        field_agg = self._rows(
            "SELECT project, kind, field, "
            "  COUNT(DISTINCT COALESCE(src_key, tgt_key)) AS affected_issues, "
            "  COUNT(DISTINCT CASE "
            "    WHEN json_extract(detail_json,'$.tgt') IS NULL "
            "      OR TRIM(CAST(json_extract(detail_json,'$.tgt') AS TEXT)) = '' "
            "      OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = '[]' "
            "      OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = '{}' "
            "      OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = 'null' "
            "    THEN COALESCE(src_key, tgt_key) END) AS empty_issues "
            "FROM findings_issue "
            "WHERE run_id=? "
            "  AND kind IN ('field_mismatch','link_mismatch') "
            "  AND field IS NOT NULL "
            "  AND COALESCE(src_key, tgt_key) IS NOT NULL "
            "GROUP BY project, kind, field",
            (run_id,))

        # ── per-(project, field, src_val) counts for empty-target findings
        # Used to compute top_pattern in derive_fidelity_from_counts — the most
        # common src value when the target is empty. Returns O(projects × fields
        # × distinct_src_values) rows — still far fewer than all findings.
        src_patterns = self._rows(
            "SELECT project, field, "
            "  CAST(json_extract(detail_json,'$.src') AS TEXT) AS src_val, "
            "  COUNT(DISTINCT COALESCE(src_key, tgt_key)) AS cnt "
            "FROM findings_issue "
            "WHERE run_id=? "
            "  AND kind IN ('field_mismatch','link_mismatch') "
            "  AND field IS NOT NULL "
            "  AND COALESCE(src_key, tgt_key) IS NOT NULL "
            "  AND (json_extract(detail_json,'$.tgt') IS NULL "
            "       OR TRIM(CAST(json_extract(detail_json,'$.tgt') AS TEXT)) = '' "
            "       OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = '[]' "
            "       OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = '{}' "
            "       OR CAST(json_extract(detail_json,'$.tgt') AS TEXT) = 'null') "
            "GROUP BY project, field, src_val",
            (run_id,))

        return {
            "holes": holes,
            "src_tails": src_tails,
            "field_agg": list(field_agg),
            "src_patterns": list(src_patterns),
        }

    def core_mismatch_counts(self, run_id: int,
                             systematic_by_project: dict[str, set]) -> dict:
        """COUNT(DISTINCT issue_key) per project for non-systematic mismatches.

        Called by derive_fidelity_from_counts AFTER systematic fields are
        identified from issue_finding_counts(). Excludes:
          - presence/coverage kinds (missing_in_tgt, tail_post_cutover, …)
          - field-like mismatches on systematic fields for each project

        systematic_by_project: {project_key: set_of_systematic_field_names}
        """
        _SKIP = ("missing_in_tgt", "tail_post_cutover", "missing_in_src",
                 "comment_uncheckable", "attachment_uncheckable")
        _FIELD_LIKE = ("field_mismatch", "link_mismatch")

        # Build the systematic exclusion clause: for each (project, field) pair
        # that is systematic, exclude field-like findings on that field in that
        # project. The NOT (...) clause filters them out so only non-systematic
        # mismatches remain.
        sys_clauses = []
        sys_args: list = []
        for proj, fields in systematic_by_project.items():
            for field in fields:
                sys_clauses.append(
                    "(project=? AND kind IN ('field_mismatch','link_mismatch') "
                    "AND field=?)")
                sys_args += [proj, field]

        base_where = (
            "run_id=? "
            f"AND kind NOT IN ({','.join('?' * len(_SKIP))}) "
            "AND COALESCE(src_key, tgt_key) IS NOT NULL"
        )
        args: list = [run_id] + list(_SKIP)

        if sys_clauses:
            excl = " OR ".join(sys_clauses)
            sql = (f"SELECT project, "
                   f"COUNT(DISTINCT COALESCE(src_key, tgt_key)) AS core_mismatched "
                   f"FROM findings_issue "
                   f"WHERE {base_where} AND NOT ({excl}) "
                   f"GROUP BY project")
            rows = self._rows(sql, args + sys_args)
        else:
            sql = (f"SELECT project, "
                   f"COUNT(DISTINCT COALESCE(src_key, tgt_key)) AS core_mismatched "
                   f"FROM findings_issue "
                   f"WHERE {base_where} "
                   f"GROUP BY project")
            rows = self._rows(sql, args)

        return {r["project"]: r["core_mismatched"] for r in rows}

    def issue_kind_counts(self, run_id: int, project=None) -> dict:
        where, args = ["run_id=?"], [run_id]
        if project:
            where.append("project=?"); args.append(project)
        rows = self._rows(
            f"SELECT kind, COUNT(*) c FROM findings_issue WHERE "
            f"{' AND '.join(where)} GROUP BY kind", args)
        return {r["kind"]: r["c"] for r in rows}

    def insert_findings_config(self, run_id: int, rows: list[dict]) -> None:
        with self._txn() as db:
            db.executemany(
                "INSERT INTO findings_config(run_id,area,name,kind,detail_json,"
                "fix_payload) VALUES(?,?,?,?,?,?)",
                [(run_id, r["area"], r.get("name"), r["kind"],
                  json.dumps(r.get("detail") or {}, default=str),
                  json.dumps(r["fix_payload"], default=str)
                  if r.get("fix_payload") is not None else None) for r in rows])

    def config_areas(self, run_id: int) -> list[str]:
        return [r["area"] for r in self._rows(
            "SELECT DISTINCT area FROM findings_config WHERE run_id=? ORDER BY area",
            (run_id,))]

    def query_config(self, run_id: int, area: str) -> list[dict]:
        rows = self._rows("SELECT * FROM findings_config WHERE run_id=? AND area=? "
                          "ORDER BY id", (run_id, area))
        for r in rows:
            r["detail"] = json.loads(r.get("detail_json") or "{}")
        return self._decode_fix_payload(rows)

    # --------------------------------------------------------------- events
    def add_event(self, run_id: int, phase: str, level: str, message: str) -> None:
        self._exec("INSERT INTO events(run_id,ts,phase,level,message) "
                   "VALUES(?,?,?,?,?)", (run_id, time.time(), phase, level, message))

    def get_events(self, run_id: int, after_id: int = 0) -> list[dict]:
        return self._rows("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
                          (run_id, after_id))

    # -------------------------------------------------------- solutions cache
    def save_solutions(self, run_id: int, sig: str, payload: dict) -> None:
        self._exec(
            "INSERT INTO finding_solutions(run_id,finding_sig,payload_json,created_at)"
            " VALUES(?,?,?,?) ON CONFLICT(run_id,finding_sig) DO UPDATE SET "
            "payload_json=excluded.payload_json,created_at=excluded.created_at",
            (run_id, sig, json.dumps(payload, default=str), time.time()))

    def get_solutions(self, run_id: int, sig: str) -> dict | None:
        r = self._row("SELECT payload_json,created_at FROM finding_solutions "
                      "WHERE run_id=? AND finding_sig=?", (run_id, sig))
        if not r:
            return None
        return {"payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"]}
