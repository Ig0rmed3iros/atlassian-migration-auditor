"""Gather a single Jira environment's configuration into a snapshot (spec R3).

Reuses config_audit's area readers and capability gates: DC areas with no
list API are recorded {skipped:True}, never a false empty. Names/counts only —
no issue data is read here."""
from __future__ import annotations
import datetime as _dt
from typing import Callable
from ..config_audit import (SIMPLE, CLOUD_ONLY, DC_KEYS, _dc_list_sliced,
                            _names, _norm_name, _wf_name, _screen_fields,
                            _SCREEN_DEEP_CAP, _SELECT_MARKERS)
from ._pool import map_results, worker_count

_GROUPS_PROBE_CAP = 60   # member-count probe limit
_BOARD_CAP = 500
_FILTER_CAP = 500
_DASHBOARD_CAP = 500
_CUSTOM_FIELD_OPTIONS_CAP = 80

# A project whose newest issue update is older than this is "stale" (abandoned
# release/activity calendar). Atlassian's inactive-project cleanup guidance.
_STALE_PROJECT_DAYS = 365

# Share-permission types that expose a filter/dashboard beyond a specific
# group/role/user — i.e. to anyone on the web or every logged-in user. Read
# the share-permission TYPE only; never the holder identity.
_PUBLIC_SHARE_TYPES = {"global", "loggedin", "authenticated", "project-public"}

# JCMA migrates a custom field's VALUES only when its type is in Atlassian's
# built-in customfield namespace; any other namespace prefix is an app-provided
# type whose values are silently dropped on a DC->Cloud migration. The full type
# key is needed to tell them apart — by_type below keeps only the short suffix
# for display/AI, which is too lossy to classify on (review Bug 1).
_SUPPORTED_CF_NAMESPACE = "com.atlassian.jira.plugin.system.customfieldtypes:"


def _cf_type_key(field: dict) -> str:
    """A custom field's full type key, e.g.
    'com.atlassian.jira.plugin.system.customfieldtypes:select' (built-in) or
    'com.pyxis.greenhopper.jira:gh-sprint' (app-provided). '' when absent."""
    return str((field.get("schema") or {}).get("custom", ""))

# ---------------------------------------------------------------------------
# Section 3 — ISSUE-LEVEL / DATA QUALITY count-only queries (invariant I1).
#
# Each query is a count-only JQL probe issued via client.approx_count, which
# returns an INTEGER (Cloud POST /search/approximate-count -> count; DC GET
# /search?maxResults=0 -> total). The gather stores ONLY those integers.
#
# ABSOLUTE PRIVACY RULE (I1): these queries surface aggregate COUNTS of data-
# quality defects. They NEVER fetch or store issue summaries, descriptions,
# comments, field values, reporter/assignee identities, or even issue keys.
# The whole point of approx_count is that it returns a single number — we keep
# the number and nothing else. The queries are instance-wide (no project filter
# in v1). JQL is taken verbatim from the env-audit coverage catalog §3.
# ---------------------------------------------------------------------------
_ISSUE_QUALITY_QUERIES = (
    # done_unresolved (catalog #30): the single most-cited data defect —
    # Done-category issues with no resolution break Unresolved filters,
    # release warnings, velocity, and burndown.
    ("done_unresolved", "statusCategory = Done AND resolution = EMPTY"),
    # stale_open (catalog #33 shape): unresolved/open issues untouched for a
    # year — Atlassian's named archive/cleanup target.
    ("stale_open", 'statusCategory != Done AND updated <= "-365d"'),
    # unassigned_unresolved (catalog #32 variant, instance-wide): open work
    # with no owner. assignee is EMPTY -> no identity is read, count only.
    ("unassigned_unresolved", "resolution = EMPTY AND assignee is EMPTY"),
    # resolved_but_open (catalog #31, exact): the mirror defect — a resolution
    # set while the issue sits in a non-Done status.
    ("resolved_but_open", "resolution != EMPTY AND statusCategory != Done"),
    # total_unresolved: the denominator for the stale/unassigned ratios.
    ("total_unresolved", "resolution = EMPTY"),
)


def _gather_issue_quality(client):
    """Run the fixed set of count-only data-quality probes (invariant I1).

    Returns {metric: int|None, ..., error: None|str}. Each query is guarded so
    an individual failure (approx_count returns a non-int "ERR.." sentinel, or
    raises) yields None for THAT metric only — never a whole-area abort. The
    area `error` is set ONLY when EVERY query failed (the approx-count surface
    is entirely unavailable), so a fully-None area reads as unevaluable rather
    than a false clean.

    PRIVACY: approx_count returns a single integer; this stores exactly that
    integer (or None). No issue key, summary, field value, or identity is ever
    read or retained — there is nothing else in scope to leak."""
    out: dict = {}
    n_ok = 0
    n_run = 0
    for metric, jql in _ISSUE_QUALITY_QUERIES:
        n_run += 1
        try:
            raw = client.approx_count(jql)
        except Exception:
            raw = None
        # approx_count yields an int on success, an "ERR.." string on a query
        # failure, or None on an exception. Keep ONLY a real integer.
        if isinstance(raw, int) and not isinstance(raw, bool):
            out[metric] = raw
            n_ok += 1
        else:
            out[metric] = None
    # Whole-area failure: every probe came back None -> the issue-search surface
    # is unavailable. Record an error so consumers treat the area as unevaluable
    # (never a false clean). A partial failure leaves error None.
    out["error"] = None if (n_ok > 0 or n_run == 0) else \
        "issue-search count surface unavailable (all data-quality probes failed)"
    return out


