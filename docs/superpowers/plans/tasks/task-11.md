### Task 11: `webapp/runs.py` — run engine

**Files:**
- Create: `webapp/runs.py`
- Test: `tests/test_runs.py`

The engine owns the phase state machine. Core stage functions are injected as a `stages` dict so tests run instantly with stubs and `main.py` wires the real ones. Elevation is NOT a phase action — the permissions phase only detects and records; elevation happens via an explicit endpoint between runs (spec §6: consent-gated), after which the operator re-runs.

- [ ] **Step 1: Write the failing tests**

`tests/test_runs.py`:
```python
import time
import pytest
from webapp.runs import PHASES, RunEngine
from webapp.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(db_path=str(tmp_path / "t.db"), key_path=str(tmp_path / ".key"))


def ok_stages(record=None):
    rec = record if record is not None else []
    def stage(name):
        def fn(ctx):
            rec.append(name)
            if name == "compare":
                ctx["project_results"] = {"AC": {"stats": {
                    "project": "AC", "src": 10, "tgt": 10, "common": 10,
                    "missing_in_tgt": 0, "missing_in_src": 0, "tails": 0,
                    "collisions": 0, "issues_with_mismatches": 0,
                    "fidelity_pct": 100.0}}}
                ctx["issue_findings"] = []
            if name == "config":
                ctx["config_result"] = {"areas": {}, "findings": []}
            if name == "permissions":
                ctx["blind_spots"] = []
        return fn
    return {p: stage(p) for p in PHASES if p != "finalize"}, rec


def wait_done(store, rid, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = store.get_run(rid)
        if r["status"] != "running":
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_happy_path_runs_all_phases_and_finalizes(store, tmp_path):
    stages, rec = ok_stages()
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {"projects": ["AC"]})
    r = wait_done(store, rid)
    assert r["status"] == "done" and r["verdict"] == "CLEAN"
    assert rec == [p for p in PHASES if p != "finalize"]
    evs = store.get_events(rid)
    assert any("finalize" == e["phase"] for e in evs)


def test_failing_phase_marks_run_failed_with_event(store, tmp_path):
    stages, _ = ok_stages()
    def boom(ctx):
        raise RuntimeError("source unreachable")
    stages["extract"] = boom
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    r = wait_done(store, rid)
    assert r["status"] == "failed"
    msgs = [e["message"] for e in store.get_events(rid)]
    assert any("source unreachable" in m for m in msgs)


def test_second_start_while_active_raises(store, tmp_path):
    started = []
    stages, _ = ok_stages(started)
    import threading
    gate = threading.Event()
    def slow(ctx):
        gate.wait(2)
    stages["verify"] = slow
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    with pytest.raises(RuntimeError):
        eng.start(mid, {})
    gate.set()
    wait_done(store, rid)


def test_cancel_stops_between_phases(store, tmp_path):
    stages, rec = ok_stages()
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    # cancel before the thread even starts by pre-setting the flag via start+cancel
    import threading
    hold = threading.Event()
    def first(ctx):
        hold.wait(2)
    stages["verify"] = first
    rid = eng.start(mid, {})
    eng.cancel(rid)
    hold.set()
    r = wait_done(store, rid)
    assert r["status"] == "cancelled"
    assert "scope" not in rec        # no phase after the cancel point ran


def test_mark_stale_failed(store, tmp_path):
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})        # simulates a run orphaned by restart
    eng = RunEngine(store, str(tmp_path / "ws"), stages={})
    n = eng.mark_stale_failed()
    assert n == 1 and store.get_run(rid)["status"] == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runs.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.runs'`.

- [ ] **Step 3: Write the implementation**

