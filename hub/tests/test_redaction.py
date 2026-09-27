"""Tests for DT-44: title redaction.

The claims: a title that matches a rule (banking, health, password managers, private windows, or the user's own
words) is stored as [redacted] with the app name kept, whichever collector sends it, and the desktop tracker never
even holds it; ordinary titles are left alone; a rule change applies to the next event; a resent event stays one
event; and not even a hash of a sensitive title is kept.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_tracker_base import Clock, Recorder, Script

from daytrace_hub import redaction
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.models import Event
from daytrace_hub.redaction import REDACTED, LiveRedactor, Redactor
from daytrace_hub.seed import seed
from daytrace_hub.tracker.base import Reading, Tracker, TrackerService

PHONE = ("192.168.1.40", 50000)
WINDOW = {"kind": "window", "source": "tracker", "start": "2026-09-25T14:00:00-04:00", "end": "2026-09-25T14:05:00-04:00",
          "app": "Microsoft Edge", "app_id": "msedge.exe"}


@pytest.fixture
def tokens(db: Database) -> dict[str, str]:
    with db.connect() as conn:
        return {
            "windows-2": register_device(conn, device_id="windows-2", name="Laptop", device_type="windows"),
            "iphone-1": register_device(conn, device_id="iphone-1", name="iPhone", device_type="ios"),
            "viewer-1": register_device(conn, device_id="viewer-1", name="Phone browser", device_type="viewer"),
        }


def send(client: TestClient, tokens: dict[str, str], device_id: str, *events: dict[str, Any]) -> dict[str, Any]:
    body = {"events": [{**event, "device_id": device_id} for event in events]}
    response = client.post("/api/v1/events", json=body, headers={"Authorization": f"Bearer {tokens[device_id]}"})
    assert response.status_code == 200, response.text
    return response.json()


def rows(db: Database, device_id: str = "windows-2") -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM events WHERE device_id = ? ORDER BY id", (device_id,))]


def window(seq: int, title: str, **changes: Any) -> dict[str, Any]:
    return {**WINDOW, "seq": seq, "title": title, **changes}


def put_rules(client: TestClient, body: dict[str, Any], **kwargs: Any) -> Any:
    return client.put("/api/v1/privacy/redaction", json=body, **kwargs)


# --- the rules -----------------------------------------------------------------------------------------------------


def test_the_ticket_example_is_stored_redacted_with_the_app_kept(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    send(client, tokens, "windows-2", window(1, "MyBank - Account Summary"))
    (row,) = rows(db)
    assert (row["title"], row["app"], row["app_id"]) == (REDACTED, "Microsoft Edge", "msedge.exe")
    assert row["start_utc"] == "2026-09-25T18:00:00.000000Z"  # the time still counts


@pytest.mark.parametrize(("title", "app", "app_id", "rule"), [
    ("Sign in - RBC Royal Bank - Microsoft Edge", "Microsoft Edge", "msedge.exe", "banking"),
    ("Online Banking | Tangerine", "Google Chrome", "chrome.exe", "banking"),
    ("Send an e-Transfer", "Google Chrome", "chrome.exe", "banking"),
    ("Activity", "PayPal", "com.paypal.android.p2pmobile", "banking"),
    ("MyChart - Test Results", "Microsoft Edge", "msedge.exe", "health"),
    ("Therapy with Dr. Lee", None, None, "health"),
    ("Dentist appointment", None, None, "health"),
    ("GitHub login", "1Password", "1Password.exe", "passwords"),
    ("Vault", "Bitwarden", "com.x8bit.bitwarden", "passwords"),
    ("New tab - [InPrivate] - Microsoft Edge", "Microsoft Edge", "msedge.exe", "private"),
    ("Google - Google Chrome (Incognito)", "Google Chrome", "chrome.exe", "private"),
    ("Mozilla Firefox Private Browsing", "Firefox", "firefox.exe", "private"),
])
def test_each_built_in_rule(title: str, app: str | None, app_id: str | None, rule: str) -> None:
    found = Redactor().match(title, app, app_id)
    assert found is not None and found.id == rule


@pytest.mark.parametrize("title", [
    "stats.py - daytrace - Visual Studio Code", "YouTube - Microsoft Edge", "Study block: algorithms", "Lecture notes",
    "Doctor Who S01E01 - VLC", "Hydrate reminder", "Cardio at the gym", "Standup", REDACTED,
])
def test_ordinary_titles_are_left_alone(title: str) -> None:
    assert Redactor().match(title, "Visual Studio Code", "Code.exe") is None


def test_a_redacted_calendar_event_keeps_only_whether_it_is_all_day(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    event = {"kind": "calendar_event", "source": "shortcuts", "external_id": "cal:1", "title": "Therapy with Dr. Lee",
             "start": "2026-09-25T16:00:00-04:00", "end": "2026-09-25T17:00:00-04:00",
             "data": {"all_day": False, "location": "12 Clinic Road", "notes": "bring the forms"}}
    send(client, tokens, "iphone-1", event)
    (row,) = rows(db, "iphone-1")
    assert row["title"] == REDACTED and json.loads(row["data"]) == {"all_day": False}


def test_the_seeded_titles_are_not_redacted(tmp_path: Path) -> None:
    settings = Settings(profile=get_profile("demo"), data_dir=tmp_path)
    seed(settings, 3, UTC, datetime(2026, 9, 25, 21, 0, tzinfo=UTC))
    with Database(settings.database_path).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE title = ?", (REDACTED,)).fetchone()[0] == 0  # the demo keeps its titles


# --- changing the rules --------------------------------------------------------------------------------------------


def test_rule_changes_apply_to_the_next_event(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    send(client, tokens, "windows-2", window(1, "Project Falcon plan.docx - Word", app="Word", app_id="WINWORD.EXE"))
    assert rows(db)[0]["title"] == "Project Falcon plan.docx - Word"
    body = put_rules(client, {"disabled": ["health"], "custom": [{"name": "Work client", "words": ["project falcon", "  Acme  "]}]}).json()
    assert [(rule["id"], rule["enabled"]) for rule in body["rules"]] == [
        ("banking", True), ("health", False), ("passwords", True), ("private", True), ("custom-1", True)]
    assert body["rules"][-1]["words"] == ["project falcon", "Acme"]
    send(client, tokens, "windows-2", window(2, "Project Falcon budget.xlsx - Excel", app="Excel", app_id="EXCEL.EXE"),
         window(3, "Dentist appointment - Outlook", app="Outlook", app_id="OUTLOOK.EXE"))
    stored = [row["title"] for row in rows(db)]
    assert stored == ["Project Falcon plan.docx - Word", REDACTED, "Dentist appointment - Outlook"]  # health is off now


def test_your_words_match_whole_words_in_titles_and_app_names(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    put_rules(client, {"custom": [{"name": "Chats", "words": ["Signal", "go"]}]})
    send(client, tokens, "windows-2", window(1, "Chat with Sam", app="Signal", app_id="Signal.exe"),
         window(2, "Google Maps - Google Chrome", app="Google Chrome", app_id="chrome.exe"),
         window(3, "Let's go hiking", app="Notepad", app_id="notepad.exe"))
    assert [row["title"] for row in rows(db)] == [REDACTED, "Google Maps - Google Chrome", REDACTED]


@pytest.mark.parametrize("body", [
    {"disabled": ["everything"]},
    {"custom": [{"name": "Empty", "words": []}]},
    {"custom": [{"name": "Short", "words": ["a"]}]},
    {"custom": [{"name": "Long", "words": ["x" * 101]}]},
    {"custom": [{"name": "Hidden", "words": ["ab\u200bcd"]}]},
    {"custom": [{"name": "", "words": ["fine"]}]},
    {"custom": [{"name": f"Rule {n}", "words": ["fine"]} for n in range(21)]},
    {"custom": [{"name": "Many", "words": [f"word{n}" for n in range(51)]}]},
])
def test_rules_that_cannot_be_saved_are_refused(client: TestClient, body: dict[str, Any]) -> None:
    response = put_rules(client, body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"]
    assert client.get("/api/v1/privacy/redaction").json()["rules"][-1]["id"] == "private"  # nothing was saved


def test_only_the_dashboard_can_change_the_rules(client: TestClient, tokens: dict[str, str]) -> None:
    with TestClient(client.app, client=PHONE) as phone:
        assert phone.get("/api/v1/privacy/redaction").status_code == 401
        as_collector = {"Authorization": f"Bearer {tokens['windows-2']}"}
        assert phone.get("/api/v1/privacy/redaction", headers=as_collector).status_code == 200
        assert put_rules(phone, {"disabled": ["private"]}, headers=as_collector).status_code == 403
        as_viewer = {"Authorization": f"Bearer {tokens['viewer-1']}"}
        assert put_rules(phone, {"disabled": ["private"]}, headers=as_viewer).status_code == 200


def test_trying_a_title(client: TestClient) -> None:
    hit = client.post("/api/v1/privacy/redaction/check", json={"title": "Online Banking - TD Bank", "app": "Edge"}).json()
    assert hit == {"redacted": True, "stored_as": REDACTED, "rule": "banking", "rule_name": "Banking and payments"}
    miss = client.post("/api/v1/privacy/redaction/check", json={"title": "daytrace - GitHub"}).json()
    assert miss == {"redacted": False, "stored_as": "daytrace - GitHub", "rule": None, "rule_name": None}


def test_unreadable_choices_mean_every_built_in_rule(db: Database) -> None:
    with db.connect() as conn, transaction(conn):
        conn.execute("INSERT INTO settings (key, value) VALUES ('redaction', 'not json')")
    with db.connect() as conn:
        assert [rule.id for rule in redaction.redactor_for(conn).rules] == ["banking", "health", "passwords", "private"]


# --- resending, keys ----------------------------------------------------------------------------------------------


def test_an_event_resent_under_a_new_rule_is_the_same_event(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    first = window(7, "Acme roadmap - Word", app="Word", app_id="WINWORD.EXE")
    ext = {**window(8, "Acme notes - OneNote", app="OneNote", app_id="ONENOTE.EXE"), "external_id": "tracker:1:8"}
    send(client, tokens, "windows-2", first, ext)
    put_rules(client, {"custom": [{"name": "Client", "words": ["Acme"]}]})
    again = send(client, tokens, "windows-2", first, {**ext, "seq": 9})  # a retry after the rule was added
    assert again["rejected"] == [] and again["accepted"] == 0
    assert [row["title"] for row in rows(db)] == [REDACTED, REDACTED]  # the stored words went too, one row each


def test_a_content_key_is_made_from_the_redacted_event(client: TestClient, db: Database, tokens: dict[str, str]) -> None:
    event = {"kind": "app_open", "source": "shortcuts", "start": "2026-09-25T14:00:00-04:00", "app": "Safari",
             "title": "Online Banking - Chase Online"}
    send(client, tokens, "iphone-1", event)
    (row,) = rows(db, "iphone-1")
    original = Event.model_validate({**event, "device_id": "iphone-1"})
    assert row["dedup_key"] == Redactor().event(original).dedup_key() != original.dedup_key()
    send(client, tokens, "iphone-1", event)
    assert len(rows(db, "iphone-1")) == 1  # resending stays safe


# --- the desktop tracker --------------------------------------------------------------------------------------------


def test_the_tracker_never_holds_a_sensitive_title(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    moment = [1000.0]
    monkeypatch.setattr(redaction, "monotonic", lambda: moment[0])
    clock, probe, sink = Clock(), Script(), Recorder()
    live = LiveRedactor(db)
    tracker = Tracker(probe, sink, "windows-1", last_seq=0, wall=lambda: clock.now, monotonic=lambda: clock.mono, redact=live)

    def show(reading: Reading) -> None:  # long enough that a new title starts a new span (not a quick relabel)
        probe.next = reading
        for _ in range(8):
            tracker.step()
            clock.advance(2)

    show(Reading("Microsoft Edge", "msedge.exe", "MyBank - Account Summary", 0.0))
    show(Reading("Word", "WINWORD.EXE", "Falcon launch - Word", 0.0))
    with db.connect() as conn, transaction(conn):
        redaction.save_choices(conn, redaction.check_choices([], [("Work", ["Falcon"])]))
    moment[0] += redaction.TRACKER_REFRESH_SECONDS  # the tracker looks again
    show(Reading("Word", "WINWORD.EXE", "Falcon budget - Word", 0.0))
    tracker.flush()
    titles = [span.get("title") for span in sink.spans() if span["kind"] == "window"]
    assert titles[0] == REDACTED and titles[1] == "Falcon launch - Word" and titles[-1] == REDACTED
    assert not any("MyBank" in json.dumps(write, default=str) for write in sink.writes)


def test_the_tracker_service_redacts_by_default(db: Database, tmp_path: Path) -> None:
    service = TrackerService(db, Script(), "windows", "PC", tmp_path / "tracker.lock")
    assert isinstance(service.redact, LiveRedactor)
