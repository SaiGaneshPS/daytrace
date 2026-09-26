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
    Lookup,
    Override,
    canonical_key,
    domain_suffixes,
    load_builtin,
    parse_builtin,
    save_override,
)
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event
from daytrace_hub.sessions import sessions_for

PHONE = ("192.168.1.50", 40000)
RLO = chr(0x202E)


def user(category: str) -> Override:
    return Override(category, "user", "2026-09-25T00:00:00.000000Z")


def ai(category: str) -> Override:
    return Override(category, "ai", "2026-09-25T00:00:00.000000Z")


# --- the built-in list ----------------------------------------------------------------------------------------


def test_the_built_in_list_covers_every_category_and_about_150_apps() -> None:
    raw = json.loads(resources.files("daytrace_hub").joinpath("data", "categories.json").read_text(encoding="utf-8"))
    assert {key for key in raw if not key.startswith("$")} == set(CATEGORIES)
    assert sum(len(raw[category]["apps"]) for category in CATEGORIES) >= 150
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
        ("Microsoft Word", "WINWORD.EXE", "app", "work"),  # Windows: exe name in app_id, as the schema says
        ("Microsoft Excel", "EXCEL.EXE", "app", "work"),
        ("IntelliJ IDEA", "idea64.exe", "app", "work"),
        ("Visual Studio Code", "Code.exe", "app", "work"),
        ("Microsoft Word", None, "app", "work"),  # the macOS app name
        ("Xcode", "com.apple.dt.Xcode", "app", "work"),  # case does not matter
        ("WhatsApp", "com.whatsapp", "app", "comms"),
        ("Duolingo", None, "app", "study"),
        ("Roblox", "RobloxPlayerBeta.exe", "app", "games"),
        ("Pokémon GO", None, "app", "games"),
        ("Samsung Health", "com.sec.android.app.shealth", "app", "health"),
        ("YouTube", "com.google.android.youtube", "app", "video"),
        ("Chrome", "com.android.chrome", "app", "other"),
        ("youtube.com", None, "web", "video"),
        ("m.youtube.com", None, "web", "video"),  # subdomains match
        ("www.YouTube.com", None, "web", "video"),
        ("youtube.com:443", None, "web", "video"),
        ("music.youtube.com", None, "web", "other"),  # the most specific entry wins
        ("mail.google.com", None, "web", "comms"),
        ("docs.google.com", None, "web", "work"),
        ("gemini.google.com", None, "web", "work"),
        ("google.com", None, "web", "other"),
        ("mail.proton.me", None, "web", "comms"),
        ("drive.proton.me", None, "web", "other"),  # not every Proton product is mail
        ("read.amazon.com", None, "web", "study"),
        ("my-school.instructure.com", None, "web", "study"),
        ("some-new-app", None, "app", "other"),
    ],
)
def test_common_apps_and_sites(app: str | None, app_id: str | None, kind: str, expected: str) -> None:
    assert Categorizer().category(app, app_id, kind) == expected


def test_the_lookup_order_for_apps() -> None:
    categorizer = Categorizer({"obsidian": user("study"), "mystery": ai("games"), "instagram": ai("work")})
    assert categorizer.lookup("Obsidian") == Lookup("study", "user", "obsidian")
    assert categorizer.lookup("Obsidian", collector="games") == Lookup("study", "user", "obsidian")  # user first
    assert categorizer.lookup("Instagram", collector="games") == Lookup("games", "collector")  # hint beats list
    assert categorizer.lookup("Instagram") == Lookup("social", "builtin")  # the list beats an AI guess
    assert categorizer.lookup("Mystery") == Lookup("games", "ai", "mystery")  # AI only for unknown apps
    assert categorizer.lookup("Brand New App", collector="not-a-category") == Lookup("other", "default")
    assert categorizer.lookup(None) == Lookup("other", "default")


def test_the_id_override_beats_the_name_override() -> None:
    categorizer = Categorizer({"com.google.android.youtube": user("study"), "youtube": user("games")})
    assert categorizer.lookup("YouTube", "com.google.android.youtube") == Lookup("study", "user", "com.google.android.youtube")
    assert categorizer.lookup("YouTube", "com.google.ios.youtube") == Lookup("games", "user", "youtube")


