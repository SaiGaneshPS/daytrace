"""Tests for DT-5: scripts/notion_sync.py, the GitHub Action that moves Notion tickets. No network: Notion is faked."""
from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import urllib.error
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
SOURCE = "2f1d4a21e57481f6bef8000b98cc5e42"
DATABASE = "2f1d4a21e5748062b721ee456b9738c1"
PR_URL = "https://github.com/SaiGaneshPS/daytrace/pull/23"
OTHER_PR = "https://github.com/SaiGaneshPS/daytrace/pull/9"


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
        if method == "POST" and path == f"/data_sources/{SOURCE}/query":
            assert body is not None
            wanted = body["filter"]["rich_text"]["equals"]
            return {"results": [self.page(ticket) for ticket in self.rows if ticket == wanted]}
        if method == "POST" and path.endswith("/query"):
            raise sync.NotionError(404, "Could not find data source")
        if method == "GET" and path == f"/databases/{DATABASE}":
            return {"object": "database", "data_sources": [{"id": SOURCE, "name": "Tasks Tracker"}]}
        if method == "PATCH":
            return {"object": "page"}
        raise AssertionError(f"unexpected call {method} {path}")

    def page(self, ticket: str) -> dict[str, Any]:
        status, url = self.rows[ticket]
        return {"id": f"page-{ticket}", "properties": {"Status": {"status": {"name": status}}, "PR": {"url": url}}}

    def patches(self) -> list[tuple[str, dict[str, Any]]]:
        return [(path, body) for method, path, body in self.calls if method == "PATCH" and body is not None]


def run(tmp_path: Path, fake: FakeNotion, action: str, title: str = "DT-5: notion sync", branch: str = "DT-5-notion-sync",
        merged: bool = False, state: str = "open", secrets: bool = True, database: str = SOURCE, base: str = "development",
        title_before: str | None = None) -> tuple[int, list[str]]:
    event: dict[str, Any] = {
        "action": action,
        "pull_request": {"title": title, "head": {"ref": branch}, "base": {"ref": base}, "html_url": PR_URL,
                         "merged": merged, "state": state},
        "repository": {"default_branch": "development"},
    }
    if title_before is not None:
        event["changes"] = {"title": {"from": title_before}}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    env = {"GITHUB_EVENT_PATH": str(path)}
    if secrets:
        env |= {"NOTION_TOKEN": "secret-test-token", "NOTION_TICKETS_DB": database}
    lines: list[str] = []
    return sync.main(env, http_factory=fake.factory, out=lines.append), lines


# --- what to do ----------------------------------------------------------------------------------------------------


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
        ("Fix the sync", "sai_DT-5-fix", "DT-5"),  # after an underscore too
    ],
)
def test_the_ticket_comes_from_the_title_then_the_branch(title: str, branch: str | None, ticket: str | None) -> None:
    assert sync.ticket_from(title, branch) == ticket


