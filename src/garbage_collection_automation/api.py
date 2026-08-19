"""What the page asks the server for: the JSON behind the buttons and the form.

:mod:`web` is the HTTP in front of this and knows nothing about the job; this
module is the job as the page sees it, and knows nothing about sockets. Four
things happen here, in order of how much they change:

===============  ==========================  ==================================
``state()``      reads the config and the    nothing is written, nothing is
                 local record                fetched
``collect()``    asks mijnafvalwijzer.nl     nothing is written
``check()``      also asks Todoist           nothing is written
``apply()``      also writes the to-dos      and the state file
===============  ==========================  ==================================

Everything but ``state()`` is a real run of the pipeline, so all of them go
through :func:`_locked`, which takes the same lock file ``run-job.sh`` holds -
the cron run and a pressed button must never be inside the export at once.

The page also shows what the run said while it ran, so every action captures the
log records the job emitted and hands them back with the result.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import application, configuration, state
from .application import JobResult
from .configuration import WASTE_TYPES, Config, ConfigError
from .data_processing import Collection
from .reconciliation import Delta

log = logging.getLogger(__name__)

#: The lock ``run-job.sh`` takes, in both layouts: next to the state file it guards.
LOCK_NAME = "run.lock"

#: Where the installer puts the schedule. The page shows it and cannot change it:
#: the file is root's, and a service user that could rewrite a crontab would be a
#: service user that could run anything as anyone. Editing it is an ssh away.
DEFAULT_CRON = Path("/etc/cron.d/garbage-collection-automation")

#: Everything the pipeline logs is under this name, and nothing else is.
LOG_ROOT = __name__.rsplit(".", 1)[0]

#: One run's worth of log lines is a few dozen; this is only a ceiling on a
#: pathological one, so the response cannot grow without bound.
MAX_LOG_LINES = 500


class Busy(Exception):
    """Another run - the cron job, or another tab - is inside the export already."""


class ApiError(Exception):
    """The request was understood and refused. The message is shown on the page."""


@dataclass(frozen=True)
class Paths:
    """Where this server's copy of the application keeps its files."""

    config: Path
    state: Path
    #: Read for display only; see DEFAULT_CRON.
    cron: Path = DEFAULT_CRON

    @property
    def lock(self) -> Path:
        return self.state.parent / LOCK_NAME


#: Two requests in this process must not both be in the pipeline; the flock below
#: keeps other processes out, and this keeps our own threads out.
_in_process = threading.Lock()


# --- what the page shows -------------------------------------------------------------


def state_payload(config: Config, paths: Paths) -> dict:
    """Everything the page needs to draw itself, without touching the network."""
    return {
        "config": _config_payload(config, paths),
        "last_run": _last_run_payload(paths.state),
        "schedule": _schedule_payload(paths.cron),
    }


def _config_payload(config: Config, paths: Paths) -> dict:
    todoist = config.export.todoist
    from_env = bool(os.environ.get(configuration.TOKEN_ENV_VAR, "").strip())
    key_from_env = bool(os.environ.get(configuration.API_KEY_ENV_VAR, "").strip())
    return {
        "postcode": config.address.postcode,
        "house_number": config.address.house_number,
        "addition": config.address.addition,
        "api_key": config.collection.api_key,
        # Same story as token_from_environment below, for the schedule API's key.
        "api_key_from_environment": key_from_env,
        "lookahead_days": config.collection.lookahead_days,
        "due_time": config.collection.due_time.isoformat("minutes"),
        "types": list(config.collection.types),
        "known_types": [{"code": code, "label": label} for code, label in WASTE_TYPES.items()],
        "todoist_enabled": todoist.enabled,
        "todoist_token": todoist.token,
        "todoist_project": todoist.project,
        "remind_days_before": todoist.remind_days_before,
        # The page grays the token field out when this is true: the environment
        # wins over the file, so saving one from here would change nothing.
        "token_from_environment": from_env,
        "config_path": str(paths.config),
        "state_path": str(paths.state),
        "writable": _writable(paths.config),
    }


