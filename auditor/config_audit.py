"""Instance configuration parity audit (port of config_audit.py + config_fix.py).

Compares EVERY config object by NAME (IDs are re-minted by migration):
simple dimensions, custom fields (type + select options), workflows
(structure), screens (deep field check, capped), and JSM request types +
queues per selected project. Emits spec-shaped config findings.

Deployment honesty (spec R4): each side speaks its own dialect via
``client.api_prefix`` (SIMPLE holds path SUFFIXES). Areas with no Data
Center list API are reported as explicitly ``skipped`` — never silently
absent, never false findings. Deep checks that only Cloud can answer
(select options, workflow structure) are gated and the gate is recorded
in the area summary (``options_checked`` / ``structure_checked``).
"""
from __future__ import annotations

from typing import Callable

from .client import ClientError, JiraClient
from auditor.envaudit._pool import map_results

SIMPLE = [
    ("statuses", "/status", None),
    ("issue_types", "/issuetype", None),
    ("priorities", "/priority", None),
    ("resolutions", "/resolution", None),
    ("link_types", "/issueLinkType", "issueLinkTypes"),
    ("roles", "/role", None),
    ("screens", "/screens", None),
    ("screen_schemes", "/screenscheme", None),
    ("issuetype_screen_schemes", "/issuetypescreenscheme", None),
    ("workflow_schemes", "/workflowscheme", None),
    ("issuetype_schemes", "/issuetypescheme", None),
    ("field_configurations", "/fieldconfiguration", None),
    ("field_config_schemes", "/fieldconfigurationscheme", None),
    ("permission_schemes", "/permissionscheme", "permissionSchemes"),
    ("notification_schemes", "/notificationscheme", None),
]
# Verified against the DC 11.3 OpenAPI path inventory: DC has a list-all REST
# endpoint for NONE of these (/workflowscheme on DC is per-id/per-project
# only). Skipped loudly when either side is dc.
CLOUD_ONLY = {"screen_schemes", "issuetype_screen_schemes", "workflow_schemes",
              "field_configurations", "field_config_schemes"}
# DC wraps some list responses differently than Cloud's {values,isLast}
# envelope; per-area array key overrides consulted when a side is dc.
DC_KEYS = {"issuetype_schemes": "schemes"}
_SELECT_MARKERS = ("select", "radio", "checkbox", "cascading")
_SCREEN_DEEP_CAP = 60
_OPTION_CONTEXT_CAP = 3


def _slice_key(x):
    """Replay-guard identity for a sliced row: id when present, else name,
    else the row's repr. Id-less rows must not all collapse onto one key —
    a second page of NEW rows would read as a replay and be silently
    dropped (a truncation the area summary then renders as clean)."""
    if isinstance(x, dict):
        if "id" in x:
            return x["id"]
        if "name" in x:
            return x["name"]
    return repr(x)


def _dc_list_sliced(client: JiraClient, path: str) -> tuple[list, str | None]:
    """Paginate a DC endpoint that returns PLAIN ARRAY SLICES (e.g.
    /rest/api/2/screens): startAt/maxResults are honored but there is no
    envelope and no isLast, so paginate_start_at's list branch would silently
    keep only the first page. Slice forward until a short chunk; the seen-ids
    guard stops endpoints that ignore startAt and replay the full list
    (otherwise an infinite loop). Mirrors paginate_start_at's loud-truncation
    posture on mid-loop failures; a 200 whose body is not a list is an error
    too (ERRshape), never a clean empty."""
    out: list = []
    seen: set = set()
    start = 0
    while True:
        st, d = client.req(path, params={"startAt": start, "maxResults": 50})
        if st != 200:
            err = (f"ERR{st}:truncated" if out
                   else f"ERR{st}:{str(d.get('_error', ''))[:60]}")
            return out, err
        if not isinstance(d, list):
            err = ("ERRshape:truncated" if out
                   else f"ERRshape:expected list, got {type(d).__name__}")
            return out, err
        new = [x for x in d if _slice_key(x) not in seen]
        if not new:
            return out, None
        seen.update(_slice_key(x) for x in new)
        out += new
        if len(d) < 50:
            return out, None
        start += len(d)


def _names(items, fn=lambda x: x.get("name")):
    return [fn(i) for i in (items or []) if fn(i)]


_MIGRATED_SUFFIX = "(migrated)"


def _norm_name(s: str) -> str:
    """Conservative match key for a config object name.

    Lowercases, collapses internal whitespace, and strips a single trailing
    " (migrated)" token left by the migration tool (case-insensitive, with
    optional surrounding spaces). Conservative ONLY — no fuzzy matching, so
    genuinely different names never collide. Used purely as a MATCH KEY; the
    original name is always kept for display.
    """
    norm = " ".join((s or "").split()).lower()
    if norm.endswith(_MIGRATED_SUFFIX):
        norm = " ".join(norm[: -len(_MIGRATED_SUFFIX)].split())
    return norm


def _norm_index(names):
    """Map normalized-key -> first original name, preserving input order.

    Two names that normalize equal de-dupe to a single key; the first original
    encountered wins for display. ``names`` may be any iterable of strings
    (including dict keys).
    """
    idx: dict = {}
    for original in names:
        if original is None:
            continue
        k = _norm_name(original)
        idx.setdefault(k, original)
    return idx


def _summary(label, s_names, t_names):
    # Match on normalized keys so a target object renamed to "<name> (migrated)"
    # or differing only by case/whitespace is NOT double-counted as both
    # source-only and target-only. Keep ORIGINAL names for display.
    s_idx = _norm_index(s_names)
    t_idx = _norm_index(t_names)
    s_keys, t_keys = set(s_idx), set(t_idx)
    source_only = sorted(s_idx[k] for k in (s_keys - t_keys))
    target_only = sorted(t_idx[k] for k in (t_keys - s_keys))
    return {"label": label, "src": len(s_keys), "tgt": len(t_keys),
            "in_both": len(s_keys & t_keys), "source_only": source_only,
            "target_only_count": len(target_only),
            "target_only": target_only}


