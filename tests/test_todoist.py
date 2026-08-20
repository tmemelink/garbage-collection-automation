"""The Todoist client. No test here touches the network; every reply is canned."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from garbage_collection_automation import reconciliation, state, todoist
from garbage_collection_automation.configuration import TodoistExportConfig
from garbage_collection_automation.data_processing import Collection
from garbage_collection_automation.reconciliation import RemoteTask
from garbage_collection_automation.todoist import LABEL, Todoist, TodoistError

from .conftest import make_config
from .test_reconciliation import GFT, RESTAFVAL, TODAY, config

#: The project the configuration names, as Todoist knows it.
HOME = "77"
#: A section in it, for the tests that configure one.
RECURRING = "88"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retries are real seconds in production; keep the suite instant."""
    monkeypatch.setattr(todoist, "_BACKOFF_SECONDS", 0)


def reply(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def page(*items: dict, cursor: str | None = None) -> httpx.Response:
    return reply({"results": list(items), "next_cursor": cursor})


def project(project_id: str, name: str) -> dict:
    return {"id": project_id, "name": name}


def section(section_id: str, name: str) -> dict:
    return {"id": section_id, "name": name, "project_id": HOME}


def task(task_id: str, collection: Collection, *, project_id: str = HOME, **overrides) -> dict:
    """A todo as Todoist hands it back: ours, unless a test says otherwise."""
    return {
        "id": task_id,
        "content": f"{collection.waste_type} buitenzetten",
        "description": f"[gca:{collection.date.isoformat()}:{collection.code}]\nnotes of my own",
        "project_id": project_id,
        "labels": [LABEL],
    } | overrides


def body(request: httpx.Request) -> dict:
    return json.loads(request.content)


class Api:
    """Todoist as a table of canned replies, remembering every call it was sent.

    A route is ``"<METHOD> <path>"``; its value is one response, or a list handed
    out one per call with the last one repeating. The project listing comes for
    free: resolving the configured project by name is the first thing a write does.
    """

    def __init__(self, routes: dict | None = None) -> None:
        self.routes: dict = {"GET projects": page(project(HOME, "Home"))} | (routes or {})
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = f"{request.method} {request.url.path.removeprefix('/api/v1/')}"
        answer = self.routes.get(key)
        if answer is None:
            return reply({"error": f"the test has no route for {key}"}, status=404)
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def client(self, **todoist_config) -> Todoist:
        transport = httpx.MockTransport(self.handle)
        return Todoist(config(**todoist_config), client=httpx.Client(transport=transport))

    def calls(self, key: str) -> list[httpx.Request]:
        return [
            request
            for request in self.seen
            if f"{request.method} {request.url.path.removeprefix('/api/v1/')}" == key
        ]


# --- who we are ------------------------------------------------------------------------


def test_every_call_carries_the_token_and_says_who_is_calling():
    api = Api({"GET tasks": page()})

    api.client().list_tasks()

    sent = api.calls("GET tasks")[0]
    assert sent.headers["Authorization"] == "Bearer secret"
    assert sent.headers["User-Agent"].startswith("garbage-collection-automation/")
    assert sent.headers["X-Request-Id"], "what lets todoist recognise a repeated write"


def test_a_client_without_a_token_refuses_to_be_built():
    """configuration.load() switches the export off instead; this is the backstop."""
    tokenless = make_config(todoist=TodoistExportConfig(enabled=True, token=""))

    with pytest.raises(TodoistError, match="GCA_TODOIST_TOKEN"):
        Todoist(tokenless)


def test_a_client_that_opened_its_own_transport_closes_it():
    client = Todoist(config())

    client.close()

    assert client._client.is_closed


def test_a_borrowed_transport_is_left_open():
    """data_export hands over a transport in the tests; closing it is not ours to do."""
    borrowed = httpx.Client(transport=httpx.MockTransport(lambda request: reply({})))

    Todoist(config(), client=borrowed).close()

    assert not borrowed.is_closed


# --- reading what is there -------------------------------------------------------------


def test_the_listing_asks_for_our_label_and_nothing_else():
    """Ownership is the label. Not the project: a todo that was moved is still ours."""
    api = Api({"GET tasks": page(task("1", RESTAFVAL))})

    tasks = api.client().list_tasks()

    sent = api.calls("GET tasks")[0]
    assert sent.url.params["label"] == LABEL
    assert "project_id" not in sent.url.params
    assert tasks == [RemoteTask("1", date(2026, 8, 20), "restafval")]


def test_the_marker_says_which_collection_a_todo_is_for():
    """Not the content: a person may rewrite that, and then we must still know."""
    renamed = task("1", RESTAFVAL, content="Grijze bak!!")

    assert api_tasks(renamed) == [RemoteTask("1", date(2026, 8, 20), "restafval")]


def test_a_todo_without_a_marker_is_left_completely_alone(caplog):
    """Our label, but nothing saying what it stands for - removing it is not ours to do."""
    with caplog.at_level("WARNING"):
        assert api_tasks(task("1", RESTAFVAL, description="")) == []

    assert "does not say which collection" in caplog.text


def test_a_marker_that_is_not_a_real_date_is_left_alone_too(caplog):
    with caplog.at_level("WARNING"):
        assert api_tasks(task("1", RESTAFVAL, description="[gca:2026-02-31:gft]")) == []

    assert "2026-02-31" in caplog.text


def test_every_page_of_the_listing_is_read():
    api = Api(
        {
            "GET tasks": [
                page(task("1", RESTAFVAL), cursor="more"),
                page(task("2", GFT)),
            ]
        }
    )

    tasks = api.client().list_tasks()

    assert [item.task_id for item in tasks] == ["1", "2"]
    assert api.calls("GET tasks")[1].url.params["cursor"] == "more"


def test_a_cursor_that_never_ends_stops_the_run_rather_than_spinning():
    api = Api({"GET tasks": page(task("1", RESTAFVAL), cursor="and-another")})

    with pytest.raises(TodoistError, match="more pages"):
        api.client().list_tasks()


def test_an_answer_that_is_not_a_listing_is_refused():
    api = Api({"GET tasks": reply({"tasks": []})})

    with pytest.raises(TodoistError, match="without a list of results"):
        api.client().list_tasks()


def test_an_answer_that_is_not_json_is_refused():
    api = Api({"GET tasks": httpx.Response(200, text="<html>maintenance</html>")})

    with pytest.raises(TodoistError, match="not JSON"):
        api.client().list_tasks()


def api_tasks(*items: dict) -> list[RemoteTask]:
    return Api({"GET tasks": page(*items)}).client().list_tasks()


# --- writing --------------------------------------------------------------------------


def test_a_created_todo_says_what_it_is_for_twice_over():
    """Once in the sentence a person reads, once in the marker the next run reads."""
    api = Api({"POST tasks": reply({"id": "42"}), "POST reminders": reply({"id": "r1"})})

    assert api.client().create_task(RESTAFVAL) == "42"

    sent = body(api.calls("POST tasks")[0])
    assert sent["content"] == "Restafval buitenzetten"
    assert "[gca:2026-08-20:restafval]" in sent["description"]
    assert sent["labels"] == [LABEL]
    assert sent["project_id"] == HOME


def test_a_created_todo_is_due_at_the_configured_time_in_dutch_terms():
    """07:00 in Amsterdam is 05:00 UTC in August; the API is told the moment, not the clock."""
    api = Api({"POST tasks": reply({"id": "42"}), "POST reminders": reply({"id": "r1"})})

    api.client().create_task(RESTAFVAL)

    assert body(api.calls("POST tasks")[0])["due_datetime"] == "2026-08-20T05:00:00Z"


def test_the_only_reminder_a_todo_gets_is_the_configured_one():
    api = Api({"POST tasks": reply({"id": "42"}), "POST reminders": reply({"id": "r1"})})

    api.client(remind_days_before=2).create_task(RESTAFVAL)

    assert body(api.calls("POST tasks")[0])["auto_reminder"] is False
    reminder = body(api.calls("POST reminders")[0])
    assert reminder == {"task_id": "42", "reminder_type": "relative", "minute_offset": 2 * 1440}


def test_a_todo_is_still_written_when_the_account_may_not_have_reminders(caplog):
    """Custom reminders are a Todoist Pro feature; a free account answers 403 to every one.

    Losing the whole export over that would leave every run half done, and the
    todo - due moment and all - is the point of this project.
    """
    api = Api(
        {
            "POST tasks": reply({"id": "42"}),
            "POST reminders": reply({"error": "Feature not available"}, status=403),
        }
    )

    with caplog.at_level("WARNING"):
        assert api.client().create_task(RESTAFVAL) == "42"

    assert "Todoist Pro" in caplog.text


def test_a_refused_reminder_is_asked_for_once_and_then_left_alone(caplog):
    api = Api(
        {
            "POST tasks": [reply({"id": "42"}), reply({"id": "43"})],
            "POST reminders": reply({"error": "Feature not available"}, status=403),
        }
    )
    client = api.client()

    with caplog.at_level("WARNING"):
        client.create_task(RESTAFVAL)
        client.create_task(GFT)

    assert len(api.calls("POST reminders")) == 1, "the second todo knows better than to ask"
    assert caplog.text.count("Todoist Pro") == 1, "said once, not once per todo"


def test_a_rewrite_leaves_the_reminder_alone_when_the_account_may_not_have_one(caplog):
    """The same refusal, met while reading rather than writing."""
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL)),
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": reply({"error": "Feature not available"}, status=403),
        }
    )

    with caplog.at_level("WARNING"):
        api.client().update_task("42", RESTAFVAL)

    assert api.calls("POST reminders") == [], "asking again would only be refused again"
    assert "Todoist Pro" in caplog.text


