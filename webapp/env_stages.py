"""Environment-audit run stages: gather one environment's configuration, run
deterministic health checks, then an optional AI assessment. Read-only — an env
audit never writes to the source environment.

Mirrors fix_stages.py / stages.py structure: each stage is fn(ctx); the engine
owns persistence and the finalize (runs.py::_finalize_env). The single client is
built from the SOURCE connection (an env project configures one side only) via
build_clients(..., require_both=False)."""
from __future__ import annotations

import os

from auditor.envaudit.analysis import analyze, analyze_sectioned
from auditor.envaudit.checks import run_checks
from auditor.envaudit.confluence_checks import run_checks_confluence
from auditor.envaudit.confluence_gather import gather_confluence
from auditor.envaudit.fixes import annotate_fixes
from auditor.envaudit.gather import gather_config
from .ai_provider import ai_provider
from .stages import _say, build_clients


def stage_env_verify(ctx):
    store = ctx["store"]
    # An env project configures one side only (the source). require_both=False
    # builds just the source client; the target is absent by design.
    src, _tgt, connector = build_clients(store, ctx["migration_id"],
                                         require_both=False)
    if src is None:
        raise RuntimeError("environment audit has no source connection")
    ctx["src"], ctx["connector"] = src, connector
    me = connector.verify(src)    # raises ClientError loudly on auth failure
    row = store.get_connection(ctx["migration_id"], "source")
    store.mark_connection_verified(row["id"], me.get("email") or "")
    _say(ctx, "verify",
         f"source authenticated as {me.get('display_name') or '?'}")


def stage_env_scope(ctx):
    connector = ctx["connector"]
    rows, err = connector.list_containers(ctx["src"])
    if err:
        raise RuntimeError(
            f"{connector.container_label} enumeration failed: {err}")
    ctx["selected"] = [r["key"] for r in rows if r.get("key")]
    _say(ctx, "scope",
         f"{len(ctx['selected'])} {connector.container_label}(s) in environment")


def stage_env_gather(ctx):
    # Product dispatch: Confluence gathers spaces/permissions/content via
    # gather_confluence; Jira (and any other product) uses gather_config. Both
    # return the SAME outer shape {deployment, projects, areas}, so every
    # downstream stage stays product-agnostic.
    if ctx["connector"].product == "confluence":
        snapshot = gather_confluence(
            ctx["src"], ctx.get("selected", []),
            progress=lambda msg: _say(ctx, "gather", msg))
    else:
        snapshot = gather_config(
            ctx["src"], ctx.get("selected", []),
            progress=lambda msg: _say(ctx, "gather", msg))
    ctx["snapshot"] = snapshot
    _say(ctx, "gather",
         f"gathered {len(snapshot.get('areas') or {})} configuration area(s)")


def stage_env_checks(ctx):
    # Product dispatch: Confluence runs the spaces/permissions/content rules;
    # Jira (and any other product) runs the Jira config rules. annotate_fixes
    # then runs unchanged — the shared fix registry now covers both products'
    # kinds (kinds are unique across products).
    snapshot = ctx.get("snapshot") or {}
    if ctx["connector"].product == "confluence":
        findings = run_checks_confluence(snapshot)
    else:
        findings = run_checks(snapshot)
    # Pass the deployment + site_url so annotate_fixes can attach deployment-aware
    # admin deep-links. The snapshot owns the authoritative deployment; the live
    # client carries the site_url. Both are best-effort — a missing one just
    # means no links, never a failed audit.
    src = ctx.get("src")
    site_url = getattr(getattr(src, "conn", None), "site_url", None)
    annotate_fixes(findings, deployment=snapshot.get("deployment"),
                   site_url=site_url)
    ctx["env_findings"] = findings
    _say(ctx, "checks", f"{len(findings)} finding(s) from health checks")


def stage_env_analysis(ctx):
    # The AI assessment is OPTIONAL and must NEVER fail the audit run — the
    # deterministic findings + verdict stand on their own. ai_provider(store)
    # returns the configured provider (Anthropic OR an OpenAI-compatible
    # endpoint, per Settings), or None when unconfigured. But BUILDING the
    # provider can itself raise (e.g. the optional 'openai' package is not
    # installed, or a malformed config) — that must degrade to a skipped
    # assessment with a visible reason, not crash the run.
    try:
        provider = ai_provider(ctx["store"])
    except Exception as exc:
        ctx["ai"] = {"skipped": True, "error": str(exc), "health_score": None,
                     "grade": None, "summary": "", "themes": [],
                     "top_risks": [], "quick_wins": [], "model": None}
        _say(ctx, "analysis", f"AI analysis skipped: {exc}", "warn")
        return
    # Analysis mode: the default is the map-reduce "sectioned" pass (parallel
    # per-area analyses + a synthesis that re-correlates across areas — deeper,
    # at N× the provider cost). Set MA_AI_ANALYSIS_MODE=single to use the cheaper
    # one-shot pass. Both share the metadata-only boundary and return shape.
    # Product dispatch picks the Confluence vs Jira prompts inside analyze*.
    analyze_fn = (analyze if os.environ.get("MA_AI_ANALYSIS_MODE") == "single"
                  else analyze_sectioned)
    # Reasoning effort: default HIGH (the CLI provider forwards --effort; the API
    # providers map it to a thinking budget). Override with MA_AI_EFFORT
    # (low|medium|high|xhigh|max). The sectioned synthesis bumps one notch higher.
    effort = os.environ.get("MA_AI_EFFORT") or "high"
    # The AI call itself must NEVER fail the run (a malformed reply, a non-finite
    # token, a provider error). Any exception degrades to a skipped assessment so
    # the deterministic findings + verdict + score still finalize.
    try:
        ai = analyze_fn(ctx.get("snapshot") or {}, ctx.get("env_findings", []),
                        provider, product=ctx["connector"].product, effort=effort)
    except Exception as exc:  # noqa: BLE001 — AI is optional, never load-bearing
        ai = {"skipped": True, "error": str(exc), "health_score": None,
              "grade": None, "summary": "", "themes": [], "top_risks": [],
              "quick_wins": [], "ai_findings": [], "roadmap": [], "gaps": [],
              "model": None}
        _say(ctx, "analysis",
             f"AI analysis failed (degraded to skipped): {exc}", "warn")
    ctx["ai"] = ai
    if ai.get("skipped"):
        _say(ctx, "analysis", "AI analysis skipped (no AI provider configured)")
    elif ai.get("error"):
        _say(ctx, "analysis", f"AI analysis error: {ai['error']}", "warn")
    else:
        _say(ctx, "analysis",
             f"AI assessment: grade {ai.get('grade')} "
             f"(score {ai.get('health_score')})")


def build_env_stages() -> dict:
    return {"verify": stage_env_verify, "scope": stage_env_scope,
            "gather": stage_env_gather, "checks": stage_env_checks,
            "analysis": stage_env_analysis}
