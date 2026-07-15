"""FastAPI app: wizard, runs, SSE, analysis pages, settings, elevation."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from auditor.client import ClientError, Connection
from auditor.connectors import get_connector
from auditor.permissions import (apply_elevation, find_admin_role_id,
                                 undo_elevation)
from auditor.solutions import find_solutions, finding_signature
from .analysis import make_router
from .anthropic_key import anthropic_client
from .anthropic_key import save_key as save_anthropic_key
from .anthropic_key import load_key as load_anthropic_key
from .ai_provider import (get_provider_choice, load_openai_config,
                          save_openai_config, set_provider_choice,
                          load_claude_cli_config, save_claude_cli_config)
from .config import Config, assert_safe_bind, load_config
from . import compare as _compare
from . import export as _export
from . import report as _report
from .env_fix_routes import make_env_fix_router
from .env_fix_stages import build_env_fix_stages
from .env_stages import build_env_stages
from .fix_stages import build_fix_stages
from .oauth import accessible_resources, build_authorize_url, exchange_code
from .remediate import make_fix_router
from .runs import RunEngine
from auditor.scope import match_projects
from .stages import build_clients, build_stages, undo_migration_elevations
from .store import Store

_HERE = os.path.dirname(__file__)

# State-changing methods get a same-origin check (CSRF defense). GET/HEAD/
# OPTIONS are safe and never blocked.
_STATE_CHANGING = frozenset(("POST", "PUT", "PATCH", "DELETE"))


def _origin_tuple(url: str):
    """(scheme, host, port) of a URL, with the default port filled in so
    http://h and http://h:80 compare equal. Returns None for an unparseable
    or scheme/host-less value."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return (parts.scheme.lower(), parts.hostname.lower(), port)


class CSRFOriginMiddleware(BaseHTTPMiddleware):
    """Cookieless CSRF defense via a same-origin check on state-changing
    requests (POST/PUT/PATCH/DELETE).

    This app holds live Jira/Confluence admin credentials and exposes POST
    routes that perform DESTRUCTIVE live writes and local deletes. With no
    session/cookie there is no token to forge, but a malicious page in the
    operator's browser could still auto-submit a form to localhost. A browser
    always sends Origin (and/or Referer) on such a cross-site submit, so:

      * If Origin is present, its scheme://host:port MUST equal this request's
        own host (derived from the Host header via request.base_url).
      * Else if Referer is present, its origin must match the same way.
      * If BOTH are absent (curl, the test client, server-to-server) ALLOW —
        CSRF is a browser-only attack and non-browser tools never send these.

    A mismatch is rejected with 403. Safe methods are never blocked."""

    async def dispatch(self, request, call_next):
        if request.method in _STATE_CHANGING:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer
            if source:                     # browser-supplied: must be same-origin
                own = _origin_tuple(str(request.base_url))
                claimed = _origin_tuple(source)
                if own is None or claimed is None or claimed != own:
                    return PlainTextResponse(
                        "cross-origin request blocked", status_code=403)
            else:
                # No Origin/Referer: a MODERN browser still sends Sec-Fetch-Site,
                # so a cross-site/same-site submit is caught even without Origin
                # (closing the no-Origin CSRF gap). A genuine non-browser caller
                # (curl, server-to-server) sends none of the three -> allow.
                sfs = request.headers.get("sec-fetch-site")
                if sfs and sfs.lower() not in ("same-origin", "none"):
                    return PlainTextResponse(
                        "cross-origin request blocked", status_code=403)
        return await call_next(request)


def _reconstruct_solution_finding(store, run_id, *, kind, area, name, project,
                                  src_key, tgt_key, field):
    """Rebuild a solution-query finding from the STORED audit rows, matching on
    safe identifiers only (spec R7). Issue findings match by keys/field and
    NEVER carry the issue summary; config findings carry only the audited
    object name. Returns None when no stored finding matches the request."""
    # Config finding: an area + object name, no issue keys.
    if area and not (src_key or tgt_key):
        for row in store.query_config(run_id, area):
            if row.get("kind") == kind and (row.get("name") or "") == name:
                return {"kind": kind, "area": area, "name": row.get("name")}
        return None
    # Issue finding: matched by keys + field; the summary column is never read.
    for row in store.all_issue_findings(run_id):
        if (row.get("kind") == kind
                and (row.get("project") or "") == project
                and (row.get("src_key") or "") == src_key
                and (row.get("tgt_key") or "") == tgt_key
                and (row.get("field") or "") == field):
            return {"kind": kind, "area": None, "project": row.get("project"),
                    "src_key": row.get("src_key"), "tgt_key": row.get("tgt_key"),
                    "field": row.get("field"), "name": None}
    return None


def _app_version() -> str:
    try:
        from importlib.metadata import version
        return version("migration-auditor")
    except Exception:  # noqa: BLE001 — not installed as a dist (source run)
        return "0.0.0"


APP_VERSION = _app_version()


