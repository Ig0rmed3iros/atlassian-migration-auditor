# User Access Cloning — Design

**Status:** approved design, pre-implementation
**Date:** 2026-06-24
**Author:** session brainstorm (igordmb)

## Summary

A new feature in the Migration-auditor app that **clones a Jira Cloud user's access onto another account** — additively. Given a `main` user and a `clone` user on the same instance, the tool gives `clone` the same **group memberships** and **direct project-role memberships** that `main` has. It supports a single pair entered in a form and a `main,clone` CSV for bulk runs. It exposes a core engine, a CLI subcommand, and a web UI, reusing the app's existing `JiraClient`, connection vault, live-write safety layer, and background run/event/stream infrastructure.

This is a **live-write** tool. It is additive only and idempotent, defaults to a read-only preview, and runs under the existing blast-radius cap and circuit breaker.

## Goals

- Clone, additively, `main` → `clone` on one chosen instance: **all group memberships** and **all direct (user-actor) project-role memberships**.
- Two input modes: a single `main → clone` pair (form/flags), and a `main,clone` CSV (bulk).
- Identity by **accountId or email** (auto-detected), with honest reporting of anything unresolvable.
- **Groups first (fast, foreground); project roles in the background** with live progress.
- A **preview** (read-only) before any write, plus a full **dry-run** mode.
- Reuse the existing app: connections, `JiraClient`, live-write safety, run/event/stream UI.

## Non-goals (stated so the report never implies otherwise)

