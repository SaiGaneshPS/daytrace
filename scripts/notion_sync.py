"""DT-5: move the Notion ticket a pull request names, with the free Notion API.

GitHub Actions runs this on pull_request events into the default branch (.github/workflows/notion-sync.yml). The
ticket is the first DT-<number> in the PR title, else in the branch name. Its row in the Tasks Tracker is found by
the Ticket text property. A PR only ever moves its ticket forward, never back:

- opened, reopened, or edited while open: Not started becomes In progress, and PR is set to the pull request's
  link (a ticket already Done keeps its status and link: a revert or an old second PR doesn't undo it);
- merged: Done, with PR set to the merged pull request;
- closed without merging: nothing changes (the work may go on in another PR);
- a title edit that names another ticket also takes this PR's link off the ticket it named before.

Because nothing moves back, the order runs finish in doesn't matter. Nothing is written when the row already looks
right. Two repository secrets are needed, which the owner sets with `gh secret set` so they are never pasted
anywhere: NOTION_TOKEN (an internal integration added to the Tasks database under Connections) and
NOTION_TICKETS_DB (the Tasks Tracker's data source ID; its database ID or a link to it works too). Without them (a
fork, or before setup) it says so and does nothing.

Only the standard library is used, so the workflow needs no install step.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"  # data sources: a database's rows are queried through its data source
NOT_STARTED, IN_PROGRESS, DONE = "Not started", "In progress", "Done"
PROGRESS = {NOT_STARTED: 0, IN_PROGRESS: 1, DONE: 2}  # any other status counts as not started
TICKET = re.compile(r"(?<![A-Za-z0-9])DT-(\d+)", re.IGNORECASE)  # "sai_DT-5" and "feature/DT-5", not "ADT-5"
NOTION_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)
RETRY_STATUSES = (409, 429, 500, 502, 503, 504)  # 409: Notion's conflict_error, a passing collision
ATTEMPTS = 3
MAX_WAIT = 60.0  # seconds; the job has 5 minutes
TOKEN_HINT = " The token may be wrong or refreshed: set it again with gh secret set NOTION_TOKEN."
ACCESS_HINT = " Check that the integration is added to the Tasks database under Connections."

Http = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]


class SyncError(Exception):
    """The sync couldn't be done: a setting that isn't usable, or Notion couldn't be reached."""


class NotionError(SyncError):
    """Notion answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Notion answered {status}: {message}")
        self.status = status


# --- what to do (no network) -------------------------------------------------------------------------------------


def ticket_from(title: str | None, branch: str | None) -> str | None:
    """The first DT-<number> in the title, else in the branch name ("DT-05" is DT-5), or None."""
    for text in (title, branch):
        match = TICKET.search(text or "")
        if match:
            return f"DT-{int(match.group(1))}"
    return None


@dataclass(frozen=True)
class Change:
    status: str
    pr_url: str | None


def change_for(action: str | None, pull_request: Mapping[str, Any]) -> Change | None:
    """What the event means for its ticket, or None when it means nothing (closed without merging, an edit to a
    closed PR)."""
    url = pull_request.get("html_url") or None
    if action in ("opened", "reopened") or (action == "edited" and pull_request.get("state") == "open"):
        return Change(IN_PROGRESS, url)
    if action == "closed" and pull_request.get("merged"):
        return Change(DONE, url)
    return None


def current(page: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The page's Status name and PR link."""
    properties = page.get("properties") or {}
    status = ((properties.get("Status") or {}).get("status") or {}).get("name")
    return status, (properties.get("PR") or {}).get("url")


def properties_for(page: Mapping[str, Any], change: Change) -> dict[str, Any]:
    """The properties to write: the status only when it moves forward, the link unless a Done ticket would get the
    link of a PR that didn't finish it. Empty when the row already looks right."""
    status, link = current(page)
    properties: dict[str, Any] = {}
    if PROGRESS[change.status] > PROGRESS.get(status or "", 0):
        properties["Status"] = {"status": {"name": change.status}}
    if change.pr_url and link != change.pr_url and (status != DONE or change.status == DONE):
        properties["PR"] = {"url": change.pr_url}
    return properties


def describe(page: Mapping[str, Any], properties: Mapping[str, Any]) -> str:
    status = current(page)[0] or "without a status"
    if "Status" in properties:
        name = properties["Status"]["status"]["name"]
        return f"moved to {name}" + (" with the PR link" if "PR" in properties else "")
    if "PR" in properties:
        return f"PR link updated (still {status})"
    return f"already {status}: left as it is"


def previous_ticket(event: Mapping[str, Any]) -> str | None:
    """The ticket the title named before an edit, if the title changed."""
    before = ((event.get("changes") or {}).get("title") or {}).get("from")
    return ticket_from(before, None) if isinstance(before, str) else None


def notion_id(text: str) -> str | None:
    """The ID in a data source ID, a database ID, a collection:// URL or a Notion link (32 hex digits)."""
    match = NOTION_ID.search(text.strip())
    return match.group(0).replace("-", "").lower() if match else None


