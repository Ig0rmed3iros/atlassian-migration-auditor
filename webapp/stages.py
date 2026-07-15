"""Production stage functions: the only module that knows both the core
library and the store. Each stage is fn(ctx); ctx comes from RunEngine.

Every product-specific call goes through the Connector resolved from
`migrations.product` — stages never import jira/confluence modules directly,
so adding a product means registering a connector, not touching this file.
Deployment differences (auth, api paths, pagination, dialect) live inside
each product client; the only deployment branches HERE are the two honest
capability gates: blind spots (jira cloud only) and cross_dialect compare.

ctx keys written here and consumed downstream:
  clients (src, tgt) · connector · scope rows · blind_spots ·
  project_results · issue_findings · config_result
"""
from __future__ import annotations

import os

import httpx

from auditor import scope as scope_mod
from auditor.client import Connection
from auditor.connectors import (EXTRACT_FORMAT, extract_format, get_connector,
                                supports_blind_spots)
from auditor.envaudit._pool import map_results
from auditor.remediation.payload import capture_config_payload
from auditor.remediation.usergap import detect_user_gaps
from auditor.remediation.values import capture_fields_values
from . import oauth as oauth_mod
from .store import Store


def _oauth_secret(store: Store) -> tuple[str | None, str | None]:
    cid = store.settings_get("oauth_client_id")
    enc = store.settings_get("oauth_client_secret_enc")
    sec = store.decrypt(enc.encode())["secret"] if enc else None
    return cid, sec


def build_clients(store: Store, migration_id: int,
                  http: httpx.Client | None = None,
                  require_both: bool = True):
    """Returns (src, tgt, connector). The connector is resolved once from the
    migration's product and minted clients come from connector.make_client,
    so callers stay product-blind."""
    mig = store.get_migration(migration_id)
    connector = get_connector(mig["product"])
    out = []
    cid, csec = _oauth_secret(store)
    for role in ("source", "target"):
        row = store.get_connection(migration_id, role)
        if row is None:
            if require_both:
                raise RuntimeError(f"no {role} connection configured")
            out.append(None)
            continue
        secret = store.connection_secret(row)
        deployment = row["deployment"] or "cloud"
        if row["auth_type"] == "pat":
            # dc secrets carry no email key (PAT-as-Bearer): .get keeps one
            # construction path for both deployments.
            conn = Connection(auth_type="pat", site_url=row["site_url"],
                              deployment=deployment,
                              email=secret.get("email"),
                              api_token=secret["token"])
        else:
            if connector.product != "jira":
                # The gateway token exchange + /ex/jira/{cloud_id} base are
                # Jira-Cloud-only; minting an oauth client for another
                # product would call the wrong API with real credentials.
                raise RuntimeError("oauth is only supported for Jira Cloud")
            conn = Connection(auth_type="oauth", site_url=row["site_url"],
                              deployment=deployment,
                              cloud_id=row["cloud_id"],
                              access_token=secret.get("access_token"),
                              refresh_token=secret.get("refresh_token"),
                              expires_at=float(secret.get("expires_at") or 0))
            conn_id = row["id"]
            if cid and csec:
                conn.refresh_fn = lambda rt, _cid=cid, _cs=csec: \
                    oauth_mod.refresh_tokens(_cid, _cs, rt, http=http)
            conn.on_tokens_refreshed = lambda c, _id=conn_id: \
                store.update_connection_secret(_id, {
                    "access_token": c.access_token,
                    "refresh_token": c.refresh_token,
                    "expires_at": c.expires_at})
        out.append(connector.make_client(conn, http))
    return out[0], out[1], connector


# ------------------------------------------------------------------ stages
def _say(ctx, phase, msg, level="info"):
    ctx["store"].add_event(ctx["run_id"], phase, level, msg)


def _extract_workers() -> int:
    """Concurrency for a project's source/target extraction. Default 2 (the two
    sides hit different instances with separate rate limits). MA_EXTRACT_WORKERS
    overrides; clamped to >= 1 (1 == forced sequential)."""
    raw = os.environ.get("MA_EXTRACT_WORKERS")
    if raw is None:
        return 2
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 2
    return max(1, n)