def test_a_reminder_refused_for_any_other_reason_still_fails_the_run():
    """Only a refusal is the account's plan; a 500 is Todoist having a moment."""
    api = Api({"POST tasks": reply({"id": "42"}), "POST reminders": reply({}, status=500)})

    with pytest.raises(TodoistError, match="HTTP 500 on POST /reminders"):
        api.client().create_task(RESTAFVAL)


def test_a_creation_that_does_not_say_which_todo_it_made_is_refused():
    """An id we never learned is a todo the record cannot name; better to fail loudly."""
    api = Api({"POST tasks": reply({"ok": True})})

    with pytest.raises(TodoistError, match="without saying which one"):
        api.client().create_task(RESTAFVAL)


def test_the_project_is_looked_up_by_name_and_only_once():
    api = Api(
        {
            "GET projects": page(project("1", "Werk"), project(HOME, "home")),
            "POST tasks": reply({"id": "42"}),
            "POST reminders": reply({"id": "r1"}),
        }
    )
    client = api.client()

    client.create_task(RESTAFVAL)
    client.create_task(GFT)

    assert len(api.calls("GET projects")) == 1, "one lookup per client, not per todo"
    assert body(api.calls("POST tasks")[0])["project_id"] == HOME


def test_a_project_that_does_not_exist_is_said_out_loud_rather_than_created():
    """Creating it would turn a typo into a second project nobody ever looks at."""
    api = Api({"GET projects": page(project("1", "Werk"))})

    with pytest.raises(TodoistError, match="no project named 'Home'; it has Werk"):
        api.client().create_task(RESTAFVAL)


