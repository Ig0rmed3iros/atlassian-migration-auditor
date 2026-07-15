"""Safety + concurrency hardening for the live-write env-fix apply path.

These tests pin three NEW defense-in-depth layers and the parallelisation of the
per-finding apply loop. They share the same real-JiraClient + httpx.MockTransport
harness as test_env_fix.py so they exercise client.req / paginate, never a mock
of the apply code itself.

Layers under test:
  L1  destructive-ops hard cap (MA_MAX_DESTRUCTIVE) — abort the WHOLE batch
      before any HTTP when the app-tier selection exceeds the cap.
  L2  circuit-breaker (MA_BREAKER_THRESHOLD) — a shared, thread-safe breaker
      trips on a server-side (5xx/429) write-failure storm and aborts the
      remaining deletes instead of hammering an instance that is failing.
  L3  pre-delete object snapshot — the live object JSON is captured at resolve
      time and attached to the DELETE log record (local-only, forensic/restore).

Concurrency:
  - apply_env_fixes fans the per-finding work out over a bounded pool; output is
    byte-for-byte identical to the sequential (1-worker) path (equivalence),
    a task that raises is isolated (failure isolation), and the pool is bounded.
"""
from __future__ import annotations

import copy
import threading

import httpx
import pytest

from auditor.client import Connection, JiraClient
from auditor.confluence.client import ConfluenceClient
from auditor.envaudit import apply as apply_mod
from auditor.envaudit import confluence_apply as capply_mod
from auditor.envaudit.apply import apply_env_fixes
from auditor.envaudit.confluence_apply import apply_confluence_fixes

CONF_BASE = "https://acme.atlassian.net/wiki"


# ---------------------------------------------------------------------------
# Harness (mirrors test_env_fix.py)
# ---------------------------------------------------------------------------

def _conn(site="https://acme.atlassian.net"):
    return Connection(auth_type="pat", site_url=site,
                      deployment="cloud", email="a@b.c", api_token="x")


def _client(handler, site="https://acme.atlassian.net"):
    return JiraClient(_conn(site), http=httpx.Client(
        transport=httpx.MockTransport(handler)), sleeper=lambda s: None)


def _env_finding(kind, name, area="schemes"):
    from auditor.envaudit.fixes import _FIXES, category_for
    fix = copy.copy(_FIXES.get(kind, {
        "tier": "human", "tier_label": "Fixable by a human",
        "title": kind, "detail": "n/a", "api_hint": None,
        "risk": "low", "reversible": True, "caveat": None,
    }))
    return {
        "area": area, "name": name, "kind": kind, "severity": "low",
        "detail": {"fix": fix, "category": category_for(kind), "severity": "low"},
    }


def _scheme_handler(deleted, *, delete_status=204, delete_names=None):
    """Resolve any workflowscheme by name, DELETE it (recording the id), then
    report it gone. delete_names, when given, restricts which scheme ids exist."""
    schemes = delete_names or {}

    def handler(req):
        p = str(req.url.path)
        m = req.method
        if p == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me"})
        if p == "/rest/api/3/workflowscheme" and m == "GET":
            # Report every not-yet-deleted scheme as live.
            return httpx.Response(200, json={"values": [
                {"id": sid, "name": nm}
                for nm, sid in schemes.items() if sid not in deleted]})
        if p.startswith("/rest/api/3/workflowscheme/") and m == "DELETE":
            sid = p.rsplit("/", 1)[-1]
            deleted.append(sid)
            return httpx.Response(delete_status, json={} if delete_status < 400
                                  else {"errorMessages": ["boom"]})
        return httpx.Response(404, json={})
    return handler


# ===========================================================================
# L1 — destructive-ops hard cap
# ===========================================================================

