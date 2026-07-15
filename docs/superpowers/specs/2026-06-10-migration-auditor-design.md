# Migration Auditor — Design

**Date:** 2026-06-10 · **Status:** Approved pending user spec review · **Home:** `D:\Atlassian-Products\Migration-auditor`

## 1. Purpose

A local-first web application that audits Jira Cloud → Jira Cloud migrations. An operator supplies a **source** and a **target** site, picks the projects in scope, and the app runs a deterministic audit: issue-data fidelity (every issue, content fingerprints), instance-config parity, and permission blind-spot detection. The result is **not a document** — it is a full in-app, multi-page interactive analysis with dashboards, drill-downs, filterable tables, and clickable links into both Jira instances.

Born from a real Data Center→Cloud UAT migration audit (the reference pipeline), generalized so any migration can be audited ad hoc by configuring origin and destination.

## 2. Decisions already made (with the user)

| Decision | Choice |
|---|---|
| Jira auth | **Atlassian OAuth 2.0 (3LO)** to connect source/target (users sign in with Google/Microsoft on Atlassian's consent screen) **+ manual PAT** (site URL + email + API token) as the fallback. No app-level user accounts in v1. |
| Form factor | **Local web app** (`uvicorn` on localhost), architected to be hostable later without rewrite. |
| Output | **In-app analysis only.** No PDF, no static report file. Findings persist as queryable data; the app renders them as multi-page dashboards. |
| Home | Local git repo at `D:\Atlassian-Products\Migration-auditor`; GitHub home decided later. |
| Approach | B — product core + local web app (thin-wrapper A and SaaS-grade C rejected). |

## 3. Constraints and facts that shaped the design

- **Jira Cloud's REST API does not accept Google/Microsoft OAuth tokens.** Only Basic (email + API token) or Atlassian OAuth 2.0 (3LO) Bearer tokens work. Google/MS identity enters via Atlassian's own consent screen.
- OAuth calls go through the gateway: `https://api.atlassian.com/ex/jira/{cloudId}/rest/...`; PAT calls go direct: `https://{site}.atlassian.net/rest/...`. The client must abstract both.
- Atlassian uses **rotating refresh tokens** — the new refresh token must be persisted on every refresh or the connection dies.
- A distributable product cannot ship a shared OAuth client secret → each install registers its own (free) Atlassian OAuth app; client ID/secret entered in Settings. PAT path needs zero setup.
- Scopes for OAuth connections: `read:jira-work read:jira-user offline_access`. The optional permission-elevation step needs project-role writes; if the granted scopes don't allow it, the app falls back to instructing the operator (or use a PAT connection for elevation).
- Proven pipeline logic to port from the reference migration-audit pipeline: paginated/429-aware client (`lib.py`), content-fingerprint extraction (`extract_core.py`), fidelity compare + collision metadata check (`compare.py`), config audit incl. the servicedeskapi pagination fix (`config_audit.py` + `config_fix.py`), permission blind-spot lesson (the "MS 0/16,016" browse-permission artifact and the grant/undo admin flow), report semantics (`build_report.py` — its *analysis content* survives as the in-app UI; its file-rendering does not).
- Known hardcodings to remove: absolute reference-pipeline paths, instance-specific custom fields (`customfield_10002/10047`), `creds.sh` env coupling.

## 4. Architecture

**Stack:** Python 3.11+ · FastAPI + uvicorn · Jinja2 templates + vanilla JS (no build step) · SQLite (stdlib `sqlite3`) · `httpx` for Jira calls · `cryptography` (Fernet) for token encryption. Optional dev deps: pytest, Playwright (UI smoke only).

```
Migration-auditor/
├── auditor/                    # CORE LIBRARY — pure logic, no web imports
│   ├── client.py               # Connection abstraction: PAT (Basic, direct base) and
│   │                           # OAuth (Bearer, api.atlassian.com/ex/jira/{cloudId},
│   │                           # auto-refresh w/ rotating refresh-token persistence hook).
│   │                           # Retry/429/5xx + cursor & startAt pagination (from lib.py).
│   ├── scope.py                # Project enumeration both sides; match by key, detect renames;
│   │                           # per-project approximate counts.
│   ├── permissions.py          # Blind-spot detector: approx_count vs browse visibility per
│   │                           # project; optional role-grant elevation + recorded auto-undo.
│   ├── extract.py              # Full per-project extraction → gz JSONL in the run workspace;
│   │                           # ADF→text content fingerprints (sha) for description/comments;
│   │                           # count verification (extracted == approx_count).
│   ├── compare.py              # Per-issue: presence, field fidelity, content shas, comments,
│   │                           # attachments, links; key-collision metadata verification;
│   │                           # post-cutover tail classification (created after snapshot).
│   ├── config_audit.py         # Instance parity: statuses, issue types, priorities, resolutions,
│   │                           # link types, custom fields (type + select options), screens (+deep
│   │                           # field check), screen/workflow/issuetype/field-config/permission/
│   │                           # notification schemes, roles, workflows (structure), JSM request
│   │                           # types + queues (paginated servicedeskapi — fix folded in).
│   └── findings.py             # PURE normalizer: turns stage outputs into plain finding-row
│   │                           # dicts + run-level stats and the verdict. No I/O — the web
│   │                           # layer (store.py) persists what this returns.
├── webapp/
│   ├── main.py                 # App factory; routes; binds MA_BIND (default 127.0.0.1:8484).
│   ├── config.py               # Env config: MA_DATA_DIR, MA_PUBLIC_BASE_URL, MA_BIND,
│   │                           # MA_SECRET_KEY (else auto-keyfile data/.key).
│   ├── store.py                # SQLite schema + DAO; Fernet encrypt/decrypt for secrets.
│   ├── oauth.py                # 3LO: authorize URL w/ state, token exchange, accessible-resources
│   │                           # site picker, refresh-and-persist callback for client.py.
│   ├── runs.py                 # Background run engine: one thread per run, phase state machine,
│   │                           # progress events (SSE), cancel, per-phase re-run.
│   ├── analysis.py             # JSON endpoints powering the analysis UI: paginated/filtered
│   │                           # queries over findings tables.
│   ├── templates/              # Jinja pages (broadsheet design system)
│   └── static/                 # app.css (ported broadsheet system), app.js (vanilla)
├── data/                       # MA_DATA_DIR default — gitignored. SQLite db + workspaces:
│   └── migrations/<id>/runs/<run_id>/{src,tgt}/<KEY>.core.jsonl.gz
├── tests/                      # pytest; synthetic fixtures shaped from real payload structures
├── docs/superpowers/specs/     # this document
├── Dockerfile                  # hosting-ready container (uvicorn, MA_* env-driven)
├── pyproject.toml              # package + `migration-auditor` console entry (serve command)
└── README.md                   # quickstart; "register your own Atlassian OAuth app" walkthrough
```

**Core/web boundary:** `auditor/` never imports from `webapp/`. The core takes injected `Connection` objects, a workspace path, and a progress callback; it returns/streams plain data. The web layer owns persistence (via `store.py`), HTTP, and rendering. This is the seam that makes the engine reusable (CLI later, hosted worker later) and testable without a server.

## 5. Data model (SQLite)

```
settings        (key TEXT PK, value TEXT)            -- oauth client id; secret encrypted
migrations      (id, name, created_at)
connections     (id, migration_id, role 'source'|'target', auth_type 'oauth'|'pat',
                 site_url, cloud_id, account_email, secret_enc BLOB,  -- PAT or token bundle
                 status, verified_at)
runs            (id, migration_id, status 'running'|'done'|'failed'|'cancelled',
                 phase, started_at, finished_at, params_json, stats_json, verdict)
run_projects    (run_id, key, name, src_count, tgt_count, missing, tail_count,
                 fidelity_pct, blind_spot INT, status)
findings_issue  (id, run_id, project, kind, src_key, tgt_key, field, summary, detail_json)
                 -- kind: missing_in_tgt | missing_in_src | tail_post_cutover |
                 --       field_mismatch | content_mismatch | comment_mismatch |
                 --       attachment_mismatch | link_mismatch | key_collision
                 -- tails carry direction + created-vs-snapshot evidence in detail_json
                 -- INDEX (run_id, project, kind)
findings_config (id, run_id, area, name, kind, detail_json)
                 -- area: statuses | issue_types | priorities | resolutions | link_types |
                 --       custom_fields | screens | screen_schemes | workflow_schemes |
                 --       issuetype_schemes | field_configs | permission_schemes |
                 --       notification_schemes | roles | workflows | jsm
events          (run_id, ts, phase, level, message)   -- progress log, feeds SSE + run page
```

Raw extracts (gz JSONL) stay on disk in the run workspace — they are the cache that makes per-phase re-runs cheap, and they never go into SQLite.

**Secret handling:** `secret_enc` holds Fernet-encrypted JSON — for PAT: `{email, token}`; for OAuth: `{access_token, refresh_token, expires_at}`. Key from `MA_SECRET_KEY` env or auto-generated `data/.key` (chmod 600). Secrets never logged; PAT fields render masked in the UI.

## 6. Run lifecycle

Phases, each resumable and individually re-runnable against the cached extracts:

1. **verify** — `/myself` + serverInfo on both connections; fail fast with a clear error.
2. **scope** — enumerate projects both sides, match keys, per-project approximate counts; persists the scope table. The UI shows matched / source-only / target-only and the operator selects (default: all matched).
3. **permissions** — for each selected project on each side: `approx_count(project)` vs a browse probe. Discrepancy ⇒ **blind-spot warning** surfaced in the UI (this is the "MS looked empty" lesson). The operator may then trigger **elevation**: grant the connection's account the project Administrators role on affected projects (recorded in `events` + an undo list), with automatic de-grant at run end and a manual "undo now" button. Elevation requires explicit confirmation each time; it is never automatic.
4. **extract** — full extraction per selected project, both sides, with live per-project progress (`AC 12,400/40,092…`); count-verified.
5. **compare** — per-issue fidelity diff; collision metadata check; tail classification: an issue absent on the other side whose `created` is after the migration snapshot is a `tail_post_cutover` (expected drift, not data loss), direction-aware for both sides; genuinely absent pre-snapshot issues are `missing_in_tgt` / `missing_in_src`.
6. **config** — instance config parity (independent of project selection except JSM checks, which run per selected JSM project).
7. **finalize** — `findings.py` computes run stats + verdict (`CLEAN` / `CLEAN WITH TAILS` / `GAPS FOUND` / `CRITICAL`), un-does any elevation still active, marks the run done.

One run at a time per migration (enforced); concurrent runs across different migrations allowed. Runs survive page reloads (state in SQLite; the engine thread is owned by the process — a server restart marks in-flight runs `failed` with a resume-from-phase offer).

## 7. The Analysis UI (the product's centerpiece)

Multi-page, in-app, rendered from the findings store — Jinja shell + vanilla JS fetching paginated JSON from `analysis.py`. Visual language: the financial-broadsheet design system already proven in an earlier internal report (warm paper, petrol/claret accents, serif display + mono data, CSS conic-gradient donuts, bar charts, heatmaps — **no chart library**).

Pages per run:

1. **Overview** — verdict banner; KPI cards (projects matched, issues migrated %, content fidelity %, config gaps, blind spots, tails); **data-driven headline findings** in prose (e.g. "MS migrated 15,105 of 16,022; the 917 gap is a post-cutover tail, not loss"); phase timeline.
2. **Projects** — dashboard table + project×category heatmap (missing / tails / mismatches / fidelity %); each row links to the drill-down.
3. **Project drill-down** — tabs: Missing issues (paginated table, every key links to source and target browse URLs), Field mismatches (grouped by field), Content (description/comment sha mismatches with length deltas), Attachments/Links, Collisions. All tables server-side paginated and filterable.
4. **Config parity** — one section per area with src/tgt counts and the source-only diff list; custom fields include type and option mismatches; screens include the deep field-level check; JSM shows request types + queues per project.
5. **Discrepancy master table** — every `findings_issue` row across projects: full-text search, filters by project/kind/field, sortable, paginated.
6. **Run log** — the `events` stream (also live during the run via SSE).

Cross-cutting: a migration dashboard lists its runs with verdict chips (history); deep-linkable URLs for every page/filter state so findings can be shared as links among the team later when hosted.

## 8. Web flows

- **Settings:** Atlassian OAuth client ID/secret (optional — only needed for the OAuth path), data dir display, key status.
- **New migration wizard:** name → source connection (Connect with Atlassian → consent → pick site from accessible-resources; or PAT form) → verify → target connection (same) → verify → scope screen → confirm → run starts; redirects to the live run page (SSE progress).
- **OAuth callback:** `{MA_PUBLIC_BASE_URL}/oauth/callback` — defaults to `http://localhost:8484/oauth/callback`; hosted mode just changes the env var and the registered callback.

## 9. Hosting-ready seams (built now, used later)

- All config via `MA_*` env vars; zero absolute paths (everything under `MA_DATA_DIR`).
- Web layer stateless beyond SQLite + workspace files → containerizable as one volume.
- `Dockerfile` shipped and CI-smoke-tested locally.
- Auth hook point: a no-op `MA_AUTH_MODE=none` middleware seam where app-level login (e.g. OIDC) lands when hosted. Out of scope to implement in v1.
- Run engine behind an interface in `runs.py` so a queue/worker can replace the in-process thread without touching `auditor/`.

## 10. Error handling posture

Ported from the audit's hard-won lessons: 429 honored with `Retry-After`; 5xx retried with backoff; **an unreachable/unauthorized side fails the phase loudly** — never rendered as "0 issues" (the fail-open class from sup-triage applies here too: an outage must be distinguishable from an empty result). Count verification (`extracted == approx_count`) gates the compare phase. Permission blind-spots warn before extraction so the operator never trusts a permission-shaped zero.

## 11. Testing

- **Core:** pytest with synthetic fixtures mirroring real payload shapes (ADF bodies, paginated project search, servicedeskapi pages, collision metadata) — fidelity math, tail classification, collision logic, config diff areas, blind-spot detection, scope matching.
- **Client:** httpx mock transport for pagination, 429/5xx retries, OAuth refresh incl. rotating-refresh persistence, PAT vs gateway base construction.
- **Web:** FastAPI TestClient for the wizard endpoints, run state machine (with a stubbed core), analysis pagination/filters; Fernet round-trip; SSE event framing.
- **Smoke:** one Playwright run against a seeded fake dataset — wizard → run page → analysis pages render.
- All synthetic data uses placeholder companies per the synthetic-data rule.

## 12. Out of scope for v1 (explicit)

Multi-user/app login, hosted deployment itself, Postgres/queue, scheduled re-audits, run-vs-run comparison view, Confluence migration audits, standalone HTML/PDF export (deliberately removed per the product decision), Jira Server/DC as a source (Cloud→Cloud only; DC source is a natural v2 since the client layer is the only change).

## 13. Open items

- Product display name: working title **Migration Auditor** (rename is cosmetic).
- GitHub home: deferred by decision; local git only.