def test_an_account_full_of_projects_is_not_recited_in_full():
    """The message goes in a cron log; a hundred names in it help nobody."""
    api = Api({"GET projects": page(*(project(str(n), f"Project {n}") for n in range(11)))})

    with pytest.raises(TodoistError, match=r"Project \d+, \.\.\. - create it"):
        api.client().create_task(RESTAFVAL)


# --- the section within the project ----------------------------------------------------


def test_a_todo_goes_in_the_configured_section():
    api = Api(
        {
            "GET sections": page(section("1", "Errands"), section(RECURRING, "recurring")),
            "POST tasks": reply({"id": "42"}),
            "POST reminders": reply({"id": "r1"}),
        }
    )
    client = api.client(section="Recurring")

    client.create_task(RESTAFVAL)
    client.create_task(GFT)

    sent = body(api.calls("POST tasks")[0])
    assert sent["section_id"] == RECURRING
    assert sent["project_id"] == HOME, "the project is still said outright"
    assert api.calls("GET sections")[0].url.params["project_id"] == HOME
    assert len(api.calls("GET sections")) == 1, "one lookup per client, not per todo"


def test_no_section_configured_means_the_project_itself_and_no_lookup():
    """Todoist is never asked about sections by an export that names none."""
    api = Api({"POST tasks": reply({"id": "42"}), "POST reminders": reply({"id": "r1"})})

    api.client().create_task(RESTAFVAL)

    assert "section_id" not in body(api.calls("POST tasks")[0])
    assert api.calls("GET sections") == []


