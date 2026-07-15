"""Apply app-tier env-audit findings against a live Jira instance.

Safety contracts (spec R8 / I3 / I4):
  1. Identity guard:  before any HTTP write, verify the client's api_base
     matches expected_api_base.  A mis-wired caller raises ValueError
     immediately, before any write.  Copied from auditor.remediation.apply.
  2. Tier re-derivation (I4):  only findings whose kind maps to tier='app'
     in the _FIXES registry are ever applied — the server re-derives the
     tier from the stored finding's kind, never trusting client input.
  3. Idempotent:  before deleting, re-read the object list by name. If the
     object is already absent, log a no-op 'already absent' and count it
     as closed — do not raise or count as failed.
  4. Every API call is logged (method, path, status, ok, object_name, error).
  5. ONLY scope-set app-tier kinds are ever applied. The scope set is the
     exhaustive list defined here.
  6. NEVER delete on stale audit data (C1/C2). At apply time, for every
     selected finding, FETCH THE LIVE STATE and re-verify the precondition
     before any DELETE, using the freshly-fetched object id from the live
     list — never a stale id, and never the name alone:
       - schemes:  re-read the scheme's CURRENT project usage; if it is now
                   used by >=1 project, ABORT (no DELETE, still_open, ok=True
                   'skipped — now in use').
       - groups:   re-read the group's CURRENT member count; if >0, ABORT.
       - empty_screen: re-fetch the screen's tabs+fields; if it has ANY field
                   now, ABORT.
       - screen_not_in_scheme: re-read screen-scheme membership; if any scheme
                   now references the screen id, ABORT.
       - workflow_unreferenced: re-read workflow-scheme usage; if any scheme
                   now references the workflow name, ABORT.
       - unused_custom_field: re-fetch every screen's fields; if the field id is
                   now on a screen, ABORT. PLUS a VALUE CHECK: approx_count
                   `cf[<numericId>] is not EMPTY`; if > 0 the field holds data on
                   some issue, ABORT (deleting would destroy data).
       - empty_project: re-verify approx_count(project = "<KEY>") == 0; if it
                   now has issues, ABORT.
  7. Name-collision safety (H2/M2). The live id is resolved by collecting ALL
     entries whose name matches the finding name:
       - exactly ONE match  → use its freshly-fetched id;
       - ZERO matches       → already absent (no-op, closed);
       - MORE THAN ONE      → ambiguous, ABORT (no DELETE, still_open);
       - one match but its id key is missing → ERROR (still_open, ok=False),
         NOT a false 'already absent' (the API list shape differs).
  8. BUILT-IN / DEFAULT OBJECT PROTECTION (critical). _is_protected() SKIPS
     (still_open, ok=True, logs 'looks like a built-in/default object', never
     deletes) when the resolved id is numeric and <= 10000 (Jira reserves low
     ids for system/default objects; for custom fields the customfield_NNNNN
     numeric part is used), OR the name matches a default/system pattern
     (starts with 'Default', or a well-known system name). When uncertain, do
     NOT delete.

Delete endpoints (Cloud /rest/api/3):
  scheme_unused                 → GET /workflowscheme (list) + DELETE /workflowscheme/{id}
  unused_issue_type_scheme      → GET /issuetypescheme (list) + DELETE /issuetypescheme/{issueTypeSchemeId}
  unused_issue_type_screen_scheme → GET /issuetypescreenscheme (list) + DELETE /issuetypescreenscheme/{issueTypeScreenSchemeId}
  empty_group                   → GET /group/bulk (list by name + groupId) + DELETE /group?groupId=...
  empty_screen                  → GET /screens (list) + DELETE /screens/{id}
  screen_not_in_scheme          → GET /screens (list) + DELETE /screens/{id}
  workflow_unreferenced         → GET /workflow/search (list) + DELETE /workflow/{entityId}
  unused_custom_field           → GET /field (list) + DELETE /field/{customfield_NNNNN}
  empty_project                 → GET /project/search (list) + DELETE /project/{key}
  status_not_in_workflow        → GET /statuses (list) + DELETE /statuses?id={id}
                                  (TOCTOU re-read /workflow/search statuses;
                                   VALUE-CHECK approx_count status = "<name>")

Returns (closed: int, still_open: int) for the _finalize_fix verdict logic.
"""
from __future__ import annotations

import json
import os
import re
import threading

from auditor.client import escape_query_key
from auditor.envaudit._pool import apply_worker_count, map_results
from auditor.envaudit.fixes import _FIXES


class DestructiveCapExceeded(RuntimeError):
    """Raised before any HTTP when a single apply batch would perform more
    destructive (delete) operations than the configured blast-radius cap.

    This is a kill-switch against a runaway selection (a UI "select all" typo,
    a malformed ref set, or a logic error fanning out deletes): the operator
    must intentionally trim the selection or raise MA_MAX_DESTRUCTIVE."""


# Blast-radius cap: the maximum number of app-tier (destructive) findings a
# single apply batch may carry. Override with MA_MAX_DESTRUCTIVE. 0 is a valid
# value meaning "block every destructive op" (a global dry kill-switch); a
# negative / unparseable value falls back to the default.
_DEFAULT_MAX_DESTRUCTIVE = 50


def _destructive_cap() -> int:
    raw = os.environ.get("MA_MAX_DESTRUCTIVE")
    if raw is None:
        return _DEFAULT_MAX_DESTRUCTIVE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_DESTRUCTIVE
    return n if n >= 0 else _DEFAULT_MAX_DESTRUCTIVE


# Circuit-breaker: how many SERVER-SIDE (5xx / 429) write failures a single apply
# batch tolerates before it stops issuing further DELETEs. A failing instance
# (outage, throttling storm) should not be hammered — especially once writes run
# concurrently. Override with MA_BREAKER_THRESHOLD; 0 disables the breaker.
_DEFAULT_BREAKER_THRESHOLD = 5


def _breaker_threshold() -> int:
    raw = os.environ.get("MA_BREAKER_THRESHOLD")
    if raw is None:
        return _DEFAULT_BREAKER_THRESHOLD
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_BREAKER_THRESHOLD
    return n if n >= 0 else _DEFAULT_BREAKER_THRESHOLD


