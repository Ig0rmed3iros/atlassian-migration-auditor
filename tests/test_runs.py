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


def wait_done(store, rid, timeout=30.0):
    # Generous timeout: the run engine is a daemon thread, and under full-suite
    # CPU contention it can miss a tight poll window (the historical
    # intermittent flake) — a longer ceiling returns the instant it finishes, so
    # it never slows a passing run, only survives a loaded machine.
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


def test_finalize_uses_ctx_vocabulary_labels(store, tmp_path):
    """stage_verify puts the connector's labels in ctx; finalize must template
    the headlines on them — a confluence run reads page(s), never issue(s)."""
    import json
    stages, _ = ok_stages()
    def verify(ctx):
        ctx["item_label"] = "page"
        ctx["container_label"] = "space"
    stages["verify"] = verify
    def compare(ctx):
        ctx["project_results"] = {"DOCS": {"stats": {
            "project": "DOCS", "src": 10, "tgt": 10, "common": 10,
            "missing_in_tgt": 0, "missing_in_src": 0, "tails": 0,
            "collisions": 0, "issues_with_mismatches": 2,
            "fidelity_pct": 80.0}}}
        ctx["issue_findings"] = []
    stages["compare"] = compare
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m", product="confluence")
    rid = eng.start(mid, {})
    r = wait_done(store, rid)
    assert r["status"] == "done" and r["verdict"] == "GAPS_FOUND"
    heads = " ".join(json.loads(r["stats_json"])["headlines"])
    assert "page(s)" in heads
    assert "issue" not in heads.lower()


def test_elevation_undo_runs_before_run_marked_done(store, tmp_path):
    # The elevation grant must be de-granted BEFORE the run is marked done — so
    # the privilege window is closed by the time the run reports complete, a
    # crash in the window leaves the run not-done (recoverable, no silent leak),
    # and the finalize-undo test stops racing (run-engine ordering).
    stages, rec = ok_stages()
    stages["verify"] = lambda ctx: ctx.update(src="SRC", tgt="TGT")
    status_at_undo = []

    def undo(src, tgt, mid, rid):
        status_at_undo.append(store.get_run(rid)["status"])
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages, elevation_undo=undo)
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    wait_done(store, rid)
    assert status_at_undo == ["running"]   # undo ran before status flipped to done


def test_finalize_invokes_elevation_undo(store, tmp_path):
    stages, rec = ok_stages()
    # make verify put sentinel clients in ctx
    def verify(ctx):
        ctx["src"] = "SRC"; ctx["tgt"] = "TGT"
    stages["verify"] = verify
    calls = []
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages,
                    elevation_undo=lambda src, tgt, mid, rid: calls.append((src, tgt, mid, rid)))
    mid = store.create_migration("m")
    rid = eng.start(mid, {})
    r = wait_done(store, rid)
    assert r["status"] == "done"
    assert calls == [("SRC", "TGT", mid, rid)]


def test_mark_stale_failed(store, tmp_path):
    mid = store.create_migration("m")
    rid = store.create_run(mid, {})        # simulates a run orphaned by restart
    eng = RunEngine(store, str(tmp_path / "ws"), stages={})
    n = eng.mark_stale_failed()
    assert n == 1 and store.get_run(rid)["status"] == "failed"


def test_concurrent_start_spawns_only_one_run(store, tmp_path):
    import threading
    stages, rec = ok_stages()
    gate = threading.Event()
    def slow_verify(ctx):
        gate.wait(2)
    stages["verify"] = slow_verify
    eng = RunEngine(store, str(tmp_path / "ws"), stages=stages)
    mid = store.create_migration("m")
    results = []
    barrier = threading.Barrier(2)
    def go():
        barrier.wait()
        try:
            results.append(("ok", eng.start(mid, {})))
        except RuntimeError:
            results.append(("rejected", None))
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    oks = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(oks) == 1 and len(rejected) == 1, results   # exactly one wins
    assert len(store.list_runs(mid)) == 1                   # only one run row
    gate.set()
    wait_done(store, oks[0][1])