def test_each_event_maps_to_a_change() -> None:
    pr = {"html_url": PR_URL, "state": "open", "merged": False}
    assert sync.change_for("opened", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("reopened", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("edited", pr) == sync.Change("In progress", PR_URL)
    assert sync.change_for("edited", {**pr, "state": "closed"}) is None
    assert sync.change_for("closed", {**pr, "state": "closed", "merged": True}) == sync.Change("Done", PR_URL)
    assert sync.change_for("closed", {**pr, "state": "closed"}) is None  # abandoned: the ticket stays as it is
    assert sync.change_for("labeled", pr) is None


@pytest.mark.parametrize(
    ("now", "change", "written"),
    [
        (("Not started", None), "In progress", {"Status": "In progress", "PR": PR_URL}),
        (("In progress", OTHER_PR), "In progress", {"PR": PR_URL}),  # the newest open PR
        (("In progress", PR_URL), "In progress", {}),
        (("Done", OTHER_PR), "In progress", {}),  # a revert or an old second PR never undoes Done
        (("In progress", PR_URL), "Done", {"Status": "Done"}),
        (("Done", OTHER_PR), "Done", {"PR": PR_URL}),  # a follow-up PR finished it last
        (("Blocked", None), "In progress", {"Status": "In progress", "PR": PR_URL}),  # an unknown status is not started
    ],
)
def test_a_ticket_only_moves_forward(now: tuple[str, str | None], change: str, written: dict[str, str]) -> None:
    page = {"id": "p", "properties": {"Status": {"status": {"name": now[0]}}, "PR": {"url": now[1]}}}
    properties = sync.properties_for(page, sync.Change(change, PR_URL))
    flat = {name: value.get("url") or value.get("status", {}).get("name") for name, value in properties.items()}
    assert flat == written


# --- the workflow step ---------------------------------------------------------------------------------------------


def test_an_opened_pr_moves_its_ticket_to_in_progress_with_the_link(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "opened")
    assert code == 0 and lines == ["DT-5: moved to In progress with the PR link"]
    assert fake.patches() == [("/pages/page-DT-5", {"properties": {"Status": {"status": {"name": "In progress"}},
                                                                   "PR": {"url": PR_URL}}})]
    assert fake.tokens == ["secret-test-token"]
    assert [call[0] for call in fake.calls] == ["POST", "PATCH"]  # the data source ID is used as it is: 2 calls


def test_a_merged_pr_moves_its_ticket_to_done(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("In progress", PR_URL)})
    code, lines = run(tmp_path, fake, "closed", merged=True, state="closed")
    assert code == 0 and lines == ["DT-5: moved to Done"]
    assert fake.patches() == [("/pages/page-DT-5", {"properties": {"Status": {"status": {"name": "Done"}}}})]


def test_a_pr_closed_without_merging_changes_nothing(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("Not started", None)})
    code, lines = run(tmp_path, fake, "closed", state="closed")
    assert code == 0 and fake.calls == [] and "Nothing to change" in lines[0]


def test_only_prs_into_the_default_branch_count(tmp_path: Path) -> None:
    fake = FakeNotion()
    code, lines = run(tmp_path, fake, "closed", merged=True, state="closed", base="DT-39-day-story")
    assert code == 0 and fake.calls == [] and "move only for PRs into development" in lines[0]


def test_a_title_that_names_another_ticket_moves_the_link(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("In progress", PR_URL), "DT-6": ("Not started", None)})
    code, lines = run(tmp_path, fake, "edited", title="DT-6: notion sync", branch="fix", title_before="DT-5: notion sync")
    assert code == 0
    assert fake.patches() == [
        ("/pages/page-DT-5", {"properties": {"PR": {"url": None}}}),
        ("/pages/page-DT-6", {"properties": {"Status": {"status": {"name": "In progress"}}, "PR": {"url": PR_URL}}}),
    ]
    assert lines == ["DT-5: PR link removed (the title now names DT-6)", "DT-6: moved to In progress with the PR link"]


def test_the_old_ticket_keeps_a_link_to_another_pr(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("Done", OTHER_PR), "DT-6": ("Not started", None)})
    run(tmp_path, fake, "edited", title="DT-6: notion sync", branch="fix", title_before="DT-5: notion sync")
    assert [path for path, _ in fake.patches()] == ["/pages/page-DT-6"]


def test_nothing_is_written_when_nothing_changes(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("In progress", PR_URL)})
    code, lines = run(tmp_path, fake, "edited")
    assert code == 0 and fake.patches() == [] and lines == ["DT-5: already In progress: left as it is"]


def test_only_the_link_changing_says_so(tmp_path: Path) -> None:
    fake = FakeNotion({"DT-5": ("Done", OTHER_PR)})
    code, lines = run(tmp_path, fake, "closed", merged=True, state="closed")
    assert code == 0 and lines == ["DT-5: PR link updated (still Done)"]


