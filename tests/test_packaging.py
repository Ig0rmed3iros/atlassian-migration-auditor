"""Packaging integrity: a `pip install` must ship every importable package.

The setuptools `packages` list is explicit, so a new subpackage that is not
added there is silently excluded from the wheel — the dev tree imports fine but
the installed app raises ImportError on first use (no-bias review: auditor.
envaudit + auditor.remediation were both missing)."""
from __future__ import annotations

import pathlib
import tomllib


_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _declared_packages() -> set[str]:
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return set(cfg["tool"]["setuptools"]["packages"])


def _actual_packages() -> set[str]:
    out: set[str] = set()
    for top in ("auditor", "webapp"):
        for init in (_ROOT / top).rglob("__init__.py"):
            rel = init.parent.relative_to(_ROOT)
            out.add(".".join(rel.parts))
    return out


def test_pyproject_declares_every_importable_subpackage():
    declared, actual = _declared_packages(), _actual_packages()
    missing = actual - declared
    assert not missing, f"pyproject [tool.setuptools].packages omits: {sorted(missing)}"
