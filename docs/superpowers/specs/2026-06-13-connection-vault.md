# Connection Vault — Saved Credentials Library — Design

**Date:** 2026-06-13
**Goal:** A single place to store reusable, encrypted credentials — Jira/Confluence
sites + their PATs, and the Anthropic AI key — so an operator defines a connection
once and then SELECTS it from a dropdown when creating migration or environment
audits, instead of re-typing site URLs and tokens every time.

## Requirements

### R1 — Saved-connection store (`webapp/store.py`)
New table `saved_connections`:
```sql
id INTEGER PK, name TEXT NOT NULL,           -- operator label, e.g. "Acme Prod (Cloud)"
product TEXT NOT NULL,                        -- jira | confluence
deployment TEXT NOT NULL,                     -- cloud | dc
site_url TEXT NOT NULL,
account_email TEXT,                           -- verified identity (filled on verify)
secret_enc TEXT NOT NULL,                     -- Fernet({"token":..., "email":...})
status TEXT NOT NULL DEFAULT 'unverified',    -- unverified | verified
created_at REAL NOT NULL
```
Methods: `list_saved_connections(product=None)`, `get_saved_connection(id)`,
`create_saved_connection(name, product, deployment, site_url, email, token)`
(encrypts the secret), `mark_saved_connection_verified(id, account_email)`,
`delete_saved_connection(id)`. Secret never returned in plaintext except via the
existing `connection_secret`-style decrypt for internal use. Idempotent table
creation in `_migrate`.

### R2 — Connections config screen (`/connections`)
A new page under the sidebar "Configure" group ("Connections"), listing all saved
connections (name, product chip, deployment, site, status). Each row: a Verify
button (live `connector.verify` against the stored secret, marks status) and a
Delete button. A "New connection" form: name, product (jira/confluence),
deployment (cloud/dc), site URL, email (required for cloud), API token. On submit:
encrypt + store, then attempt a live verify (best-effort; a failed verify still
saves but stays `unverified` with a flash). The token field is write-only (never
rendered back). Reuse the glass design + the existing PAT-entry hints.

### R3 — AI key stays on Settings, cross-linked
The Anthropic key remains on `/settings` (already encrypted there). Add a small
cross-link between `/settings` and `/connections` so the "configuration" hub is
discoverable. (No behavior change to the AI key.)

### R4 — Select a saved connection in audits (`migration.html` + route)
On the audit detail page connection step, for EACH role (source/target for
migration; source for environment), add a "Use a saved connection" dropdown listing
saved connections whose `product` matches the audit's product. Selecting one and
submitting POSTs to a new route `POST /migrations/{mid}/connections/from-saved`
with `role` + `saved_id`. The route:
- loads the saved connection, decrypts its secret,
- runs the SAME live verify the manual path runs (`connector.verify`),
- on success copies site/secret/deployment/email into the migration's role
  connection via the existing `store.save_connection` + `mark_connection_verified`
  (so everything downstream — scope, run, fix — is unchanged),
- on failure redirects back with an error.
The manual entry form remains as the alternative. The dropdown is hidden/empty-state
when no saved connections exist for that product.

### R5 — Safety / privacy
- Secrets Fernet-encrypted at rest (reuse `store.encrypt`); never logged; never
  rendered back to the browser; token inputs are write-only.
- `/connections/from-saved` re-verifies live before trusting a saved secret (a
  rotated/expired token must fail loudly, not silently copy a dead credential).
- Deleting a saved connection does NOT touch any migration connection already
  created from it (copy semantics, not link).

### R6 — Tests
- store: CRUD + encryption-at-rest (token not in `secret_enc` plaintext) + verify
  marking + product filter.
- routes: `/connections` renders + create/verify/delete; from-saved copies into the
  migration connection and verifies; from-saved with a bad secret errors; dropdown
  filtered by product; deleting a saved connection leaves an existing migration
  connection intact.

## Out of scope
- Editing a saved connection's token in place (delete + recreate instead).
- OAuth saved connections (PAT only in v1; OAuth stays per-migration).
- Sharing/multi-user (single-operator localhost).
