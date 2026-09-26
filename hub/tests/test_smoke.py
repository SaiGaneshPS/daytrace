"""DT-1 smoke test: the package imports and the skeleton entry points work. DT-30: the hub serves the dashboard."""
from __future__ import annotations

import importlib
import importlib.metadata
import pkgutil
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import daytrace_hub
from daytrace_hub import __version__
from daytrace_hub.__main__ import main
from daytrace_hub.app import DASHBOARD_CSP, IMMUTABLE, create_app
from daytrace_hub.config import Settings, load_settings

# Modules that import OS-only packages (pywin32 / pyobjc are installed only on their own platform).
PLATFORM_ONLY = {
    "daytrace_hub.tracker.windows": "win32",
    "daytrace_hub.tracker.macos": "darwin",
}


def test_version_matches_package_metadata() -> None:
    assert __version__ == importlib.metadata.version("daytrace-hub")


def test_create_app() -> None:
    assert isinstance(create_app(), FastAPI)


def test_cli_without_command_prints_help(capsys) -> None:
    assert main([]) == 0
    assert "daytrace-hub" in capsys.readouterr().out


def test_every_module_imports() -> None:
    for module in pkgutil.walk_packages(daytrace_hub.__path__, prefix="daytrace_hub."):
        platform = PLATFORM_ONLY.get(module.name)
        if platform and sys.platform != platform:
            continue
        importlib.import_module(module.name)


# --- DT-30: the dashboard ----------------------------------------------------------------------------------------


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A stand-in for dashboard/dist after `npm run build`, with a file next to it that must never be served."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Daytrace</title><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('hi')", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('fetch', () => {})", encoding="utf-8")
    (dist / "workbox-1a2b3c.js").write_text("// workbox", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"name": "Daytrace"}', encoding="utf-8")
    (dist / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "secret.txt").write_text("not for the web", encoding="utf-8")
    return dist


def hub(settings: Settings, dist: Path) -> TestClient:
    return TestClient(create_app(settings, dashboard_dir=dist), client=("127.0.0.1", 50000), base_url="http://localhost:8765")


def test_every_page_of_the_dashboard_loads_and_reloads(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        for path in ("/", "/insights", "/devices", "/ask/anything/deeper"):
            page = client.get(path)
            assert page.status_code == 200 and "<div id=root>" in page.text, path
            assert page.headers["content-type"].startswith("text/html")
            assert page.headers["cache-control"] == "no-cache"  # a new build is picked up on the next load
            assert page.headers["content-security-policy"] == DASHBOARD_CSP
            assert page.headers["x-content-type-options"] == "nosniff"


def test_build_files_are_served_with_the_right_type_and_caching(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        script = client.get("/assets/index-abc123.js")
        assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
        assert script.headers["cache-control"] == IMMUTABLE  # named by its content
        assert client.get("/workbox-1a2b3c.js").headers["cache-control"] == IMMUTABLE
        worker = client.get("/sw.js")
        assert worker.headers["content-type"].startswith("text/javascript") and worker.headers["cache-control"] == "no-cache"
        assert client.get("/manifest.webmanifest").headers["content-type"] == "application/manifest+json"
        missing = client.get("/assets/index-old999.js")  # an old build's file: a 404, not the page posing as a script
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_the_api_keeps_its_own_answers(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        assert client.get("/openapi.json").json()["info"]["title"].startswith("Daytrace hub")
        for method in ("GET", "POST", "DELETE"):
            unknown = client.request(method, "/api/v1/no-such-thing")
            assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found", method
        wrong = client.post("/insights")
        assert wrong.status_code == 405 and wrong.json()["error"]["code"] == "method_not_allowed"
        assert client.put("/api/v1/health").status_code == 405  # a real API path keeps its own answer


def test_paths_never_leave_the_dashboard_folder(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        for path in ("/%2e%2e/secret.txt", "/..%5csecret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"):
            response = client.get(path)
            assert "not for the web" not in response.text, path
            assert response.status_code == 404, path


def test_before_the_first_build_the_hub_says_how_to_build(settings: Settings, tmp_path: Path) -> None:
    with hub(settings, tmp_path / "not-built") as client:
        page = client.get("/insights")
        assert page.status_code == 200 and "npm run build" in page.text
        assert client.get("/api/v1/health").status_code == 200


def test_no_path_reaches_another_machine_or_drive(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        started = time.monotonic()
        for path in ("/%5c%5c10.255.255.7%5cshare%5cx.js", "/%5c%5c10.255.255.7%5cshare", "/C:/Windows/win.ini",
                     "/c:%5cwindows%5cwin.ini", "/index.html::$DATA", "/%00.js"):
            response = client.get(path, headers={"sec-fetch-mode": "navigate"})
            assert response.status_code == 404, path
            assert response.headers["content-security-policy"] == DASHBOARD_CSP  # errors carry the headers too
        assert time.monotonic() - started < 2  # refused before the disk (or an SMB share) is touched


def test_an_unchanged_file_answers_304(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        first = client.get("/")
        again = client.get("/", headers={"if-none-match": first.headers["etag"]})
        assert again.status_code == 304 and again.content == b""
        assert client.get("/", headers={"if-modified-since": first.headers["last-modified"]}).status_code == 304
        assert client.get("/", headers={"if-none-match": '"something-else"'}).status_code == 200


def test_a_page_with_a_dot_reloads_but_a_missing_script_does_not(settings: Settings, built: Path) -> None:
    with hub(settings, built) as client:
        reload = client.get("/story/2026.09.25", headers={"sec-fetch-mode": "navigate", "accept": "text/html"})
        assert reload.status_code == 200 and "<div id=root>" in reload.text
        script = client.get("/story/2026.09.25", headers={"sec-fetch-mode": "no-cors", "accept": "*/*"})
        assert script.status_code == 404
        wrong = client.post("/insights")
        assert wrong.status_code == 405 and wrong.headers["content-security-policy"] == DASHBOARD_CSP
        assert client.get("/photo.jpg").headers["content-type"] == "image/jpeg"


def test_behind_a_proxy_path_the_api_is_still_the_api(settings: Settings, built: Path) -> None:
    app = create_app(settings, dashboard_dir=built)
    with TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:8765", root_path="/daytrace") as client:
        assert client.get("/daytrace/api/v1/health").json()["status"] == "ok"
        assert client.get("/daytrace/api/v1/nope").json()["error"]["code"] == "not_found"
        assert "<div id=root>" in client.get("/daytrace/insights").text


def test_a_route_added_later_still_comes_first(settings: Settings, built: Path) -> None:
    app = create_app(settings, dashboard_dir=built)

    @app.get("/later")
    def later() -> dict[str, str]:
        return {"route": "later"}

    with TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:8765") as client:
        assert client.get("/later").json() == {"route": "later"}


def test_the_dashboard_folder_setting(tmp_path: Path) -> None:
    assert load_settings(env={}).dashboard_dir is None  # dashboard/dist next to the hub
    assert load_settings(env={"DAYTRACE_DASHBOARD_DIR": f"  {tmp_path}  "}).dashboard_dir == tmp_path
    assert load_settings(env={"DAYTRACE_DASHBOARD_DIR": "~/dist"}).dashboard_dir == Path("~/dist").expanduser()
    with pytest.raises(ValueError, match="DAYTRACE_DASHBOARD_DIR must be an absolute path"):
        load_settings(env={"DAYTRACE_DASHBOARD_DIR": "dist"})
