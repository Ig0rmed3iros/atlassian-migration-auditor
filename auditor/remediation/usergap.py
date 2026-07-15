"""Detect users referenced by audited issues but unresolved on the target.

Detection only (Tier-2): Cloud users live on a separate identity plane and a
freshly-invited account gets a new id that cannot be retro-attached to
existing issues' authorship. The honest remedy is invite-then-re-migrate;
this module produces the precise list the guidance renders."""
from __future__ import annotations

import gzip
import json
import os

_USER_FIELDS = ("reporter", "assignee", "creator")


def _iter_issues(workspace: str, key: str):
    path = os.path.join(workspace, "src", f"{key}.core.jsonl.gz")
    if not os.path.exists(path):
        return
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i == 0:          # skip the _extract_format stamp
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def referenced_users(workspace: str, key: str) -> dict:
    """account_id (or DC name) -> displayName, over one space/project."""
    out: dict = {}
    for iss in _iter_issues(workspace, key):
        for fld in _USER_FIELDS:
            u = (iss.get("fields") or {}).get(fld)
            if isinstance(u, dict):
                uid = u.get("accountId") or u.get("name") or u.get("key")
                if uid:
                    out.setdefault(uid, u.get("displayName") or uid)
    return out


def _resolves_on_target(tgt_client, uid: str) -> bool:
    st, _ = tgt_client.req(f"{tgt_client.api_prefix}/user",
                           params={"accountId": uid})
    return st == 200


def detect_user_gaps(workspace: str, keys: list[str], tgt_client) -> list[dict]:
    seen, gaps = {}, []
    for key in keys:
        for uid, name in referenced_users(workspace, key).items():
            seen.setdefault(uid, name)
    for uid, name in seen.items():
        if not _resolves_on_target(tgt_client, uid):
            gaps.append({"area": "users", "name": name, "kind": "user_gap",
                         "detail": {"account_id": uid, "display_name": name}})
    return gaps
