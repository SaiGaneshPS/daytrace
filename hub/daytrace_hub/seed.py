"""DT-15: seed data generator (14 days across 4 devices, plus the browser extension).

`python -m daytrace_hub seed --profile demo --days 14` fills a demo profile with a believable student's
fortnight, so the dashboard, charts, streaks, AI and demo work without real devices. Everything is tagged
`source: "seed"`, and the patterns are there on purpose:

- Late nights on the phone lower the next day's focus (the correlation the Insights page and the AI find).
- A 5-day focus streak that breaks once, after a late night, then starts again.
- Weekday study blocks on the calendar (sometimes with TikTok in the middle), meals, sleep, daily steps.

Re-running is always safe: the seed devices' seed events are replaced in one transaction, so shifting the
window to a new day never duplicates or conflicts. The personal profile (real data) is never seeded.
"""
from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from .api.events import store_events
from .db import Database, transaction, utc_text
from .models import Event

SOURCE = "seed"
REFUSED_PROFILES = frozenset({"personal"})
MAX_DAYS = 90
DEVICES: dict[str, tuple[str, str]] = {
    "seed-windows": ("windows", "Desk PC (demo)"),
    "seed-mac": ("macos", "MacBook (demo)"),
    "seed-android": ("android", "Galaxy phone (demo)"),
    "seed-iphone": ("ios", "iPhone (demo)"),
    "seed-browser": ("browser", "Edge extension (demo)"),
}
# Collectors with a local store number their events; iPhone Shortcuts are stateless and send no seq.
NUMBERED = frozenset({"seed-windows", "seed-mac", "seed-android", "seed-browser"})
Focus = Literal["good", "ok", "poor"]

CODE = ("Visual Studio Code", "Code.exe")
EDGE = ("Microsoft Edge", "msedge.exe")
SLACK = ("Slack", "slack.exe")
DISCORD = ("Discord", "Discord.exe")
FIGMA = ("Figma", "Figma.exe")
MINECRAFT = ("Minecraft", "Minecraft.exe")
STEAM = ("Steam", "steam.exe")
FILES = ("stats.py", "sessions.py", "timeline.py", "Today.tsx", "Timeline.tsx", "seed.py", "api.md")
WORK_SITES = ("github.com", "stackoverflow.com", "docs.google.com", "developer.mozilla.org")
FUN_SITES = ("youtube.com", "reddit.com", "netflix.com")
PHONE_APPS = {
    "social": [("Instagram", "com.instagram.android"), ("TikTok", "com.zhiliaoapp.musically")],
    "video": [("YouTube", "com.google.android.youtube")],
    "comms": [("WhatsApp", "com.whatsapp"), ("Messages", "com.google.android.apps.messaging")],
}
STUDY_TOPICS = ("algorithms", "databases", "linear algebra", "operating systems", "statistics")
MEALS = {
    "breakfast": [["oatmeal", "banana", "coffee"], ["toast", "eggs"], ["yogurt", "granola"], ["poha", "chai"]],
    "lunch": [["chicken wrap", "apple"], ["rice", "dal", "salad"], ["pasta", "garlic bread"], ["sushi"]],
    "dinner": [["two rotis", "dal", "sabzi"], ["salmon", "rice", "broccoli"], ["pizza", "salad"], ["ramen"]],
}


@dataclass(frozen=True)
class DayPlan:
    day: date
    index: int
    late_before: int  # minutes on the phone after 23:20 the night before this day
    late_after: int
    focus_score: float  # 1.0 = a sharp day; lower after a late night, in proportion to how late

    @property
    def focus(self) -> Focus:
        return "good" if self.focus_score >= 0.95 else "poor" if self.focus_score < 0.5 else "ok"


@dataclass
class SeedResult:
    first_day: date
    last_day: date
    events: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.events.values())


