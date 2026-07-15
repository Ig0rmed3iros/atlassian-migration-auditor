from webapp.config import load_config


def test_defaults(tmp_path, monkeypatch):
    for v in ("MA_DATA_DIR", "MA_BIND", "MA_PUBLIC_BASE_URL", "MA_SECRET_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)                      # no legacy ./data here
    cfg = load_config()
    # New install -> a STABLE per-user path, NOT cwd/data (review: cwd-relative
    # default silently orphaned prior runs + the encryption key).
    assert cfg.data_dir == str(tmp_path / "xdg" / "migration-auditor")
    assert cfg.bind_host == "127.0.0.1" and cfg.bind_port == 8484
    assert cfg.public_base_url == "http://localhost:8484"
    assert cfg.oauth_redirect_uri == "http://localhost:8484/oauth/callback"
    assert cfg.secret_key is None


def test_legacy_cwd_data_dir_is_preserved(tmp_path, monkeypatch):
    # An existing install whose data lives in ./data must keep using it.
    monkeypatch.delenv("MA_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    assert load_config().data_dir == str(tmp_path / "data")


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


def test_bind_without_port_raises_clear_error(monkeypatch):
    import pytest
    monkeypatch.setenv("MA_BIND", "0.0.0.0")
    with pytest.raises(ValueError, match="MA_BIND must be host:port"):
        load_config()


# --- fail-closed non-loopback bind (the app has NO auth) --------------------
# load_config stays pure (so `backup` works on a public-bound host); the guard
# is assert_safe_bind(), enforced only on the serving path.

def test_non_loopback_bind_refused_without_optin(monkeypatch):
    import pytest
    from webapp.config import assert_safe_bind
    monkeypatch.delenv("MA_ALLOW_PUBLIC_BIND", raising=False)
    for host in ("0.0.0.0", "::", "192.168.1.5", "0", "127.0.0.1.evil.com"):
        with pytest.raises(ValueError, match="no authentication|non-loopback"):
            assert_safe_bind(host)


def test_non_loopback_bind_allowed_with_optin(monkeypatch):
    from webapp.config import assert_safe_bind
    monkeypatch.setenv("MA_ALLOW_PUBLIC_BIND", "true")
    assert_safe_bind("192.168.1.5")              # must not raise


def test_loopback_variants_allowed(monkeypatch):
    from webapp.config import assert_safe_bind
    monkeypatch.delenv("MA_ALLOW_PUBLIC_BIND", raising=False)
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "127.0.0.5"):
        assert_safe_bind(host)                   # loopback: never raises


def test_ipv6_bracket_bind_host_is_stripped_for_uvicorn(monkeypatch, tmp_path):
    # [::1]:8484 must yield a BARE host uvicorn can bind (brackets crash it).
    monkeypatch.setenv("MA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("MA_BIND", "[::1]:8484")
    assert load_config().bind_host == "::1"
