"""Tests for DT-10: profiles, data folders, and which networks each profile accepts."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daytrace_hub import __main__ as cli
from daytrace_hub.app import create_app
from daytrace_hub.config import (
    PROFILE_SETTINGS,
    PROFILES,
    Settings,
    client_allowed,
    default_data_dir,
    get_profile,
    load_settings,
)


def test_every_profile_has_settings_and_its_own_port() -> None:
    assert tuple(PROFILE_SETTINGS) == PROFILES
    ports = [p.port for p in PROFILE_SETTINGS.values()]
    assert len(set(ports)) == len(ports)
    assert get_profile("personal").port == 8765
    assert get_profile("shared-dev").port == 8766
    assert get_profile("demo").port == 8767


def test_only_shared_dev_is_reachable_over_tailscale() -> None:
    assert {name for name, p in PROFILE_SETTINGS.items() if p.allow_tailscale} == {"shared-dev"}


def test_unknown_profile_names_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("production")


@pytest.mark.parametrize(
    ("host", "personal", "shared_dev"),
    [
        ("127.0.0.1", True, True),
        ("::1", True, True),
        ("192.168.1.23", True, True),
        ("10.0.0.5", True, True),
        ("172.20.1.1", True, True),
        ("172.32.0.1", False, False),  # just outside 172.16.0.0/12
        ("::ffff:192.168.1.5", True, True),  # IPv4-mapped IPv6
        ("fe80::1%eth0", True, True),  # link-local with a zone
        ("fd12:3456::1", True, True),  # unique local IPv6
        ("100.101.102.103", False, True),  # Tailscale IPv4
        ("fd7a:115c:a1e0::1", False, True),  # Tailscale IPv6
        ("8.8.8.8", False, False),
        ("2001:4860:4860::8888", False, False),
        ("testclient", False, False),
        ("", False, False),
        (None, False, False),
    ],
)
def test_client_networks(host: str | None, personal: bool, shared_dev: bool) -> None:
    assert client_allowed(host, get_profile("personal")) is personal
    assert client_allowed(host, get_profile("shared-dev")) is shared_dev


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("win32", {"LOCALAPPDATA": r"C:\Users\a\AppData\Local"}, Path(r"C:\Users\a\AppData\Local") / "Daytrace"),
        ("darwin", {"HOME": "/Users/a"}, Path("/Users/a/Library/Application Support/Daytrace")),
        ("linux", {"HOME": "/home/a"}, Path("/home/a/.local/share/daytrace")),
        ("linux", {"HOME": "/home/a", "XDG_DATA_HOME": "/data"}, Path("/data/daytrace")),
    ],
)
def test_default_data_dir(platform: str, env: dict[str, str], expected: Path) -> None:
    assert default_data_dir(env, platform) == expected


def test_data_dir_comes_from_the_environment(tmp_path: Path) -> None:
    settings = load_settings("demo", env={"DAYTRACE_DATA_DIR": f"  {tmp_path}  "})
    assert settings.data_dir == tmp_path
    assert settings.database_path == tmp_path / "daytrace-demo.db"


def test_each_profile_has_its_own_database_file(tmp_path: Path) -> None:
    paths = {load_settings(name, env={"DAYTRACE_DATA_DIR": str(tmp_path)}).database_path for name in PROFILES}
    assert len(paths) == len(PROFILES)


# --- the app enforces the network rules -------------------------------------------------------------------


def _get(settings: Settings, host: str) -> int:
    with TestClient(create_app(settings), client=(host, 50000)) as test_client:
        return test_client.get("/openapi.json").status_code


def test_personal_refuses_tailscale_but_serves_the_home_network(settings: Settings) -> None:
    assert _get(settings, "192.168.1.40") == 200
    assert _get(settings, "100.101.102.103") == 403
    assert _get(settings, "8.8.8.8") == 403


def test_shared_dev_accepts_tailscale(tmp_path: Path) -> None:
    shared = Settings(profile=get_profile("shared-dev"), data_dir=tmp_path)
    assert _get(shared, "100.101.102.103") == 200


def test_refusals_use_the_documented_error_shape(settings: Settings) -> None:
    with TestClient(create_app(settings), client=("100.64.0.9", 1)) as test_client:
        response = test_client.get("/openapi.json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_network"


def test_starting_the_app_creates_and_migrates_the_database(settings: Settings) -> None:
    assert not settings.database_path.exists()
    with TestClient(create_app(settings), client=("127.0.0.1", 1)):
        pass
    assert settings.database_path.exists()


def test_two_profiles_can_run_side_by_side(tmp_path: Path) -> None:
    apps = [create_app(Settings(profile=get_profile(name), data_dir=tmp_path)) for name in ("personal", "demo")]
    with TestClient(apps[0], client=("127.0.0.1", 1)) as first, TestClient(apps[1], client=("127.0.0.1", 1)) as second:
        assert first.get("/openapi.json").status_code == 200
        assert second.get("/openapi.json").status_code == 200
    assert (tmp_path / "daytrace-personal.db").exists()
    assert (tmp_path / "daytrace-demo.db").exists()


def test_run_starts_the_chosen_profile_on_its_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("DAYTRACE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    assert cli.main(["run", "--profile", "shared-dev"]) == 0
    assert calls[0]["port"] == 8766
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["app"].state.settings.database_path == tmp_path / "daytrace-shared-dev.db"
