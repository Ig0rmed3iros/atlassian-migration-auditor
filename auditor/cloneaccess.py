"""User-access cloning engine: resolve → gather → plan → apply.

Additive only. Clones a Jira Cloud user's group memberships and direct
project-role memberships onto another account. UI/CLI agnostic — callers pass
a JiraClient and receive plain dict reports.
"""
from __future__ import annotations

import os
import re

from auditor.envaudit._pool import map_results

# accountId shapes: Cloud "<digits>:<uuid>" and legacy 24-hex.
# The 24-hex match is a heuristic — a bare 24-hex string with no domain is
# treated as an accountId without an HTTP check.
ACCOUNT_ID_RE = re.compile(r"^(\d+:[0-9a-fA-F-]{8,}|[0-9a-fA-F]{24})$")


class CloneError(Exception):
    """A fatal clone-run error (e.g. cannot enumerate projects)."""


def looks_like_account_id(value: str) -> bool:
    return bool(ACCOUNT_ID_RE.match((value or "").strip()))


def resolve_identity(client, value: str) -> dict:
    """Resolve a raw input (accountId or email) to an accountId.

    accountId -> passthrough (no HTTP). email -> user-search for exactly one
    active atlassian account. Returns status resolved | unresolved | ambiguous;
    never guesses.
    """
    v = (value or "").strip()
    out = {"input": v, "account_id": None, "status": "unresolved", "reason": None}
    if not v:
        out["reason"] = "empty value"
        return out
    if looks_like_account_id(v):
        out.update(account_id=v, status="resolved")
        return out
    users, err = client.search_users(v)
    if err:
        out["reason"] = f"lookup failed: {err}"
        return out
    hits = [u for u in users
            if u.get("accountType") == "atlassian" and u.get("active")]
    # Prefer an exact email match when the API exposes emails.
    exact = [u for u in hits
             if (u.get("emailAddress") or "").lower() == v.lower()]
    pool = exact or hits
    if len(pool) == 1:
        out.update(account_id=pool[0]["accountId"], status="resolved")
    elif len(pool) > 1:
        out["status"] = "ambiguous"
        out["reason"] = f"{len(pool)} accounts match '{v}' — use an accountId"
    else:
        out["reason"] = f"no active account matches '{v}'"
    return out


def build_role_index(client, progress=None) -> dict:
    """Scan every project's role actors once and index direct USER actors:
    {account_id: [{"project","role_id","role"}]}. Group actors are excluded
    (group membership is cloned separately and already grants those roles).
    Concurrent (MA_GATHER_WORKERS); the merge is deterministic (main thread,
    sorted), so output is identical regardless of worker count.
    """
    projects, err = client.all_projects()
    if err:
        raise CloneError(f"could not list projects: {err}")
    keys = [p.get("key") for p in projects if p.get("key")]
    if progress:
        progress(f"scanning {len(keys)} projects for direct role grants")

    def _project_actor_rows(key):
        rmap, e = client.project_role_map(key)
        if e:
            return []
        rows = []
        for role_name, role_id in rmap.items():
            actors, e2 = client.project_role_actors(key, role_id)
            if e2:
                continue
            for a in actors:
                if a.get("type") == "atlassian-user-role-actor":
                    aid = (a.get("actorUser") or {}).get("accountId")
                    if aid:
                        rows.append((aid, {"project": key, "role_id": role_id,
                                           "role": role_name}))
        return rows

    results = map_results(keys, _project_actor_rows)
    index: dict = {}
    # Merge in project order, then sort each list, for determinism.
    for res in results:
        if isinstance(res, Exception):
            continue
        for aid, row in res:
            index.setdefault(aid, []).append(row)
    for aid in index:
        index[aid].sort(key=lambda r: (r["project"], r["role"]))
    return index


def _role_key(r: dict) -> str:
    return f"{r['project']}/{r['role']}"


class CloneAborted(CloneError):
    """Circuit breaker tripped: too many server-side write failures."""


def breaker_threshold() -> int:
    raw = os.environ.get("MA_BREAKER_THRESHOLD")
    if raw is None:
        return 5
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 5
    return n if n >= 0 else 5


def _server_side(status: int) -> bool:
    return status < 0 or status == 429 or 500 <= status < 600


def plan_pair(client, main_id: str, clone_id: str, role_index=None) -> dict:
    """Additive diff for one pair: what groups/roles the main has that the
    clone lacks. role_index None => roles not scanned (groups-only preview).
    """
    main_groups, _ = client.user_groups(main_id)
    clone_groups, _ = client.user_groups(clone_id)
    clone_gids = {g.get("groupId") for g in clone_groups}
    groups_add, groups_already = [], []
    for g in main_groups:
        (groups_already.append(g.get("name")) if g.get("groupId") in clone_gids
         else groups_add.append({"name": g.get("name"), "groupId": g.get("groupId")}))

    roles_add, roles_already = [], []
    scanned = role_index is not None
    if scanned:
        clone_roles = {_role_key(r) for r in role_index.get(clone_id, [])}
        for r in role_index.get(main_id, []):
            (roles_already.append(_role_key(r)) if _role_key(r) in clone_roles
             else roles_add.append(r))
    return {"groups_add": groups_add, "groups_already": sorted(groups_already),
            "roles_add": roles_add, "roles_already": sorted(roles_already),
            "roles_scanned": scanned}


