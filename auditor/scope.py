"""Project scope: enumerate both sides and match by key.

Counts (src_count/tgt_count) are left None here; the run engine fills them
via approx_count so this stays a pure function over project lists.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Container keys (project/space keys) and custom-field ids come from the
# AUDITED instance, which the threat model treats as a potentially-hostile /
# MITM'd data source. These values become FILENAMES downstream (workspace
# extract paths, field-value capture files). A hostile key like "../../etc/x"
# would otherwise be used verbatim as a path component. Real Jira project keys,
# Confluence space keys, and customfield ids ALWAYS match this charset, so
# anything outside it is rejected at the boundary and never used as a path.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


def is_safe_container_key(key) -> bool:
    """True iff `key` is a safe container key / field id (matches
    ^[A-Za-z0-9._-]{1,255}$). Rejects path-traversal / separator characters
    so an audited-instance-supplied key can never escape the workspace."""
    return isinstance(key, str) and bool(_SAFE_KEY.match(key))


def match_projects(src_projects: list, tgt_projects: list) -> dict:
    # The key set is the audited scope boundary: drop any container whose key
    # is not filesystem-safe (it would later become an extract filename). A
    # rejected key is logged and skipped, never silently used as a path.
    src_projects = _filter_safe(src_projects, "source")
    tgt_projects = _filter_safe(tgt_projects, "target")
    s = {p["key"]: p for p in src_projects}
    t = {p["key"]: p for p in tgt_projects}
    matched = []
    for key in sorted(set(s) & set(t)):
        matched.append({
            "key": key,
            "name": s[key].get("name"),
            "src_id": s[key].get("id"),
            "tgt_id": t[key].get("id"),
            "src_count": None,
            "tgt_count": None,
        })
    source_only = [{"key": k, "name": s[k].get("name")}
                   for k in sorted(set(s) - set(t))]
    target_only = [{"key": k, "name": t[k].get("name")}
                   for k in sorted(set(t) - set(s))]
    return {"matched": matched, "source_only": source_only,
            "target_only": target_only}


def _filter_safe(projects: list, side: str) -> list:
    """Keep only projects whose container key is filesystem-safe. A rejected
    key is logged and dropped so a hostile audited instance cannot inject a
    path-traversal key into the scope set."""
    out = []
    for p in projects or []:
        if is_safe_container_key(p.get("key")):
            out.append(p)
        else:
            log.warning("scope: skipping %s container with unsafe key %r "
                        "(rejected by path-traversal guard)", side,
                        p.get("key"))
    return out