def annotation(level: str, message: str) -> str:
    """A GitHub Actions workflow command, escaped so a message can never start a command of its own."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{level}::{escaped}"


# --- Notion --------------------------------------------------------------------------------------------------------


def _message(body: bytes) -> str:
    try:
        return str(json.loads(body).get("message") or "no details")[:300]
    except (ValueError, AttributeError):
        return body.decode("utf-8", "replace")[:300] or "no details"


def _wait(retry_after: str | None) -> float:
    try:
        seconds = float(retry_after or 1)
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.5), MAX_WAIT)


def urllib_http(token: str, sleep: Callable[[float], None] = time.sleep) -> Http:
    """Calls to the Notion API. A busy or colliding Notion (409, 429, 5xx: Retry-After honoured up to a minute)
    and a dropped or timed-out connection are tried again, 3 attempts in all."""

    def send(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(NOTION_API + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    raw = response.read()
            except urllib.error.HTTPError as error:
                if error.code in RETRY_STATUSES and attempt < ATTEMPTS:
                    sleep(_wait(error.headers.get("Retry-After")))
                    continue
                raise NotionError(error.code, _message(error.read())) from None
            except (OSError, http.client.HTTPException) as error:  # no answer: DNS, refused, timeout, cut off
                if attempt < ATTEMPTS:
                    sleep(float(attempt))
                    continue
                reason = getattr(error, "reason", None) or type(error).__name__
                raise SyncError(f"could not reach Notion ({reason})") from None
            try:
                return dict(json.loads(raw or b"{}"))
            except (ValueError, TypeError):
                raise SyncError("Notion sent a reply that isn't JSON") from None

    return send


class Tracker:
    """The Tasks Tracker. NOTION_TICKETS_DB may be its data source ID or its database ID (or a link); a database is
    resolved to its data source only when the first query says the ID isn't a data source."""

    def __init__(self, http: Http, given: str) -> None:
        ident = notion_id(given)
        if ident is None:
            raise SyncError("NOTION_TICKETS_DB is not a Notion ID or link")
        self.http, self.ident = http, ident
        self.source: str | None = None

    def _query(self, source: str, ticket: str) -> list[dict[str, Any]]:
        body = {"filter": {"property": "Ticket", "rich_text": {"equals": ticket}}, "page_size": 5}
        return list(self.http("POST", f"/data_sources/{source}/query", body).get("results") or [])

    def rows(self, ticket: str) -> list[dict[str, Any]]:
        if self.source is not None:
            return self._query(self.source, ticket)
        try:
            rows = self._query(self.ident, ticket)
        except NotionError as error:
            if error.status not in (400, 404):
                raise
            sources = self.http("GET", f"/databases/{self.ident}", None).get("data_sources") or []
            if not sources:
                raise SyncError("NOTION_TICKETS_DB names a database without a data source") from None
            self.source = str(sources[0]["id"])
            return self._query(self.source, ticket)
        self.source = self.ident
        return rows

    def update(self, page: Mapping[str, Any], properties: Mapping[str, Any]) -> None:
        self.http("PATCH", f"/pages/{page['id']}", {"properties": dict(properties)})


# --- the workflow step ---------------------------------------------------------------------------------------------


def main(env: Mapping[str, str] | None = None, http_factory: Callable[[str], Http] = urllib_http,
         out: Callable[[str], None] = print) -> int:
    env = os.environ if env is None else env
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    action = event.get("action")
    base = (pull_request.get("base") or {}).get("ref")
    default = (event.get("repository") or {}).get("default_branch")
    if base and default and base != default:
        out(annotation("notice", f"This PR goes into {base}: tickets move only for PRs into {default}."))
        return 0
    ticket = ticket_from(pull_request.get("title"), (pull_request.get("head") or {}).get("ref"))
    change = change_for(action, pull_request) if ticket else None
    before = previous_ticket(event) if action == "edited" else None
    before = None if before == ticket else before
    if change is None and before is None:
        reason = f"Nothing to change in Notion for this event ({ticket})." if ticket else (
            "No DT-<number> in the PR title or branch, so no Notion ticket to move.")
        out(annotation("notice", reason))
        return 0
    token, source = env.get("NOTION_TOKEN", "").strip(), env.get("NOTION_TICKETS_DB", "").strip()
    if not token or not source:
        out(annotation("notice", "NOTION_TOKEN or NOTION_TICKETS_DB is not set (a fork, or before setup): skipped."))
        return 0
    try:
        tracker = Tracker(http_factory(token), source)
        if before is not None:  # the title named another ticket before: take this PR's link off it
            for page in tracker.rows(before):
                link = current(page)[1]
                if link and link == pull_request.get("html_url"):
                    tracker.update(page, {"PR": {"url": None}})
                    out(f"{before}: PR link removed (the title now names {ticket or 'no ticket'})")
        if change is not None and ticket is not None:
            pages = tracker.rows(ticket)
            if not pages:
                out(annotation("warning", f"{ticket} is not in the Tasks Tracker (or the integration can't see it)."))
                return 0
            if len(pages) > 1:
                out(annotation("warning", f"{len(pages)} rows have Ticket {ticket}; only the first is moved."))
            properties = properties_for(pages[0], change)
            if properties:
                tracker.update(pages[0], properties)
            out(f"{ticket}: {describe(pages[0], properties)}")
    except SyncError as error:
        status = error.status if isinstance(error, NotionError) else None
        hint = TOKEN_HINT if status == 401 else ACCESS_HINT if status in (403, 404) else ""
        reason = str(error).rstrip(".")  # Notion ends its messages with a period already
        out(annotation("error", f"{ticket or before}: {reason}.{hint}"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