def stage_verify(ctx):
    store: Store = ctx["store"]
    src, tgt, connector = build_clients(store, ctx["migration_id"])
    ctx["src"], ctx["tgt"] = src, tgt
    ctx["connector"] = connector
    # The run engine's finalize templates the summary headlines on these —
    # plain strings so runs.py stays free of a connectors import.
    ctx["item_label"] = connector.item_label
    ctx["container_label"] = connector.container_label
    for role, cl in (("source", src), ("target", tgt)):
        me = connector.verify(cl)   # raises ClientError loudly on auth failure
        row = store.get_connection(ctx["migration_id"], role)
        store.mark_connection_verified(row["id"], me.get("email") or "")
        ctx[f"{role}_account_id"] = me.get("account_id")
        # DC identities may lack a display name and a connector's verify may
        # omit the key entirely — never KeyError after a successful auth.
        _say(ctx, "verify",
             f"{role}: authenticated as {me.get('display_name') or '?'}")


def stage_scope(ctx):
    connector = ctx["connector"]
    src, tgt = ctx["src"], ctx["tgt"]
    sp, serr = connector.list_containers(src)
    tp, terr = connector.list_containers(tgt)
    if serr or terr:
        raise RuntimeError(
            f"{connector.container_label} enumeration failed: "
            f"src={serr} tgt={terr}")
    matched = scope_mod.match_projects(sp, tp)
    selected = ctx["params"].get("projects") or \
        [m["key"] for m in matched["matched"]]
    ctx["selected"] = [m for m in matched["matched"] if m["key"] in selected]
    requested = ctx["params"].get("projects")
    if requested:
        matched_keys = {m["key"] for m in matched["matched"]}
        for k in requested:
            if k not in matched_keys:
                _say(ctx, "scope",
                     f"requested {connector.container_label} {k!r} is not "
                     f"matched on both sides — skipped", "warn")
    ctx["scope"] = matched
    rows = []
    for m in ctx["selected"]:
        m["src_count"] = connector.count_items(src, m["key"])
        m["tgt_count"] = connector.count_items(tgt, m["key"])
        rows.append({"key": m["key"], "name": m["name"],
                     "src_count": m["src_count"] if isinstance(m["src_count"], int) else None,
                     "tgt_count": m["tgt_count"] if isinstance(m["tgt_count"], int) else None,
                     "status": "scoped"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)
    _say(ctx, "scope",
         f"{len(ctx['selected'])} {connector.container_label}(s) in scope; "
         f"{len(matched['source_only'])} source-only, "
         f"{len(matched['target_only'])} target-only")


def stage_permissions(ctx):
    connector = ctx["connector"]
    keys = [m["key"] for m in ctx["selected"]]
    spots = []
    for side, cl in (("source", ctx["src"]), ("target", ctx["tgt"])):
        if not supports_blind_spots(connector, cl.conn.deployment):
            # R9 honesty: never report fake zero-blind-spot confidence for a
            # side we cannot actually probe.
            _say(ctx, "permissions",
                 f"blind-spot detection not supported for {connector.product} "
                 f"on {cl.conn.deployment} — {side} counts unverified", "warn")
            continue
        for s in connector.detect_blind_spots(cl, keys):
            s["side"] = side
            spots.append(s)
            if s["blind_spot"]:
                _say(ctx, "permissions",
                     f"BLIND SPOT on {side} {s['key']}: search sees "
                     f"{s['search_count']} of {s['insight_count']}. Fix access "
                     f"(elevation) and re-run before trusting counts.", "warn")
            elif s.get("indeterminate"):
                _say(ctx, "permissions",
                     f"COULD NOT VERIFY {side} {s['key']}: issue-count lookup "
                     f"errored while insight reports issues exist — extraction "
                     f"will proceed on an unverified count.", "warn")
    ctx["blind_spots"] = spots
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    blind_keys = {s["key"] for s in spots if s["blind_spot"]}
    for r in rows:
        r["blind_spot"] = 1 if r["key"] in blind_keys else 0
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_extract(ctx):
    connector = ctx["connector"]
    reuse = bool(ctx["params"].get("reuse_extracts_from"))
    workers = _extract_workers()
    for m in ctx["selected"]:
        # Partition this project's sides into reuse (skip) vs fetch.
        tasks = []
        for side, cl in (("src", ctx["src"]), ("tgt", ctx["tgt"])):
            path = os.path.join(ctx["workspace"], side,
                                f"{m['key']}.core.jsonl.gz")
            if reuse and os.path.exists(path):
                fmt = extract_format(path)
                if fmt == EXTRACT_FORMAT:
                    _say(ctx, "extract",
                         f"{side} {m['key']}: reusing cached extract")
                    continue
                _say(ctx, "extract",
                     f"{side} {m['key']}: cached extract has incompatible "
                     f"format {fmt} (current {EXTRACT_FORMAT}) — "
                     f"re-extracting", "warn")
            total = m["src_count"] if side == "src" else m["tgt_count"]
            tasks.append((side, cl, path, total))

        # The two sides hit DIFFERENT instances — run them concurrently.
        # Projects stay sequential (no extra same-instance pressure).
        def _extract_one(task, _m=m):
            side, cl, path, total = task
            res = connector.extract(
                cl, _m["key"], path,
                progress=lambda n, k=_m["key"], s=side, t=total: _say(
                    ctx, "extract",
                    f"{s} {k}: {n}/{t if isinstance(t, int) else '?'}"))
            return side, res

        # Gate on the MAIN thread, in input (src, tgt) order, preserving the
        # fail-loud verified-count semantics. A worker that raised is returned
        # as an Exception by map_results — re-raise it, never swallow.
        for outcome in map_results(tasks, _extract_one, workers=workers):
            if isinstance(outcome, Exception):
                raise outcome
            side, res = outcome
            if not res["verified"]:
                if isinstance(res["approx"], int):
                    raise RuntimeError(
                        f"{side} {m['key']}: extracted {res['extracted']} but "
                        f"approximate-count says {res['approx']} — extraction "
                        f"not complete, refusing to compare")
                _say(ctx, "extract",
                     f"{side} {m['key']}: approximate-count unavailable "
                     f"({res['approx']}); proceeding on extracted="
                     f"{res['extracted']}", "warn")


def stage_compare(ctx):
    connector = ctx["connector"]
    # Mixed jira deployments author different body dialects (wiki vs ADF):
    # flag the comparison so content findings carry the representation-
    # sensitive badge. Confluence serves storage XHTML on both deployments.
    cross = (connector.product == "jira" and
             ctx["src"].conn.deployment != ctx["tgt"].conn.deployment)
    results, all_findings = {}, []
    for m in ctx["selected"]:
        out = connector.compare(
            m["key"],
            os.path.join(ctx["workspace"], "src", f"{m['key']}.core.jsonl.gz"),
            os.path.join(ctx["workspace"], "tgt", f"{m['key']}.core.jsonl.gz"),
            cross_dialect=cross)
        results[m["key"]] = out
        all_findings.extend(out["findings"])
        s = out["stats"]
        _say(ctx, "compare",
             f"{m['key']}: common={s['common']} holes={s['missing_in_tgt']} "
             f"tails={s['tails']} mismatched={s['issues_with_mismatches']} "
             f"fidelity={s['fidelity_pct']}%")
    ctx["project_results"] = results
    ctx["issue_findings"] = all_findings
    rows = ctx["store"].get_run_projects(ctx["run_id"])
    for r in rows:
        st = results.get(r["key"], {}).get("stats")
        if st:
            r.update({"missing": st["missing_in_tgt"], "tail_count": st["tails"],
                      "fidelity_pct": st["fidelity_pct"], "status": "compared"})
    ctx["store"].set_run_projects(ctx["run_id"], rows)


def stage_config(ctx):
    connector = ctx["connector"]
    # params["jsm_projects"] narrows the jira JSM sweep; the connector
    # contract calls them containers so confluence reuses the slot.
    containers = ctx["params"].get("jsm_projects") or \
        [m["key"] for m in ctx["selected"]]
    result = connector.audit_config(
        ctx["src"], ctx["tgt"], containers=containers,
        workspace=ctx["workspace"],
        progress=lambda msg: _say(ctx, "config", msg))
    # R1: gather the full source definition for every fixable finding, in this
    # same scan, so remediation never re-reads. Bounded to findings; jira only
    # (confluence config = macro inventory, captured in stage_usergap path).
    if ctx["params"].get("capture_remediation", True) and connector.product == "jira":
        for f in result.get("findings", []):
            payload = capture_config_payload(ctx["src"], f)
            if payload is not None:
                f["fix_payload"] = payload
    ctx["config_result"] = result


def undo_migration_elevations(store, migration_id, src_client, tgt_client, log=None):
    """Undo every still-active elevation recorded for any run of this migration.
    Safe because only one run per migration is ever active. Best-effort: a client
    that is None (e.g. verify failed) skips that side."""
    clients = {"source": src_client, "target": tgt_client}
    for r in store.list_runs(migration_id):
        for side in ("source", "target"):
            raw = store.settings_get(f"elevation:{r['id']}:{side}")
            if not raw:
                continue
            cl = clients.get(side)
            if cl is None:
                continue
            import json as _json
            data = _json.loads(raw)
            from auditor.permissions import undo_elevation
            result = undo_elevation(cl, data["grants"], data["role_id"],
                                    data["account_id"])
            # Only drop the local record once EVERY de-grant actually succeeded.
            # If any server-side DELETE failed, the grant is still live — keep the
            # record so a later run/undo retries it; deleting it here would orphan
            # a live privilege grant that no sweep could ever find (review).
            if all(row.get("ok") for row in result):
                store.settings_delete(f"elevation:{r['id']}:{side}")
                if log:
                    log(side, r["id"])


def stage_capture_values(ctx):
    """Bounded value capture (R2). Runs after config; only when remediation
    capture is on AND there are missing custom-field findings. No-op otherwise
    so a lean or confluence run stores nothing."""
    if not ctx["params"].get("capture_remediation", True):
        return
    if ctx["connector"].product != "jira":
        return
    findings = ctx.get("config_result", {}).get("findings", [])
    missing = [f for f in findings
               if f.get("area") == "custom_fields"
               and f.get("kind") == "missing_in_tgt"
               and (f.get("fix_payload") or {}).get("field_id")]
    keys = [m["key"] for m in ctx["selected"]]
    if not missing:
        return
    out_dir = os.path.join(ctx["workspace"], "fix", "values")
    field_ids = [f["fix_payload"]["field_id"] for f in missing]
    counts = capture_fields_values(ctx["src"], keys, field_ids, out_dir)
    for f in missing:
        fid = f["fix_payload"]["field_id"]
        out = os.path.join(out_dir, f"{fid}.jsonl.gz")
        f["fix_payload"]["values_file"] = os.path.relpath(out, ctx["workspace"])
        f["fix_payload"]["values_count"] = counts.get(fid, 0)
    _say(ctx, "capture_values",
         f"captured {sum(counts.values())} source value(s) across "
         f"{len(field_ids)} field(s) in one pass")


def stage_usergap(ctx):
    if ctx["connector"].product != "jira":
        return
    keys = [m["key"] for m in ctx["selected"]]
    gaps = detect_user_gaps(ctx["workspace"], keys, ctx["tgt"])
    for g in gaps:
        # Do NOT put the username in `project` — that created a phantom project
        # entry in the per-project distribution for each unresolved user.
        # The username already lives in detail.display_name (set by
        # detect_user_gaps) so guidance._user_gap can still list every user.
        g.pop("name", None)
        g.setdefault("project", "")
    ctx.setdefault("issue_findings", []).extend(gaps)
    if gaps:
        _say(ctx, "compare", f"{len(gaps)} referenced user(s) unresolved on "
             f"the target — see remediation guidance", "warn")


def build_stages() -> dict:
    # stage_usergap and stage_capture_values ride inside the compare/config
    # phases (compose wrappers below) so the audit captures user gaps and
    # field values without adding phases to AUDIT_PHASES.
    return {"verify": stage_verify, "scope": stage_scope,
            "permissions": stage_permissions, "extract": stage_extract,
            "compare": _compare_then_usergap, "config": _config_then_values}


def _compare_then_usergap(ctx):
    stage_compare(ctx)
    stage_usergap(ctx)


def _config_then_values(ctx):
    stage_config(ctx)
    stage_capture_values(ctx)