def test_a_section_that_does_not_exist_is_said_out_loud_rather_than_created():
    """Same reasoning as the project: a typo is not permission to add one."""
    api = Api({"GET sections": page(section("1", "Errands"))})

    with pytest.raises(
        TodoistError, match="project 'Home' has no section named 'Recurring'; it has Errands"
    ):
        api.client(section="Recurring").create_task(RESTAFVAL)


def test_a_project_full_of_sections_is_not_recited_in_full():
    api = Api({"GET sections": page(*(section(str(n), f"Section {n}") for n in range(11)))})

    with pytest.raises(TodoistError, match=r"Section \d+, \.\.\. - create it"):
        api.client(section="Recurring").create_task(RESTAFVAL)


def test_a_todo_in_the_wrong_section_is_moved_back_when_it_is_rewritten():
    """One move says both: a section id carries its project along with it."""
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL, project_id="9", section_id="3")),
            "GET sections": page(section(RECURRING, "Recurring")),
            "POST tasks/42": reply({"id": "42"}),
            "POST tasks/42/move": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )
    client = api.client(section="Recurring")

    client.list_tasks()
    client.update_task("42", RESTAFVAL)

    assert body(api.calls("POST tasks/42/move")[0]) == {"section_id": RECURRING}


def test_a_todo_already_in_the_configured_section_is_not_moved():
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL, section_id=RECURRING)),
            "GET sections": page(section(RECURRING, "Recurring")),
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )
    client = api.client(section="Recurring")

    client.list_tasks()
    client.update_task("42", RESTAFVAL)

    assert api.calls("POST tasks/42/move") == []


def test_a_todo_filed_in_a_section_is_left_there_when_none_is_configured():
    """Without a section to insist on, where inside the project it sits is not ours."""
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL, section_id="3")),
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )
    client = api.client()

    client.list_tasks()
    client.update_task("42", RESTAFVAL)

    assert api.calls("POST tasks/42/move") == []