def _share_is_public(share_perms) -> bool:
    """True if ANY share permission opens the object to anonymous or all
    logged-in users. PRIVACY: reads the share TYPE only, never the holder."""
    for sp in (share_perms or []):
        if not isinstance(sp, dict):
            continue
        t = str(sp.get("type", "")).strip().lower()
        if t in _PUBLIC_SHARE_TYPES:
            return True
    return False


def _shared_object_item(row) -> dict:
    """Reduce a filter/dashboard row to booleans ONLY (invariant I1).

    Stores {owner_active, public}. The owner object is read for `.active` and
    then DISCARDED — no name, accountId, email, or displayName is retained.

    owner_active is TRI-STATE: True/False when an owner with an `active` flag is
    present, and None when no owner exists at all (the built-in System Dashboard
    has no owner). The owned-by-inactive check counts only `owner_active is
    False`, so an ownerless object never reads as inactive-owned — without this,
    every site's System Dashboard produced a false HIGH finding."""
    owner = row.get("owner") if isinstance(row, dict) else None
    owner_active = (bool(owner.get("active"))
                    if isinstance(owner, dict) and "active" in owner else None)
    return {"owner_active": owner_active,
            "public": _share_is_public(row.get("sharePermissions"))}


def _is_stale_last_update(last_update, issue_count) -> bool:
    """True when the project has issues AND its last issue-update timestamp is
    older than _STALE_PROJECT_DAYS. Computed with a real datetime in gather so
    the snapshot stores only the boolean — never the raw timestamp.

    A missing/unparseable timestamp or zero issue count yields False (we never
    flag a project stale on absent data — that would be a false positive)."""
    if not last_update or not issue_count:
        return False
    raw = str(last_update).strip()
    # Jira insight returns e.g. "2024-01-02T03:04:05.000+0000"; normalise the
    # trailing +0000 offset to +00:00 so fromisoformat can parse it.
    norm = raw
    if len(norm) >= 5 and (norm[-5] in "+-") and norm[-3] != ":":
        norm = norm[:-2] + ":" + norm[-2:]
    try:
        dt = _dt.datetime.fromisoformat(norm)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    age = _dt.datetime.now(_dt.timezone.utc) - dt
    return age.days > _STALE_PROJECT_DAYS


def _fetch_area(client, area, suffix, key):
    if client.conn.deployment == "dc":
        if area == "screens":
            return _dc_list_sliced(client, f"{client.api_prefix}{suffix}")
        key = DC_KEYS.get(area, key)
    return client.paginate_start_at(f"{client.api_prefix}{suffix}", key=key)


def _gather_screen_fields(client, rows, workers=1):
    """Return {screen_name: [field_name, ...]} for up to _SCREEN_DEEP_CAP screens.

    Errors per-screen are swallowed safely: a failed tab fetch produces an
    empty field list, which at worst causes a false empty_screen finding, never
    a false clean. Cloud-only — callers must skip on DC.

    The per-screen deep-fetches are INDEPENDENT, so they run ~workers-wide; the
    {name: sorted(fields)} result is keyed by name and merged in the main
    thread, so completion order never changes the output."""
    candidates = [s for s in (rows or [])[:_SCREEN_DEEP_CAP]
                  if s.get("name") is not None and s.get("id") is not None]

    def _one(screen):
        return (screen["name"], sorted(_screen_fields(client, screen["id"])))

    fields: dict = {}
    for res in map_results(candidates, _one, workers):
        if isinstance(res, Exception):
            # Isolated: a deep-fetch crash drops that one screen (empty/absent),
            # never aborts the others — same fail-safe posture as the sequential
            # per-screen swallow in _screen_fields.
            continue
        nm, flds = res
        fields[nm] = flds
    return fields


def _status_id_name_map(workflow):
    """Return {status_id: status_name} for a Cloud /workflow/search row.

    Cloud lists each workflow status as {id, name}; transitions reference
    statuses by id, so we resolve ids to names for a PII-free edge list."""
    out: dict = {}
    for s in (workflow.get("statuses") or []):
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        nm = s.get("name")
        if sid is not None and nm is not None:
            out[str(sid)] = nm
    return out


