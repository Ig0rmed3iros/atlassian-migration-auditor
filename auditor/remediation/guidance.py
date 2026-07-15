"""Detect-and-guide artifacts for defects no API can fix faithfully.

Each returns a dict the UI renders read-only and the operator can copy:
summary, why_unfixable, missing[], selection_query (JQL/CQL), next_step."""
from __future__ import annotations

from collections import defaultdict


def _missing_issues(findings):
    by_proj = defaultdict(list)
    for f in findings:
        if f.get("kind") == "missing_in_tgt" and f.get("src_key"):
            by_proj[f.get("project")].append(f["src_key"])
    if not by_proj:
        return None
    keys = [k for ks in by_proj.values() for k in ks]
    clauses = " OR ".join(
        f'(project = "{p}" AND key in ({", ".join(ks)}))'
        for p, ks in by_proj.items())
    return {
        "summary": f"{len(keys)} issue(s) are missing below the cutover line "
                   f"and cannot be faithfully recreated via REST.",
        "why_unfixable": "An issue's created date, reporter, comment "
                         "authorship and history are immutable. A POSTed issue "
                         "would be dated today under your account — a forgery, "
                         "not a fix.",
        "missing": keys,
        "selection_query": clauses,
        "next_step": "Re-migrate exactly these keys with JCMA/CCMA, then "
                     "re-audit to confirm the holes closed.",
        "count": len(keys)}


def _user_gap(findings):
    users = [f.get("detail", {}) for f in findings if f.get("kind") == "user_gap"]
    if not users:
        return None
    return {
        "summary": f"{len(users)} user(s) referenced by source issues do not "
                   f"resolve on the target.",
        "why_unfixable": "Cloud users live on a separate identity plane; the "
                         "Jira API cannot create them, and an invited account "
                         "gets a new id that cannot be retro-attached to "
                         "existing issues' authorship.",
        "missing": [f"{u.get('display_name')} ({u.get('account_id')})"
                    for u in users],
        "selection_query": "",
        "next_step": "Invite these users (org admin), then re-migrate so the "
                     "migration tool maps authorship. Auto-invite is a "
                     "documented fast-follow.",
        "count": len(users)}


def _workflow_wire(findings):
    """C3/I4: statuses that were created but must be wired into workflow(s) manually.

    Live-workflow editing is Tier-2 (spec R8): it edits live workflow behaviour
    and is not automated. The guidance lists the status names so the operator can
    open each affected workflow in the Jira workflow editor and add the status and
    a transition by hand."""
    names = [f.get("name") for f in findings
             if f.get("kind") == "missing_in_tgt" and f.get("name")]
    if not names:
        return None
    return {
        "summary": f"{len(names)} status(es) were created on the target but are "
                   f"not yet reachable — each must be wired into its workflow(s) "
                   f"manually.",
        "why_unfixable": "Workflow editing modifies live transition behaviour. "
                         "Automated wiring risks corrupting running workflows; "
                         "this is a deliberate Tier-2 boundary (spec R8).",
        "missing": names,
        "selection_query": "",
        "next_step": "In Jira Administration > Workflows, open each affected "
                     "workflow, add the status and at least one incoming "
                     "transition, then publish the draft. Re-audit to confirm "
                     "issues can reach the status.",
        "count": len(names)}


def _key_collision(findings):
    """I6: issues whose identity metadata disagrees between source and target.

    Key collisions mean the same issue key exists on both sides with different
    content fingerprints — a re-migration or manual data reconciliation is
    required. This is Tier-2 (spec R8) because overwriting target issue data is
    destructive and irreversible."""
    collisions = [f for f in findings if f.get("kind") == "key_collision"]
    if not collisions:
        return None
    keys = [f.get("src_key") or f.get("tgt_key") for f in collisions if
            f.get("src_key") or f.get("tgt_key")]
    by_proj = defaultdict(list)
    for f in collisions:
        p = f.get("project") or ""
        k = f.get("src_key") or f.get("tgt_key")
        if k:
            by_proj[p].append(k)
    clauses = " OR ".join(
        f'(project = "{p}" AND key in ({", ".join(ks)}))'
        for p, ks in by_proj.items() if p)
    return {
        "summary": f"{len(collisions)} issue key(s) exist on both source and "
                   f"target with mismatched content.",
        "why_unfixable": "Overwriting target issue content is destructive and "
                         "irreversible. Resolving a key collision requires "
                         "manual review to decide whether to accept the target "
                         "version, re-migrate from source, or merge changes.",
        "missing": keys,
        "selection_query": clauses,
        "next_step": "Review each colliding key. If the target copy is "
                     "authoritative, accept it and mark the finding resolved. "
                     "If the source copy should win, re-migrate only those keys "
                     "with JCMA/CCMA and re-audit.",
        "count": len(collisions)}


def _workflow_structure_mismatch(findings):
    """I6: workflows whose transition/status topology differs between source and target.

    Structural workflow mismatches are Tier-2 (spec R8): reconciling them
    requires editing live workflows, which risks corrupting running transitions."""
    mismatches = [f for f in findings if f.get("kind") == "structure_mismatch"]
    if not mismatches:
        return None
    names = [f.get("name") for f in mismatches if f.get("name")]
    return {
        "summary": f"{len(mismatches)} workflow(s) have structural differences "
                   f"(missing statuses or transitions) between source and target.",
        "why_unfixable": "Workflow topology changes edit live transition "
                         "behaviour. Automated reconciliation risks breaking "
                         "running workflows; this is Tier-2 (spec R8).",
        "missing": names,
        "selection_query": "",
        "next_step": "In Jira Administration > Workflows, open each affected "
                     "workflow and manually add the missing statuses and "
                     "transitions to match the source topology. Publish the "
                     "draft and re-audit to confirm the mismatch is closed.",
        "count": len(mismatches)}


_GUIDES = {
    "missing_issues": _missing_issues,
    "user_gap": _user_gap,
    "workflow_wire": _workflow_wire,
    "key_collision": _key_collision,
    "workflow_structure_mismatch": _workflow_structure_mismatch,
}


def guidance_for(kind: str, findings: list) -> dict | None:
    fn = _GUIDES.get(kind)
    return fn(findings) if fn else None
