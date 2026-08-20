"""The Todoist client: the one place this project speaks HTTP to Todoist.

:mod:`reconciliation` works out what has to happen and says it in four calls -
list, create, update, delete - and this module is the implementation of them
that talks to the real API. Nothing above it knows about tokens, cursors or
HTTP status codes; nothing here knows what a schedule is.

Three things make a todo ours, and each answers a different question:

* **the label** answers *is this one of ours?* A listing asks Todoist for the
  to-dos carrying it, whatever project they have ended up in;
* **the marker** in the description answers *which collection is it for?* The
  content is a sentence for a person to read, and a person may rewrite it, so
  the identity is kept somewhere a person has no reason to touch;
* **the project** from the configuration - and the section within it, when one
  is named - answers *where do they go?* New to-dos are created there, and an
  existing one is moved back when either setting changes.

A todo that has lost its marker is left completely alone: it carries our label,
but nothing says what it stands for, and removing something we cannot identify
is not ours to do.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import httpx

from .configuration import TOKEN_ENV_VAR, Config
from .data_collection import USER_AGENT
from .data_processing import Collection
from .reconciliation import RemoteTask

log = logging.getLogger(__name__)

API_URL = "https://api.todoist.com/api/v1"

#: What ownership means here. Changing it orphans every todo written under the
#: old one: they would drop out of the listing, so nothing would ever update or
#: remove them again.
LABEL = "garbage-collection"

#: How a todo says which collection it stands for. Written into the description
#: rather than derived from the content, so renaming a todo cannot make this
#: project lose track of it - or, worse, create a second one beside it.
MARKER = re.compile(r"\[gca:(\d{4}-\d{2}-\d{2}):([a-z0-9]+)\]")

#: ``remind_days_before`` is in days; the API takes an offset in minutes.
_MINUTES_PER_DAY = 24 * 60

#: Todoist gives itself 60 seconds; a job that runs unattended need not wait that long.
_TIMEOUT_SECONDS = 20.0

#: One attempt and two more. What may be repeated is decided in ``_request()``.
_ATTEMPTS = 3
_BACKOFF_SECONDS = 2.0
#: However long Todoist asks us to wait, this is as long as a run will.
_MAX_WAIT_SECONDS = 30.0

#: How many names an error message may list before it stops being one.
_NAMED_IN_ERRORS = 10

#: The API's maximum page, and a ceiling on how many we will follow: this
#: listing is a few dozen to-dos, so more than that means the cursor is broken
#: and a cron run must not spin on it forever.
_PAGE_SIZE = 200
_MAX_PAGES = 20

#: Worth trying again. 429 means the request was refused before it was carried
#: out; the rest are Todoist having a moment.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class TodoistError(Exception):
    """Todoist could not be reached, or refused what was asked of it.

    Carries the HTTP status when there was one, so a caller that knows what a
    particular code means - a delete finding the todo already gone - can say so.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Todoist:
    """Todoist as :mod:`reconciliation` needs to see it; it implements ``TodoistClient``.

    Pass *client* to supply your own transport; otherwise one is opened here and
    closed again by :meth:`close`, which is what :mod:`data_export` does around
    every run that builds its own.
    """

    def __init__(self, config: Config, *, client: httpx.Client | None = None) -> None:
        todoist = config.export.todoist
        if not todoist.token.strip():
            # configuration.load() switches the export off when there is no
            # token, so getting here means someone built a client by hand.
            raise TodoistError(
                f"no todoist token is configured; set {TOKEN_ENV_VAR} or [export.todoist] token"
            )
        self._token = todoist.token.strip()
        self._project_name = todoist.project
        #: Empty when the to-dos go in the project itself rather than a section of it.
        self._section_name = todoist.section.strip()
        self._reminder_offset = todoist.remind_days_before * _MINUTES_PER_DAY
        self._due_time = config.collection.due_time
        self._client = client if client is not None else httpx.Client()
        self._owns_client = client is None
        self._project_id: str | None = None
        self._section_id: str | None = None
        #: Whether reminders are worth asking for at all. Turned off for the rest
        #: of the run the first time Todoist refuses one; see _reminders_or_not().
        self._reminders = True
        #: Which project - and which section of it, if any - each listed todo is
        #: in, so an update knows whether to move it. Only ever what this client
        #: saw itself.
        self._project_of: dict[str, str] = {}
        self._section_of: dict[str, str] = {}

    def close(self) -> None:
        """Close the transport, unless it was handed to us."""
        if self._owns_client:
            self._client.close()

    # --- what reconciliation asks for --------------------------------------------

    def list_tasks(self) -> list[RemoteTask]:
        """Every open todo carrying our label, in whichever project it sits.

        Deliberately not filtered by project: a todo that was moved - by hand,
        or by a change to ``[export.todoist] project`` - is still ours, and this
        is the only way anything ever gets to notice.
        """
        tasks: list[RemoteTask] = []
        ignored = 0
        for item in self._pages("tasks", {"label": LABEL}):
            task = _as_remote_task(item)
            if task is None:
                ignored += 1
                continue
            tasks.append(task)
            self._project_of[task.task_id] = str(item.get("project_id") or "")
            self._section_of[task.task_id] = str(item.get("section_id") or "")
        log.debug("todoist holds %d todo(s) with the %s label", len(tasks) + ignored, LABEL)
        return tasks

    def create_task(self, collection: Collection) -> str:
        """Write the todo for *collection* and return its Todoist id."""
        section = self._section()
        created = self._post(
            "tasks",
            {
                "content": _content(collection),
                "description": _description(collection),
                "project_id": self._project(),
                # Left out altogether when no section is configured: Todoist then
                # puts the todo in the project itself, which is what that means.
                **({"section_id": section} if section else {}),
                "labels": [LABEL],
                "due_datetime": _due(collection.due_at(self._due_time)),
                # Todoist would otherwise add the account's default reminder on
                # top of the one below, which is the only one we ask for.
                "auto_reminder": False,
            },
        )
        task_id = str(created.get("id") or "")
        if not task_id:
            raise TodoistError("todoist created a todo without saying which one")
        self._project_of[task_id] = self._project()
        self._section_of[task_id] = section
        self._add_reminder(task_id)
        return task_id

    def update_task(self, task_id: str, collection: Collection) -> None:
        """Rewrite an existing todo so it says what *collection* says.

        The labels are left exactly as they are: ours is already among them -
        that is how the todo was found - and sending a list would drop any a
        person has added of their own.
        """
        self._post(
            f"tasks/{task_id}",
            {
                "content": _content(collection),
                "description": _description(collection),
                "due_datetime": _due(collection.due_at(self._due_time)),
            },
        )
        self._move(task_id)
        self._fix_reminder(task_id)

    def delete_task(self, task_id: str) -> None:
        """Remove a todo. One that is already gone is one less thing to do."""
        try:
            self._request("DELETE", f"tasks/{task_id}")
        except TodoistError as exc:
            if exc.status != httpx.codes.NOT_FOUND:
                raise
            log.info("todo %s was gone from todoist already", task_id)

    # --- the bits of a todo that are not the todo --------------------------------

    def _add_reminder(self, task_id: str) -> None:
        """Set the reminder a freshly created todo has none of yet.

        It is relative to the due moment rather than a date of its own, so a
        collection that moves takes its reminder along without being asked.
        """
        if not self._reminders:
            return
        with self._reminders_or_not():
            self._post(
                "reminders",
                {
                    "task_id": task_id,
                    "reminder_type": "relative",
                    "minute_offset": self._reminder_offset,
                },
            )

    def _fix_reminder(self, task_id: str) -> None:
        """Make an existing todo's reminder agree with ``remind_days_before``.

        Only the first relative reminder is ours to touch; anything else on the
        todo was put there by a person, and a rewrite is not a reason to lose it.
        """
        if not self._reminders:
            return
        with self._reminders_or_not():
            for item in self._pages("reminders", {"task_id": task_id}):
                if item.get("type") != "relative":
                    continue
                reminder_id = str(item.get("id") or "")
                if not reminder_id:
                    continue
                if item.get("minute_offset") == self._reminder_offset:
                    return
                self._post(f"reminders/{reminder_id}", {"minute_offset": self._reminder_offset})
                return
        self._add_reminder(task_id)

    @contextlib.contextmanager
    def _reminders_or_not(self) -> Iterator[None]:
        """Let a reminder call fail the run for any reason except a refusal.

        Custom reminders are a Todoist Pro feature, and an account without it
        answers 403 to every one of them. That is Todoist saying what this
        account may have, not this run going wrong: the todo itself is written,
        due moment and all, and only the reminder on it is missing. So say it
        once, and stop asking for the rest of the run.
        """
        try:
            yield
        except TodoistError as exc:
            if exc.status != httpx.codes.FORBIDDEN:
                raise
            self._reminders = False
            log.warning(
                "todoist refused a reminder (HTTP 403) and none will be asked for again "
                "this run; custom reminders need Todoist Pro, and an API token that may "
                "write. The to-dos themselves are written as usual, each due at %s on the "
                "collection day - only the reminder %d day(s) beforehand is missing",
                self._due_time.isoformat("minutes"),
                self._reminder_offset // _MINUTES_PER_DAY,
            )

    def _move(self, task_id: str) -> None:
        """Put a todo back where the configuration says it goes, if it is elsewhere.

        Neither the project nor the section can be changed through an update, so
        this is its own call - and it is made only when the listing showed the
        todo somewhere else.

        A section id carries its project along with it, so it is the whole answer
        when a section is configured. Without one, only the project is ours to
        insist on: where inside it someone has filed a todo is their business.
        """
        where = self._project_of.get(task_id)
        if where is None:
            return
        section = self._section()
        if section:
            if self._section_of.get(task_id) == section:
                return
            destination = {"section_id": section}
            name = f"{self._project_name} / {self._section_name}"
        else:
            if where == self._project():
                return
            destination = {"project_id": self._project()}
            name = self._project_name
        self._post(f"tasks/{task_id}/move", destination)
        self._project_of[task_id] = self._project()
        self._section_of[task_id] = section
        log.info("moved todo %s to %s", task_id, name)

    def _project(self) -> str:
        """The id of the configured project, looked up once per client."""
        if self._project_id is None:
            self._project_id = self._find_project()
        return self._project_id

    def _find_project(self) -> str:
        wanted = self._project_name.strip().casefold()
        names: list[str] = []
        for item in self._pages("projects", {}):
            name = str(item.get("name") or "")
            project_id = str(item.get("id") or "")
            if name and project_id:
                names.append(name)
                if name.strip().casefold() == wanted:
                    log.debug("todoist project %r is %s", name, project_id)
                    return project_id
        # Creating it would turn a typo into a second project full of to-dos
        # nobody looks at, so say what is there and let a person decide.
        raise TodoistError(
            f"todoist has no project named '{self._project_name}'; it has "
            f"{_listed(names)} - create it, or correct [export.todoist] project"
        )

    def _section(self) -> str:
        """The id of the configured section, or "" when the to-dos go in the project.

        Looked up once per client, like the project, and only when there is a
        name to look up: an export without a section never asks Todoist for one.
        """
        if not self._section_name:
            return ""
        if self._section_id is None:
            self._section_id = self._find_section()
        return self._section_id

    def _find_section(self) -> str:
        wanted = self._section_name.casefold()
        names: list[str] = []
        for item in self._pages("sections", {"project_id": self._project()}):
            name = str(item.get("name") or "")
            section_id = str(item.get("id") or "")
            if name and section_id:
                names.append(name)
                if name.strip().casefold() == wanted:
                    log.debug("todoist section %r is %s", name, section_id)
                    return section_id
        # Same reasoning as the project above: a typo is not permission to add
        # something to someone's project.
        raise TodoistError(
            f"todoist project '{self._project_name}' has no section named "
            f"'{self._section_name}'; it has {_listed(names)} - create it, or "
            "correct [export.todoist] section"
        )

    # --- talking to the API ------------------------------------------------------

    def _pages(self, path: str, params: dict) -> Iterator[dict]:
        """Walk a paginated listing, one object at a time."""
        cursor = ""
        for _ in range(_MAX_PAGES):
            page = self._get(
                path, {**params, "limit": _PAGE_SIZE} | ({"cursor": cursor} if cursor else {})
            )
            results = page.get("results")
            if not isinstance(results, list):
                raise TodoistError(f"todoist answered /{path} without a list of results")
            yield from (item for item in results if isinstance(item, dict))
            cursor = str(page.get("next_cursor") or "")
            if not cursor:
                return
        raise TodoistError(f"todoist kept handing out more pages of /{path} than there can be")

    def _get(self, path: str, params: dict) -> dict:
        return _json(self._request("GET", path, params=params))

    def _post(self, path: str, payload: dict) -> dict:
        return _json(self._request("POST", path, payload=payload))

    def _request(
        self, method: str, path: str, *, params: dict | None = None, payload: dict | None = None
    ) -> httpx.Response:
        """One call to the API, repeated only where repeating it is safe.

        A GET and a DELETE may be repeated as often as we like. A POST may not:
        a request that never came back may still have created a todo, and asking
        again would create a second one. The one exception is a 429, which says
        the request was turned away before it was carried out - and Todoist says
        with it how long to wait. The request id goes along regardless: it is
        what lets Todoist recognise a repeat of a write it has already done.
        """
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }
        url = f"{API_URL}/{path}"
        # What the message says went wrong. A cron log is read hours later by
        # someone who has only these words to go on, and "todoist said no" is
        # a different problem depending on which call it said it to.
        call = f"{method} /{path}"
        repeatable = method in ("GET", "DELETE")

        for attempt in range(1, _ATTEMPTS + 1):
            wait = _BACKOFF_SECONDS * attempt
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=headers,
                    timeout=_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                problem = f"could not reach {httpx.URL(url).host} for {call} ({type(exc).__name__})"
                if not repeatable or attempt == _ATTEMPTS:
                    raise TodoistError(problem) from exc
            else:
                if response.is_success:
                    return response
                problem = _problem(response, call)
                status = response.status_code
                worth_repeating = status in _RETRY_STATUSES and (
                    repeatable or status == httpx.codes.TOO_MANY_REQUESTS
                )
                if not worth_repeating or attempt == _ATTEMPTS:
                    raise TodoistError(problem, status=status)
                wait = _retry_after(response, wait)

            log.warning("%s; retrying (%d/%d)", problem, attempt, _ATTEMPTS - 1)
            time.sleep(wait)

        raise AssertionError("unreachable")  # pragma: no cover


