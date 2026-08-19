"""Keeping Todoist in step with the schedule, without querying it on every run.

Two states matter: what the source says will be collected, and what Todoist
already holds. The local file written by :mod:`state` stands in for the second
one, so an ordinary run - the schedule unchanged since the last one - costs no
API call at all. When the two disagree the local record is set aside, Todoist
itself is asked what is really there, and the difference is turned into a delta
that is then executed.

One rule runs through all of it: **the past is never touched**. A collection
that has been and gone is history, and so is its todo, whether or not it was
ever ticked off. Everything from today onwards carrying our label is ours, and
is made to match the schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import data_processing, state
from .configuration import Config
from .data_collection import RawSchedule
from .data_processing import Collection
from .state import ExportState, TaskRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteTask:
    """A todo that already exists in Todoist, as the client found it."""

    task_id: str
    date: date
    code: str

    @property
    def key(self) -> str:
        return f"{self.date.isoformat()}:{self.code}"

    def __str__(self) -> str:
        return f"{self.date.isoformat()} {self.code}"


@runtime_checkable
class TodoistClient(Protocol):
    """What reconciliation needs from Todoist; :mod:`data_export` supplies the real one.

    ``list_tasks`` returns only the to-dos this project owns - the ones carrying
    its label - so everything it hands back may be changed or removed.
    """

    def list_tasks(self) -> list[RemoteTask]: ...

    def create_task(self, collection: Collection) -> str: ...

    def update_task(self, task_id: str, collection: Collection) -> None: ...

    def delete_task(self, task_id: str) -> None: ...


@dataclass(frozen=True)
class Change:
    """An existing todo that has to be rewritten in place."""

    task_id: str
    collection: Collection


@dataclass(frozen=True)
class Delta:
    """The work that turns the current Todoist state into the wanted one."""

    create: tuple[Collection, ...] = ()
    update: tuple[Change, ...] = ()
    delete: tuple[RemoteTask, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.create or self.update or self.delete)

    def __str__(self) -> str:
        return (
            f"{len(self.create)} to add, {len(self.update)} to rewrite, "
            f"{len(self.delete)} to remove"
        )


@dataclass(frozen=True)
class Decision:
    """Whether Todoist has to be asked, and the sentence explaining why."""

    check_remote: bool
    reason: str


@dataclass(frozen=True)
class Report:
    """What one reconciliation did: whether Todoist was asked, and what changed."""

    decision: Decision
    delta: Delta = Delta()
    #: Whether the Todoist API was actually called. Usually this is the decision
    #: coming true, but preview() asks whatever the decision says, so it is
    #: recorded rather than derived.
    queried: bool = False


def signature(config: Config) -> str:
    """The shape of the to-dos themselves; when it changes, every todo is rewritten.

    Deliberately not the whole configuration: ``lookahead_days`` changes which
    to-dos exist, and that already shows up as dates appearing or disappearing.
    """
    todoist = config.export.todoist
    return (
        f"todoist project={todoist.project} "
        f"due={config.collection.due_time.isoformat('minutes')} "
        f"remind={todoist.remind_days_before}"
    )


def decide(known: ExportState, collections: list[Collection], config: Config) -> Decision:
    """Say whether the local record still covers *collections*, or Todoist must be asked.

    *known* is expected to have had the past pruned already - see ``ExportState.upcoming``.
    """
    if not known.known:
        return Decision(True, "there is no local record of what was exported")

    if not known.complete:
        return Decision(True, "the last export stopped halfway, so what Todoist holds is unknown")

    wanted_signature = signature(config)
    if known.signature != wanted_signature:
        return Decision(
            True, f"the todo format changed ({known.signature or 'unknown'} -> {wanted_signature})"
        )

    wanted = {collection.key for collection in collections}
    if wanted != known.keys:
        return Decision(
            True,
            f"the schedule moved: {len(wanted - known.keys)} new date(s), "
            f"{len(known.keys - wanted)} gone",
        )

    return Decision(False, f"{len(wanted)} todo(s) exported earlier are still correct")


def plan(
    collections: list[Collection],
    remote: list[RemoteTask],
    *,
    today: date,
    rewrite: bool = False,
) -> Delta:
    """Work out what has to change in Todoist to match *collections*.

    Only dates from *today* onwards take part: a todo for a collection that has
    already happened is left exactly as it is. Pass *rewrite* to also refresh
    the to-dos that are in the right place but were written in an older format.
    """
    wanted = {collection.key: collection for collection in collections if collection.date >= today}

    current: dict[str, RemoteTask] = {}
    duplicates: list[RemoteTask] = []
    for task in remote:
        if task.date < today:
            continue
        if task.key in current:
            # Two to-dos for one collection: an earlier run must have tripped
            # between creating and recording. Keep the first, remove the rest.
            duplicates.append(task)
        else:
            current[task.key] = task

    obsolete = sorted(current.keys() - wanted.keys())
    shared = sorted(wanted.keys() & current.keys()) if rewrite else []
    return Delta(
        create=tuple(wanted[key] for key in sorted(wanted.keys() - current.keys())),
        update=tuple(Change(current[key].task_id, wanted[key]) for key in shared),
        delete=tuple([current[key] for key in obsolete] + duplicates),
    )


def reconcile(
    collections: list[Collection],
    config: Config,
    *,
    state_path: Path,
    client: TodoistClient,
    schedule: RawSchedule | None = None,
    today: date | None = None,
) -> Report:
    """Bring Todoist in line with *collections* and record what is now there.

    Returns both halves of the answer: whether Todoist had to be asked at all,
    and the delta that was executed - empty when nothing had to change, and
    empty as well when the local record already matched, in which case Todoist
    was never called.
    """
    day = data_processing.day_or_today(today)
    known = state.load(state_path).upcoming(day)
    _log_schedule_version(known, schedule)

    decision = decide(known, collections, config)
    if not decision.check_remote:
        log.info("todoist not queried: %s", decision.reason)
        _record(state_path, known.tasks, config, schedule)
        return Report(decision)

    log.info("querying todoist: %s", decision.reason)
    remote = client.list_tasks()
    delta = plan(collections, remote, today=day, rewrite=_rewrite_needed(known, config))

    # What Todoist holds right now, before a single call: _apply() keeps it true
    # as it goes, so whatever happens the record describes what is really there
    # rather than what the delta hoped for. Where a collection has twins, the
    # first one is the keeper, exactly as plan() decided.
    records: dict[str, TaskRecord] = {}
    for task in remote:
        if task.date >= day:
            records.setdefault(task.key, TaskRecord(task.date, task.code, task.task_id))

    if delta.is_empty:
        log.info("todoist already matches the schedule (%d todo(s))", len(records))
    else:
        log.info("reconciling todoist: %s", delta)

    try:
        _apply(delta, client, records)
    except Exception:
        # The export is half done: every todo in the record is real, but the
        # calls we never reached are unaccounted for - an obsolete todo that is
        # still there, or one still written in the old format. Neither of those
        # shows up as a missing key, so the record is saved as unfinished, which
        # makes the next run ask Todoist itself instead of believing this file.
        try:
            _record(state_path, records.values(), config, schedule, complete=False)
        except state.StateError as exc:
            log.error("could not record the partial export: %s", exc)
        raise

    _record(state_path, records.values(), config, schedule)
    return Report(decision, delta, queried=True)


def preview(
    collections: list[Collection],
    config: Config,
    *,
    state_path: Path,
    client: TodoistClient,
    today: date | None = None,
) -> Report:
    """Say what ``reconcile()`` would do, and change nothing at all.

    Todoist is read - that is the whole point of asking - but no todo is
    written and the state file is left as it was, so the answer is a question
    the caller may still say no to. The web interface's "check Todoist" button
    is what this is for.

    Unlike ``reconcile()`` it always asks, and the decision comes back alongside
    the delta: the person pressing the button wants to know what is really there,
    including when the local record says nothing has moved.
    """
    day = data_processing.day_or_today(today)
    known = state.load(state_path).upcoming(day)

    decision = decide(known, collections, config)
    log.info("querying todoist without writing: %s", decision.reason)
    delta = plan(
        collections, client.list_tasks(), today=day, rewrite=_rewrite_needed(known, config)
    )
    log.info("todoist would need %s", delta)
    return Report(decision, delta, queried=True)


def _rewrite_needed(known: ExportState, config: Config) -> bool:
    """Whether the to-dos that stay where they are have to be rewritten as well.

    Either the format changed since they were written, or the export that wrote
    them stopped halfway - and then the recorded format is only the one we were
    aiming for, so which to-dos actually carry it is anyone's guess.
    """
    return not known.complete or known.signature != signature(config)


def _apply(delta: Delta, client: TodoistClient, records: dict[str, TaskRecord]) -> None:
    """Execute *delta*, keeping *records* correct after every single call."""
    for task in delta.delete:
        client.delete_task(task.task_id)
        # A duplicate's twin is not the todo this collection is recorded under,
        # so removing it leaves the record it shares a key with standing.
        if records.get(task.key) == TaskRecord(task.date, task.code, task.task_id):
            del records[task.key]
        log.info("removed %s", task)
    for change in delta.update:
        client.update_task(change.task_id, change.collection)
        log.info("rewrote %s", change.collection)
    for collection in delta.create:
        task_id = client.create_task(collection)
        records[collection.key] = TaskRecord(collection.date, collection.code, task_id)
        log.info("added %s", collection)


def _record(
    path: Path, tasks, config: Config, schedule: RawSchedule | None, *, complete: bool = True
) -> None:
    state.save(
        path,
        ExportState(
            known=True,
            complete=complete,
            signature=signature(config),
            address=schedule.address if schedule else "",
            data_version=schedule.data_version if schedule else "",
            updated_at=datetime.now(UTC),
            tasks=tuple(sorted(tasks, key=lambda task: task.key)),
        ),
    )


def _log_schedule_version(known: ExportState, schedule: RawSchedule | None) -> None:
    """Explain a surprise before it happens: the municipality edited the schedule."""
    if schedule is None or not known.data_version:
        return
    if schedule.data_version != known.data_version:
        log.info(
            "the municipality changed the schedule (version %s -> %s)",
            known.data_version,
            schedule.data_version,
        )
