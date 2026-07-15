"""Execute a FixPlan against the TARGET client only.

Three invariants the elevation flow taught us, generalized from role grants to
config writes: (1) every write targets the target side — the single client
passed by fix_apply is always the target, and build_plan emits only
side='target' actions; the side guard catches a future planner regression, not
a runtime client-identity check; (2) pre-check existence so re-running is a
logged no-op, never a double-create; (3) log every call (success, no-op,
failure) — a 200 is not 'fixed', it is one logged step the re-audit will
judge.

The optional `expected_api_base` parameter in apply_plan enforces a true
client-identity guard at the call boundary: fix_apply passes the target
client's api_base so a mis-wired caller (wrong client object) raises before
any HTTP, independently of the planner-level side field."""
from __future__ import annotations

import gzip
import json
import os

_PRECHECK = {        # area -> (list_path, name_key) to detect an existing object
    "statuses": ("/rest/api/3/status", "name"),
    "priorities": ("/rest/api/3/priority", "name"),
    "resolutions": ("/rest/api/3/resolution", "name"),
    "issue_types": ("/rest/api/3/issuetype", "name"),
    "link_types": ("/rest/api/3/issueLinkType", "name"),
    "custom_fields": ("/rest/api/3/field", "name"),
    # screens removed from FIXES (I5/I7) — no precheck entry needed
}


def _exists(client, area, name) -> bool:
    spec = _PRECHECK.get(area)
    if not spec:
        return False
    path, key = spec
    st, d = client.req(path)
    # Unwrap every list shape Jira returns: a bare array (statuses/priorities/
    # issuetypes/screens), the {values:[...]} envelope, or issueLinkType's
    # {issueLinkTypes:[...]} wrapper. Missing the wrapper read every link type
    # as absent — re-runs double-created them and reaudit never saw them close.
    if isinstance(d, list):
        items = d
    elif isinstance(d, dict):
        items = d.get("values") or d.get("issueLinkTypes") or []
    else:
        items = []
    return any(i.get(key) == name for i in items)


def _rec(action, method, path, status, ok, created_id=None, error=None):
    return {"finding_ref": action.finding_ref, "fix_id": action.fix_id,
            "object_name": action.object_name, "method": method, "path": path,
            "status": status, "ok": ok, "created_id": created_id, "error": error}


def _resolve_target_field_id(client, name):
    """Resolve a custom field's TARGET id by NAME at apply time.

    payload['field_id'] is the SOURCE id (captured against the DC/source
    tenant) and is meaningless on the target — wiring or populating against it
    404s or writes nothing. Always re-read /field on the target and match by
    name. Works whether the field was just created in this plan or pre-existed
    (idempotent re-run). Returns None when the named field is not present on the
    target so callers fail loud rather than writing to the wrong field."""
    st, d = client.req("/rest/api/3/field")
    if st != 200 or not isinstance(d, list):
        return None
    for f in d:
        if f.get("custom") and f.get("name") == name:
            return f.get("id")
    return None


