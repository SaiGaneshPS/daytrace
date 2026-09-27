"""Tests for DT-14 (DT-42 adds AI categorization): app and website categories."""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from conftest import LOCAL_CLIENT, LOCAL_URL, FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub import redaction
from daytrace_hub.api.events import store_events
from daytrace_hub.app import create_app
from daytrace_hub.auth import register_device
from daytrace_hub.categories import (
    AI_MEANINGS,
    AI_SKIP_SECONDS,
    AI_WINDOW_DAYS,
    CATEGORIES,
    AiCategorizer,
    Categorizer,
    Lookup,
    Override,
    UnknownApp,
    ask_categories,
    canonical_key,
    categorize_unknown,
    domain_suffixes,
    load_builtin,
    load_declined,
    load_overrides,
    parse_builtin,
    save_override,
    unknown_apps,
)
from daytrace_hub.config import Settings, load_settings
from daytrace_hub.db import Database, transaction
from daytrace_hub.llm import LLMBadAnswer, LLMError, json_in
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


# --- DT-42: the local model sorts apps nothing knows ------------------------------------------------------------


def web(n: int, domain: str) -> dict[str, Any]:
    start, end = recent(60 * (n + 1))
    return {"kind": "web", "source": "browser", "start": start, "end": end, "seq": n, "data": {"domain": domain},
            "app_id": "msedge"}


def answer(fake: FakeModelServer, *categories: str) -> None:
    fake.reply_text(json.dumps({"apps": [{"n": n, "category": c} for n, c in enumerate(categories, start=1)]}))


def test_unknown_apps_are_the_ones_nothing_knows_most_used_first(db: Database) -> None:
    add(db, "android-1", "android", [
        session(0, "Instagram", "com.instagram.android"),  # built in
        session(1, "Mystery"), session(2, "Mystery"),  # used twice: first
        session(3, "Hinted", category="games"),  # the collector said
        session(4, "Chosen"), session(5, "Guessed"),
        session(6, None, "com.example.onlyid"),
        session(7, "[redacted]"),
    ])
    add(db, "browser-1", "browser", [web(0, "news.example.org"), web(1, "m.youtube.com"), web(2, "news.example.org")])
    with db.connect() as conn, transaction(conn):
        save_override(conn, "chosen", "work", "user")
        save_override(conn, "guessed", "study", "ai")
    with db.connect() as conn:
        found = unknown_apps(conn)
    assert {app.key for app in found} == {"mystery", "com.example.onlyid", "news.example.org"}
    assert found[0].key in ("mystery", "news.example.org")  # used twice, the others once
    assert UnknownApp(key="mystery", web=False, name="Mystery", app_id=None) in found
    assert UnknownApp(key="news.example.org", web=True, name="news.example.org", app_id=None) in found
    assert UnknownApp(key="com.example.onlyid", web=False, name=None, app_id="com.example.onlyid") == found[-1]


def test_names_the_privacy_rules_hide_are_never_asked_about(db: Database) -> None:
    add(db, "android-1", "android", [session(0, "Acme Planner"), session(1, "Mystery")])
    add(db, "browser-1", "browser", [web(0, "acme.example.com")])
    with db.connect() as conn, transaction(conn):
        redaction.save_choices(conn, redaction.check_choices([], [("Work", ["Acme"])]))
    with db.connect() as conn:
        assert [app.key for app in unknown_apps(conn)] == ["mystery"]


def test_the_model_sorts_them_and_each_is_asked_about_once(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery"), session(1, "Mystery"), session(2, "Zork"),
                                     session(3, None, "com.example.flash")])
    answer(fake_llm, "study", "games", "other")  # most used first: Mystery, then the two used once
    assert categorize_unknown(db, fake_llm.llm()) == 3
    with db.connect() as conn:
        overrides = load_overrides(conn)
        categorizer = Categorizer.from_db(conn)
    assert overrides["mystery"].category == "study"
    assert {o.source for o in overrides.values()} == {"ai"}
    assert set(overrides) == {"mystery", "zork", "com.example.flash"}
    assert categorizer.lookup("Mystery") == Lookup("study", "ai", "mystery")
    assert categorize_unknown(db, fake_llm.llm()) == 0  # an answer of "other" counts: nothing is asked again
    assert len(fake_llm.chats()) == 1