def plan_days(today: date, days: int) -> list[DayPlan]:
    """How late each night runs and how focused each day is. With 14 days (index 13 is today): late nights
    of 90, 20, 130 and 45 minutes before days 1 to 4, a 5-day focus streak on days 5 to 9 (about 4.5 h of work
    apps a day), broken on day 10 after a 130-minute night, then sharp days again. The later the night, the
    less focus the next day, so the correlation is there to find, not just an on/off switch."""
    late_by_index = {days - 13: 90, days - 12: 20, days - 11: 130, days - 10: 45, days - 4: 130}
    late = {index: minutes for index, minutes in late_by_index.items() if index >= 1}
    plans = []
    for index in range(days):
        before = late.get(index, 0)
        plans.append(
            DayPlan(
                day=today - timedelta(days=days - 1 - index),
                index=index,
                late_before=before,
                late_after=late.get(index + 1, 0),
                focus_score=max(0.1, 1 - before / 140),
            )
        )
    return plans


class _Day:
    """Builds one day's events in local time; every time goes through at() so jitter stays reproducible."""

    def __init__(self, plan: DayPlan, tz: tzinfo) -> None:
        self.plan = plan
        self.tz = tz
        self.rng = random.Random(f"daytrace-seed:{plan.day.isoformat()}")
        self.events: list[dict[str, Any]] = []

    def at(self, clock: str, jitter: int = 0, day_offset: int = 0) -> datetime:
        hours, minutes = (int(part) for part in clock.split(":"))
        local = datetime.combine(self.plan.day + timedelta(days=day_offset), time(hours, minutes), tzinfo=self.tz)
        return local + timedelta(minutes=self.rng.randint(-jitter, jitter) if jitter else 0)

    def minutes(self, low: int, high: int) -> timedelta:
        return timedelta(minutes=self.rng.randint(low, high), seconds=self.rng.randint(0, 59))

    def add(self, device: str, kind: str, start: datetime, end: datetime | None = None, **fields: Any) -> datetime:
        event = {"device_id": device, "kind": kind, "source": SOURCE, "start": start.isoformat(), **fields}
        if end is not None:
            event["end"] = end.isoformat()
        self.events.append(event)
        return end or start

    # --- devices ---

    def window(self, start: datetime, length: timedelta, app: tuple[str, str], title: str,
               device: str = "seed-windows") -> datetime:
        return self.add(device, "window", start, start + length, app=app[0], app_id=app[1], title=title)

    def web(self, start: datetime, length: timedelta, domain: str) -> datetime:
        """The desktop Edge window, plus the browser extension's domain-only event for the same time."""
        self.add("seed-browser", "web", start, start + length, app_id="msedge.exe", data={"domain": domain})
        return self.window(start, length, EDGE, f"{domain} - Microsoft Edge")

    def phone(self, start: datetime, length: timedelta, group: str) -> datetime:
        app, package = self.rng.choice(PHONE_APPS[group])
        return self.add("seed-android", "app_session", start, start + length, app=app, app_id=package)

    def iphone(self, start: datetime, length: timedelta, app: str) -> datetime:
        self.add("seed-iphone", "app_open", start, app=app)
        return self.add("seed-iphone", "app_close", start + length, app=app)

    def meal(self, when: datetime, meal_type: str) -> None:
        items = self.rng.choice(MEALS[meal_type])
        self.add("seed-iphone", "meal", when, data={"text": " and ".join(items), "items": items, "meal_type": meal_type})

    def calendar(self, start: datetime, end: datetime, title: str, slug: str) -> None:
        self.add("seed-iphone", "calendar_event", start, end, title=title, data={"all_day": False},
                 external_id=f"cal:{self.plan.day.isoformat()}:{slug}")

    # --- the day ---

    def build(self) -> list[dict[str, Any]]:
        plan, weekday = self.plan, self.plan.day.weekday() < 5
        # A late night ends late and pushes the next morning back; same arithmetic on both days so they meet.
        wake = self.at("07:05", 15) + timedelta(minutes=plan.late_before)
        sleep_start = self.at("23:25", day_offset=-1) + timedelta(minutes=plan.late_before)
        bedtime = self.at("23:20") + timedelta(minutes=plan.late_after)
        self.add("seed-android", "sleep", sleep_start, wake, data={"stage": "asleep", "measured": True},
                 external_id=f"sleep:{plan.day.isoformat()}")
        self.add("seed-android", "screen_on", wake + timedelta(minutes=2))
        t = self.phone(wake + timedelta(minutes=3), self.minutes(8, 18), "social")
        t = self.phone(t + timedelta(minutes=1), self.minutes(3, 8), "comms")
        self.add("seed-android", "app_session", t + timedelta(minutes=1), t + self.minutes(2, 4),
                 app="Samsung Health", app_id="com.sec.android.app.shealth")  # checking last night's sleep
        self.meal(wake + timedelta(minutes=35), "breakfast")

        self._work(max(wake + timedelta(minutes=80), self.at("09:00")), self.at("12:30", 5))
        self.add("seed-windows", "afk", self.at("12:31"), self.at("13:20", 5))
        self.meal(self.at("12:40", 10), "lunch")
        self.phone(self.at("12:50", 5), self.minutes(10, 20), "video")
        self._work(self.at("13:25", 5), self.at("14:55"))

        if weekday:
            topic = STUDY_TOPICS[plan.day.toordinal() % len(STUDY_TOPICS)]
            self.calendar(self.at("15:00"), self.at("17:00"), f"Study: {topic}", "study")
            self._study(self.at("15:05", 3), self.at("16:55"))
            if plan.day.weekday() in (0, 2):
                self.calendar(self.at("11:00"), self.at("11:30"), "Team sync", "sync")
        else:
            self.window(self.at("15:30", 20), self.minutes(40, 70), ("Notion", "notion.id"), "Weekly review",
                        device="seed-mac")

        self.meal(self.at("19:10", 15), "dinner")
        self._evening(self.at("19:45", 10), self.at("22:30", 10), weekday)
        self.phone(self.at("22:45", 3), self.minutes(8, 12), "social")
        if plan.late_after:
            self._late_night(self.at("23:20"), bedtime)
        self.add("seed-android", "screen_off", bedtime)
        steps = int(self.rng.randint(6500, 11500) * (0.4 + 0.6 * plan.focus_score))
        self.add("seed-iphone", "steps", self.at("00:00"), self.at("21:00"), data={"count": steps},
                 external_id=f"steps:{plan.day.isoformat()}")
        return self.events

    def _work(self, t: datetime, until: datetime) -> None:
        focus, score = self.plan.focus, self.plan.focus_score
        block = (int(8 + 32 * score), int(18 + 62 * score))  # 40-80 min on a sharp day, 11-24 after a bad night
        while t < until - timedelta(minutes=5):
            file = self.rng.choice(FILES)
            app = FIGMA if file.endswith(".tsx") and self.rng.random() < 0.3 else CODE
            t = self.window(t, min(self.minutes(*block), until - t), app, f"{file} - daytrace - {app[0]}")
            if t >= until:
                break
            if focus == "good":
                t = self.web(t, self.minutes(3, 8), self.rng.choice(WORK_SITES))
                if self.rng.random() < 0.5:
                    t = self.window(t, self.minutes(2, 4), SLACK, "general - Slack")
            elif focus == "ok":
                t = self.web(t, self.minutes(4, 10), self.rng.choice(WORK_SITES + ("youtube.com",)))
                t = self.window(t, self.minutes(2, 6), self.rng.choice((SLACK, DISCORD)), "chat")
            else:
                t = self.web(t, self.minutes(8, 18), self.rng.choice(FUN_SITES))
                pickup = self.minutes(6, 14)  # on the phone: the desk sits idle
                self.add("seed-windows", "afk", t, t + pickup)
                t = self.phone(t + timedelta(seconds=20), pickup - timedelta(seconds=40), "social")
                t = self.window(t + timedelta(seconds=30), self.minutes(3, 8), DISCORD, "gaming - Discord")

    def _study(self, t: datetime, until: datetime) -> None:
        mac = "seed-mac"
        t = self.window(t, self.minutes(20, 30), ("Anki", "net.ankiweb.dtop"), "Anki", device=mac)
        if self.plan.focus == "poor":
            t = self.iphone(t + timedelta(minutes=1), self.minutes(12, 20), "TikTok")  # during the study block
        notes = ("Notability", "com.gingerlabs.notability")
        t = self.window(t + timedelta(minutes=1), self.minutes(30, 45), notes, "Lecture notes", device=mac)
        t = self.window(t, self.minutes(8, 15), ("Safari", "com.apple.Safari"), "Khan Academy", device=mac)
        if t < until:
            self.window(t, min(self.minutes(15, 30), until - t), notes, "Practice problems", device=mac)

    def _evening(self, t: datetime, until: datetime, weekday: bool) -> None:
        started = t
        if not weekday or self.rng.random() < 0.35:
            game = self.rng.choice((MINECRAFT, STEAM))
            t = self.window(t, self.minutes(60, 110), game, game[0])
        else:
            t = self.web(t, self.minutes(45, 90), "netflix.com")
        # A second screen: a quick look at the phone while the show or game runs on the PC.
        self.iphone(started + self.minutes(20, 30), self.minutes(4, 9), "Instagram")
        for _ in range(self.rng.randint(2, 3)):
            if t >= until:
                break
            t = self.iphone(t + self.minutes(5, 20), self.minutes(5, 14), self.rng.choice(("Instagram", "TikTok")))
        self.phone(t + timedelta(minutes=5), self.minutes(6, 12), "comms")

    def _late_night(self, t: datetime, until: datetime) -> None:
        while t < until - timedelta(minutes=10):
            t = self.phone(t, min(self.minutes(15, 35), until - t), self.rng.choice(("social", "video")))
            t += timedelta(minutes=self.rng.randint(0, 2))