class _WriteBreaker:
    """Thread-safe circuit-breaker for the live-write batch.

    record(status) is called once per LOGICAL delete (after the client's own
    per-call 5xx/429 retry/backoff has resolved to a final status). Only
    server-side failures (>=500 or 429) count toward tripping — a 4xx is an
    object-level problem, not an instance outage, and must not open the breaker.
    Once `threshold` server-side failures accumulate the breaker trips, and
    should_block() returns True so the driver skips remaining deletes. With
    threshold <= 0 the breaker is disabled (never trips)."""

    def __init__(self, threshold: int):
        self._threshold = threshold
        self._failures = 0
        self._tripped = False
        self._lock = threading.Lock()

    def record(self, status: int) -> None:
        if self._threshold <= 0:
            return
        # Server-side failures trip the breaker: 5xx, 429, AND status < 0 — the
        # client's exhausted-idempotent-retries return on a TRANSPORT failure
        # (connection drop / reset / timeout). Without status < 0 a connection-
        # dropping instance would never trip the breaker. A 4xx is object-level
        # and must NOT trip.
        if status >= 500 or status == 429 or status < 0:
            with self._lock:
                self._failures += 1
                if self._failures >= self._threshold:
                    self._tripped = True

    def should_block(self) -> bool:
        # Take the lock (cheap — called once per finding) so the trip is observed
        # with a proper happens-before edge: this makes the "threshold + workers-1"
        # bound provable rather than GIL-incidental, and correct on a no-GIL build.
        with self._lock:
            return self._tripped


# A disabled breaker for direct callers of the per-finding helpers in tests.
_NULL_BREAKER = _WriteBreaker(0)

_BREAKER_SKIP_MSG = ("skipped — circuit breaker open (instance returned repeated "
                     "server errors); rerun when it recovers")

# The exhaustive scope set: only these kinds are ever auto-applied. Extending
# the scope requires adding an entry here AND a resolver/_delete helper below —
# this set is the authoritative gate.
_APP_TIER_SCOPE = frozenset({
    "scheme_unused",
    "unused_issue_type_scheme",
    "unused_issue_type_screen_scheme",
    "empty_group",
    # Expanded cleanup deletes (each with a dedicated resolver + TOCTOU below).
    "empty_screen",
    "screen_not_in_scheme",
    "workflow_unreferenced",
    "unused_custom_field",
    "empty_project",
    # status_not_in_workflow: the status is in NO workflow, so the delete is
    # clean. Guarded by built-in status protection + a live workflow re-read +
    # an issues-in-status value-check (see _apply_expanded).
    "status_not_in_workflow",
})

# Kinds handled by the dedicated _apply_expanded path (everything outside the
# original scheme/group generic table).
_EXPANDED_KINDS = frozenset({
    "empty_screen",
    "screen_not_in_scheme",
    "workflow_unreferenced",
    "unused_custom_field",
    "empty_project",
    "status_not_in_workflow",
})


def _is_destructive(finding: dict) -> bool:
    """True when this finding would actually attempt a destructive op: its kind
    is app-tier in the registry AND in the apply scope. The single source of
    truth shared by the blast-radius cap and the per-finding apply driver."""
    kind = finding.get("kind") or ""
    entry = _FIXES.get(kind)
    return bool(entry) and entry.get("tier") == "app" and kind in _APP_TIER_SCOPE

# ---------------------------------------------------------------------------
# Built-in / default object protection (spec R8, NEW critical guard).
# ---------------------------------------------------------------------------

# Jira reserves ids <= this for system/default objects (the user explicitly
# wants "anything with ID 10000" protected, hence <=).
_SYSTEM_ID_MAX = 10000

# Well-known system/default object names (case-insensitive exact matches), plus
# any name starting with "Default" is treated as a default object. Be
# conservative: when uncertain, DO NOT delete.
_SYSTEM_NAMES = frozenset(n.lower() for n in (
    "jira",                       # the built-in default workflow
    "Default Screen",
    "Resolve Issue Screen",
    "Workflow Screen",
    "Default Issue Screen",
    "Resolution Screen",
    "Default Workflow Scheme",
    "Default Screen Scheme",
    "Default Issue Type Scheme",
    "Default Issue Type Screen Scheme",
    "Default Field Configuration",
    "Default Field Configuration Scheme",
    "Default Permission Scheme",
    "Default Notification Scheme",
    # Well-known built-in STATUS names — a status_not_in_workflow finding must
    # never delete one of these even if Jira happens to give it a high id.
    "To Do",
    "In Progress",
    "Done",
    "Open",
    "Reopened",
    "Resolved",
    "Closed",
    "In Review",
    "Backlog",
    "Selected for Development",
))

_CF_NUM_RE = re.compile(r"customfield_(\d+)$")


def _numeric_id(obj_id):
    """Return the integer id for a protection check, or None when not numeric.

    Handles plain numeric ids ("10001") and the customfield_NNNNN form (the
    numeric suffix is what Jira reserves). A non-numeric id (e.g. a workflow
    entityId UUID, or a project KEY string) returns None — protection then falls
    back to the name check only."""
    if obj_id is None:
        return None
    s = str(obj_id)
    m = _CF_NUM_RE.match(s)
    if m:
        return int(m.group(1))
    if s.isdigit():
        return int(s)
    return None


def _is_protected(area, name, obj):
    """Return (protected: bool, reason: str|None).

    A True result means the object looks like a Jira system/default object and
    must NEVER be deleted — the caller SKIPS it (still_open, ok=True, logged).

    Protected when:
      - the resolved object id is numeric and <= _SYSTEM_ID_MAX (for custom
        fields the customfield_NNNNN numeric part), OR
      - the name starts with "Default" (case-insensitive), OR is one of the
        well-known system names.
    Conservative by design: an unknown shape is NOT auto-protected here (the
    delete still runs its own TOCTOU re-verify), but anything matching the id or
    name heuristics above is always spared.
    """
    obj = obj or {}
    # id-based protection (numeric id / customfield numeric part)
    obj_id = obj.get("_resolved_id")
    n = _numeric_id(obj_id)
    if n is not None and n <= _SYSTEM_ID_MAX:
        return True, (f"id {obj_id} <= {_SYSTEM_ID_MAX} — looks like a "
                      f"built-in/default object")
    # name-based protection
    nm = (name or "").strip()
    low = nm.lower()
    if low.startswith("default"):
        return True, f"name {nm!r} starts with 'Default' — looks like a default object"
    if low in _SYSTEM_NAMES:
        return True, f"name {nm!r} is a well-known built-in/default object"
    return False, None