def _field_options(client: JiraClient, fid: str) -> set:
    opts = []
    # Discarded pagination errors here err safe: an outage -> empty set -> at
    # worst a false option mismatch that over-reports to GAPS_FOUND, never a
    # false CLEAN. v1-acceptable.
    ctx, _ = client.paginate_start_at(f"{client.api_prefix}/field/{fid}/context")
    for c in (ctx or [])[:_OPTION_CONTEXT_CAP]:
        o, _ = client.paginate_start_at(
            f"{client.api_prefix}/field/{fid}/context/{c['id']}/option")
        opts += _names(o or [], lambda x: x.get("value"))
    return set(opts)


def _screen_fields(client: JiraClient, sid) -> set:
    out = []
    # Discarded pagination error errs safe: an outage -> empty set -> at worst a
    # false field mismatch that over-reports to GAPS_FOUND, never a false CLEAN.
    # v1-acceptable. Tabs/fields endpoints exist on DC too — path via api_prefix.
    tabs, _ = client.paginate_start_at(f"{client.api_prefix}/screens/{sid}/tabs")
    for tb in (tabs or []):
        st, flds = client.req(
            f"{client.api_prefix}/screens/{sid}/tabs/{tb['id']}/fields")
        if st == 200 and isinstance(flds, list):
            out += _names(flds)
    return set(out)


def _wf_name(w):
    return (w.get("id") or {}).get("name") if isinstance(w.get("id"), dict) \
        else w.get("name")


# --- per-project scope resolution (NEVER a false clean) ----------------------
# Resolving the schemes a SELECTED project actually uses lets the workflows and
# screens areas compare only what those projects touch, instead of the whole
# instance. The cardinal rule (mirroring the rest of this module): scoping may
# only ever NARROW over-reporting — it must NEVER hide a real gap. So every
# resolver returns (names:set, ok:bool) and signals ok=False on ANY non-200,
# missing scheme, or empty result, in which case the caller FALLS BACK to the
# instance-wide list for that area (over-reporting is acceptable; a false clean
# is not). These run Cloud-only (the per-project scheme REST surface is Cloud);
# any DC side simply leaves the area instance-wide.

def _project_id(client: JiraClient, key: str):
    """Numeric project id for a key, or None on any failure. Tries the
    /project/{key} read first (cheap, exact); falls back to scanning
    all_projects(). None -> caller treats as a resolution failure (fail-safe)."""
    try:
        st, d = client.req(f"{client.api_prefix}/project/{key}")
        if st == 200 and isinstance(d, dict) and d.get("id"):
            return str(d["id"])
        rows, _err = client.all_projects()
        for r in (rows or []):
            if r.get("key") == key and r.get("id"):
                return str(r["id"])
        return None
    except Exception:  # noqa: BLE001
        return None


def _scoped_workflow_names(client: JiraClient, keys) -> tuple[set, bool]:
    """Union of workflow NAMES used by the selected projects on this side.

    Returns (names, ok). ok=False on the FIRST resolution failure for any
    project (missing id, non-200, missing scheme, empty mapping) so the caller
    falls back to instance-wide — under-reporting is never acceptable."""
    names, _scheme_names, ok = _scoped_workflow_and_scheme_names(client, keys)
    return names, ok


def _scoped_workflow_and_scheme_names(client: JiraClient, keys) -> tuple[
        set, set, bool]:
    """Resolve, per selected project on this side, BOTH the workflow NAMES the
    project uses AND the project's workflow-SCHEME name. Returns
    (workflow_names, scheme_names, ok). One pass over the shared
    /workflowscheme/project endpoint so the ``workflows`` and ``workflow_schemes``
    areas can both be scoped from a single fetch. Same no-false-clean fail-safe:
    ok=False on the FIRST failure for any project so each caller falls back to
    instance-wide for its area."""
    try:
        wf_names: set = set()
        scheme_names: set = set()
        for key in keys:
            pid = _project_id(client, key)
            if not pid:
                return wf_names, scheme_names, False
            st, d = client.req(f"{client.api_prefix}/workflowscheme/project",
                               params={"projectId": pid})
            if st != 200 or not isinstance(d, dict):
                return wf_names, scheme_names, False
            vals = d.get("values") or []
            if not vals:
                return wf_names, scheme_names, False
            scheme = (vals[0] or {}).get("workflowScheme") or {}
            sch_name = scheme.get("name")
            if sch_name:
                scheme_names.add(sch_name)
            proj_names = set()
            default = scheme.get("defaultWorkflow")
            if default:
                proj_names.add(default)
            for wf in (scheme.get("issueTypeMappings") or {}).values():
                if wf:
                    proj_names.add(wf)
            if not proj_names:
                return wf_names, scheme_names, False
            wf_names |= proj_names
        return wf_names, scheme_names, True
    except Exception:  # noqa: BLE001
        return set(), set(), False


def _scoped_screen_names(client: JiraClient, keys, global_screens) -> tuple[
        set, bool]:
    """Union of screen NAMES referenced by the selected projects on this side.

    Thin wrapper over :func:`_scoped_screen_and_scheme_names` (which also
    surfaces ITSS / screen-scheme names for the issuetype_screen_schemes and
    screen_schemes areas) returning only the screen-name set + the SCREEN-level
    ok (the deepest, strictest of the three resolutions)."""
    screen_names, _itss, _ss, _itss_ok, _ss_ok, screen_ok = \
        _scoped_screen_and_scheme_names(client, keys, global_screens)
    return screen_names, screen_ok