def generate(days: int, tz: tzinfo, now: datetime) -> list[dict[str, Any]]:
    """Every seed event for the `days` days ending today (in tz), nothing after `now`, with seq assigned."""
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    today = now.astimezone(tz).date()
    raw = [event for plan in plan_days(today, days) for event in _Day(plan, tz).build()]
    kept = list(_up_to(raw, now))
    kept.sort(key=lambda e: (datetime.fromisoformat(e["start"]), e["device_id"], e["kind"]))  # real time, not text
    counters: dict[str, int] = defaultdict(int)
    for event in kept:
        if event["device_id"] in NUMBERED:
            event["seq"] = counters[event["device_id"]]
            counters[event["device_id"]] += 1
    return kept


def _up_to(events: list[dict[str, Any]], now: datetime) -> Iterator[dict[str, Any]]:
    """Drop what has not happened yet; spans running at `now` end at `now` (a step total shrinks with them)."""
    for event in events:
        start = datetime.fromisoformat(event["start"])
        if start >= now:
            continue
        if "end" in event and (end := datetime.fromisoformat(event["end"])) > now:
            event = {**event, "end": now.astimezone(start.tzinfo).isoformat()}
            if event["kind"] == "steps":
                share = (now - start) / (end - start)
                event["data"] = {"count": int(event["data"]["count"] * share)}
        yield event