def _apply_create(client, a, log):
    p = a.payload
    if a.area == "statuses":
        st, d = client.create_status(p["name"], p["category"])
        cid = d[0]["id"] if isinstance(d, list) and d else None
        return _rec(a, "POST", "/rest/api/3/statuses", st, st < 300, cid,
                    None if st < 300 else str(d))
    if a.area == "priorities":
        st, d = client.create_priority(p["name"], p.get("description", ""))
        return _rec(a, "POST", "/rest/api/3/priority", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "resolutions":
        st, d = client.create_resolution(p["name"], p.get("description", ""))
        return _rec(a, "POST", "/rest/api/3/resolution", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "issue_types":
        st, d = client.create_issue_type(p["name"], p.get("description", ""),
                                         p.get("hierarchy_level", 0))
        return _rec(a, "POST", "/rest/api/3/issuetype", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "link_types":
        st, d = client.create_link_type(p["name"], p.get("inward", ""),
                                        p.get("outward", ""))
        return _rec(a, "POST", "/rest/api/3/issueLinkType", st, st < 300,
                    d.get("id"), None if st < 300 else str(d))
    if a.area == "custom_fields":
        return _apply_create_field(client, a, log)
    # screens removed from FIXES (I5/I7) — no create branch needed
    return _rec(a, "-", "-", 0, False, error=f"no creator for area {a.area}")


def _apply_create_field(client, a, log):
    p = a.payload
    st, d = client.create_field(p["name"] if "name" in p else a.object_name,
                                p.get("type", "textfield"))
    rec = _rec(a, "POST", "/rest/api/3/field", st, st < 300, d.get("id"),
               None if st < 300 else str(d))
    log(rec)
    if st >= 300:
        return None      # already logged; don't double-log via the caller
    field_id = d["id"]
    for ctx in p.get("contexts", []):
        st2, d2 = client.create_field_context(field_id, ctx.get("name", "Default"))
        log(_rec(a, "POST", f"/rest/api/3/field/{field_id}/context", st2,
                 st2 < 300, d2.get("id"), None if st2 < 300 else str(d2)))
        if st2 < 300 and ctx.get("options"):
            st3, _ = client.add_field_options(field_id, d2["id"], ctx["options"])
            log(_rec(a, "POST",
                     f"/rest/api/3/field/{field_id}/context/{d2['id']}/option",
                     st3, st3 < 300))
    return None          # field creation logs its own (multi-call) trail


# Blast-radius cap for a single populate action: the maximum number of live
# per-issue value writes it will issue. A populate is bulk by design (so the
# default is high — a sanity ceiling against a runaway values file, not a tight
# limit), overridable with MA_MAX_POPULATE; a negative/unparseable value falls
# back to the default, and a deliberate over-cap run raises MA_MAX_POPULATE.
_DEFAULT_MAX_POPULATE = 10000
# Circuit breaker: how many SERVER-SIDE (5xx/429) write failures the populate
# loop tolerates before it STOPS — a failing/throttling instance must not be
# hammered for every remaining issue. Shares MA_BREAKER_THRESHOLD with the
# env-fix path; 0 disables it.
_DEFAULT_BREAKER_THRESHOLD = 5


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _count_rows(path: str) -> int:
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


def _apply_populate(client, a, workspace, log):
    rel = a.payload.get("values_file")
    if not rel:
        log(_rec(a, "-", "-", 0, False, error="no captured values"))
        return
    path = os.path.join(workspace, rel)
    if not os.path.exists(path):
        log(_rec(a, "-", "-", 0, False, error="values file missing"))
        return
    # Blast-radius cap BEFORE any write (or even the field lookup): refuse a
    # values file larger than the cap rather than bulk-write to it.
    cap = _int_env("MA_MAX_POPULATE", _DEFAULT_MAX_POPULATE)
    total = _count_rows(path)
    if total > cap:
        log(_rec(a, "-", "-", 0, False,
                 error=f"{total} value writes exceed the populate cap ({cap}); "
                       f"refusing — raise MA_MAX_POPULATE deliberately"))
        return
    # Resolve the TARGET field id by name — payload['field_id'] is the source
    # id and writing to it silently writes nothing (I2).
    field_id = _resolve_target_field_id(client, a.object_name)
    if not field_id:
        log(_rec(a, "-", "-", 0, False,
                 error=f"target field {a.object_name!r} not found"))
        return
    # Stream the values (never load the whole file) and stop on a failing
    # instance. The loop is sequential, so a plain counter is enough. NB: each
    # logical write is an idempotent PUT, which the client retries internally on
    # 5xx/429 before returning — so reaching the trip can cost up to
    # threshold x (client retries) HTTP calls; the breaker bounds LOGICAL
    # failures, not raw requests.
    threshold = _int_env("MA_BREAKER_THRESHOLD", _DEFAULT_BREAKER_THRESHOLD)
    ok = bad = skipped = server_fails = 0
    tripped = False
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if tripped:
                skipped += 1
                continue
            try:
                row = json.loads(line)
                key, value = row["issue_key"], row["value"]
            except (ValueError, KeyError, TypeError):
                bad += 1          # a corrupt captured line — log it, don't crash
                continue
            st, _ = client.set_issue_fields(key, {field_id: value})
            # ONLY a real 2xx is a write that landed. st == -1 is the client's
            # exhausted-idempotent-retries return on a TRANSPORT failure (the
            # write never landed) — count it as failed AND feed it to the
            # breaker (a 4xx stays object-level and must not trip). Without this
            # a connection-drop storm would read as all-success and hammer every
            # remaining issue.
            if 200 <= st < 300:
                ok += 1
            else:
                bad += 1
                if threshold > 0 and (st >= 500 or st == 429 or st < 0):
                    server_fails += 1
                    if server_fails >= threshold:
                        tripped = True
    parts = []
    if bad:
        parts.append(f"{bad} write(s) failed")
    if skipped:
        parts.append(f"{skipped} skipped — circuit breaker open (instance "
                     "returned repeated server errors); rerun when it recovers")
    err = "; ".join(parts) or None
    clean = not bad and not skipped
    log(_rec(a, "PUT", f"/rest/api/3/issue/* ({field_id})",
             200 if clean else 207, clean, str(ok), err))


def apply_plan(tgt_client, plan, log, workspace: str = "",
               expected_api_base: str | None = None) -> None:
    """Apply *plan* using *tgt_client*.

    Two guards before any HTTP:
    - expected_api_base (runtime identity): if provided, the client's api_base
      must match; a mis-wired caller (wrong client object) raises immediately.
      fix_apply always passes this so the guard is active in production.
    - action.side (planner-level regression guard): raises on a 'source'-side
      action emitted by a future planner bug.  Does NOT verify that the client
      is the target — that is expected_api_base's job."""
    if expected_api_base is not None and tgt_client.api_base != expected_api_base:
        raise ValueError(
            f"apply_plan client mismatch: expected api_base "
            f"{expected_api_base!r}, got {tgt_client.api_base!r}; "
            f"writes must flow through the target client only")
    for a in plan.actions:
        if a.side != "target":
            raise ValueError(f"fix action must target the target side, "
                             f"got {a.side!r} for {a.fix_id}")
        # add_options carries tier "create" but operates on an EXISTING field
        # (adding options to it), so it must NOT pass through the create
        # precheck — the field is present, which would wrongly log a no-op and
        # skip the option write (C2/I5).
        if a.fix_id == "jira.custom_field.add_options":
            _apply_add_options(tgt_client, a, log)
            continue
        if a.tier == "create" and _exists(tgt_client, a.area, a.object_name):
            log(_rec(a, "GET", "(precheck)", 0, True, error="exists"))
            continue
        if a.tier == "create":
            rec = _apply_create(tgt_client, a, log)
            if rec is not None:
                log(rec)
        elif a.tier == "populate":
            _apply_populate(tgt_client, a, workspace, log)
        elif a.tier == "wire":
            _apply_wire(tgt_client, a, log)


def _apply_add_options(client, a, log):
    """Add the missing select options to an existing target field (C2/I5).

    Resolves the target field id by name, fetches its default context (first
    one returned by GET /field/{id}/context), and adds the delta captured at
    audit time. A missing field / context / empty delta is a logged failure,
    never a silent skip."""
    missing = a.payload.get("missing_options") or []
    if not missing:
        log(_rec(a, "-", "-", 0, False, error="no missing options captured"))
        return
    field_id = _resolve_target_field_id(client, a.object_name)
    if not field_id:
        log(_rec(a, "-", "-", 0, False,
                 error=f"target field {a.object_name!r} not found"))
        return
    st, d = client.req(f"/rest/api/3/field/{field_id}/context")
    ctxs = d.get("values") if isinstance(d, dict) else d
    if st != 200 or not isinstance(ctxs, list) or not ctxs:
        log(_rec(a, "GET", f"/rest/api/3/field/{field_id}/context", st, False,
                 error="no context on target field"))
        return
    context_id = ctxs[0].get("id")
    st2, d2 = client.add_field_options(field_id, context_id, missing)
    log(_rec(a, "POST",
             f"/rest/api/3/field/{field_id}/context/{context_id}/option",
             st2, st2 < 300, None, None if st2 < 300 else str(d2)))


def _apply_wire(client, a, log):
    """Wire fixes. Field->screen uses the captured screen placements.

    jira.status.wire_workflow was removed (C3/I4): live-workflow editing is
    Tier-2 and the previous branch always returned ok=False. Guidance is
    generated by guidance_for('workflow_wire', ...) instead.

    A missing prerequisite is a logged failure, never a silent skip."""
    p = a.payload
    if a.fix_id == "jira.custom_field.wire_screen":
        # Resolve the TARGET field id by name — payload['field_id'] is the
        # source id and wiring it onto a target screen 404s (C1).
        fid = _resolve_target_field_id(client, a.object_name)
        if not fid:
            log(_rec(a, "-", "-", 0, False,
                     error=f"target field {a.object_name!r} not found"))
            return
        placed = False
        for scr in p.get("screens", []):
            sid, tid = scr.get("screen_id"), scr.get("tab_id")
            if sid and tid and fid:
                st, _ = client.add_field_to_screen(sid, tid, fid)
                log(_rec(a, "POST",
                         f"/rest/api/3/screens/{sid}/tabs/{tid}/fields", st,
                         st < 300))
                placed = True
        if not placed:
            log(_rec(a, "-", "-", 0, False,
                     error="no screen placements captured"))
        return
