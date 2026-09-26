"""DT-15: seed data generator (14 days across 4 devices, plus the browser extension).

`python -m daytrace_hub seed --profile demo --days 14` fills a demo profile with a believable student's
fortnight, so the dashboard, charts, streaks, AI and demo work without real devices. Everything is tagged
`source: "seed"`, and the patterns are there on purpose:

- Late nights on the phone lower the next day's focus, in proportion to how late (the correlation the
  Insights page and the AI find).
- A 5-day focus streak that breaks once, after a late night, then starts again.
- Weekday study blocks on the calendar (sometimes with TikTok in the middle), meals, sleep, daily steps.

The data is physically possible: one person does one thing at a time with their hands (the two phones) and at
their desk (the two computers); the only overlaps are a deliberate phone check during the evening show and
phone pickups while the desk sits idle. Time arithmetic is done in UTC, so DST changes (even at midnight)
never produce impossible events.

Re-running is always safe: the seed devices' seed events are replaced in one transaction, so shifting the
window to a new day never duplicates or conflicts. Profiles with real data (personal) are never seeded.
"""
from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from .api.events import store_events
from .config import Settings
from .db import Database, transaction, utc_text
from .models import Event

SOURCE = "seed"
MAX_DAYS = 90
DEVICES: dict[str, tuple[str, str]] = {
    "seed-windows": ("windows", "Desk PC (demo)"),
    "seed-mac": ("macos", "MacBook (demo)"),
    "seed-android": ("android", "Galaxy phone (demo)"),
    "seed-iphone": ("ios", "iPhone (demo)"),
    "seed-browser": ("browser", "Edge extension (demo)"),
}
# One person: one thing at a time in their hands (phones) and at their desk (computers).
LANES = {"seed-windows": "desk", "seed-mac": "desk", "seed-android": "hand", "seed-iphone": "hand"}
# Collectors with a local store number their events; iPhone Shortcuts are stateless and send no seq.
NUMBERED = frozenset({"seed-windows", "seed-mac", "seed-android", "seed-browser"})
GAP = timedelta(seconds=20)  # between two things done in a row
SHORTEST = timedelta(minutes=1)  # anything squeezed below this is left out
Focus = Literal["good", "ok", "poor"]

CODE = ("Visual Studio Code", "Code.exe")
EDGE = ("Microsoft Edge", "msedge.exe")
SLACK = ("Slack", "slack.exe")
DISCORD = ("Discord", "Discord.exe")
FIGMA = ("Figma", "Figma.exe")
MINECRAFT = ("Minecraft", "Minecraft.exe")
STEAM = ("Steam", "steam.exe")
NOTES = ("Notability", "com.gingerlabs.notability")
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


class SeedRefused(ValueError):
    """The profile holds real data (or the request cannot be seeded)."""


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
    return [
        DayPlan(
            day=today - timedelta(days=days - 1 - index),
            index=index,
            late_before=late.get(index, 0),
            late_after=late.get(index + 1, 0),
            focus_score=max(0.1, 1 - late.get(index, 0) / 140),
        )
        for index in range(days)
    ]


