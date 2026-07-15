"""Env-fix run stages: consent-gated apply of app-tier env findings against
the audited source environment.

Mirrors fix_stages.py structure (fn(ctx)); the engine owns persistence and
_finalize_fix for the verdict.  The single client is the SOURCE (the audited
env side) — writes go to the same live instance the env audit read from.

Safety invariants (spec R8):
  - expected_api_base guard: apply_env_fixes raises before any write when the
    client's api_base doesn't match the audited connection's site_url.
  - Tier re-derivation inside apply_env_fixes (I4): server re-derives tier
    from the stored kind via _FIXES; human/unfixable are never applied even
    if somehow selected.
  - Idempotent: already-absent objects are logged as no-op and counted closed.
  - Closure proven by re-reading the object lists (apply_env_fixes returns
    closed/still_open counts used directly in the ctx closure block).
"""
from __future__ import annotations

import json

from auditor.envaudit.apply import apply_env_fixes
from auditor.envaudit.confluence_apply import apply_confluence_fixes
from auditor.envaudit.fixes import _FIXES
from .stages import build_clients, _say

# The exhaustive set of app-tier kinds that env_fix applies (v1 scope), keyed
# by product. Replicated here for the verify / filter step; the per-product
# apply function is the authoritative guard for the actual apply.
_APPLYABLE_KINDS = frozenset({
    # jira
    "scheme_unused",
    "unused_issue_type_scheme",
    "unused_issue_type_screen_scheme",
    "empty_group",
    # jira — expanded cleanup deletes (built-in protection + per-kind TOCTOU
    # re-verify, incl. the custom-field value-check, live in apply.py).
    "empty_screen",
    "screen_not_in_scheme",
    "workflow_unreferenced",
    "unused_custom_field",
    "empty_project",
    # status_not_in_workflow: the status is in NO workflow, so the delete is
    # clean. Built-in status protection + a live workflow re-read + an
    # issues-in-status value-check live in apply.py. (unreachable_status /
    # dead_end_status stay HUMAN — see fixes.py.)
    "status_not_in_workflow",
    # confluence
    "empty_space",
    "confluence_empty_group",
})


def _source_env_findings(store, source_run_id: int) -> list[dict]:
    """Re-read env findings from the source audit run (I4)."""
    out = []
    for area in store.config_areas(source_run_id):
        out.extend(store.query_config(source_run_id, area))
    return out


def env_fix_verify(ctx):
    store = ctx["store"]
    # An env_fix run writes to the SOURCE (the audited environment) — not a
    # target. require_both=False builds just the source client.
    src, _tgt, connector = build_clients(store, ctx["migration_id"],
                                         require_both=False)
    if src is None:
        raise RuntimeError("env_fix run has no source connection")
    ctx["src"], ctx["connector"] = src, connector
    me = connector.verify(src)
    row = store.get_connection(ctx["migration_id"], "source")
    store.mark_connection_verified(row["id"], me.get("email") or "")
    # Capture the expected api_base for the identity guard in apply.
    ctx["expected_api_base"] = src.api_base
    _say(ctx, "verify",
         f"source authenticated as {me.get('display_name') or '?'}; "
         f"writes will target {src.api_base} only")


