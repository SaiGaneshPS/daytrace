"""DT-14: app and website categories (AI fill-in: DT-42).

Every session gets exactly one category, looked up in this order:
1. The user's override (or one DT-42's AI saved; the user's always wins over the AI's).
2. The built-in list in data/categories.json: Android package or iOS/macOS bundle id, then the app name,
   and for web sessions the domain, where the longest matching suffix wins (m.youtube.com is youtube.com,
   and music.youtube.com has its own entry).
3. The category the collector sent with the event.
4. "other".
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from typing import Literal

from .db import utc_text
from .models import Category

CATEGORIES: tuple[str, ...] = tuple(category.value for category in Category)
CategorySource = Literal["user", "ai", "builtin", "collector", "default"]
_STRIPPED_SUFFIXES = (".exe",)  # Windows reports code.exe, the list says code


def normalize_key(text: str) -> str:
    """How app names, ids and domains are compared: trimmed, lowercase, no .exe, no trailing dot."""
    key = text.strip().lower().rstrip(".")
    for suffix in _STRIPPED_SUFFIXES:
        key = key.removesuffix(suffix)
    return key


def normalize_domain(text: str) -> str:
    domain = normalize_key(text)
    domain = domain.split(":", 1)[0]  # a port, if a collector ever sends one
    return domain.removeprefix("www.")


def domain_suffixes(domain: str) -> list[str]:
    """m.youtube.com -> [m.youtube.com, youtube.com] (never the bare top-level domain)."""
    parts = normalize_domain(domain).split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] or [".".join(parts)]


@dataclass(frozen=True)
class Builtin:
    names: dict[str, str]
    ids: dict[str, str]
    domains: dict[str, str]

    @property
    def size(self) -> int:
        return len(self.names) + len(self.ids) + len(self.domains)


def parse_builtin(data: dict[str, object]) -> Builtin:
    """Read the built-in list; a key listed under two categories is an error, not a silent coin flip."""
    tables: dict[str, dict[str, str]] = {"apps": {}, "ids": {}, "domains": {}}
    for category, groups in data.items():
        if category.startswith("$"):
            continue
        if category not in CATEGORIES:
            raise ValueError(f"categories.json: unknown category {category!r}")
        assert isinstance(groups, dict)
        for group, entries in groups.items():
            if group not in tables:
                raise ValueError(f"categories.json: {category}.{group} must be apps, ids or domains")
            for entry in entries:
                key = normalize_domain(entry) if group == "domains" else normalize_key(entry)
                if tables[group].get(key, category) != category:
                    raise ValueError(f"categories.json: {entry!r} is listed as {tables[group][key]} and {category}")
                tables[group][key] = category
    return Builtin(names=tables["apps"], ids=tables["ids"], domains=tables["domains"])


@lru_cache(maxsize=1)
def load_builtin() -> Builtin:
    text = resources.files("daytrace_hub").joinpath("data", "categories.json").read_text(encoding="utf-8")
    return parse_builtin(json.loads(text))


@dataclass(frozen=True)
class Override:
    category: str
    source: Literal["user", "ai"]
    updated_at: str


def load_overrides(conn: sqlite3.Connection) -> dict[str, Override]:
    rows = conn.execute("SELECT app_key, category, source, updated_at FROM category_overrides")
    return {row["app_key"]: Override(row["category"], row["source"], row["updated_at"]) for row in rows}


class Categorizer:
    """Looks up categories with one set of overrides (load it once per request)."""

    def __init__(self, overrides: dict[str, Override] | None = None, builtin: Builtin | None = None) -> None:
        self.overrides = overrides or {}
        self.builtin = builtin or load_builtin()

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> Categorizer:
        return cls(load_overrides(conn))

    def lookup(
        self, app: str | None, app_id: str | None = None, kind: str = "app", collector: str | None = None
    ) -> tuple[str, CategorySource]:
        """(category, where it came from) for one app or website."""
        if kind == "web":
            keys = domain_suffixes(app) if app else []
            for key in keys:
                if key in self.overrides:
                    return self.overrides[key].category, self.overrides[key].source
            for key in keys:
                if key in self.builtin.domains:
                    return self.builtin.domains[key], "builtin"
        else:
            id_key = normalize_key(app_id) if app_id else None
            name_key = normalize_key(app) if app else None
            for key in (id_key, name_key):
                if key is not None and key in self.overrides:
                    return self.overrides[key].category, self.overrides[key].source
            if id_key is not None and id_key in self.builtin.ids:
                return self.builtin.ids[id_key], "builtin"
            if name_key is not None and name_key in self.builtin.names:
                return self.builtin.names[name_key], "builtin"
        if collector in CATEGORIES:
            return collector, "collector"  # type: ignore[return-value]
        return "other", "default"

    def category(self, app: str | None, app_id: str | None = None, kind: str = "app", collector: str | None = None) -> str:
        return self.lookup(app, app_id, kind, collector)[0]


def override_key(app: str | None, app_id: str | None, kind: str) -> str | None:
    """The key an override for this app is saved under: the domain for web, else the id, else the name."""
    if kind == "web":
        return normalize_domain(app) if app else None
    if app_id:
        return normalize_key(app_id)
    return normalize_key(app) if app else None


def save_override(conn: sqlite3.Connection, key: str, category: str, source: Literal["user", "ai"]) -> Override:
    """Store an override. An AI guess never replaces a choice the user made."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    now = utc_text(datetime.now(UTC))
    conn.execute(
        "INSERT INTO category_overrides (app_key, category, source, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (app_key) DO UPDATE SET category = excluded.category, source = excluded.source,"
        " updated_at = excluded.updated_at WHERE excluded.source = 'user' OR category_overrides.source = 'ai'",
        (key, category, source, now),
    )
    row = conn.execute(
        "SELECT category, source, updated_at FROM category_overrides WHERE app_key = ?", (key,)
    ).fetchone()
    return Override(row["category"], row["source"], row["updated_at"])


def delete_override(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("DELETE FROM category_overrides WHERE app_key = ?", (key,)).rowcount > 0