class TestDestructiveCap:

    def test_cap_aborts_whole_batch_before_any_http(self, monkeypatch):
        """3 app-tier findings with cap=2 -> raise before ANY request is made."""
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "2")
        seen = []

        def handler(req):
            seen.append(str(req.url.path))
            return httpx.Response(200, json={"values": []})

        cl = _client(handler)
        findings = [_env_finding("scheme_unused", f"S{i}") for i in range(3)]
        with pytest.raises(apply_mod.DestructiveCapExceeded):
            apply_env_fixes(cl, findings, lambda r: None,
                            expected_api_base="https://acme.atlassian.net")
        assert seen == [], "cap must abort before any HTTP call"

    def test_cap_zero_is_a_kill_switch(self, monkeypatch):
        """MA_MAX_DESTRUCTIVE=0 blocks even a single destructive op."""
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "0")
        seen = []
        cl = _client(lambda req: (seen.append(1), httpx.Response(200, json={}))[1])
        with pytest.raises(apply_mod.DestructiveCapExceeded):
            apply_env_fixes(cl, [_env_finding("scheme_unused", "S0")],
                            lambda r: None,
                            expected_api_base="https://acme.atlassian.net")
        assert seen == []

    def test_cap_counts_only_app_tier_findings(self, monkeypatch):
        """Human-tier findings do not count toward the destructive cap."""
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "1")
        deleted = []
        cl = _client(_scheme_handler(deleted, delete_names={"Old": "101"}))
        findings = [
            _env_finding("scheme_unused", "Old"),          # app-tier (counts)
            _env_finding("duplicate_field", "A", "fields"),  # human (ignored)
            _env_finding("duplicate_field", "B", "fields"),  # human (ignored)
        ]
        closed, still_open = apply_env_fixes(
            cl, findings, lambda r: None,
            expected_api_base="https://acme.atlassian.net")
        assert deleted == ["101"], "the single app-tier delete must proceed"
        assert closed == 1

    def test_cap_at_limit_is_allowed(self, monkeypatch):
        """Exactly cap app-tier findings is allowed (boundary: > not >=)."""
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "2")
        deleted = []
        cl = _client(_scheme_handler(deleted,
                                     delete_names={"Old0": "100", "Old1": "101"}))
        findings = [_env_finding("scheme_unused", f"Old{i}") for i in range(2)]
        closed, _ = apply_env_fixes(cl, findings, lambda r: None,
                                    expected_api_base="https://acme.atlassian.net")
        assert closed == 2 and sorted(deleted) == ["100", "101"]


def _persistent_scheme_handler(attempts, status):
    """Schemes S0..S9 are ALWAYS live (deletes return `status` and never make an
    object disappear), so every finding reaches its own DELETE unless the breaker
    blocks it. attempts records each DELETE actually attempted."""
    def handler(req):
        p, m = str(req.url.path), req.method
        if p == "/rest/api/3/workflowscheme" and m == "GET":
            return httpx.Response(200, json={"values": [
                {"id": str(100 + i), "name": f"S{i}"} for i in range(10)]})
        if p.startswith("/rest/api/3/workflowscheme/") and m == "DELETE":
            attempts.append(p.rsplit("/", 1)[-1])
            return httpx.Response(status, json={"errorMessages": ["x"]})
        return httpx.Response(404, json={})
    return handler


# ===========================================================================
# L2 — circuit-breaker
# ===========================================================================