def _status_ref_name(ref, id_to_name):
    """Resolve a single transition status reference (id string, {id:...}, or a
    name-bearing object) to a status NAME via id_to_name. Returns None when the
    reference cannot be resolved."""
    if isinstance(ref, dict):
        rid = ref.get("id")
        if rid is not None and str(rid) in id_to_name:
            return id_to_name[str(rid)]
        # Some shapes carry the name directly.
        nm = ref.get("name")
        if nm in id_to_name.values():
            return nm
        return nm if isinstance(nm, str) else None
    if ref is not None and str(ref) in id_to_name:
        return id_to_name[str(ref)]
    return None


def _transition_edges(workflow):
    """Reduce a Cloud workflow's transitions to a privacy-safe edge list.

    Each edge is {to: <status name>, from: [<status name>...], global: bool}.
    PRIVACY: status NAMES + a boolean only — never transition ids, rules,
    conditions, validators, or post-functions. A transition is `global` when
    Jira types it "global" OR its from-set is empty/covers all statuses AND it
    is not the workflow's create/initial transition (which has no real source).
    """
    id_to_name = _status_id_name_map(workflow)
    all_status_names = set(id_to_name.values())
    edges: list = []
    for t in (workflow.get("transitions") or []):
        if not isinstance(t, dict):
            continue
        to_name = _status_ref_name(t.get("to"), id_to_name)
        if to_name is None:
            continue
        ttype = str(t.get("type", "")).strip().lower()
        raw_from = t.get("from")
        from_names = []
        for ref in (raw_from or []):
            nm = _status_ref_name(ref, id_to_name)
            if nm is not None:
                from_names.append(nm)
        # The create/initial transition has no real source status — it is the
        # entry point, NOT a global transition.
        is_initial = ttype == "initial"
        # global: typed global, OR an empty/all-status from-set on a
        # non-initial transition (applies from ANY status).
        covers_all = bool(all_status_names) and \
            set(from_names) >= all_status_names
        is_global = (ttype == "global") or (
            not is_initial and (not from_names or covers_all))
        edges.append({"to": to_name, "from": from_names, "global": is_global})
    return edges


def _initial_status_name(workflow, edges):
    """Best-effort initial/create status NAME: the destination of the create
    transition (type == initial), else the first listed status. Used so the
    unreachable_status check never flags the legitimate entry status."""
    for t in (workflow.get("transitions") or []):
        if isinstance(t, dict) and str(t.get("type", "")).lower() == "initial":
            nm = _status_ref_name(t.get("to"), _status_id_name_map(workflow))
            if nm is not None:
                return nm
    statuses = workflow.get("statuses") or []
    if statuses and isinstance(statuses[0], dict):
        return statuses[0].get("name")
    return None


def _gather_workflows_used(scheme_rows):
    """Return the SET (as a sorted list) of workflow names referenced by any
    workflow scheme's defaultWorkflow or issueTypeMappings. Cloud
    /workflowscheme list rows carry these inline.

    The caller treats a missing 'workflows_used' key as unevaluable (never as
    'all workflows unreferenced') — this is only called on a successful fetch."""
    used: set = set()
    for row in (scheme_rows or []):
        if not isinstance(row, dict):
            continue
        dw = row.get("defaultWorkflow")
        if isinstance(dw, str) and dw:
            used.add(dw)
        mappings = row.get("issueTypeMappings")
        if isinstance(mappings, dict):
            for wf_name in mappings.values():
                if isinstance(wf_name, str) and wf_name:
                    used.add(wf_name)
    return sorted(used)


def _gather_screens_used(scheme_rows, screen_id_to_name):
    """Return the SET (sorted list) of screen names referenced by any screen
    scheme's `screens` map values. Cloud /screenscheme references screens by id,
    resolved to names via screen_id_to_name.

    Missing key -> unevaluable (the caller guards). Only called on success."""
    used: set = set()
    for row in (scheme_rows or []):
        if not isinstance(row, dict):
            continue
        screens = row.get("screens")
        if not isinstance(screens, dict):
            continue
        for sid in screens.values():
            nm = screen_id_to_name.get(str(sid))
            if nm:
                used.add(nm)
    return sorted(used)


def _gather_projects_using(scheme_rows):
    """Return {scheme_name: [project_id, ...]} from the inline projectIds field
    that Jira Cloud API v3 includes in /workflowscheme, /screenscheme, and
    /fieldconfigurationscheme list responses.

    The caller must treat a missing 'projects_using' key as unevaluable
    (not as 'all schemes unused') — this function is only called when the
    area was fetched successfully."""
    projects_using: dict = {}
    for row in (scheme_rows or []):
        nm = row.get("name")
        if nm is None:
            continue
        projects_using[nm] = [str(pid) for pid in row.get("projectIds") or []]
    return projects_using