def env_fix_apply(ctx):
    store = ctx["store"]
    source_run_id = store.get_run(ctx["run_id"]).get("source_run_id")
    if not source_run_id:
        raise RuntimeError("env_fix run has no source_run_id")

    # Reconstruct findings from the source env audit run (I4).
    all_findings = _source_env_findings(store, source_run_id)

    # Extract the finding_refs the user selected (passed in params).
    # Format: "{kind}:{name}" strings. The server re-validates tier.
    selected_refs = ctx["params"].get("finding_refs", [])
    if isinstance(selected_refs, str):
        selected_refs = [r.strip() for r in selected_refs.split(",")
                         if r.strip()]

    # Build a lookup {(kind, name): finding} from stored findings.
    stored_lookup: dict[tuple[str, str], dict] = {}
    for f in all_findings:
        key = (f.get("kind") or "", f.get("name") or "")
        stored_lookup[key] = f

    # Filter to: (a) only selected refs, (b) only app-tier kinds.
    # The server re-derives tier from the kind (I4) — never trusts client.
    selected_findings = []
    fix_skipped = 0
    for ref in selected_refs:
        # Ref format: "kind:name"
        if ":" not in ref:
            fix_skipped += 1
            _say(ctx, "apply", f"skipped malformed ref {ref!r}", "warn")
            continue
        kind, name = ref.split(":", 1)
        fix_entry = _FIXES.get(kind)
        if fix_entry is None or fix_entry.get("tier") != "app":
            fix_skipped += 1
            _say(ctx, "apply",
                 f"refused {ref!r}: tier is not app — skipping to protect "
                 f"live environment", "warn")
            continue
        f = stored_lookup.get((kind, name))
        if f is None:
            fix_skipped += 1
            _say(ctx, "apply",
                 f"skipped {ref!r}: no stored finding with this kind+name",
                 "warn")
            continue
        selected_findings.append(f)

    log = []
    # Durable write-through: persist each action the instant it fires, so a hard
    # crash mid-apply can't erase the record of DELETEs already sent to the live
    # instance (review Bug 4). _finalize_fix sees fix_log_streamed and skips the
    # bulk re-insert. dry_run previews stream their WOULD-* rows the same way.
    run_id = ctx["run_id"]
    sink = lambda r: store.append_fix_action(run_id, r)  # noqa: E731
    if selected_findings:
        # Product dispatch: the env_fix run writes to the SOURCE (the audited
        # instance). ctx["src"] is already the connector-built client for the
        # migration's product (a ConfluenceClient for a Confluence env audit, a
        # JiraClient for a Jira one), and expected_api_base is that audited
        # connection's api_base — so the identity guard inside each apply
        # function checks the writes flow only to the audited instance.
        connector = ctx.get("connector")
        product = connector.product if connector is not None else "jira"
        # dry_run preview: every guard runs, no write is issued. Server-controlled
        # (set by the route), never client-tamperable after the run starts.
        dry_run = bool(ctx["params"].get("dry_run"))
        if product == "confluence":
            closed, still_open = apply_confluence_fixes(
                ctx["src"], selected_findings, log.append,
                expected_api_base=ctx.get("expected_api_base"), dry_run=dry_run,
                record_sink=sink)
        else:
            closed, still_open = apply_env_fixes(
                ctx["src"], selected_findings, log.append,
                expected_api_base=ctx.get("expected_api_base"), dry_run=dry_run,
                record_sink=sink)
    else:
        closed, still_open = 0, 0

    ctx["fix_log"] = log
    # Records were streamed durably above -> finalize must not re-insert them.
    ctx["fix_log_streamed"] = True
    ctx["fix_skipped"] = fix_skipped
    ctx["closure"] = {
        "closed": closed, "still_open": still_open, "unchanged": 0,
        "detail": [],
    }
    ok_count = sum(1 for r in log if r.get("ok"))
    if bool(ctx["params"].get("dry_run")):
        _say(ctx, "apply",
             f"PREVIEW (no changes written): {closed} finding(s) would be "
             f"fixed, {still_open} would be skipped")
    else:
        _say(ctx, "apply",
             f"{ok_count}/{len(log)} action(s) succeeded; "
             f"{closed} closed, {still_open} still open")


def env_fix_reaudit(ctx):
    # The closure (closed/still_open) is computed inside apply_env_fixes by
    # re-reading the object lists post-delete. It is already set on ctx by
    # env_fix_apply. This stage is a no-op: it exists so the engine phase
    # list matches the fix run contract (verify→apply→reaudit→finalize).
    closure = ctx.get("closure", {})
    _say(ctx, "reaudit",
         f"closure confirmed: {closure.get('closed', 0)} closed, "
         f"{closure.get('still_open', 0)} still open")


def build_env_fix_stages() -> dict:
    return {"verify": env_fix_verify, "apply": env_fix_apply,
            "reaudit": env_fix_reaudit}