class TestCircuitBreaker:

    def test_breaker_trips_on_server_error_storm(self, monkeypatch):
        """After `threshold` server-side (5xx) delete failures, the breaker opens
        and the remaining deletes are SKIPPED, not attempted."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "1")   # deterministic order
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
        attempts = []
        cl = _client(_persistent_scheme_handler(attempts, 500))
        findings = [_env_finding("scheme_unused", f"S{i}") for i in range(5)]
        log = []
        closed, still_open = apply_env_fixes(
            cl, findings, log.append,
            expected_api_base="https://acme.atlassian.net")
        # Count DISTINCT schemes whose DELETE was attempted (the client retries a
        # 5xx internally, so one logical delete == several transport attempts).
        assert len(set(attempts)) == 2, (
            f"breaker must stop after 2 server-side failures, got {set(attempts)}")
        assert closed == 0 and still_open == 5
        assert any("circuit breaker" in (r.get("error") or "").lower()
                   for r in log), "skipped findings must record a breaker reason"

    def test_breaker_ignores_client_errors(self, monkeypatch):
        """A 4xx (object-level) delete failure is NOT an instance outage and must
        not trip the breaker — every finding still gets attempted."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "1")
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
        attempts = []
        cl = _client(_persistent_scheme_handler(attempts, 400))
        findings = [_env_finding("scheme_unused", f"S{i}") for i in range(4)]
        apply_env_fixes(cl, findings, lambda r: None,
                        expected_api_base="https://acme.atlassian.net")
        assert len(set(attempts)) == 4, "4xx must not trip the breaker"

    def test_breaker_disabled_when_threshold_zero(self, monkeypatch):
        """MA_BREAKER_THRESHOLD=0 disables the breaker entirely."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "1")
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "0")
        attempts = []
        cl = _client(_persistent_scheme_handler(attempts, 500))
        findings = [_env_finding("scheme_unused", f"S{i}") for i in range(4)]
        apply_env_fixes(cl, findings, lambda r: None,
                        expected_api_base="https://acme.atlassian.net")
        assert len(set(attempts)) == 4, "threshold=0 must never trip"


# ===========================================================================
# L3 — pre-delete object snapshot
# ===========================================================================

class TestPreDeleteSnapshot:

    def test_delete_record_carries_snapshot_of_object(self):
        """The DELETE log record captures the resolved object's live JSON, so the
        destructive action is forensically reconstructable (local-only)."""
        import json
        deleted = []
        cl = _client(_scheme_handler(deleted, delete_names={"Old Scheme": "101"}))
        log = []
        apply_env_fixes(cl, [_env_finding("scheme_unused", "Old Scheme")],
                        log.append,
                        expected_api_base="https://acme.atlassian.net")
        dels = [r for r in log if r.get("method") == "DELETE" and r.get("ok")]
        assert dels, f"expected a successful DELETE record: {log}"
        snap = json.loads(dels[0]["snapshot_json"])
        assert snap.get("name") == "Old Scheme" and str(snap.get("id")) == "101"

    def test_non_destructive_records_have_no_snapshot(self):
        """A resolve-absent (idempotent no-op) record carries no snapshot."""
        def handler(req):
            if "workflowscheme" in str(req.url.path):
                return httpx.Response(200, json={"values": []})
            return httpx.Response(404, json={})
        cl = _client(handler)
        log = []
        apply_env_fixes(cl, [_env_finding("scheme_unused", "Ghost")], log.append,
                        expected_api_base="https://acme.atlassian.net")
        assert log and all(r.get("snapshot_json") is None for r in log)

    def test_store_persists_snapshot_json(self, tmp_path):
        """fix_actions persists and returns the snapshot_json column."""
        from webapp.store import Store
        s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
        mid = s.create_migration("m")
        rid = s.create_run(mid, {}, kind="env_fix")
        s.insert_fix_actions(rid, [{
            "fix_id": "scheme_unused", "object_name": "Old", "method": "DELETE",
            "path": "/x", "status": 204, "ok": True, "created_id": None,
            "error": None, "snapshot_json": '{"id":"101","name":"Old"}',
        }])
        rows = s.get_fix_actions(rid)
        assert rows and rows[0]["snapshot_json"] == '{"id":"101","name":"Old"}'


BASE = "https://acme.atlassian.net"


# ===========================================================================
# Concurrency — parallel per-finding apply
# ===========================================================================

class TestApplyConcurrency:

    def test_deletes_run_concurrently(self, monkeypatch):
        """With >1 worker the per-finding deletes actually overlap in time."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "4")
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
        names = {f"S{i}": str(100 + i) for i in range(4)}
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0}
        barrier = threading.Barrier(4, timeout=4)

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/workflowscheme" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": sid, "name": nm} for nm, sid in names.items()]})
            if p.startswith("/rest/api/3/workflowscheme/") and m == "DELETE":
                with lock:
                    state["inflight"] += 1
                    state["peak"] = max(state["peak"], state["inflight"])
                try:
                    barrier.wait()   # release only once all 4 deletes overlap
                except threading.BrokenBarrierError:
                    pass             # sequential path: times out -> peak stays 1
                with lock:
                    state["inflight"] -= 1
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        findings = [_env_finding("scheme_unused", n) for n in names]
        apply_env_fixes(cl, findings, lambda r: None, expected_api_base=BASE)
        assert state["peak"] >= 2, (
            f"deletes did not overlap (peak in-flight={state['peak']})")

    def test_unused_field_screen_scan_runs_concurrently(self, monkeypatch):
        """The unused_custom_field TOCTOU sweeps EVERY screen for the field id;
        those per-screen reads must overlap, not run one screen at a time."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "4")   # scan honours the apply knob
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
        sids = ["10001", "10002", "10003", "10004"]
        deleted = {"f": False}
        lock = threading.Lock()
        state = {"inflight": 0, "peak": 0}
        barrier = threading.Barrier(4, timeout=4)

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/field" and m == "GET":
                return httpx.Response(200, json=[] if deleted["f"] else [
                    {"id": "customfield_10500", "name": "Migrated Notes",
                     "custom": True}])
            if p == "/rest/api/3/screens" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": s, "name": f"Screen {s}"} for s in sids]})
            if p.endswith("/tabs") and "/screens/" in p and m == "GET":
                with lock:
                    state["inflight"] += 1
                    state["peak"] = max(state["peak"], state["inflight"])
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                with lock:
                    state["inflight"] -= 1
                return httpx.Response(200, json=[{"id": "1", "name": "Tab"}])
            if "/tabs/1/fields" in p and m == "GET":
                return httpx.Response(200, json=[{"id": "summary"}])
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/filter/search" and m == "GET":
                return httpx.Response(200, json={"values": [], "isLast": True})
            if p == "/rest/api/3/field/customfield_10500" and m == "DELETE":
                deleted["f"] = True
                return httpx.Response(303, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        log = []
        closed, _ = apply_env_fixes(
            cl, [_env_finding("unused_custom_field", "Migrated Notes", "fields")],
            log.append, expected_api_base=BASE)
        assert state["peak"] >= 2, (
            f"screen scan did not overlap (peak={state['peak']})")
        assert closed == 1, "field on no screen + zero values must be deleted"

    def test_isolates_a_crashing_finding(self, monkeypatch):
        """If one finding's worker raises, siblings still complete and the
        crashed one is recorded (not swallowed, not aborting the batch)."""
        findings = [_env_finding("scheme_unused", "A"),
                    _env_finding("scheme_unused", "B")]

        def fake_one(client, finding, log, breaker, dry_run=False):
            if finding.get("name") == "A":
                raise RuntimeError("boom in A")
            return "closed"

        monkeypatch.setattr(apply_mod, "_apply_one", fake_one)
        cl = _client(lambda req: httpx.Response(404, json={}))
        log = []
        closed, still_open = apply_env_fixes(cl, findings, log.append,
                                             expected_api_base=BASE)
        assert closed == 1, "the healthy finding must still succeed"
        assert still_open == 1, "the crashing finding is isolated as still_open"
        assert any("boom in A" in (r.get("error") or "") for r in log)

    def test_seq_vs_parallel_equivalence(self, monkeypatch):
        """Output is byte-for-byte identical between 1 worker and 8 workers —
        the parallelisation changes timing only, never the result or the log."""
        names = {f"Sch{i}": str(200 + i) for i in range(8)}
        findings = [_env_finding("scheme_unused", n) for n in names]
        findings.append(_env_finding("scheme_unused", "Ghost"))       # absent
        findings.append(_env_finding("duplicate_field", "X", "fields"))  # human

        def run(workers):
            monkeypatch.setenv("MA_APPLY_WORKERS", str(workers))
            monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
            deleted = []
            cl = _client(_scheme_handler(deleted, delete_names=names))
            log = []
            c, s = apply_env_fixes(cl, findings, log.append,
                                   expected_api_base=BASE)
            norm = [(r.get("method"), r.get("path"), r.get("ok"),
                     r.get("error"), r.get("snapshot_json")) for r in log]
            return c, s, norm

        seq = run(1)
        par = run(8)
        assert seq == par, "sequential and parallel apply must be identical"
        assert seq[0] == 9 and seq[1] == 0   # 8 deletes + ghost(absent) closed

    def test_breaker_bounds_deletes_under_concurrency(self, monkeypatch):
        """Even parallel, a 5xx storm trips the breaker and bounds total deletes
        to roughly threshold + in-flight, never the whole batch."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "4")
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
        attempts = []
        cl = _client(_persistent_scheme_handler(attempts, 500))
        findings = [_env_finding("scheme_unused", f"S{i}") for i in range(10)]
        apply_env_fixes(cl, findings, lambda r: None, expected_api_base=BASE)
        # threshold(2) + at most (workers-1=3) already in-flight when it trips.
        assert len(set(attempts)) <= 2 + 3, (
            f"breaker failed to bound a parallel delete storm: {set(attempts)}")
        assert len(set(attempts)) < 10, "breaker must stop well short of all 10"


