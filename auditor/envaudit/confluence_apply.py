"""Apply app-tier env-audit findings against a live CONFLUENCE instance.

The Confluence counterpart to auditor.envaudit.apply (the Jira path). It mirrors
that module's safety contract EXACTLY — these are LIVE writes to a production
Confluence instance, so every guard the Jira review hardened is reproduced here:

  1. Identity guard (must be FIRST, before any HTTP): the client's api_base must
     match expected_api_base when provided, else ValueError before any write.
     Writes only ever hit the audited Confluence instance.
  2. Tier re-derivation (I4): the tier is re-derived SERVER-SIDE from the _FIXES
     registry by the stored finding's kind — client input is never trusted. Only
     empty_space + confluence_empty_group (both tier='app') are appliable; any
     human/unfixable kind is refused. A belt-and-suspenders scope set
     (_CONFLUENCE_APP_TIER_SCOPE) is the authoritative gate on top of the tier.
  3. TOCTOU re-verify (C1/C2): before EACH write, the CURRENT live precondition
     is re-read and re-checked. Never mutate on stale audit data:
       - empty_space: re-read the space's CURRENT page count via
         cql_count(space="<key>" and type=page). If > 0 now (or unreadable),
         ABORT (no write, still_open, ok=True 'skipped — now has N page(s)').
       - confluence_empty_group: re-read the group's CURRENT member count. If
         > 0 (or unreadable), ABORT (no write, still_open, ok=True 'skipped —
         now has N member(s)').
  4. Name-collision safety (H2/M2): the live object is resolved by collecting
     ALL entries whose name matches the finding name:
       - exactly ONE match → use its freshly-fetched id/key;
       - ZERO matches      → already gone (idempotent no-op, closed);
       - MORE THAN ONE     → ambiguous, ABORT (no write, still_open);
       - one match but its id/key cannot be extracted → ERROR (still_open,
         ok=False), NOT a false 'already gone'.
  5. Idempotent: an already-archived space / already-gone group is a logged
     no-op counted as closed — never raised, never counted as failed.
  6. Closure proven by RE-READING (space status==archived / group absent),
     never assumed from a 2xx.
  7. Every action is logged with the full per-action record shape (identical to
     the Jira fix log).

Returns (closed: int, still_open: int) for the _finalize_fix verdict logic.
"""
from __future__ import annotations

import json

from auditor.client import escape_query_key
from auditor.envaudit._pool import apply_worker_count, map_results
from auditor.envaudit.apply import (
    DestructiveCapExceeded,
    _BREAKER_SKIP_MSG,
    _NULL_BREAKER,
    _SNAP_CAP,
    _WriteBreaker,
    _breaker_threshold,
    _destructive_cap,
)
from auditor.envaudit.fixes import _FIXES

# The exhaustive scope set: only these Confluence kinds are ever auto-applied.
# Extending it requires adding an entry here AND a write path below — this set
# is the authoritative gate (in addition to the registry tier re-derivation).
_CONFLUENCE_APP_TIER_SCOPE = frozenset({
    "empty_space",
    "confluence_empty_group",
})


def _is_destructive(finding: dict) -> bool:
    """True when this Confluence finding would attempt a destructive op (app-tier
    AND in the Confluence apply scope). Shared by the blast-radius cap below."""
    kind = finding.get("kind") or ""
    entry = _FIXES.get(kind)
    return (bool(entry) and entry.get("tier") == "app"
            and kind in _CONFLUENCE_APP_TIER_SCOPE)


def _rec(object_name, method, path, status, ok, error=None, snapshot=None):
    """Build a log record matching the fix-log shape (identical to the Jira
    apply record). When `snapshot` is given (the live object JSON resolved just
    before a destructive op) it is captured LOCALLY for the operator's audit /
    restore trail — never transmitted externally (see auditor.envaudit.apply)."""
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


# Sentinel resolution outcomes for the name->object step (H2/M2).
_RESOLVE_OK = "ok"                # exactly one match with a usable id/key
_RESOLVE_ABSENT = "absent"        # zero matches → already gone (idempotent)
_RESOLVE_AMBIGUOUS = "ambiguous"  # >1 match → cannot safely target one
_RESOLVE_ERROR = "error"          # list failed, or one match but no id/key