# --- what a todo looks like ------------------------------------------------------------


def _content(collection: Collection) -> str:
    """The line a person reads in Todoist."""
    return f"{collection.waste_type} buitenzetten"


def _description(collection: Collection) -> str:
    """The marker, and nothing else: it is how the next run recognises this todo."""
    return f"[gca:{collection.date.isoformat()}:{collection.code}]"


def _due(moment: datetime) -> str:
    """The due moment as the API takes it: RFC 3339, in UTC."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_remote_task(item: dict) -> RemoteTask | None:
    """The todo *item* stands for, or None when it does not say.

    Todoist's own idea of the due date is not consulted: it is stored in the
    account's timezone and shown in the reader's, and this project keeps to
    Dutch collection days. The marker is what was meant.
    """
    task_id = str(item.get("id") or "")
    found = MARKER.search(str(item.get("description") or ""))
    if not task_id or found is None:
        log.warning(
            "leaving todo %s alone: it carries the %s label but does not say which "
            "collection it is for",
            task_id or "(without an id)",
            LABEL,
        )
        return None
    try:
        day = date.fromisoformat(found.group(1))
    except ValueError:
        log.warning("leaving todo %s alone: it is marked for %s", task_id, found.group(1))
        return None
    return RemoteTask(task_id, day, found.group(2))


def _json(response: httpx.Response) -> dict:
    """The answer as an object. Only ``create_task`` reads one; the rest just check.

    An empty body is an empty object rather than a complaint: several of these
    calls have nothing to say beyond having worked.
    """
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise TodoistError(f"todoist answered with something that is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TodoistError(f"todoist answered with {type(payload).__name__}, not an object")
    return payload


def _listed(names: list[str]) -> str:
    """The *names* an error may recite, and a hint that there were more."""
    listing = ", ".join(sorted(names)[:_NAMED_IN_ERRORS]) or "none at all"
    return f"{listing}, ..." if len(names) > _NAMED_IN_ERRORS else listing


def _problem(response: httpx.Response, call: str) -> str:
    """What went wrong, in a sentence fit for a cron log.

    Todoist explains itself in the body, and whatever it said is worth more than
    anything guessed here, so it is always passed on. The two refusals are kept
    apart because they send the reader to opposite ends of the problem: 401 is
    the token, and 403 is everything the token is not allowed to do.
    """
    detail = " ".join(response.text.split())[:200]
    said = f": {detail}" if detail else ""
    if response.status_code == httpx.codes.UNAUTHORIZED:
        return (
            f"todoist rejected the token on {call} (HTTP 401); "
            f"check {TOKEN_ENV_VAR} or [export.todoist] token{said}"
        )
    if response.status_code == httpx.codes.FORBIDDEN:
        # Not the token itself: it got this far. Either it may not write - an
        # OAuth token without data:read_write - or the account's plan does not
        # include what was asked for, which is what a reminder runs into.
        return (
            f"todoist took the token but would not do {call} (HTTP 403); "
            f"the token may be read-only, or the account's plan does not "
            f"include this{said}"
        )
    return f"todoist returned HTTP {response.status_code} on {call}{said}"


def _retry_after(response: httpx.Response, fallback: float) -> float:
    """However long Todoist asks for, within reason; *fallback* when it says nothing."""
    header = response.headers.get("Retry-After", "").strip()
    try:
        asked = float(header)
    except ValueError:
        return fallback
    return min(max(asked, 0.0), _MAX_WAIT_SECONDS)