# ===========================================================================
# Confluence apply — same safety layers + concurrency parity
# ===========================================================================

def _conf_client(handler):
    conn = Connection(auth_type="pat", site_url="https://acme.atlassian.net",
                      deployment="cloud", email="a@b.c", api_token="x")
    return ConfluenceClient(conn, http=httpx.Client(
        transport=httpx.MockTransport(handler)), sleeper=lambda s: None)


def _conf_space_handler(attempts, archived_ids, *, put_status=200, n=12,
                        barrier=None, peak=None, lock=None):
    spaces = {f"K{i}": str(100 + i) for i in range(n)}

    def handler(req):
        p, m = str(req.url.path), req.method
        if p == "/wiki/api/v2/spaces" and m == "GET":
            return httpx.Response(200, json={"results": [
                {"id": sid, "key": k, "name": f"Name {k}", "type": "global",
                 "status": "archived" if sid in archived_ids else "current"}
                for k, sid in spaces.items()], "_links": {}})
        if p == "/wiki/rest/api/search" and m == "GET":
            return httpx.Response(200, json={"totalSize": 0, "results": []})
        if p.startswith("/wiki/api/v2/spaces/") and m == "PUT":
            sid = p.rsplit("/", 1)[-1]
            attempts.append(sid)
            if barrier is not None:
                with lock:
                    peak["inflight"] += 1
                    peak["peak"] = max(peak["peak"], peak["inflight"])
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                with lock:
                    peak["inflight"] -= 1
            if put_status < 300:
                archived_ids.append(sid)
                return httpx.Response(put_status,
                                      json={"id": sid, "status": "archived"})
            return httpx.Response(put_status, json={"message": "boom"})
        return httpx.Response(404, json={})
    return handler