def _resolve_space(client, key):
    """Resolve the live space for space *key* against a freshly-fetched list.

    The empty_space finding NAME is the space KEY (the gather keys by_space by
    key), so resolution matches on key — a server-derived, unique handle.

    Returns (outcome, item, detail). On _RESOLVE_OK, *item* is the live
    spaces_detailed row ({key,name,id,type,status}) carrying the FRESH id+key
    and status (never a stale value)."""
    rows, err = client.spaces_detailed()
    if err and not rows:
        return _RESOLVE_ERROR, None, f"list failed: {err}"

    matches = [r for r in (rows or []) if r.get("key") == key]
    if not matches:
        return _RESOLVE_ABSENT, None, None
    if len(matches) > 1:
        return _RESOLVE_AMBIGUOUS, None, len(matches)

    item = matches[0]
    # The write target is the id (Cloud v2) or the key (DC v1). A single match
    # whose targeting handle cannot be read must NOT be a false 'already gone'.
    if client.conn.deployment == "cloud":
        if not item.get("id"):
            return _RESOLVE_ERROR, item, f"matched key {key!r} but no id field"
    else:
        if not item.get("key"):
            return _RESOLVE_ERROR, item, f"matched key {key!r} but no key field"
    return _RESOLVE_OK, item, None


def _space_is_archived(client, key):
    """Re-read the space list and report whether the space KEY is archived.

    Returns True only when a live row with this key reports status=='archived'.
    A read error or a still-current row returns False so closure is never
    assumed from a 2xx (it must be PROVEN by the re-read)."""
    rows, err = client.spaces_detailed()
    if err and not rows:
        return False     # conservative: cannot prove archived → not closed
    for r in (rows or []):
        if r.get("key") == key:
            return (r.get("status") == "archived")
    # The space is gone entirely (deleted out from under us) — also closed.
    return True


def _space_page_count(client, space_key):
    """Re-verify a space's CURRENT page count at apply time (C1).

    Returns (count: int, error: str | None). cql_count returns an int or an
    ERR<status> string on failure; an unreadable count is treated
    conservatively by the caller as non-empty (do not archive)."""
    # Escape the key (defense in depth): this gates a destructive archive, so a
    # key carrying a `"` must never break out of the CQL literal and make the
    # emptiness check query the wrong space.
    cnt = client.cql_count(f'space="{escape_query_key(space_key)}" and type=page')
    if isinstance(cnt, int):
        return cnt, None
    return 1, f"page-count read failed: {cnt}"


def _group_present(client, name):
    """Re-check post-delete: True if a group with this name is still present."""
    names, _counts, _capped, err = client.groups_with_counts()
    if err and not names:
        return True      # conservative: treat fetch error as still-present
    return name in (names or [])


def _group_member_count(client, name):
    """Re-verify a group's CURRENT member count at apply time (C2).

    Returns (count: int, error: str | None). groups_with_counts probes member
    counts; a name absent from the count map (e.g. capped out) cannot be
    confirmed empty, so it is treated conservatively as non-empty."""
    names, counts, _capped, err = client.groups_with_counts()
    if err and not names:
        return 1, f"member-count read failed: {err}"
    if name not in counts:
        # Not in the probed subset → cannot prove empty.
        return 1, "member count not in probed subset"
    cnt = counts.get(name)
    if not isinstance(cnt, int):
        return 1, "member count not an integer"
    return cnt, None