def test_a_parent_domain_override_does_not_beat_a_more_specific_entry() -> None:
    categorizer = Categorizer({"google.com": user("study")})
    assert categorizer.lookup("google.com", kind="web") == Lookup("study", "user", "google.com")
    assert categorizer.lookup("news.google.com", kind="web") == Lookup("study", "user", "google.com")
    assert categorizer.lookup("mail.google.com", kind="web") == Lookup("comms", "builtin")


def test_an_exact_domain_override_beats_everything() -> None:
    categorizer = Categorizer({"mail.google.com": user("work")})
    assert categorizer.lookup("mail.google.com", kind="web", collector="comms") == Lookup("work", "user", "mail.google.com")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("  Code.EXE ", "code"), ("www.YouTube.com.", "youtube.com"), ("youtube.com:8080", "youtube.com"),
        ("Pokémon GO", "pokémon go"), ("ＹｏｕＴｕｂｅ", "youtube"), ("com.instagram.android", "com.instagram.android"),
    ],
)
def test_canonical_keys(text: str, key: str) -> None:
    assert canonical_key(text) == key


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
        exists = conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        token = "" if exists else register_device(conn, device_id=device_id, name=device_id, device_type=device_type)
        with transaction(conn):
            store_events(conn, device_id, [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)])
    return token


def recent(minutes_ago: int, length: int = 5) -> tuple[str, str]:
    start = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return start.isoformat(), (start + timedelta(minutes=length)).isoformat()


def session(n: int, app: str | None, app_id: str | None = None, **extra: Any) -> dict[str, Any]:
    start, end = recent(60 * (n + 1))
    event = {"kind": "app_session", "source": "usagestats", "start": start, "end": end, "seq": n, **extra}
    if app is not None:
        event["app"] = app
    if app_id is not None:
        event["app_id"] = app_id
    return event


def seed(db: Database) -> str:
    return add(db, "android-1", "android", [
        session(0, "Instagram", "com.instagram.android"), session(1, "Instagram", "com.instagram.android"),
        session(2, "Obsidian", "md.obsidian"), session(3, "Mystery"),
    ])


def viewer(db: Database) -> str:
    with db.connect() as conn:
        return register_device(conn, device_id="viewer-1", name="Phone browser", device_type="viewer")


def test_the_list_shows_apps_seen_recently_with_their_category(client: TestClient, db: Database) -> None:
    seed(db)
    body = client.get("/api/v1/categories").json()
    assert body["categories"] == list(CATEGORIES)
    apps = {a["app"]: a for a in body["apps"]}
    assert body["apps"][0]["app"] == "Instagram"  # most used first
    assert apps["Instagram"] == {"key": "com.instagram.android", "app": "Instagram", "app_id": "com.instagram.android",
                                 "kind": "app", "category": "social", "source": "builtin", "override": None, "events": 2}
    assert (apps["Mystery"]["category"], apps["Mystery"]["source"], apps["Mystery"]["key"]) == ("other", "default", "mystery")
    assert body["overrides"] == []


def test_spellings_of_one_app_are_one_row(client: TestClient, db: Database) -> None:
    add(db, "windows-1", "windows", [
        session(0, "Code", "Code.exe", source="tracker", kind="window"),
        session(1, "Visual Studio Code", "code.exe", source="tracker", kind="window"),
    ])
    start, end = recent(30)
    add(db, "browser-1", "browser", [
        {"kind": "web", "source": "browser", "start": start, "end": end, "data": {"domain": "www.youtube.com"}},
        {"kind": "web", "source": "browser", "start": end, "end": recent(20)[1], "data": {"domain": "youtube.com"}},
    ])
    apps = client.get("/api/v1/categories").json()["apps"]
    assert sorted((a["key"], a["events"]) for a in apps) == [("code", 2), ("youtube.com", 2)]


def test_apps_with_only_an_id_are_listed_and_blank_names_are_not(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [session(0, None, "com.example.unknown"), session(1, "   ")])
    apps = client.get("/api/v1/categories").json()["apps"]
    assert [(a["key"], a["app"]) for a in apps] == [("com.example.unknown", "com.example.unknown")]


