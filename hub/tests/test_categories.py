"""Tests for DT-14 (DT-42 adds AI categorization): app and website categories."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daytrace_hub.api.events import store_events
from daytrace_hub.auth import register_device
from daytrace_hub.categories import (
    CATEGORIES,
    Categorizer,
    Override,
    domain_suffixes,
    load_builtin,
    parse_builtin,
    save_override,
)
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event

PHONE = ("192.168.1.50", 40000)


# --- the built-in list ----------------------------------------------------------------------------------------


def test_the_built_in_list_covers_every_category_and_about_150_apps() -> None:
    raw = json.loads(resources.files("daytrace_hub").joinpath("data", "categories.json").read_text(encoding="utf-8"))
    assert {key for key in raw if not key.startswith("$")} == set(CATEGORIES)
    apps = sum(len(raw[category]["apps"]) for category in CATEGORIES)
    assert apps >= 150
    assert load_builtin().size > 450  # names, ids and domains together


def test_a_key_in_two_categories_is_refused() -> None:
    with pytest.raises(ValueError, match="listed as social and video"):
        parse_builtin({"social": {"apps": ["YouTube"]}, "video": {"apps": ["youtube"]}})


def test_unknown_categories_and_groups_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown category"):
        parse_builtin({"music": {"apps": ["Spotify"]}})
    with pytest.raises(ValueError, match="apps, ids or domains"):
        parse_builtin({"social": {"packages": ["x"]}})


@pytest.mark.parametrize(
    ("app", "app_id", "kind", "expected"),
    [
        ("Instagram", None, "app", "social"),  # iPhone Shortcut app name
        (None, "com.instagram.android", "app", "social"),  # Android package
        ("Instagram", "com.burbn.instagram", "app", "social"),  # iOS bundle id
        ("Code.exe", None, "app", "work"),  # Windows exe
        ("code", None, "app", "work"),
        ("Xcode", "com.apple.dt.Xcode", "app", "work"),  # macOS, case does not matter
        ("WhatsApp", "com.whatsapp", "app", "comms"),
        ("Duolingo", None, "app", "study"),
        ("RobloxPlayerBeta.exe", None, "app", "games"),
        ("Samsung Health", "com.sec.android.app.shealth", "app", "health"),
        ("YouTube", "com.google.android.youtube", "app", "video"),
        ("Chrome", "com.android.chrome", "app", "other"),
        ("youtube.com", None, "web", "video"),
        ("m.youtube.com", None, "web", "video"),  # subdomains match
        ("www.YouTube.com", None, "web", "video"),
        ("music.youtube.com", None, "web", "other"),  # the longest match wins
        ("mail.google.com", None, "web", "comms"),
        ("docs.google.com", None, "web", "work"),
        ("google.com", None, "web", "other"),
        ("my-school.instructure.com", None, "web", "study"),
        ("some-new-app", None, "app", "other"),
    ],
)
def test_common_apps_and_sites(app: str | None, app_id: str | None, kind: str, expected: str) -> None:
    assert Categorizer().category(app, app_id, kind) == expected


def test_where_a_category_came_from() -> None:
    categorizer = Categorizer({"obsidian": Override("study", "user", "2026-09-25T00:00:00.000000Z")})
    assert categorizer.lookup("Obsidian") == ("study", "user")
    assert categorizer.lookup("Instagram") == ("social", "builtin")
    assert categorizer.lookup("Brand New App", collector="games") == ("games", "collector")
    assert categorizer.lookup("Brand New App", collector="not-a-category") == ("other", "default")
    assert categorizer.lookup(None) == ("other", "default")


def test_an_override_beats_the_built_in_list_and_the_id_beats_the_name() -> None:
    categorizer = Categorizer({
        "youtube.com": Override("study", "user", "x"),
        "com.google.android.youtube": Override("study", "user", "x"),
    })
    assert categorizer.category("m.youtube.com", kind="web") == "study"
    assert categorizer.category("YouTube", "com.google.android.youtube") == "study"
    assert categorizer.category("YouTube", "com.google.ios.youtube") == "video"  # other ids are untouched


def test_domain_suffixes() -> None:
    assert domain_suffixes("a.b.example.co.uk") == ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk", "co.uk"]
    assert domain_suffixes("localhost") == ["localhost"]
    assert domain_suffixes("WWW.Example.com.") == ["example.com"]


def test_the_ai_never_replaces_the_users_choice(db: Database) -> None:
    with db.connect() as conn, transaction(conn):
        assert save_override(conn, "obsidian", "study", "ai").source == "ai"
        assert save_override(conn, "obsidian", "work", "user").category == "work"
        kept = save_override(conn, "obsidian", "games", "ai")
    assert (kept.category, kept.source) == ("work", "user")


# --- the API --------------------------------------------------------------------------------------------------


def add(db: Database, device_id: str, device_type: str, events: list[dict[str, Any]]) -> str:
    with db.connect() as conn:
        token = register_device(conn, device_id=device_id, name=device_id, device_type=device_type)
        with transaction(conn):
            store_events(conn, device_id, [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)])
    return token


def recent(minutes_ago: int, length: int = 5) -> tuple[str, str]:
    start = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return start.isoformat(), (start + timedelta(minutes=length)).isoformat()


def seed(db: Database) -> str:
    events = []
    for n, (app, app_id) in enumerate([("Instagram", "com.instagram.android"), ("Instagram", "com.instagram.android"),
                                       ("Obsidian", "md.obsidian"), ("Mystery", None)]):
        start, end = recent(60 * (n + 1))
        events.append({"kind": "app_session", "source": "usagestats", "start": start, "end": end, "app": app,
                       "seq": n, **({"app_id": app_id} if app_id else {})})
    return add(db, "android-1", "android", events)


def test_the_list_shows_apps_seen_recently_with_their_category(client: TestClient, db: Database) -> None:
    seed(db)
    body = client.get("/api/v1/categories").json()
    assert body["categories"] == list(CATEGORIES)
    apps = {a["app"]: a for a in body["apps"]}
    assert apps["Instagram"]["events"] == 2  # most used first
    assert body["apps"][0]["app"] == "Instagram"
    assert (apps["Instagram"]["category"], apps["Instagram"]["source"], apps["Instagram"]["key"]) == (
        "social", "builtin", "com.instagram.android")
    assert (apps["Mystery"]["category"], apps["Mystery"]["source"], apps["Mystery"]["key"]) == ("other", "default", "mystery")
    assert body["overrides"] == []


def test_old_apps_are_not_listed(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [{"kind": "app_session", "source": "usagestats", "seq": 1, "app": "Ancient",
                                      "start": "2020-01-01T10:00:00Z", "end": "2020-01-01T10:05:00Z"}])
    assert client.get("/api/v1/categories").json()["apps"] == []


def test_websites_are_listed_by_domain(client: TestClient, db: Database) -> None:
    start, end = recent(30)
    add(db, "browser-1", "browser", [{"kind": "web", "source": "browser", "start": start, "end": end,
                                      "data": {"domain": "m.youtube.com"}, "app_id": "msedge"}])
    app = client.get("/api/v1/categories").json()["apps"][0]
    assert (app["app"], app["kind"], app["key"], app["category"], app["app_id"]) == (
        "m.youtube.com", "web", "m.youtube.com", "video", None)


def test_a_users_choice_is_saved_used_and_can_be_undone(client: TestClient, db: Database) -> None:
    seed(db)
    response = client.put("/api/v1/categories/md.obsidian", json={"category": "study"})
    assert response.status_code == 200
    assert response.json()["key"] == "md.obsidian"
    assert (response.json()["category"], response.json()["source"]) == ("study", "user")
    apps = {a["app"]: a for a in client.get("/api/v1/categories").json()["apps"]}
    assert (apps["Obsidian"]["category"], apps["Obsidian"]["source"]) == ("study", "user")
    assert client.delete("/api/v1/categories/md.obsidian").status_code == 204
    apps = {a["app"]: a for a in client.get("/api/v1/categories").json()["apps"]}
    assert (apps["Obsidian"]["category"], apps["Obsidian"]["source"]) == ("work", "builtin")
    assert client.delete("/api/v1/categories/md.obsidian").status_code == 404


def test_keys_are_matched_without_case_or_exe(client: TestClient) -> None:
    assert client.put("/api/v1/categories/Notepad.EXE", json={"category": "study"}).json()["key"] == "notepad"


@pytest.mark.parametrize("body", [{"category": "fun"}, {"category": "study", "extra": 1}, {}])
def test_bad_category_choices_get_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.put("/api/v1/categories/code", json=body).status_code == 422


def test_the_timeline_uses_the_categories(client: TestClient, db: Database) -> None:
    seed(db)  # events 1 to 4 hours ago, which may be yesterday in UTC
    client.put("/api/v1/categories/mystery", json={"category": "games"})
    today = datetime.now(UTC).date()
    by_app: dict[str, str] = {}
    for day in (today - timedelta(days=1), today):
        body = client.get("/api/v1/timeline", params={"date": day.isoformat(), "tz": "UTC"}).json()
        by_app.update({s["app"]: s["category"] for lane in body["lanes"] for s in lane["sessions"]})
    assert by_app["Instagram"] == "social"
    assert by_app["Mystery"] == "games"
    assert by_app["Obsidian"] == "work"


def test_phones_need_a_token_to_read_or_change_categories(client: TestClient, db: Database) -> None:
    token = seed(db)
    with TestClient(client.app, client=PHONE) as phone:
        assert phone.get("/api/v1/categories").status_code == 401
        assert phone.put("/api/v1/categories/code", json={"category": "work"}).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert phone.get("/api/v1/categories", headers=headers).status_code == 200
        assert phone.put("/api/v1/categories/code", json={"category": "study"}, headers=headers).status_code == 200