def _writable(path: Path) -> bool:
    """Whether a save would land. The installer decides this, not the page.

    A save is a temp file next to the config and a rename over it, so the
    directory has to allow it - and the file itself has to as well, but only
    once there is one: ``configuration.save()`` writes a config that is not
    there yet rather than refusing. This asks the same question in advance, and
    has to answer it the same way, or the page grays out a button that works.
    """
    if path.exists() and not os.access(path, os.W_OK):
        return False
    return os.access(path.parent, os.W_OK | os.X_OK)


def _schedule_payload(cron_path: Path) -> dict:
    """When cron runs the job, for the page to show. Never written from here."""
    return {"cron": _cron_line(cron_path), "path": str(cron_path)}


def _cron_line(cron_path: Path) -> str | None:
    """The five schedule fields out of the crontab, or None when there is no file.

    A checkout has none - install.sh is what writes it - and neither has a
    container where the job was installed with --no-cron.
    """
    try:
        text = cron_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in text.splitlines():
        line = line.strip()
        # Comments, blanks, and the SHELL=/PATH=/MAILTO= settings above the entry.
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        fields = line.split()
        if len(fields) >= 6:
            return " ".join(fields[:5])
    return None


def _last_run_payload(state_path: Path) -> dict:
    """The local record of the last export - what the page shows before any button."""
    known = state.load(state_path)
    return {
        "known": known.known,
        "complete": known.complete,
        "address": known.address,
        "data_version": known.data_version,
        "updated_at": known.updated_at.isoformat() if known.updated_at else None,
        "tasks": [
            {
                "date": task.date.isoformat(),
                "code": task.code,
                "waste_type": WASTE_TYPES.get(task.code, task.code.title()),
                "task_id": task.task_id,
            }
            for task in known.tasks
        ],
    }


# --- the three buttons ---------------------------------------------------------------


def collect(config: Config, paths: Paths) -> dict:
    """Fetch the schedule and say what it would mean. Writes nothing, asks no API."""
    with _locked(paths), _captured() as lines:
        result = application.run(config, state_path=paths.state, dry_run=True)
    return _action_payload(result, config, paths, lines)


def check(config: Config, paths: Paths) -> dict:
    """Also ask Todoist what it really holds, and show the difference. Writes nothing."""
    with _locked(paths), _captured() as lines:
        result = application.check(config, state_path=paths.state)
    return _action_payload(result, config, paths, lines)


def apply(config: Config, paths: Paths) -> dict:
    """The real run: write the to-dos and record them. What cron does every morning."""
    with _locked(paths), _captured() as lines:
        result = application.run(config, state_path=paths.state, dry_run=False)
    return _action_payload(result, config, paths, lines)


# --- saving the form -----------------------------------------------------------------

#: What the form may change. Everything else in config.toml - the query limits,
#: [web], [logging] - is left exactly as the file has it, so a save through the
#: page never silently drops a key the page does not know about.
FORM_FIELDS = frozenset(
    {
        "postcode",
        "house_number",
        "addition",
        "api_key",
        "lookahead_days",
        "due_time",
        "types",
        "todoist_enabled",
        "todoist_token",
        "todoist_project",
        "remind_days_before",
    }
)


def save_config(payload: dict, paths: Paths) -> dict:
    """Write the form to config.toml and return the configuration now on disk.

    The form is overlaid on the document already there and handed to
    :func:`configuration.from_data`, so every value is refused in the same words
    the file would be refused in, and nothing is written until all of it passes.

    The running server keeps serving the configuration it started with: [web]
    only takes effect at startup, and the pipeline reads the file per action.
    """
    unknown = sorted(set(payload) - FORM_FIELDS)
    if unknown:
        raise ApiError(f"unknown field(s): {', '.join(unknown)}")

    try:
        document = configuration.read_toml(paths.config)
    except ConfigError as exc:
        raise ApiError(str(exc)) from exc

    token = _secret_to_write(
        document.get("export", {}).get("todoist", {}),
        payload,
        field="todoist_token",
        key="token",
        env_var=configuration.TOKEN_ENV_VAR,
    )
    api_key = _secret_to_write(
        document.get("collection", {}),
        payload,
        field="api_key",
        key="api_key",
        env_var=configuration.API_KEY_ENV_VAR,
    )
    _overlay(document, payload)

    try:
        config = configuration.from_data(document)
        configuration.save(paths.config, config, token=token, api_key=api_key)
    except ConfigError as exc:
        raise ApiError(str(exc)) from exc

    log.info("configuration saved to %s from the web interface", paths.config)
    return {"config": _config_payload(config, paths), "saved": True}