def _scoped_screen_and_scheme_names(client: JiraClient, keys,
                                    global_screens) -> tuple[
        set, set, set, bool, bool, bool]:
    """One pass over the project -> ITSS -> screen-scheme -> screen walk that
    surfaces THREE name sets and THREE INDEPENDENT ok flags for the selected
    projects on this side:

      * issue-type-screen-scheme NAMES (the ITSS each project uses) + itss_ok,
      * screen-scheme NAMES (every screen-scheme the project's ITSS maps to)
        + ss_ok,
      * screen NAMES (mapped from screen ids via the global /screens list)
        + screen_ok.

    Returns (screen_names, itss_names, ss_names, itss_ok, ss_ok, screen_ok).
    The walk has nested depth: a failure at depth D invalidates every area AT OR
    DEEPER than D (their ok flips False) but leaves the SHALLOWER areas valid —
    e.g. a broken /screenscheme read must NOT abandon ITSS scoping that already
    resolved cleanly. Per-project first-failure semantics still hold within each
    depth (the no-false-clean rule: any project failing an area reverts THAT
    area to instance-wide)."""
    id_to_name = {str(s.get("id")): s.get("name")
                  for s in (global_screens or []) if s.get("id") is not None}
    names: set = set()
    itss_names: set = set()
    ss_names: set = set()
    itss_ok = ss_ok = screen_ok = True
    try:
        for key in keys:
            pid = _project_id(client, key)
            if not pid:
                return set(), set(), set(), False, False, False
            st, d = client.req(
                f"{client.api_prefix}/issuetypescreenscheme/project",
                params={"projectId": pid})
            if st != 200 or not isinstance(d, dict) or not d.get("values"):
                itss_ok = ss_ok = screen_ok = False
                continue
            itss = (d["values"][0] or {}).get("issueTypeScreenScheme") or {}
            itss_id = itss.get("id")
            if itss.get("name"):
                itss_names.add(itss["name"])
            else:
                itss_ok = False
            if not itss_id:
                ss_ok = screen_ok = False
                continue
            st, d = client.req(
                f"{client.api_prefix}/issuetypescreenscheme/mapping",
                params={"issueTypeScreenSchemeId": itss_id})
            if st != 200 or not isinstance(d, dict):
                ss_ok = screen_ok = False
                continue
            ss_ids = sorted({str(m.get("screenSchemeId"))
                             for m in (d.get("values") or [])
                             if m.get("screenSchemeId") is not None})
            if not ss_ids:
                ss_ok = screen_ok = False
                continue
            scr_ids: set = set()
            proj_ss: set = set()
            walk_ok = True
            for ss in ss_ids:
                st, d = client.req(f"{client.api_prefix}/screenscheme",
                                   params={"id": ss})
                if st != 200 or not isinstance(d, dict):
                    walk_ok = False
                    break
                for row in (d.get("values") or []):
                    if row.get("name"):
                        proj_ss.add(row["name"])
                    for sid in (row.get("screens") or {}).values():
                        if sid is not None:
                            scr_ids.add(str(sid))
            if not walk_ok:
                # A broken /screenscheme read invalidates BOTH the screen-scheme
                # NAME set and the deeper screen NAME set for this side.
                ss_ok = screen_ok = False
                continue
            # screen-scheme NAME scoping needs at least one named scheme; the
            # deeper screen NAME scoping does NOT depend on scheme names.
            if proj_ss:
                ss_names |= proj_ss
            else:
                ss_ok = False
            if not scr_ids:
                screen_ok = False
                continue
            proj_names = {id_to_name[s] for s in scr_ids
                          if s in id_to_name and id_to_name[s]}
            if not proj_names:
                screen_ok = False
                continue
            names |= proj_names
        return (names, itss_names, ss_names, itss_ok, ss_ok, screen_ok)
    except Exception:  # noqa: BLE001
        return set(), set(), set(), False, False, False


def _scoped_issuetype_and_scheme_names(client: JiraClient, keys,
                                       global_issue_types) -> tuple[
        set, set, bool, bool]:
    """Per selected project on this side, resolve the issue-type-SCHEME name and
    the ISSUE-TYPE names that scheme contains. Returns
    (issuetype_scheme_names, issue_type_names, scheme_ok, types_ok) — two
    INDEPENDENT ok flags so a broken /issuetypescheme/mapping read (types) does
    NOT abandon issuetype_schemes scoping that already resolved. Per-project
    first-failure semantics within each: any project failing an area reverts
    THAT area to instance-wide."""
    id_to_name = {str(t.get("id")): t.get("name")
                  for t in (global_issue_types or [])
                  if t.get("id") is not None}
    scheme_names: set = set()
    type_names: set = set()
    scheme_ok = types_ok = True
    try:
        for key in keys:
            pid = _project_id(client, key)
            if not pid:
                return set(), set(), False, False
            st, d = client.req(f"{client.api_prefix}/issuetypescheme/project",
                               params={"projectId": pid})
            if st != 200 or not isinstance(d, dict) or not d.get("values"):
                scheme_ok = types_ok = False
                continue
            scheme = (d["values"][0] or {}).get("issueTypeScheme") or {}
            sid = scheme.get("id")
            if scheme.get("name"):
                scheme_names.add(scheme["name"])
            else:
                scheme_ok = False
            if not sid:
                types_ok = False
                continue
            st, d = client.req(f"{client.api_prefix}/issuetypescheme/mapping",
                               params={"issueTypeSchemeId": sid})
            if st != 200 or not isinstance(d, dict):
                types_ok = False
                continue
            tids = [str(m.get("issueTypeId"))
                    for m in (d.get("values") or [])
                    if m.get("issueTypeId") is not None]
            proj_types = {id_to_name[t] for t in tids
                          if t in id_to_name and id_to_name[t]}
            if not proj_types:
                types_ok = False
                continue
            type_names |= proj_types
        return scheme_names, type_names, scheme_ok, types_ok
    except Exception:  # noqa: BLE001
        return set(), set(), False, False