class TestConfluenceParity:

    def test_confluence_cap_aborts_before_any_http(self, monkeypatch):
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "2")
        seen = []
        cl = _conf_client(lambda req: (seen.append(1),
                                       httpx.Response(200, json={"results": []}))[1])
        findings = [_env_finding("empty_space", f"K{i}", "spaces")
                    for i in range(3)]
        with pytest.raises(apply_mod.DestructiveCapExceeded):
            apply_confluence_fixes(cl, findings, lambda r: None,
                                   expected_api_base=CONF_BASE)
        assert seen == []

    def test_confluence_breaker_trips_on_server_storm(self, monkeypatch):
        monkeypatch.setenv("MA_APPLY_WORKERS", "1")
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
        attempts, archived = [], []
        cl = _conf_client(_conf_space_handler(attempts, archived, put_status=500))
        findings = [_env_finding("empty_space", f"K{i}", "spaces")
                    for i in range(5)]
        apply_confluence_fixes(cl, findings, lambda r: None,
                               expected_api_base=CONF_BASE)
        assert len(set(attempts)) == 2, (
            f"breaker must bound confluence archives: {set(attempts)}")

    def test_confluence_archive_record_carries_snapshot(self):
        import json
        attempts, archived = [], []
        cl = _conf_client(_conf_space_handler(attempts, archived, n=1))
        log = []
        apply_confluence_fixes(cl, [_env_finding("empty_space", "K0", "spaces")],
                               log.append, expected_api_base=CONF_BASE)
        puts = [r for r in log if r.get("method") == "PUT" and r.get("ok")]
        assert puts, f"expected a successful archive record: {log}"
        snap = json.loads(puts[0]["snapshot_json"])
        assert snap.get("key") == "K0"

    def test_confluence_archives_concurrently(self, monkeypatch):
        monkeypatch.setenv("MA_APPLY_WORKERS", "4")
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
        attempts, archived = [], []
        lock = threading.Lock()
        peak = {"inflight": 0, "peak": 0}
        barrier = threading.Barrier(4, timeout=4)
        cl = _conf_client(_conf_space_handler(
            attempts, archived, n=4, barrier=barrier, peak=peak, lock=lock))
        findings = [_env_finding("empty_space", f"K{i}", "spaces")
                    for i in range(4)]
        apply_confluence_fixes(cl, findings, lambda r: None,
                               expected_api_base=CONF_BASE)
        assert peak["peak"] >= 2, f"archives did not overlap (peak={peak['peak']})"

    def test_confluence_seq_vs_parallel_equivalence(self, monkeypatch):
        findings = [_env_finding("empty_space", f"K{i}", "spaces")
                    for i in range(6)]

        def run(workers):
            monkeypatch.setenv("MA_APPLY_WORKERS", str(workers))
            monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
            attempts, archived = [], []
            cl = _conf_client(_conf_space_handler(attempts, archived, n=6))
            log = []
            c, s = apply_confluence_fixes(cl, findings, log.append,
                                          expected_api_base=CONF_BASE)
            norm = [(r.get("method"), r.get("path"), r.get("ok"),
                     r.get("error"), r.get("snapshot_json")) for r in log]
            return c, s, norm

        assert run(1) == run(8)


