"""Tests for DT-10: profiles, data folders, and which networks each profile accepts."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from daytrace_hub import __main__ as cli
from daytrace_hub.app import create_app
from daytrace_hub.config import (
    PROFILE_SETTINGS,
    PROFILES,
    Settings,
    client_allowed,
    default_data_dir,
    get_profile,
    host_allowed,
    load_settings,
    parse_lan_networks,
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
        ("169.254.10.20", True, True),  # IPv4 link-local, like fe80::
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
        # WSL can forward the Windows USERPROFILE; on Linux and macOS HOME must win.
        ("linux", {"HOME": "/home/a", "USERPROFILE": "/mnt/c/Users/a"}, Path("/home/a/.local/share/daytrace")),
        ("darwin", {"HOME": "/Users/a", "USERPROFILE": "/x"}, Path("/Users/a/Library/Application Support/Daytrace")),
    ],
)
def test_default_data_dir(platform: str, env: dict[str, str], expected: Path) -> None:
    assert default_data_dir(env, platform) == expected


def test_data_dir_comes_from_the_environment(tmp_path: Path) -> None:
    settings = load_settings("demo", env={"DAYTRACE_DATA_DIR": f"  {tmp_path}  "})
    assert settings.data_dir == tmp_path
    assert settings.database_path == tmp_path / "daytrace-demo.db"


def test_data_dir_expands_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DT_TEST_BASE", str(tmp_path))
    settings = load_settings("demo", env={"DAYTRACE_DATA_DIR": "${DT_TEST_BASE}/hub-data"})
    assert settings.data_dir == tmp_path / "hub-data"


@pytest.mark.parametrize("value", ["data", "./data", "..\\data"])
def test_a_relative_data_dir_is_refused(value: str) -> None:
    # It would follow the current folder and quietly start a new, empty database somewhere else.
    with pytest.raises(ValueError, match="absolute path"):
        load_settings("personal", env={"DAYTRACE_DATA_DIR": value})


# --- narrowing the LAN on Wi-Fi you do not trust ----------------------------------------------------------


def test_lan_networks_narrow_who_may_connect(tmp_path: Path) -> None:
    settings = load_settings(
        "personal", env={"DAYTRACE_DATA_DIR": str(tmp_path), "DAYTRACE_LAN_NETWORKS": "192.168.1.0/24, fd00:1::/64"}
    )
    personal = settings.profile
    assert client_allowed("192.168.1.40", personal, settings.lan_networks)
    assert client_allowed("fd00:1::5", personal, settings.lan_networks)
    assert client_allowed("127.0.0.1", personal, settings.lan_networks)  # the hub computer itself, always
    assert not client_allowed("192.168.2.40", personal, settings.lan_networks)
    assert not client_allowed("10.0.0.5", personal, settings.lan_networks)


def test_lan_networks_do_not_open_tailscale_on_personal() -> None:
    assert not client_allowed("100.101.102.103", get_profile("personal"), parse_lan_networks("10.0.0.0/8"))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("0.0.0.0/0", "not a private LAN range"),
        ("8.8.8.0/24", "not a private LAN range"),
        ("100.64.0.0/10", "not a private LAN range"),  # Tailscale is controlled by the profile, not this list
        ("192.168.1.0/33", "not a network"),
        ("home", "not a network"),
        (" , ", "lists no networks"),
    ],
)
def test_bad_lan_networks_are_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_lan_networks(text)


# --- Host header (DNS rebinding) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "personal", "shared_dev"),
    [
        (None, True, True),
        ("localhost:8765", True, True),
        ("127.0.0.1:8765", True, True),
        ("192.168.1.23:8765", True, True),
        ("[::1]:8765", True, True),
        ("[fe80::1]", True, True),
        ("DESKTOP-SAI:8765", True, True),  # a PC name
        ("daytrace-hub.local:8765", True, True),
        ("daytrace-hub.local.:8765", True, True),
        ("sai-pc.lan", True, True),
        ("sai-pc.home.arpa", True, True),
        ("sai-pc.tail1234.ts.net:8766", False, True),  # MagicDNS, only where Tailscale is allowed
        ("evil.example:8765", False, False),
        ("127.0.0.1.nip.io:8765", False, False),
        ("abcd-8765.usw2.devtunnels.ms", False, False),  # a forwarded tunnel URL
        ("[not-an-ip]:8765", False, False),
        (":8765", False, False),
    ],
)
def test_host_names(host: str | None, personal: bool, shared_dev: bool) -> None:
    assert host_allowed(host, get_profile("personal")) is personal
    assert host_allowed(host, get_profile("shared-dev")) is shared_dev


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
    assert response.json() == {
        "error": {
            "code": "forbidden_network",
            "message": "the personal profile does not accept requests from this network",
            "details": [],
        }
    }


def test_a_rebinding_host_name_is_refused_even_from_this_computer(settings: Settings) -> None:
    with TestClient(create_app(settings), client=("127.0.0.1", 1)) as test_client:
        response = test_client.get("/openapi.json", headers={"host": "evil.example:8765"})
        assert test_client.get("/openapi.json", headers={"host": "localhost:8765"}).status_code == 200
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_host"


def _app_with_websocket(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.websocket("/ws-test")
    async def ws_test(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    return app


def test_websockets_from_the_lan_are_accepted(settings: Settings) -> None:
    app = _app_with_websocket(settings)
    with TestClient(app, client=("192.168.1.40", 1)) as local, local.websocket_connect("/ws-test") as ws:
        assert ws.receive_text() == "hello"


@pytest.mark.parametrize(
    ("client", "headers"),
    [
        (("8.8.8.8", 1), {}),
        (("100.101.102.103", 1), {}),  # Tailscale on personal
        (("127.0.0.1", 1), {"host": "evil.example"}),
    ],
)
def test_websockets_follow_the_same_network_rules(
    settings: Settings, client: tuple[str, int], headers: dict[str, str]
) -> None:
    app = _app_with_websocket(settings)
    with (
        TestClient(app, client=client) as outside,
        pytest.raises(WebSocketDisconnect),
        outside.websocket_connect("/ws-test", headers=headers),
    ):
        pass


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
