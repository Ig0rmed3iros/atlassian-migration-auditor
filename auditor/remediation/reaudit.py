"""Prove closure by re-reading the live target — never trust an apply 200.

For each touched config finding, check whether the fix actually closed it on
the target. Closure is judged BY FINDING KIND, not by object-name presence
alone:
  * missing_in_tgt  — closed iff the named object now exists on the target.
  * option_mismatch — the field already exists, so presence is meaningless;
    closed only when the previously-missing options are now on the target field
    (I8 — judging this by name presence always reported it closed → false CLEAN).
  * everything else (type_mismatch, structure/field_mismatch, value/populate-
    style) cannot be cheaply re-verified here → counted as 'not_verifiable' and
    surfaced honestly, never counted as closed.
A finding whose area was not touched is left 'unchanged' (not re-checked) so the
verdict reflects only what we acted on. A touched finding whose area has no
_PRECHECK entry is also 'not_verifiable'."""
from __future__ import annotations

import logging

from .apply import _PRECHECK, _exists, _resolve_target_field_id

log = logging.getLogger(__name__)


def _target_field_option_set(tgt_client, name) -> set:
    """Flat set of option values currently on the named target field (across
    its contexts). Empty set if the field or its options can't be read — which
    keeps an option_mismatch finding 'still open' rather than falsely closed."""
    fid = _resolve_target_field_id(tgt_client, name)
    if not fid:
        return set()
    st, d = tgt_client.req(f"/rest/api/3/field/{fid}/context")
    ctxs = d.get("values") if isinstance(d, dict) else d
    if st != 200 or not isinstance(ctxs, list):
        return set()
    opts = set()
    for c in ctxs:
        cid = c.get("id")
        st2, d2 = tgt_client.req(
            f"/rest/api/3/field/{fid}/context/{cid}/option")
        rows = d2.get("values") if isinstance(d2, dict) else d2
        if st2 == 200 and isinstance(rows, list):
            opts |= {x.get("value") for x in rows if x.get("value")}
    return opts


def _option_mismatch_closed(tgt_client, f) -> bool:
    missing = (f.get("detail") or {}).get("missing_options_in_tgt") or []
    if not missing:
        return False
    have = _target_field_option_set(tgt_client, f.get("name"))
    return all(v in have for v in missing)


def compute_closure(tgt_client, findings: list, touched_areas: set) -> dict:
    closed = still_open = unchanged = not_verifiable = 0
    detail = []
    for f in findings:
        area = f.get("area")
        kind = f.get("kind")
        if area not in touched_areas:
            unchanged += 1
            continue
        if kind == "option_mismatch" and area == "custom_fields":
            ok = _option_mismatch_closed(tgt_client, f)
            closed += ok
            still_open += not ok
            detail.append({"finding": f"{area}/{f.get('name')} (options)",
                           "closed": ok})
            continue
        if kind != "missing_in_tgt" or area not in _PRECHECK:
            # type/structure/field_mismatch, value/populate-style, or a touched
            # area with no presence pre-check — none cheaply re-verifiable here.
            not_verifiable += 1
            log.warning("re-audit: finding %r (kind=%r, area=%r) cannot be "
                        "verified — counting as not_verifiable",
                        f.get("name"), kind, area)
            continue
        present = _exists(tgt_client, area, f.get("name"))
        if present:
            closed += 1
            detail.append({"finding": f"{area}/{f.get('name')}", "closed": True})
        else:
            still_open += 1
            detail.append({"finding": f"{area}/{f.get('name')}", "closed": False})
    return {"closed": closed, "still_open": still_open,
            "unchanged": unchanged, "not_verifiable": not_verifiable,
            "detail": detail}
