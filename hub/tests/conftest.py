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


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own data folder, so no test can ever touch real hub data."""
    data_dir = tmp_path / "daytrace-data"
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(data_dir))
    return data_dir


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
    """A test client that looks like a request from the hub computer itself."""
    with TestClient(create_app(settings), client=LOCAL_CLIENT) as test_client:
        yield test_client