def setup_logging() -> None:
    """Operator-facing logging. Level from MA_LOG_LEVEL (default INFO). Called
    only from the CLI entry points — never from create_app, so importing the app
    (e.g. in tests) does not reconfigure the root logger."""
    level = os.environ.get("MA_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run_audit_cli(args, out=None, err=None) -> int:
    """Headless environment audit for CI/cron: build a client from flags + the
    MA_AUDIT_TOKEN env var (never a flag — keeps the PAT out of shell history),
    run the deterministic env-audit pipeline, print a summary (or --json), and
    return an EXIT CODE — 0 when the verdict is better than --fail-on, 2 when it
    meets/exceeds it, 1 on an operational error. Streams/files are injected so
    this is unit-testable without spawning a process."""
    import sys
    from auditor.connectors import get_connector
    from auditor.envaudit.headless import VERDICT_RANK, run_env_audit
    from auditor.client import Connection
    out = out or sys.stdout
    err = err or sys.stderr
    token = os.environ.get("MA_AUDIT_TOKEN")
    if not token:
        print("error: set MA_AUDIT_TOKEN to the PAT for the audited site",
              file=err)
        return 1
    # A Cloud PAT authenticates as Basic email:token; without --email the header
    # would be the literal "None:<token>" and fail as a confusing 401. DC PATs
    # are bearer tokens with no email, so it must be OMITTED there.
    if args.deployment == "cloud" and not args.email:
        print("error: --email is required for a Cloud PAT (Basic auth); omit it "
              "only for --deployment dc", file=err)
        return 1
    client = None
    try:
        connector = get_connector(args.product)
        conn = Connection(auth_type="pat", site_url=args.site,
                          deployment=args.deployment, email=args.email,
                          api_token=token)
        client = connector.make_client(conn, None)   # init runs SSRF guard
        log = logging.getLogger("migration_auditor.audit")
        result = run_env_audit(client, connector,
                               progress=lambda m: log.info("%s", m))
    except Exception as exc:  # noqa: BLE001 — a gate must ALWAYS return an exit
        # code, never a traceback CI would read as a harness failure.
        print(f"audit failed: {exc}", file=err)
        return 1
    finally:
        # One-shot CLI: close the client we built so the pool isn't leaked.
        http = getattr(client, "http", None)
        if http is not None:
            try:
                http.close()
            except Exception:  # noqa: BLE001
                pass
    if getattr(args, "json", None):
        payload = json.dumps(result, indent=2, default=str)
        if args.json == "-":
            print(payload, file=out)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(payload)
            print(f"wrote {args.json}", file=out)
    else:
        print(f"{result['product']} {result['deployment']}: {result['verdict']} "
              f"(health {result['health_score']}/{result['grade']}, "
              f"{result['finding_total']} finding(s))", file=out)
        for h in result["headlines"]:
            print(f"  - {h}", file=out)
    # Gate: exit 2 when the verdict is at or worse than the --fail-on threshold.
    if VERDICT_RANK.get(result["verdict"], 0) >= VERDICT_RANK[args.fail_on]:
        return 2
    return 0


def _clone_modes(apply: bool, dry_run: bool) -> tuple:
    """Resolve (dry_run, scan_roles) from the CLI flags. --dry-run ALWAYS wins
    (never writes), so --apply --dry-run is a safe full dry-run, not a write."""
    effective_dry = dry_run or not apply
    scan_roles = apply or dry_run
    return effective_dry, scan_roles


def parse_pairs_csv(path: str) -> list:
    """Read a CSV with header columns 'main' and 'clone' (case-insensitive;
    extra columns ignored). Returns [(main, clone), ...], skipping blank rows.
    """
    import csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower(): c for c in (reader.fieldnames or [])}
        if "main" not in cols or "clone" not in cols:
            raise ValueError(
                f"CSV must have 'main' and 'clone' columns; got "
                f"{reader.fieldnames}")
        out = []
        for row in reader:
            main = (row.get(cols["main"]) or "").strip()
            clone = (row.get(cols["clone"]) or "").strip()
            if main or clone:
                out.append((main, clone))
        return out


def _print_clone_summary(report, dry_run) -> None:
    mode = "DRY-RUN" if dry_run else "APPLIED"
    s = report["summary"]
    print(f"[{mode}] pairs={s['pairs']} blocked={s['blocked']} "
          f"partial={s.get('partial', 0)} "
          f"groups+={s['groups_added']} roles+={s['roles_added']} "
          f"failed={s['failed']}"
          + ("" if report["scanned_roles"] else "  (roles not scanned — preview)"))
    for p in report["pairs"]:
        head = f"  {p['main']} -> {p['clone']} [{p['status']}]"
        if p["status"] in ("blocked", "noop"):
            print(head + f": {p['reason']}")
            continue
        print(head + f": +{len(p['groups']['added'])} groups, "
              f"+{len(p['roles']['added'])} roles "
              f"({len(p['groups']['already'])}/{len(p['roles']['already'])} already)")
        for f in p["groups"]["failed"] + p["roles"]["failed"]:
            print(f"      FAILED {f}")


def _write_clone_json(path, report) -> None:
    import sys
    if path == "-":
        json.dump(report, sys.stdout, indent=2); print()
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)


def run_clone_cli(args) -> int:
    """Headless clone-access. Builds a Jira client from a saved connection and
    runs the engine. Exit: 0 clean, 1 operational error, 2 blocked-or-failed."""
    import sys
    from auditor.cloneaccess import run_clone, CloneAborted, CloneError
    from auditor.client import Connection
    from auditor.connectors import get_connector
    cfg = load_config()
    store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                  secret_key=cfg.secret_key)
    # Resolve the saved connection by id (numeric) or name.
    rows = store.list_saved_connections("jira")
    match = [r for r in rows
             if str(r["id"]) == str(args.conn) or r["name"] == args.conn]
    if not match:
        print(f"no jira saved connection matching {args.conn!r}", file=sys.stderr)
        return 1
    row = match[0]
    secret = store.saved_connection_secret(row)
    conn = Connection(auth_type="pat", site_url=row["site_url"],
                      deployment=row["deployment"] or "cloud",
                      email=secret.get("email") or None,
                      api_token=secret.get("token"))
    connector = get_connector("jira")
    client = connector.make_client(conn, None)

    if args.csv:
        try:
            pairs = parse_pairs_csv(args.csv)
        except (OSError, ValueError) as e:
            print(f"CSV error: {e}", file=sys.stderr)
            return 1
    elif args.main and args.clone:
        pairs = [(args.main, args.clone)]
    else:
        print("provide --main and --clone, or --csv", file=sys.stderr)
        return 1

    dry_run, scan_roles = _clone_modes(args.apply, args.dry_run)
    try:
        report = run_clone(client, pairs, dry_run=dry_run, scan_roles=scan_roles,
                           progress=lambda m: print(f"... {m}", file=sys.stderr))
    except CloneError as e:
        partial = getattr(e, "partial", None)
        print(f"aborted: {e}", file=sys.stderr)
        if partial:
            _print_clone_summary(partial, dry_run)
            if args.json:
                _write_clone_json(args.json, partial)
        return 1

    _print_clone_summary(report, dry_run)
    if args.json:
        _write_clone_json(args.json, report)
    s = report["summary"]
    return 2 if (s["blocked"] or s["failed"]) else 0