def _scoped_fieldconfig_and_scheme_names(client: JiraClient, keys,
                                         global_field_configs) -> tuple[
        set, set, bool, bool]:
    """Per selected project on this side, resolve the field-configuration-SCHEME
    name and the FIELD-CONFIGURATION names that scheme contains. Returns
    (fieldconfig_scheme_names, fieldconfig_names, scheme_ok, configs_ok) — two
    INDEPENDENT ok flags so a broken /fieldconfigurationscheme/mapping read
    (configs) does NOT abandon field_config_schemes scoping that already
    resolved. Same no-false-clean per-project first-failure posture."""
    id_to_name = {str(c.get("id")): c.get("name")
                  for c in (global_field_configs or [])
                  if c.get("id") is not None}
    scheme_names: set = set()
    config_names: set = set()
    scheme_ok = configs_ok = True
    try:
        for key in keys:
            pid = _project_id(client, key)
            if not pid:
                return set(), set(), False, False
            st, d = client.req(
                f"{client.api_prefix}/fieldconfigurationscheme/project",
                params={"projectId": pid})
            if st != 200 or not isinstance(d, dict) or not d.get("values"):
                scheme_ok = configs_ok = False
                continue
            scheme = (d["values"][0] or {}).get("fieldConfigurationScheme") or {}
            sid = scheme.get("id")
            if scheme.get("name"):
                scheme_names.add(scheme["name"])
            else:
                scheme_ok = False
            if not sid:
                configs_ok = False
                continue
            st, d = client.req(
                f"{client.api_prefix}/fieldconfigurationscheme/mapping",
                params={"fieldConfigurationSchemeId": sid})
            if st != 200 or not isinstance(d, dict):
                configs_ok = False
                continue
            cids = [str(m.get("fieldConfigurationId"))
                    for m in (d.get("values") or [])
                    if m.get("fieldConfigurationId") is not None]
            proj_configs = {id_to_name[c] for c in cids
                            if c in id_to_name and id_to_name[c]}
            if not proj_configs:
                configs_ok = False
                continue
            config_names |= proj_configs
        return scheme_names, config_names, scheme_ok, configs_ok
    except Exception:  # noqa: BLE001
        return set(), set(), False, False


def _scoped_project_attr_names(client: JiraClient, keys, suffix,
                               extract) -> tuple[set, bool]:
    """Generic per-project resolver for an attribute read off a SINGLE
    /project/{key}/... endpoint that returns one object (permission scheme,
    notification scheme) or a list (statuses). ``extract(d)`` turns the parsed
    body into a set of names for that project. Returns (names, ok); ok=False on
    the FIRST failure (missing key resolution is not needed — the endpoint is
    keyed by project KEY directly — but a non-200, wrong shape, or empty result
    for any project reverts the area to instance-wide). Same no-false-clean
    posture as the scheme resolvers."""
    try:
        names: set = set()
        for key in keys:
            st, d = client.req(f"{client.api_prefix}/project/{key}/{suffix}")
            if st != 200:
                return names, False
            proj_names = extract(d)
            if not proj_names:
                return names, False
            names |= proj_names
        return names, True
    except Exception:  # noqa: BLE001
        return set(), False


def _scoped_permission_scheme_names(client: JiraClient, keys) -> tuple[set,
                                                                       bool]:
    """Names of the permission scheme each selected project uses."""
    def extract(d):
        if isinstance(d, dict) and d.get("name"):
            return {d["name"]}
        return set()
    return _scoped_project_attr_names(client, keys, "permissionscheme", extract)


def _scoped_notification_scheme_names(client: JiraClient, keys) -> tuple[set,
                                                                         bool]:
    """Names of the notification scheme each selected project uses."""
    def extract(d):
        if isinstance(d, dict) and d.get("name"):
            return {d["name"]}
        return set()
    return _scoped_project_attr_names(client, keys, "notificationscheme",
                                      extract)


def _scoped_status_names(client: JiraClient, keys) -> tuple[set, bool]:
    """Union of STATUS names actually used by the selected projects' workflows
    on this side, via GET /project/{key}/statuses (a list of issue-type entries,
    each carrying a .statuses[].name list)."""
    def extract(d):
        out: set = set()
        if not isinstance(d, list):
            return out
        for it in d:
            for s in ((it or {}).get("statuses") or []):
                if s.get("name"):
                    out.add(s["name"])
        return out
    return _scoped_project_attr_names(client, keys, "statuses", extract)


def _scoped_custom_field_names(client: JiraClient, keys, custom_recs) -> tuple[
        set, bool]:
    """Names of custom fields IN SCOPE for the selected projects on this side.

    A custom field is in scope if it has at least one context applicable to ANY
    selected project — GET /field/{id}/context?projectId={pid} returns the
    contexts visible to that project (global contexts + project-specific
    contexts), so a non-empty result for ANY selected project means in-scope.

    ``custom_recs`` is the side's list of custom-field records (each with id +
    name). The per-field checks fan out over map_results (N+1 over fields).

    Fail-safe is two-tiered and STRICTLY no-false-clean:
      * if the per-field context check ERRORS/raises for a given field, that
        field is treated as IN SCOPE (never dropped),
      * if project-id resolution fails for any selected project, the WHOLE area
        reverts to instance-wide (ok=False) — we cannot trust any narrowing."""
    try:
        recs = [r for r in (custom_recs or []) if r.get("id") and r.get("name")]
        pids = []
        for key in keys:
            pid = _project_id(client, key)
            if not pid:
                return set(), False
            pids.append(pid)
        if not pids:
            return set(), False

        def _in_scope(rec) -> bool:
            # In scope if ANY selected project sees >=1 context for this field.
            # On ANY error reading a context, fail SAFE -> treat as in scope.
            for pid in pids:
                try:
                    st, d = client.req(
                        f"{client.api_prefix}/field/{rec['id']}/context",
                        params={"projectId": pid})
                except Exception:  # noqa: BLE001
                    return True
                if st != 200 or not isinstance(d, dict):
                    return True
                if (d.get("values") or []):
                    return True
            return False

        results = map_results(recs, _in_scope)
        names: set = set()
        for rec, res in zip(recs, results):
            # A task that raised comes back as the exception -> fail safe (keep).
            if isinstance(res, bool):
                if res:
                    names.add(rec["name"])
            else:
                names.add(rec["name"])
        return names, True
    except Exception:  # noqa: BLE001
        return set(), False


def _area_errors(area, se, te):
    """Yield a loud area_error finding per errored side.

    An unreachable/unauthorized side must surface as a finding — never be
    rendered as a clean 0-issue area. Source errors hide losses; target
    errors inflate/hide findings; both must be visible to the verdict.
    """
    out = []
    if se:
        out.append({"area": area, "name": area, "kind": "area_error",
                    "detail": {"side": "source", "error": se}})
    if te:
        out.append({"area": area, "name": area, "kind": "area_error",
                    "detail": {"side": "target", "error": te}})
    return out


