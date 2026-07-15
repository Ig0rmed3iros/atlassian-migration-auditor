> **Public portfolio copy.** A sanitized, self-contained snapshot of a personal
> project, published to demonstrate AI-assisted development on a non-trivial
> codebase (~47k lines of Python, 1,585 tests). Real instance data, databases, credentials,
> and internal review artifacts are excluded; every sample identifier (emails,
> tenant URLs, API tokens) is a synthetic placeholder. Built with **Claude Code**
> using spec-driven, test-driven development and multi-agent adversarial review —
> the `docs/superpowers/` specs and plans capture that workflow.

---

# Atlassian Audit Platform

Local web app for auditing Atlassian instances — Jira or Confluence, Cloud or
Data Center. Two audit types share one engine, one connection model, and one
interactive analysis UI.

## Audits

The platform runs two kinds of audit. Both are read-only against the sources
they read, both render an in-app interactive analysis, and both show their
workflow as a live phase stepper.

### Migration audit

Point at a **source and a target** and audit a migration end to end:
per-item fidelity on standard fields, description, comments, attachments, and
custom-field values (via content fingerprints, matched by field name and
normalized per type), config parity, and permission blind-spot detection. Jira
or Confluence, Cloud or Data Center source; the target side is always Cloud.
This is the original
feature — see [Supported migrations](#supported-migrations),
[Remediation](#remediation), and [Find solutions](#find-solutions) below.

### Environment audit

Point at a **single live Jira environment** (Cloud or Data Center) and run an
AI-powered health and consistency audit. The audit gathers the environment's
configuration, runs deterministic health checks, and (optionally) has Claude
analyze the whole picture to surface inconsistencies, risks, root causes, and
prioritized recommendations. Like the migration audit, but on one live
environment.

**Workflow.** Each environment audit walks a visible sequence of steps, shown
live on the run page and as an explainer on the create-audit page:

1. **Connect** — authenticate the single source connection (read-only; an
   environment audit never writes to the environment).
2. **Scope** — enumerate the environment's projects.
3. **Gather** — pull the configuration into a snapshot: statuses, issue types,
   priorities, resolutions, link types, roles, screens, schemes, custom
   fields, and workflows (with transitions/statuses on Cloud). Data Center
   areas with no list API are recorded as `skipped`, never silently absent.
4. **Checks** — run deterministic rules over the snapshot (duplicate fields,
   transition-less workflows, unused schemes, orphaned statuses, and more),
   emitting findings with a severity.
5. **AI analysis** — an optional Claude assessment of the snapshot summary and
   findings (health score, grade, themes, top risks, quick wins).
6. **Report** — a verdict, findings by area and severity, and the AI
   assessment, rendered on the analysis page.

**AI is metadata-only and optional.** The AI analysis is the only outbound
path, and it enforces the same hard privacy boundary as Find solutions: only
configuration **metadata** leaves the machine — per-area object names and
counts, scheme / workflow / field / screen names, the boolean structure of
workflows (status and transition names), and the deterministic findings.
**Never** issue or page bodies, descriptions, comments, field values,
account IDs, member identities, or any issue data. Object **names** do leave
(they are the audit's subject), and because an admin can name a group / filter /
field after a person, email addresses embedded in names are **redacted** from
the outbound payload before it is sent — other identifiers a name might contain
(e.g. a personal name) are not, so treat names as potentially identifying. The
pass is also fully **optional** —
with no Anthropic key configured the run still completes with the
deterministic findings and an "AI analysis skipped (no key)" note; a missing
key never blocks a run. To enable it, add an Anthropic API key in Settings
(see [Find solutions → Setup](#setup)).

**Jira and Confluence.** Environment audit supports Jira (Cloud and Data Center)
and Confluence (the gather/checks dispatch on the connected product), each with
its own area readers and deterministic checks.

## Supported migrations

| Migration | Status | Notes |
|---|---|---|
| Jira Cloud → Cloud | ✅ | standard-field + content-fingerprint + custom-field-value fidelity, config parity, blind-spot detection + one-click elevation |
| Jira Data Center → Cloud | ✅ | standard-field + content-fingerprint + custom-field-value fidelity; config parity is partial — areas with no DC API are reported as `skipped`, never silently absent |
| Confluence Cloud → Cloud | ✅ | page **and blog-post** fidelity + macro inventory & target signature |
| Confluence Data Center → Cloud | ✅ | page **and blog-post** fidelity + macro inventory & target signature |

The target side is always Cloud. Cloud → DC and DC → DC are not blocked by
the design, but they are untested — no support claim.

## Quickstart

    pip install -e .[dev]
    migration-auditor serve          # -> http://localhost:8484

From the dashboard, pick an audit type. A **migration audit** asks for the
product (Jira or Confluence) and connects a source and a target side. An
**environment audit** connects a single Jira side and runs the workflow above.

## Authentication per deployment

- **Cloud + PAT** — site URL, email and an API token from
  https://id.atlassian.com/manage-profile/security/api-tokens. Sent as
  Basic email:token.
- **Data Center + PAT** — site URL and a Personal Access Token (profile →
  Personal Access Tokens). Sent as `Bearer <token>`; a DC PAT has no email,
  so the form hides the field when you pick Data Center.
- **Atlassian OAuth** — Jira Cloud only (Settings → register a client
  first; see below).

## Registering your own Atlassian OAuth app (optional, Jira Cloud only)

1. Go to https://developer.atlassian.com/console/myapps -> Create -> OAuth 2.0 integration.
2. Add the **Jira API** with scopes: `read:jira-work`, `read:jira-user`, `offline_access`. Add `manage:jira` too if you want the one-click permission-elevation feature; without it the app will tell you to grant access manually.
3. Set callback URL: `http://localhost:8484/oauth/callback`.
4. Copy the Client ID and Secret into the app's Settings page.

## Confluence scope notes

- **Current pages only.** Blog posts, archived pages and drafts are out of
  scope.
- **Spaces match by key; pages match by exact title** within a space (title
  is the only page identity that survives a migration). A renamed page
  therefore reads as one missing + one extra page — known limitation.
- **Macro inventory + target fidelity.** Occurrences of every
  `<ac:structured-macro>` are counted per side and diffed (a source macro with
  no occurrences on the target is flagged — the classic DC macro with no Cloud
  equivalent). Per page, a macro **target signature** (the `ac:parameter`
  values and `ri:*` resource references that the prose fingerprint strips) is
  also compared, so a macro pointing at the wrong JQL / included page /
  attachment / space — which renders the same prose but the wrong content — is
  caught (`macro_param_mismatch`) instead of reading clean. Space permissions,
  templates and settings are NOT audited.
- **Inline expansion caps are honest.** Per-page comments/attachments past
  the API's inline cap are treated as floors, never as complete sets. When
  the complete side contradicts a floor (an inline source attachment absent
  from the target's exact set, or a capped count floor exceeding the other
  side's exact total) that is a proven, real mismatch; everything
  unprovable is reported as a `*_uncheckable` advisory instead — a
  truncated set is never diffed as if it were the whole truth.

## Remediation

After an audit run surfaces findings you can open the Fix options screen and
start a fix run. Fix runs operate on the **target side only** — they never
read from or write to the source, and they never modify an issue or page body.

### Fix tiers

| Tier | What it does | Applies to |
|---|---|---|
| **create** | Creates the missing config object on the target (status, priority, resolution, issue type, link type, custom field + context + options) | Config-parity findings with a `fix_payload` |
| **wire** | Places a created custom field onto an existing screen (screen-tab mapping captured from source) | `jira.custom_field.wire_screen` |
| **populate** | Bulk-sets a custom field on migrated issues using captured source values | `jira.custom_field.populate` |

All three tiers are **target-only** and **forward-only**: objects are created
or updated, never deleted. Rollback is not automated.

### Guarantees

- **Pre-check before write.** Every create action checks whether the named
  object already exists on the target. A duplicate-name match is logged as a
  no-op (`error: exists`) and not double-created.
- **Proof by re-audit.** After applying, the fix run re-checks each touched
  finding against the live target to compute a closure count. The run verdict
  (`FIXED_CLEAN`, `FIXED_PARTIAL`, `FIX_FAILED`) reflects live state, not the
  HTTP response code from the write call.
- **Persist-after-act.** Fix actions are recorded (in the finalize phase) after
  the apply phase completes. A completed or failed run can be inspected
  post-hoc, and the pre-check makes a retry safe.

### Capability matrix

**AUTO-FIXED in v1** (checkbox in the Fix options screen; applied by the fix engine):

| Finding type | What the fix does |
|---|---|
| Missing status / priority / resolution / issue type / link type | Creates the definition on the target |
| Missing custom field | Creates the field with its context(s) and select options |
| Select options missing on an existing field | Adds the delta options to the field's default context |
| Custom field not on screen | Wires the field onto its source screen tab(s) on the target |
| Custom field values missing on issues | Bulk-sets each issue's source value on the target |

**DETECT-AND-GUIDE in v1** (shown as read-only guidance; operator acts manually):

| Finding type | Why not automated | Guidance provided |
|---|---|---|
| Screens missing or incomplete | No payload capture in v1; future work | Not yet |
| Workflow status/transition wiring | Edits live workflow behaviour (Tier-2, spec R8) | Status names to wire listed in guidance |
| Workflow structure mismatch | Topology changes on live workflows (Tier-2, spec R8) | Affected workflow names listed in guidance |
| Key collisions | Overwriting target issue content is destructive | Colliding keys listed with JQL for review |
| Missing issues / pages | Created-date, reporter and history are immutable | Source keys listed; re-migrate with JCMA/CCMA |
| Missing users | User provisioning goes through Atlassian Access, not the Jira API | User list provided for org-admin invite |
| Confluence labels / macros | No Confluence payload pipeline in v1 | Not yet |

### v1 non-goals

The following are explicitly out of scope for v1:

- User creation (provisioning goes through Atlassian Access, not the Jira
  API).
- Issue or page recreation from the source.
- Screen creation (no payload capture; listed as future work).
- Workflow editing — status/transition wiring and structural workflow
  reconciliation (the risk of corrupting a live workflow outweighs the
  benefit of automation; guidance names the exact statuses and workflows
  to fix manually).
- Confluence remediation — label creation and macro substitution (detect-
  and-guide only; no Confluence payload pipeline in v1).
- Automatic rollback (the target is authoritative; forward-only corrections
  are safer than attempted undo).

## Find solutions

Each finding in an audit run has a **Find solutions** button. When clicked it
sends the finding to the Claude API, which web-searches for community threads,
documentation, and known workarounds relevant to that specific finding type,
then returns a ranked summary with source links.

### What leaves the machine

The feature enforces a strict **metadata-only privacy boundary**: the only
information sent to Claude is the finding's kind (e.g. `missing_in_tgt`,
`option_mismatch`), the object name or issue key (e.g. a field, status, macro,
or `ACME-7`), the affected field name, and the product and migration direction
(e.g. "Jira Data Center to Cloud"). Issue bodies, page content, comments,
attachments, captured values, content fingerprints, user data, and every other
piece of customer-facing content stay entirely on the local machine and are
never included in the query.

### Setup

1. Open **Settings** in the app.
2. Paste an Anthropic API key into the **Anthropic API key** field.
3. Optionally set `MA_SOLUTIONS_MODEL` in the environment to use a different
   Claude model (default: `claude-opus-4-8`; accepts any model ID supported
   by your key).

### Behaviour

- **On-demand.** Nothing is searched automatically. A search runs only when
  you explicitly click Find solutions for a finding.
- **Cached.** Results are cached per finding signature (kind, area, name/keys,
  field, product and direction).
  A second click on the same finding returns the cached result instantly
  without a second API call. The cache persists for the lifetime of the run.
- **Read-only.** The feature never applies, creates, or modifies anything.
  It only returns informational search results.

### Clone user access

Additively clone a Jira Cloud user's group memberships and direct project-role
memberships onto another account — single pair or a `main,clone` CSV.

**Modes:**

| Invocation | What happens |
|---|---|
| (default — no extra flags) | Groups-only **preview**: resolves identities, diffs groups, **no writes**, roles not scanned |
| `--dry-run` | Full plan incl. role scan, **writes nothing** |
| `--apply` | Perform the additive writes (groups + roles) |
| `--apply --dry-run` | Safe full dry-run — `--dry-run` always wins; never writes |

**Identity.** Each account is specified as an accountId or email. AccountIds
are detected by shape (no HTTP call). Emails are resolved via Jira's user-search
API; unresolved or ambiguous accounts are reported and never guessed.

**Guarantees.**
- **Additive only** — never removes a user from a group or role. Re-running is
  safe (idempotent).
- **Exit codes:** `0` clean / `1` operational error / `2` blocked or failed.

**Non-goals (stated plainly).** No account provisioning. No removals.
Permission-scheme direct user grants and personal filter/dashboard shares are
NOT cloned.

**Knobs.** Reuses `MA_GATHER_WORKERS` for role-scan concurrency and
`MA_BREAKER_THRESHOLD` for the write circuit breaker (see the Configuration
table above).

**Usage examples:**

```
# Preview (groups only, no writes):
migration-auditor clone-access --conn my-jira \
  --main alice@example.com --clone bob@example.com

# Full dry-run (roles scanned, no writes):
migration-auditor clone-access --conn my-jira \
  --main alice@example.com --clone bob@example.com --dry-run

# Apply (live writes):
migration-auditor clone-access --conn my-jira \
  --main alice@example.com --clone bob@example.com --apply

# Bulk via CSV (columns: main,clone):
migration-auditor clone-access --conn my-jira \
  --csv pairs.csv --apply --json report.json
```

## Known limitations

- **Cross-dialect content (Jira DC → Cloud).** Wiki-markup and ADF bodies
  are compared through a canonicalized text fingerprint, so faithful prose
  hashes equal across dialects. Macro-heavy bodies can still differ
  structurally; those findings carry a `cross_dialect` badge rather than
  being suppressed.
- **Capability honesty over coverage claims.** Blind-spot detection and
  elevation are Jira Cloud only — any other side is skipped with one
  explicit warning (counts unverified). Jira DC config areas without a DC
  API surface as `skipped` in the area summary, and DC version differences
  degrade to `skipped`/`area_error`, never to silence.
- **`reuse_extracts_from` is format-checked.** Extract files are stamped
  with a format version; re-running against a prior run's workspace reuses
  only current-format extracts. A cached side written by a pre-upgrade
  build (different fingerprint scheme) is re-extracted automatically, with
  a warn event — mixing formats would flag every common item as drifted.
- **Confluence bodies are compared in the storage representation** (both
  deployments serve `body.storage` XHTML). Other representations are out of
  scope.

### Audit coverage limitations (v1)

What the engine does NOT yet verify — stated plainly so a high fidelity score is
never mistaken for completeness:

- **Custom-field VALUES** *are* now compared (every type), matched by field
  NAME and normalized per type, and a value drift or loss dents fidelity. The
  caveat: types whose cross-instance identity is inherently uncertain (user /
  group pickers → account remap, cascading selects, app-provided fields, rich
  text across wiki/ADF) are tagged **verify-sensitive** — treat those mismatches
  as "verify", not certain loss. A field absent on the target, or whose NAME is
  duplicated on an instance, is disclosed (and the absent-field case dents
  fidelity, since its values are lost). This path is validated against synthetic
  fixtures, not a live DC↔Cloud pair, so spot-check the first real run. A
  rich-text custom field whose entire body is non-prose (image / link / mention
  only) is treated as empty for value comparison — the same canonical-text rule
  the description/environment checks use — so its migration is not value-verified.
- **Worklog / time-tracking** is compared as a count only; logged hours,
  authors and estimates are not verified. **Attachment** fidelity keys on
  `filename|size`, so a same-name same-size content swap is invisible.
- **Subtask membership and issue-link integrity** are under-verified (subtasks
  are extracted but not diffed).
- **Memory:** migration extract/compare load each project's issues into memory
  (no streaming); a very large single project is bounded by available RAM.
- **Cloud approximate-count** can lag the search index. The completeness gate
  and the auto-fix value/empty checks inherit that and are deliberately
  conservative (they skip a delete they cannot confirm), but a freshly-changed
  count may read stale.
- **Confluence env audit** probes per-space page-count and permissions for the
  first 250 spaces; larger instances are sampled. Attachments and templates are
  gathered but do not yet produce findings.
- **Concurrency:** the app is single-process / single-user by design (one shared
  SQLite connection behind one lock); the "hosting" multi-tenant seam is not
  implemented.
- **AI metadata boundary** is a per-area allowlist enforced by the area readers;
  a newly-added area forwards only counts + names by those readers, but review a
  new area's reader before trusting it as metadata-only.

## Configuration (env)

| Var | Default | Purpose |
|---|---|---|
| `MA_DATA_DIR` | existing `./data`, else `$XDG_DATA_HOME/migration-auditor` | SQLite DB + run workspaces (resolved path is logged at startup) |
| `MA_BIND` | `127.0.0.1:8484` | listen address. A **non-loopback** host is **refused at startup** (this app has no authentication, so exposing it hands stored admin credentials and the live-write delete path to the network) unless `MA_ALLOW_PUBLIC_BIND=1`. |
| `MA_ALLOW_PUBLIC_BIND` | unset | Opt-in to bind a non-loopback host. Only set it when an authenticating reverse proxy / tunnel fronts the service. |
| `MA_PUBLIC_BASE_URL` | `http://localhost:8484` | OAuth callback base |
| `MA_SECRET_KEY` | auto-keyfile `<data dir>/.key` | Fernet key for secrets at rest. On a filesystem that ignores chmod (e.g. WSL drvfs) the on-disk key is not owner-only — supply this out-of-band, or set `MA_STRICT_PERMS=1` to refuse to start when at-rest perms can't be enforced. |
| `MA_STRICT_PERMS` | unset | Refuse to start if the DB / key at-rest permissions can't be enforced (see above). |
| `MA_LOG_LEVEL` | `INFO` | Root log level for the `serve`/`backup` CLI. |
| `MA_SOLUTIONS_MODEL` | `claude-opus-4-8` | Default Claude model for Find solutions |
| `MA_MAX_DESTRUCTIVE` | `50` | Live-fix blast-radius cap: a batch deleting more than this many objects aborts before any write (`0` = kill switch). |
| `MA_MAX_POPULATE` | `10000` | Blast-radius cap for one migration value-populate: a values file larger than this is refused before any write (a sanity ceiling against a runaway file). |
| `MA_BREAKER_THRESHOLD` | `5` | Live-fix circuit breaker: stop after this many server-side 5xx/429 during a write batch (`0` = disabled). |
| `MA_APPLY_WORKERS` / `MA_GATHER_WORKERS` | `6` / `10` | Concurrency for the live-write apply / gather pools (`1` = sequential). `MA_GATHER_WORKERS` governs both the env-audit gather and the migration config-parity gather (areas + custom-field options + screen fields). |
| `MA_EXTRACT_PAGE` | `100` | Issues fetched per search page during migration extract. Fewer pages = fewer round-trips and less rate-limit exposure. Clamped to >= 1. |
| `MA_EXTRACT_WORKERS` | `2` | Concurrency for a project's source vs target extraction (the two sides hit different instances). `1` = sequential. Projects are still extracted one at a time. |

### Operate

- **Health:** `GET /healthz` → `200 {"status":"ok","db":true,"version","schema_version"}`, or `503` when the DB is unreachable (probe-friendly, no auth, no side effects).
- **Backup:** `migration-auditor backup [DEST]` writes a consistent snapshot via SQLite `VACUUM INTO` (safe while serving; refuses to overwrite). Default `DEST` is `<data dir>/backups/auditor-<UTC timestamp>.db`. Run one **before** any live-fix apply.
- **Schema version:** stamped in `PRAGMA user_version` and surfaced by `/healthz`.

### Headless audit (CI / cron)

Run an environment audit without the web app and **gate on the result** — the
deterministic verdict + health score, no DB, no AI:

```
MA_AUDIT_TOKEN=<pat> migration-auditor audit \
  --site https://acme.atlassian.net --product jira --deployment cloud \
  --email you@acme.example --json result.json --fail-on NEEDS_ATTENTION
```

The PAT is read from `MA_AUDIT_TOKEN` (never a flag — keeps it out of shell
history); `--email` is required for a Cloud PAT and omitted for `--deployment
dc`. Exit code: **0** when the verdict is better than `--fail-on`, **2** when it
meets/exceeds it (so a CI step fails the build), **1** on an operational error.
`--json [PATH]` writes the full result (verdict, health, grade, severity counts,
every finding) to a file or stdout; omit it for a one-line summary.

The headless verdict is **deterministic-only** (no AI), so it is reproducible
for gating — note the web app's verdict can be *stricter* (a poor AI grade alone
can raise it to NEEDS_ATTENTION), so a green CI gate is not a promise the
dashboard shows the same.

Extracted issue/page data contains customer content. It stays under
`MA_DATA_DIR` (gitignored). Do not commit or share it. **Note:** the CSV/JSON
finding export is a download that includes raw issue `summary` text — treat an
exported file as sensitive output, not just metadata.