def create_app(cfg: Config | None = None, http: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                  secret_key=cfg.secret_key)
    engine = RunEngine(
        store, os.path.join(cfg.data_dir, "migrations"),
        stages=build_stages(), fix_stages=build_fix_stages(),
        env_stages=build_env_stages(),
        env_fix_stages=build_env_fix_stages(),
        elevation_undo=lambda src, tgt, mid, rid: undo_migration_elevations(
            store, mid, src, tgt,
            log=lambda side, frm: store.add_event(
                rid, "finalize", "info",
                f"auto-undo elevation on {side} (from run {frm})")))
    engine.mark_stale_failed()
    # Bound the ever-growing events log on each boot (cheap). VACUUM is left to an
    # explicit/scheduled call so a large DB never delays startup.
    try:
        store.prune_events()
    except Exception:          # noqa: BLE001 — maintenance must never block boot
        pass

    app = FastAPI(title="Atlassian Audit Platform")
    # CSRF defense: reject cross-origin state-changing requests (the app's
    # POST routes do destructive live writes / local deletes / secret writes).
    app.add_middleware(CSRFOriginMiddleware)
    app.state.store = store
    app.state.engine = engine
    app.state.config = cfg
    app.state.http = http            # injected mock in tests; None in prod
    app.state.oauth_pending = {}     # state -> {migration_id, role, tokens?}
    templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")),
              name="static")
    app.include_router(make_router())
    app.include_router(make_fix_router(store, engine))
    app.include_router(make_env_fix_router(store, engine))
    from webapp.clone_routes import make_clone_router
    from webapp.clone_runner import CloneRunner
    _clone_runner = CloneRunner(store, lambda: app.state.http)
    app.include_router(make_clone_router(
        store, _clone_runner, lambda: app.state.http, templates))

    def page(request, name, **ctx):
        return templates.TemplateResponse(request, name, ctx)

    def _rel_time(ts: float | None) -> str:
        """Return a human-readable relative time string (e.g. '2h ago')."""
        if ts is None:
            return "—"
        delta = time.time() - ts
        if delta < 60:
            return "just now"
        if delta < 3600:
            mins = math.floor(delta / 60)
            return f"{mins}m ago"
        if delta < 86400:
            hrs = math.floor(delta / 3600)
            return f"{hrs}h ago"
        days = math.floor(delta / 86400)
        return f"{days}d ago"

    # --------------------------------------------------------- audit listing
    def _audit_rows(audit_type: str) -> list[dict]:
        """Enriched audit cards for ONE audit_type (migration | environment).

        Legacy rows predate the audit_type column; a missing value reads as
        'migration' so they keep appearing on the migration list, never lost.
        """
        migs = [m for m in store.list_migrations()
                if (m.get("audit_type") or "migration") == audit_type]
        for m in migs:
            runs = store.list_runs(m["id"])
            m["last_run"] = runs[0] if runs else None
            # source / target site labels (strip https://)
            src_conn = store.get_connection(m["id"], "source")
            tgt_conn = store.get_connection(m["id"], "target")
            m["source_site"] = (
                (src_conn.get("site_url") or "").removeprefix("https://").rstrip("/")
                if src_conn else None)
            m["target_site"] = (
                (tgt_conn.get("site_url") or "").removeprefix("https://").rstrip("/")
                if tgt_conn else None)
            # stats from last finished run
            m["projects"] = None
            m["issues_src_total"] = None
            if m["last_run"] and m["last_run"].get("status") not in ("running",):
                try:
                    stats = json.loads(m["last_run"].get("stats_json") or "{}")
                    m["projects"] = stats.get("projects")
                    m["issues_src_total"] = stats.get("issues_src_total")
                except (json.JSONDecodeError, TypeError):
                    pass
            # relative time for last run
            finished_at = (m["last_run"].get("finished_at") if m["last_run"] else None)
            started_at = (m["last_run"].get("started_at") if m["last_run"] else None)
            m["last_run_rel"] = _rel_time(finished_at or started_at)
        return migs

    # ------------------------------------------------------ operability
    @app.get("/healthz")
    def healthz():
        """Liveness + DB-reachability probe (no auth, no side effects). 200 when
        the DB answers; 503 when it doesn't, so a supervisor can restart."""
        try:
            store.db_ping()
            return JSONResponse({"status": "ok", "db": True,
                                 "version": APP_VERSION,
                                 "schema_version": store.schema_version()})
        except Exception:  # noqa: BLE001 — a probe must never raise
            return JSONResponse({"status": "degraded", "db": False,
                                 "version": APP_VERSION}, status_code=503)

    # ------------------------------------------------------ migration audits
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, error: str = ""):
        return page(request, "index.html", active_nav="migration",
                    migrations=_audit_rows("migration"),
                    section="migration", section_title="Migration audits",
                    section_short="Migrations",
                    section_sub="Compare a source and target instance — projects, "
                                "issues, attachments, config — and report gaps "
                                "before you cut over.",
                    new_label="New migration audit",
                    name_placeholder="e.g. Acme DC → Globex Cloud",
                    create_audit_type="migration", allow_confluence=True,
                    workflow_steps=["Connect", "Scope", "Extract", "Compare",
                                    "Config", "Report"],
                    flash=error)

    # ---------------------------------------------------- environment audits
    @app.get("/environments", response_class=HTMLResponse)
    def environments(request: Request, error: str = ""):
        return page(request, "index.html", active_nav="environment",
                    migrations=_audit_rows("environment"),
                    section="environment", section_title="Environment audits",
                    section_short="Environments",
                    section_sub="Inspect a single live Jira or Confluence "
                                "instance — health checks, configuration "
                                "hygiene, and AI-assisted analysis — without "
                                "needing a target.",
                    new_label="New environment audit",
                    name_placeholder="e.g. Acme Production",
                    create_audit_type="environment", allow_confluence=True,
                    workflow_steps=["Connect", "Gather config", "Health checks",
                                    "AI analysis", "Report"],
                    flash=error)

    @app.post("/migrations")
    def create_migration(name: str = Form(...), product: str = Form("jira"),
                         audit_type: str = Form("migration")):
        # A failed create returns to the list the user started from, so the
        # error is shown in context (environment creates never bounce to the
        # migration dashboard).
        err_target = "/environments" if audit_type == "environment" else "/"
        try:
            # The store validates product against the connector REGISTRY
            # (auditor.connectors.known_products): a row whose product no
            # connector can serve must never be created — it would 500 on
            # every follow-up step instead of failing here with an error.
            mid = store.create_migration(name.strip() or "untitled",
                                         product=product,
                                         audit_type=audit_type)
        except ValueError as exc:
            return RedirectResponse(f"{err_target}?error={exc}", status_code=303)
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ------------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: int = 0):
        oa = load_openai_config(store)
        cli = load_claude_cli_config(store)
        choice = get_provider_choice(store)
        # "configured" reflects whether the SELECTED provider has its config
        # present (an Anthropic key, or a full OpenAI base_url+model+key). This
        # is a config-presence check — it does NOT instantiate a client, so the
        # settings page renders fine even when the optional `openai` package is
        # not installed. The local Claude-CLI provider needs NO api key (it uses
        # local auth), so it is ALWAYS considered configured when selected.
        if choice == "anthropic":
            active_configured = bool(load_anthropic_key(store))
        elif choice == "claude_cli":
            active_configured = True
        else:
            active_configured = oa is not None
        return page(request, "settings.html",
                    client_id=store.settings_get("oauth_client_id") or "",
                    has_secret=bool(store.settings_get("oauth_client_secret_enc")),
                    has_anthropic_key=bool(load_anthropic_key(store)),
                    ai_provider_choice=choice,
                    openai_base_url=(oa or {}).get("base_url") or "",
                    openai_model=(oa or {}).get("model") or "",
                    has_openai_key=bool(oa),
                    claude_cli_model=cli["model"] or "",
                    claude_cli_binary=cli["binary"],
                    claude_cli_timeout=cli["timeout"],
                    active_provider_configured=active_configured,
                    redirect_uri=cfg.oauth_redirect_uri, saved=saved)

    @app.post("/settings")
    def settings_save(oauth_client_id: str = Form(""),
                      oauth_client_secret: str = Form(""),
                      anthropic_api_key: str = Form(""),
                      ai_provider_choice: str = Form(""),
                      openai_base_url: str = Form(""),
                      openai_model: str = Form(""),
                      openai_api_key: str = Form(""),
                      claude_cli_model: str = Form(""),
                      claude_cli_binary: str = Form(""),
                      claude_cli_timeout: str = Form("")):
        # Guard each write: the Anthropic-key save posts to this same endpoint
        # without the OAuth fields, so an unconditional write would wipe a
        # previously-saved client id with an empty string.
        if oauth_client_id.strip():
            store.settings_set("oauth_client_id", oauth_client_id.strip())
        if oauth_client_secret.strip():
            store.settings_set(
                "oauth_client_secret_enc",
                store.encrypt({"secret": oauth_client_secret.strip()}).decode())
        if anthropic_api_key.strip():
            save_anthropic_key(store, anthropic_api_key)
        # OpenAI-compatible endpoint config: base_url + model + write-only key.
        # save_openai_config guards each field (blank key keeps the current one).
        if (openai_base_url.strip() or openai_model.strip()
                or openai_api_key.strip()):
            save_openai_config(store, openai_base_url, openai_model,
                               openai_api_key)
        # Local Claude-CLI config (both non-secret): model override + binary
        # path. save_claude_cli_config guards each field, so a blank one never
        # wipes a previously-saved value.
        if (claude_cli_model.strip() or claude_cli_binary.strip()
                or claude_cli_timeout.strip()):
            save_claude_cli_config(store, claude_cli_model, claude_cli_binary,
                                   timeout=claude_cli_timeout.strip() or None)
        # Provider selector ('anthropic' | 'openai' | 'claude_cli');
        # set_provider_choice ignores any unknown value, so a stray POST can't
        # break the selection.
        if ai_provider_choice.strip():
            set_provider_choice(store, ai_provider_choice.strip())
        return RedirectResponse("/settings?saved=1", status_code=303)

    # ----------------------------------------------------- connection vault
    def _verify_saved(product, deployment, site_url, secret):
        """Live-verify a saved/about-to-be-saved PAT secret against its site.

        Mirrors the manual-PAT path (save_pat_connection): build a pat
        Connection, make_client, connector.verify. Returns the identity dict
        on success; raises (ValueError for an unregistered product,
        ClientError for an auth/HTTP failure) so callers decide policy. The
        secret is never logged here."""
        connector = get_connector(product)
        conn = Connection(auth_type="pat", site_url=site_url,
                          deployment=deployment,
                          email=secret.get("email") or None,
                          api_token=secret.get("token"))
        return connector.verify(connector.make_client(conn, app.state.http))

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request, error: str = "", saved: int = 0):
        return page(request, "connections.html", active_nav="connections",
                    connections=store.list_saved_connections(),
                    flash=error, saved=saved)

    @app.post("/connections")
    def connections_create(name: str = Form(...), product: str = Form("jira"),
                           deployment: str = Form("cloud"),
                           site_url: str = Form(...), email: str = Form(""),
                           api_token: str = Form(...)):
        site = site_url.strip().rstrip("/")
        if site and not site.startswith("http"):
            site = "https://" + site
        email = email.strip()
        try:
            cid = store.create_saved_connection(
                name=name.strip() or "untitled", product=product,
                deployment=deployment, site_url=site,
                email=email or None, token=api_token.strip())
        except ValueError as exc:
            return RedirectResponse(f"/connections?error={exc}", status_code=303)
        # Best-effort live verify: a failure still keeps the saved row, just
        # unverified, so the operator can fix the token via delete+recreate.
        row = store.get_saved_connection(cid)
        secret = store.saved_connection_secret(row)
        try:
            me = _verify_saved(product, deployment, site, secret)
            store.mark_saved_connection_verified(cid, me.get("email"))
            return RedirectResponse("/connections?saved=1", status_code=303)
        except (ClientError, ValueError) as exc:
            detail = (f"HTTP {exc.status}" if isinstance(exc, ClientError)
                      else str(exc))
            return RedirectResponse(
                f"/connections?error=Saved, but could not verify "
                f"{name.strip()}: {detail}", status_code=303)

    @app.post("/connections/{conn_id}/verify")
    def connections_verify(conn_id: int):
        row = store.get_saved_connection(conn_id)
        if row is None:
            return RedirectResponse("/connections", status_code=303)
        secret = store.saved_connection_secret(row)
        try:
            me = _verify_saved(row["product"], row["deployment"],
                               row["site_url"], secret)
        except (ClientError, ValueError) as exc:
            detail = (f"HTTP {exc.status}" if isinstance(exc, ClientError)
                      else str(exc))
            return RedirectResponse(
                f"/connections?error=Could not verify {row['name']}: {detail}",
                status_code=303)
        store.mark_saved_connection_verified(conn_id, me.get("email"))
        return RedirectResponse("/connections?saved=1", status_code=303)

    @app.post("/connections/{conn_id}/delete")
    def connections_delete(conn_id: int):
        store.delete_saved_connection(conn_id)
        return RedirectResponse("/connections", status_code=303)

    # ------------------------------------------------------- migration page
    @app.get("/migrations/{mid}", response_class=HTMLResponse)
    def migration_page(request: Request, mid: int, error: str = ""):
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        conns = {role: store.get_connection(mid, role)
                 for role in ("source", "target")}
        nav = ("environment" if (mig.get("audit_type") == "environment")
               else "migration")
        return page(request, "migration.html", mig=mig, conns=conns,
                    product=mig["product"], active_nav=nav,
                    runs=store.list_runs(mid), error=error,
                    oauth_ready=bool(store.settings_get("oauth_client_id")),
                    saved_connections=store.list_saved_connections(mig["product"]),
                    active=store.active_run(mid))

    # POST-only by design (a GET delete would let a crawl/prefetch destroy an
    # audit). Refuses while a run is active so an in-flight engine thread is
    # never left writing into deleted rows; redirects to the correct list by
    # audit_type so the operator lands back in the section they came from.
    @app.post("/migrations/{mid}/delete")
    def delete_migration(mid: int):
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        if store.active_run(mid) is not None:
            return RedirectResponse(
                f"/migrations/{mid}?error=Cancel the active run before "
                f"deleting this audit", status_code=303)
        store.delete_migration(mid)
        dest = "/environments" if mig.get("audit_type") == "environment" else "/"
        return RedirectResponse(dest, status_code=303)

    @app.post("/migrations/{mid}/connections")
    def save_pat_connection(mid: int, role: str = Form(...),
                            site_url: str = Form(...), email: str = Form(""),
                            api_token: str = Form(...),
                            deployment: str = Form("cloud")):
        mig = store.get_migration(mid)
        if mig is None:
            # mirror the GET counterpart: unknown migration redirects home.
            return RedirectResponse("/", status_code=303)
        if deployment not in ("cloud", "dc"):
            # Validate BEFORE the live verification request: an unknown value
            # would silently behave as cloud here and only explode in
            # store.save_connection after a real HTTP call.
            return RedirectResponse(
                f"/migrations/{mid}?error=unknown deployment "
                f"{deployment!r} (expected cloud or dc)", status_code=303)
        site = site_url.strip().rstrip("/")
        if not site.startswith("http"):
            site = "https://" + site
        email = email.strip()
        if deployment == "cloud" and not email:
            # Cloud PAT is Basic email:token; DC PAT-as-Bearer has no
            # server-side email at all, so the field is only enforced here.
            return RedirectResponse(
                f"/migrations/{mid}?error=email is required for cloud "
                f"connections", status_code=303)
        try:
            connector = get_connector(mig["product"])
        except ValueError as exc:
            # legacy row naming a product no connector serves (creation is
            # registry-gated now, but old DBs may carry such rows): an
            # honest error beats a 500 one step later.
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        conn = Connection(auth_type="pat", site_url=site,
                          deployment=deployment, email=email or None,
                          api_token=api_token.strip())
        try:
            me = connector.verify(connector.make_client(conn, app.state.http))
        except ClientError as exc:
            return RedirectResponse(
                f"/migrations/{mid}?error=Could not authenticate {role}: "
                f"HTTP {exc.status}", status_code=303)
        secret = {"token": api_token.strip()}
        if deployment == "cloud":
            secret["email"] = email
        store.save_connection(mid, role, "pat", site, secret=secret,
                              account_email=me.get("email"),
                              deployment=deployment)
        row = store.get_connection(mid, role)
        store.mark_connection_verified(row["id"], me.get("email") or "")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    @app.post("/migrations/{mid}/connections/from-saved")
    def connection_from_saved(mid: int, role: str = Form(...),
                              saved_id: int = Form(...)):
        """Copy a saved-vault connection into this migration's role connection.

        Copy semantics (spec R5): the saved secret is re-verified LIVE before
        anything is written, so a rotated/expired token fails loudly instead
        of silently copying a dead credential. On success the site/secret/
        deployment/email are written via the SAME store.save_connection +
        mark_connection_verified the manual path uses, so everything
        downstream (scope, run, fix) is unchanged. Deleting the saved row
        later does NOT touch this copy."""
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        saved = store.get_saved_connection(saved_id)
        if saved is None:
            return RedirectResponse(
                f"/migrations/{mid}?error=Saved connection not found",
                status_code=303)
        if saved["product"] != mig["product"]:
            # A confluence credential against a jira audit (or vice versa) would
            # verify against the wrong API — reject before any live request.
            return RedirectResponse(
                f"/migrations/{mid}?error=Saved connection product "
                f"{saved['product']!r} does not match this audit", status_code=303)
        secret = store.saved_connection_secret(saved)
        deployment = saved["deployment"]
        site = saved["site_url"]
        try:
            me = _verify_saved(mig["product"], deployment, site, secret)
        except ClientError as exc:
            return RedirectResponse(
                f"/migrations/{mid}?error=Could not authenticate {role} from "
                f"saved connection: HTTP {exc.status}", status_code=303)
        except ValueError as exc:
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        copied = {"token": secret.get("token")}
        if deployment == "cloud":
            # DC PAT-as-Bearer has no server-side email; only cloud carries it.
            copied["email"] = secret.get("email")
        store.save_connection(mid, role, "pat", site, secret=copied,
                              account_email=me.get("email"),
                              deployment=deployment)
        conn_row = store.get_connection(mid, role)
        store.mark_connection_verified(conn_row["id"], me.get("email") or "")
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    # ----------------------------------------------------------- oauth flow
    @app.get("/oauth/start")
    def oauth_start(migration_id: int, role: str):
        mig = store.get_migration(migration_id)
        if mig is None or mig["product"] != "jira":
            # The 3LO gateway (/ex/jira/{cloud_id}) is Jira-Cloud-only (R2);
            # starting the dance for another product would mint a client
            # against the wrong API with real credentials.
            return RedirectResponse(
                f"/migrations/{migration_id}?error=OAuth is only supported "
                f"for Jira Cloud", status_code=303)
        client_id = store.settings_get("oauth_client_id")
        if not client_id:
            return RedirectResponse(
                f"/migrations/{migration_id}?error=Configure the OAuth client "
                f"in Settings first", status_code=303)
        state = secrets.token_urlsafe(24)
        app.state.oauth_pending[state] = {"migration_id": migration_id,
                                          "role": role}
        return RedirectResponse(build_authorize_url(
            client_id, cfg.oauth_redirect_uri, state), status_code=303)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(request: Request, state: str = "", code: str = "",
                       error: str = ""):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None or error or not code:
            return page(request, "index.html", migrations=store.list_migrations(),
                        flash=f"OAuth failed: {error or 'invalid state'}")
        client_id = store.settings_get("oauth_client_id")
        enc = store.settings_get("oauth_client_secret_enc")
        secret = store.decrypt(enc.encode())["secret"] if enc else ""
        # Atlassian can 400/500 the token exchange or accessible-resources call
        # (revoked client, expired code, outage). The raised RuntimeError carries
        # the Atlassian response body (which does NOT contain the client_secret);
        # degrade to the same OAuth-failed flash instead of a raw 500.
        try:
            tokens = exchange_code(client_id, secret, code,
                                   cfg.oauth_redirect_uri, http=app.state.http)
            sites = accessible_resources(tokens["access_token"],
                                         http=app.state.http)
        except (RuntimeError, httpx.HTTPError, KeyError) as exc:
            return page(request, "index.html",
                        migrations=store.list_migrations(),
                        flash=f"OAuth failed: {exc}")
        pend["tokens"] = tokens
        new_state = secrets.token_urlsafe(24)
        app.state.oauth_pending[new_state] = pend
        return page(request, "migration.html",
                    mig=store.get_migration(pend["migration_id"]),
                    conns={r: store.get_connection(pend["migration_id"], r)
                           for r in ("source", "target")},
                    runs=store.list_runs(pend["migration_id"]),
                    error="", oauth_ready=True,
                    active=store.active_run(pend["migration_id"]),
                    site_pick={"state": new_state, "sites": sites,
                               "role": pend["role"]})

    @app.post("/oauth/select")
    def oauth_select(state: str = Form(...), cloud_id: str = Form(...),
                     site_url: str = Form(...)):
        pend = app.state.oauth_pending.pop(state, None)
        if pend is None:
            return RedirectResponse("/", status_code=303)
        t = pend["tokens"]
        store.save_connection(
            pend["migration_id"], pend["role"], "oauth", site_url,
            cloud_id=cloud_id,
            secret={"access_token": t["access_token"],
                    "refresh_token": t.get("refresh_token"),
                    "expires_at": time.time() + float(t.get("expires_in", 3600))})
        return RedirectResponse(f"/migrations/{pend['migration_id']}",
                                status_code=303)

    # ----------------------------------------------------------------- runs
    @app.post("/migrations/{mid}/runs")
    def start_run(mid: int, projects: str = Form(""),
                  reuse_extracts_from: str = Form("")):
        if not (store.get_connection(mid, "source")
                and store.get_connection(mid, "target")):
            return RedirectResponse(
                f"/migrations/{mid}?error=Configure both connections first",
                status_code=303)
        params = {}
        keys = [k.strip() for k in projects.split(",") if k.strip()]
        if keys:
            params["projects"] = keys
        if reuse_extracts_from.strip().isdigit():
            params["reuse_extracts_from"] = int(reuse_extracts_from)
        try:
            rid = engine.start(mid, params)
        except RuntimeError as exc:
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/runs/{rid}", status_code=303)

    @app.post("/migrations/{mid}/env-runs")
    def start_env_run(mid: int):
        # Environment audit: a single read-only pass over one configured side.
        # Distinct from start_run (the two-sided migration audit) because it
        # needs only a source connection and launches with kind='env_audit',
        # which routes the run through build_env_stages() / _finalize_env.
        mig = store.get_migration(mid)
        if mig is None:
            return RedirectResponse("/", status_code=303)
        if mig["audit_type"] != "environment":
            return RedirectResponse(
                f"/migrations/{mid}?error=This migration is not an environment "
                f"audit", status_code=303)
        if not store.get_connection(mid, "source"):
            return RedirectResponse(
                f"/migrations/{mid}?error=Configure the source connection first",
                status_code=303)
        try:
            rid = engine.start(mid, {}, kind="env_audit")
        except RuntimeError as exc:
            return RedirectResponse(f"/migrations/{mid}?error={exc}",
                                    status_code=303)
        return RedirectResponse(f"/runs/{rid}", status_code=303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        mig = store.get_migration(run["migration_id"])
        nav = ("environment" if (mig or {}).get("audit_type") == "environment"
               else "migration")
        return page(request, "run.html", run=run, mig=mig, active_nav=nav)

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: int):
        engine.cancel(run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    # POST-only by design: a GET delete would let a crawl/prefetch destroy a
    # run. Refuses to delete a run that is the migration's live active run —
    # cancel it first so the engine thread isn't pulled out from under itself.
    @app.post("/runs/{run_id}/delete")
    def delete_run(run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        mid = run["migration_id"]
        active = store.active_run(mid)
        if run["status"] == "running" and active and active["id"] == run_id:
            return RedirectResponse(
                f"/runs/{run_id}?error=Cancel the run before deleting it",
                status_code=303)
        store.delete_run(run_id)
        return RedirectResponse(f"/migrations/{mid}", status_code=303)

    @app.get("/runs/{run_id}/stream")
    async def run_stream(run_id: int, request: Request):
        async def gen():
            last = 0
            while True:
                if await request.is_disconnected():
                    return
                for e in store.get_events(run_id, after_id=last):
                    last = e["id"]
                    yield f"data: {json.dumps(e)}\n\n"
                run = store.get_run(run_id)
                if run is None or run["status"] != "running":
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(1.0)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------- analysis
    @app.get("/runs/{run_id}/analysis", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/{view}", response_class=HTMLResponse)
    @app.get("/runs/{run_id}/analysis/projects/{project}",
             response_class=HTMLResponse)
    def analysis_page(request: Request, run_id: int, view: str = "overview",
                      project: str = ""):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if project:
            view = "project"
        src = store.get_connection(run["migration_id"], "source") or {}
        tgt = store.get_connection(run["migration_id"], "target") or {}
        mig = store.get_migration(run["migration_id"])
        _atype = mig.get("audit_type") or "migration"
        return page(request, "analysis.html", run=run, view=view,
                    project=project, mig=mig, product=mig["product"],
                    audit_type=_atype,
                    active_nav=("environment" if _atype == "environment"
                                else "migration"),
                    src_base=(src.get("site_url") or "").rstrip("/"),
                    tgt_base=(tgt.get("site_url") or "").rstrip("/"),
                    src_deployment=src.get("deployment") or "cloud",
                    tgt_deployment=tgt.get("deployment") or "cloud")

    # ------------------------------------------------- executive-summary report
    # Dispatches by audit type: env_audit -> the AI executive summary, a
    # migration audit -> the source->target fidelity report.
    @app.get("/runs/{run_id}/report.pdf")
    def report_pdf(run_id: int):
        res = _report.report_for_run(store, run_id)
        if res is None:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        template, ctx = res
        try:
            pdf = _report.render_report_pdf(templates, ctx, template)
        except _report.ReportUnavailable as exc:
            return PlainTextResponse(str(exc), status_code=503)
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{_report.report_filename(ctx)}"'})

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def report_html(request: Request, run_id: int):
        res = _report.report_for_run(store, run_id)
        if res is None:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        template, ctx = res
        return templates.TemplateResponse(request, template, ctx)

    # ------------------------------------------------- findings export (CSV/JSON)
    @app.get("/runs/{run_id}/findings.csv")
    def findings_csv(run_id: int):
        res = _export.export_findings(store, run_id)
        if res is None:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        fields, rows = res
        return Response(
            content=_export.rows_to_csv(fields, rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="run-{run_id}-findings.csv"'})

    @app.get("/runs/{run_id}/findings.json")
    def findings_json(run_id: int):
        res = _export.export_findings(store, run_id)
        if res is None:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        fields, rows = res
        return JSONResponse({"run_id": run_id, "fields": fields,
                             "count": len(rows), "findings": rows})

    # ----------------------------------------------- run-over-run diff
    @app.get("/runs/{run_id}/diff", response_class=HTMLResponse)
    def run_diff(request: Request, run_id: int, base: int = 0):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        candidates = _compare.candidate_base_runs(store, run)
        diff = _compare.compare_runs(store, base, run_id) if base else None
        return page(request, "diff.html", run=run,
                    mig=store.get_migration(run["migration_id"]),
                    candidates=candidates, base=base, diff=diff)

    # ------------------------------------------------------------ solutions
    @app.post("/runs/{run_id}/solutions")
    def run_solutions(run_id: int, kind: str = Form(...), area: str = Form(""),
                      name: str = Form(""), project: str = Form(""),
                      src_key: str = Form(""), tgt_key: str = Form(""),
                      field: str = Form(""), refresh: str = Form("")):
        run = store.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        # R7 privacy: reconstruct the query metadata from the STORED finding row
        # by safe identifiers — NEVER trust client-sent content. A client could
        # otherwise put an issue title in `name`; here issue findings are matched
        # by key/field and their summary is never read, and config findings carry
        # only the audited object name. product/deployment come from the
        # migration + source connection, not the client.
        finding = _reconstruct_solution_finding(
            store, run_id, kind=kind, area=area, name=name, project=project,
            src_key=src_key, tgt_key=tgt_key, field=field)
        if finding is None:
            return JSONResponse(
                {"error": "finding not found in this run"}, status_code=404)
        mig = store.get_migration(run["migration_id"]) or {}
        src_conn = store.get_connection(run["migration_id"], "source") or {}
        finding["product"] = mig.get("product")
        finding["deployment_from"] = src_conn.get("deployment") or "cloud"
        sig = finding_signature(finding)
        if not refresh:
            cached = store.get_solutions(run_id, sig)
            if cached:
                p = cached["payload"]
                p["cached"] = True
                p["searched_at"] = cached["created_at"]
                return JSONResponse(p)
        client = anthropic_client(store)
        if client is None:
            return JSONResponse(
                {"error": "Add an Anthropic API key in Settings to search for "
                          "solutions."}, status_code=400)
        result = find_solutions(finding, client)
        result["searched_at"] = time.time()
        result["cached"] = False
        # Only persist successful searches. Caching an error (rate limit,
        # connection drop, refusal) would make a transient failure sticky:
        # every later request with the same signature and no refresh=1 would
        # serve the stale error forever. Leaving errors uncached lets the
        # next request retry naturally.
        if not result.get("error"):
            store.save_solutions(run_id, sig, result)
        return JSONResponse(result)

    # ------------------------------------------------------- scope preview
    @app.get("/migrations/{mid}/scope.json")
    def scope_preview(mid: int):
        if store.get_migration(mid) is None:
            return JSONResponse({"error": "migration not found"}, status_code=404)
        if not (store.get_connection(mid, "source")
                and store.get_connection(mid, "target")):
            return JSONResponse(
                {"error": "configure both connections first"}, status_code=400)
        try:
            src, tgt, connector = build_clients(store, mid,
                                                http=app.state.http)
        except ValueError as exc:
            # legacy migration row naming an unregistered product: the
            # connector lookup inside build_clients raises ValueError —
            # answer 400 with the reason, never a 500.
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ClientError as exc:
            return JSONResponse(
                {"error": f"could not connect: {exc}"}, status_code=502)
        try:
            sp, serr = connector.list_containers(src)
            tp, terr = connector.list_containers(tgt)
        except ClientError as exc:
            return JSONResponse(
                {"error": f"could not read {connector.container_label}s: "
                          f"{exc}"}, status_code=502)
        if serr or terr:
            return JSONResponse(
                {"error": f"could not read {connector.container_label}s: "
                          f"src={serr} tgt={terr}"}, status_code=502)
        m = match_projects(sp, tp)
        for proj in m["matched"]:
            key = proj["key"]
            sc = connector.count_items(src, key)
            tc = connector.count_items(tgt, key)
            proj["src_count"] = sc if isinstance(sc, int) else None
            proj["tgt_count"] = tc if isinstance(tc, int) else None
            # remove internal ids not needed by the picker
            proj.pop("src_id", None)
            proj.pop("tgt_id", None)
        return JSONResponse({
            "matched": m["matched"],
            "source_only": m["source_only"],
            "target_only": m["target_only"],
            "product": connector.product,
            "container_label": connector.container_label,
            "item_label": connector.item_label,
        })

    # ------------------------------------------------------------ elevation
    # NOTE: state-changing POSTs are protected by CSRFOriginMiddleware (a
    # cookieless same-origin check), not a per-form token. The app binds to
    # 127.0.0.1 by default; hosting it still needs the (unimplemented) auth
    # layer at the MA_AUTH_MODE seam.
    def _elevation_blocked(run, side=None):
        """R9 guard: elevation mutates project roles via Cloud-only admin
        APIs. The CONNECTOR owns the product capability (supports_elevation)
        so the registry is the single source of truth; each involved side
        must additionally be cloud. An unregistered legacy product blocks
        (redirect) instead of raising. side=None checks both sides — the
        confirm page is all or nothing; per-side POSTs check only the side
        they would mutate."""
        mig = store.get_migration(run["migration_id"])
        try:
            connector = get_connector(mig["product"])
        except ValueError:
            return True
        if not connector.supports_elevation:
            return True
        for role in ((side,) if side else ("source", "target")):
            row = store.get_connection(run["migration_id"], role)
            if row and (row["deployment"] or "cloud") != "cloud":
                return True
        return False

    def _elevation_redirect(run_id):
        store.add_event(run_id, "permissions", "error",
                        "elevation is only supported for Jira Cloud")
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id}/elevate", response_class=HTMLResponse)
    def elevate_confirm(request: Request, run_id: int):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if _elevation_blocked(run):
            return _elevation_redirect(run_id)
        rows = [r for r in store.get_run_projects(run_id) if r["blind_spot"]]
        return page(request, "elevate.html", run=run, rows=rows,
                    undo_src=store.settings_get(f"elevation:{run_id}:source"),
                    undo_tgt=store.settings_get(f"elevation:{run_id}:target"))

    def _side_client(run, side):
        src, tgt, _connector = build_clients(store, run["migration_id"],
                                             http=app.state.http)
        return src if side == "source" else tgt

    @app.post("/runs/{run_id}/elevate")
    def elevate_apply(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if _elevation_blocked(run, side):
            return _elevation_redirect(run_id)
        cl = _side_client(run, side)
        me = cl.myself()
        role_id = find_admin_role_id(cl)
        if role_id is None:
            store.add_event(run_id, "permissions", "warn",
                            f"elevation aborted on {side}: no Administrators "
                            f"role found")
            return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)
        blind = {r["key"] for r in store.get_run_projects(run_id)
                 if r["blind_spot"]}
        projects, _ = cl.all_projects()
        ids = [p["id"] for p in projects if p.get("key") in blind]
        grants = apply_elevation(cl, ids, role_id, me["accountId"])
        store.settings_set(f"elevation:{run_id}:{side}", json.dumps(
            {"role_id": role_id, "account_id": me["accountId"],
             "grants": grants}))
        store.add_event(run_id, "permissions", "warn",
                        f"elevation applied on {side}: "
                        f"{sum(1 for g in grants if g['ok'])}/{len(grants)} "
                        f"projects (undo available)")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    @app.post("/runs/{run_id}/elevate/undo")
    def elevate_undo(run_id: int, side: str = Form(...)):
        run = store.get_run(run_id)
        if run is None:
            return RedirectResponse("/", status_code=303)
        if _elevation_blocked(run, side):
            return _elevation_redirect(run_id)
        raw = store.settings_get(f"elevation:{run_id}:{side}")
        if raw:
            data = json.loads(raw)
            cl = _side_client(run, side)
            undo_elevation(cl, data["grants"], data["role_id"],
                           data["account_id"])
            store.settings_delete(f"elevation:{run_id}:{side}")
            store.add_event(run_id, "permissions", "info",
                            f"elevation undone on {side}")
        return RedirectResponse(f"/runs/{run_id}/elevate", status_code=303)

    return app


def cli():
    import argparse
    import datetime
    ap = argparse.ArgumentParser(prog="migration-auditor")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web app")
    bp = sub.add_parser("backup", help="write a consistent DB snapshot")
    bp.add_argument("dest", nargs="?", help="destination .db path "
                    "(default: <data_dir>/backups/auditor-<UTC timestamp>.db)")
    aup = sub.add_parser("audit", help="run a headless environment audit "
                         "(CI/cron). The PAT comes from MA_AUDIT_TOKEN.")
    aup.add_argument("--site", required=True, help="instance URL")
    aup.add_argument("--product", default="jira", choices=["jira", "confluence"])
    aup.add_argument("--deployment", default="cloud", choices=["cloud", "dc"])
    aup.add_argument("--email", default=None,
                     help="account email (Cloud PAT basic auth; omit for DC)")
    aup.add_argument("--json", nargs="?", const="-", default=None,
                     metavar="PATH", help="write full result JSON "
                     "(no PATH or '-' = stdout)")
    aup.add_argument("--fail-on", dest="fail_on", default="CRITICAL",
                     choices=["CRITICAL", "NEEDS_ATTENTION",
                              "HEALTHY_WITH_NOTES", "HEALTHY"],
                     help="exit 2 when the verdict is at/worse than this")
    cp = sub.add_parser("clone-access",
                        help="additively clone a user's groups & project roles "
                             "onto another account (single pair or --csv)")
    cp.add_argument("--conn", required=True,
                    help="saved jira connection name or id (the instance)")
    cp.add_argument("--main", help="source account (accountId or email)")
    cp.add_argument("--clone", help="target account (accountId or email)")
    cp.add_argument("--csv", help="CSV with 'main,clone' columns (bulk)")
    cp.add_argument("--apply", action="store_true",
                    help="perform writes (default is a groups-only preview)")
    cp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="full plan incl. role scan, write nothing")
    cp.add_argument("--json", nargs="?", const="-", default=None, metavar="PATH",
                    help="write the full JSON report (no PATH or '-' = stdout)")
    args = ap.parse_args()
    setup_logging()
    if args.command == "audit":
        import sys
        sys.exit(run_audit_cli(args))
    if args.command == "clone-access":
        import sys
        sys.exit(run_clone_cli(args))
    cfg = load_config()
    if args.command == "backup":
        store = Store(db_path=cfg.db_path, key_path=cfg.key_path,
                      secret_key=cfg.secret_key)
        dest = args.dest
        if not dest:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ")
            bdir = os.path.join(cfg.data_dir, "backups")
            os.makedirs(bdir, exist_ok=True)
            # token suffix so two backups in the same second don't collide.
            dest = os.path.join(bdir, f"auditor-{ts}-{secrets.token_hex(3)}.db")
        out = store.backup(dest)
        logging.getLogger("migration_auditor").info("backup written: %s", out)
        print(out)
        return
    # default (no subcommand) and "serve" both serve. The bind guard runs HERE
    # (not in load_config), so it gates serving without blocking `backup`.
    assert_safe_bind(cfg.bind_host)
    uvicorn.run(create_app(cfg), host=cfg.bind_host, port=cfg.bind_port)