# ===========================================================================
# Regression: the REAL apply log must persist (fix_id NOT NULL) + crash safety
# ===========================================================================

class TestApplyLogPersistAndCrashSafety:

    def test_real_apply_log_persists_into_store(self, tmp_path):
        """The genuine apply log (records straight from apply_env_fixes) must
        round-trip into fix_actions without an IntegrityError. Regression for the
        fix_id-NOT-NULL crash that fired AFTER the live deletes, destroying the
        forensic log + snapshots. (Every prior store test hand-fed a fix_id.)"""
        from webapp.store import Store
        deleted = []
        cl = _client(_scheme_handler(deleted, delete_names={"Old Scheme": "101"}))
        log = []
        apply_env_fixes(cl, [_env_finding("scheme_unused", "Old Scheme")],
                        log.append, expected_api_base=BASE)
        assert log, "apply must have produced at least one record"

        s = Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))
        mid = s.create_migration("m")
        rid = s.create_run(mid, {}, kind="env_fix")
        s.insert_fix_actions(rid, log)   # must NOT raise
        rows = s.get_fix_actions(rid)
        assert rows, "the apply log must persist"
        assert all(r["fix_id"] for r in rows), "every persisted action needs a fix_id"
        # fix_id is the meaningful finding kind, not a placeholder.
        assert any(r["fix_id"] == "scheme_unused" for r in rows)

    def test_partial_records_survive_a_worker_crash(self, monkeypatch):
        """If a worker buffers records and THEN raises, those records must still
        be replayed (audit trail under crash), and the crash itself recorded."""
        def fake_one(client, finding, log, breaker, dry_run=False):
            log(apply_mod._rec(finding.get("name"), "DELETE", "/x", 204, True))
            raise RuntimeError("boom after record")

        monkeypatch.setattr(apply_mod, "_apply_one", fake_one)
        cl = _client(lambda req: httpx.Response(404, json={}))
        log = []
        closed, still_open = apply_env_fixes(
            cl, [_env_finding("scheme_unused", "A")], log.append,
            expected_api_base=BASE)
        assert any(r.get("method") == "DELETE" for r in log), (
            "a record buffered before the crash must survive into the log")
        assert any("boom after record" in (r.get("error") or "") for r in log)
        assert still_open == 1