- **No removals.** Additive only — the `clone` keeps whatever access it already had; nothing is taken away. (No "exact mirror" mode in v1.)
- **Direct user grants inside permission schemes** are not cloned (rare; Jira Cloud overwhelmingly grants schemes to groups/roles, both of which ARE covered — globals and group-mediated project access via group cloning, role-mediated access via role cloning). Surfaced in the report as "not cloned."
- **Personal filter / dashboard / board shares** are not cloned. Surfaced as "not cloned."
- **No user provisioning.** Both `main` and `clone` must already exist as accounts on the instance. The tool never creates accounts.
- Jira Cloud only (the app's existing Cloud `JiraClient`); Data Center is out of scope for v1.

## Identity model

Each `main`/`clone` value is either a Jira **accountId** or an **email**, auto-detected:

- **accountId** — used directly (format is distinctive: `557058:<uuid>` style, or legacy 24-hex). No lookup.
- **email** — resolved via `GET /rest/api/3/user/search?query=<email>`, matching an active `atlassian`-type account whose `emailAddress` equals the input (case-insensitive). On instances that hide emails, the search index can still match the query even when the response omits the email.

Resolution outcomes per value:
- **resolved** — exactly one account found → proceed.
- **unresolved** — zero matches → the pair is **blocked** (reported, never guessed, never written).
- **ambiguous** — more than one match → blocked and reported (operator must disambiguate with an accountId).

Both sides of a pair must resolve, and `main != clone` (a self-clone is a no-op, reported).

## What gets cloned

For one resolved pair `(main_id, clone_id)` on instance `C`:

### Phase 1 — Group memberships (foreground, fast)
1. Read `main`'s groups: `GET /rest/api/3/user/groups?accountId={main_id}` → `[{name, groupId}]`.
2. Read `clone`'s groups (same call) to compute the additive delta (skip groups `clone` is already in).
3. For each missing group: `POST /rest/api/3/group/user?groupId={groupId}` body `{"accountId": clone_id}`.
4. Idempotent: a group `clone` already belongs to is recorded as `already-member`, not re-added. An API "already a member" response is treated as `already-member`, never a failure.

This phase alone covers **global permissions** and all **group-mediated project access**, because Jira Cloud grants those to groups.

### Phase 2 — Direct project-role memberships (background)
Some users are added to a project role **directly** as a user actor (not via a group). These need a scan:
1. **One-time instance scan** (per run, concurrent — reuses `auditor.envaudit._pool.map_results`): enumerate projects (`all_projects()`), and for each project read its roles and actors (`GET /project/{key}/role` → per-role actor details). Build an index `accountId → [(projectKey, roleId, roleName)]` from `atlassian-user-role-actor` actors only (group actors are already handled by Phase 1).
2. For each pair: the roles `main` holds directly = `index[main_id]`. The additive delta = those minus roles `clone` already holds directly (also from the index).
3. For each missing role: `POST /rest/api/3/project/{projectKey}/role/{roleId}` body `{"user": [clone_id]}`.
4. Idempotent: a role `clone` is already an actor in is `already-member`.

The scan is the expensive part; it runs **once per run** and serves all pairs in the run (the index is built before the per-pair role apply), so a bulk CSV pays the scan cost a single time.

## Run model & execution

A clone operation is a **background run** reusing the existing `RunEngine` (the same machinery as audits/fixes: a daemon thread, the `events` table, the `/runs/{id}/stream` SSE feed, and the phase stepper UI).

**Run kind:** `clone`. **Phases:** `resolve → groups → role-scan → roles → finalize`.
- `groups` completes fast → its per-pair results are visible almost immediately.
- `role-scan` + `roles` continue in the background; progress and results stream in.

**Persistence wrinkle (resolved in the plan):** the existing `runs` table is migration-centric (`migration_id`). A clone run is keyed to a **`saved_connection`**, not a migration. The plan will either make `migration_id` nullable for clone-kind runs and add a `connection_id` reference, or carry the connection in `params_json` — whichever is least invasive. The run/event/stream UI keys on `run_id`, so it works regardless. Clone runs get their own list page (not shown under migrations).

**Run params (`params_json`):** `{connection_id, mode: "pair"|"csv", pairs: [[main, clone], ...], dry_run: bool, apply: bool}`.

**Results** are recorded per pair and per action so the run page (and a downloadable CSV/JSON report) can show, for each `main → clone`: resolved ids, groups `added` / `already-member` / `failed`, roles `added` / `already-member` / `failed`, and any `blocked` reason. Failures carry the HTTP status + message.

## Safety

- **Preview (read-only):** resolves identities and computes the **group** delta for every pair, with no writes. Shows add / already-member / blocked counts per pair. Project roles are noted as "scanned on apply" (the scan is too expensive to run on every keystroke-level preview). Implemented as a `dry_run` run that stops after the `groups` plan, or a synchronous preview endpoint — plan decides.
- **Full dry-run** (`--dry-run` / UI toggle): runs **all** phases including the role scan, reports everything that *would* change, writes nothing. For when the operator wants the complete picture and accepts the scan time.
- **Idempotent:** every write is preceded by a membership check; re-running a completed run is safe.
- **Blast-radius cap:** reuse the existing additive-write guard. The relevant cap is on *additions* per run; reuse `MA_MAX_*` conventions (the plan picks/extends the exact knob — these are non-destructive adds, so the destructive cap does not apply; a dedicated `MA_MAX_CLONE_WRITES` or similar may be introduced).
- **Circuit breaker:** reuse `auditor.envaudit.apply`'s breaker (`MA_BREAKER_THRESHOLD`) so repeated 5xx/429 during a write batch halts the run loudly.
- **Concurrency:** the role-actor scan uses `map_results` (shared `MA_GATHER_WORKERS`). The per-pair writes may use the apply pool (`MA_APPLY_WORKERS`); ordering does not matter (additive, idempotent).

## Architecture (units)

- **`auditor/cloneaccess.py`** — the engine, product-agnostic of UI/CLI:
  - `resolve_identity(client, value) -> {kind, accountId|None, reason|None}` — accountId passthrough or email search.
  - `gather_user_access(client, account_id) -> {groups: [{name, groupId}], direct_roles: [(projectKey, roleId, roleName)]}` (roles via the prebuilt index).
  - `build_role_index(client, progress) -> {accountId: [(projectKey, roleId, roleName)]}` — the concurrent instance scan.
  - `plan_clone(main_access, clone_access) -> {groups_to_add, roles_to_add, already_member, ...}` — additive diff.
  - `apply_groups(client, clone_id, plan, dry_run, breaker) -> results`
  - `apply_roles(client, clone_id, plan, dry_run, breaker) -> results`
- **New `JiraClient` methods** (`auditor/client.py`): `user_groups(account_id)`, `add_user_to_group(group_id, account_id)`, `project_role_map(project_key)`, `project_role_actors(project_key, role_id)`, `add_user_to_project_role(project_key, role_id, account_id)`. Each returns `(value, err)` or raises consistently with the existing client surface, and is exercised by `MockTransport` tests.
- **Store** — a `clone` run kind + per-pair/per-action result rows (or results in the run's events/results JSON). Exact schema in the plan.
- **CLI** (`webapp/main.py` `cli()`): `migration-auditor clone-access`.
- **Web UI** — a "Clone access" page + router (mirrors `make_fix_router(store, engine)`), a Jinja template extending `base.html`, listed in the app nav.

## CLI

```
migration-auditor clone-access --conn <saved-connection-name-or-id> \
    (--main X --clone Y | --csv pairs.csv) \
    [--apply] [--dry-run] [--json report.json]
```

- Default (no `--apply`): **preview** (groups plan + identity resolution; roles noted, not scanned).
- `--dry-run`: full plan incl. role scan, no writes.
- `--apply`: perform the additive writes (groups foreground, roles after the scan).
- `--csv`: a file with a header row containing `main,clone` columns (extra columns ignored). One pair per row.
- Exit code: `0` clean, `1` operational error, `2` if any pair was blocked or any write failed (so automation can gate).

## Web UI

A single **Clone access** page:
- **Connection picker** — choose a verified `saved_connection` (the instance to operate on).
- **Single pair** — two inputs (`main`, `clone`) with an accountId/email hint.
- **CSV upload** — a `main,clone` file; parsed and shown as a table of pairs.
- **Preview** button → a table per pair: resolved identity (or blocked reason), groups to add / already-member counts; roles shown as "scanned on apply."
- **Apply** button → starts the background `clone` run and navigates to the run/stream view: the phase stepper (`resolve → groups → role-scan → roles → finalize`), live events, and a results table that fills in (groups immediately, roles as the background scan completes).
- **Download report** (CSV/JSON) from the finished run.
- A **dry-run** toggle for the full no-write plan.

## CSV format

Header row with (at least) `main` and `clone` columns (case-insensitive; extra columns ignored). Each value is an accountId or email. Example:

```
main,clone
admin@acme-source.example,admin@acme-target.example
557058:1111-2222,557058:3333-4444
```

## Error handling & reporting

- **Per-value:** unresolved / ambiguous identity → pair `blocked`, reported with the input value and reason; no writes for that pair.
- **Per-action:** a failed `POST` (4xx object error) is recorded against that group/role with status + message; siblings continue. Repeated 5xx/429 trips the circuit breaker and halts the run loudly (the partial results are persisted and inspectable).
- **Self-clone** (`main` resolves to the same accountId as `clone`) → reported as a no-op.
- **The final report** lists, per pair: added groups, added roles, already-member items, failures, blocked reasons, and the explicit **"not cloned"** categories (permission-scheme direct user grants; personal shares).

## Testing approach

- **Engine unit tests** (`MockTransport` `JiraClient`, the project's established pattern): identity resolution (accountId passthrough, email hit, miss, ambiguous); group delta + idempotency; role-index build from project/role actors; additive role delta; dry-run writes nothing; failure isolation; breaker trip.
- **CLI tests:** preview vs `--apply` vs `--dry-run`; CSV parsing (header detection, extra columns, blank rows); exit codes (0 / 1 / 2).
- **Determinism for the role scan:** the concurrent index build must equal the sequential build (`MA_GATHER_WORKERS=1` vs `>1`), mirroring the existing gather-equivalence tests.
- **Route smoke test:** preview + apply through `TestClient`, asserting the run is created and the results render.

## Open items (for the plan to resolve, not blockers)

- Exact `runs`-table change for a connection-keyed (non-migration) clone run vs carrying the connection in `params_json`.
- The precise blast-radius knob for additive writes (reuse vs a new `MA_MAX_CLONE_WRITES`).
- Whether preview computes the group plan synchronously (a fast endpoint) or as a short dry-run run.