def test_the_latest_collector_hint_is_used(client: TestClient, db: Database) -> None:
    add(db, "android-1", "android", [session(3, "Mystery", category="games"), session(0, "Mystery", category="work")])
    app = client.get("/api/v1/categories").json()["apps"][0]
    assert (app["category"], app["source"]) == ("work", "collector")


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
    assert (response.json()["key"], response.json()["category"], response.json()["source"]) == ("md.obsidian", "study", "user")
    apps = {a["app"]: a for a in client.get("/api/v1/categories").json()["apps"]}
    assert (apps["Obsidian"]["category"], apps["Obsidian"]["source"], apps["Obsidian"]["override"]) == (
        "study", "user", "md.obsidian")
    assert client.delete(f"/api/v1/categories/{apps['Obsidian']['override']}").status_code == 204
    apps = {a["app"]: a for a in client.get("/api/v1/categories").json()["apps"]}
    assert (apps["Obsidian"]["category"], apps["Obsidian"]["source"]) == ("work", "builtin")
    assert client.delete("/api/v1/categories/md.obsidian").status_code == 404


def test_the_row_names_the_override_that_decided_so_it_can_be_removed(client: TestClient, db: Database) -> None:
    seed(db)
    client.put("/api/v1/categories/instagram", json={"category": "study"})  # saved under the name, not the id
    row = next(a for a in client.get("/api/v1/categories").json()["apps"] if a["app"] == "Instagram")
    assert (row["key"], row["override"], row["category"]) == ("com.instagram.android", "instagram", "study")
    assert client.delete(f"/api/v1/categories/{row['override']}").status_code == 204


def test_www_and_case_in_keys_reach_the_same_override(client: TestClient, db: Database) -> None:
    start, end = recent(30)
    add(db, "browser-1", "browser", [{"kind": "web", "source": "browser", "start": start, "end": end,
                                      "data": {"domain": "youtube.com"}}])
    assert client.put("/api/v1/categories/www.YouTube.com", json={"category": "study"}).json()["key"] == "youtube.com"
    assert client.get("/api/v1/categories").json()["apps"][0]["category"] == "study"
    assert client.delete("/api/v1/categories/WWW.youtube.com").status_code == 204


def test_keys_are_matched_without_case_or_exe_and_may_contain_slashes(client: TestClient) -> None:
    assert client.put("/api/v1/categories/Notepad.EXE", json={"category": "study"}).json()["key"] == "notepad"
    assert client.put("/api/v1/categories/AC/DC Player", json={"category": "games"}).json()["key"] == "ac/dc player"


@pytest.mark.parametrize("key", [f"evil{RLO}gnp", "x" * 201, "   "])
def test_unreadable_keys_are_refused(client: TestClient, key: str) -> None:
    assert client.put(f"/api/v1/categories/{key}", json={"category": "study"}).status_code in (400, 404, 405)


@pytest.mark.parametrize("body", [{"category": "fun"}, {"category": "study", "extra": 1}, {}])
def test_bad_category_choices_get_422(client: TestClient, body: dict[str, Any]) -> None:
    assert client.put("/api/v1/categories/code", json=body).status_code == 422


def test_sessions_and_the_timeline_carry_the_resolved_category(client: TestClient, db: Database) -> None:
    seed(db)  # events 1 to 4 hours ago, which may be yesterday in UTC
    client.put("/api/v1/categories/mystery", json={"category": "games"})
    now = datetime.now(UTC)
    with db.connect() as conn:
        shared = {s.app: s.category for s in sessions_for(conn, now - timedelta(hours=6), now)}
    assert shared == {"Instagram": "social", "Obsidian": "work", "Mystery": "games"}
    by_app: dict[str, str] = {}
    for day in (now.date() - timedelta(days=1), now.date()):
        body = client.get("/api/v1/timeline", params={"date": day.isoformat(), "tz": "UTC"}).json()
        by_app.update({s["app"]: s["category"] for lane in body["lanes"] for s in lane["sessions"]})
    assert by_app == shared  # stats and the timeline agree


def test_only_the_dashboard_can_change_categories(client: TestClient, db: Database) -> None:
    collector = seed(db)
    viewer_token = viewer(db)
    with TestClient(client.app, client=PHONE) as phone:
        assert phone.get("/api/v1/categories").status_code == 401
        assert phone.put("/api/v1/categories/code", json={"category": "work"}).status_code == 401
        as_collector = {"Authorization": f"Bearer {collector}"}
        assert phone.get("/api/v1/categories", headers=as_collector).status_code == 200  # reading is fine
        refused = phone.put("/api/v1/categories/code", json={"category": "study"}, headers=as_collector)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "forbidden"
        assert phone.delete("/api/v1/categories/code", headers=as_collector).status_code == 403
        as_viewer = {"Authorization": f"Bearer {viewer_token}"}
        assert phone.put("/api/v1/categories/code", json={"category": "study"}, headers=as_viewer).status_code == 200