# Jira REST paths for listing and deleting each kind.
# Each entry:
#   list_path:            GET path returning a {values:[...]} page
#   id_key:               key of the id field in each list item
#   name_key:             key of the name field
#   delete_path:          format string; {id} is the resolved object id.
#                         For groups, the delete is via query param.
#   group_delete:         True only for empty_group (query-param DELETE).
#   usage_inline_key:     key on the live list item holding the project-id list
#                         (Cloud /workflowscheme carries projectIds inline); None
#                         when usage is not inline and must be fetched per-scheme.
#   usage_path:           per-scheme usage endpoint (GET) used when the inline
#                         key is absent; {id} is the resolved id. The response is
#                         a {values:[...]} page whose items carry projectIds.
#                         None when usage is always inline (or N/A for groups).
#   usage_param:          query-param name carrying the scheme id on usage_path.
_KIND_SPEC = {
    "scheme_unused": {
        "list_path": "/rest/api/3/workflowscheme",
        "id_key": "id",
        "name_key": "name",
        "delete_path": "/rest/api/3/workflowscheme/{id}",
        "group_delete": False,
        "usage_inline_key": "projectIds",
        "usage_path": None,
        "usage_param": None,
    },
    "unused_issue_type_scheme": {
        "list_path": "/rest/api/3/issuetypescheme",
        "id_key": "id",
        "name_key": "name",
        "delete_path": "/rest/api/3/issuetypescheme/{id}",
        "group_delete": False,
        "usage_inline_key": None,
        "usage_path": "/rest/api/3/issuetypescheme/project",
        "usage_param": "issueTypeSchemeId",
    },
    "unused_issue_type_screen_scheme": {
        "list_path": "/rest/api/3/issuetypescreenscheme",
        "id_key": "id",
        "name_key": "name",
        "delete_path": "/rest/api/3/issuetypescreenscheme/{id}",
        "group_delete": False,
        "usage_inline_key": None,
        "usage_path": "/rest/api/3/issuetypescreenscheme/project",
        "usage_param": "issueTypeScreenSchemeId",
    },
    "empty_group": {
        "list_path": "/rest/api/3/group/bulk",
        "id_key": "groupId",
        "name_key": "name",
        "delete_path": "/rest/api/3/group",   # DELETE with ?groupId= param
        "group_delete": True,
        "usage_inline_key": None,
        "usage_path": None,
        "usage_param": None,
    },
}


# Cap the captured snapshot so a pathological object (e.g. a field with a huge
# option context) cannot bloat the local fix-log. Config objects are small.
_SNAP_CAP = 8000


def _rec(object_name, method, path, status, ok, error=None, snapshot=None):
    """Build a log record matching the fix-log shape (spec R8).

    L3 (pre-delete snapshot): when `snapshot` is given (the live object JSON that
    was just resolved), it is captured on the record so a destructive action is
    forensically reconstructable. This is stored LOCALLY only (the fix_actions
    table) — it is never sent to an AI provider or any external service; it is
    config metadata for the operator's own audit/restore trail."""
    snap_json = None
    if snapshot is not None:
        try:
            snap_json = json.dumps(snapshot, default=str)[:_SNAP_CAP]
        except (TypeError, ValueError):
            snap_json = None
    return {
        "finding_ref": None,
        "fix_id": None,
        "object_name": object_name,
        "method": method,
        "path": path,
        "status": status,
        "ok": ok,
        "created_id": None,
        "error": error,
        "snapshot_json": snap_json,
    }


# Sentinel resolution outcomes for the name->id step (H2/M2).
_RESOLVE_OK = "ok"                # exactly one match with a usable id
_RESOLVE_ABSENT = "absent"        # zero matches → already gone (idempotent)
_RESOLVE_AMBIGUOUS = "ambiguous"  # >1 match → cannot safely target one
_RESOLVE_ERROR = "error"          # list failed, or one match but no id key


def _resolve_live(client, spec, name):
    """Resolve the live object for *name* against a freshly-fetched list.

    Returns (outcome, item, object_id, detail):
      - outcome == _RESOLVE_OK:        exactly one name match WITH a usable id.
                                       item is the live list entry; object_id is
                                       its freshly-fetched id (never stale).
      - outcome == _RESOLVE_ABSENT:    zero name matches → already gone.
      - outcome == _RESOLVE_AMBIGUOUS: >1 name match → ambiguous; detail carries
                                       the match count.
      - outcome == _RESOLVE_ERROR:     the list call failed, OR a single entry
                                       matched but its id key is missing (the API
                                       list shape differs) — this must NOT be a
                                       false 'already absent' (M2). detail holds
                                       the error string.
    """
    id_key = spec["id_key"]
    name_key = spec["name_key"]

    items, err = client.paginate_start_at(spec["list_path"], key="values")
    if err:
        return _RESOLVE_ERROR, None, None, f"list failed: {err}"

    matches = [it for it in (items or []) if it.get(name_key) == name]
    if not matches:
        return _RESOLVE_ABSENT, None, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, None, len(matches)

    item = matches[0]
    if id_key not in item or item.get(id_key) in (None, ""):
        # A single entry matched by name but its id cannot be extracted. Do NOT
        # treat as already-absent: that would falsely close the finding when the
        # API list shape differs. Surface as a loud error instead (M2).
        return _RESOLVE_ERROR, item, None, f"matched {name!r} but no id field"
    return _RESOLVE_OK, item, str(item[id_key]), None


def _scheme_in_use(client, spec, obj_id, item):
    """Re-verify a scheme's CURRENT project usage at apply time (C1).

    Returns (in_use_count: int, error: str | None). A non-None error means the
    usage could not be read; the caller treats that conservatively as 'in use'
    (do not delete on an unverifiable precondition).

    Usage is read from the freshly-fetched live list item's inline project-id
    list when present (Cloud /workflowscheme), else from a per-scheme usage
    endpoint.
    """
    inline_key = spec.get("usage_inline_key")
    if inline_key:
        # The inline project-id list is authoritative for this kind (Cloud
        # /workflowscheme always includes projectIds; an absent key means the
        # scheme has no attached projects). Read it from the FRESH live item.
        return len((item or {}).get(inline_key) or []), None

    usage_path = spec.get("usage_path")
    if not usage_path:
        # No way to re-verify usage for this kind → conservative: cannot prove
        # unused, so report as in use to block the delete.
        return 1, "no usage endpoint to re-verify"

    rows, err = client.paginate_start_at(
        usage_path, params={spec["usage_param"]: obj_id}, key="values")
    if err:
        return 1, f"usage read failed: {err}"
    # Each usage row carries this scheme's attached project ids.
    total = 0
    for row in (rows or []):
        total += len(row.get("projectIds") or [])
    return total, None


def _group_member_count(client, group_id):
    """Re-verify a group's CURRENT member count at apply time (C2).

    Returns (count: int, error: str | None). On an unreadable count the caller
    treats it conservatively as non-empty (do not delete)."""
    st, d = client.req("/rest/api/3/group/member", method="GET",
                       params={"groupId": group_id, "maxResults": 0})
    if st != 200 or not isinstance(d, dict):
        return 1, f"member-count read failed: status {st}"
    try:
        return int(d.get("total") or 0), None
    except (TypeError, ValueError):
        return 1, "member-count not an integer"


def _object_exists(client, spec, name):
    """Re-check post-delete: return True if the object is still present."""
    items, err = client.paginate_start_at(spec["list_path"], key="values")
    if err:
        return True    # conservative: treat fetch error as still-present
    return any(item.get(spec["name_key"]) == name for item in (items or []))


# ===========================================================================
# Expanded app-tier deletes (empty_screen, screen_not_in_scheme,
# workflow_unreferenced, unused_custom_field, empty_project).
#
# Each kind has a resolver returning (outcome, obj_id, item, detail) using the
# same H2/M2 name-collision sentinels, then a TOCTOU re-verify, then a delete.
# Built-in protection (R8) runs after resolution and before any TOCTOU/delete.
# ===========================================================================