def audit_config(src: JiraClient, tgt: JiraClient, jsm_projects=(),
                 progress: Callable[[str], None] | None = None) -> dict:
    areas: dict = {}
    findings: list[dict] = []
    say = progress or (lambda m: None)
    dc_side = "dc" in (src.conn.deployment, tgt.conn.deployment)

    def _fetch_area(client, area, suffix, key):
        # Each side joins the suffix with its OWN prefix (src and tgt may
        # speak different dialects in a DC->Cloud audit).
        path = f"{client.api_prefix}{suffix}"
        if client.conn.deployment == "dc":
            if area == "screens":
                return _dc_list_sliced(client, path)
            key = DC_KEYS.get(area, key)
        return client.paginate_start_at(path, key=key)

    # ---- simple dimensions (fetch src+tgt for every area concurrently, then
    # build summaries/findings on the main thread in SIMPLE order -> identical
    # output regardless of completion order; same determinism contract as the
    # env gather pool).
    active = [(area, suffix, key) for area, suffix, key in SIMPLE
              if not (dc_side and area in CLOUD_ONLY)]
    fetch_tasks = []   # (area, side, client, suffix, key)
    for area, suffix, key in active:
        fetch_tasks.append((area, "src", src, suffix, key))
        fetch_tasks.append((area, "tgt", tgt, suffix, key))
    fetched = {}
    results = map_results(
        fetch_tasks,
        lambda t: _fetch_area(t[2], t[0], t[3], t[4]))
    for (area, side, _c, _s, _k), res in zip(fetch_tasks, results):
        # A task that raised is returned as the exception; treat as an errored
        # side ((items, err)) so _area_errors still surfaces it loudly.
        fetched[(area, side)] = res if isinstance(res, tuple) else ([], str(res))

    # ---- per-project scope resolution for the scopable SIMPLE areas.
    # When the operator audits only SOME projects (and neither side is DC), each
    # scheme/status/issue-type area is narrowed to the objects those projects
    # actually use. Mirrors the workflows/screens scoping below: resolve each
    # side's in-scope NAME set; if EITHER side fails, that area reverts to the
    # full instance-wide list (no-false-clean). Resolution shares per-project
    # endpoint walks across the areas that read the same endpoint.
    # scoped[area] = (src_names:set, tgt_names:set, ok:bool); absent => no scope.
    scoped: dict = {}

    def _global_items(area, side):
        items, _err = fetched.get((area, side), ([], None))
        return items

    if jsm_projects and not dc_side:
        # Workflow scheme (shares the /workflowscheme/project walk; workflow
        # NAME scoping below reuses _scoped_workflow_names separately).
        s_wfsch, s_ok = _scoped_workflow_and_scheme_names(src, jsm_projects)[1:3]
        t_wfsch, t_ok = _scoped_workflow_and_scheme_names(tgt, jsm_projects)[1:3]
        scoped["workflow_schemes"] = (s_wfsch, t_wfsch, s_ok and t_ok)

        # ITSS + screen-scheme names (shares the project->ITSS->screenscheme
        # walk with the screens section below). The walk surfaces INDEPENDENT
        # ok flags per depth: ITSS scoping survives a deeper screen-scheme read
        # failure, and vice-versa. Tuple shape:
        # (screen_names, itss_names, ss_names, itss_ok, ss_ok, screen_ok).
        s_scr = _scoped_screen_and_scheme_names(
            src, jsm_projects, _global_items("screens", "src"))
        t_scr = _scoped_screen_and_scheme_names(
            tgt, jsm_projects, _global_items("screens", "tgt"))
        scoped["issuetype_screen_schemes"] = (
            s_scr[1], t_scr[1], s_scr[3] and t_scr[3])
        scoped["screen_schemes"] = (s_scr[2], t_scr[2], s_scr[4] and t_scr[4])

        # Issue-type scheme + issue types (shares /issuetypescheme/project).
        # Tuple: (scheme_names, type_names, scheme_ok, types_ok) — independent.
        s_its = _scoped_issuetype_and_scheme_names(
            src, jsm_projects, _global_items("issue_types", "src"))
        t_its = _scoped_issuetype_and_scheme_names(
            tgt, jsm_projects, _global_items("issue_types", "tgt"))
        scoped["issuetype_schemes"] = (
            s_its[0], t_its[0], s_its[2] and t_its[2])
        scoped["issue_types"] = (s_its[1], t_its[1], s_its[3] and t_its[3])

        # Field-config scheme + field configurations (shares
        # /fieldconfigurationscheme/project). Tuple:
        # (scheme_names, config_names, scheme_ok, configs_ok) — independent.
        s_fc = _scoped_fieldconfig_and_scheme_names(
            src, jsm_projects, _global_items("field_configurations", "src"))
        t_fc = _scoped_fieldconfig_and_scheme_names(
            tgt, jsm_projects, _global_items("field_configurations", "tgt"))
        scoped["field_config_schemes"] = (
            s_fc[0], t_fc[0], s_fc[2] and t_fc[2])
        scoped["field_configurations"] = (
            s_fc[1], t_fc[1], s_fc[3] and t_fc[3])

        # Permission / notification schemes + project statuses (one endpoint
        # each, keyed by project KEY).
        s_ps, s_ok = _scoped_permission_scheme_names(src, jsm_projects)
        t_ps, t_ok = _scoped_permission_scheme_names(tgt, jsm_projects)
        scoped["permission_schemes"] = (s_ps, t_ps, s_ok and t_ok)
        s_ns, s_ok = _scoped_notification_scheme_names(src, jsm_projects)
        t_ns, t_ok = _scoped_notification_scheme_names(tgt, jsm_projects)
        scoped["notification_schemes"] = (s_ns, t_ns, s_ok and t_ok)
        s_st, s_ok = _scoped_status_names(src, jsm_projects)
        t_st, t_ok = _scoped_status_names(tgt, jsm_projects)
        scoped["statuses"] = (s_st, t_st, s_ok and t_ok)

    def _apply_scope(area, s_items, t_items):
        """Return (s_items, t_items, scope_label). When a scope was resolved
        successfully for ``area``, filter both global lists to the in-scope
        union (by normalized name) and label scope='projects'; otherwise (no
        scope requested, or resolution failed) return the lists unchanged,
        instance-wide, and emit a loud fallback note on a genuine FAILURE."""
        ent = scoped.get(area)
        if ent is None:
            return s_items, t_items, "instance"
        s_names, t_names, ok = ent
        if not ok:
            say(f"scope resolution failed for selected projects — comparing "
                f"{area} instance-wide")
            return s_items, t_items, "instance"
        in_scope = {_norm_name(n) for n in (s_names | t_names)}
        s_f = [x for x in s_items if _norm_name(x.get("name", "")) in in_scope]
        t_f = [x for x in t_items if _norm_name(x.get("name", "")) in in_scope]
        return s_f, t_f, "projects"

    for area, suffix, key in SIMPLE:
        if dc_side and area in CLOUD_ONLY:
            areas[area] = {"label": area, "skipped": True,
                           "reason": "no Data Center API — verify manually"}
            say(f"[{area}] skipped — no Data Center API")
            continue
        if area == "screens":
            # screens is owned wholly by the dedicated section below (it adds a
            # deep field check and, when projects are selected, scopes the set).
            # Emitting its summary/findings here too would double-count.
            continue
        s, se = fetched[(area, "src")]
        t, te = fetched[(area, "tgt")]
        # Per-project scoping: narrow scheme/status/issue-type lists to what the
        # selected projects use. Errored sides keep the (empty) global list and
        # surface their error below — _apply_scope only filters the present rows
        # and the no-false-clean fallback governs any resolution failure. The
        # genuinely-global areas (priorities, resolutions, link_types, roles)
        # have no scope entry -> stay instance-wide.
        s, t, scope = _apply_scope(area, s, t)
        summ = _summary(area, _names(s), _names(t))
        summ["scope"] = scope
        if se or te:
            summ["error"] = f"src={se} tgt={te}"
        areas[area] = summ
        findings.extend(_area_errors(area, se, te))
        for name in summ["source_only"]:
            findings.append({"area": area, "name": name,
                             "kind": "missing_in_tgt", "detail": {}})
        say(f"[{area}] src={summ['src']} tgt={summ['tgt']} "
            f"source-only={len(summ['source_only'])} scope={scope}")

    # ---- custom fields: presence + type + select options
    sf, sfe = src.paginate_start_at(f"{src.api_prefix}/field")
    tf, tfe = tgt.paginate_start_at(f"{tgt.api_prefix}/field")
    findings.extend(_area_errors("custom_fields", sfe, tfe))
    # Key by normalized name so " (migrated)"/case/whitespace variants match;
    # first original wins for display + record lookup.
    scustom: dict = {}
    for f in (sf or []):
        if f.get("custom") and f.get("name"):
            scustom.setdefault(_norm_name(f["name"]), f)
    tcustom: dict = {}
    for f in (tf or []):
        if f.get("custom") and f.get("name"):
            tcustom.setdefault(_norm_name(f["name"]), f)

    # Per-project scoping: a custom field is in scope when it has a context
    # applicable to a selected project (global contexts included). Narrow both
    # sides to the in-scope union; on ANY resolution failure on EITHER side,
    # revert the WHOLE area to instance-wide (no-false-clean). DC stays
    # instance-wide. A per-field context error keeps that field IN scope.
    cf_scope = "instance"
    if jsm_projects and not dc_side:
        s_cf, s_ok = _scoped_custom_field_names(src, jsm_projects,
                                                list(scustom.values()))
        t_cf, t_ok = _scoped_custom_field_names(tgt, jsm_projects,
                                                list(tcustom.values()))
        if s_ok and t_ok:
            in_scope = {_norm_name(n) for n in (s_cf | t_cf)}
            scustom = {k: v for k, v in scustom.items() if k in in_scope}
            tcustom = {k: v for k, v in tcustom.items() if k in in_scope}
            cf_scope = "projects"
        else:
            say("scope resolution failed for selected projects — comparing "
                "custom_fields instance-wide")
    summ = _summary("custom_fields",
                    (f["name"] for f in scustom.values()),
                    (f["name"] for f in tcustom.values()))
    for key in sorted(set(scustom) - set(tcustom)):
        rec = scustom[key]
        findings.append({"area": "custom_fields", "name": rec["name"],
                         "kind": "missing_in_tgt",
                         "detail": {"type": str((rec.get("schema") or {})
                                                .get("custom", "")).split(":")[-1]}})
    # Pass 1 (no I/O): type mismatches + collect the select fields needing the
    # option deep-check.
    select_keys = []
    for key in sorted(set(scustom) & set(tcustom)):
        srec, trec = scustom[key], tcustom[key]
        name = srec["name"]
        s_type = str((srec.get("schema") or {}).get("custom", "")).split(":")[-1]
        t_type = str((trec.get("schema") or {}).get("custom", "")).split(":")[-1]
        if s_type != t_type:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "type_mismatch",
                             "detail": {"src_type": s_type, "tgt_type": t_type}})
        ct = str((srec.get("schema") or {}).get("custom", ""))
        if not dc_side and any(mk in ct for mk in _SELECT_MARKERS):
            select_keys.append(key)

    # Pass 2 (parallel I/O): fetch src+tgt options for every select field at
    # once, then build option_mismatch findings on the main thread in sorted
    # order -> identical to the sequential version.
    opt_tasks = []   # (key, side, client, fid)
    for key in select_keys:
        opt_tasks.append((key, "src", src, scustom[key]["id"]))
        opt_tasks.append((key, "tgt", tgt, tcustom[key]["id"]))
    opt_results = map_results(opt_tasks, lambda t: _field_options(t[2], t[3]))
    opts = {}
    for (key, side, _c, _f), res in zip(opt_tasks, opt_results):
        opts[(key, side)] = res if isinstance(res, set) else set()
    for key in select_keys:
        name = scustom[key]["name"]
        so, to = opts[(key, "src")], opts[(key, "tgt")]
        miss = sorted(so - to)
        if miss:
            findings.append({"area": "custom_fields", "name": name,
                             "kind": "option_mismatch",
                             "detail": {"missing_options_in_tgt": miss[:40],
                                        "src_opts": len(so),
                                        "tgt_opts": len(to)}})
    checked = len(select_keys)
    summ["select_fields_checked"] = checked
    # Scope label reflects whether the per-field context scoping narrowed the
    # area to the selected projects (cf_scope) or it stayed instance-wide.
    summ["scope"] = cf_scope
    if dc_side:
        summ["options_checked"] = False
    areas["custom_fields"] = summ
    say(f"[custom_fields] src={summ['src']} tgt={summ['tgt']} checked={checked}")

    # ---- workflows: structural comparison for in-both (Cloud pairs only —
    # DC's /workflow is a plain array whose `steps` is an int COUNT, no
    # transition detail exists, so any dc side downgrades to name presence)
    def _workflows(client):
        if client.conn.deployment == "dc":
            return client.paginate_start_at(f"{client.api_prefix}/workflow")
        return client.paginate_start_at(
            f"{client.api_prefix}/workflow/search",
            params={"expand": "transitions,statuses"})

    sw, swe = _workflows(src)
    tw, twe = _workflows(tgt)
    findings.extend(_area_errors("workflows", swe, twe))
    # Key by normalized workflow name; first original wins for display.
    swn: dict = {}
    for w in (sw or []):
        nm = _wf_name(w)
        if nm:
            swn.setdefault(_norm_name(nm), w)
    twn: dict = {}
    for w in (tw or []):
        nm = _wf_name(w)
        if nm:
            twn.setdefault(_norm_name(nm), w)

    # Per-project scoping: when the operator audits only SOME projects, compare
    # only the workflows those projects actually use (union across selected
    # projects, on BOTH sides) instead of every workflow in the instance. The
    # no-false-clean rule governs the fallback: if scope resolution fails on
    # EITHER side, revert the WHOLE area to instance-wide (over-report, never
    # under-report) and say so loudly. DC has no per-project scheme REST here,
    # so any DC side stays instance-wide.
    wf_scope = "instance"
    if jsm_projects and not dc_side:
        s_names, s_ok = _scoped_workflow_names(src, jsm_projects)
        t_names, t_ok = _scoped_workflow_names(tgt, jsm_projects)
        if s_ok and t_ok:
            in_scope = {_norm_name(n) for n in (s_names | t_names)}
            swn = {k: v for k, v in swn.items() if k in in_scope}
            twn = {k: v for k, v in twn.items() if k in in_scope}
            wf_scope = "projects"
        else:
            say("scope resolution failed for selected projects — comparing "
                "workflows instance-wide")

    areas["workflows"] = _summary("workflows",
                                  (_wf_name(w) for w in swn.values()),
                                  (_wf_name(w) for w in twn.values()))
    areas["workflows"]["scope"] = wf_scope
    for key in sorted(set(swn) - set(twn)):
        findings.append({"area": "workflows", "name": _wf_name(swn[key]),
                         "kind": "missing_in_tgt", "detail": {}})
    if dc_side:
        areas["workflows"]["structure_checked"] = False
    else:
        for key in sorted(set(swn) & set(twn)):
            name = _wf_name(swn[key])
            s_tr = set(tr.get("name")
                       for tr in (swn[key].get("transitions") or []))
            t_tr = set(tr.get("name")
                       for tr in (twn[key].get("transitions") or []))
            s_st = len(swn[key].get("statuses") or [])
            t_st = len(twn[key].get("statuses") or [])
            if s_tr != t_tr or s_st != t_st:
                findings.append({"area": "workflows", "name": name,
                                 "kind": "structure_mismatch",
                                 "detail": {"src_statuses": s_st,
                                            "tgt_statuses": t_st,
                                            "transitions_missing_in_tgt":
                                                sorted(s_tr - t_tr)[:20]}})
    say(f"[workflows] in_both={areas['workflows']['in_both']}")

    # ---- screens: presence + deep field check for in-both (bounded). Reuse the
    # concurrent fetch from the SIMPLE phase (the SIMPLE loop skips screens'
    # emission so this section is the single owner of the area).
    ss, sse = fetched[("screens", "src")]
    ts, tse = fetched[("screens", "tgt")]
    findings.extend(_area_errors("screens", sse, tse))
    # Key by normalized screen name; first original wins for display.
    ssn: dict = {}
    for s in (ss or []):
        if s.get("name"):
            ssn.setdefault(_norm_name(s["name"]), s)
    tsn: dict = {}
    for s in (ts or []):
        if s.get("name"):
            tsn.setdefault(_norm_name(s["name"]), s)

    # Per-project scoping (same no-false-clean contract as workflows): narrow
    # the screen sets to those the selected projects reference. project ->
    # ITSS -> screen-scheme ids -> screen ids -> names (mapped via the global
    # /screens list just fetched). On ANY failure on EITHER side, revert the
    # WHOLE area to instance-wide and say so loudly. DC stays instance-wide.
    scr_scope = "instance"
    if jsm_projects and not dc_side:
        s_scr, s_ok = _scoped_screen_names(src, jsm_projects, ss)
        t_scr, t_ok = _scoped_screen_names(tgt, jsm_projects, ts)
        if s_ok and t_ok:
            in_scope = {_norm_name(n) for n in (s_scr | t_scr)}
            ssn = {k: v for k, v in ssn.items() if k in in_scope}
            tsn = {k: v for k, v in tsn.items() if k in in_scope}
            scr_scope = "projects"
        else:
            say("scope resolution failed for selected projects — comparing "
                "screens instance-wide")
    # Recompute the presence summary over the (possibly scoped) sets so the
    # area counts/source-only reflect the scope; the SIMPLE-loop summary was
    # instance-wide. deep_checked/capped are added below.
    areas["screens"] = _summary("screens",
                                 (s["name"] for s in ssn.values()),
                                 (s["name"] for s in tsn.values()))
    if sse or tse:
        areas["screens"]["error"] = f"src={sse} tgt={tse}"
    areas["screens"]["scope"] = scr_scope
    for key in sorted(set(ssn) - set(tsn)):
        findings.append({"area": "screens", "name": ssn[key]["name"],
                         "kind": "missing_in_tgt", "detail": {}})

    in_both = sorted(set(ssn) & set(tsn))
    deep = in_both[:_SCREEN_DEEP_CAP]
    sf_tasks = []   # (key, side, client, sid)
    for key in deep:
        sf_tasks.append((key, "src", src, ssn[key]["id"]))
        sf_tasks.append((key, "tgt", tgt, tsn[key]["id"]))
    sf_results = map_results(sf_tasks, lambda t: _screen_fields(t[2], t[3]))
    sflds = {}
    for (key, side, _c, _s), res in zip(sf_tasks, sf_results):
        sflds[(key, side)] = res if isinstance(res, set) else set()
    for key in deep:
        s_f, t_f = sflds[(key, "src")], sflds[(key, "tgt")]
        miss = sorted(s_f - t_f)
        if miss:
            findings.append({"area": "screens", "name": ssn[key]["name"],
                             "kind": "field_mismatch",
                             "detail": {"fields_missing_in_tgt": miss[:25]}})
    areas["screens"]["deep_checked"] = len(deep)
    areas["screens"]["capped"] = len(in_both) > _SCREEN_DEEP_CAP
    say(f"[screens] deep_checked={len(deep)} capped={areas['screens']['capped']}")

    # ---- JSM request types + queues per selected project (paginated correctly)
    def _sd_id(client, key):
        for s in client.sd_list("/rest/servicedeskapi/servicedesk"):
            if s.get("projectKey") == key:
                return s.get("id")
        return None

    def _jsm(client, key):
        sid = _sd_id(client, key)
        if not sid:
            return {"request_types": [], "queues": []}
        rt = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/requesttype")
        q = client.sd_list(f"/rest/servicedeskapi/servicedesk/{sid}/queue")
        return {"request_types": _names(rt), "queues": _names(q)}

    jsm_area = {}
    for key in jsm_projects:
        # JSM is fetched via sd_list, which RAISES on outage. Scope that
        # failure to this project: emit a loud area_error and keep already-
        # computed findings (statuses/fields/workflows/screens) — never abort
        # the whole audit.
        try:
            s_j, t_j = _jsm(src, key), _jsm(tgt, key)
        except ClientError as exc:
            jsm_area[key] = {"error": str(exc)}
            findings.append({"area": "jsm", "name": f"{key}: lookup failed",
                             "kind": "area_error",
                             "detail": {"project": key, "error": str(exc)}})
            say(f"[jsm {key}] lookup failed: {exc}")
            continue
        entry = {}
        for obj in ("request_types", "queues"):
            # Normalized match so a " (migrated)"/case variant isn't a false gap;
            # original name kept for the finding text.
            s_idx, t_idx = _norm_index(s_j[obj]), _norm_index(t_j[obj])
            source_only = sorted(s_idx[k] for k in (set(s_idx) - set(t_idx)))
            entry[obj] = {"src": len(s_idx), "tgt": len(t_idx),
                          "source_only": source_only}
            label = "request type" if obj == "request_types" else "queue"
            for name in source_only:
                findings.append({"area": "jsm",
                                 "name": f"{key}: {label} '{name}'",
                                 "kind": "missing_in_tgt",
                                 "detail": {"project": key,
                                            "object": label.replace(" ", "_")}})
        jsm_area[key] = entry
        say(f"[jsm {key}] rt src={entry['request_types']['src']}/"
            f"tgt={entry['request_types']['tgt']}")
    if jsm_projects:
        areas["jsm"] = jsm_area

    # ---- per-project components + versions parity (detect-and-guide)
    # A very common silent migration loss: project components/versions are not
    # carried over. Compared by normalized name per project. An unreadable side
    # for a project must surface as an area_error (carrying the project), never
    # a clean read — same fail-loud posture as the global areas above.
    # Components and versions exist on BOTH deployments as plain-array endpoints
    # (no Cloud-only gating needed); paginate_start_at returns the array
    # directly and reports a loud ERR on any non-200.
    for area, suffix in (("components", "components"), ("versions", "versions")):
        summ = {"label": area, "src": 0, "tgt": 0, "in_both": 0,
                "source_only": [], "target_only_count": 0, "target_only": []}
        errs: list[str] = []
        for key in jsm_projects:
            s, se = src.paginate_start_at(
                f"{src.api_prefix}/project/{key}/{suffix}")
            t, te = tgt.paginate_start_at(
                f"{tgt.api_prefix}/project/{key}/{suffix}")
            if se:
                findings.append({"area": area, "name": f"{key} / {area}",
                                 "kind": "area_error",
                                 "detail": {"side": "source", "project": key,
                                            "error": se}})
                errs.append(f"{key}:src={se}")
            if te:
                findings.append({"area": area, "name": f"{key} / {area}",
                                 "kind": "area_error",
                                 "detail": {"side": "target", "project": key,
                                            "error": te}})
                errs.append(f"{key}:tgt={te}")
            # Never derive a missing_in_tgt gap for a project whose source or
            # target list could not be read — that would manufacture a false
            # gap or, worse, a false clean. Compare only when both sides read.
            if se or te:
                continue
            s_idx = _norm_index(_names(s))
            t_idx = _norm_index(_names(t))
            source_only = sorted(s_idx[k] for k in (set(s_idx) - set(t_idx)))
            summ["src"] += len(s_idx)
            summ["tgt"] += len(t_idx)
            summ["in_both"] += len(set(s_idx) & set(t_idx))
            for name in source_only:
                summ["source_only"].append(f"{key} / {name}")
                findings.append({"area": area, "name": f"{key} / {name}",
                                 "kind": "missing_in_tgt",
                                 "detail": {"project": key}})
        if errs:
            summ["error"] = " ".join(errs)
        if jsm_projects:
            areas[area] = summ
            say(f"[{area}] projects={len(jsm_projects)} src={summ['src']} "
                f"tgt={summ['tgt']} source-only={len(summ['source_only'])}")

    return {"areas": areas, "findings": findings}
