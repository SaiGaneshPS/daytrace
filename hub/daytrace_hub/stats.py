"""DT-38: stats engine: every number the app shows is computed here, never by the LLM.

Charts, streaks, the AI's facts and the phone widget all come from these functions, so they all agree. They
build on the session rules of sessions.py (no double counting on one device, AFK cut out, whole seconds, split
at local midnight) and add the views across devices:

- Screen time is counted per device and added up, as on the timeline; a browser extension's lane is detail
  inside the desktop and never counted again. Desktop browser time is credited to the site the extension saw
  at the same moment (each moment of the extension's lane to one desktop browser only), so YouTube in Edge
  counts as video rather than as "Microsoft Edge". Every grouping (app, category, device, hour, day) splits
  the same pieces, so they all add up to the same total.
- Every result is a plain dict with `unit`, `range` (start, end, tz), `source` (real, seed or mixed) and
  `estimated`: the `meta` the API documents.
- A day or device with no screen data at all (no app, window, site, AFK or screen event up to now) is
  reported as missing (`None`, or listed under `missing`), never as zero; a calendar entry or a steps total
  says nothing about the screen. Zero means "screen data arrived, and none of it counted".
- Nothing after `now` counts, and a night or block that is not over yet says so.

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

from .api.timeline import DETAIL_ONLY_TYPES, day_window, minutes, union_seconds
from .categories import Categorizer
from .sessions import Session, StoredEvent, build_sessions, load_events, parse_utc, snap, with_categories

GroupBy = Literal["app", "category", "device", "hour", "day"]
GROUPINGS: tuple[str, ...] = ("app", "category", "device", "hour", "day")
PRODUCTIVE = frozenset({"work", "study"})
DISTRACTING = frozenset({"social", "video", "games"})
PHONE_TYPES = frozenset({"android", "ios"})
DESK_TYPES = frozenset({"windows", "macos"})
COUNTED_TYPES = frozenset({"windows", "macos", "android", "ios"})
# Events that say something about the screen. Only these make a device "have data" for a stretch of time.
SCREEN_KINDS = frozenset({"app_session", "window", "web", "afk", "app_open", "app_close", "screen_on", "screen_off"})
# Desktop browsers whose time the browser extension (DT-18) can credit to sites.
BROWSER_APP_IDS = frozenset({
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
    "com.microsoft.edgemac", "com.google.chrome", "org.mozilla.firefox", "com.apple.safari", "com.brave.browser",
    "company.thebrowser.browser",
})
# Meeting apps and sites: a calendar block spent in one is kept (unlike chatting in other comms apps).
MEETING_MARKERS = ("zoom", "teams", "meet.google.com", "webex", "facetime", "skype", "gotomeeting", "whereby")
FOCUS_BLOCK = timedelta(minutes=10)  # the shortest stretch of work that counts as focus
FOCUS_JOIN = timedelta(seconds=60)  # a gap this short (switching windows) does not end a block
SWITCH_GAP = timedelta(minutes=5)  # coming back to the screen later is not a switch
PICKUP_REST = timedelta(seconds=60)  # the phone rested at least this long before being picked up again
PICKUP_LOOKBACK = timedelta(hours=1)  # enough to see a session running over midnight
LATE_FROM, LATE_UNTIL = time(23, 0), time(3, 0)
SLEEP_FROM, SLEEP_UNTIL = time(21, 0), time(12, 0)
MAX_DAYS = 366
CAVEAT = (
    "Correlation, not cause: days with more late-night screen time were followed by different focus, but other "
    "things (deadlines, illness, weekends) can explain both."
)

Interval = tuple[datetime, datetime]


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
    """`intervals` (merged first) with every moment covered by `cuts` taken out."""
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


def clip_to(intervals: Iterable[Interval], start: datetime, end: datetime) -> list[Interval]:
    return [(max(a, start), min(b, end)) for a, b in intervals if a < end and b > start]


def is_meeting(session: Session) -> bool:
    text = f"{session.app or ''} {session.app_id or ''}".lower()
    return any(marker in text for marker in MEETING_MARKERS)


def attribute_browser_time(sessions: Sequence[Session], web: Sequence[Session], device_types: dict[str, str]) -> list[Session]:
    """Desktop browser sessions split into the sites the browser extension saw at the same moment; the rest stays
    the browser. Each moment of the extension's lane is credited to one desktop browser session only (the most
    recently started, as on one screen), never to every desktop that had a browser open. The pieces keep the
    desktop's device and partition each session exactly, so no total changes: only what the time is credited to."""
    def is_browser(session: Session) -> bool:
        return (session.kind == "app" and device_types.get(session.device_id) in DESK_TYPES
                and (session.app_id or "").lower() in BROWSER_APP_IDS)

    browsers = sorted((s for s in sessions if is_browser(s)), key=lambda s: s.start, reverse=True)
    unclaimed: dict[int, list[Interval]] = {i: [(w.start, w.end)] for i, w in enumerate(web)}
    claims: dict[int, list[tuple[datetime, datetime, Session]]] = defaultdict(list)  # browser index -> site pieces
    for b_index, browser in enumerate(browsers):
        app_id = (browser.app_id or "").lower()
        for w_index, site in enumerate(web):
            if (site.app_id or app_id).lower() != app_id:
                continue
            taken = clip_to(unclaimed[w_index], browser.start, browser.end)
            if not taken:
                continue
            unclaimed[w_index] = subtract_intervals(unclaimed[w_index], taken)
            claims[b_index].extend((start, end, site) for start, end in taken)
    index_of = {id(browser): i for i, browser in enumerate(browsers)}
    result: list[Session] = []
    for session in sessions:
        if id(session) not in index_of:
            result.append(session)
            continue
        cursor = session.start
        for start, end, site in sorted(claims[index_of[id(session)]], key=lambda claim: claim[0]):
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
    """Everything loaded for one stretch of time [start, end), UTC, counted up to `until` (now at the latest)."""

    start: datetime
    end: datetime
    until: datetime
    events: list[StoredEvent]  # as load_events returns them: with context from just outside the window
    sessions: list[Session]  # categorized, every device, clipped to [start, until)
    device_types: dict[str, str]
    _screen_data: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._screen_data = {
            e.device_id for e in self.events if e.kind in SCREEN_KINDS and self.overlaps(e, self.start, self.until)
        }

    @staticmethod
    def overlaps(event: StoredEvent, start: datetime, end: datetime) -> bool:
        if event.end is None:
            return start <= event.start < end
        return event.start < end and event.end > start

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
        """Whether the device sent any screen data for this stretch (up to now)."""
        return device_id in self._screen_data

    @property
    def counted_devices_with_data(self) -> set[str]:
        return {d for d in self._screen_data if self.device_types.get(d) in COUNTED_TYPES}

    def type_of(self, session: Session) -> str | None:
        return self.device_types.get(session.device_id)

    @property
    def over(self) -> bool:
        return self.until >= self.end