def test_a_call_that_answers_with_nothing_at_all_is_still_an_answer():
    """Not every write has something to say; only a created todo is read back."""
    api = Api(
        {
            "POST tasks/42": httpx.Response(204),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )

    api.client().update_task("42", RESTAFVAL)

    assert len(api.calls("POST tasks/42")) == 1


def test_a_rewrite_leaves_the_persons_content_description_and_labels_alone():
    """The marker already identifies it; sending any of these would replace a person's edits."""
    api = Api(
        {
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )

    api.client().update_task("42", RESTAFVAL)

    sent = body(api.calls("POST tasks/42")[0])
    assert "labels" not in sent
    assert "content" not in sent
    assert "description" not in sent
    assert sent["due_datetime"] == "2026-08-20T05:00:00Z"


def test_a_rewrite_leaves_a_reminder_that_is_already_right_untouched():
    api = Api(
        {
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )

    api.client().update_task("42", RESTAFVAL)

    assert api.calls("POST reminders/r1") == []
    assert api.calls("POST reminders") == []


def test_a_rewrite_moves_the_reminder_when_the_configuration_moved_it():
    api = Api(
        {
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
            "POST reminders/r1": reply({"id": "r1"}),
        }
    )

    api.client(remind_days_before=3).update_task("42", RESTAFVAL)

    assert body(api.calls("POST reminders/r1")[0]) == {"minute_offset": 3 * 1440}


def test_a_rewrite_gives_back_a_reminder_that_was_lost():
    """A create that failed halfway leaves a todo without one; the rewrite repairs it."""
    api = Api(
        {
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page(
                {"id": "r9", "type": "absolute", "due": {}},
                {"type": "relative"},  # one todoist described without an id
            ),
            "POST reminders": reply({"id": "r1"}),
        }
    )

    api.client().update_task("42", RESTAFVAL)

    assert body(api.calls("POST reminders")[0])["task_id"] == "42"


def test_a_todo_in_another_project_is_moved_back_when_it_is_rewritten():
    """``project`` changed, and an update cannot carry a todo across on its own."""
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL, project_id="9")),
            "POST tasks/42": reply({"id": "42"}),
            "POST tasks/42/move": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )
    client = api.client()

    client.list_tasks()
    client.update_task("42", RESTAFVAL)

    assert body(api.calls("POST tasks/42/move")[0]) == {"project_id": HOME}


def test_a_todo_already_in_the_configured_project_is_not_moved():
    api = Api(
        {
            "GET tasks": page(task("42", RESTAFVAL)),
            "POST tasks/42": reply({"id": "42"}),
            "GET reminders": page({"id": "r1", "type": "relative", "minute_offset": 1440}),
        }
    )
    client = api.client()

    client.list_tasks()
    client.update_task("42", RESTAFVAL)

    assert api.calls("POST tasks/42/move") == []


def test_deleting_a_todo_deletes_it():
    api = Api({"DELETE tasks/42": httpx.Response(204)})

    api.client().delete_task("42")

    assert len(api.calls("DELETE tasks/42")) == 1


def test_a_todo_that_is_already_gone_is_one_less_thing_to_do(caplog):
    """Someone deleted it by hand between the listing and now. That is the wanted end."""
    api = Api({"DELETE tasks/42": reply({"error": "not found"}, status=404)})

    with caplog.at_level("INFO"):
        api.client().delete_task("42")

    assert "gone from todoist already" in caplog.text


# --- when todoist says no ---------------------------------------------------------------


def test_an_answer_that_is_not_an_object_is_refused():
    api = Api({"GET tasks": reply(["a list, of all things"])})

    with pytest.raises(TodoistError, match="not an object"):
        api.client().list_tasks()


def test_a_delete_that_fails_for_any_other_reason_still_fails():
    """Only "it is already gone" is the wanted end; a refusal is not."""
    api = Api({"DELETE tasks/42": reply({"error": "nope"}, status=403)})

    with pytest.raises(TodoistError, match="would not do DELETE /tasks/42"):
        api.client().delete_task("42")


def test_a_refused_token_says_where_to_put_the_right_one():
    api = Api({"GET tasks": reply({"error": "invalid token"}, status=401)})

    with pytest.raises(TodoistError, match="GCA_TODOIST_TOKEN") as raised:
        api.client().list_tasks()

    assert raised.value.status == 401


def test_a_refusal_names_the_call_and_does_not_blame_the_token():
    """403 is not 401: the token got this far, so sending someone to it wastes their evening."""
    api = Api({"POST tasks": reply({"error": "Feature not available"}, status=403)})

    with pytest.raises(TodoistError) as raised:
        api.client().create_task(RESTAFVAL)

    assert "POST /tasks" in str(raised.value)
    assert "Feature not available" in str(raised.value), "what todoist said beats what we guess"
    assert "read-only" in str(raised.value)
    assert raised.value.status == 403


