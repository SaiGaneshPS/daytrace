"""DT-38: stats engine: every number the app shows is computed here, never by the LLM.

Charts, streaks, the AI's facts and the phone widget all come from these functions, so they all agree. They
build on the session rules of sessions.py (no double counting on one device, AFK cut out, whole seconds, split
at local midnight) and add the views across devices:

- Screen time is counted per device and added up, as on the timeline; a browser extension's lane is detail
  inside the desktop and never counted again. Desktop browser time is credited to the site the extension saw
  at the same moment, so YouTube in Edge counts as video rather than as "Microsoft Edge". Every grouping (app,
  category, device, hour, day) splits the same pieces, so they all add up to the same total.
- Every result is a plain dict with `unit`, `range` (start, end, tz), `source` (real, seed or mixed) and
  `estimated`: the `meta` the API documents.
- A day or device with no data at all is reported as missing (`None`, or listed under `missing`), never as
  zero. Zero means "data arrived, and none of it counted".

`Stats` works in one time zone and remembers each window it has loaded, so a page of charts reads every day
once.
"""
from __future__ import annotations

import itertools
import math
import sqlite3
import warnings
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import cached_property
from typing import Any, Literal

from scipy import stats as scipy_stats

from .api.timeline import DETAIL_ONLY_TYPES, NOT_ASLEEP, day_window, union_seconds
from .categories import Categorizer
from .sessions import Session, StoredEvent, build_sessions, load_events, snap, with_categories

GroupBy = Literal["app", "category", "device", "hour", "day"]
GROUPINGS: tuple[str, ...] = ("app", "category", "device", "hour", "day")
PRODUCTIVE = frozenset({"work", "study"})
DISTRACTING = frozenset({"social", "video", "games"})
ON_PLAN = frozenset({"work", "study", "comms"})  # a calendar block is kept by working, studying or meeting
PHONE_TYPES = frozenset({"android", "ios"})
DESK_TYPES = frozenset({"windows", "macos"})
COUNTED_TYPES = frozenset({"windows", "macos", "android", "ios"})
# Desktop browsers whose time the browser extension (DT-18) can credit to sites.
BROWSER_APP_IDS = frozenset({
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
    "com.microsoft.edgemac", "com.google.chrome", "org.mozilla.firefox", "com.apple.safari", "com.brave.browser",
    "company.thebrowser.browser",
})
FOCUS_BLOCK = timedelta(minutes=10)  # the shortest stretch of work that counts as focus
FOCUS_JOIN = timedelta(seconds=60)  # a gap this short (switching windows) does not end a block
SWITCH_GAP = timedelta(minutes=5)  # coming back to the screen later is not a switch
PICKUP_REST = timedelta(seconds=60)  # the phone rested at least this long before being picked up again
LATE_FROM, LATE_UNTIL = time(23, 0), time(3, 0)
SLEEP_FROM, SLEEP_UNTIL = time(21, 0), time(12, 0)
MAX_DAYS = 366
CAVEAT = (
    "Correlation, not cause: days with more late-night screen time were followed by different focus, but other "
    "things (deadlines, illness, weekends) can explain both."
)

Interval = tuple[datetime, datetime]


def minutes(seconds: float) -> float:
    return round(seconds / 60, 2)


