"""Capture the full source definition a fix needs, bounded to one finding.

Called from the audit's config stage for each fixable config finding; the
returned dict is persisted as finding['fix_payload'] so remediation never
re-scans the source. Returns None for findings no Tier-1 fix can recreate
(workflows, schemes, jsm) — those are detect-and-guide."""
from __future__ import annotations

import logging

from auditor.scope import is_safe_container_key

log = logging.getLogger(__name__)

# Jira statusCategory.key -> the create-status API's statusCategory token.
_CAT = {"new": "TODO", "indeterminate": "IN_PROGRESS", "done": "DONE"}
# Must stay in sync with _SELECT_MARKERS in config_audit.py (no shared import
# because config_audit is not a dependency of the remediation layer).
_SELECT = ("select", "radio", "checkbox", "cascading")
# Must stay in sync with _OPTION_CONTEXT_CAP in config_audit.py.
_OPTION_CONTEXT_CAP = 3


def _find_field(client, name):
    # Uses paginate_start_at to match how config_audit.py fetches /field
    # (large tenants can have hundreds of custom fields spread across pages;
    # a bare req() would silently truncate at the first page).
    fields, _ = client.paginate_start_at(f"{client.api_prefix}/field")
    for f in (fields or []):
        if f.get("custom") and f.get("name") == name:
            return f
    return None


def _capture_custom_field(client, name):
    rec = _find_field(client, name)
    if rec is None:
        return None
    # field_id comes from the audited instance's /field response and becomes a
    # FILENAME downstream ({field_id}.jsonl.gz in stage_capture_values). A
    # hostile/MITM'd instance could return an id like "../../etc/x"; reject any
    # non-filesystem-safe id here so it is never used as a path. Real Jira
    # customfield ids (e.g. "customfield_10001") always pass.
    fid = rec.get("id")
    if not is_safe_container_key(fid):
        log.warning("payload: skipping custom field %r with unsafe field_id "
                    "%r (rejected by path-traversal guard)", name, fid)
        return None
    ctype = str((rec.get("schema") or {}).get("custom", "")).split(":")[-1]
    out = {"type": ctype, "field_id": fid, "contexts": []}
    if any(m in ctype for m in _SELECT):
        # Read options PER CONTEXT (the audit's _field_options returns a flat
        # set across contexts; faithful recreation needs them grouped so each
        # recreated context gets its own option list).
        ctx, _ = client.paginate_start_at(
            f"{client.api_prefix}/field/{rec['id']}/context")
        for c in (ctx or [])[:_OPTION_CONTEXT_CAP]:
            o, _ = client.paginate_start_at(
                f"{client.api_prefix}/field/{rec['id']}/context/{c['id']}/option")
            out["contexts"].append(
                {"name": c.get("name"),
                 "options": [x.get("value") for x in (o or []) if x.get("value")]})
    return out


def _source_field_option_set(client, rec) -> set:
    """Flat set of every option value the source field carries (across its
    capped contexts) — used to recompute the missing-option delta for fidelity
    rather than trusting a possibly-truncated finding detail."""
    opts = set()
    ctx, _ = client.paginate_start_at(
        f"{client.api_prefix}/field/{rec['id']}/context")
    for c in (ctx or [])[:_OPTION_CONTEXT_CAP]:
        o, _ = client.paginate_start_at(
            f"{client.api_prefix}/field/{rec['id']}/context/{c['id']}/option")
        opts |= {x.get("value") for x in (o or []) if x.get("value")}
    return opts


def _capture_option_mismatch(client, finding):
    """Capture the missing-option delta for an option_mismatch finding so the
    add_options fix has a payload. Re-reads the source field's live options and
    keeps only the previously-flagged-missing values that still exist on the
    source (fidelity), preserving the finding's detail when the live read is
    empty (e.g. a transient source outage)."""
    name = finding.get("name")
    rec = _find_field(client, name)
    if rec is None:
        return None
    detail = finding.get("detail") or {}
    flagged = detail.get("missing_options_in_tgt") or []
    live = _source_field_option_set(client, rec)
    missing = [v for v in flagged if v in live] if live else list(flagged)
    if not missing:
        return None
    return {"field_name": name, "missing_options": missing}


def _simple(client, list_path, name, build):
    st, d = client.req(list_path)
    if st != 200 or not isinstance(d, list):
        return None
    for obj in d:
        if obj.get("name") == name:
            return build(obj)
    return None


def capture_config_payload(src_client, finding: dict) -> dict | None:
    # option_mismatch on a custom field has a recreatable delta (the missing
    # options) that the add_options fix applies — capture it before the
    # missing_in_tgt guard below (C2/I5).
    if (finding.get("kind") == "option_mismatch"
            and finding.get("area") == "custom_fields"):
        return _capture_option_mismatch(src_client, finding)
    # Only missing_in_tgt findings (beyond the option_mismatch case above) have
    # a recreatable source definition; area_error / type_mismatch findings have
    # no payload that remediation can apply, so exit early for every area.
    if finding.get("kind") != "missing_in_tgt":
        return None
    area, name = finding.get("area"), finding.get("name")
    pre = src_client.api_prefix
    if area == "custom_fields":
        return _capture_custom_field(src_client, name)
    if area == "statuses":
        return _simple(src_client, f"{pre}/status", name, lambda o: {
            "name": o["name"],
            "category": _CAT.get((o.get("statusCategory") or {}).get("key"),
                                 "TODO")})
    if area == "priorities":
        return _simple(src_client, f"{pre}/priority", name, lambda o: {
            "name": o["name"], "description": o.get("description", "")})
    if area == "resolutions":
        return _simple(src_client, f"{pre}/resolution", name, lambda o: {
            "name": o["name"], "description": o.get("description", "")})
    if area == "issue_types":
        return _simple(src_client, f"{pre}/issuetype", name, lambda o: {
            "name": o["name"], "description": o.get("description", ""),
            "hierarchy_level": o.get("hierarchyLevel", 0)})
    if area == "link_types":
        st, d = src_client.req(f"{pre}/issueLinkType")
        if st != 200 or not isinstance(d, dict):
            return None
        for lt in d.get("issueLinkTypes", []):
            if lt.get("name") == name:
                return {"name": lt["name"], "inward": lt.get("inward", ""),
                        "outward": lt.get("outward", "")}
        return None
    return None   # workflows / schemes / jsm / screens-deep → detect-and-guide
