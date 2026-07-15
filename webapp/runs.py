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

from auditor.aggregate import compute_run_fidelity
from auditor.findings import build_run_summary
from .store import Store

AUDIT_PHASES = ["verify", "scope", "permissions", "extract", "compare",
                "config", "finalize"]
FIX_PHASES = ["verify", "apply", "reaudit", "finalize"]
ENV_PHASES = ["verify", "scope", "gather", "checks", "analysis", "finalize"]
ENV_FIX_PHASES = ["verify", "apply", "reaudit", "finalize"]
PHASES = AUDIT_PHASES   # back-compat alias for existing imports


class RunEngine:
    def __init__(self, store: Store, workspace_root: str, stages: dict | None = None,
                 fix_stages: dict | None = None, env_stages: dict | None = None,
                 env_fix_stages: dict | None = None,
                 elevation_undo=None):
        self.store = store
        self.workspace_root = workspace_root
        self.stages = stages or {}
        self.fix_stages = fix_stages or {}
        self.env_stages = env_stages or {}
        self.env_fix_stages = env_fix_stages or {}
        # Injected by create_app: undo(src, tgt, migration_id, run_id). Default
        # no-op keeps runs.py free of a stages import and lets tests pass a stub.
        self.elevation_undo = elevation_undo or (lambda src, tgt, mid, rid: None)
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self, migration_id: int, params: dict, kind: str = "audit",
              source_run_id: int | None = None) -> int:
        # Hold the lock across check-then-create so two concurrent start() calls
        # can't both pass the active-run guard (TOCTOU -> duplicate audit threads).
        with self._lock:
            if self.store.active_run(migration_id):
                raise RuntimeError("a run is already active for this migration")
            run_id = self.store.create_run(migration_id, params, kind=kind,
                                           source_run_id=source_run_id)
        # Resumability (spec §6): re-running with reuse_extracts_from points
        # this run at the PRIOR run's workspace so cached gz extracts are
        # reused and stage_extract skips re-pulling them.
        ws_run = params.get("reuse_extracts_from") or run_id
        ws = os.path.join(self.workspace_root, str(migration_id), str(ws_run))
        os.makedirs(os.path.join(ws, "src"), exist_ok=True)
        # env_audit and env_fix are single-connection (source only) — no
        # target side. Only the two-sided migration/fix kinds get a tgt/ dir.
        if kind not in ("env_audit", "env_fix"):
            os.makedirs(os.path.join(ws, "tgt"), exist_ok=True)
        t = threading.Thread(target=self._execute,
                             args=(run_id, migration_id, params, ws, kind),
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
    def _execute(self, run_id: int, migration_id: int, params: dict, ws: str,
                 kind: str = "audit"):
        store = self.store
        ctx = {"run_id": run_id, "migration_id": migration_id,
               "params": params, "workspace": ws, "store": store, "kind": kind,
               "project_results": {}, "issue_findings": [],
               "config_result": {"areas": {}, "findings": []},
               "blind_spots": []}
        if kind == "fix":
            phases, stages = FIX_PHASES, self.fix_stages
        elif kind == "env_audit":
            phases, stages = ENV_PHASES, self.env_stages
        elif kind == "env_fix":
            phases, stages = ENV_FIX_PHASES, self.env_fix_stages
        else:
            phases, stages = AUDIT_PHASES, self.stages

        def say(phase, msg, level="info"):
            store.add_event(run_id, phase, level, msg)

        try:
            for phase in phases:
                if self._is_cancelled(run_id):
                    store.update_run(run_id, status="cancelled")
                    say("engine", "run cancelled", "warn")
                    return
                store.update_run(run_id, phase=phase)
                say(phase, f"phase started: {phase}")
                if phase == "finalize":
                    if kind in ("fix", "env_fix"):
                        self._finalize_fix(ctx)
                        return
                    if kind == "env_audit":
                        self._finalize_env(ctx)
                        return
                    # Product vocabulary: stage_verify copies the connector's
                    # labels into ctx; defaults keep label-less stage sets
                    # (tests, partial pipelines) on jira prose.
                    summary = build_run_summary(
                        ctx["project_results"],
                        ctx["config_result"],
                        ctx["blind_spots"],
                        item_label=ctx.get("item_label", "issue"),
                        container_label=ctx.get("container_label", "project"))
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
                    # Precompute the derived fidelity ONCE (this run can carry
                    # 400k+ findings); the analysis page then serves a small
                    # cached blob instead of re-aggregating on every view. Guard
                    # so a derivation hiccup never fails an otherwise-good run —
                    # the summary route falls back to computing it live.
                    try:
                        stats["derived_fidelity"] = compute_run_fidelity(
                            store, run_id, stats)
                    except Exception as fid_exc:  # noqa: BLE001
                        say("engine", f"fidelity precompute skipped: {fid_exc}",
                            "warn")
                    # Spec §6 phase 7: bound the privilege window to <=1 run by
                    # auto-de-granting any still-active elevation across all of
                    # this migration's runs. Do this BEFORE marking the run done:
                    # the privilege window is then closed by the time the run
                    # reports complete, and a crash in the window leaves the run
                    # NOT-done so the stale-run sweep can recover it (no silent
                    # leak). Guarded (best-effort) so a cleanup error never
                    # downgrades a successful audit. ctx["src"]/["tgt"] come from
                    # stage_verify; None (verify failed) makes the helper skip.
                    try:
                        self.elevation_undo(ctx.get("src"), ctx.get("tgt"),
                                            migration_id, run_id)
                    except Exception as undo_exc:  # noqa: BLE001
                        say("engine", f"elevation cleanup failed: {undo_exc}",
                            "error")
                    store.update_run(run_id, status="done",
                                     verdict=summary["verdict"], stats=stats)
                    say(phase, f"run complete: verdict={summary['verdict']}")
                    return
                fn = stages.get(phase)
                if fn is not None:
                    fn(ctx)
                say(phase, f"phase done: {phase}")
        except Exception as exc:  # noqa: BLE001 — any stage failure must land in the run record, not a dead thread
            say("engine", f"run failed: {exc}", "error")
            say("engine", traceback.format_exc()[-1500:], "error")
            store.update_run(run_id, status="failed")
            # Best-effort cleanup on the failure path too: a failed run must not
            # leave the migration's elevation grants behind. Guarded so a cleanup
            # error never masks the original failure.
            try:
                self.elevation_undo(ctx.get("src"), ctx.get("tgt"),
                                    migration_id, run_id)
            except Exception as undo_exc:  # noqa: BLE001
                say("engine", f"elevation cleanup failed: {undo_exc}", "error")

    # ----------------------------------------------------------- fix finalize
    def _finalize_fix(self, ctx):
        store, run_id = ctx["store"], ctx["run_id"]
        log = ctx.get("fix_log", [])
        # When apply streamed each record to the store as it fired (the durable
        # write-through path, review Bug 4), the rows are already persisted —
        # re-inserting here would double them. The in-memory log is still used
        # below for the verdict/stats. Otherwise (legacy) persist it now.
        if log and not ctx.get("fix_log_streamed"):
            store.insert_fix_actions(run_id, log)
        closure = ctx.get("closure",
                          {"closed": 0, "still_open": 0, "unchanged": 0,
                           "detail": []})
        failed = sum(1 for a in log if not a.get("ok"))
        fix_skipped = ctx.get("fix_skipped", 0)
        # I10: when no actions were applied and all selected findings were
        # skipped (no fix_payload captured at audit time), the run is not clean
        # — nothing was attempted. Use a distinct NOTHING_APPLIED verdict with a
        # headline directing the user to re-run the audit to capture fix data.
        if not log and fix_skipped > 0:
            verdict = "NOTHING_APPLIED"
            stats = {"closed": 0, "still_open": 0, "unchanged": 0,
                     "actions": 0, "failed": 0,
                     "headlines": [
                         "Nothing was applied: all selected findings lacked fix "
                         "data captured at audit time. Re-run the audit to "
                         "capture fix data, then try again."]}
        else:
            # Verdict ladder (product decision): everything closed and no action
            # failed is the only clean outcome. Any failed action is FIX_FAILED
            # unless every finding closed anyway — a partial close must not mask
            # a failed action. With no failures, a non-zero close is
            # FIXED_PARTIAL.
            if closure["still_open"] == 0 and failed == 0:
                verdict = "FIXED_CLEAN"
            elif failed > 0:
                verdict = "FIX_FAILED"
            elif closure["closed"] > 0:
                verdict = "FIXED_PARTIAL"
            else:
                verdict = "FIX_FAILED"
            stats = {"closed": closure["closed"],
                     "still_open": closure["still_open"],
                     "unchanged": closure["unchanged"], "actions": len(log),
                     "failed": failed,
                     "headlines": [
                         f"{closure['closed']} finding(s) closed, "
                         f"{closure['still_open']} still open, "
                         f"{failed} action(s) failed."]}
        # Mirror the audit finalize path (spec §6 phase 7): bound the privilege
        # window by de-revoking any elevation a fix run held (it may run its own
        # verify/apply under elevation), BEFORE marking the run done so the
        # window is closed by the time the run reports complete and a crash
        # leaves it recoverable. Guarded (best-effort). ctx["src"]/["tgt"] come
        # from stage_verify; None (verify failed) makes the helper skip.
        try:
            self.elevation_undo(ctx.get("src"), ctx.get("tgt"),
                                ctx["migration_id"], run_id)
        except Exception as undo_exc:  # noqa: BLE001
            store.add_event(run_id, "finalize", "error",
                            f"elevation cleanup failed: {undo_exc}")
        store.update_run(run_id, status="done", verdict=verdict, stats=stats)
        store.add_event(run_id, "finalize", "info",
                        f"fix run complete: {verdict}")

    # ----------------------------------------------------------- env finalize
    def _finalize_env(self, ctx):
        from auditor.envaudit.report import build_env_summary
        store, run_id = ctx["store"], ctx["run_id"]
        findings = ctx.get("env_findings", [])
        ai = ctx.get("ai", {"skipped": True})
        summary = build_env_summary(findings, ai)
        if findings:
            # A1: fold severity into detail so it survives the findings_config
            # round-trip (severity is a sibling of detail in the finding dict but
            # insert_findings_config only serialises the detail sub-dict).
            for f in findings:
                sev = f.get("severity")
                if sev is not None:
                    f.setdefault("detail", {})["severity"] = sev
            store.insert_findings_config(run_id, findings)
        stats = dict(summary["stats"])
        stats["headlines"] = summary["headlines"]
        stats["ai"] = ai
        store.update_run(run_id, status="done", verdict=summary["verdict"],
                         stats=stats)
        store.add_event(run_id, "finalize", "info",
                        f"env audit complete: {summary['verdict']}")
