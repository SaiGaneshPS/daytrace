"""Shared fixtures: a temporary data folder and database (DT-10). DT-37 adds a fake LLM server."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daytrace_hub.app import create_app
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database

LOCAL_CLIENT = ("127.0.0.1", 50000)
LOCAL_URL = "http://localhost:8765"  # the hub computer's own dashboard address (trusted as local)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own data folder, so no test can ever touch real hub data."""
    data_dir = tmp_path / "daytrace-data"
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DAYTRACE_MDNS", "off")  # never announce anything on the real network from a test
    return data_dir


@pytest.fixture(autouse=True)
def _no_real_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to pass a fake zeroconf fails loudly instead of using the network."""
    from daytrace_hub import discovery

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("tests must not start a real zeroconf; pass zeroconf_factory=FakeZeroconf")

    monkeypatch.setattr(discovery, "AsyncZeroconf", refuse)
    original_init = discovery.Advertiser.__init__

    def guarded_init(self: object, settings: object, zeroconf_factory: object = refuse, **kwargs: object) -> None:
        original_init(self, settings, zeroconf_factory=zeroconf_factory, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(discovery.Advertiser, "__init__", guarded_init)


@pytest.fixture
def settings(_isolated_data_dir: Path) -> Settings:
    return Settings(profile=get_profile("personal"), data_dir=_isolated_data_dir)


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A test client that looks like the dashboard on the hub computer itself."""
    with TestClient(create_app(settings), client=LOCAL_CLIENT, base_url=LOCAL_URL) as test_client:
        yield test_client
