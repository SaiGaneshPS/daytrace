"""Tests for DT-5: scripts/notion_sync.py, the GitHub Action that moves Notion tickets. No network: Notion is faked."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "notion_sync.py"
    spec = importlib.util.spec_from_file_location("notion_sync", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["notion_sync"] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


sync = _load()
SOURCE = "2f1d4a21-e574-81f6-bef8-000b98cc5e42"
DATABASE = "2f1d4a21e5748062b721ee456b9738c1"
PR_URL = "https://github.com/SaiGaneshPS/daytrace/pull/23"


class FakeNotion:
    """Answers like the Notion API for one data source holding a few ticket rows; keeps every call."""

    def __init__(self, rows: dict[str, tuple[str, str | None]] | None = None, fail: int | None = None) -> None:
        self.rows = rows if rows is not None else {"DT-5": ("Not started", None)}
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.tokens: list[str] = []

    def factory(self, token: str) -> Any:
        self.tokens.append(token)
        return self.send

    def send(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if self.fail:
            raise sync.NotionError(self.fail, "API token is invalid.")
        source = SOURCE.replace("-", "")
        if method == "GET" and path == f"/data_sources/{source}":
            return {"object": "data_source", "id": SOURCE}
        if method == "GET" and path.startswith("/data_sources/"):
            raise sync.NotionError(404, "Could not find data source")
        if method == "GET" and path == f"/databases/{DATABASE}":
            return {"object": "database", "data_sources": [{"id": SOURCE, "name": "Tasks Tracker"}]}
        if method == "POST" and path in (f"/data_sources/{source}/query", f"/data_sources/{SOURCE}/query"):
            assert body is not None
            wanted = body["filter"]["rich_text"]["equals"]
            return {"results": [self.page(ticket) for ticket in self.rows if ticket == wanted]}
        if method == "PATCH":
            return {"object": "page"}
        raise AssertionError(f"unexpected call {method} {path}")

    def page(self, ticket: str) -> dict[str, Any]:
        status, url = self.rows[ticket]
        return {"id": f"page-{ticket}", "properties": {"Status": {"status": {"name": status}}, "PR": {"url": url}}}

    def patches(self) -> list[dict[str, Any]]:
        return [body for method, _, body in self.calls if method == "PATCH" and body is not None]


def run(tmp_path: Path, fake: FakeNotion, action: str, title: str = "DT-5: notion sync", branch: str = "DT-5-notion-sync",
        merged: bool = False, state: str = "open", secrets: bool = True, database: str = SOURCE) -> tuple[int, list[str]]:
    event = {"action": action, "pull_request": {"title": title, "head": {"ref": branch}, "html_url": PR_URL,
                                                "merged": merged, "state": state}}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    env = {"GITHUB_EVENT_PATH": str(path)}
    if secrets:
        env |= {"NOTION_TOKEN": "secret-test-token", "NOTION_TICKETS_DB": database}
    lines: list[str] = []
    return sync.main(env, http_factory=fake.factory, out=lines.append), lines


@pytest.mark.parametrize(
    ("title", "branch", "ticket"),
    [
        ("DT-5: notion sync", "DT-5-notion-sync", "DT-5"),
        ("Fix the sync", "DT-39-day-story", "DT-39"),  # the branch when the title has none
        ("dt-40: ask", None, "DT-40"),
        ("DT-05: padded", None, "DT-5"),
        ("DT-59: widget, see DT-5", None, "DT-59"),  # the first one, and 59 is not 5
        ("Bump the version", "main", None),
        ("ADT-5 is not a ticket", "feature/DT-7-x", "DT-7"),
    ],
)
def test_the_ticket_comes_from_the_title_then_the_branch(title: str, branch: str | None, ticket: str | None) -> None:
    assert sync.ticket_from(title, branch) == ticket


def test_each_event_maps_to_a_status() -> None:
    pr = {"html_url": PR_URL, "state": "open", "merged": False}
    assert sync.change_for("opened", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("reopened", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("edited", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("edited", {**pr, "state": "closed"}) is None
    assert sync.change_for("closed", {**pr, "state": "closed", "merged": True}) == sync.Change("Done", PR_URL)
    assert sync.change_for("closed", {**pr, "state": "closed"}) == sync.Change("In progress", None, keep_done=True)
    assert sync.change_for("labeled", pr) is None


def test_an_opened_pr_moves_its_ticket_to_in_progress_with_the_link(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "opened")
    assert code == 0 and lines == ["DT-5: moved to In progress with the PR link"]
    assert fake.patches() == [{"properties": {"Status": {"status": {"name": "In progress"}}, "PR": {"url": PR_URL}}}]
    assert fake.tokens == ["secret-test-token"]


def test_a_merged_pr_moves_its_ticket_to_done(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("In progress", PR_URL)})
    code, lines = run(tmp_path, fake, "closed", merged=True, state="closed")
    assert code == 0 and lines == ["DT-5: moved to Done"]
    assert fake.patches() == [{"properties": {"Status": {"status": {"name": "Done"}}}}]


def test_a_pr_closed_without_merging_never_undoes_done(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("Done", "https://github.com/SaiGaneshPS/daytrace/pull/9")})
    code, lines = run(tmp_path, fake, "closed", state="closed")
    assert code == 0 and fake.patches() == [] and lines == ["DT-5: already Done: left as it is"]


def test_nothing_is_written_when_nothing_changes(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("In progress", PR_URL)})
    code, lines = run(tmp_path, fake, "edited")
    assert code == 0 and fake.patches() == [] and lines == ["DT-5: already In progress: nothing to change"]


def test_a_database_id_or_link_finds_its_data_source(tmp_path: Path) -> None:
    fake = FakeNotion()
    link = f"https://app.notion.com/p/{DATABASE}?v=2f1d4a21e574812cadd0000cd05757ea"
    code, _ = run(tmp_path, fake, "opened", database=link)
    assert code == 0 and fake.patches()
    assert ("GET", f"/databases/{DATABASE}", None) in fake.calls


def test_without_secrets_it_skips_and_passes(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "opened", secrets=False)
    assert code == 0 and fake.calls == [] and "skipped" in lines[0] and lines[0].startswith("::notice::")


def test_a_pr_without_a_ticket_is_left_alone(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "opened", title="Bump the version", branch="bump")
    assert code == 0 and fake.calls == [] and lines[0].startswith("::notice::No DT-<number>")


def test_an_unknown_ticket_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "opened", title="DT-99: nothing", branch="DT-99-x")
    assert code == 0 and fake.patches() == [] and lines[0].startswith("::warning::DT-99 is not in the Tasks Tracker")


def test_a_notion_error_fails_the_step_with_a_hint(tmp_path: Path) -> None:
    code, lines = run(tmp_path, FakeNotion(fail=401), "opened")
    assert code == 1 and lines[0].startswith("::error::DT-5: Notion answered 401")
    assert "Connections" in lines[0] and "secret-test-token" not in lines[0]


def test_messages_can_never_start_a_workflow_command() -> None:
    assert sync.annotation("notice", "a\n::error::fake%") == "::notice::a%0A::error::fake%25"


def test_ids_are_read_from_ids_urls_and_links() -> None:
    assert sync.notion_id(SOURCE) == SOURCE.replace("-", "")
    assert sync.notion_id(f"collection://{SOURCE}") == SOURCE.replace("-", "")
    assert sync.notion_id(f"https://www.notion.so/Tasks-{DATABASE}?v=1") == DATABASE
    assert sync.notion_id("not an id") is None


def test_busy_notion_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real HTTP function, with urlopen faked: a 429 is retried after Retry-After, then the answer comes."""
    import io
    import urllib.error

    answers: list[Any] = [
        urllib.error.HTTPError("https://api.notion.com/v1/pages/x", 429, "busy", {"Retry-After": "2"}, io.BytesIO(b"{}")),
        io.BytesIO(b'{"object": "page"}'),
    ]
    sent: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        sent.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    slept: list[float] = []
    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    http = sync.urllib_http("secret-test-token", sleep=slept.append)
    assert http("PATCH", "/pages/x", {"properties": {}}) == {"object": "page"}
    assert slept == [2.0] and len(sent) == 2
    assert sent[0].get_header("Notion-version") == sync.NOTION_VERSION
    assert sent[0].get_header("Authorization") == "Bearer secret-test-token"
