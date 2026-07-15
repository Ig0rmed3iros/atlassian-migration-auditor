### Task 0: Scaffold the package

**Files:**
- Create: `pyproject.toml`, `pytest.ini`, `auditor/__init__.py`, `webapp/__init__.py`, `tests/__init__.py`, `README.md`

- [ ] **Step 1: Write the files**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "migration-auditor"
version = "0.1.0"
description = "Audit Jira Cloud-to-Cloud migrations: issue fidelity, config parity, interactive analysis."
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn>=0.29",
  "httpx>=0.27",
  "jinja2>=3.1",
  "cryptography>=42",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
migration-auditor = "webapp.main:cli"

[tool.setuptools]
packages = ["auditor", "webapp"]

[tool.setuptools.package-data]
webapp = ["templates/*.html", "static/*"]
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
addopts = -q
```

`auditor/__init__.py`, `webapp/__init__.py`, `tests/__init__.py`: empty files.

`README.md`:
```markdown
# Migration Auditor

Local web app that audits Jira Cloud -> Jira Cloud migrations: issue-data
fidelity (every issue, content fingerprints), config parity, permission
blind-spot detection — rendered as an in-app interactive analysis.

## Quickstart

    pip install -e .[dev]
    migration-auditor serve          # -> http://127.0.0.1:8484

Connect source and target with **Atlassian OAuth** (Settings -> register a
client first; see below) or a **PAT** (site URL + email + API token from
https://id.atlassian.com/manage-profile/security/api-tokens).

## Registering your own Atlassian OAuth app (optional, for the OAuth path)

1. Go to https://developer.atlassian.com/console/myapps -> Create -> OAuth 2.0 integration.
2. Add the **Jira API** with scopes: `read:jira-work`, `read:jira-user`, `offline_access`.
3. Set callback URL: `http://localhost:8484/oauth/callback`.
4. Copy the Client ID and Secret into the app's Settings page.

## Configuration (env)

| Var | Default | Purpose |
|---|---|---|
| `MA_DATA_DIR` | `./data` | SQLite DB + run workspaces |
| `MA_BIND` | `127.0.0.1:8484` | listen address |
| `MA_PUBLIC_BASE_URL` | `http://localhost:8484` | OAuth callback base |
| `MA_SECRET_KEY` | auto-keyfile `data/.key` | Fernet key for secrets at rest |

Extracted issue data contains customer content. It stays under `MA_DATA_DIR`
(gitignored). Do not commit or share it.
```

- [ ] **Step 2: Configure git identity + install**

```bash
cd /mnt/d/Atlassian-Products/Migration-auditor
git config user.name "Igor Medeiros" && git config user.email "dev@example.com"
python3 -m pip install -e .[dev] 2>&1 | tail -2
```
Expected: `Successfully installed migration-auditor-0.1.0` (deps may already be satisfied).

- [ ] **Step 3: Sanity check pytest collects nothing but runs**

Run: `python3 -m pytest`
Expected: `no tests ran` (exit code 5 is fine at this point).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml pytest.ini auditor/__init__.py webapp/__init__.py tests/__init__.py README.md
git commit -m "chore: scaffold migration-auditor package"
```

---

