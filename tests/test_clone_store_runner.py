import time
from webapp.store import Store
from webapp import clone_runner as cr


def _store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def test_clone_run_crud_roundtrip(tmp_path):
    s = _store(tmp_path)
    cid = s.create_saved_connection("acme", "jira", "cloud",
                                    "https://acme.atlassian.net",
                                    email="e@x.y", token="tok")
    rid = s.create_clone_run(cid, {"pairs": [["a", "b"]], "dry_run": True})
    row = s.get_clone_run(rid)
    assert row["status"] == "running" and row["conn_id"] == cid
    s.append_clone_log(rid, "hello")
    s.append_clone_log(rid, "world")
    s.update_clone_run(rid, phase="groups")
    row = s.get_clone_run(rid)
    import json
    assert json.loads(row["log_json"]) == ["hello", "world"]
    assert row["phase"] == "groups"
    s.update_clone_run(rid, status="done",
                       report={"summary": {"pairs": 1}}, finished=True)
    row = s.get_clone_run(rid)
    assert row["status"] == "done" and row["finished_at"]
    assert json.loads(row["report_json"])["summary"]["pairs"] == 1
    assert any(r["id"] == rid for r in s.list_clone_runs())


def test_runner_executes_and_persists_report(tmp_path, monkeypatch):
    s = _store(tmp_path)
    cid = s.create_saved_connection("acme", "jira", "cloud",
                                    "https://acme.atlassian.net",
                                    email="e@x.y", token="tok")

    # Stub the engine so no real client is needed; assert the runner wires
    # progress -> log and persists the returned report + status=done.
    def fake_run_clone(client, pairs, *, dry_run, scan_roles, progress=None):
        if progress:
            progress("scanning")
        return {"dry_run": dry_run, "scanned_roles": scan_roles, "pairs": [],
                "summary": {"pairs": 0, "blocked": 0, "groups_added": 0,
                            "roles_added": 0, "failed": 0, "partial": 0}}
    monkeypatch.setattr(cr, "run_clone", fake_run_clone)
    monkeypatch.setattr(cr, "build_clone_client",
                        lambda store, conn_id, http: (object(), object(), {"id": conn_id}))

    runner = cr.CloneRunner(s, http_getter=lambda: None)
    rid = runner.start(cid, [("a", "b")], dry_run=True, scan_roles=False)
    # join the worker thread deterministically
    for _ in range(200):
        if s.get_clone_run(rid)["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    row = s.get_clone_run(rid)
    assert row["status"] == "done"
    import json
    assert "scanning" in json.loads(row["log_json"])
    assert json.loads(row["report_json"])["summary"]["pairs"] == 0


def test_runner_unexpected_error_marks_failed_not_stuck(tmp_path, monkeypatch):
    s = _store(tmp_path)
    cid = s.create_saved_connection("acme", "jira", "cloud",
                                    "https://acme.atlassian.net",
                                    email="e@x.y", token="tok")

    # Stub build_clone_client to succeed, but run_clone raises an unexpected error.
    monkeypatch.setattr(cr, "build_clone_client",
                        lambda store, conn_id, http: (object(), object(), {"id": conn_id}))
    monkeypatch.setattr(cr, "run_clone",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    runner = cr.CloneRunner(s, http_getter=lambda: None)
    rid = runner.start(cid, [("a", "b")], dry_run=False, scan_roles=False)
    # Poll until the run reaches a terminal state (never stuck "running").
    for _ in range(200):
        if s.get_clone_run(rid)["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    import json
    row = s.get_clone_run(rid)
    assert row["status"] == "failed", f"run must not be stuck; got status={row['status']!r}"
    log = json.loads(row["log_json"])
    assert any("unexpected error" in entry for entry in log), f"expected 'unexpected error' in log: {log}"