def merge(intervals: Iterable[Interval], join: timedelta = timedelta(0)) -> list[Interval]:
    """Sorted, non-overlapping intervals; ones closer than `join` become one (the gap included)."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= join:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(intervals: Iterable[Interval], cuts: Iterable[Interval]) -> list[Interval]:
    """`intervals` with every moment covered by `cuts` taken out."""
    cuts = merge(cuts)
    result: list[Interval] = []
    for start, end in merge(intervals):
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def seconds_of(intervals: Iterable[Interval]) -> int:
    return round(sum((end - start).total_seconds() for start, end in intervals))


def attribute_browser_time(sessions: Sequence[Session], web: Sequence[Session], device_types: dict[str, str]) -> list[Session]:
    """Desktop browser sessions split into the sites the browser extension saw at the same moment (the rest stays
    the browser). The pieces keep the desktop's device, so per-device totals do not change, and they partition
    each session exactly, so no total changes either: only what the time is credited to."""
    result: list[Session] = []
    for session in sessions:
        app_id = (session.app_id or "").lower()
        if session.kind != "app" or device_types.get(session.device_id) not in DESK_TYPES or app_id not in BROWSER_APP_IDS:
            result.append(session)
            continue
        cursor = session.start
        overlapping = sorted(
            (w for w in web if w.start < session.end and w.end > session.start and (w.app_id or app_id).lower() == app_id),
            key=lambda w: w.start,
        )
        for site in overlapping:
            start, end = max(site.start, cursor), min(site.end, session.end)
            if end <= start:
                continue
            if start > cursor:
                result.append(replace(session, start=cursor, end=start))
            result.append(replace(
                session, start=start, end=end, app=site.app, app_id=site.app_id, title=None, category=site.category,
                kind="web", estimated=session.estimated or site.estimated, event_ids=session.event_ids + site.event_ids,
            ))
            cursor = end
        if cursor < session.end:
            result.append(replace(session, start=cursor, end=session.end))
    return result


@dataclass
class Window:
    """Everything loaded for one stretch of time [start, end), UTC."""

    start: datetime
    end: datetime
    events: list[StoredEvent]  # as load_events returns them: with context from just outside the window
    sessions: list[Session]  # categorized, every device, clipped to [start, min(end, now))
    device_types: dict[str, str]
    _device_data: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._device_data = {e.device_id for e in self.events if self.overlaps(e)}

    def overlaps(self, event: StoredEvent) -> bool:
        if event.end is None:
            return self.start <= event.start < self.end
        return event.start < self.end and event.end > self.start

    @cached_property
    def counted(self) -> list[Session]:
        """Sessions that count as screen time (every device but browser-extension lanes)."""
        return [s for s in self.sessions if self.device_types.get(s.device_id) not in DETAIL_ONLY_TYPES]

    @cached_property
    def pieces(self) -> list[Session]:
        """The counted sessions, with desktop browser time credited to sites."""
        web = [s for s in self.sessions if self.device_types.get(s.device_id) in DETAIL_ONLY_TYPES]
        return attribute_browser_time(self.counted, web, self.device_types)

    def has_data(self, device_id: str) -> bool:
        return device_id in self._device_data

    @property
    def counted_devices_with_data(self) -> set[str]:
        return {d for d in self._device_data if self.device_types.get(d) in COUNTED_TYPES}

    def type_of(self, session: Session) -> str | None:
        return self.device_types.get(session.device_id)


class Stats:
    """The stats of one hub database in one time zone. `now` bounds everything (nothing is counted after it)."""

    def __init__(self, conn: sqlite3.Connection, tz: tzinfo, tz_name: str, now: datetime | None = None) -> None:
        self._conn = conn
        self.tz = tz
        self.tz_name = tz_name
        self.now = (now or datetime.now(UTC)).astimezone(UTC)
        self._windows: dict[Interval, Window] = {}
        self._categorizer = Categorizer.from_db(conn)
        rows = conn.execute("SELECT device_id, device_type, revoked_at FROM devices").fetchall()
        self._device_types = {row["device_id"]: row["device_type"] for row in rows}
        self._active = {row["device_id"] for row in rows if row["revoked_at"] is None}

    # --- loading ---------------------------------------------------------------------------------------------

    def window(self, start: datetime, end: datetime) -> Window:
        key = (start, end)
        if key not in self._windows:
            events = load_events(self._conn, start, end)
            until = min(end, self.now)
            sessions = with_categories(build_sessions(events, start, until), self._categorizer) if until > start else []
            self._windows[key] = Window(start, end, events, sessions, self._device_types)
        return self._windows[key]

    def day(self, day: date) -> Window:
        return self.window(*day_window(day, self.tz))

    def at(self, day: date, clock: time) -> datetime:
        """A local wall-clock time on `day`, in UTC."""
        return datetime.combine(day, clock, tzinfo=self.tz).astimezone(UTC)

    def _expected_devices(self, windows: Sequence[Window], types: frozenset[str] = COUNTED_TYPES) -> set[str]:
        """Devices that should have sent something: paired and not revoked, or that did send something."""
        sent = {d for w in windows for d in w.counted_devices_with_data}
        return {d for d in self._active | sent if self._device_types.get(d) in types}

    def _meta(self, start: datetime, end: datetime, windows: Sequence[Window], estimated: bool,
              unit: str = "minutes") -> dict[str, Any]:
        sources = {e.source for w in windows for e in w.events if w.overlaps(e)}
        return {
            "unit": unit,
            "range": {"start": start.astimezone(self.tz).isoformat(), "end": end.astimezone(self.tz).isoformat(),
                      "tz": self.tz_name},
            "source": "seed" if sources == {"seed"} else "mixed" if "seed" in sources else "real",
            "estimated": estimated,
        }

    def _days(self, first: date, last: date) -> list[date]:
        if last < first:
            raise ValueError("the last day must not be before the first")
        count = (last - first).days + 1
        if count > MAX_DAYS:
            raise ValueError(f"at most {MAX_DAYS} days at a time")
        return [first + timedelta(days=i) for i in range(count)]

    # --- totals ----------------------------------------------------------------------------------------------

    def totals(self, first: date, last: date | None = None, group_by: GroupBy = "app") -> dict[str, Any]:
        """Screen time from `first` to `last` (inclusive, local days), grouped by app, category, device, local
        hour of the day (00 to 23, all days together) or day. Every grouping adds up to `total_seconds`.

        `missing` lists, per device, the days it sent nothing at all (devices that are paired, or that sent
        something in the range). A day on which no device sent anything is absent from the "day" grouping and
        listed in `missing_days`.
        """
        if group_by not in GROUPINGS:
            raise ValueError(f"group_by must be one of {', '.join(GROUPINGS)}")
        days = self._days(first, last or first)
        windows = [self.day(d) for d in days]
        expected = self._expected_devices(windows)
        by_key: dict[str, int] = defaultdict(int)
        missing: dict[str, list[str]] = defaultdict(list)
        missing_days: list[str] = []
        for day, window in zip(days, windows, strict=True):
            for device in sorted(expected - window.counted_devices_with_data):
                missing[device].append(day.isoformat())
            if not window.counted_devices_with_data:
                missing_days.append(day.isoformat())
            for piece in window.pieces:
                for key, seconds in self._keys(piece, group_by, day):
                    by_key[key] += seconds
        total = sum(s.seconds for w in windows for s in w.counted)
        order = (lambda kv: kv[0]) if group_by in ("hour", "day") else (lambda kv: (-kv[1], kv[0]))
        items = [{"key": key, "seconds": seconds, "minutes": minutes(seconds)} for key, seconds in sorted(by_key.items(), key=order)]
        return {
            "group_by": group_by,
            "items": items,
            "total_seconds": total,
            "total_minutes": minutes(total),
            "days": len(days),
            "missing": dict(missing),
            "missing_days": missing_days,
            **self._meta(windows[0].start, windows[-1].end, windows, any(s.estimated for w in windows for s in w.counted)),
        }

    def _keys(self, piece: Session, group_by: str, day: date) -> list[tuple[str, int]]:
        if group_by == "app":
            return [(piece.app or piece.app_id or "unknown", piece.seconds)]
        if group_by == "category":
            return [(piece.category or "other", piece.seconds)]
        if group_by == "device":
            return [(piece.device_id, piece.seconds)]
        if group_by == "day":
            return [(day.isoformat(), piece.seconds)]
        return self._by_hour(piece.start, piece.end)

    def _by_hour(self, start: datetime, end: datetime) -> list[tuple[str, int]]:
        """Seconds per local hour of the day. Hour boundaries follow the local clock (Newfoundland's are at :30
        UTC), so the pieces stay whole seconds and add up exactly."""
        parts: list[tuple[str, int]] = []
        cursor = start
        while cursor < end:
            local = cursor.astimezone(self.tz)
            boundary = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(UTC)
            if boundary <= cursor:  # a DST change: move on by an hour of real time
                boundary = cursor + timedelta(hours=1)
            stop = min(boundary, end)
            parts.append((f"{local.hour:02d}", round((stop - cursor).total_seconds())))
            cursor = stop
        return parts

    # --- focus -----------------------------------------------------------------------------------------------

    def _missing_day(self, window: Window, unit: str = "minutes", **fields: Any) -> dict[str, Any]:
        return {"value": None, "missing": True, **fields, **self._meta(window.start, window.end, [window], False, unit)}

    def focused_minutes(self, day: date) -> dict[str, Any]:
        """Work and study time in blocks of 10 minutes or more with no phone distraction.

        Work and study are sessions in those categories on any device (overlaps counted once). Time with a
        social, video or game app open on a phone is taken out, and what is left is grouped into blocks
        (gaps up to 60 s, a window switch, do not end a block). A block counts when it spans 10 minutes or
        more; its work time (not its short gaps) is the focused time.
        """
        window = self.day(day)
        if not window.counted_devices_with_data:
            return self._missing_day(window, blocks=[])
        work = merge((s.start, s.end) for s in window.pieces if s.category in PRODUCTIVE)
        distractions = [(s.start, s.end) for s in window.pieces if window.type_of(s) in PHONE_TYPES and s.category in DISTRACTING]
        kept = subtract_intervals(work, distractions)
        blocks: list[dict[str, Any]] = []
        focused = 0
        for block_start, block_end in merge(kept, join=FOCUS_JOIN):
            if block_end - block_start < FOCUS_BLOCK:
                continue
            seconds = seconds_of((max(a, block_start), min(b, block_end)) for a, b in kept if a < block_end and b > block_start)
            focused += seconds
            blocks.append({"start": block_start.astimezone(self.tz).isoformat(), "end": block_end.astimezone(self.tz).isoformat(),
                           "seconds": seconds, "minutes": minutes(seconds)})
        estimated = any(s.estimated for s in window.pieces)
        return {"value": minutes(focused), "seconds": focused, "missing": False, "blocks": blocks,
                **self._meta(window.start, window.end, [window], estimated)}

    def focus_score(self, day: date) -> dict[str, Any]:
        """How much of the day's work-or-distraction time was deep focus, from 0 to 100.

            focus_score = round(100 x focused / (work_or_study + distracted))

        - focused: focused_minutes() (work or study in blocks of 10+ minutes, no phone distraction)
        - work_or_study: time in work or study apps and sites, any device (overlaps counted once)
        - distracted: time in social, video or game apps and sites, any device (overlaps counted once)

        focused is part of work_or_study, so the score never exceeds 100. A day with neither work nor
        distraction has no score (None, with a reason), which is not the same as a score of 0.
        """
        window = self.day(day)
        focus = self.focused_minutes(day)
        if focus["missing"]:
            return self._missing_day(window, unit="score")
        work = seconds_of(merge((s.start, s.end) for s in window.pieces if s.category in PRODUCTIVE))
        distracted = seconds_of(merge((s.start, s.end) for s in window.pieces if s.category in DISTRACTING))
        denominator = work + distracted
        value = round(100 * focus["seconds"] / denominator) if denominator else None
        return {
            "value": value,
            "missing": False,
            "reason": None if denominator else "no work, study or distraction time this day",
            "focused_seconds": focus["seconds"],
            "work_or_study_seconds": work,
            "distracted_seconds": distracted,
            "formula": "round(100 x focused / (work_or_study + distracted))",
            **self._meta(window.start, window.end, [window], focus["estimated"], unit="score"),
        }

    def switches_per_hour(self, day: date) -> dict[str, Any]:
        """App switches per hour of screen time. A switch is one session followed by a different app or site on
        the same device within 5 minutes (switching tabs between sites counts; coming back later does not).
        `by_hour` counts switches by the local hour they happened in."""
        window = self.day(day)
        if not window.counted_devices_with_data:
            return self._missing_day(window, unit="switches per hour", switches=None, by_hour={})
        by_device: dict[str, list[Session]] = defaultdict(list)
        for piece in window.pieces:
            by_device[piece.device_id].append(piece)
        switches = 0
        by_hour: dict[str, int] = defaultdict(int)
        for pieces in by_device.values():
            pieces.sort(key=lambda s: s.start)
            for before, after in itertools.pairwise(pieces):
                if after.start - before.end <= SWITCH_GAP and (after.app, after.app_id, after.kind) != (before.app, before.app_id, before.kind):
                    switches += 1
                    by_hour[f"{after.start.astimezone(self.tz).hour:02d}"] += 1
        active = union_seconds([(s.start, s.end) for s in window.counted])
        rate = round(switches / (active / 3600), 1) if active else None
        return {"value": rate, "missing": False, "switches": switches, "active_seconds": active,
                "by_hour": dict(sorted(by_hour.items())),
                **self._meta(window.start, window.end, [window], False, unit="switches per hour")}

    def pickups(self, day: date) -> dict[str, Any]:
        """How many times a phone was picked up: a phone app coming into use after the phone rested for a
        minute or more (the first use of the day counts). Glancing at the lock screen without opening an app
        is not counted. Phones that sent nothing this day are listed under `missing`."""
        window = self.day(day)
        phones = self._expected_devices([window], PHONE_TYPES)
        with_data = sorted(d for d in phones if window.has_data(d))
        if not with_data:
            return self._missing_day(window, unit="pickups", by_device={}, missing_devices=sorted(phones))
        by_device: dict[str, int] = {}
        for device in with_data:
            sessions = sorted((s for s in window.counted if s.device_id == device), key=lambda s: s.start)
            count, last_end = 0, None
            for session in sessions:
                if last_end is None or session.start - last_end >= PICKUP_REST:
                    count += 1
                last_end = session.end if last_end is None else max(last_end, session.end)
            by_device[device] = count
        estimated = any(s.estimated for s in window.counted if s.device_id in by_device)
        return {"value": sum(by_device.values()), "missing": False, "by_device": by_device,
                "missing_devices": sorted(set(phones) - set(with_data)),
                **self._meta(window.start, window.end, [window], estimated, unit="pickups")}

    def late_night_minutes(self, day: date) -> dict[str, Any]:
        """Screen time on any device from 23:00 on `day` to 03:00 the next morning (overlaps counted once), the
        night that can cost the next day's focus. None when no device sent anything that day or night."""
        start, end = self.at(day, LATE_FROM), self.at(day + timedelta(days=1), LATE_UNTIL)
        night = self.window(start, end)
        if not night.counted_devices_with_data and not self.day(day).counted_devices_with_data:
            return self._missing_day(night, by_device={})
        seconds = union_seconds([(s.start, s.end) for s in night.counted])
        by_device: dict[str, int] = defaultdict(int)
        for session in night.counted:
            by_device[session.device_id] += session.seconds
        estimated = any(s.estimated for s in night.counted)
        return {"value": minutes(seconds), "seconds": seconds, "missing": False, "by_device": dict(by_device),
                **self._meta(start, end, [night], estimated)}

    def sleep_estimate(self, day: date) -> dict[str, Any]:
        """Last night's sleep: the night that ends on the morning of `day`.

        From health data when there is any (sleep that ended between 21:00 the evening before and 12:00;
        stages that are not sleep, such as awake or in bed, are left out, and the same night from two phones
        counts once). Otherwise estimated: the longest stretch between 21:00 and 12:00 with no screen in use
        on any device (a computer in use means you were awake too). None when no device sent anything then.
        """
        start = self.at(day - timedelta(days=1), SLEEP_FROM)
        end = self.at(day, SLEEP_UNTIL)
        night = self.window(start, end)
        seen: set[tuple[datetime, datetime, object]] = set()
        asleep: list[Interval] = []
        measured = True
        for event in night.events:
            if event.kind != "sleep" or event.end is None or not start < event.end <= end:
                continue
            stage = event.data.get("stage")
            key = (event.start, event.end, stage)
            if key in seen or stage in NOT_ASLEEP:
                continue
            seen.add(key)
            asleep.append((snap(event.start), snap(event.end)))
            measured = measured and event.data.get("measured") is not False
        if asleep:
            spans = merge(asleep)
            seconds = seconds_of(spans)
            return {
                "value": minutes(seconds), "seconds": seconds, "missing": False, "method": "health",
                "measured": measured, "start": spans[0][0].astimezone(self.tz).isoformat(),
                "end": spans[-1][1].astimezone(self.tz).isoformat(),
                **self._meta(start, end, [night], not measured),
            }
        if not night.counted_devices_with_data:
            return self._missing_day(night, method=None)
        until = min(end, self.now)
        busy = merge((s.start, s.end) for s in night.counted)
        gaps = subtract_intervals([(start, until)], busy) if until > start else []
        if not gaps:
            return {"value": 0.0, "seconds": 0, "missing": False, "method": "idle_gap", "measured": False,
                    "start": None, "end": None, **self._meta(start, end, [night], True)}
        gap_start, gap_end = max(gaps, key=lambda gap: gap[1] - gap[0])
        seconds = seconds_of([(gap_start, gap_end)])
        return {
            "value": minutes(seconds), "seconds": seconds, "missing": False, "method": "idle_gap", "measured": False,
            "start": gap_start.astimezone(self.tz).isoformat(), "end": gap_end.astimezone(self.tz).isoformat(),
            **self._meta(start, end, [night], True),
        }

    # --- plans -----------------------------------------------------------------------------------------------

    def planned_vs_actual(self, day: date) -> dict[str, Any]:
        """For each calendar block of the day (not all-day ones), what the time went to, moment by moment:
        on plan (work, study or a meeting app), off plan (social, video or games, which win when both are on
        screen), other screen time, or no screen at all. The four always add up to the block's length. A block
        that has not ended yet counts up to now. The same event from two phones is listed once."""
        window = self.day(day)
        until = min(window.end, self.now)
        seen: set[tuple[str | None, datetime, datetime]] = set()
        blocks: list[dict[str, Any]] = []
        totals = {"planned": 0, "on_plan": 0, "off_plan": 0, "other": 0, "idle": 0}
        for event in sorted(window.events, key=lambda e: e.start):
            if event.kind != "calendar_event" or event.end is None or event.data.get("all_day") is True:
                continue
            key = (event.title, event.start, event.end)
            if key in seen or not window.overlaps(event):
                continue
            seen.add(key)
            start, end = max(snap(event.start), window.start), min(snap(event.end), window.end, until)
            if end <= start:
                continue
            parts = self._block_parts(window.pieces, start, end)
            planned = round((end - start).total_seconds())
            blocks.append({
                "title": event.title, "start": start.astimezone(self.tz).isoformat(), "end": end.astimezone(self.tz).isoformat(),
                "planned_seconds": planned, "in_progress": snap(event.end) > until, **{f"{k}_seconds": v for k, v in parts.items()},
                "on_plan_pct": round(100 * parts["on_plan"] / planned) if planned else None,
            })
            totals["planned"] += planned
            for name, seconds in parts.items():
                totals[name] += seconds
        return {
            "value": round(100 * totals["on_plan"] / totals["planned"]) if totals["planned"] else None,
            "blocks": blocks,
            "totals_seconds": totals,
            "totals_minutes": {name: minutes(seconds) for name, seconds in totals.items()},
            **self._meta(window.start, window.end, [window], any(s.estimated for s in window.pieces), unit="percent"),
        }

    @staticmethod
    def _block_parts(pieces: Sequence[Session], start: datetime, end: datetime) -> dict[str, int]:
        inside = [s for s in pieces if s.start < end and s.end > start]
        points = sorted({start, end, *(max(s.start, start) for s in inside), *(min(s.end, end) for s in inside)})
        parts = {"on_plan": 0, "off_plan": 0, "other": 0, "idle": 0}
        for left, right in itertools.pairwise(points):
            active = {s.category or "other" for s in inside if s.start < right and s.end > left}
            name = "off_plan" if active & DISTRACTING else "on_plan" if active & ON_PLAN else "other" if active else "idle"
            parts[name] += round((right - left).total_seconds())
        return parts

    # --- patterns --------------------------------------------------------------------------------------------

    def correlation(self, first: date, last: date) -> dict[str, Any]:
        """Late nights against the next day's focus: Spearman's rank correlation between late_night_minutes(d)
        and focus_score(d + 1) for every night d from `first` to the night before `last` where both are known.
        rho runs from -1 (later nights, less focus) to 1; p says how likely a pattern this strong is by chance.
        Needs 3 pairs or more (rho and p are None otherwise, or when either side never varies)."""
        days = self._days(first, last)
        pairs: list[dict[str, Any]] = []
        for night in days[:-1]:
            late = self.late_night_minutes(night)
            focus = self.focus_score(night + timedelta(days=1))
            if late["value"] is None or focus["value"] is None:
                continue
            pairs.append({"night": night.isoformat(), "late_minutes": late["value"],
                          "next_day": (night + timedelta(days=1)).isoformat(), "focus_score": focus["value"]})
        rho = p = None
        if len(pairs) >= 3:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # a side that never varies: SciPy warns and returns nan
                result = scipy_stats.spearmanr([pair["late_minutes"] for pair in pairs], [pair["focus_score"] for pair in pairs])
            statistic, pvalue = float(result.statistic), float(result.pvalue)
            if not (math.isnan(statistic) or math.isnan(pvalue)):
                rho, p = round(statistic, 3), round(pvalue, 4)
        windows = [self.day(d) for d in days]
        return {
            "rho": rho, "p": p, "n": len(pairs), "pairs": pairs, "caveat": CAVEAT,
            **self._meta(windows[0].start, windows[-1].end, windows, False, unit="rank correlation"),
        }