def _resolve_named(client, list_path, name, id_key="id", name_key="name"):
    """Resolve a flat {id, name} list by name (H2/M2). Returns the standard
    (_RESOLVE_*, item, object_id, detail) tuple used across this module."""
    items, err = client.paginate_start_at(list_path, key="values")
    if err:
        return _RESOLVE_ERROR, None, None, f"list failed: {err}"
    matches = [it for it in (items or []) if it.get(name_key) == name]
    if not matches:
        return _RESOLVE_ABSENT, None, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, None, len(matches)
    item = matches[0]
    if id_key not in item or item.get(id_key) in (None, ""):
        return _RESOLVE_ERROR, item, None, f"matched {name!r} but no id field"
    return _RESOLVE_OK, item, str(item[id_key]), None


def _resolve_field(client, name):
    """Resolve a custom field by display name from the flat /field list.

    Returns (_RESOLVE_*, item, field_id, detail) where field_id is the
    customfield_NNNNN id. Only entries with custom=True are considered (a system
    field never matches a custom-field finding)."""
    st, d = client.req("/rest/api/3/field")
    if st != 200 or not isinstance(d, list):
        return _RESOLVE_ERROR, None, None, f"field list failed: status {st}"
    matches = [f for f in d if f.get("custom") and f.get("name") == name]
    if not matches:
        return _RESOLVE_ABSENT, None, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, None, len(matches)
    item = matches[0]
    fid = item.get("id")
    if not fid:
        return _RESOLVE_ERROR, item, None, f"matched {name!r} but no id field"
    return _RESOLVE_OK, item, str(fid), None


def _resolve_status(client, name):
    """Resolve a status by name from the flat Cloud /statuses list.

    Cloud /rest/api/3/statuses returns a plain JSON array of {id, name, ...}.
    Returns (_RESOLVE_*, item, status_id, detail) using the standard H2/M2
    name-collision sentinels."""
    st, d = client.req("/rest/api/3/statuses")
    if st != 200 or not isinstance(d, list):
        return _RESOLVE_ERROR, None, None, f"status list failed: status {st}"
    matches = [s for s in d
               if isinstance(s, dict) and s.get("name") == name]
    if not matches:
        return _RESOLVE_ABSENT, None, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, None, len(matches)
    item = matches[0]
    sid = item.get("id")
    if sid in (None, ""):
        return _RESOLVE_ERROR, item, None, f"matched {name!r} but no id field"
    return _RESOLVE_OK, item, str(sid), None


def _resolve_workflow(client, name):
    """Resolve a workflow by name from Cloud /workflow/search.

    Cloud rows carry id as {name, entityId}; the delete is keyed by entityId.
    Returns (_RESOLVE_*, item, entity_id, detail)."""
    items, err = client.paginate_start_at("/rest/api/3/workflow/search",
                                          key="values")
    if err:
        return _RESOLVE_ERROR, None, None, f"list failed: {err}"

    def _wf_name(w):
        wid = w.get("id")
        return wid.get("name") if isinstance(wid, dict) else w.get("name")

    matches = [w for w in (items or []) if _wf_name(w) == name]
    if not matches:
        return _RESOLVE_ABSENT, None, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, None, len(matches)
    item = matches[0]
    wid = item.get("id")
    entity = wid.get("entityId") if isinstance(wid, dict) else item.get("entityId")
    if not entity:
        return _RESOLVE_ERROR, item, None, f"matched {name!r} but no entityId"
    return _RESOLVE_OK, item, str(entity), None


def _screen_has_fields(client, screen_id):
    """TOCTOU (empty_screen): re-fetch the screen's tabs+fields. Returns
    (has_fields: bool, error: str|None). On an unreadable state, errs to
    has_fields=True (conservative — never delete on a state we can't confirm)."""
    tabs, err = client.paginate_start_at(
        f"/rest/api/3/screens/{screen_id}/tabs", key="values")
    if err:
        return True, f"tab read failed: {err}"
    for tb in (tabs or []):
        tid = tb.get("id")
        st, flds = client.req(
            f"/rest/api/3/screens/{screen_id}/tabs/{tid}/fields")
        if st != 200 or not isinstance(flds, list):
            return True, f"field read failed: status {st}"
        if flds:
            return True, None
    return False, None


def _screen_field_ids(client, screen_id):
    """Return the SET of field ids on a screen (used by the custom-field TOCTOU
    to check whether the field has reappeared on a screen). Returns
    (ids: set, error: str|None)."""
    out: set = set()
    tabs, err = client.paginate_start_at(
        f"/rest/api/3/screens/{screen_id}/tabs", key="values")
    if err:
        return out, f"tab read failed: {err}"
    for tb in (tabs or []):
        tid = tb.get("id")
        st, flds = client.req(
            f"/rest/api/3/screens/{screen_id}/tabs/{tid}/fields")
        if st != 200 or not isinstance(flds, list):
            return out, f"field read failed: status {st}"
        for f in flds:
            if isinstance(f, dict) and f.get("id"):
                out.add(str(f["id"]))
    return out, None


def _screen_in_any_scheme(client, screen_id):
    """TOCTOU (screen_not_in_scheme): re-read /screenscheme membership. Returns
    (in_scheme: bool, error: str|None). Conservative on error (True)."""
    rows, err = client.paginate_start_at("/rest/api/3/screenscheme", key="values")
    if err:
        return True, f"screenscheme read failed: {err}"
    sid = str(screen_id)
    for row in (rows or []):
        screens = row.get("screens")
        if isinstance(screens, dict):
            if any(str(v) == sid for v in screens.values()):
                return True, None
    return False, None


def _workflow_referenced(client, name):
    """TOCTOU (workflow_unreferenced): re-read /workflowscheme usage
    (defaultWorkflow + issueTypeMappings, both by workflow NAME). Returns
    (referenced: bool, error: str|None). Conservative on error (True)."""
    rows, err = client.paginate_start_at("/rest/api/3/workflowscheme", key="values")
    if err:
        return True, f"workflowscheme read failed: {err}"
    for row in (rows or []):
        if row.get("defaultWorkflow") == name:
            return True, None
        mappings = row.get("issueTypeMappings")
        if isinstance(mappings, dict) and name in mappings.values():
            return True, None
    return False, None


def _status_in_any_workflow(client, status_name):
    """TOCTOU (status_not_in_workflow): re-read /workflow/search and check
    whether ANY workflow now lists this status in its status set. Returns
    (in_workflow: bool, error: str|None). Conservative on error (True): never
    delete a status whose workflow membership we cannot confirm."""
    rows, err = client.paginate_start_at("/rest/api/3/workflow/search",
                                         key="values")
    if err:
        return True, f"workflow read failed: {err}"
    for w in (rows or []):
        if not isinstance(w, dict):
            continue
        for s in (w.get("statuses") or []):
            nm = s.get("name") if isinstance(s, dict) else s
            if nm == status_name:
                return True, None
    return False, None