`webapp/runs.py`:
```python
"""Background run engine: one thread per run, phase state machine, events.

Stages are injected callables `fn(ctx)` keyed by phase name; ctx is a dict
the stages share (clients, params, results). The engine owns persistence:
phase transitions, events, findings, the final verdict. Swapping the thread
for a queue worker later only touches this file (spec §9).
"""
from __future__ import annotations

import os
import threading
import traceback

from auditor.findings import build_run_summary
from .store import Store

PHASES = ["verify", "scope", "permissions", "extract", "compare", "config",
          "finalize"]


class RunEngine:
    def __init__(self, store: Store, workspace_root: str, stages: dict | None = None):
        self.store = store
        self.workspace_root = workspace_root
        self.stages = stages or {}
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self, migration_id: int, params: dict) -> int:
        if self.store.active_run(migration_id):
            raise RuntimeError("a run is already active for this migration")
        run_id = self.store.create_run(migration_id, params)
        # Resumability (spec §6): re-running with reuse_extracts_from points
        # this run at the PRIOR run's workspace so cached gz extracts are
        # reused and stage_extract skips re-pulling them.
        ws_run = params.get("reuse_extracts_from") or run_id
        ws = os.path.join(self.workspace_root, str(migration_id), str(ws_run))
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        os.makedirs(os.path.join(ws, "tgt"), exist_ok=True)
        t = threading.Thread(target=self._execute,
                             args=(run_id, migration_id, params, ws),
                             daemon=True, name=f"run-{run_id}")
        t.start()
        return run_id

    def cancel(self, run_id: int) -> None:
        with self._lock:
            self._cancelled.add(run_id)
        self.store.add_event(run_id, "engine", "warn", "cancel requested")

    def mark_stale_failed(self) -> int:
        stale = self.store.stale_running()
        for r in stale:
            self.store.update_run(r["id"], status="failed")
            self.store.add_event(r["id"], "engine", "error",
                                 "marked failed: server restarted mid-run")
        return len(stale)

    def _is_cancelled(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._cancelled

    # -------------------------------------------------------------- execute
    def _execute(self, run_id: int, migration_id: int, params: dict, ws: str):
        store = self.store
        ctx = {"run_id": run_id, "migration_id": migration_id,
               "params": params, "workspace": ws, "store": store,
               "project_results": {}, "issue_findings": [],
               "config_result": {"areas": {}, "findings": []},
               "blind_spots": []}

        def say(phase, msg, level="info"):
            store.add_event(run_id, phase, level, msg)

        try:
            for phase in PHASES:
                if self._is_cancelled(run_id):
                    store.update_run(run_id, status="cancelled")
                    say("engine", "run cancelled", "warn")
                    return
                store.update_run(run_id, phase=phase)
                say(phase, f"phase started: {phase}")
                if phase == "finalize":
                    summary = build_run_summary(ctx["project_results"],
                                                ctx["config_result"],
                                                ctx["blind_spots"])
                    if ctx["issue_findings"]:
                        store.insert_findings_issue(run_id, ctx["issue_findings"])
                    if ctx["config_result"].get("findings"):
                        store.insert_findings_config(
                            run_id, ctx["config_result"]["findings"])
                    stats = dict(summary["stats"])
                    stats["headlines"] = summary["headlines"]
                    stats["areas"] = ctx["config_result"].get("areas", {})
                    stats["project_stats"] = {
                        k: v["stats"] for k, v in ctx["project_results"].items()}
                    store.update_run(run_id, status="done",
                                     verdict=summary["verdict"], stats=stats)
                    say(phase, f"run complete: verdict={summary['verdict']}")
                    return
                fn = self.stages.get(phase)
                if fn is not None:
                    fn(ctx)
                say(phase, f"phase done: {phase}")
        except Exception as exc:  # noqa: BLE001 — any stage failure must land in the run record, not a dead thread
            say("engine", f"run failed: {exc}", "error")
            say("engine", traceback.format_exc()[-1500:], "error")
            store.update_run(run_id, status="failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_runs.py -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/runs.py tests/test_runs.py
git commit -m "feat: threaded run engine with phase state machine, cancel, stale-run recovery"
```

---

> NOTE from Task 3 review: Store serializes ALL statements behind one RLock — engine writes and SSE/API reads are safe to interleave, but hold no Store call open across long work.

## Post-review amendments (applied)

Active-run guard now holds the engine lock across check-then-create (closes a proven ~70% TOCTOU that spawned duplicate audit threads); regression test added (`test_concurrent_start_spawns_only_one_run`). The lock is `threading.Lock()` (not RLock) — confirmed no re-entrancy: `active_run` and `create_run` are Store calls that acquire only the Store's own separate RLock, never the engine lock.