# DC apps whose scripted config (scripted fields, listeners, behaviours, custom
# workflow post-functions/conditions/validators) does NOT migrate to Cloud and
# must be rebuilt. Detected by app key; only the boolean is stored.
_SCRIPT_APP_KEYS = {
    "com.onresolve.jira.groovy.groovyrunner",              # ScriptRunner
    "com.googlecode.jira-suite-utilities",                 # JSU
    "com.innovalog.jmwe.jira-misc-workflow-extensions",    # JMWE
    "com.atlassian.jira.plugins.jira-workflow-sharing-plugin",
}


def _gather_plugins(client):
    """plugins area (DC only): user-installed / enabled app COUNTS + a
    script-app-present boolean. App keys (public Marketplace identifiers) are
    reduced to counts + one boolean — no key list reaches the snapshot."""
    rows, err = client.installed_plugins()
    if err and not rows:
        return {"user_installed_count": None, "enabled_count": None,
                "script_apps_present": None, "error": err}
    user = [p for p in (rows or []) if isinstance(p, dict)
            and p.get("userInstalled")]
    enabled = [p for p in user if p.get("enabled")]
    has_script = any((p.get("key") or "").lower() in _SCRIPT_APP_KEYS
                     for p in user)
    return {"user_installed_count": len(user), "enabled_count": len(enabled),
            "script_apps_present": has_script, "error": None}


