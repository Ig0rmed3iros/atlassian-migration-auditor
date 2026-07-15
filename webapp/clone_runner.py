"""Background runner + saved-connection client builder for user-access cloning.

Wraps auditor.cloneaccess.run_clone in a daemon thread, persisting progress to
clone_runs.log_json and the final report to report_json. A synchronous
run_preview is exposed for the read-only groups-only preview path.
"""
from __future__ import annotations

import threading

from auditor.client import Connection
from auditor.cloneaccess import run_clone, CloneError
from auditor.connectors import get_connector


def build_clone_client(store, conn_id: int, http):
    """Build a Jira client from a saved connection. Raises ValueError if the
    connection is missing or not a jira connection."""
    row = store.get_saved_connection(conn_id)
    if row is None or row["product"] != "jira":
        raise ValueError(f"no jira saved connection with id {conn_id}")
    secret = store.saved_connection_secret(row)
    conn = Connection(auth_type="pat", site_url=row["site_url"],
                      deployment=row["deployment"] or "cloud",
                      email=secret.get("email") or None,
                      api_token=secret.get("token"))
    connector = get_connector("jira")
    return connector.make_client(conn, http), connector, row


def run_preview(store, conn_id: int, pairs: list, http) -> dict:
    """Synchronous, read-only, groups-only preview (no writes, no role scan)."""
    client, _, _ = build_clone_client(store, conn_id, http)
    return run_clone(client, pairs, dry_run=True, scan_roles=False)


class CloneRunner:
    def __init__(self, store, http_getter):
        self.store = store
        self._http_getter = http_getter

    def start(self, conn_id: int, pairs: list, *, dry_run: bool,
              scan_roles: bool) -> int:
        run_id = self.store.create_clone_run(
            conn_id, {"pairs": [list(p) for p in pairs],
                      "dry_run": dry_run, "scan_roles": scan_roles})
        t = threading.Thread(target=self._execute,
                             args=(run_id, conn_id, pairs, dry_run, scan_roles),
                             daemon=True, name=f"clone-{run_id}")
        t.start()
        return run_id

    def _execute(self, run_id, conn_id, pairs, dry_run, scan_roles):
        store = self.store
        try:
            try:
                client, _, _ = build_clone_client(store, conn_id, self._http_getter())
            except ValueError as e:
                store.append_clone_log(run_id, f"error: {e}")
                store.update_clone_run(run_id, status="failed", finished=True)
                return

            def progress(msg):
                store.append_clone_log(run_id, msg)

            store.update_clone_run(run_id, phase="groups")
            try:
                report = run_clone(client, pairs, dry_run=dry_run,
                                   scan_roles=scan_roles, progress=progress)
            except CloneError as e:
                partial = getattr(e, "partial", None)
                store.append_clone_log(run_id, f"aborted: {e}")
                store.update_clone_run(run_id, status="failed", report=partial,
                                       finished=True)
                return
            store.append_clone_log(run_id, "done")
            store.update_clone_run(run_id, status="done", phase="finalize",
                                   report=report, finished=True)
        except Exception as e:  # noqa: BLE001 — background worker must never leave a run stuck 'running'
            store.append_clone_log(run_id, f"unexpected error: {e}")
            store.update_clone_run(run_id, status="failed", finished=True)