def test_the_question_lists_names_ids_and_sites_and_the_fixed_categories(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Flash Cards", "com.example.flash"), session(1, "Zork", "zork")])
    add(db, "browser-1", "browser", [web(0, "news.example.org")])
    answer(fake_llm, "study", "other", "games")
    categorize_unknown(db, fake_llm.llm())
    body = fake_llm.chats()[0]["body"]
    items = json.loads(body["messages"][-1]["content"])
    assert sorted(item["n"] for item in items) == [1, 2, 3]
    listed = [{key: value for key, value in item.items() if key != "n"} for item in items]
    assert sorted(listed, key=json.dumps) == sorted([
        {"app": "Flash Cards", "id": "com.example.flash"},  # both, so the model can tell
        {"app": "Zork"},  # an id that only repeats the name is left out
        {"website": "news.example.org"},  # the browser it was seen in is not the site
    ], key=json.dumps)
    system = body["messages"][0]["content"]
    assert all(f"- {name}: {meaning}" in system for name, meaning in AI_MEANINGS.items())
    assert "Browsers are other" in system
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["apps"]["items"]["properties"]["category"]["enum"] == list(CATEGORIES)
    assert body["temperature"] == 0


def test_an_answer_for_too_few_apps_saves_nothing_and_they_wait(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery"), session(1, "Zork"), session(2, "Blip")])
    fake_llm.reply_text(json.dumps({"apps": [{"n": 1, "category": "games"}, {"n": 9, "category": "games"},
                                             {"n": 2, "category": "not-a-category"}, {"n": True, "category": "work"}]}))
    with pytest.raises(LLMBadAnswer, match="1 of 3"):
        ask_categories(fake_llm.llm(), [UnknownApp(k, False, k, None) for k in ("mystery", "zork", "blip")])
    fake_llm.reply_text(json.dumps({"apps": [{"n": 1, "category": "games"}]}))
    skip: dict[str, float] = {}
    assert categorize_unknown(db, fake_llm.llm(), skip=skip, clock=lambda: 100.0) == 0
    assert set(skip) == {"mystery", "zork", "blip"} and set(skip.values()) == {100.0 + AI_SKIP_SECONDS}
    with db.connect() as conn:
        assert load_overrides(conn) == {}
    assert categorize_unknown(db, fake_llm.llm(), skip=skip, clock=lambda: 200.0) == 0
    assert len(fake_llm.chats()) == 2  # the direct question and the first run's: still waiting, not asked again
    answer(fake_llm, "games", "study", "other")
    assert categorize_unknown(db, fake_llm.llm(), skip=skip, clock=lambda: 101.0 + AI_SKIP_SECONDS) == 3
    assert skip == {}


def test_one_bad_batch_never_blocks_the_next(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery"), session(1, "Mystery"), session(2, "Zork"), session(3, "Blip")])
    fake_llm.reply_text("I would rather not say.")  # the first batch: Mystery and one more
    answer(fake_llm, "games")
    skip: dict[str, float] = {}
    assert categorize_unknown(db, fake_llm.llm(), batch=2, skip=skip) == 1
    assert len(skip) == 2 and "mystery" in skip
    with db.connect() as conn:
        assert [key for key, o in load_overrides(conn).items() if o.source == "ai"] == [
            ({"zork", "blip"} - set(skip)).pop()]


def test_a_run_stops_between_batches_and_before_saving(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery"), session(1, "Zork")])
    assert categorize_unknown(db, fake_llm.llm(), batch=1, stopped=lambda: True) == 0
    assert fake_llm.chats() == []
    answer(fake_llm, "games")
    calls = iter([False, True])  # stopped while the model thought
    assert categorize_unknown(db, fake_llm.llm(), batch=1, stopped=lambda: next(calls, True)) == 0
    with db.connect() as conn:
        assert load_overrides(conn) == {}


def test_a_run_waits_while_the_model_answers_someone_else(db: Database, fake_llm: FakeModelServer, monkeypatch: pytest.MonkeyPatch) -> None:
    add(db, "android-1", "android", [session(0, "Mystery")])
    llm = fake_llm.llm()
    monkeypatch.setattr(llm, "busy", lambda: True)
    assert categorize_unknown(db, llm) == 0
    assert fake_llm.chats() == []


def test_the_model_is_busy_while_a_chat_is_answered(fake_llm: FakeModelServer) -> None:
    llm = fake_llm.llm()
    llm.current_model()
    fake_llm.hold_chats = threading.Event()
    reply = threading.Thread(target=lambda: llm.chat([{"role": "user", "content": "hi"}]))
    reply.start()
    assert fake_llm.chat_started.wait(5)
    assert llm.busy()
    fake_llm.hold_chats.set()
    reply.join(5)
    assert not llm.busy()


def test_nothing_is_saved_for_apps_gone_or_hidden_while_the_model_thought(db: Database, fake_llm: FakeModelServer,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import daytrace_hub.categories as module
    from daytrace_hub.api.privacy import delete_everything

    add(db, "android-1", "android", [session(0, "Acme Planner"), session(1, "Zork")])
    answer(fake_llm, "work", "games")
    real = module.ask_categories

    def meanwhile(llm: Any, apps: list[UnknownApp]) -> dict[str, str]:
        answers = real(llm, apps)
        with db.connect() as conn, transaction(conn):
            redaction.save_choices(conn, redaction.check_choices([], [("Work", ["Acme"])]))  # a new rule
        return answers

    monkeypatch.setattr(module, "ask_categories", meanwhile)
    assert categorize_unknown(db, fake_llm.llm()) == 1
    with db.connect() as conn:
        assert set(load_overrides(conn)) == {"zork"}  # never "acme planner"

    add(db, "android-1", "android", [session(2, "Blip")])
    answer(fake_llm, "games")

    def delete_first(llm: Any, apps: list[UnknownApp]) -> dict[str, str]:
        answers = real(llm, apps)
        delete_everything(db, keep_redaction_rules=True)  # Delete everything, while the model thought
        return answers

    monkeypatch.setattr(module, "ask_categories", delete_first)
    assert categorize_unknown(db, fake_llm.llm()) == 0
    with db.connect() as conn:
        assert load_overrides(conn) == {}


def test_a_guess_the_user_removes_is_not_made_again(client: TestClient, db: Database, fake_llm: FakeModelServer) -> None:
    seed(db)  # Mystery is the one unknown app
    answer(fake_llm, "games")
    categorize_unknown(db, fake_llm.llm())
    assert client.delete("/api/v1/categories/mystery").status_code == 204
    with db.connect() as conn:
        assert load_declined(conn) == {"mystery"}
        assert unknown_apps(conn) == []
    assert categorize_unknown(db, fake_llm.llm()) == 0 and len(fake_llm.chats()) == 1
    assert client.put("/api/v1/categories/mystery", json={"category": "study"}).status_code == 200  # still theirs to set
    client.put("/api/v1/categories/zork", json={"category": "games"})
    assert client.delete("/api/v1/categories/zork").status_code == 204  # the user's own choice: nothing declined
    with db.connect() as conn:
        assert load_declined(conn) == {"mystery"}


def test_apps_not_used_in_the_window_are_not_asked_about(db: Database) -> None:
    long_ago = (datetime.now(UTC) - timedelta(days=AI_WINDOW_DAYS + 1)).isoformat()
    later = (datetime.now(UTC) - timedelta(days=AI_WINDOW_DAYS + 1) + timedelta(minutes=5)).isoformat()
    add(db, "android-1", "android", [{"kind": "app_session", "source": "usagestats", "start": long_ago, "end": later,
                                      "seq": 0, "app": "Ancient"}, session(1, "Mystery")])
    with db.connect() as conn:
        assert [app.key for app in unknown_apps(conn)] == ["mystery"]


def test_the_worker_skips_a_run_when_nothing_changed(db: Database, fake_llm: FakeModelServer, monkeypatch: pytest.MonkeyPatch) -> None:
    import daytrace_hub.categories as module

    add(db, "android-1", "android", [session(0, "Mystery")])
    answer(fake_llm, "games")
    worker = AiCategorizer(db, fake_llm.llm())
    assert worker.run_once() == 1
    runs: list[bool] = []
    real = module.categorize_unknown
    monkeypatch.setattr(module, "categorize_unknown", lambda *a, **k: runs.append(True) or real(*a, **k))
    assert worker.run_once() == 0  # its own save changed the data once: one more look finds nothing
    assert worker.run_once() == 0
    assert len(runs) <= 1
    add(db, "android-1", "android", [session(1, "Zork")])
    answer(fake_llm, "games")
    assert worker.run_once() == 1  # new events: it looks again
    worker.forget()
    assert worker._done_at is None


def test_delete_all_clears_what_the_worker_remembers(settings: Settings, fake_llm: FakeModelServer) -> None:
    on = Settings(profile=settings.profile, data_dir=settings.data_dir, ai_categories=True)
    with TestClient(create_app(on, llm=fake_llm.llm()), client=LOCAL_CLIENT, base_url=LOCAL_URL) as client:
        worker = client.app.state.ai_categorizer
        worker._skip["mystery"] = 1e18
        body = {"confirm": "delete all my daytrace data", "keep_redaction_rules": True}
        assert client.post("/api/v1/privacy/delete", json=body).status_code == 200
        assert worker._skip == {}


def test_asking_once_for_the_model_is_one_request_when_retry_is_off(fake_llm: FakeModelServer) -> None:
    fake_llm.status_codes = [500, 500]
    with pytest.raises(LLMError):
        fake_llm.llm().current_model(retry=False)
    assert len(fake_llm.requests) == 1


def test_apps_the_model_skips_are_other_and_its_first_answer_wins(fake_llm: FakeModelServer) -> None:
    apps = [UnknownApp(f"app{n}", False, f"App {n}", None) for n in range(1, 4)]
    fence = "`" * 3
    fake_llm.reply_text(fence + "json\n" + json.dumps({"apps": [
        {"n": 1, "category": "games"}, {"n": 1, "category": "work"}, {"n": 2, "category": "video"}]}) + "\n" + fence)
    assert ask_categories(fake_llm.llm(), apps) == {"app1": "games", "app2": "video", "app3": "other"}


def test_no_model_means_nothing_is_saved_and_it_is_tried_again_later(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery")])
    fake_llm.down = True
    with pytest.raises(LLMError):
        categorize_unknown(db, fake_llm.llm())
    fake_llm.down = False
    answer(fake_llm, "games")
    assert categorize_unknown(db, fake_llm.llm()) == 1


def test_a_choice_the_user_makes_meanwhile_is_kept(db: Database, fake_llm: FakeModelServer, monkeypatch: pytest.MonkeyPatch) -> None:
    import daytrace_hub.categories as module

    add(db, "android-1", "android", [session(0, "Mystery")])
    answer(fake_llm, "games")
    real = module.ask_categories

    def user_first(llm: Any, apps: list[UnknownApp]) -> dict[str, str]:
        with db.connect() as conn, transaction(conn):
            save_override(conn, "mystery", "study", "user")  # while the model thinks
        return real(llm, apps)

    monkeypatch.setattr(module, "ask_categories", user_first)
    categorize_unknown(db, fake_llm.llm())
    with db.connect() as conn:
        kept = load_overrides(conn)["mystery"]
    assert (kept.category, kept.source) == ("study", "user")


def test_the_list_shows_the_models_category_until_the_user_changes_it(client: TestClient, db: Database, fake_llm: FakeModelServer) -> None:
    seed(db)  # Mystery is the one unknown app
    answer(fake_llm, "games")
    categorize_unknown(db, fake_llm.llm())
    row = next(app for app in client.get("/api/v1/categories").json()["apps"] if app["app"] == "Mystery")
    assert (row["category"], row["source"]) == ("games", "ai")
    client.put("/api/v1/categories/mystery", json={"category": "work"})
    row = next(app for app in client.get("/api/v1/categories").json()["apps"] if app["app"] == "Mystery")
    assert (row["category"], row["source"]) == ("work", "user")


def test_applying_the_privacy_rules_drops_guesses_under_hidden_names(db: Database) -> None:
    with db.connect() as conn, transaction(conn):
        save_override(conn, "acme planner", "work", "ai")
        save_override(conn, "acme.example.com", "work", "ai")
        save_override(conn, "acme tools", "work", "user")
        save_override(conn, "zork", "games", "ai")
        redaction.save_choices(conn, redaction.check_choices([], [("Work", ["Acme"])]))
    redaction.stored_matches(db, apply=True)
    with db.connect() as conn:
        assert {key: o.source for key, o in load_overrides(conn).items()} == {"acme tools": "user", "zork": "ai"}


def test_the_worker_runs_a_while_after_it_is_woken_and_stops(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery")])
    answer(fake_llm, "games")
    worker = AiCategorizer(db, fake_llm.llm(), interval=60.0, delay=0.05)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while worker.runs < 1 and time.monotonic() < deadline:
            time.sleep(0.02)  # the first look comes `delay` after the start
    finally:
        worker.stop()
    assert worker.runs == 1
    with db.connect() as conn:
        assert load_overrides(conn)["mystery"].category == "games"


def test_a_failing_run_never_stops_the_worker(db: Database, fake_llm: FakeModelServer) -> None:
    add(db, "android-1", "android", [session(0, "Mystery")])
    fake_llm.down = True
    assert AiCategorizer(db, fake_llm.llm()).run_once() == 0  # logged, not raised


def test_the_hub_starts_the_worker_only_when_asked_and_an_ingest_wakes_it(settings: Settings, fake_llm: FakeModelServer, db: Database) -> None:
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=LOCAL_CLIENT, base_url=LOCAL_URL) as off:
        assert off.app.state.ai_categorizer is None
    on = Settings(profile=settings.profile, data_dir=settings.data_dir, ai_categories=True)
    token = add(db, "android-1", "android", [])
    with TestClient(create_app(on, llm=fake_llm.llm()), client=LOCAL_CLIENT, base_url=LOCAL_URL) as client:
        worker = client.app.state.ai_categorizer
        assert isinstance(worker, AiCategorizer)
        woken: list[bool] = []
        worker.wake = lambda: woken.append(True)  # type: ignore[method-assign]
        event = {"device_id": "android-1", **session(1, "Mystery")}
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post("/api/v1/events", json=event, headers=headers).status_code == 200
        assert woken == [True]
        assert client.post("/api/v1/events", json=event, headers=headers).json()["duplicates"] == 1
        assert woken == [True]  # a resend changes nothing


@pytest.mark.parametrize(("value", "expected"), [(None, True), ("off", False), ("on", True)])
def test_the_setting_is_on_unless_switched_off(tmp_path: Path, value: str | None, expected: bool) -> None:
    env = {"DAYTRACE_DATA_DIR": str(tmp_path)}
    if value is not None:
        env["DAYTRACE_AI_CATEGORIES"] = value
    assert load_settings("demo", env).ai_categories is expected


def test_a_bad_setting_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DAYTRACE_AI_CATEGORIES"):
        load_settings("demo", {"DAYTRACE_DATA_DIR": str(tmp_path), "DAYTRACE_AI_CATEGORIES": "maybe"})


@pytest.mark.parametrize(("text", "expected"), [
    ('{"apps": []}', {"apps": []}),
    ('<think>hmm {not this}</think>{"apps": [1]}', {"apps": [1]}),
    ("`" * 3 + 'json\n{"apps": [2]}\n' + "`" * 3, {"apps": [2]}),
    ('Sure! {"apps": [3]} Hope that helps.', {"apps": [3]}),
])
def test_json_is_found_in_a_models_reply(text: str, expected: Any) -> None:
    assert json_in(text) == expected


@pytest.mark.parametrize("text", ["", "no json here", '<think>{"a": 1}</think>', "{broken"])
def test_a_reply_without_json_is_an_error(text: str) -> None:
    with pytest.raises(ValueError):
        json_in(text)


def test_a_server_that_refuses_structured_output_is_asked_again_without_it(fake_llm: FakeModelServer) -> None:
    llm = fake_llm.llm()
    llm.current_model()  # chosen already, so the 400 below answers the chat itself
    fake_llm.status_codes = [400]
    fake_llm.reply_text('{"ok": true}')
    assert llm.json_reply([{"role": "user", "content": "hi"}], {"type": "object"}, "test") == {"ok": True}
    first, second = fake_llm.chats()
    assert "response_format" in first["body"] and "response_format" not in second["body"]


def test_a_server_error_is_not_asked_again(fake_llm: FakeModelServer) -> None:
    llm = fake_llm.llm()
    llm.current_model()
    fake_llm.status_codes = [500, 500]
    with pytest.raises(LLMError):
        llm.json_reply([{"role": "user", "content": "hi"}], {"type": "object"}, "test", retry=False)
    assert len(fake_llm.chats()) == 1


def test_a_cut_off_or_wordy_reply_is_an_error(fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text('{"ok": tr', finish_reason="length")
    with pytest.raises(LLMError, match="cut off"):
        fake_llm.llm().json_reply([{"role": "user", "content": "hi"}], {"type": "object"}, "test")
    fake_llm.reply_text("I would rather not.")
    with pytest.raises(LLMError, match="not the JSON"):
        fake_llm.llm().json_reply([{"role": "user", "content": "hi"}], {"type": "object"}, "test")