def gather_config(client, project_keys, progress: Callable[[str], None] | None = None):
    say = progress or (lambda m: None)
    dc = client.conn.deployment == "dc"
    # Bounded pool width for the independent per-object/per-project reads below.
    # MA_GATHER_WORKERS overrides the default (1 == forced sequential). httpx's
    # shared client is thread-safe and carries the per-call 429/5xx backoff, so
    # the same client is reused across threads and the pool stays modest.
    _workers = worker_count()
    areas: dict = {}
    screen_rows: list = []
    # The SIMPLE areas are INDEPENDENT list-fetches — run them ~_workers-wide.
    # Skipped (DC/Cloud-only) areas need no I/O, so they are recorded directly;
    # the rest are fetched concurrently and merged back in SIMPLE order so the
    # area dict and the captured screen_rows are completion-order-independent.
    _to_fetch = [(area, suffix, key) for area, suffix, key in SIMPLE
                 if not (dc and area in CLOUD_ONLY)]
    for area, _, _ in SIMPLE:
        if dc and area in CLOUD_ONLY:
            areas[area] = {"label": area, "skipped": True,
                           "reason": "no Data Center API — verify manually"}
            say(f"[{area}] skipped (no DC API)")

    def _fetch_simple(spec):
        area, suffix, key = spec
        rows, err = _fetch_area(client, area, suffix, key)
        return (area, rows, err)

    _simple_results = map_results(_to_fetch, _fetch_simple, _workers)
    for spec, res in zip(_to_fetch, _simple_results):
        area = spec[0]
        if isinstance(res, Exception):
            # Isolated: an unexpected crash records a loud area error with no
            # rows, never aborts the sibling fetches or the gather.
            areas[area] = {"error": str(res), "names": [], "count": 0}
            say(f"[{area}] FAILED: {res}")
            continue
        _, rows, err = res
        if err:
            areas[area] = {"error": err, "names": sorted(set(_names(rows))),
                           "count": len(rows or [])}
        else:
            areas[area] = {"names": sorted(set(_names(rows))),
                           "count": len(rows or []), "error": None}
        if area == "screens":
            screen_rows = rows or []
        say(f"[{area}] {areas[area].get('count', 0)}")

    # screens: populate tab/field membership (Cloud-only; skip quietly on DC)
    if not dc and "screens" in areas and not areas["screens"].get("error"):
        areas["screens"]["fields"] = _gather_screen_fields(
            client, screen_rows, _workers)
        say(f"[screens] fields gathered for {len(areas['screens']['fields'])} screens")

    # custom fields (type-aware)
    cf, cferr = client.paginate_start_at(f"{client.api_prefix}/field")
    customs = [f for f in (cf or []) if f.get("custom") and f.get("name")]
    areas["custom_fields"] = {
        "names": sorted(f["name"] for f in customs), "count": len(customs),
        "by_type": {f["name"]: _cf_type_key(f).split(":")[-1] for f in customs},
        # Classify from the FULL type key here; downstream reads this verdict
        # instead of re-deriving from the lossy by_type suffix (review Bug 1).
        "app_provided_count": sum(
            1 for f in customs
            if _cf_type_key(f)
            and not _cf_type_key(f).startswith(_SUPPORTED_CF_NAMESPACE)),
        "error": cferr}

    # Screen id -> name map (Cloud) for resolving screen-scheme references to
    # names. Built from the raw screen rows captured in the SIMPLE loop.
    screen_id_to_name: dict = {}
    if not dc:
        for s in (screen_rows or []):
            if isinstance(s, dict) and s.get("id") is not None and s.get("name"):
                screen_id_to_name[str(s["id"])] = s["name"]

    # scheme areas: populate projects_using on Cloud (not present on DC — already skipped)
    for scheme_area in ("workflow_schemes", "screen_schemes", "field_config_schemes"):
        sa = areas.get(scheme_area)
        if sa and not sa.get("skipped") and not sa.get("error"):
            # The SIMPLE-loop pass kept only names, so re-fetch this area to read
            # projectIds per scheme. Capture the error: on a transient failure we
            # leave 'projects_using' ABSENT so the usage checks skip safely — a
            # discarded error would yield {} and flag every scheme as unused (a
            # false positive, the inverse of the never-a-false-clean contract).
            _, suffix, key = next(
                (a, s, k) for a, s, k in SIMPLE if a == scheme_area)
            rows_detail, detail_err = _fetch_area(client, scheme_area, suffix, key)
            if detail_err is None:
                sa["projects_using"] = _gather_projects_using(rows_detail)
                say(f"[{scheme_area}] projects_using gathered")
                # workflow_schemes: capture the set of workflow names referenced
                # (defaultWorkflow + issueTypeMappings) so workflow_unreferenced
                # can fire. Absent on a fetch failure -> check skips safely.
                if scheme_area == "workflow_schemes":
                    sa["workflows_used"] = _gather_workflows_used(rows_detail)
                    say("[workflow_schemes] workflows_used gathered")
                # screen_schemes: capture the set of screen names referenced by
                # each scheme's `screens` map (ids resolved to names) so
                # screen_not_in_scheme can fire. Absent on failure -> skip safe.
                if scheme_area == "screen_schemes":
                    sa["screens_used"] = _gather_screens_used(
                        rows_detail, screen_id_to_name)
                    say("[screen_schemes] screens_used gathered")
            else:
                say(f"[{scheme_area}] projects_using unavailable ({detail_err}) "
                    f"— usage checks skipped")

    # workflows (structure on cloud)
    if dc:
        wf, wferr = client.paginate_start_at(f"{client.api_prefix}/workflow")
        wf_idx: dict = {}
        for w in (wf or []):
            nm = _wf_name(w)
            if nm:
                wf_idx.setdefault(_norm_name(nm), w)
        areas["workflows"] = {"names": sorted(_wf_name(w) for w in wf_idx.values()),
                              "count": len(wf_idx),
                              "structure_checked": False, "error": wferr}
    else:
        wf, wferr = client.paginate_start_at(
            f"{client.api_prefix}/workflow/search",
            params={"expand": "transitions,statuses"})
        wf_idx = {}
        for w in (wf or []):
            nm = _wf_name(w)
            if nm:
                wf_idx.setdefault(_norm_name(nm), w)
        detail = {}
        for w in wf_idx.values():
            nm = _wf_name(w)
            edges = _transition_edges(w)
            detail[nm] = {
                "statuses": [s.get("name") for s in (w.get("statuses") or [])],
                "transitions": [t.get("name") for t in (w.get("transitions") or [])],
                # Transition GRAPH (structure only, no PII). edges resolves
                # status ids to names; initial_status pins the create entry.
                "edges": edges,
                "initial_status": _initial_status_name(w, edges),
            }
        areas["workflows"] = {"names": sorted(detail),
                              "count": len(detail),
                              "structure_checked": True,
                              "detail": detail, "error": wferr}
    say("[workflows] done")

    # ---- permission_scheme_grants (Cloud only) ---------------------------------
    if dc:
        areas["permission_scheme_grants"] = {
            "label": "permission_scheme_grants", "skipped": True,
            "reason": "no Data Center API — verify manually"}
    else:
        try:
            schemes, psg_err = client.paginate_start_at(
                f"{client.api_prefix}/permissionscheme",
                params={"expand": "permissions"},
                key="permissionSchemes")
            by_scheme: dict = {}
            total_grants = 0
            for scheme in (schemes or []):
                name = scheme.get("name")
                if not name:
                    continue
                grants = []
                for perm in (scheme.get("permissions") or []):
                    holder = perm.get("holder") or {}
                    # PRIVACY: only permission key + holder type; no parameter/value/accountId
                    grants.append({
                        "permission": perm.get("permission"),
                        "holder_type": holder.get("type"),
                    })
                by_scheme[name] = grants
                total_grants += len(grants)
            areas["permission_scheme_grants"] = {
                "by_scheme": by_scheme, "count": total_grants, "error": psg_err}
            say(f"[permission_scheme_grants] {len(by_scheme)} schemes, {total_grants} grants")
        except Exception as exc:
            areas["permission_scheme_grants"] = {
                "by_scheme": {}, "count": 0, "error": str(exc)}

    # ---- groups (Cloud only) --------------------------------------------------
    if dc:
        areas["groups"] = {
            "label": "groups", "skipped": True,
            "reason": "no Data Center API — verify manually"}
    else:
        try:
            all_groups, grp_err = client.paginate_start_at(
                f"{client.api_prefix}/group/bulk", key="values")
            all_names = [g["name"] for g in (all_groups or []) if g.get("name")]
            capped = len(all_names) > _GROUPS_PROBE_CAP
            probe_groups = [g for g in (all_groups or [])[:_GROUPS_PROBE_CAP]
                            if g.get("name") and g.get("groupId")]

            # Per-group member-count probes are INDEPENDENT — run ~_workers-wide.
            # Each task returns (name, count) or None; merged by name in the main
            # thread so completion order can't reorder member_counts.
            def _probe_group(g):
                st, md = client.req(
                    f"{client.api_prefix}/group/member",
                    params={"groupId": g["groupId"], "maxResults": 0})
                if st == 200 and isinstance(md, dict):
                    return (g["name"], md.get("total", 0))
                return None

            member_counts: dict = {}
            for res in map_results(probe_groups, _probe_group, _workers):
                # An exception OR a non-200 probe is skipped individually (same
                # as the sequential try/except + status guard) — never aborts
                # sibling probes.
                if isinstance(res, Exception) or res is None:
                    continue
                gname, total = res
                member_counts[gname] = total
            areas["groups"] = {
                "names": all_names, "count": len(all_names),
                "member_counts": member_counts, "capped": capped, "error": grp_err}
            say(f"[groups] {len(all_names)} groups, capped={capped}")
        except Exception as exc:
            areas["groups"] = {
                "names": [], "count": 0, "member_counts": {}, "capped": False,
                "error": str(exc)}

    # ---- components (Cloud + DC) ---------------------------------------------
    # Per-project reads are INDEPENDENT — run them ~worker_count()-wide. The
    # merge below is order-free: results come back in project_keys order, are
    # keyed by project key, and the error is the LAST errored project in key
    # order (identical to the sequential last-writer-wins), so completion order
    # never changes the snapshot.
    try:
        def _fetch_components(key):
            st, comps = client.req(f"{client.api_prefix}/project/{key}/components")
            if st != 200:
                return (key, None, f"ERR{st} on {key}")
            if not isinstance(comps, list):
                return (key, None, f"ERRshape on {key}")
            entries = []
            for c in comps:
                # PRIVACY: name, has_lead (bool), assignee_type — no lead identity
                entries.append({
                    "name": c.get("name"),
                    "has_lead": bool(c.get("lead")),
                    "assignee_type": c.get("assigneeType"),
                })
            return (key, entries, None)

        by_project_comp: dict = {}
        total_comp = 0
        comp_err = None
        for res in map_results(list(project_keys), _fetch_components, _workers):
            if isinstance(res, Exception):
                # A task that raised: surface as a loud error (last-writer-wins,
                # matching how an exception inside the sequential loop bubbled to
                # the area try/except — here it stays scoped to its own project).
                comp_err = str(res)
                continue
            key, entries, err = res
            if err is not None:
                comp_err = err
                continue
            by_project_comp[key] = entries
            total_comp += len(entries)
        areas["components"] = {
            "by_project": by_project_comp, "count": total_comp, "error": comp_err}
        say(f"[components] {total_comp}")
    except Exception as exc:
        areas["components"] = {"by_project": {}, "count": 0, "error": str(exc)}

    # ---- versions (Cloud + DC) -----------------------------------------------
    # Same independent per-project pattern as components: ~worker_count()-wide,
    # merged in project_keys order so the result + last-error are deterministic.
    try:
        def _fetch_versions(key):
            st, vers = client.req(f"{client.api_prefix}/project/{key}/versions")
            if st != 200:
                return (key, None, f"ERR{st} on {key}")
            if not isinstance(vers, list):
                return (key, None, f"ERRshape on {key}")
            entries = []
            for v in vers:
                # PRIVACY: name, booleans only — no releaseDate, description, creator
                entries.append({
                    "name": v.get("name"),
                    "released": bool(v.get("released")),
                    "archived": bool(v.get("archived")),
                    "overdue": bool(v.get("overdue", False)),
                })
            return (key, entries, None)

        by_project_ver: dict = {}
        total_ver = 0
        ver_err = None
        for res in map_results(list(project_keys), _fetch_versions, _workers):
            if isinstance(res, Exception):
                ver_err = str(res)
                continue
            key, entries, err = res
            if err is not None:
                ver_err = err
                continue
            by_project_ver[key] = entries
            total_ver += len(entries)
        areas["versions"] = {
            "by_project": by_project_ver, "count": total_ver, "error": ver_err}
        say(f"[versions] {total_ver}")
    except Exception as exc:
        areas["versions"] = {"by_project": {}, "count": 0, "error": str(exc)}

    # ---- custom_field_options (Cloud only) -----------------------------------
    if dc:
        areas["custom_field_options"] = {
            "label": "custom_field_options", "skipped": True,
            "reason": "no Data Center API — verify manually"}
    else:
        try:
            # Reuse already-fetched customs list (cf / customs computed above)
            select_fields = [
                f for f in (customs or [])
                if any(m in str((f.get("schema") or {}).get("custom", ""))
                       for m in _SELECT_MARKERS)
            ]
            cfo_capped = len(select_fields) > _CUSTOM_FIELD_OPTIONS_CAP
            select_fields = select_fields[:_CUSTOM_FIELD_OPTIONS_CAP]

            # Each field's contexts+options fetch is INDEPENDENT of the others —
            # run ~_workers-wide. A task returns (name, {contexts,options}, err)
            # where err is the LAST error within that field (context or option).
            # Merging in select_fields order and overwriting cfo_err on each
            # non-None field error reproduces the sequential last-writer-wins
            # exactly: the overall last error == the last errored field's last
            # error, in field order.
            def _field_options(f):
                fid = f["id"]
                fname = f["name"]
                ctx_list, ctx_err = client.paginate_start_at(
                    f"{client.api_prefix}/field/{fid}/context")
                if ctx_err:
                    # Sentinel 0/0 — consumers MUST guard on the area error flag,
                    # not on individual field counts.
                    return (fname, {"contexts": 0, "options": 0}, ctx_err)
                ctx_count = len(ctx_list or [])
                opt_count = 0
                field_err = None
                for ctx in (ctx_list or []):
                    cid = ctx.get("id")
                    if cid is None:
                        continue
                    opts, opt_err = client.paginate_start_at(
                        f"{client.api_prefix}/field/{fid}/context/{cid}/option")
                    if opt_err:
                        field_err = opt_err  # last option error within this field
                    opt_count += len(opts or [])
                return (fname, {"contexts": ctx_count, "options": opt_count},
                        field_err)

            by_field_opts: dict = {}
            cfo_err = None
            for res in map_results(select_fields, _field_options, _workers):
                if isinstance(res, Exception):
                    # An unexpected crash in a field task surfaces as the area
                    # error (scoped to this area; siblings already completed).
                    cfo_err = str(res)
                    continue
                fname, counts, field_err = res
                by_field_opts[fname] = counts
                if field_err:
                    cfo_err = field_err  # last-writer-wins in field order
            areas["custom_field_options"] = {
                "by_field": by_field_opts, "capped": cfo_capped, "error": cfo_err}
            say(f"[custom_field_options] {len(by_field_opts)} fields, capped={cfo_capped}")
        except Exception as exc:
            areas["custom_field_options"] = {
                "by_field": {}, "capped": False, "error": str(exc)}

    # ---- boards (Cloud + DC) -------------------------------------------------
    try:
        board_rows, board_err = client.paginate_start_at(
            "/rest/agile/1.0/board", cap=_BOARD_CAP)
        board_names = [b["name"] for b in (board_rows or []) if b.get("name")]
        board_capped = len(board_rows or []) >= _BOARD_CAP  # conservative: flag at cap boundary
        areas["boards"] = {
            "names": board_names, "count": len(board_names),
            "capped": board_capped, "error": board_err}
        say(f"[boards] {len(board_names)}, capped={board_capped}")
    except Exception as exc:
        areas["boards"] = {"names": [], "count": 0, "capped": False, "error": str(exc)}

    # ---- projects activity (Cloud + DC) --------------------------------------
    # Cloud: GET /project/search?expand=insight gives totalIssueCount +
    # lastIssueUpdateTime (dates + counts only, privacy-safe). DC: GET /project
    # has no insight, so issue_count is None and stale is False.
    # PRIVACY (I1): store ONLY {key: {issue_count, stale}} — never the project
    # lead identity, name, or the raw lastIssueUpdateTime timestamp.
    try:
        if dc:
            proj_rows, proj_err = client.paginate_start_at(
                f"{client.api_prefix}/project")
        else:
            proj_rows, proj_err = client.paginate_start_at(
                f"{client.api_prefix}/project/search",
                params={"expand": "insight"})
        by_project_act: dict = {}
        for pr in (proj_rows or []):
            if not isinstance(pr, dict):
                continue
            key = pr.get("key")
            if not key:
                continue
            insight = pr.get("insight") if isinstance(pr.get("insight"), dict) else None
            if insight is not None:
                ic = insight.get("totalIssueCount")
                issue_count = int(ic) if isinstance(ic, int) else None
                stale = _is_stale_last_update(
                    insight.get("lastIssueUpdateTime"), issue_count)
            else:
                # DC (or no insight expand): no activity data available.
                issue_count, stale = None, False
            by_project_act[key] = {"issue_count": issue_count, "stale": stale}
        areas["projects"] = {
            "by_project": by_project_act, "count": len(by_project_act),
            "error": proj_err}
        say(f"[projects] {len(by_project_act)}")
    except Exception as exc:
        areas["projects"] = {"by_project": {}, "count": 0, "error": str(exc)}

    # ---- filters (Cloud + DC) ------------------------------------------------
    # Cloud: expand owner + sharePermissions, then reduce each filter to
    # {owner_active, public} booleans ONLY (invariant I1). DC: count-only —
    # the expand surface is unreliable, so we keep the legacy count contract.
    try:
        if dc:
            filter_rows, filter_err = client.paginate_start_at(
                f"{client.api_prefix}/filter/search", cap=_FILTER_CAP)
            filter_count = len(filter_rows or [])
            filter_capped = filter_count >= _FILTER_CAP
            areas["filters"] = {
                "count": filter_count, "capped": filter_capped, "error": filter_err}
        else:
            filter_rows, filter_err = client.paginate_start_at(
                f"{client.api_prefix}/filter/search",
                params={"expand": "owner,sharePermissions"}, cap=_FILTER_CAP)
            filter_count = len(filter_rows or [])
            filter_capped = filter_count >= _FILTER_CAP
            areas["filters"] = {
                "count": filter_count, "capped": filter_capped,
                "items": [_shared_object_item(r) for r in (filter_rows or [])],
                "error": filter_err}
        say(f"[filters] {filter_count}, capped={filter_capped}")
    except Exception as exc:
        areas["filters"] = {"count": 0, "capped": False, "error": str(exc)}

    # ---- dashboards (Cloud + DC) ---------------------------------------------
    # Cloud: same {owner_active, public} reduction as filters. DC: count-only.
    try:
        dash_rows, dash_err = client.paginate_start_at(
            f"{client.api_prefix}/dashboard",
            key="dashboards", cap=_DASHBOARD_CAP)
        dash_count = len(dash_rows or [])
        dash_capped = dash_count >= _DASHBOARD_CAP  # conservative: flag at cap boundary
        if dc:
            areas["dashboards"] = {
                "count": dash_count, "capped": dash_capped, "error": dash_err}
        else:
            areas["dashboards"] = {
                "count": dash_count, "capped": dash_capped,
                "items": [_shared_object_item(r) for r in (dash_rows or [])],
                "error": dash_err}
        say(f"[dashboards] {dash_count}, capped={dash_capped}")
    except Exception as exc:
        areas["dashboards"] = {"count": 0, "capped": False, "error": str(exc)}

    # ---- extend projects_using: issuetype_schemes + issuetype_screen_schemes --
    # Cloud only (same gate as the scheme_area loop above).
    if not dc:
        for area_name, proj_path, scheme_key, proj_key in (
            ("issuetype_schemes",
             f"{client.api_prefix}/issuetypescheme/project",
             "issueTypeScheme", "projectIds"),
            ("issuetype_screen_schemes",
             f"{client.api_prefix}/issuetypescreenscheme/project",
             "issueTypeScreenScheme", "projectIds"),
        ):
            sa = areas.get(area_name)
            if sa and not sa.get("skipped") and not sa.get("error"):
                rows, pu_err = client.paginate_start_at(proj_path)
                if pu_err is None:
                    pu: dict = {}
                    for row in (rows or []):
                        scheme_obj = row.get(scheme_key) or {}
                        name = scheme_obj.get("name")
                        if not name:
                            continue
                        pu[name] = [str(pid) for pid in row.get(proj_key) or []]
                    sa["projects_using"] = pu
                    say(f"[{area_name}] projects_using gathered")
                else:
                    say(f"[{area_name}] projects_using unavailable ({pu_err})")

    # ---- issue_quality: Section-3 count-only data-quality probes (Cloud + DC)
    # Runs a small, fixed set of approx-count JQL queries (invariant I1: counts
    # ONLY — never issue content, keys, or identities). The whole area is
    # guarded so any failure records an error and continues, and each query is
    # guarded so a single failure yields None for that metric, not an abort.
    try:
        areas["issue_quality"] = _gather_issue_quality(client)
        iq = areas["issue_quality"]
        say(f"[issue_quality] done_unresolved={iq.get('done_unresolved')} "
            f"total_unresolved={iq.get('total_unresolved')} "
            f"error={iq.get('error')}")
    except Exception as exc:
        # Defensive: the whole area must never crash gather. Record the error
        # with all metrics None so consumers treat it as unevaluable.
        areas["issue_quality"] = {
            "done_unresolved": None, "stale_open": None,
            "unassigned_unresolved": None, "resolved_but_open": None,
            "total_unresolved": None, "error": str(exc)}

    # plugins (Data Center / Server ONLY): installed-app inventory for migration
    # assessment. Cloud has no UPM endpoint and Cloud apps are assessed via the
    # Marketplace, not this audit, so Cloud is skipped (a coverage signal, not a
    # false clean). Reduced to counts + one boolean — no app-key list is stored.
    if dc:
        try:
            areas["plugins"] = _gather_plugins(client)
        except Exception as exc:  # noqa: BLE001
            areas["plugins"] = {"user_installed_count": None,
                                "enabled_count": None,
                                "script_apps_present": None, "error": str(exc)}
    else:
        areas["plugins"] = {"label": "plugins", "skipped": True,
                            "reason": "Cloud apps are assessed via the "
                                      "Marketplace, not this audit"}

    return {"deployment": client.conn.deployment, "projects": list(project_keys),
            "areas": areas}