def test_a_database_id_or_link_finds_its_data_source(tmp_path: Path) -> None:
    fake = FakeNotion()
    link = f"https://app.notion.com/p/{DATABASE}?v=2f1d4a21e574812cadd0000cd05757ea"
    code, _ = run(tmp_path, fake, "opened", database=link)
    assert code == 0 and fake.patches()
    assert [(method, path) for method, path, _ in fake.calls] == [
        ("POST", f"/data_sources/{DATABASE}/query"), ("GET", f"/databases/{DATABASE}"),
        ("POST", f"/data_sources/{SOURCE}/query"), ("PATCH", "/pages/page-DT-5")]


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


@pytest.mark.parametrize(
    ("status", "hint"),
    [(401, "gh secret set NOTION_TOKEN"), (403, "Connections"), (404, "Connections"), (500, None)],
)
def test_a_notion_error_fails_the_step_with_the_right_hint(tmp_path: Path, status: int, hint: str | None) -> None:
    code, lines = run(tmp_path, FakeNotion(fail=status), "opened")
    assert code == 1 and lines[0].startswith(f"::error::DT-5: Notion answered {status}")
    assert "secret-test-token" not in lines[0] and ".." not in lines[0]
    assert (hint in lines[0]) if hint else ("Connections" not in lines[0] and "gh secret" not in lines[0])


def test_a_setting_that_isnt_an_id_is_not_blamed_on_notion(tmp_path: Path) -> None:
    code, lines = run(tmp_path, FakeNotion(), "opened", database="the tasks board")
    assert code == 1 and "not a Notion ID or link" in lines[0] and "Notion answered" not in lines[0]


def test_messages_can_never_start_a_workflow_command() -> None:
    assert sync.annotation("notice", "a\n::error::fake%") == "::notice::a%0A::error::fake%25"


def test_ids_are_read_from_ids_urls_and_links() -> None:
    dashed = "2f1d4a21-e574-81f6-bef8-000b98cc5e42"
    assert sync.notion_id(dashed) == SOURCE
    assert sync.notion_id(f"collection://{dashed}") == SOURCE
    assert sync.notion_id(f"https://www.notion.so/Tasks-{DATABASE}?v=1") == DATABASE
    assert sync.notion_id("not an id") is None


# --- the HTTP function, with urlopen faked -------------------------------------------------------------------------


def http_with(monkeypatch: pytest.MonkeyPatch, answers: list[Any]) -> tuple[Any, list[Any], list[float]]:
    sent: list[Any] = []
    slept: list[float] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        sent.append(request)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    return sync.urllib_http("secret-test-token", sleep=slept.append), sent, slept


def busy(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://api.notion.com/v1/pages/x", code, "busy", headers, io.BytesIO(b"{}"))


def test_busy_or_colliding_notion_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    send, sent, slept = http_with(monkeypatch, [busy(429, "30"), busy(409), io.BytesIO(b'{"object": "page"}')])
    assert send("PATCH", "/pages/x", {"properties": {}}) == {"object": "page"}
    assert slept == [30.0, 1.0] and len(sent) == 3  # the full Retry-After, not cut to 10 seconds
    assert sent[0].get_header("Notion-version") == sync.NOTION_VERSION
    assert sent[0].get_header("Authorization") == "Bearer secret-test-token"


def test_a_dropped_connection_is_tried_again_then_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    send, sent, _ = http_with(monkeypatch, [http.client.RemoteDisconnected("gone"), TimeoutError(), io.BytesIO(b"{}")])
    assert send("GET", "/x", None) == {} and len(sent) == 3
    send, _, _ = http_with(monkeypatch, [TimeoutError(), TimeoutError(), http.client.IncompleteRead(b"")])
    with pytest.raises(sync.SyncError, match="could not reach Notion") as caught:
        send("GET", "/x", None)
    assert not isinstance(caught.value, sync.NotionError)


def test_a_reply_that_isnt_json_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    send, _, _ = http_with(monkeypatch, [io.BytesIO(b"<html>Just a moment...</html>")])
    with pytest.raises(sync.SyncError, match="isn't JSON"):
        send("GET", "/x", None)