class _Day:
    """One day's events. Times are UTC internally; at() turns a local clock time into UTC once."""

    def __init__(self, plan: DayPlan, tz: tzinfo) -> None:
        self.plan = plan
        self.tz = tz
        self.rng = random.Random(f"daytrace-seed:{plan.day.isoformat()}")
        self.events: list[dict[str, Any]] = []
        self.busy: dict[str, datetime] = defaultdict(lambda: datetime.min.replace(tzinfo=UTC))
        self._pairs = 0

    def at(self, clock: str, jitter: int = 0, day_offset: int = 0) -> datetime:
        hours, minutes = (int(part) for part in clock.split(":"))
        local = datetime.combine(self.plan.day + timedelta(days=day_offset), time(hours, minutes), tzinfo=self.tz)
        shift = timedelta(minutes=self.rng.randint(-jitter, jitter)) if jitter else timedelta(0)
        return local.astimezone(UTC) + shift  # arithmetic in UTC from here on: DST never bends durations

    def minutes(self, low: int, high: int) -> timedelta:
        return timedelta(minutes=self.rng.randint(low, high), seconds=self.rng.randint(0, 59))

    def _add(self, device: str, kind: str, start: datetime, end: datetime | None = None, **fields: Any) -> None:
        event = {"device_id": device, "kind": kind, "source": SOURCE, "start": start, **fields}
        if end is not None:
            event["end"] = end
        self.events.append(event)

    def _slot(self, device: str, start: datetime, length: timedelta, limit: datetime | None) -> tuple[datetime, datetime] | None:
        """When this can really happen: after whatever the same hands or desk are doing, and never past `limit`."""
        lane = LANES.get(device)
        if lane is not None:
            start = max(start, self.busy[lane] + GAP)
        end = start + length if limit is None else min(start + length, limit)
        if end - start < SHORTEST:
            return None
        if lane is not None:
            self.busy[lane] = end
        return start, end

    # --- devices ---

    def window(self, start: datetime, length: timedelta, app: tuple[str, str], title: str,
               device: str = "seed-windows", limit: datetime | None = None) -> datetime:
        slot = self._slot(device, start, length, limit)
        if slot is None:
            return start
        self._add(device, "window", *slot, app=app[0], app_id=app[1], title=title)
        return slot[1]

    def web(self, start: datetime, length: timedelta, domain: str, limit: datetime | None = None) -> datetime:
        """The desktop Edge window, plus the browser extension's domain-only event for the same time."""
        slot = self._slot("seed-windows", start, length, limit)
        if slot is None:
            return start
        self._add("seed-windows", "window", *slot, app=EDGE[0], app_id=EDGE[1], title=f"{domain} - Microsoft Edge")
        self._add("seed-browser", "web", *slot, app_id="msedge.exe", data={"domain": domain})
        return slot[1]

    def afk(self, start: datetime, end: datetime) -> datetime:
        """The desk sits idle (the tracker reports AFK); nothing can happen at the desk meanwhile."""
        start = max(start, self.busy["desk"])
        if end - start >= SHORTEST:
            self._add("seed-windows", "afk", start, end)
            self.busy["desk"] = end
        return end

    def phone(self, start: datetime, length: timedelta, group: str, limit: datetime | None = None) -> datetime:
        app, package = self.rng.choice(PHONE_APPS[group])
        return self.android(start, length, app, package, limit)

    def android(self, start: datetime, length: timedelta, app: str, package: str,
                limit: datetime | None = None) -> datetime:
        slot = self._slot("seed-android", start, length, limit)
        if slot is None:
            return start
        self._add("seed-android", "app_session", *slot, app=app, app_id=package)
        return slot[1]

    def iphone(self, start: datetime, length: timedelta, app: str, limit: datetime | None = None) -> datetime:
        slot = self._slot("seed-iphone", start, length, limit)
        if slot is None:
            return start
        self._pairs += 1
        pair = f"{self.plan.day.isoformat()}:{self._pairs}"
        self._add("seed-iphone", "app_open", slot[0], app=app, _pair=pair)
        self._add("seed-iphone", "app_close", slot[1], app=app, _pair=pair)
        return slot[1]

    def meal(self, when: datetime, meal_type: str) -> None:
        items = self.rng.choice(MEALS[meal_type])
        self._add("seed-iphone", "meal", when, data={"text": " and ".join(items), "items": items, "meal_type": meal_type})

    def calendar(self, start: datetime, end: datetime, title: str, slug: str) -> None:
        self._add("seed-iphone", "calendar_event", start, end, title=title, data={"all_day": False},
                  external_id=f"cal:{self.plan.day.isoformat()}:{slug}")

    # --- the day ---

    def build(self) -> list[dict[str, Any]]:
        plan, weekday = self.plan, self.plan.day.weekday() < 5
        # A late night ends late and pushes the next morning back; the same arithmetic on both days lines up.
        sleep_start = self.at("23:25", day_offset=-1) + timedelta(minutes=plan.late_before)
        wake = self.at("07:05", 15) + timedelta(minutes=plan.late_before)
        bedtime = self.at("23:20") + timedelta(minutes=plan.late_after)
        self._add("seed-android", "sleep", sleep_start, wake, data={"stage": "asleep", "measured": True},
                  external_id=f"sleep:{plan.day.isoformat()}")
        self.busy["hand"] = wake  # asleep until then
        self._add("seed-android", "screen_on", wake + timedelta(minutes=2))
        t = self.phone(wake + timedelta(minutes=3), self.minutes(8, 18), "social")
        t = self.phone(t, self.minutes(3, 8), "comms")
        self.android(t, self.minutes(2, 4), "Samsung Health", "com.sec.android.app.shealth")  # last night's sleep
        self.meal(wake + timedelta(minutes=35), "breakfast")

        morning_end = self._work(max(wake + timedelta(minutes=80), self.at("09:00")), self.at("12:30", 5))
        lunch_end = self.afk(morning_end, self.at("13:20", 5))
        self.meal(self.at("12:40", 10), "lunch")
        self.phone(self.at("12:50", 5), self.minutes(10, 20), "video")
        self._work(lunch_end + timedelta(minutes=4), self.at("14:55"))

        if weekday:
            topic = STUDY_TOPICS[plan.day.toordinal() % len(STUDY_TOPICS)]
            self.calendar(self.at("15:00"), self.at("17:00"), f"Study: {topic}", "study")
            self._study(self.at("15:05", 3), self.at("16:55"))
            if plan.day.weekday() in (0, 2):
                self.calendar(self.at("11:00"), self.at("11:30"), "Team sync", "sync")
        else:
            self.window(self.at("15:30", 20), self.minutes(40, 70), ("Notion", "notion.id"), "Weekly review",
                        device="seed-mac", limit=self.at("17:00"))

        self.meal(self.at("19:10", 15), "dinner")
        self._evening(self.at("19:45", 10), self.at("22:30", 10), weekday)
        self.phone(self.at("22:45", 3), self.minutes(8, 12), "social", limit=self.at("23:15"))
        if plan.late_after:
            self._late_night(self.at("23:20"), bedtime)
        self._add("seed-android", "screen_off", max(bedtime, self.busy["hand"]))
        steps = int(self.rng.randint(6500, 11500) * (0.4 + 0.6 * plan.focus_score))
        self._add("seed-iphone", "steps", self.at("00:00"), self.at("21:00"), data={"count": steps},
                  external_id=f"steps:{plan.day.isoformat()}")
        return self.events

    def _work(self, t: datetime, until: datetime) -> datetime:
        """Desk work until `until`; returns when it really ended. Every step stops at `until`."""
        focus, score = self.plan.focus, self.plan.focus_score
        block = (int(8 + 32 * score), int(18 + 62 * score))  # 40-80 min on a sharp day, 11-24 after a bad night
        last = t
        while t < until - SHORTEST:
            file = self.rng.choice(FILES)
            app = FIGMA if file.endswith(".tsx") and self.rng.random() < 0.3 else CODE
            ended = self.window(t, self.minutes(*block), app, f"{file} - daytrace - {app[0]}", limit=until)
            if ended == t:
                break  # no room left before `until`
            t = last = ended
            if focus == "good":
                t = self.web(t, self.minutes(3, 8), self.rng.choice(WORK_SITES), limit=until)
                if self.rng.random() < 0.5:
                    t = self.window(t, self.minutes(2, 4), SLACK, "general - Slack", limit=until)
            elif focus == "ok":
                t = self.web(t, self.minutes(4, 10), self.rng.choice((*WORK_SITES, "youtube.com")), limit=until)
                t = self.window(t, self.minutes(2, 6), self.rng.choice((SLACK, DISCORD)), "chat", limit=until)
            else:
                t = self.web(t, self.minutes(8, 18), self.rng.choice(FUN_SITES), limit=until)
                pickup = min(self.minutes(6, 14), until - t)
                if pickup >= SHORTEST * 3:  # on the phone while the desk sits idle
                    self.afk(t, t + pickup)
                    self.phone(t + GAP, pickup - 2 * GAP, "social", limit=t + pickup)
                    t += pickup
                t = self.window(t, self.minutes(3, 8), DISCORD, "gaming - Discord", limit=until)
            last = max(last, t)
        return max(last, self.busy["desk"])

    def _study(self, t: datetime, until: datetime) -> None:
        mac = "seed-mac"
        t = self.window(t, self.minutes(20, 30), ("Anki", "net.ankiweb.dtop"), "Anki", device=mac, limit=until)
        if self.plan.focus == "poor":
            tiktok_end = self.iphone(t + GAP, self.minutes(12, 20), "TikTok", limit=until)  # during the study block
            t = max(t, tiktok_end)
        t = self.window(t, self.minutes(30, 45), NOTES, "Lecture notes", device=mac, limit=until)
        t = self.window(t, self.minutes(8, 15), ("Safari", "com.apple.Safari"), "Khan Academy", device=mac, limit=until)
        self.window(t, self.minutes(15, 30), NOTES, "Practice problems", device=mac, limit=until)

    def _evening(self, t: datetime, until: datetime, weekday: bool) -> None:
        started = t
        if not weekday or self.rng.random() < 0.35:
            game = self.rng.choice((MINECRAFT, STEAM))
            t = self.window(t, self.minutes(60, 110), game, game[0], limit=until)
        else:
            t = self.web(t, self.minutes(45, 90), "netflix.com", limit=until)
        # A second screen: a quick look at the phone while the show or game runs on the PC.
        self.iphone(started + self.minutes(20, 30), self.minutes(4, 9), "Instagram", limit=until)
        for _ in range(self.rng.randint(2, 3)):
            t = self.iphone(t + self.minutes(5, 20), self.minutes(5, 14), self.rng.choice(("Instagram", "TikTok")),
                            limit=until)
        self.phone(t + timedelta(minutes=5), self.minutes(6, 12), "comms", limit=until)

    def _late_night(self, t: datetime, until: datetime) -> None:
        while t < until - SHORTEST * 5:
            ended = self.phone(t, self.minutes(15, 35), self.rng.choice(("social", "video")), limit=until)
            if ended == t:
                break  # no room left before bedtime
            t = ended + timedelta(minutes=self.rng.randint(0, 2))