def run_clone(client, pairs, *, dry_run: bool, scan_roles: bool,
              progress=None) -> dict:
    """Resolve, plan, and (unless dry_run) apply each pair additively.

    Groups are always planned. Roles are planned+applied only when scan_roles
    (a one-time instance scan precedes the per-pair work). Idempotent; a
    breaker aborts after MA_BREAKER_THRESHOLD consecutive server-side write
    failures, returning the partial report via CloneAborted.partial.
    """
    role_index = None
    if scan_roles:
        role_index = build_role_index(client, progress)

    thr = breaker_threshold()
    consec = 0
    report = {"dry_run": dry_run, "scanned_roles": scan_roles, "pairs": [],
              "summary": {"pairs": 0, "blocked": 0, "groups_added": 0,
                          "roles_added": 0, "failed": 0, "partial": 0}}

    def _do_write(call):
        nonlocal consec
        st, d = call()
        ok = 200 <= st < 300
        if ok:
            consec = 0
        elif _server_side(st):
            consec += 1
            if thr and consec >= thr:
                raise CloneAborted(
                    f"circuit breaker: {consec} consecutive server-side "
                    f"failures (>= {thr})")
        return ok, st, d

    for main_raw, clone_raw in pairs:
        if progress:
            progress(f"{main_raw} -> {clone_raw}")
        rec = {"main": main_raw, "clone": clone_raw, "main_id": None,
               "clone_id": None, "status": "ok", "reason": None,
               "groups": {"added": [], "already": [], "failed": []},
               "roles": {"added": [], "already": [], "failed": [],
                         "scanned": scan_roles}}
        m = resolve_identity(client, main_raw)
        c = resolve_identity(client, clone_raw)
        rec["main_id"], rec["clone_id"] = m["account_id"], c["account_id"]
        if m["status"] != "resolved" or c["status"] != "resolved":
            rec["status"] = "blocked"
            rec["reason"] = "; ".join(
                x["reason"] for x in (m, c) if x["status"] != "resolved")
            report["pairs"].append(rec)
            continue
        if m["account_id"] == c["account_id"]:
            rec["status"] = "noop"
            rec["reason"] = "main and clone are the same account"
            report["pairs"].append(rec)
            continue

        plan = plan_pair(client, m["account_id"], c["account_id"], role_index)
        rec["groups"]["already"] = plan["groups_already"]
        rec["roles"]["already"] = plan["roles_already"]
        try:
            for g in plan["groups_add"]:
                if dry_run:
                    rec["groups"]["added"].append(g["name"])
                    continue
                ok, st, d = _do_write(
                    lambda g=g: client.add_user_to_group(g["groupId"],
                                                         c["account_id"]))
                if ok:
                    rec["groups"]["added"].append(g["name"])
                else:
                    err = d.get("_error") if isinstance(d, dict) else str(d)
                    # An "already a member" 4xx is success, not a failure.
                    if isinstance(d, dict) and "already" in (err or "").lower():
                        rec["groups"]["already"].append(g["name"])
                    else:
                        rec["status"] = "partial"
                        rec["groups"]["failed"].append({"group": g["name"],
                                                        "error": f"{st} {err}"})
            for r in plan["roles_add"]:
                if dry_run:
                    rec["roles"]["added"].append(_role_key(r))
                    continue
                ok, st, d = _do_write(
                    lambda r=r: client.add_user_to_project_role(
                        r["project"], r["role_id"], c["account_id"]))
                if ok:
                    rec["roles"]["added"].append(_role_key(r))
                else:
                    err = d.get("_error") if isinstance(d, dict) else str(d)
                    # An "already a member" 4xx is success, not a failure.
                    if isinstance(d, dict) and "already" in (err or "").lower():
                        rec["roles"]["already"].append(_role_key(r))
                    else:
                        rec["status"] = "partial"
                        rec["roles"]["failed"].append({"role": _role_key(r),
                                                       "error": f"{st} {err}"})
        except CloneAborted as exc:
            rec["reason"] = str(exc)
            report["pairs"].append(rec)
            report["summary"]["pairs"] = len(report["pairs"])
            _tally(report)
            exc.partial = report
            raise
        report["pairs"].append(rec)

    report["summary"]["pairs"] = len(report["pairs"])
    _tally(report)
    return report


def _tally(report: dict) -> None:
    s = report["summary"]
    s["groups_added"] = sum(len(p["groups"]["added"]) for p in report["pairs"])
    s["roles_added"] = sum(len(p["roles"]["added"]) for p in report["pairs"])
    s["failed"] = sum(len(p["groups"]["failed"]) + len(p["roles"]["failed"])
                      for p in report["pairs"])
    s["blocked"] = sum(1 for p in report["pairs"] if p["status"] == "blocked")
    s["partial"] = sum(1 for p in report["pairs"] if p["status"] == "partial")