def _secret_to_write(section: dict, payload: dict, *, field: str, key: str, env_var: str) -> str:
    """The secret that lands in the file - which is not always the one in the form.

    *section* is the parsed table it lives in under *key*, and *field* is the
    form's name for it. The environment wins over the file everywhere else, so
    writing the form's value while *env_var* is set would put a second copy in a
    file that nothing reads. The file's own value is kept instead.
    """
    existing = section.get(key, "")
    existing = existing if isinstance(existing, str) else ""
    if os.environ.get(env_var, "").strip():
        return existing
    if field not in payload:
        return existing

    value = payload[field]
    if not isinstance(value, str):
        raise ApiError(f"{field} must be a string")
    return value.strip()


def _overlay(document: dict, payload: dict) -> None:
    """Put the form's values into the parsed document, in the file's own shape."""
    address = document.setdefault("address", {})
    collection = document.setdefault("collection", {})
    todoist = document.setdefault("export", {}).setdefault("todoist", {})

    for field, section, key in (
        ("postcode", address, "postcode"),
        ("api_key", collection, "api_key"),
        ("house_number", address, "house_number"),
        ("addition", address, "addition"),
        ("lookahead_days", collection, "lookahead_days"),
        ("due_time", collection, "due_time"),
        ("types", collection, "types"),
        ("todoist_enabled", todoist, "enabled"),
        ("todoist_project", todoist, "project"),
        ("remind_days_before", todoist, "remind_days_before"),
    ):
        if field in payload:
            section[key] = payload[field]

    # from_data() reads the environment for this one, and would then accept an
    # enabled export with an empty file token on a host that has the variable.
    # The token written is _secret_to_write()'s answer, so keep the two the same.
    if "todoist_token" in payload:
        todoist["token"] = payload["todoist_token"]


# --- turning a JobResult into what the page draws ------------------------------------


def _action_payload(result: JobResult, config: Config, paths: Paths, lines: list[dict]) -> dict:
    return {"result": _result_payload(result, config, paths), "log": lines}


def _result_payload(result: JobResult, config: Config, paths: Paths) -> dict:
    return {
        "status": str(result.status),
        "ok": result.ok,
        "summary": result.summary,
        "dry_run": result.dry_run,
        "queried": result.queried,
        "address": result.schedule.address if result.schedule else "",
        "data_version": result.schedule.data_version if result.schedule else "",
        "decision": (
            None
            if result.decision is None
            else {"check_remote": result.decision.check_remote, "reason": result.decision.reason}
        ),
        "delta": None if result.delta is None else _delta_payload(result.delta, config),
        "rows": _rows(result, config, paths),
    }


def _delta_payload(delta: Delta, config: Config) -> dict:
    return {
        "create": [_collection_payload(item, config) for item in delta.create],
        "update": [_collection_payload(change.collection, config) for change in delta.update],
        "delete": [
            {
                "date": task.date.isoformat(),
                "code": task.code,
                "waste_type": WASTE_TYPES.get(task.code, task.code.title()),
                "task_id": task.task_id,
            }
            for task in delta.delete
        ],
    }


def _collection_payload(collection: Collection, config: Config) -> dict:
    due = collection.due_at(config.collection.due_time)
    remind = due - timedelta(days=config.export.todoist.remind_days_before)
    return {
        "date": collection.date.isoformat(),
        "weekday": collection.date.strftime("%A"),
        "code": collection.code,
        "waste_type": collection.waste_type,
        "due_at": due.isoformat(),
        "remind_at": remind.isoformat(),
    }