def _status_issue_count(client, status_name):
    """VALUE CHECK (status_not_in_workflow): count issues currently sitting in
    the status. Deleting a status that holds issues would lose their state, so
    this gate aborts the delete when the count is > 0 OR unreadable. Returns
    (count: int, error|None); conservative on error: returns (1, err)."""
    cnt = client.approx_count(f'status = "{escape_query_key(status_name)}"')
    if isinstance(cnt, int):
        return cnt, None
    return 1, f"issue-count failed: {cnt}"


def _field_value_count(client, field_id, field_name):
    """VALUE CHECK (unused_custom_field): count issues where the field is not
    EMPTY. Uses cf[<numericId>] when the id is the customfield_NNNNN form,
    else falls back to the quoted field name. Returns (count: int, error|None).
    Conservative on error: returns (1, err) so a field whose value-count cannot
    be read is treated as holding data (never deleted)."""
    m = _CF_NUM_RE.match(str(field_id))
    if m:
        jql = f"cf[{m.group(1)}] is not EMPTY"
    else:
        jql = f'"{escape_query_key(field_name)}" is not EMPTY'
    cnt = client.approx_count(jql)
    if isinstance(cnt, int):
        return cnt, None
    return 1, f"value-count failed: {cnt}"


def _group_grants_in_permission_scheme(client, group_id, group_name):
    """REFERENCE CHECK (empty_group): is the group a holder of a grant in any
    permission scheme? A group with zero MEMBERS can still GRANT access via a
    scheme; deleting it silently removes that grant, and re-creating the group
    later does NOT restore it. Returns (referenced, error|None); conservative on
    error (True) so an unreadable scheme set never green-lights the delete.

    Scope: DIRECT permission-scheme grants only. NOT checked — and each is also
    an access-revocation path on delete, so they are real residuals, not just
    cosmetic: a group that is a PROJECT-ROLE ACTOR (the common Cloud pattern —
    schemes grant to a role, the group is an actor in it) or an ISSUE-SECURITY
    scheme member. (Notification schemes are the only cosmetic-on-delete case.)
    Holder shape is matched broadly (parameter / value / group.groupId /
    group.name) across API variants, anchored on the freshly-resolved groupId."""
    st, d = client.req(f"{client.api_prefix}/permissionscheme",
                       params={"expand": "permissions"})
    if st != 200 or not isinstance(d, dict):
        return True, f"permission-scheme read failed (status {st})"
    schemes = d.get("permissionSchemes") or []
    # Defence against API/expand drift: schemes exist but NONE carry any grants
    # means permissions did not expand — treat as unverifiable, not as 'no grant'.
    if schemes and not any(s.get("permissions") for s in schemes):
        return True, "permission grants did not expand (unverifiable)"
    gid, gname = str(group_id), str(group_name)
    for sch in schemes:
        for grant in (sch.get("permissions") or []):
            h = grant.get("holder") or {}
            if h.get("type") != "group":
                continue
            grp = h.get("group") or {}
            cands = {str(h.get("parameter")), str(h.get("value")),
                     str(grp.get("groupId")), str(grp.get("name"))}
            if gid in cands or gname in cands:
                return True, None
    return False, None


def _field_in_any_filter(client, field_id, field_name):
    """REFERENCE CHECK (unused_custom_field): does any SAVED FILTER's JQL
    reference this field? Deleting a field used in a filter's JQL silently breaks
    that filter — the on-screen + value checks miss this. Match the canonical
    cf[<numericId>] token OR the quoted field name in each filter's jql. Returns
    (referenced: bool, error|None); conservative on error (True) so an unreadable
    filter set never green-lights the delete."""
    m = _CF_NUM_RE.match(str(field_id))
    cf_token = f"cf[{m.group(1)}]" if m else None
    filters, err = client.paginate_start_at(
        f"{client.api_prefix}/filter/search",
        params={"expand": "jql"}, key="values")
    if err:
        return True, err
    for flt in (filters or []):
        jql = str(flt.get("jql") or "")
        if cf_token and cf_token in jql:
            return True, None
        if field_name and field_name in jql:
            return True, None
    return False, None


def _project_issue_count(client, key):
    """TOCTOU (empty_project): count issues in the project. Returns
    (count: int, error|None). Conservative on error (1)."""
    cnt = client.approx_count(f'project = "{escape_query_key(key)}"')
    if isinstance(cnt, int):
        return cnt, None
    return 1, f"issue-count failed: {cnt}"