def seed(database: Database, profile: str, days: int, tz: tzinfo, now: datetime | None = None) -> SeedResult:
    """Replace the seed devices' seed events with a fresh `days`-day history. Refuses the personal profile."""
    if profile in REFUSED_PROFILES:
        raise ValueError(f"the {profile} profile holds your real data and is never seeded; use demo or shared-dev")
    now = now or datetime.now(UTC)
    events = [Event.model_validate(event) for event in generate(days, tz, now)]
    by_device: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_device[event.device_id].append(event)
    database.initialize()
    with database.connect() as conn, transaction(conn):
        paired_at = utc_text(datetime.now(UTC))
        for device_id, (device_type, name) in DEVICES.items():
            conn.execute(
                "INSERT INTO devices (device_id, name, device_type, token_hash, paired_at) VALUES (?, ?, ?, NULL, ?)"
                " ON CONFLICT (device_id) DO UPDATE SET name = excluded.name, device_type = excluded.device_type",
                (device_id, name, device_type, paired_at),
            )
        conn.execute(
            f"DELETE FROM events WHERE source = ? AND device_id IN ({', '.join('?' for _ in DEVICES)})",
            (SOURCE, *DEVICES),
        )
        for device_id, device_events in by_device.items():
            stored = store_events(conn, device_id, list(enumerate(device_events)))
            if stored.rejected:
                raise RuntimeError(f"seed data was rejected for {device_id}: {stored.rejected[0].reason}")
    today = now.astimezone(tz).date()
    return SeedResult(
        first_day=today - timedelta(days=days - 1),
        last_day=today,
        events={device_id: len(device_events) for device_id, device_events in sorted(by_device.items())},
    )
