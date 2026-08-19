"""The job itself: one run, from the source to Todoist and back to the record.

Every other module does one thing; this one is the order they happen in. Ask
mijnafvalwijzer.nl what will be collected, keep the dates we want a todo for,
compare them against the local record of the last export, and - only when the
two no longer agree - ask Todoist what it really holds, work out the difference
and execute it. Then write down what is now there.

The run is described by what it returns, not by what it raises. Everything that
goes wrong in the ordinary course of a scheduled job - an unreachable source, an
address the source does not know, a Todoist that refuses, a state file that
cannot be written - comes back as a :class:`Status` on a :class:`JobResult`. The CLI turns that into an
exit code; anything else that wants to run the job (a web page, a test) gets the
same object and decides for itself what to do with it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from . import data_collection, data_export, data_processing, state
from .configuration import Config
from .data_collection import RawSchedule
from .data_processing import Collection
from .reconciliation import Decision, Delta, TodoistClient
from .todoist import TodoistError

log = logging.getLogger(__name__)


class Status(StrEnum):
    """How a run ended. Each one has an exit code; see ``EXIT_CODES`` in ``__init__``."""

    OK = "ok"
    #: The schedule could not be fetched, or the source does not know the address.
    COLLECTION_ERROR = "collection-error"
    #: Todoist could not be reached, or refused what was asked of it. Whatever
    #: the run got through before that is written; the rest is not.
    TODOIST_ERROR = "todoist-error"
    #: The to-dos may exist while the local record of them does not.
    EXPORT_ERROR = "export-error"


@dataclass(frozen=True)
class JobResult:
    """What one run found and did - the whole outcome, in one object."""

    status: Status
    #: One line fit for a cron log; on a failure it is the reason it failed.
    summary: str
    #: The upcoming collections, after filtering and the lookahead window.
    collections: tuple[Collection, ...] = ()
    schedule: RawSchedule | None = None
    #: Whether Todoist had to be asked at all, and why. None when the target is off.
    decision: Decision | None = None
    #: Whether the Todoist API was actually called during this run.
    queried: bool = False
    #: What was changed in Todoist - empty when nothing had to be. None when no
    #: export was attempted at all: a dry run, or a run that failed before it.
    delta: Delta | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.status is Status.OK


def run(
    config: Config,
    *,
    state_path: Path,
    dry_run: bool = False,
    client: TodoistClient | None = None,
    today: date | None = None,
) -> JobResult:
    """Do one run and report what happened.

    Pass *client* to supply your own Todoist client, and *today* to fix the day
    the past is measured from; both default to the real thing.

    A dry run collects and processes, says what it would export and whether it
    would have to ask Todoist, and writes nothing at all - neither a todo nor
    the state file.
    """
    day = data_processing.day_or_today(today)

    gathered = _gather(config, day=day, dry_run=dry_run)
    if isinstance(gathered, JobResult):
        return gathered
    schedule, collections = gathered
    found = tuple(collections)

    if dry_run:
        for collection in collections:
            log.info("would export %s", collection)
        decision = data_export.preview(collections, config, state_path=state_path, today=day)
        return _done(
            JobResult(
                Status.OK,
                _summary(found, config, dry_run=True),
                collections=found,
                schedule=schedule,
                decision=decision,
                dry_run=True,
            )
        )

    try:
        report = data_export.export(
            collections,
            config,
            state_path=state_path,
            schedule=schedule,
            client=client,
            today=day,
        )
    except TodoistError as exc:
        # Their end, not ours: an unreachable API, a refused token, a project
        # that is not there. reconcile() has recorded whatever did get through.
        return JobResult(Status.TODOIST_ERROR, f"todoist: {exc}", found, schedule)
    except state.StateError as exc:
        # The to-dos may well have been written; only the record of them was not.
        return JobResult(Status.EXPORT_ERROR, f"export: {exc}", found, schedule)

    return _done(
        JobResult(
            Status.OK,
            _summary(found, config, delta=report.delta),
            collections=found,
            schedule=schedule,
            decision=report.decision,
            queried=report.queried,
            delta=report.delta,
        )
    )


def check(
    config: Config,
    *,
    state_path: Path,
    client: TodoistClient | None = None,
    today: date | None = None,
) -> JobResult:
    """Collect, ask Todoist what it really holds, and report the difference.

    Nothing is written: not a todo, not the state file. This is the honest
    answer to "what would a run do right now", and unlike a dry run it costs an
    API call, because it is the API it goes and looks at. The web interface's
    "check Todoist" button is what it is for; a scheduled run uses ``run()``.
    """
    day = data_processing.day_or_today(today)

    gathered = _gather(config, day=day, dry_run=True)
    if isinstance(gathered, JobResult):
        return gathered
    schedule, collections = gathered
    found = tuple(collections)

    if not config.export.todoist.enabled:
        return _done(
            JobResult(
                Status.OK,
                f"{len(found)} collection(s) upcoming; todoist export is disabled",
                collections=found,
                schedule=schedule,
                dry_run=True,
            )
        )

    try:
        report = data_export.check(
            collections, config, state_path=state_path, client=client, today=day
        )
    except TodoistError as exc:
        return JobResult(Status.TODOIST_ERROR, f"todoist: {exc}", found, schedule, dry_run=True)

    return _done(
        JobResult(
            Status.OK,
            _checked(found, report.delta),
            collections=found,
            schedule=schedule,
            decision=report.decision,
            queried=report.queried,
            delta=report.delta,
            dry_run=True,
        )
    )


def _checked(collections: tuple[Collection, ...], delta: Delta) -> str:
    """What check() found, said the way the summary of a run says it."""
    found = f"{len(collections)} collection(s) upcoming"
    if delta.is_empty:
        return f"{found}; todoist already matches, nothing to change"
    return f"{found}; todoist would need {delta}"


def _gather(
    config: Config, *, day: date, dry_run: bool
) -> tuple[RawSchedule, list[Collection]] | JobResult:
    """The half every action shares: ask the source, keep what we want a todo for.

    Returns the failed ``JobResult`` instead when the source could not be
    reached - there is nothing for the caller to do with it but hand it back.
    """
    try:
        schedule = data_collection.collect(config)
    except data_collection.CollectionError as exc:
        return JobResult(Status.COLLECTION_ERROR, f"collection: {exc}", dry_run=dry_run)

    return schedule, data_processing.process(schedule, config, today=day)


def _summary(
    collections: tuple[Collection, ...],
    config: Config,
    *,
    delta: Delta | None = None,
    dry_run: bool = False,
) -> str:
    """The single line the log is read through: what was found, what changed."""
    found = f"{len(collections)} collection(s) upcoming"
    if dry_run:
        return f"{found}; nothing written (dry run)"
    if not config.export.todoist.enabled:
        return f"{found}; todoist export is disabled"
    if delta is None or delta.is_empty:
        return f"{found}; nothing to change in todoist"
    return f"{found}; {delta}"


def _done(result: JobResult) -> JobResult:
    """A run that reached the end says so itself; a failed one is the caller's to report."""
    log.info("%s", result.summary)
    return result