def _apply_expanded(client, kind, name, log, breaker=None, dry_run=False):
    """Apply one expanded-kind finding. Returns 'closed' | 'still_open' |
    'would_close' (dry_run only).

    Flow per finding:
      resolve live (H2/M2) → built-in protection → TOCTOU re-verify →
      circuit-breaker gate → DELETE → prove closure by re-read.

    dry_run: run every read-only guard above, then STOP at the DELETE — emit a
    WOULD-DELETE record and return 'would_close' without writing.
    """
    if breaker is None:
        breaker = _NULL_BREAKER
    # 1) Resolve the live object by name (fresh id; never stale, never name-only)
    if kind in ("empty_screen", "screen_not_in_scheme"):
        list_path = "/rest/api/3/screens"
        outcome, item, obj_id, detail = _resolve_named(client, list_path, name)
    elif kind == "workflow_unreferenced":
        list_path = "/rest/api/3/workflow/search"
        outcome, item, obj_id, detail = _resolve_workflow(client, name)
    elif kind == "unused_custom_field":
        list_path = "/rest/api/3/field"
        outcome, item, obj_id, detail = _resolve_field(client, name)
    elif kind == "empty_project":
        list_path = "/rest/api/3/project/search"
        outcome, item, obj_id, detail = _resolve_named(
            client, list_path, name, id_key="id", name_key="key")
    elif kind == "status_not_in_workflow":
        list_path = "/rest/api/3/statuses"
        outcome, item, obj_id, detail = _resolve_status(client, name)
    else:  # unreachable (scope-gated), but fail safe
        log(_rec(name, "SKIP", "-", 0, False, error=f"unknown kind {kind!r}"))
        return "still_open"

    if outcome == _RESOLVE_ERROR:
        log(_rec(name, "GET", list_path, 0, False,
                 error=f"resolve failed: {detail}"))
        return "still_open"
    if outcome == _RESOLVE_ABSENT:
        log(_rec(name, "GET", list_path, 200, True, error="already absent"))
        return "closed"     # idempotent
    if outcome == _RESOLVE_AMBIGUOUS:
        log(_rec(name, "GET", list_path, 200, True,
                 error=f"skipped — name is ambiguous ({detail} matches)"))
        return "still_open"

    # 2) Built-in / default object protection (R8) — NEVER delete a system object.
    guard_obj = dict(item or {})
    guard_obj["_resolved_id"] = obj_id
    protected, why = _is_protected(kind, name, guard_obj)
    if protected:
        log(_rec(name, "SKIP", list_path, 200, True,
                 error=f"skipped — looks like a built-in/default object ({why})"))
        return "still_open"

    # 3) Per-kind TOCTOU re-verify (never delete on stale audit data). Most
    # kinds delete by a path id/key (delete_params stays None); only the status
    # delete uses a ?id= query param (set in its branch below).
    delete_params = None
    if kind == "empty_screen":
        has_fields, ferr = _screen_has_fields(client, obj_id)
        if has_fields:
            log(_rec(name, "GET", f"/rest/api/3/screens/{obj_id}/tabs", 200, True,
                     error=("skipped — screen now has at least one field"
                            if ferr is None
                            else f"skipped — could not confirm empty ({ferr})")))
            return "still_open"
        delete_path = f"/rest/api/3/screens/{obj_id}"

    elif kind == "screen_not_in_scheme":
        in_scheme, serr = _screen_in_any_scheme(client, obj_id)
        if in_scheme:
            log(_rec(name, "GET", "/rest/api/3/screenscheme", 200, True,
                     error=("skipped — screen is now used by a screen scheme"
                            if serr is None
                            else f"skipped — could not confirm orphaned ({serr})")))
            return "still_open"
        delete_path = f"/rest/api/3/screens/{obj_id}"

    elif kind == "workflow_unreferenced":
        referenced, werr = _workflow_referenced(client, name)
        if referenced:
            log(_rec(name, "GET", "/rest/api/3/workflowscheme", 200, True,
                     error=("skipped — workflow is now referenced by a scheme"
                            if werr is None
                            else f"skipped — could not confirm unreferenced ({werr})")))
            return "still_open"
        delete_path = f"/rest/api/3/workflow/{obj_id}"

    elif kind == "unused_custom_field":
        # (a) on-no-screen re-verify: scan every screen's fields for this id.
        # This is the heaviest TOCTOU (one tabs+fields read per screen, 50-200
        # screens), so the per-screen reads run CONCURRENTLY. Determinism is
        # preserved: results come back in screen-list order and the first one
        # that errors OR shows the field decides the outcome — byte-identical to
        # the old sequential early-return, just faster. (The inner per-tab loop
        # stays sequential: a screen has only a handful of tabs, and a third pool
        # nesting level would multiply threads for negligible gain.)
        screens, serr = client.paginate_start_at("/rest/api/3/screens", key="values")
        if serr:
            log(_rec(name, "GET", "/rest/api/3/screens", 200, True,
                     error=f"skipped — could not confirm off-screen ({serr})"))
            return "still_open"
        scan = [sc for sc in (screens or []) if sc.get("id") is not None]
        # Use the APPLY pool width, not the (wider) gather width: this scan nests
        # inside an apply task, so honouring apply_worker_count() bounds the total
        # live thread count at apply_worker_count² and keeps the "gentle apply
        # pool" contract instead of inheriting the 10-wide read knob.
        scan_results = map_results(
            scan, lambda sc: _screen_field_ids(client, sc.get("id")),
            apply_worker_count())
        for sc, res in zip(scan, scan_results):
            sid = sc.get("id")
            if isinstance(res, Exception):
                log(_rec(name, "GET", f"/rest/api/3/screens/{sid}/tabs", 200, True,
                         error=f"skipped — could not confirm off-screen ({res})"))
                return "still_open"
            ids, ferr = res
            if ferr:
                log(_rec(name, "GET", f"/rest/api/3/screens/{sid}/tabs", 200, True,
                         error=f"skipped — could not confirm off-screen ({ferr})"))
                return "still_open"
            if str(obj_id) in ids:
                log(_rec(name, "GET", f"/rest/api/3/screens/{sid}/tabs", 200, True,
                         error="skipped — field is now on a screen"))
                return "still_open"
        # (b) VALUE CHECK: a field holding data on any issue is never deleted.
        vcount, verr = _field_value_count(client, obj_id, name)
        if vcount > 0:
            log(_rec(name, "POST", "/rest/api/3/search/approximate-count", 200, True,
                     error=(f"skipped — field has values on {vcount} issue(s), "
                            f"review manually"
                            if verr is None
                            else f"skipped — could not confirm zero values ({verr})")))
            return "still_open"
        # (c) FILTER REFERENCE CHECK: a field used in a saved filter's JQL must
        # not be deleted (it would silently break the filter). Scope is saved
        # filters only, matched by the canonical cf[id] token or the current
        # field name — references from boards/dashboards/automation rules, or a
        # filter that stores an OLD field name after a rename, are NOT detected
        # (the residual is documented; this closes the most common case).
        fref, ferr = _field_in_any_filter(client, obj_id, name)
        if fref:
            log(_rec(name, "GET", "/rest/api/3/filter/search", 200, True,
                     error=("skipped — field is referenced by a saved filter's "
                            "JQL; remove it from the filter first"
                            if ferr is None
                            else f"skipped — could not confirm no filter "
                                 f"references ({ferr})")))
            return "still_open"
        delete_path = f"/rest/api/3/field/{obj_id}"

    elif kind == "empty_project":
        icount, ierr = _project_issue_count(client, name)
        if icount > 0:
            log(_rec(name, "POST", "/rest/api/3/search/approximate-count", 200, True,
                     error=(f"skipped — project now has {icount} issue(s)"
                            if ierr is None
                            else f"skipped — could not confirm empty ({ierr})")))
            return "still_open"
        delete_path = f"/rest/api/3/project/{name}"

    elif kind == "status_not_in_workflow":
        # (a) TOCTOU re-verify: the status must STILL be in no workflow. If any
        # workflow now lists it (or the check is unreadable), abort.
        in_wf, werr = _status_in_any_workflow(client, name)
        if in_wf:
            log(_rec(name, "GET", "/rest/api/3/workflow/search", 200, True,
                     error=("skipped — status is now used by a workflow"
                            if werr is None
                            else f"skipped — could not confirm unused ({werr})")))
            return "still_open"
        # (b) VALUE CHECK: a status that currently holds issues is never deleted
        # — deleting it would lose those issues' state. Abort on >0 OR error.
        scount, serr = _status_issue_count(client, name)
        if scount > 0:
            log(_rec(name, "POST", "/rest/api/3/search/approximate-count", 200, True,
                     error=(f"skipped — {scount} issue(s) currently sit in this "
                            f"status, review manually"
                            if serr is None
                            else f"skipped — could not confirm zero issues ({serr})")))
            return "still_open"
        # Cloud status delete is a query-param DELETE on the bulk endpoint.
        delete_path = "/rest/api/3/statuses"
        delete_params = {"id": obj_id}

    else:  # unreachable
        return "still_open"

    # 3b) Circuit-breaker gate — never add a DELETE to a server-side failure storm.
    if breaker.should_block():
        log(_rec(name, "SKIP", delete_path, 0, True, error=_BREAKER_SKIP_MSG))
        return "still_open"

    # 3c) DRY RUN: every guard above has passed (the object resolves, is not a
    # built-in, and the TOCTOU/value checks confirm it is safe to delete). Record
    # the intent and stop — no write, no breaker mutation, no closure re-read.
    if dry_run:
        log(_rec(name, "WOULD-DELETE", delete_path, 0, True,
                 error="dry run — verified safe to delete; not deleted",
                 snapshot=item))
        return "would_close"

    # 4) DELETE against the freshly-resolved id/key. A DELETE that returns a 3xx
    # (the Cloud custom-field delete answers 303 with an async task location) is
    # accepted, not a failure — only a 4xx/5xx is an error. The status delete is
    # keyed by the ?id= query param; everything else by a path id/key.
    st, d = client.req(delete_path, method="DELETE", params=delete_params)
    breaker.record(st)
    # 2xx/3xx (incl. the 303 async-delete) is success; st < 200 (notably the
    # client's -1 transport-failure return) is NOT a delete that happened.
    ok = 200 <= st < 400
    log(_rec(name, "DELETE", delete_path, st, ok,
             error=None if ok else str(d)[:200], snapshot=item))
    if not ok:
        return "still_open"

    # 5) Prove closure: re-read the live list and confirm the object is gone.
    if kind in ("empty_screen", "screen_not_in_scheme"):
        gone = _resolve_named(client, list_path, name)[0] == _RESOLVE_ABSENT
    elif kind == "workflow_unreferenced":
        gone = _resolve_workflow(client, name)[0] == _RESOLVE_ABSENT
    elif kind == "unused_custom_field":
        gone = _resolve_field(client, name)[0] == _RESOLVE_ABSENT
    elif kind == "empty_project":
        gone = _resolve_named(client, list_path, name,
                              id_key="id", name_key="key")[0] == _RESOLVE_ABSENT
    elif kind == "status_not_in_workflow":
        gone = _resolve_status(client, name)[0] == _RESOLVE_ABSENT
    else:
        gone = True

    if gone:
        return "closed"
    log(_rec(name, "GET", list_path, 200, False, error="still present after delete"))
    return "still_open"