def apply_confluence_fixes(
    client,
    findings: list[dict],
    log,
    expected_api_base: str | None = None,
    dry_run: bool = False,
    record_sink=None,
) -> tuple[int, int]:
    """Apply all app-tier Confluence findings from *findings* against *client*.

    Parameters
    ----------
    client            : ConfluenceClient aimed at the audited source instance.
    findings          : env findings list (each carries kind + name + detail).
    log               : callable(record) — called for every API action.
    expected_api_base : when provided, client.api_base must match exactly; a
                        mismatch raises ValueError before any write.
    dry_run           : when True, run every read-only guard but issue NO write —
                        each would-archive/would-delete is logged as a
                        WOULD-* record and tallied into closed (a safe preview).
    record_sink       : optional callable(record) invoked the instant EACH record
                        is emitted (fix_id already stamped) — a durable write-
                        through so a destructive op's record survives a crash that
                        hits right after it fired (review Bug 4). None keeps the
                        legacy buffer-then-finalize behaviour; must be thread-safe.

    Returns
    -------
    (closed, still_open) integers for _finalize_fix verdict logic. In dry_run the
    first value is the would-close count.
    """
    # --- identity guard (must be first, before any HTTP) ---
    if expected_api_base is not None and client.api_base != expected_api_base:
        raise ValueError(
            f"apply_confluence_fixes client mismatch: expected api_base "
            f"{expected_api_base!r}, got {client.api_base!r}; "
            f"writes must flow through the audited environment client only")

    # --- L1: destructive-ops hard cap (blast-radius limit, before any HTTP) ---
    n_destructive = sum(1 for f in findings if _is_destructive(f))
    cap = _destructive_cap()
    if n_destructive > cap:
        raise DestructiveCapExceeded(
            f"refusing to apply {n_destructive} destructive operation(s) in one "
            f"batch — the cap is {cap}. Trim the selection or raise "
            f"MA_MAX_DESTRUCTIVE deliberately.")

    # --- L2: shared circuit-breaker for the whole batch ---
    breaker = _WriteBreaker(_breaker_threshold())

    # --- Concurrency: fan the INDEPENDENT per-finding work out over a bounded
    # pool, exactly as the Jira path does. Each task buffers its own records and
    # returns ((c, s), records); the main thread replays them in INPUT order, so
    # the log + tallies are identical regardless of completion order. A task that
    # raises is isolated by map_results. Only the breaker is shared state.
    def _task(finding):
        records: list = []
        # Stamp fix_id at emission (not after the worker) so a streamed record is
        # complete if a crash follows; stream the instant each record is emitted.
        kind = finding.get("kind") or "env_fix"

        def emit(r):
            if not r.get("fix_id"):
                r["fix_id"] = kind
            records.append(r)
            if record_sink is not None:
                record_sink(r)

        try:
            cs = _apply_one_confluence(client, finding, emit, breaker,
                                       dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — isolate; KEEP buffered records
            cs = exc
        return cs, records

    results = map_results(findings, _task, apply_worker_count())

    closed = 0
    still_open = 0
    for finding, res in zip(findings, results):
        if isinstance(res, Exception):
            crash = _rec(finding.get("name") or "", "ERROR", "-", 0, False,
                         error=f"apply worker crashed: {res}")
            crash["fix_id"] = finding.get("kind") or "env_fix"
            log(crash)
            if record_sink is not None:    # built outside the worker -> stream here
                record_sink(crash)
            still_open += 1
            continue
        cs, records = res
        for r in records:
            log(r)        # in-memory replay only; already streamed in the worker
        if isinstance(cs, Exception):
            crash = _rec(finding.get("name") or "", "ERROR", "-", 0, False,
                         error=f"apply worker crashed: {cs}")
            crash["fix_id"] = finding.get("kind") or "env_fix"
            log(crash)
            if record_sink is not None:
                record_sink(crash)
            still_open += 1
            continue
        c, s = cs
        closed += c
        still_open += s

    return closed, still_open


def _apply_one_confluence(client, finding, log, breaker, dry_run=False):
    """Process ONE Confluence finding end-to-end, emitting records via `log`.
    Returns (closed_delta, still_open_delta). A pure per-finding unit sharing only
    the thread-safe `breaker`, so the driver can run many concurrently.

    dry_run runs every read-only guard then stops at the write (WOULD-* record)."""
    kind = finding.get("kind") or ""
    name = finding.get("name") or ""

    # --- tier re-derivation (I4): never trust client; derive from registry ---
    fix_entry = _FIXES.get(kind)
    if fix_entry is None or fix_entry.get("tier") != "app":
        # Not an app-tier kind — skip silently (defence in depth).
        return 0, 0

    if kind not in _CONFLUENCE_APP_TIER_SCOPE:
        # Belt-and-suspenders: a Jira app-tier kind (or a future Confluence app
        # kind added to _FIXES before this module) is refused here.
        log(_rec(name, "SKIP", "-", 0, False,
                 error=f"kind {kind!r} not in confluence apply scope"))
        return 0, 1

    if kind == "empty_space":
        return _apply_empty_space(client, name, log, breaker, dry_run=dry_run)
    return _apply_empty_group(client, name, log, breaker,
                              dry_run=dry_run)  # confluence_empty_group


def _apply_empty_space(client, name, log, breaker=None, dry_run=False):
    """Archive a single empty space. Returns (closed_delta, still_open_delta)."""
    if breaker is None:
        breaker = _NULL_BREAKER
    list_path = ("/api/v2/spaces" if client.conn.deployment == "cloud"
                 else "/rest/api/space")

    outcome, item, detail = _resolve_space(client, name)

    if outcome == _RESOLVE_ERROR:
        log(_rec(name, "GET", list_path, 0, False,
                 error=f"resolve failed: {detail}"))
        return 0, 1
    if outcome == _RESOLVE_ABSENT:
        log(_rec(name, "GET", list_path, 200, True, error="already absent"))
        return 1, 0     # idempotent: already gone counts as closed
    if outcome == _RESOLVE_AMBIGUOUS:
        log(_rec(name, "GET", list_path, 200, True,
                 error=f"skipped — name is ambiguous ({detail} matches)"))
        return 0, 1

    # outcome == _RESOLVE_OK
    # --- idempotency: already archived → no-op, closed ---
    if item.get("status") == "archived":
        log(_rec(name, "GET", list_path, 200, True,
                 error="already archived"))
        return 1, 0

    space_key = item.get("key") or ""
    # --- C1: re-verify the space is STILL empty against live state ---
    pages, perr = _space_page_count(client, space_key)
    if pages > 0:
        log(_rec(name, "GET", "/rest/api/search", 200, True,
                 error=(f"skipped — space now has {pages} page(s)"
                        if perr is None
                        else f"skipped — could not confirm empty ({perr})")))
        return 0, 1

    # --- L2: circuit-breaker — do not add to a server-side failure storm ---
    if breaker.should_block():
        log(_rec(name, "SKIP", list_path, 0, True, error=_BREAKER_SKIP_MSG))
        return 0, 1

    # --- DRY RUN: guards passed; record intent and stop (no archive) ---
    if dry_run:
        log(_rec(name, "WOULD-ARCHIVE", list_path, 0, True,
                 error="dry run — verified safe to archive; not archived",
                 snapshot=item))
        return 1, 0

    # --- perform the archive against the freshly-fetched id (Cloud) / key (DC) ---
    target = item.get("id") if client.conn.deployment == "cloud" else space_key
    st, d = client.archive_space(target)
    breaker.record(st)
    write_path = (f"/api/v2/spaces/{target}" if client.conn.deployment == "cloud"
                  else f"/rest/api/space/{target}")
    ok = 200 <= st < 300      # st < 0 (transport failure) is NOT a write
    log(_rec(name, "PUT", write_path, st, ok,
             error=None if ok else str(d)[:200], snapshot=item))
    if not ok:
        return 0, 1

    # --- prove closure: re-read to confirm the space is archived ---
    if _space_is_archived(client, name):
        return 1, 0
    log(_rec(name, "GET", list_path, 200, False,
             error="still current after archive"))
    return 0, 1


def _apply_empty_group(client, name, log, breaker=None, dry_run=False):
    """Delete a single empty group. Returns (closed_delta, still_open_delta)."""
    if breaker is None:
        breaker = _NULL_BREAKER
    list_path = "/rest/api/group"

    names, _counts, _capped, err = client.groups_with_counts()
    if err and not names:
        log(_rec(name, "GET", list_path, 0, False,
                 error=f"resolve failed: list failed: {err}"))
        return 0, 1

    matches = [g for g in (names or []) if g == name]
    if not matches:
        log(_rec(name, "GET", list_path, 200, True, error="already absent"))
        return 1, 0     # idempotent: already gone counts as closed
    if len(matches) > 1:
        # Group names are unique in the directory, but guard anyway (H2).
        log(_rec(name, "GET", list_path, 200, True,
                 error=f"skipped — name is ambiguous ({len(matches)} matches)"))
        return 0, 1

    # --- C2: re-verify the group is STILL empty against live state ---
    members, merr = _group_member_count(client, name)
    if members > 0:
        log(_rec(name, "GET", "/rest/api/group/member", 200, True,
                 error=(f"skipped — group now has {members} member(s)"
                        if merr is None
                        else f"skipped — could not confirm empty ({merr})")))
        return 0, 1

    # --- L2: circuit-breaker — do not add to a server-side failure storm ---
    if breaker.should_block():
        log(_rec(name, "SKIP", list_path, 0, True, error=_BREAKER_SKIP_MSG))
        return 0, 1

    # --- DRY RUN: guards passed; record intent and stop (no delete) ---
    if dry_run:
        log(_rec(name, "WOULD-DELETE", list_path, 0, True,
                 error="dry run — verified safe to delete; not deleted",
                 snapshot={"name": name, "type": "group"}))
        return 1, 0

    # --- perform the delete by name ---
    st, d = client.delete_group(name)
    breaker.record(st)
    ok = 200 <= st < 300      # st < 0 (transport failure) is NOT a write
    log(_rec(name, "DELETE", list_path, st, ok,
             error=None if ok else str(d)[:200],
             snapshot={"name": name, "type": "group"}))
    if not ok:
        return 0, 1

    # --- prove closure: re-read to confirm the group is gone ---
    if _group_present(client, name):
        log(_rec(name, "GET", list_path, 200, False,
                 error="still present after delete"))
        return 0, 1
    return 1, 0