def generate(days: int, tz: tzinfo, now: datetime) -> list[dict[str, Any]]:
    """Every seed event for the `days` days ending today (in tz), nothing after `now`, with seq assigned.

    Times come back as ISO strings in tz. The first night's sleep starts the evening before the first day:
    sleep belongs to the morning it ends on.
    """
    if not 1 <= days <= MAX_DAYS:
        raise SeedRefused(f"days must be between 1 and {MAX_DAYS}")
    now = now.astimezone(UTC)
    today = now.astimezone(tz).date()
    raw = [event for plan in plan_days(today, days) for event in _Day(plan, tz).build()]
    kept = sorted(_up_to(raw, now), key=lambda e: (e["start"], e["device_id"], e["kind"]))
    counters: dict[str, int] = defaultdict(int)
    for event in kept:
        event.pop("_pair", None)
        if event["device_id"] in NUMBERED:
            event["seq"] = counters[event["device_id"]]
            counters[event["device_id"]] += 1
        event["start"] = event["start"].astimezone(tz).isoformat()
        if "end" in event:
            event["end"] = event["end"].astimezone(tz).isoformat()
    return kept


def _up_to(events: list[dict[str, Any]], now: datetime) -> Iterator[dict[str, Any]]:
    """Drop what has not happened yet. Spans running at `now` end at `now` (a step total shrinks with them),
    and so does an iPhone app still open at `now`: its close moves to `now`, like a span's end."""
    opened_before_now = {e["_pair"] for e in events if e["kind"] == "app_open" and e["start"] < now}
    for event in events:
        if event["kind"] == "app_close" and event["start"] >= now and event.get("_pair") in opened_before_now:
            yield {**event, "start": now}
            continue
        if event["start"] >= now:
            continue
        if "end" in event and event["end"] > now:
            share = (now - event["start"]) / (event["end"] - event["start"])
            event = {**event, "end": now}
            if event["kind"] == "steps":
                event["data"] = {"count": int(event["data"]["count"] * share)}
        yield event


def seed(settings: Settings, days: int, tz: tzinfo, now: datetime | None = None) -> SeedResult:
    """Replace the seed devices' seed events in this profile's database with a fresh `days`-day history.

    Refuses profiles that hold real data (Profile.seedable is False), whatever the caller names them.
    """
    if not settings.profile.seedable:
        raise SeedRefused(
            f"the {settings.profile.name} profile holds your real data and is never seeded; use demo or shared-dev"
        )
    now = now or datetime.now(UTC)
    events = [Event.model_validate(event) for event in generate(days, tz, now)]
    by_device: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_device[event.device_id].append(event)
    database = Database(settings.database_path)
    database.initialize()
    with database.connect() as conn, transaction(conn):
        _replace(conn, by_device)
    today = now.astimezone(tz).date()
    return SeedResult(
        first_day=today - timedelta(days=days - 1),
        last_day=today,
        events={device_id: len(device_events) for device_id, device_events in sorted(by_device.items())},
    )


def _replace(conn: sqlite3.Connection, by_device: dict[str, list[Event]]) -> None:
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
