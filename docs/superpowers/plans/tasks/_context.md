# Migration Auditor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local-first FastAPI web app that audits Jira Cloud→Cloud migrations end-to-end (connect via Atlassian OAuth or PAT → scope → permission blind-spots → extract → compare → config parity) and serves the results as an in-app, multi-page interactive analysis. No PDF/static report.

**Architecture:** Pure core library `auditor/` (Jira client, scope matching, blind-spot detection, content-fingerprint extraction, fidelity compare, config audit, findings normalization — no web imports, all I/O via injected client/paths/callbacks) wrapped by `webapp/` (FastAPI, SQLite store with Fernet-encrypted secrets, threaded run engine with SSE progress, JSON analysis API, Jinja + vanilla-JS broadsheet UI). Spec: `docs/superpowers/specs/2026-06-10-migration-auditor-design.md`. Reference implementation being ported: the prior migration-audit pipeline (lib.py, extract_core.py, compare.py, config_audit.py, config_fix.py, grant_admin.py).

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, httpx, Jinja2, sqlite3 (stdlib), cryptography (Fernet), pytest. No JS build step, no chart library (CSS donuts/bars).

**Working directory for ALL tasks:** `/mnt/d/Atlassian-Products/Migration-auditor`

---

## Shared interfaces (single source of truth — later tasks MUST match these exactly)

```python
# auditor/client.py
@dataclass
class Connection:
    auth_type: str                    # "pat" | "oauth"
    site_url: str                     # https://acme.atlassian.net (always set; display + browse links)
    email: str | None = None          # pat only
    api_token: str | None = None      # pat only
    cloud_id: str | None = None       # oauth only
    access_token: str | None = None   # oauth only
    refresh_token: str | None = None  # oauth only
    expires_at: float = 0.0           # oauth only (unix ts)
    on_tokens_refreshed: "Callable[[Connection], None] | None" = None
    refresh_fn: "Callable[[str], dict] | None" = None  # refresh_token -> token dict

class JiraClient:
    def __init__(self, conn: Connection, http: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep): ...
    def req(self, path, method="GET", body=None, params=None, tries=6) -> tuple[int, dict|list]
    def paginate_start_at(self, path, params=None, key=None, cap=20000) -> tuple[list, str|None]
    #   Returns a NON-None error string on ANY page failure (first-page OR mid-loop truncation):
    #   a non-200 on page 2+ yields (rows_so_far, "ERR<status>:truncated") — never silent truncation.
    def search_jql(self, jql, fields, expand=None, page=100) -> Iterator[dict]
    def approx_count(self, jql) -> int | str        # "ERR<status>" on failure
    def all_projects(self) -> tuple[list, str|None]
    def sd_list(self, path) -> list                  # servicedeskapi pagination; raises ClientError on failure
    def myself(self) -> dict                         # raises ClientError on failure

def adf_text(node) -> str
def h16(s: str | None) -> str
class ClientError(RuntimeError): ...                 # carries .status

# auditor/scope.py
def match_projects(src_projects: list, tgt_projects: list) -> dict
# -> {"matched":[{"key","name","src_id","tgt_id","src_count","tgt_count"}],
#     "source_only":[{"key","name"}], "target_only":[{"key","name"}]}
# (counts filled by caller via approx_count; match_projects sets them to None)

# auditor/permissions.py
def detect_blind_spots(client: JiraClient, project_keys: list[str]) -> list[dict]
# -> [{"key","search_count","insight_count","blind_spot": bool,"indeterminate": bool}]
def find_admin_role_id(client) -> int | None
def apply_elevation(client, project_ids: list[str], role_id: int, account_id: str) -> list[dict]
# -> grant log rows: {"project_id","status","ok","added"[,"error"]}; undo only removes added=True
def undo_elevation(client, grants: list[dict], role_id: int, account_id: str) -> list[dict]

# auditor/extract.py
CORE_FIELDS: list[str]      # base fields, NO instance-specific customfields
def slim(issue: dict) -> dict
def extract_project(client, project_key, out_path, extra_fields=(), progress=None) -> dict
# -> {"extracted": n, "approx": ac, "verified": bool}

# auditor/compare.py
def compare_project(project: str, src_path: str, tgt_path: str) -> dict
# -> {"stats": {...}, "findings": [finding, ...]}
# finding: {"project","kind","src_key","tgt_key","field","summary","detail"}  (detail is a dict)
# kinds: missing_in_tgt | missing_in_src | tail_post_cutover | field_mismatch |
#        content_mismatch | comment_mismatch | comment_uncheckable |
#        attachment_mismatch | link_mismatch | key_collision
#        ; fidelity_pct is None when no issues were compared; tails require common>0

# auditor/config_audit.py
def audit_config(src: JiraClient, tgt: JiraClient, jsm_projects=(), progress=None) -> dict
# -> {"areas": {area: {"src":n,"tgt":n,"in_both":n,...}}, "findings":[config_finding,...]}
# config_finding: {"area","name","kind","detail"}
# kinds: missing_in_tgt | type_mismatch | option_mismatch | structure_mismatch | field_mismatch | area_error
#   area_error: a side (source|target) was unreachable/unauthorized for this area; detail={"side","error"}.
#   Fail-loud: NEVER rendered as a clean "0 issues" — verdict MUST treat it as at least GAPS_FOUND.

# auditor/findings.py
def build_run_summary(project_results: dict, config_result: dict, blind_spots: list) -> dict
# -> {"stats": {...}, "verdict": str, "headlines": [str, ...]}
# verdict: "CLEAN" | "CLEAN_WITH_TAILS" | "GAPS_FOUND" | "CRITICAL"

# webapp/store.py
class Store:                          # all methods synchronous; sqlite3 with check_same_thread=False
    def __init__(self, db_path: str, key_path: str, secret_key: str | None = None): ...
    # settings_get/settings_set, create_migration/list_migrations/get_migration,
    # save_connection/get_connection(migration_id, role)/connection_secret(conn_row),
    # create_run/update_run/get_run/list_runs/active_run(migration_id),
    # set_run_projects/get_run_projects, insert_findings_issue/insert_findings_config,
    # query_issues(run_id, project=None, kind=None, q=None, page=1, size=50) -> (rows, total),
    # config_areas(run_id), query_config(run_id, area),
    # add_event/get_events(run_id, after_id=0), encrypt(dict)->bytes, decrypt(bytes)->dict

# webapp/runs.py
PHASES = ["verify", "scope", "permissions", "extract", "compare", "config", "finalize"]
class RunEngine:
    def __init__(self, store: Store, workspace_root: str, stages: dict | None = None): ...
    def start(self, migration_id: int, params: dict) -> int     # run_id; raises if active run
    # params may carry {"reuse_extracts_from": <run_id>} -> the new run reuses that
    # run's workspace and stage_extract skips projects whose gz files already exist
    def cancel(self, run_id: int) -> None
    def mark_stale_failed(self) -> int
```

URL map (webapp): `GET /` dashboard · `GET|POST /settings` · `POST /migrations` · `GET /migrations/{id}` · `POST /migrations/{id}/connections` (PAT form) · `GET /oauth/start` + `GET /oauth/callback` (3LO) · `POST /migrations/{id}/scope` (preview) · `POST /migrations/{id}/runs` · `GET /runs/{id}` (live page) · `GET /runs/{id}/stream` (SSE) · `POST /runs/{id}/cancel` · analysis pages `GET /runs/{id}/analysis[/projects|/projects/{key}|/config|/issues|/log]` · JSON `GET /api/runs/{id}/summary|projects|issues|config|events`.

---

