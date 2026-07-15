### Task 2: `webapp/config.py` — environment configuration

**Files:**
- Create: `webapp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
from webapp.config import load_config


def test_defaults(tmp_path, monkeypatch):
    for v in ("MA_DATA_DIR", "MA_BIND", "MA_PUBLIC_BASE_URL", "MA_SECRET_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.data_dir.endswith("data")
    assert cfg.bind_host == "127.0.0.1" and cfg.bind_port == 8484
    assert cfg.public_base_url == "http://localhost:8484"
    assert cfg.oauth_redirect_uri == "http://localhost:8484/oauth/callback"
    assert cfg.secret_key is None


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("MA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MA_BIND", "0.0.0.0:9000")
    monkeypatch.setenv("MA_PUBLIC_BASE_URL", "https://audit.example.com/")
    monkeypatch.setenv("MA_SECRET_KEY", "k" * 44)
    cfg = load_config()
    assert cfg.data_dir == str(tmp_path / "d")
    assert cfg.bind_host == "0.0.0.0" and cfg.bind_port == 9000
    assert cfg.public_base_url == "https://audit.example.com"
    assert cfg.oauth_redirect_uri == "https://audit.example.com/oauth/callback"
    assert cfg.secret_key == "k" * 44
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: `ModuleNotFoundError: No module named 'webapp.config'`.

- [ ] **Step 3: Write the implementation**

`webapp/config.py`:
```python
"""Env-driven configuration (the hosting-ready seam). All MA_* variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    data_dir: str
    bind_host: str
    bind_port: int
    public_base_url: str
    secret_key: str | None

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "auditor.db")

    @property
    def key_path(self) -> str:
        return os.path.join(self.data_dir, ".key")

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.public_base_url}/oauth/callback"


def load_config() -> Config:
    data_dir = os.environ.get("MA_DATA_DIR") or os.path.join(os.getcwd(), "data")
    bind = os.environ.get("MA_BIND", "127.0.0.1:8484")
    host, _, port = bind.rpartition(":")
    public = os.environ.get("MA_PUBLIC_BASE_URL", "http://localhost:8484")
    return Config(
        data_dir=data_dir,
        bind_host=host or "127.0.0.1",
        bind_port=int(port),
        public_base_url=public.rstrip("/"),
        secret_key=os.environ.get("MA_SECRET_KEY") or None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add webapp/config.py tests/test_config.py
git commit -m "feat: env-driven config with hosting-ready MA_* variables"
```

---

