"""Fix-run stages: read the source audit run's findings + workspace, write to
the TARGET only, prove closure. Mirrors stages.py structure; the only client
that ever performs a write is the target."""
from __future__ import annotations

import os

from auditor.remediation.apply import apply_plan
from auditor.remediation.plan import build_plan
from auditor.remediation.reaudit import compute_closure
from auditor.remediation.registry import get_fix
from .stages import build_clients, _say


def _source_findings(store, source_run_id):
    out = []
    for area in store.config_areas(source_run_id):
        out.extend(store.query_config(source_run_id, area))
    return out


def fix_verify(ctx):
    store = ctx["store"]
    # A fix run writes to the target only, so it does not require the source
    # connection. require_both=False keeps it runnable even if the source row
    # is gone; the target is mandatory and verified below.
    src, tgt, connector = build_clients(store, ctx["migration_id"],
                                        require_both=False)
    if tgt is None:
        raise RuntimeError("fix run has no target connection")
    ctx["src"], ctx["tgt"], ctx["connector"] = src, tgt, connector
    me = connector.verify(tgt)    # raises ClientError loudly on auth failure
    row = store.get_connection(ctx["migration_id"], "target")
    store.mark_connection_verified(row["id"], me.get("email") or "")
    _say(ctx, "verify", "target authenticated; writes will target the target "
                        "side only")


def fix_apply(ctx):
    store = ctx["store"]
    # The run row is the authoritative source_run_id (set at create_run); the
    # engine ctx never carries it, so read the DB directly.
    source_run_id = store.get_run(ctx["run_id"]).get("source_run_id")
    if not source_run_id:
        raise RuntimeError("fix run has no source_run_id")
    findings = _source_findings(store, source_run_id)

    # I3: enforce requires_confirm server-side regardless of the HTTP layer.
    # Any fix that requires explicit consent must be dropped from the selected
    # set when confirm_workflow is falsy, and each refusal is logged.
    confirm_ok = bool(ctx["params"].get("confirm_workflow"))
    selected_ids = ctx["params"].get("fix_ids", [])
    refused = []
    if not confirm_ok:
        filtered = []
        for fid in selected_ids:
            try:
                fx = get_fix(fid)
            except KeyError:
                filtered.append(fid)
                continue
            if fx.requires_confirm:
                refused.append(fid)
                _say(ctx, "apply",
                     f"refused {fid}: requires_confirm but confirm_workflow "
                     f"not set — skipping to protect live workflow state",
                     "warn")
            else:
                filtered.append(fid)
        selected_ids = filtered

    plan = build_plan(findings, selected_ids,
                      product=ctx["connector"].product)
    src_ws = os.path.join(os.path.dirname(ctx["workspace"]),
                          str(source_run_id))
    # populate reads the SOURCE audit run's captured values
    log = []
    # Pass expected_api_base so apply_plan's runtime identity guard is active:
    # a mis-wired caller (wrong client object) raises before any HTTP write.
    apply_plan(ctx["tgt"], plan, log.append, workspace=src_ws,
               expected_api_base=ctx["tgt"].api_base)
    ctx["fix_log"] = log
    # fix_skipped includes both plan-level skips (no payload) and server-side
    # confirm refusals so the NOTHING_APPLIED verdict path is accurate.
    ctx["fix_skipped"] = len(plan.skipped) + len(refused)
    ctx["touched_areas"] = {a.area for a in plan.actions}
    ctx["_source_findings"] = findings
    for s in plan.skipped:
        _say(ctx, "apply", f"skipped {s['finding']}: {s['reason']}", "warn")
    _say(ctx, "apply", f"{sum(1 for a in log if a['ok'])}/{len(log)} "
                       f"action(s) succeeded")


def fix_reaudit(ctx):
    findings = ctx.get("_source_findings", [])
    ctx["closure"] = compute_closure(ctx["tgt"], findings,
                                     ctx.get("touched_areas", set()))
    _say(ctx, "reaudit", f"closure: {ctx['closure']['closed']} closed, "
                         f"{ctx['closure']['still_open']} still open")


def build_fix_stages() -> dict:
    return {"verify": fix_verify, "apply": fix_apply, "reaudit": fix_reaudit}