def _apply_one(client, finding, log, breaker, dry_run=False):
    """Process ONE finding end-to-end and emit its log records via `log`. Returns:
      "closed"      — gone now (deleted, or already absent: idempotent)
      "would_close" — dry_run only: every guard passed, a real apply WOULD delete
      "still_open"  — not deleted (error / ambiguous / in-use / protected /
                      breaker-open / delete-failed / still-present)
      "ignore"      — not an app-tier kind; no action and no count (silent skip)

    A PURE per-finding unit: it shares nothing across findings except the
    thread-safe `breaker`, so the driver can run many concurrently and merge the
    returned verdict + replayed records deterministically (input order). Every
    safety layer (tier re-derivation, scope gate, name-collision resolve,
    built-in protection, TOCTOU re-verify, value-check, breaker gate, closure
    re-read) lives inside this unit, so concurrency cannot weaken any of them."""
    kind = finding.get("kind") or ""
    name = finding.get("name") or ""

    # --- tier re-derivation (I4): never trust client; derive from registry ---
    fix_entry = _FIXES.get(kind)
    if fix_entry is None or fix_entry.get("tier") != "app":
        # Not an app-tier kind — skip silently (the route layer will have
        # filtered these, but this is a defence-in-depth check).
        return "ignore"

    if kind not in _APP_TIER_SCOPE:
        # Belt-and-suspenders: if a future app-tier kind gets added to _FIXES
        # before this module is updated, skip safely.
        log(_rec(name, "SKIP", "-", 0, False,
                 error=f"kind {kind!r} not in apply scope"))
        return "still_open"

    # --- expanded cleanup deletes (screen/workflow/field/project) take a
    # dedicated resolver + built-in protection + per-kind TOCTOU path ---
    if kind in _EXPANDED_KINDS:
        return _apply_expanded(client, kind, name, log, breaker, dry_run=dry_run)

    spec = _KIND_SPEC[kind]

    # --- resolve the live object by name (H2/M2 name-collision safety) ---
    # This re-reads the live list and yields the FRESH id; we never trust a
    # stale id or the name alone for the DELETE target.
    outcome, item, obj_id, detail = _resolve_live(client, spec, name)

    if outcome == _RESOLVE_ERROR:
        log(_rec(name, "GET", spec["list_path"], 0, False,
                 error=f"resolve failed: {detail}"))
        return "still_open"

    if outcome == _RESOLVE_ABSENT:
        log(_rec(name, "GET", spec["list_path"], 200, True,
                 error="already absent"))
        return "closed"    # idempotent: already gone counts as closed

    if outcome == _RESOLVE_AMBIGUOUS:
        log(_rec(name, "GET", spec["list_path"], 200, True,
                 error=f"skipped — name is ambiguous ({detail} matches)"))
        return "still_open"    # cannot safely target one of several same-named

    # outcome == _RESOLVE_OK: exactly one live match with a fresh id.

    # --- C1/C2: re-verify the precondition against LIVE state before any
    # DELETE. Never delete on stale audit data. ---
    if spec["group_delete"]:
        members, uerr = _group_member_count(client, obj_id)
        if members > 0:
            log(_rec(name, "GET", "/rest/api/3/group/member", 200, True,
                     error=(f"skipped — group now has {members} member(s)"
                            if uerr is None
                            else f"skipped — could not confirm empty ({uerr})")))
            return "still_open"
        # A memberless group can still GRANT access via a permission scheme;
        # deleting it would silently revoke that grant (unrestorable by
        # re-creating the group). Skip if it holds any scheme grant.
        granted, gerr = _group_grants_in_permission_scheme(client, obj_id, name)
        if granted:
            log(_rec(name, "GET", "/rest/api/3/permissionscheme", 200, True,
                     error=("skipped — group holds a permission-scheme grant; "
                            "remove it from the scheme first"
                            if gerr is None
                            else f"skipped — could not confirm no scheme "
                                 f"grants ({gerr})")))
            return "still_open"
    else:
        in_use, uerr = _scheme_in_use(client, spec, obj_id, item)
        if in_use > 0:
            log(_rec(name, "GET", spec["list_path"], 200, True,
                     error=(f"skipped — now in use by {in_use} project(s)"
                            if uerr is None
                            else f"skipped — could not confirm unused ({uerr})")))
            return "still_open"

    # --- L2: circuit-breaker — do not add to a server-side failure storm ---
    if breaker.should_block():
        log(_rec(name, "SKIP", spec["delete_path"], 0, True,
                 error=_BREAKER_SKIP_MSG))
        return "still_open"

    # --- perform the DELETE against the freshly-fetched id ---
    if spec["group_delete"]:
        # Groups: DELETE /rest/api/3/group?groupId={id}
        path = spec["delete_path"]
        del_params = {"groupId": obj_id}
    else:
        path = spec["delete_path"].format(id=obj_id)
        del_params = None

    # DRY RUN: all guards passed; record the intent and stop (no write).
    if dry_run:
        log(_rec(name, "WOULD-DELETE", path, 0, True,
                 error="dry run — verified safe to delete; not deleted",
                 snapshot=item))
        return "would_close"

    st, d = client.req(path, method="DELETE", params=del_params)
    breaker.record(st)

    # Only a real 2xx is a delete that landed; st < 200 (the client's -1
    # transport-failure return) is NOT success — the closure re-read below would
    # also catch a falsely-ok delete, but the log must not claim it either.
    ok = 200 <= st < 300
    log(_rec(name, "DELETE", path, st, ok,
             error=None if ok else str(d)[:200], snapshot=item))

    if not ok:
        return "still_open"

    # --- prove closure: re-read to confirm object is gone ---
    exists_after = _object_exists(client, spec, name)
    if exists_after:
        log(_rec(name, "GET", spec["list_path"], 200, False,
                 error="still present after delete"))
        return "still_open"
    return "closed"


