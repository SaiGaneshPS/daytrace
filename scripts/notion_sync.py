"""DT-5: move the Notion ticket a pull request names, with the free Notion API.

GitHub Actions runs this on pull_request events (.github/workflows/notion-sync.yml). The ticket is the first
DT-<number> in the PR title, else in the branch name. Its row in the Tasks Tracker is found by the Ticket text
property and updated:

- opened, reopened, or edited while open: Status "In progress", and PR set to the pull request's URL;
- closed and merged: Status "Done" (PR set too);
- closed without merging: Status back to "In progress", unless the ticket is already Done (another PR finished it).

Nothing is written when the ticket already looks like that. Two repository secrets are needed, which the owner
sets with `gh secret set` so they are never pasted anywhere: NOTION_TOKEN (an internal integration that has been
added to the Tasks database under Connections) and NOTION_TICKETS_DB (the Tasks Tracker's data source ID; its
database ID or a link to it works too). Without them (a fork, or before setup) it says so and does nothing.

Only the standard library is used, so the workflow needs no install step.
"""
from __future__ import annotations

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
IN_PROGRESS, DONE = "In progress", "Done"
TICKET = re.compile(r"\bDT-(\d+)(?!\d)", re.IGNORECASE)
NOTION_ID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)
RETRY_STATUSES = (429, 500, 502, 503, 504)

Http = Callable[[str, str, "dict[str, Any] | None"], "dict[str, Any]"]


class NotionError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Notion answered {status}: {message}")
        self.status = status
        self.message = message


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
    pr_url: str | None  # None leaves the PR property as it is
    keep_done: bool = False  # a ticket already Done stays Done


def change_for(action: str | None, pull_request: Mapping[str, Any]) -> Change | None:
    """What the event means for the ticket, or None when it means nothing (an edit to a closed PR, say)."""
    url = pull_request.get("html_url") or None
    if action in ("opened", "reopened"):
        return Change(IN_PROGRESS, url)
    if action == "edited":
        return Change(IN_PROGRESS, url) if pull_request.get("state") == "open" else None
    if action == "closed":
        return Change(DONE, url) if pull_request.get("merged") else Change(IN_PROGRESS, None, keep_done=True)
    return None


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


def urllib_http(token: str, sleep: Callable[[float], None] = time.sleep) -> Http:
    """Calls to the Notion API, retried twice when it is busy (honouring Retry-After)."""

    def send(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
        for attempt in range(3):
            request = urllib.request.Request(NOTION_API + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as error:
                if error.code in RETRY_STATUSES and attempt < 2:
                    try:
                        wait = float(error.headers.get("Retry-After") or 1)
                    except ValueError:
                        wait = 1.0
                    sleep(min(max(wait, 0.5), 10))
                    continue
                raise NotionError(error.code, _message(error.read())) from None
            except urllib.error.URLError as error:
                if attempt < 2:
                    sleep(1)
                    continue
                raise NotionError(0, f"could not reach Notion ({error.reason})") from None
        raise NotionError(0, "could not reach Notion")

    return send


def data_source_id(http: Http, given: str) -> str:
    """NOTION_TICKETS_DB as a data source ID, or a database ID (or link) whose first data source is used."""
    ident = notion_id(given)
    if ident is None:
        raise NotionError(0, "NOTION_TICKETS_DB is not a Notion ID or link")
    try:
        http("GET", f"/data_sources/{ident}", None)
        return ident
    except NotionError as error:
        if error.status not in (400, 404):
            raise
    sources = http("GET", f"/databases/{ident}", None).get("data_sources") or []
    if not sources:
        raise NotionError(404, "that database has no data source")
    return str(sources[0]["id"])


def find_tickets(http: Http, source: str, ticket: str) -> list[dict[str, Any]]:
    query = {"filter": {"property": "Ticket", "rich_text": {"equals": ticket}}, "page_size": 5}
    return list(http("POST", f"/data_sources/{source}/query", query).get("results") or [])


def current(page: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The page's Status name and PR link."""
    properties = page.get("properties") or {}
    status = ((properties.get("Status") or {}).get("status") or {}).get("name")
    return status, (properties.get("PR") or {}).get("url")


def apply(http: Http, page: Mapping[str, Any], change: Change) -> str:
    """Update the page if the change changes anything. Returns what happened, in words."""
    status, pr_url = current(page)
    if change.keep_done and status == DONE:
        return "already Done: left as it is"
    properties: dict[str, Any] = {}
    if status != change.status:
        properties["Status"] = {"status": {"name": change.status}}
    if change.pr_url and pr_url != change.pr_url:
        properties["PR"] = {"url": change.pr_url}
    if not properties:
        return f"already {change.status}: nothing to change"
    http("PATCH", f"/pages/{page['id']}", {"properties": properties})
    return f"moved to {change.status}" + (" with the PR link" if "PR" in properties else "")


# --- the workflow step ---------------------------------------------------------------------------------------------


def main(env: Mapping[str, str] | None = None, http_factory: Callable[[str], Http] = urllib_http,
         out: Callable[[str], None] = print) -> int:
    env = os.environ if env is None else env
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    ticket = ticket_from(pull_request.get("title"), (pull_request.get("head") or {}).get("ref"))
    if ticket is None:
        out(annotation("notice", "No DT-<number> in the PR title or branch, so no Notion ticket to move."))
        return 0
    change = change_for(event.get("action"), pull_request)
    if change is None:
        out(annotation("notice", f"Nothing to do in Notion for this event ({ticket})."))
        return 0
    token, source = env.get("NOTION_TOKEN", "").strip(), env.get("NOTION_TICKETS_DB", "").strip()
    if not token or not source:
        out(annotation("notice", "NOTION_TOKEN or NOTION_TICKETS_DB is not set (a fork, or before setup): skipped."))
        return 0
    http = http_factory(token)
    try:
        pages = find_tickets(http, data_source_id(http, source), ticket)
        if not pages:
            out(annotation("warning", f"{ticket} is not in the Tasks Tracker (or the integration can't see it)."))
            return 0
        if len(pages) > 1:
            out(annotation("warning", f"{len(pages)} rows have Ticket {ticket}; only the first is moved."))
        result = apply(http, pages[0], change)
    except NotionError as error:
        hint = " Check that the integration is added to the Tasks database under Connections." if error.status in (401, 403, 404) else ""
        out(annotation("error", f"{ticket}: {error}.{hint}"))
        return 1
    out(f"{ticket}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
