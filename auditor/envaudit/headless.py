"""Headless environment audit.

Runs the SAME env-audit pipeline the web app does (scope -> gather -> checks ->
deterministic verdict) against a live client, but with no web server, no DB, and
no AI — so it can run in CI or on a cron and gate on the result. Returns a plain
dict; the caller serializes it and maps the verdict to an exit code.

The AI assessment is intentionally omitted: it is non-deterministic and optional,
and a gate must be reproducible. The deterministic verdict + health score stand
on their own (they always have — the AI number is advisory in the web app too).
"""
from __future__ import annotations

from .confluence_checks import run_checks_confluence
from .confluence_gather import gather_confluence
from .checks import run_checks
from .fixes import annotate_fixes
from .gather import gather_config
from .report import build_env_summary

# Verdict ordering, worst-first index, for exit-code gating.
VERDICT_RANK = {"HEALTHY": 0, "HEALTHY_WITH_NOTES": 1,
                "NEEDS_ATTENTION": 2, "CRITICAL": 3}


def run_env_audit(client, connector, progress=None) -> dict:
    """Audit one live environment end to end. `client` is a built product
    client; `connector` is the resolved Connector (product dispatch). Raises on
    a connection/enumeration failure (the caller turns that into a non-zero
    exit). Returns a JSON-serializable result dict."""
    say = progress or (lambda m: None)

    rows, err = connector.list_containers(client)
    if err:
        raise RuntimeError(
            f"{connector.container_label} enumeration failed: {err}")
    selected = [r["key"] for r in rows if r.get("key")]
    say(f"scope: {len(selected)} {connector.container_label}(s)")

    if connector.product == "confluence":
        snapshot = gather_confluence(client, selected, progress=say)
        findings = run_checks_confluence(snapshot)
    else:
        snapshot = gather_config(client, selected, progress=say)
        findings = run_checks(snapshot)

    site_url = getattr(getattr(client, "conn", None), "site_url", None)
    annotate_fixes(findings, deployment=snapshot.get("deployment"),
                   site_url=site_url)
    say(f"checks: {len(findings)} finding(s)")

    summary = build_env_summary(findings, {})   # {} = no AI (deterministic)
    stats = summary["stats"]
    return {
        "product": connector.product,
        "deployment": snapshot.get("deployment"),
        "verdict": summary["verdict"],
        "health_score": stats["health_score"],
        "grade": stats["grade"],
        "finding_total": stats["findings"],
        "severity_counts": {"high": stats["high"], "medium": stats["medium"],
                            "low": stats["low"]},
        "capability_gaps": stats["capability_gaps"],
        "headlines": summary["headlines"],
        "findings": findings,
    }