# ===========================================================================
# Coverage: expanded-kind snapshot, Confluence group, empty batch
# ===========================================================================

class TestCoverageGaps:

    def test_empty_findings_is_a_noop(self):
        """No findings -> (0,0), zero HTTP, zero records (map_results empty path)."""
        seen = []
        cl = _client(lambda req: (seen.append(1), httpx.Response(404, json={}))[1])
        log = []
        assert apply_env_fixes(cl, [], log.append,
                               expected_api_base=BASE) == (0, 0)
        assert seen == [] and log == []

    def test_confluence_empty_findings_is_a_noop(self):
        seen = []
        cl = _conf_client(lambda req: (seen.append(1),
                                       httpx.Response(404, json={}))[1])
        log = []
        assert apply_confluence_fixes(cl, [], log.append,
                                      expected_api_base=CONF_BASE) == (0, 0)
        assert seen == [] and log == []

    def test_empty_project_delete_carries_snapshot(self):
        """The EXPANDED delete path (_apply_expanded) captures a snapshot too —
        not just the generic scheme path."""
        import json
        deleted = {"p": False}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/project/search" and m == "GET":
                return httpx.Response(200, json={"values": [] if deleted["p"] else [
                    {"id": "10500", "key": "OLD", "name": "Old Project"}]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p == "/rest/api/3/project/OLD" and m == "DELETE":
                deleted["p"] = True
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        log = []
        closed, _ = apply_env_fixes(
            cl, [_env_finding("empty_project", "OLD", "projects")], log.append,
            expected_api_base=BASE)
        dels = [r for r in log if r.get("method") == "DELETE" and r.get("ok")]
        assert dels, f"expected an empty_project DELETE: {log}"
        snap = json.loads(dels[0]["snapshot_json"])
        assert snap.get("key") == "OLD"
        assert closed == 1

    def test_confluence_empty_group_deletes_with_snapshot(self):
        """The other Confluence app-tier kind: delete + minimal group snapshot."""
        import json
        deleted = []

        def handler(req):
            p, m = str(req.url.path), req.method
            params = dict(req.url.params)
            if p == "/wiki/rest/api/group" and m == "GET":
                return httpx.Response(200, json={"results": [] if deleted else [
                    {"name": "ghost-group", "id": "gid-9"}], "_links": {}})
            if "/member" in p and m == "GET":
                return httpx.Response(200, json={"results": [], "size": 0,
                                                 "_links": {}})
            if p == "/wiki/rest/api/group" and m == "DELETE":
                assert params.get("name") == "ghost-group"
                deleted.append(True)
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _conf_client(handler)
        log = []
        closed, _ = apply_confluence_fixes(
            cl, [_env_finding("confluence_empty_group", "ghost-group", "groups")],
            log.append, expected_api_base=CONF_BASE)
        dels = [r for r in log if r.get("method") == "DELETE" and r.get("ok")]
        assert dels, f"expected a group DELETE: {log}"
        snap = json.loads(dels[0]["snapshot_json"])
        assert snap.get("name") == "ghost-group" and snap.get("type") == "group"
        assert closed == 1

    def test_breaker_trips_on_expanded_kind(self, monkeypatch):
        """The breaker gate lives on the EXPANDED path too — a 5xx storm of
        empty_project deletes is bounded, not run to completion."""
        monkeypatch.setenv("MA_APPLY_WORKERS", "1")
        monkeypatch.setenv("MA_BREAKER_THRESHOLD", "2")
        monkeypatch.setenv("MA_MAX_DESTRUCTIVE", "100")
        attempts = []

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/project/search" and m == "GET":
                return httpx.Response(200, json={"values": [
                    {"id": str(10500 + i), "key": f"K{i}", "name": f"P{i}"}
                    for i in range(6)]})
            if p == "/rest/api/3/search/approximate-count" and m == "POST":
                return httpx.Response(200, json={"count": 0})
            if p.startswith("/rest/api/3/project/") and m == "DELETE":
                attempts.append(p.rsplit("/", 1)[-1])
                return httpx.Response(500, json={"errorMessages": ["x"]})
            return httpx.Response(404, json={})

        cl = _client(handler)
        findings = [_env_finding("empty_project", f"K{i}", "projects")
                    for i in range(6)]
        apply_env_fixes(cl, findings, lambda r: None, expected_api_base=BASE)
        assert len(set(attempts)) == 2, (
            f"breaker must bound expanded-kind deletes: {set(attempts)}")


# ===========================================================================
# Durability — fix-log streamed to the store as each action fires (Bug 4)
# ===========================================================================

class TestFixLogDurability:
    """A live DELETE that fires against production must have its record durably
    persisted at the moment it happens, not buffered until finalize — else a
    hard crash mid-apply (SIGKILL / OOM / power) erases the audit trail of
    destructive ops that already hit the instance (review Bug 4)."""

    def test_record_sink_streams_each_record_with_fix_id(self):
        deleted = []
        cl = _client(_scheme_handler(deleted, delete_names={"Old": "101"}))
        streamed = []
        log = []
        closed, _ = apply_env_fixes(
            cl, [_env_finding("scheme_unused", "Old")], log.append,
            expected_api_base=BASE, record_sink=streamed.append)
        assert closed == 1
        # Every action reached the durable sink, each already stamped with the
        # finding kind (so a partial crash never leaves an unkeyed row).
        assert streamed and all(r.get("fix_id") == "scheme_unused"
                                for r in streamed)
        # The destructive DELETE is in the durable stream.
        assert any(r.get("method") == "DELETE" for r in streamed)
        # For a single finding the durable stream equals the in-memory log.
        assert streamed == log

    def test_delete_record_survives_worker_crash_after_delete(self):
        calls = {"get": 0}

        def handler(req):
            p, m = str(req.url.path), req.method
            if p == "/rest/api/3/myself":
                return httpx.Response(200, json={"accountId": "me"})
            if p == "/rest/api/3/workflowscheme" and m == "GET":
                calls["get"] += 1
                if calls["get"] == 1:
                    return httpx.Response(200, json={"values": [
                        {"id": "101", "name": "Old"}]})
                # The post-delete verify re-read dies (link dropped) AFTER the
                # DELETE already fired against production.
                raise httpx.ConnectError("link dropped post-delete")
            if p.startswith("/rest/api/3/workflowscheme/") and m == "DELETE":
                return httpx.Response(204, json={})
            return httpx.Response(404, json={})

        cl = _client(handler)
        streamed = []
        apply_env_fixes(cl, [_env_finding("scheme_unused", "Old")],
                        lambda r: None, expected_api_base=BASE,
                        record_sink=streamed.append)
        # Even though the worker crashed on the verify read, the DELETE record
        # was already streamed durably — the trail of what hit prod survives.
        assert any(r.get("method") == "DELETE" for r in streamed)

    def test_no_record_sink_keeps_legacy_buffered_behaviour(self):
        # Back-compat: without a sink, nothing streams and the caller's log is
        # the only record channel (existing finalize-persist path).
        deleted = []
        cl = _client(_scheme_handler(deleted, delete_names={"Old": "101"}))
        log = []
        apply_env_fixes(cl, [_env_finding("scheme_unused", "Old")], log.append,
                        expected_api_base=BASE)   # no record_sink
        assert any(r.get("method") == "DELETE" for r in log)


def test_write_breaker_trips_on_transport_failure_not_4xx():
    # A connection-drop storm returns st=-1 (exhausted idempotent retries); it
    # must count toward the breaker trip alongside 5xx/429. A 4xx is object-level
    # and must never trip it.
    from auditor.envaudit.apply import _WriteBreaker
    b = _WriteBreaker(2)
    b.record(404)
    b.record(400)
    assert not b.should_block(), "4xx must not trip the breaker"
    b.record(-1)                 # transport failure -> counts (1/2)
    assert not b.should_block()
    b.record(503)                # server error -> counts (2/2) -> trip
    assert b.should_block()