def _rows(result: JobResult, config: Config, paths: Paths) -> list[dict]:
    """The table on the page: every upcoming collection, and what will happen to it.

    A todo that is going away has no collection behind it any more, so it gets a
    row of its own - it is exactly the thing a person opens this page to see.

    Only a run that wrote nothing has anything to announce. An applied run's
    delta is the receipt, not the plan: the to-dos it created and rewrote are in
    the state file by now, so they are simply synced, and the ones it deleted are
    gone and have no row left to stand in.
    """
    delta = result.delta
    planned = delta if result.dry_run else None
    creating = {item.key for item in planned.create} if planned else set()
    rewriting = {change.collection.key for change in planned.update} if planned else set()
    ids = _known_ids(paths.state, delta)

    rows = []
    for collection in result.collections:
        rows.append(
            {
                **_collection_payload(collection, config),
                "state": _row_state(collection.key, creating, rewriting, ids),
                "task_id": ids.get(collection.key),
            }
        )

    for task in planned.delete if planned else ():
        # Nothing is collected on this date any more, but the row needs the same
        # columns as every other one - and building it the same way is what keeps
        # its due moment in the collection round's timezone rather than the
        # reader's, which a browser would quietly shift by an hour.
        gone = Collection(date=task.date, code=task.code)
        rows.append(
            {
                **_collection_payload(gone, config),
                "remind_at": None,  # a todo on its way out has nothing to remind about
                "state": "remove",
                "task_id": task.task_id,
            }
        )

    rows.sort(key=lambda row: (row["date"], row["code"]))
    return rows


def _row_state(key: str, creating: set[str], rewriting: set[str], ids: dict[str, str]) -> str:
    if key in creating:
        return "add"
    if key in rewriting:
        return "rewrite"
    return "synced" if key in ids else "pending"


def _known_ids(state_path: Path, delta: Delta | None) -> dict[str, str]:
    """The Todoist id per collection, as far as anything here knows one.

    The local record is the only place that has one for a todo this delta does
    not touch, and after an applied run it is the freshly written one. A delta
    that rewrites a todo carries the id too, and agrees.
    """
    ids = {task.key: task.task_id for task in state.load(state_path).tasks}
    for change in delta.update if delta else ():
        ids[change.collection.key] = change.task_id
    return ids


# --- the two things every action needs -----------------------------------------------


@contextlib.contextmanager
def _locked(paths: Paths):
    """Hold the run lock for the block, or raise ``Busy`` rather than queue behind it.

    The same file ``run-job.sh`` flocks, so this also excludes the cron run; the
    threading lock excludes the other requests to this process, which an flock
    taken twice over would not reliably do.
    """
    if not _in_process.acquire(blocking=False):
        raise Busy("another action is still running")
    try:
        try:
            paths.lock.parent.mkdir(parents=True, exist_ok=True)
            handle = paths.lock.open("w")
        except OSError as exc:
            raise ApiError(f"cannot take the run lock at {paths.lock}: {exc}") from exc

        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Busy("the scheduled run is in progress") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _in_process.release()


def wait_for_the_pipeline(timeout: float) -> bool:
    """Block until no action is inside the pipeline, or *timeout* seconds pass.

    What a stopping server waits for. An action holds the lock across the whole
    export - the to-dos and, after them, the state file that records which todo
    is which - so a process that goes away in the middle of one may have written
    to-dos nothing here has a record of. Reconciliation repairs that on the next
    run by asking Todoist, but sitting out the last few seconds of a run that is
    already underway is cheaper than the API call that repairs it.

    Returns whether the pipeline was idle in time.
    """
    if not _in_process.acquire(timeout=timeout):
        return False
    _in_process.release()
    return True


class _Recorder(logging.Handler):
    """Keeps what the job said, so the console on the page can show the same lines."""

    def __init__(self, thread_id: int):
        super().__init__(level=logging.DEBUG)
        self.thread_id = thread_id
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        # The handler is attached to a logger the whole process shares, so a
        # cron-free but still threaded server would otherwise mix two runs.
        if record.thread != self.thread_id or len(self.lines) >= MAX_LOG_LINES:
            return
        self.lines.append(
            {
                "time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                "level": record.levelname,
                "message": record.getMessage(),
            }
        )


@contextlib.contextmanager
def _captured():
    """Collect this thread's log records for the duration of the block.

    A handler alone is not enough: a record the logger's level drops never
    reaches one, so a container configured to keep its journal quiet would show
    an empty console on the page - and the console is the reason those lines are
    written at all. So the application's own logger is lowered to INFO for the
    block if it sat above it. The journal sees those lines too for that moment,
    which is the run the person at the page just asked for.
    """
    recorder = _Recorder(threading.get_ident())
    logger = logging.getLogger(LOG_ROOT)
    was = logger.level
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    logger.addHandler(recorder)
    try:
        yield recorder.lines
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(was)
