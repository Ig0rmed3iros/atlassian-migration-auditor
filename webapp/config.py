"""Env-driven configuration (the hosting-ready seam). All MA_* variables."""
from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("migration_auditor.config")


def _is_loopback_host(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, and localhost — the only hosts safe to bind
    without authentication. A name we can't classify here is treated as
    non-loopback (fail closed)."""
    h = (host or "127.0.0.1").strip().strip("[]").lower()
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def assert_safe_bind(host: str) -> None:
    """Raise unless `host` is safe to bind for an app with NO authentication.
    A non-loopback bind exposes stored admin credentials and the live-write
    delete path to the network, so it is refused unless MA_ALLOW_PUBLIC_BIND
    opts in (front the service with auth / a tunnel first). Called ONLY from the
    serving entry point — non-serving CLI subcommands (e.g. `backup`) bind
    nothing and must not be blocked by this."""
    if _is_loopback_host(host):
        return
    allow = os.environ.get("MA_ALLOW_PUBLIC_BIND", "").strip().lower() \
        in ("1", "true", "yes")
    if not allow:
        raise ValueError(
            f"refusing to bind to non-loopback host {host!r}: this app has no "
            "authentication, so a non-loopback bind exposes stored admin "
            "credentials and the live-write delete path to the network. Bind "
            "127.0.0.1 (default) and reach it via an SSH tunnel or an "
            "authenticating reverse proxy; set MA_ALLOW_PUBLIC_BIND=1 to "
            "override only if an external auth layer fronts this service.")
    log.warning("binding NON-LOOPBACK host %s with no app-level auth "
                "(MA_ALLOW_PUBLIC_BIND set) — ensure an external auth layer "
                "fronts this service", host)


def _resolve_data_dir() -> str:
    """Where the SQLite DB + Fernet key live. Resolution order:
      1. MA_DATA_DIR if set (explicit operator choice).
      2. a legacy ./data in the CURRENT directory if it already exists — so an
         existing install started from its usual cwd keeps its data.
      3. otherwise a STABLE per-user path ($XDG_DATA_HOME or ~/.local/share)/
         migration-auditor — independent of the cwd the process happens to start
         in. A cwd-relative default silently created a fresh empty DB + a new
         encryption key when the service was started from a different directory,
         orphaning prior runs and making previously-stored secrets undecryptable.
    """
    explicit = os.environ.get("MA_DATA_DIR")
    if explicit:
        return explicit
    legacy = os.path.join(os.getcwd(), "data")
    if os.path.isdir(legacy):
        return legacy
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "migration-auditor")


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
    data_dir = _resolve_data_dir()
    # Always log WHERE state lives — a wrong/unexpected data dir silently loses
    # prior runs and orphans the encryption key, so make the location visible.
    log.info("data directory: %s", data_dir)
    # IPv6 binds keep their brackets in bind_host ([::1]); consumers strip if needed.
    bind = os.environ.get("MA_BIND", "127.0.0.1:8484")
    host, sep, port = bind.rpartition(":")
    if not sep:
        raise ValueError(f"MA_BIND must be host:port, got {bind!r}")
    host = (host or "127.0.0.1").strip()
    # uvicorn wants a BARE host; an IPv6 bind is written [::1]:port, so strip the
    # brackets here ([::1] -> ::1) or uvicorn's getaddrinfo raises at bind time.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    # NB: the non-loopback bind guard is assert_safe_bind(), enforced only on the
    # serving path — load_config stays pure so non-serving CLI (backup) works
    # even on a host configured with a public MA_BIND.
    public = os.environ.get("MA_PUBLIC_BASE_URL", "http://localhost:8484")
    return Config(
        data_dir=data_dir,
        bind_host=host,
        bind_port=int(port),
        public_base_url=public.rstrip("/"),
        secret_key=os.environ.get("MA_SECRET_KEY") or None,
    )