def apply_env_fixes(
    client,
    findings: list[dict],
    log,
    expected_api_base: str | None = None,
    dry_run: bool = False,
    record_sink=None,
) -> tuple[int, int]:
    """Apply all app-tier findings from *findings* against *client*.

    Parameters
    ----------
    client            : JiraClient aimed at the audited source environment.
    findings          : env findings list (each must carry kind + name + detail.fix).
    log               : callable(record) — called for every API action.
    expected_api_base : when provided, the client's api_base must match exactly;
                        a mismatch raises ValueError before any write.
    dry_run           : when True, run every read-only guard (identity, cap,
                        resolve, built-in protection, TOCTOU re-verify,
                        value-check) but issue NO destructive write — each
                        would-delete is logged as a WOULD-DELETE record and
                        tallied into the first return value. A safe preview of
                        exactly what a real apply would do.
    record_sink       : optional callable(record) invoked the instant EACH
                        action record is emitted (inside the worker, before the
                        next op), with fix_id already stamped — a durable write-
                        through so a DELETE's record survives a crash that hits
                        right after the DELETE fired (review Bug 4). None keeps
                        the legacy buffer-then-finalize behaviour. Must be
                        thread-safe: parallel workers stream concurrently.

    Returns
    -------
    (closed, still_open) integers for _finalize_fix verdict logic. In dry_run the
    first value is the would-close count.
    """
    # --- identity guard (must be first, before any HTTP) ---
    if expected_api_base is not None and client.api_base != expected_api_base:
        raise ValueError(
            f"apply_env_fixes client mismatch: expected api_base "
            f"{expected_api_base!r}, got {client.api_base!r}; "
            f"writes must flow through the audited environment client only")

    # --- L1: destructive-ops hard cap (blast-radius limit, before any HTTP) ---
    # Count only findings that would actually attempt a destructive op (app-tier
    # AND in the apply scope). A selection larger than the cap aborts the WHOLE
    # batch here, before a single request — the operator must trim or raise the
    # cap intentionally.
    n_destructive = sum(1 for f in findings if _is_destructive(f))
    cap = _destructive_cap()
    if n_destructive > cap:
        raise DestructiveCapExceeded(
            f"refusing to apply {n_destructive} destructive operation(s) in one "
            f"batch — the cap is {cap}. Trim the selection or raise "
            f"MA_MAX_DESTRUCTIVE deliberately.")

    # --- L2: shared circuit-breaker for the whole batch (sequential or parallel)
    breaker = _WriteBreaker(_breaker_threshold())

    # --- Concurrency: fan the INDEPENDENT per-finding work out over a bounded
    # pool. Each task buffers its OWN log records and returns (verdict, records);
    # the main thread replays them in INPUT order, so the emitted log and the
    # closed/still_open tallies are identical to the sequential run regardless of
    # completion order — on any batch with ZERO server-side failures. (Under a
    # 5xx/429 storm the shared breaker may trip at a different finding depending
    # on timing, so WHICH findings are breaker-skipped can differ; that is the
    # intended outage behaviour, and the equivalence tests pin the clean case.)
    # A task that raises is isolated: it keeps its buffered records and surfaces
    # the exception as its verdict, never aborting a sibling. The only shared
    # state is the thread-safe breaker.
    def _task(finding):
        records: list = []
        # fix_id is the fix-log's NOT-NULL key + the UI "Fix" column. Stamp it
        # the instant each record is emitted (not after the worker returns) so a
        # record streamed to record_sink is already complete if a crash follows.
        kind = finding.get("kind") or "env_fix"

        def emit(r):
            if not r.get("fix_id"):
                r["fix_id"] = kind
            records.append(r)
            if record_sink is not None:
                # Durable write-through, BEFORE the next op runs: a DELETE
                # already fired against prod is now recorded even on a hard crash.
                record_sink(r)

        try:
            verdict = _apply_one(client, finding, emit, breaker, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — isolate; KEEP buffered records
            # A worker crashed mid-finding. Whatever it already logged (e.g. a
            # DELETE that DID fire) is preserved AND already streamed; surface
            # the exception as the verdict for the merge loop.
            verdict = exc
        return verdict, records

    results = map_results(findings, _task, apply_worker_count())

    closed = 0
    still_open = 0
    for finding, res in zip(findings, results):
        if isinstance(res, Exception):
            # Defence in depth: _task catches internally, so this only fires if
            # the executor itself failed. Record against the finding, never swallow.
            crash = _rec(finding.get("name") or "", "ERROR", "-", 0, False,
                         error=f"apply worker crashed: {res}")
            crash["fix_id"] = finding.get("kind") or "env_fix"
            log(crash)
            # Crash records are built HERE, outside the worker, so they never
            # passed through emit() — stream them too, else the streamed DB
            # would miss them (the worker records were already streamed).
            if record_sink is not None:
                record_sink(crash)
            still_open += 1
            continue
        verdict, records = res
        for r in records:
            log(r)        # in-memory replay only; already streamed in the worker
        if isinstance(verdict, Exception):
            crash = _rec(finding.get("name") or "", "ERROR", "-", 0, False,
                         error=f"apply worker crashed: {verdict}")
            crash["fix_id"] = finding.get("kind") or "env_fix"
            log(crash)
            if record_sink is not None:
                record_sink(crash)
            still_open += 1
        elif verdict in ("closed", "would_close"):
            # would_close (dry_run) tallies with closed: both mean "nothing left
            # to do" — either already done, or the preview proved it would be.
            closed += 1
        elif verdict == "still_open":
            still_open += 1
        # "ignore" → silent skip, no count (matches the old `continue`)

    return closed, still_open