@dataclass(frozen=True)
class Device:
    device_id: str
    device_type: str
    paired_at: datetime
    revoked_at: datetime | None

    def expected(self, start: datetime, end: datetime) -> bool:
        """Paired during [start, end): only then can it be missing."""
        return self.paired_at < end and (self.revoked_at is None or self.revoked_at > start)


class Stats:
    """The stats of one hub database in one time zone. `now` (timezone-aware) bounds everything."""

    def __init__(self, conn: sqlite3.Connection, tz: tzinfo, tz_name: str, now: datetime | None = None) -> None:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._conn = conn
        self.tz = tz
        self.tz_name = tz_name
        self.now = (now or datetime.now(UTC)).astimezone(UTC)
        self._windows: dict[Interval, Window] = {}
        self._categorizer = Categorizer.from_db(conn)
        rows = conn.execute("SELECT device_id, device_type, paired_at, revoked_at FROM devices").fetchall()
        self._devices = {
            row["device_id"]: Device(row["device_id"], row["device_type"], parse_utc(row["paired_at"]),
                                     parse_utc(row["revoked_at"]) if row["revoked_at"] else None)
            for row in rows
        }
        self._device_types = {device_id: device.device_type for device_id, device in self._devices.items()}

    # --- loading ---------------------------------------------------------------------------------------------

    def window(self, start: datetime, end: datetime) -> Window:
        key = (start, end)
        if key not in self._windows:
            events = load_events(self._conn, start, end)
            until = max(start, min(end, self.now))
            sessions = with_categories(build_sessions(events, start, until), self._categorizer) if until > start else []
            self._windows[key] = Window(start, end, until, events, sessions, self._device_types)
        return self._windows[key]

    def day(self, day: date) -> Window:
        return self.window(*day_window(day, self.tz))

    def at(self, day: date, clock: time) -> datetime:
        """A local wall-clock time on `day`, in UTC."""
        return datetime.combine(day, clock, tzinfo=self.tz).astimezone(UTC)

    def _expected(self, window: Window, types: frozenset[str] = COUNTED_TYPES) -> set[str]:
        """Devices that should have sent something for this window: paired during it (and not revoked before
        it), or that did send screen data."""
        paired = {d for d, device in self._devices.items() if device.expected(window.start, window.until)}
        return {d for d in paired | window.counted_devices_with_data if self._device_types.get(d) in types}

    def _meta(self, start: datetime, end: datetime, windows: Sequence[Window], estimated: bool,
              unit: str = "minutes") -> dict[str, Any]:
        sources = {e.source for w in windows for e in w.events if w.overlaps(e, w.start, w.until)}
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

    def _local(self, moment: datetime) -> str:
        return moment.astimezone(self.tz).isoformat()

    def _missing(self, window: Window, unit: str = "minutes", **fields: Any) -> dict[str, Any]:
        return {"value": None, "missing": True, **fields, **self._meta(window.start, window.end, [window], False, unit)}

    # --- totals ----------------------------------------------------------------------------------------------

    def totals(self, first: date, last: date | None = None, group_by: GroupBy = "app") -> dict[str, Any]:
        """Screen time from `first` to `last` (inclusive, local days), grouped by app, category, device, local
        hour of the day (00 to 23, all days together) or day. Every grouping adds up to `total_seconds`.

        `missing` lists, per device, the days it sent no screen data although it was paired then. A day on which
        no device sent screen data is absent from the "day" grouping and listed in `missing_days`; a day (or a
        device) that sent data of which nothing counted (all AFK, say) is listed with 0.
        """
        if group_by not in GROUPINGS:
            raise ValueError(f"group_by must be one of {', '.join(GROUPINGS)}")
        days = self._days(first, last or first)
        windows = [self.day(d) for d in days]
        by_key: dict[str, int] = defaultdict(int)
        missing: dict[str, list[str]] = defaultdict(list)
        missing_days: list[str] = []
        for day, window in zip(days, windows, strict=True):
            if window.until <= window.start:  # a day that has not begun is neither missing nor zero
                continue
            with_data = window.counted_devices_with_data
            for device in sorted(self._expected(window) - with_data):
                missing[device].append(day.isoformat())
            if not with_data:
                missing_days.append(day.isoformat())
                continue
            if group_by == "day":
                by_key[day.isoformat()] += 0
            if group_by == "device":
                for device in with_data:
                    by_key[device] += 0
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
        UTC, and a DST change repeats or skips an hour), so the pieces stay whole seconds and add up exactly."""
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

    def _focus(self, window: Window) -> tuple[int, list[dict[str, Any]]]:
        work = merge((s.start, s.end) for s in window.pieces if s.category in PRODUCTIVE)
        distractions = merge(
            (s.start, s.end) for s in window.pieces if window.type_of(s) in PHONE_TYPES and s.category in DISTRACTING
        )
        kept = subtract_intervals(work, distractions)
        focused = 0
        blocks: list[dict[str, Any]] = []
        # Blocks join work across short gaps, then any phone distraction, however short, splits them again.
        for candidate_start, candidate_end in subtract_intervals(merge(work, join=FOCUS_JOIN), distractions):
            if candidate_end - candidate_start < FOCUS_BLOCK:
                continue
            seconds = union_seconds(clip_to(kept, candidate_start, candidate_end))
            focused += seconds
            blocks.append({"start": self._local(candidate_start), "end": self._local(candidate_end),
                           "seconds": seconds, "minutes": minutes(seconds)})
        return focused, blocks

    def focused_minutes(self, day: date) -> dict[str, Any]:
        """Work and study time in blocks of 10 minutes or more with no phone distraction.

        Work and study are sessions in those categories on any device (overlaps counted once). They form blocks
        across short gaps (up to 60 s: a window switch); a social, video or game app on a phone, even for a few
        seconds, splits a block and its time is taken out. A block counts when it spans 10 minutes or more, and
        its work time (not its short gaps) is the focused time.
        """
        window = self.day(day)
        if not window.counted_devices_with_data:
            return self._missing(window, blocks=[])
        focused, blocks = self._focus(window)
        return {"value": minutes(focused), "seconds": focused, "missing": False, "in_progress": not window.over,
                "blocks": blocks, **self._meta(window.start, window.end, [window], any(s.estimated for s in window.pieces))}

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
        if not window.counted_devices_with_data:
            return self._missing(window, unit="score")
        focused, _ = self._focus(window)
        work = union_seconds([(s.start, s.end) for s in window.pieces if s.category in PRODUCTIVE])
        distracted = union_seconds([(s.start, s.end) for s in window.pieces if s.category in DISTRACTING])
        denominator = work + distracted
        return {
            "value": round(100 * focused / denominator) if denominator else None,
            "missing": False,
            "in_progress": not window.over,
            "reason": None if denominator else "no work, study or distraction time this day",
            "focused_seconds": focused,
            "work_or_study_seconds": work,
            "distracted_seconds": distracted,
            "formula": "round(100 x focused / (work_or_study + distracted))",
            **self._meta(window.start, window.end, [window], any(s.estimated for s in window.pieces), unit="score"),
        }

    def switches_per_hour(self, day: date) -> dict[str, Any]:
        """App switches per hour of screen time. A switch is one session followed by a different app or site on
        the same device within 5 minutes (switching tabs between sites counts; coming back later does not).
        Screen time is counted per device and added up, as in totals(), since each device switches on its own.
        `by_hour` counts switches by the local hour they happened in."""
        window = self.day(day)
        if not window.counted_devices_with_data:
            return self._missing(window, unit="switches per hour", switches=None, by_hour={})
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
        screen = sum(s.seconds for s in window.counted)
        return {"value": round(switches / (screen / 3600), 1) if screen else None, "missing": False, "switches": switches,
                "screen_seconds": screen, "by_hour": dict(sorted(by_hour.items())),
                **self._meta(window.start, window.end, [window], any(s.estimated for s in window.pieces), unit="switches per hour")}

    def pickups(self, day: date) -> dict[str, Any]:
        """How many times a phone was picked up: a phone app coming into use after the phone rested for a
        minute or more. A session still running at midnight is not a new pickup. Glancing at the lock screen
        without opening an app is not counted. Phones that sent nothing this day are in `missing_devices`."""
        window = self.day(day)
        phones = self._expected(window, PHONE_TYPES)
        with_data = sorted(d for d in phones if window.has_data(d))
        if not with_data:
            return self._missing(window, unit="pickups", by_device={}, missing_devices=sorted(phones))
        context = self.window(window.start - PICKUP_LOOKBACK, window.end)  # to see what ran over midnight
        by_device: dict[str, int] = {}
        for device in with_data:
            count, last_end = 0, None
            for session in sorted((s for s in context.counted if s.device_id == device), key=lambda s: s.start):
                if session.start >= window.start and (last_end is None or session.start - last_end >= PICKUP_REST):
                    count += 1
                last_end = session.end if last_end is None else max(last_end, session.end)
            by_device[device] = count
        estimated = any(s.estimated for s in window.counted if s.device_id in by_device)
        return {"value": sum(by_device.values()), "missing": False, "by_device": by_device,
                "missing_devices": sorted(set(phones) - set(with_data)),
                **self._meta(window.start, window.end, [window], estimated, unit="pickups")}

    def late_night_minutes(self, day: date) -> dict[str, Any]:
        """Screen time on any device from 23:00 on `day` to 03:00 the next morning (overlaps counted once), the
        night that can cost the next day's focus. None when no device sent screen data that day or night, or
        when the night has not started yet; while it runs, the minutes so far with `in_progress`."""
        start, end = self.at(day, LATE_FROM), self.at(day + timedelta(days=1), LATE_UNTIL)
        night = self.window(start, end)
        if self.now <= start:
            return {"value": None, "missing": False, "in_progress": False, "reason": "the night has not started yet",
                    "by_device": {}, **self._meta(start, end, [night], False)}
        if not night.counted_devices_with_data and not self.day(day).counted_devices_with_data:
            return self._missing(night, by_device={})
        seconds = union_seconds([(s.start, s.end) for s in night.counted])
        by_device: dict[str, int] = defaultdict(int)
        for session in night.counted:
            by_device[session.device_id] += session.seconds
        return {"value": minutes(seconds), "seconds": seconds, "missing": False, "in_progress": not night.over,
                "by_device": dict(by_device), **self._meta(start, end, [night], any(s.estimated for s in night.counted))}

    def sleep_estimate(self, day: date) -> dict[str, Any]:
        """Last night's sleep: the night that ends on the morning of `day` (21:00 the evening before to 12:00).

        From health data when there is any: sleep that ended in that window, with awake stages inside it taken
        out ("in bed" is neither added nor taken out), and the same night from two phones counted once.

        Otherwise estimated, as the ticket asks, from the phone: the longest stretch with no phone in use
        between two uses of a phone in that window. A stretch that runs into the edge of the window is not
        counted, since the phone may simply not have synced yet (or been off). None, with a reason, when no
        phone sent anything that night or there is no use after the night yet.
        """
        start, end = self.at(day - timedelta(days=1), SLEEP_FROM), self.at(day, SLEEP_UNTIL)
        night = self.window(start, end)
        if self.now <= start:
            return {"value": None, "missing": False, "method": None, "reason": "the night has not started yet",
                    **self._meta(start, end, [night], False)}
        seen: set[tuple[datetime, datetime, object]] = set()
        asleep: list[Interval] = []
        awake: list[Interval] = []
        measured = True
        for event in night.events:
            if event.kind != "sleep" or event.end is None or not start < event.end <= end:
                continue
            stage = event.data.get("stage")
            key = (event.start, event.end, stage)
            if key in seen:
                continue
            seen.add(key)
            if stage == "awake":
                awake.append((snap(event.start), snap(event.end)))
            elif stage != "in_bed":
                asleep.append((snap(event.start), snap(event.end)))
                measured = measured and event.data.get("measured") is not False
        if asleep:
            spans = subtract_intervals(asleep, awake)
            seconds = union_seconds(spans)
            return {
                "value": minutes(seconds), "seconds": seconds, "missing": False, "method": "health",
                "measured": measured, "start": self._local(spans[0][0]) if spans else None,
                "end": self._local(spans[-1][1]) if spans else None,
                **self._meta(start, end, [night], not measured),
            }
        phones = {d for d in night.counted_devices_with_data if self._device_types.get(d) in PHONE_TYPES}
        if not phones:
            return self._missing(night, method=None, reason="no phone sent anything that night")
        busy = merge((s.start, s.end) for s in night.counted if s.device_id in phones)
        gaps = [(before[1], after[0]) for before, after in itertools.pairwise(busy)]
        if not gaps:
            return {"value": None, "missing": False, "method": "idle_gap", "measured": False,
                    "reason": "no phone use after the night yet", **self._meta(start, end, [night], True)}
        gap_start, gap_end = max(gaps, key=lambda gap: gap[1] - gap[0])
        seconds = round((gap_end - gap_start).total_seconds())
        return {
            "value": minutes(seconds), "seconds": seconds, "missing": False, "method": "idle_gap", "measured": False,
            "start": self._local(gap_start), "end": self._local(gap_end), **self._meta(start, end, [night], True),
        }

    # --- plans -----------------------------------------------------------------------------------------------

    def planned_vs_actual(self, day: date) -> dict[str, Any]:
        """For each calendar block of the day (not all-day ones), what the time went to, moment by moment:
        on plan (work or study, or a meeting app such as Zoom or Teams), off plan (social, video or games, which
        win when both are on screen), other screen time (chatting included), or no screen at all. The four add
        up to the block's length. A block that has not ended counts up to now (`in_progress`). The same event
        from two phones is listed once, and the totals count overlapping blocks' time once. With no screen
        data at all that day, the blocks are listed but the result is missing (no 0% for a day nobody saw)."""
        window = self.day(day)
        seen: set[tuple[str | None, datetime, datetime]] = set()
        blocks: list[dict[str, Any]] = []
        spans: list[Interval] = []
        has_data = bool(window.counted_devices_with_data)
        for event in sorted(window.events, key=lambda e: e.start):
            if event.kind != "calendar_event" or event.end is None or event.data.get("all_day") is True:
                continue
            key = (event.title, event.start, event.end)
            if key in seen or not window.overlaps(event, window.start, window.end):
                continue
            seen.add(key)
            start, end = max(snap(event.start), window.start), min(snap(event.end), window.end, window.until)
            if end <= start:
                continue
            spans.append((start, end))
            planned = round((end - start).total_seconds())
            parts = self._block_parts(window.pieces, start, end) if has_data else None
            blocks.append({
                "title": event.title, "start": self._local(start), "end": self._local(end), "planned_seconds": planned,
                "in_progress": snap(event.end) > self.now and start < self.now,
                **{f"{name}_seconds": seconds for name, seconds in (parts or dict.fromkeys(PARTS)).items()},
                "on_plan_pct": round(100 * parts["on_plan"] / planned) if parts and planned else None,
            })
        totals = {"planned": union_seconds(spans), **dict.fromkeys(PARTS, 0)}
        if has_data:
            for start, end in merge(spans):
                for name, seconds in self._block_parts(window.pieces, start, end).items():
                    totals[name] += seconds
        meta = self._meta(window.start, window.end, [window], any(s.estimated for s in window.pieces), unit="percent")
        if not has_data:
            return {"value": None, "missing": True, "blocks": blocks,
                    "totals_seconds": {"planned": totals["planned"]}, "totals_minutes": {"planned": minutes(totals["planned"])},
                    **meta}
        return {
            "value": round(100 * totals["on_plan"] / totals["planned"]) if totals["planned"] else None,
            "missing": False,
            "blocks": blocks,
            "totals_seconds": totals,
            "totals_minutes": {name: minutes(seconds) for name, seconds in totals.items()},
            **meta,
        }

    @staticmethod
    def _block_parts(pieces: Sequence[Session], start: datetime, end: datetime) -> dict[str, int]:
        inside = [s for s in pieces if s.start < end and s.end > start]
        points = sorted({start, end, *(max(s.start, start) for s in inside), *(min(s.end, end) for s in inside)})
        parts = dict.fromkeys(PARTS, 0)
        for left, right in itertools.pairwise(points):
            active = [s for s in inside if s.start < right and s.end > left]
            categories = {s.category or "other" for s in active}
            if categories & DISTRACTING:
                name = "off_plan"
            elif categories & PRODUCTIVE or any(is_meeting(s) for s in active):
                name = "on_plan"
            else:
                name = "other" if active else "idle"
            parts[name] += round((right - left).total_seconds())
        return parts

    # --- patterns --------------------------------------------------------------------------------------------

    def correlation(self, first: date, last: date) -> dict[str, Any]:
        """Late nights against the next day's focus: Spearman's rank correlation between late_night_minutes(d)
        and focus_score(d + 1) for every night d from `first` to the night before `last` where both are known
        and the next day is over (a day still going would sit among whole ones). rho runs from -1 (later nights,
        less focus) to 1; p says how likely a pattern this strong is by chance. Needs 3 pairs or more (rho and p
        are None otherwise, or when either side never varies)."""
        days = self._days(first, last)
        pairs: list[dict[str, Any]] = []
        estimated = False
        for night in days[:-1]:
            next_day = night + timedelta(days=1)
            if not self.day(next_day).over:
                continue
            late = self.late_night_minutes(night)
            focus = self.focus_score(next_day)
            if late["value"] is None or focus["value"] is None:
                continue
            estimated = estimated or late["estimated"] or focus["estimated"]
            pairs.append({"night": night.isoformat(), "late_minutes": late["value"],
                          "next_day": next_day.isoformat(), "focus_score": focus["value"]})
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
            **self._meta(windows[0].start, windows[-1].end, windows, estimated, unit="rank correlation"),
        }


PARTS = ("on_plan", "off_plan", "other", "idle")