def test_what_todoist_said_survives_a_refused_token_too():
    api = Api({"GET tasks": reply({"error": "token expired"}, status=401)})

    with pytest.raises(TodoistError, match="token expired"):
        api.client().list_tasks()


def test_an_unreachable_api_says_which_call_never_got_there():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = Todoist(config(), client=httpx.Client(transport=httpx.MockTransport(refuse)))

    with pytest.raises(TodoistError, match=r"for GET /tasks"):
        client.list_tasks()


def test_a_reading_call_is_tried_again():
    api = Api({"GET tasks": [reply({}, status=503), page(task("1", RESTAFVAL))]})

    assert len(api.client().list_tasks()) == 1
    assert len(api.calls("GET tasks")) == 2


def test_a_write_that_may_have_gone_through_is_not_repeated():
    """A second POST could create a second todo; failing is the safer of the two."""
    api = Api({"POST tasks": reply({"error": "oops"}, status=503)})

    with pytest.raises(TodoistError, match="HTTP 503"):
        api.client().create_task(RESTAFVAL)

    assert len(api.calls("POST tasks")) == 1


def test_a_write_that_was_turned_away_at_the_door_is_repeated():
    """429 means it was never carried out, so trying again cannot duplicate anything."""
    api = Api(
        {
            "POST tasks": [
                httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0"}),
                reply({"id": "42"}),
            ],
            "POST reminders": reply({"id": "r1"}),
        }
    )

    assert api.client().create_task(RESTAFVAL) == "42"
    assert len(api.calls("POST tasks")) == 2


def test_a_wait_todoist_asks_for_is_honoured_within_reason(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(todoist.time, "sleep", slept.append)
    api = Api(
        {
            "GET tasks": [
                httpx.Response(429, json={}, headers={"Retry-After": "5"}),
                httpx.Response(429, json={}, headers={"Retry-After": "9000"}),
                page(),
            ]
        }
    )

    api.client().list_tasks()

    assert slept == [5.0, todoist._MAX_WAIT_SECONDS]


def test_an_unreachable_api_is_tried_again_and_then_reported():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = Todoist(config(), client=httpx.Client(transport=httpx.MockTransport(refuse)))

    with pytest.raises(TodoistError, match=r"could not reach api\.todoist\.com"):
        client.list_tasks()


def test_a_write_is_not_repeated_when_the_answer_never_came():
    """The todo may well have been created; only Todoist can say, and the next run asks."""
    attempts = []

    def timeout(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects"):
            return page(project(HOME, "Home"))
        attempts.append(request)
        raise httpx.ReadTimeout("took too long")

    client = Todoist(config(), client=httpx.Client(transport=httpx.MockTransport(timeout)))

    with pytest.raises(TodoistError, match="could not reach"):
        client.create_task(RESTAFVAL)

    assert len(attempts) == 1


def test_a_failure_is_reported_in_one_readable_line():
    """A cron log is read at a glance; a wall of HTML in it is no use to anyone."""
    api = Api({"GET tasks": httpx.Response(500, text="<html>\n  <body>oh dear</body>\n</html>")})

    with pytest.raises(
        TodoistError, match=r"todoist returned HTTP 500 on GET /tasks: <html> <body>oh dear"
    ):
        api.client().list_tasks()


# --- and all of it at once --------------------------------------------------------------


def test_a_reconciliation_runs_through_the_real_client(tmp_path):
    """Where the two halves meet: reconciliation's four calls, and the API's answers."""
    api = Api(
        {
            "GET tasks": page(),
            "POST tasks": [reply({"id": "a"}), reply({"id": "b"})],
            "POST reminders": reply({"id": "r"}),
        }
    )
    state_path = tmp_path / "state.json"

    report = reconciliation.reconcile(
        [RESTAFVAL, GFT], config(), state_path=state_path, client=api.client(), today=TODAY
    )

    assert report.delta.create == (RESTAFVAL, GFT)
    assert [record.task_id for record in state.load(state_path).tasks] == ["a", "b"]
    assert len(api.calls("POST reminders")) == 2, "one per todo, and none left over"
