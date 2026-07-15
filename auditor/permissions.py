"""Permission blind-spot detection + consent-gated role elevation.

Encodes the reference-audit lesson: a permission-bound zero must never be read
as an empty project. Elevation is built/applied ONLY when the operator
confirms in the UI; every grant is logged so undo is deterministic.

Grant log rows: {"project_id","status","ok","added"[,"error"]}; undo only
removes added=True rows — pre-existing memberships are pre-checked and never
deleted.

detect_blind_spots rows: {"key","search_count","insight_count","blind_spot",
"indeterminate"} — indeterminate=True when approx_count errored but
insight_count is a positive int (caller can surface 'could not verify').
"""
from __future__ import annotations

from .client import JiraClient, escape_query_key


def detect_blind_spots(client: JiraClient, project_keys: list[str]) -> list[dict]:
    projects, _ = client.all_projects()
    insight = {}
    for p in projects:
        cnt = ((p.get("insight") or {}).get("totalIssueCount"))
        insight[p.get("key")] = cnt
    out = []
    for key in project_keys:
        sc = client.approx_count(f'project = "{escape_query_key(key)}"')
        search_count = sc if isinstance(sc, int) else None
        ins = insight.get(key)
        blind = (search_count is not None and ins is not None
                 and ins > 0 and search_count < ins * 0.5)
        indeterminate = (search_count is None and isinstance(ins, int) and ins > 0)
        out.append({"key": key, "search_count": search_count,
                    "insight_count": ins, "blind_spot": bool(blind),
                    "indeterminate": bool(indeterminate)})
    return out


def find_admin_role_id(client: JiraClient) -> int | None:
    st, roles = client.req("/rest/api/3/role")
    if st != 200 or not isinstance(roles, list):
        return None
    admin = {r.get("name", ""): r.get("id") for r in roles
             if "admin" in r.get("name", "").lower()}
    return (admin.get("Administrators") or admin.get("Administrator")
            or next(iter(admin.values()), None))


def apply_elevation(client: JiraClient, project_ids: list[str], role_id: int,
                    account_id: str) -> list[dict]:
    log = []
    for pid in project_ids:
        # Pre-check: if the account is already a member, record it and skip the
        # POST so undo never removes a membership we didn't add.
        st, cur = client.req(f"/rest/api/3/project/{pid}/role/{role_id}")
        already = False
        if st == 200 and isinstance(cur, dict):
            for actor in cur.get("actors", []):
                if (actor.get("actorUser") or {}).get("accountId") == account_id:
                    already = True
                    break
        if already:
            log.append({"project_id": pid, "status": 200, "ok": True,
                        "added": False})
            continue
        # Non-200 pre-check → treat as absent and proceed with the grant.
        st, d = client.req(f"/rest/api/3/project/{pid}/role/{role_id}", "POST",
                           {"user": [account_id]})
        ok = st in (200, 204)
        row = {"project_id": pid, "status": st, "ok": ok, "added": ok}
        if not ok:
            row["error"] = (d or {}).get("_error")
        log.append(row)
    return log


def undo_elevation(client: JiraClient, grants: list[dict], role_id: int,
                   account_id: str) -> list[dict]:
    out = []
    for g in grants:
        if not g.get("added"):
            continue
        pid = g["project_id"]
        st, d = client.req(
            f"/rest/api/3/project/{pid}/role/{role_id}?user={account_id}",
            "DELETE")
        ok = st in (200, 204)
        row = {"project_id": pid, "status": st, "ok": ok}
        if not ok:
            row["error"] = (d or {}).get("_error")
        out.append(row)
    return out
