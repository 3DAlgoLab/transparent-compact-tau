"""Shared test setup: keep tests out of the developer's home and drive async tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest_plugins = ("anyio",)


@pytest.fixture
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio, matching Tau's own suite."""
    return "asyncio"


@pytest.fixture(autouse=True)
def prevent_user_home_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point home-directory lookups at the pytest temp directory.

    Tau resolves `~/.tau` for settings, trust, and session storage; without this
    a session test could read or write the developer's real configuration.
    """
    monkeypatch.delenv("TAU_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield
