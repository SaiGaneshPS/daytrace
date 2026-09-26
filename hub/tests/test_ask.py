"""Tests for DT-40: ask your day, with tool calling over the stats engine."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeModelServer
from fastapi.testclient import TestClient

from daytrace_hub import ask as ask_module
from daytrace_hub.api import ai as ai_api
from daytrace_hub.api.events import store_events
from daytrace_hub.app import create_app
from daytrace_hub.ask import DECLINED, MAX_TOOL_CALLS, TOOLS, ToolError, ask, facts_answer, run_tool
from daytrace_hub.auth import register_device
from daytrace_hub.config import Settings, get_profile
from daytrace_hub.db import Database, transaction
from daytrace_hub.llm import LLMError
from daytrace_hub.models import Event
from daytrace_hub.seed import seed
from daytrace_hub.stats import Stats
from daytrace_hub.story import unsupported_numbers

ZONE = ZoneInfo("America/Toronto")
TZ = "America/Toronto"
NOW = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)  # Monday 12:00 in Toronto: last week is 21 to 27 September
LAST_WEEK = {"first_day": "2026-09-21", "last_day": "2026-09-27"}
WEEK_DAYS = {date(2026, 9, 21) + timedelta(days=i) for i in range(8)}
YOUTUBE_AFTER_11 = {**LAST_WEEK, "app": "YouTube", "from_time": "23:00", "until_time": "03:00", "group_by": "day"}
QUESTION = "How much YouTube after 11 pm last week?"
GOOD = "Last week you watched 1 hour 50 minutes of YouTube after 11 pm, most of it on Tuesday night (45 minutes)."


def at(day: int, clock: str) -> str:
    return f"2026-09-{day:02d}T{clock}-04:00"  # Toronto in September


def span(kind: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "source": extra.pop("source", "tracker"), "start": start, "end": end, **extra}


def phone(app: str, app_id: str, start: str, end: str, seq: int) -> dict[str, Any]:
    return span("app_session", start, end, app=app, app_id=app_id, seq=seq, source="usagestats")


def add(db: Database, device_id: str, device_type: str, events: list[dict[str, Any]]) -> None:
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
            register_device(conn, device_id=device_id, name=device_id, device_type=device_type)
        with transaction(conn):
            store_events(conn, device_id, [(i, Event.model_validate({"device_id": device_id, **e})) for i, e in enumerate(events)])


YT = ("YouTube", "com.google.android.youtube")


@pytest.fixture
def week(db: Database) -> Database:
    """Last week's late YouTube (Toronto). Every expected number below is worked out from it.

    Phone (android-1): YouTube Mon 21 22:30-23:30 (30 minutes after 23:00), Tue 22 23:45-00:30 (45), Wed 23
    20:00-21:00 (none after 23:00), Fri 25 01:00-01:20 (20, the night of Thursday 24); Instagram Wed 23 23:10-23:40.
    Desk (windows-1): Edge Sat 26 23:00-23:20, where the extension saw youtube.com 23:00-23:15 (15).
    So YouTube after 11 pm last week is 30 + 45 + 20 + 15 = 110 minutes, 95 of them on the phone.
    """
    add(db, "android-1", "android", [
        phone(*YT, at(21, "22:30:00"), at(21, "23:30:00"), 1),
        phone(*YT, at(22, "23:45:00"), at(23, "00:30:00"), 2),
        phone(*YT, at(23, "20:00:00"), at(23, "21:00:00"), 3),
        phone(*YT, at(25, "01:00:00"), at(25, "01:20:00"), 4),
        phone("Instagram", "com.instagram.android", at(23, "23:10:00"), at(23, "23:40:00"), 5),
    ])
    add(db, "windows-1", "windows", [span("window", at(26, "23:00:00"), at(26, "23:20:00"), app="Microsoft Edge", app_id="msedge.exe")])
    add(db, "browser-1", "browser", [span("web", at(26, "23:00:00"), at(26, "23:15:00"), source="browser", app_id="msedge.exe",
                                          data={"domain": "youtube.com"}, seq=1)])
    return db


def asked(db: Database, fake: FakeModelServer, question: str = QUESTION) -> ask_module.AskResult:
    return ask(db, fake.llm(), question, ZONE, TZ, NOW)


def tool(db: Database, name: str, args: dict[str, Any], now: datetime = NOW) -> ask_module.ToolOutput:
    return run_tool(db, ZONE, TZ, now, name, json.dumps(args))


# --- the stats filters ask uses --------------------------------------------------------------------------------------


def test_totals_can_count_one_app_at_night(week: Database) -> None:
    night = (time(23), time(3))
    with week.connect() as conn:
        stats = Stats(conn, ZONE, TZ, NOW)
        youtube = stats.totals(date(2026, 9, 21), date(2026, 9, 27), "day", between=night, app="youtube")
        assert youtube["total_minutes"] == 110
        assert {i["key"]: i["minutes"] for i in youtube["items"] if i["minutes"]} == {
            "2026-09-21": 30, "2026-09-22": 45, "2026-09-24": 20, "2026-09-26": 15}  # after midnight: the night before
        assert youtube["missing_days"] == ["2026-09-27"]
        phones = stats.totals(date(2026, 9, 21), date(2026, 9, 27), between=night, app="youtube",
                              device_types=frozenset({"android", "ios"}))
        assert phones["total_minutes"] == 95
        assert stats.totals(date(2026, 9, 21), date(2026, 9, 27), between=night, category="social")["total_minutes"] == 30
        assert stats.totals(date(2026, 9, 23))["total_minutes"] == 30 + 60 + 30  # no filters: the whole day as before


# --- the tools -------------------------------------------------------------------------------------------------------


def test_sessions_say_when_things_happened(week: Database) -> None:
    out = tool(week, "get_sessions", {**LAST_WEEK, "app": "YouTube", "from_time": "23:00", "until_time": "03:00"})
    assert [(f.label, f.value) for f in out.facts] == [
        ("number of sessions in apps matching YouTube between 23:00 and 03:00, from Monday 2026-09-21 to Sunday 2026-09-27", 4),
        ("time in those sessions, from Monday 2026-09-21 to Sunday 2026-09-27", 110),
        ("YouTube (video) on android-1, Monday 2026-09-21 23:00 to 23:30", 30),
        ("YouTube (video) on android-1, Tuesday 2026-09-22 23:45 to 00:30", 45),  # one session across midnight
        ("YouTube (video) on android-1, Friday 2026-09-25 01:00 to 01:20", 20),
        ("youtube.com (video) on windows-1, Saturday 2026-09-26 23:00 to 23:15", 15),
    ]


def test_bad_arguments_are_refused_with_a_reason(week: Database) -> None:
    with pytest.raises(ToolError, match="at most 31 days"):
        tool(week, "get_totals", {"first_day": "2026-08-01", "last_day": "2026-09-27"})
    with pytest.raises(ToolError, match="a date like"):
        tool(week, "get_totals", {"first_day": "last monday", "last_day": "2026-09-27"})
    with pytest.raises(ToolError, match="24-hour time"):
        tool(week, "get_totals", {**LAST_WEEK, "from_time": "11pm"})
    with pytest.raises(ToolError, match="category must be one of"):
        tool(week, "get_totals", {**LAST_WEEK, "category": "fun"})
    with pytest.raises(ToolError, match="not valid JSON"):
        run_tool(week, ZONE, TZ, NOW, "get_totals", "{oops")
    with pytest.raises(ToolError, match="no tool called get_streaks"):
        run_tool(week, ZONE, TZ, NOW, "get_streaks", "{}")


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Database:
    settings = Settings(profile=get_profile("demo"), data_dir=Path(tmp_path_factory.mktemp("seeded")))
    seed(settings, 14, ZONE, NOW)
    return Database(settings.database_path)


@pytest.mark.parametrize("name", list(TOOLS))
def test_every_tool_works_on_seeded_days_and_its_facts_pass_the_check(seeded: Database, name: str) -> None:
    out = tool(seeded, name, LAST_WEEK)
    assert out.facts
    for fact in out.facts:  # a fact read back as the plain answer uses only itself: labels and values agree
        assert unsupported_numbers(facts_answer([fact]), out.facts, out.days) == [], fact


def test_the_youtube_question_on_seeded_data(seeded: Database) -> None:
    """The seed's late nights with YouTube are in the week of 14 September (its first days)."""
    out = tool(seeded, "get_totals", {**YOUTUBE_AFTER_11, "first_day": "2026-09-14", "last_day": "2026-09-20"})
    with seeded.connect() as conn:
        stats = Stats(conn, ZONE, TZ, NOW)
        nights = [stats.totals(date(2026, 9, 14) + timedelta(days=i), between=(time(23), time(3)), app="youtube")
                  for i in range(7)]
    assert out.facts[0].value == round(sum(n["total_minutes"] for n in nights)) > 60
    assert nights[0]["missing_days"] == ["2026-09-14"]  # before the seed began: missing, not zero


# --- asking ----------------------------------------------------------------------------------------------------------


def test_the_youtube_question_gets_the_right_figure(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text(GOOD)
    result = asked(week, fake_llm)
    assert (result.answer, result.fallback, result.declined, result.tools_called) == (GOOD, False, False, ["get_totals"])
    assert result.model == "qwen3-14b"
    total = result.facts[0]
    assert (total.value, total.unit) == (110, "minutes") and total.label.startswith("time in YouTube between 23:00 and 03:00")
    assert result.chart is not None
    assert [(p["label"], p["value"]) for p in result.chart["points"] if p["value"]] == [
        ("2026-09-21", 30), ("2026-09-22", 45), ("2026-09-24", 20), ("2026-09-26", 15)]
    first, second = fake_llm.chats()
    assert "Today is Monday 2026-09-28" in first["body"]["messages"][0]["content"]
    assert [t["function"]["name"] for t in first["body"]["tools"]] == list(TOOLS)
    sent = second["body"]["messages"][-1]
    assert sent["role"] == "tool" and json.loads(sent["content"])["facts"][0]["value"] == 110


def test_off_topic_questions_are_declined(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("OFF_TOPIC")
    result = asked(week, fake_llm, "What is the capital of France?")
    assert result.declined is True and result.answer == DECLINED
    assert (result.facts, result.chart, result.fallback) == ([], None, False)


def test_a_wrong_number_is_caught_and_retried(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text("You watched 2 hours 30 minutes of YouTube after 11 pm last week.")
    fake_llm.reply_text(GOOD)
    result = asked(week, fake_llm)
    assert result.answer == GOOD and result.fallback is False
    retry = fake_llm.chats()[2]["body"]["messages"][-1]["content"]
    assert "2 hours 30 minutes" in retry and "not in the tool results" in retry


def test_wrong_numbers_twice_show_the_facts_instead(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text("You watched 2 hours 30 minutes of YouTube after 11 pm last week.")
    fake_llm.reply_text("You watched 3 hours of YouTube after 11 pm last week.")
    result = asked(week, fake_llm)
    assert result.fallback is True and result.model is None and "3 hours" in (result.reason or "")
    assert result.answer.startswith("Here is what I found: time in YouTube between 23:00 and 03:00, from Monday "
                                    "2026-09-21 to Sunday 2026-09-27: 1 hour 50 minutes;")
    assert unsupported_numbers(result.answer, result.facts, WEEK_DAYS) == []  # the facts never fail their own check


def test_numbers_without_any_facts_are_sent_back(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_text("You watched about 3 hours of YouTube.")  # guessed, without calling a tool
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text(GOOD)
    result = asked(week, fake_llm)
    assert result.answer == GOOD and result.tools_called == ["get_totals"]
    assert "call a tool to get facts first" in fake_llm.chats()[1]["body"]["messages"][-1]["content"]


def test_at_most_four_tool_calls(week: Database, fake_llm: FakeModelServer) -> None:
    for _ in range(MAX_TOOL_CALLS):
        fake_llm.reply_tool_call("get_focus", LAST_WEEK)
    fake_llm.reply_text("I looked at your focus last week.")
    result = asked(week, fake_llm, "How focused was I last week?")
    assert result.tools_called == ["get_focus"] * MAX_TOOL_CALLS
    assert "tools" not in fake_llm.chats()[-1]["body"]  # the answer had to come without more tools


def test_calls_past_the_limit_are_refused(week: Database, fake_llm: FakeModelServer) -> None:
    for _ in range(MAX_TOOL_CALLS - 1):
        fake_llm.reply_tool_call("get_focus", LAST_WEEK)

    def call(n: int) -> dict[str, Any]:
        return {"id": f"call_x{n}", "type": "function", "function": {"name": "get_sleep", "arguments": json.dumps(LAST_WEEK)}}

    fake_llm.replies.append({"role": "assistant", "content": None, "tool_calls": [call(1), call(2)]})
    fake_llm.reply_text("Done.")
    result = asked(week, fake_llm, "How did I sleep and focus last week?")
    assert result.tools_called == ["get_focus"] * 3 + ["get_sleep"]
    assert "only 4 tool calls" in fake_llm.chats()[-1]["body"]["messages"][-1]["content"]


def test_a_bad_call_is_explained_to_the_model(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", {"first_day": "2026-08-01", "last_day": "2026-09-27"})
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text(GOOD)
    result = asked(week, fake_llm)
    assert result.answer == GOOD and result.tools_called == ["get_totals", "get_totals"]
    refused = fake_llm.chats()[1]["body"]["messages"][-1]
    assert "at most 31 days" in json.loads(refused["content"])["error"]


def test_the_same_fact_is_listed_once(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text(GOOD)
    result = asked(week, fake_llm)
    assert len(result.facts) == len(set(result.facts))


def test_a_reply_cut_off_while_thinking_is_retried(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text("<think>110 minutes is 1 hour 50", finish_reason="length")
    fake_llm.reply_text(f"<think>110 minutes is 1 hour 50 minutes.</think> {GOOD}")
    result = asked(week, fake_llm)
    assert result.answer == GOOD
    assert "cut off" in fake_llm.chats()[2]["body"]["messages"][-1]["content"]


def test_a_model_that_goes_away_after_the_facts_shows_them(week: Database, fake_llm: FakeModelServer,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    real = ask_module.run_tool

    def run_then_stop(*args: Any) -> ask_module.ToolOutput:
        output = real(*args)
        fake_llm.down = True
        return output

    monkeypatch.setattr(ask_module, "run_tool", run_then_stop)
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    result = asked(week, fake_llm)
    assert result.fallback is True and "1 hour 50 minutes" in result.answer


def test_without_a_model_there_is_no_answer(week: Database, fake_llm: FakeModelServer) -> None:
    fake_llm.down = True
    with pytest.raises(LLMError):
        asked(week, fake_llm)


def test_the_ask_endpoint(week: Database, settings: Settings, fake_llm: FakeModelServer,
                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_api, "current_time", lambda: NOW)
    fake_llm.reply_tool_call("get_totals", YOUTUBE_AFTER_11)
    fake_llm.reply_text(GOOD)
    with TestClient(create_app(settings, llm=fake_llm.llm()), client=("127.0.0.1", 50000), base_url="http://localhost:8765") as hub:
        body = hub.post("/api/v1/ask", json={"question": QUESTION, "tz": TZ}).json()
        assert hub.post("/api/v1/ask", json={"question": "   ", "tz": TZ}).status_code == 400
        assert hub.post("/api/v1/ask", json={"question": QUESTION, "tz": "Mars/Base"}).status_code == 400
        phone = TestClient(hub.app, client=("192.168.1.50", 40000))
        assert phone.post("/api/v1/ask", json={"question": QUESTION}).status_code == 401
        fake_llm.down = True
        away = hub.post("/api/v1/ask", json={"question": QUESTION, "tz": TZ})
        assert away.status_code == 503 and away.json()["error"]["code"] == "ai_unavailable"
    assert (body["answer"], body["tools_called"], body["model"], body["fallback"], body["declined"]) == (
        GOOD, ["get_totals"], "qwen3-14b", False, False)
    assert body["facts_used"][0]["value"] == 110 and body["chart"]["kind"] == "bar"
