"""Publishing of collection events to Todoist.

The actual work - deciding what to add, rewrite or remove - lives in
:mod:`reconciliation`; this module is the seam that hands it a Todoist client
and the local state file to keep.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from . import data_processing, reconciliation, state
from .configuration import Config
from .data_collection import RawSchedule
from .data_processing import Collection
from .reconciliation import Decision, Report, TodoistClient

log = logging.getLogger(__name__)


def export(
    collections: list[Collection],
    config: Config,
    *,
    state_path: Path,
    schedule: RawSchedule | None = None,
    client: TodoistClient | None = None,
    today: date | None = None,
) -> Report:
    """Bring every enabled export target in line with *collections*.

    Pass *client* to supply your own Todoist client; otherwise the configured
    one is built here.
    """
    if not config.export.todoist.enabled:
        log.info("todoist export is disabled in the configuration; nothing was written")
        return Report(Decision(False, "todoist export is disabled in the configuration"))

    return reconciliation.reconcile(
        collections,
        config,
        state_path=state_path,
        client=client if client is not None else todoist_client(config),
        schedule=schedule,
        today=today,
    )


def preview(
    collections: list[Collection],
    config: Config,
    *,
    state_path: Path,
    today: date | None = None,
) -> Decision | None:
    """Say what an export would do, without doing it.

    Touches neither Todoist nor the state file. Returns the decision it would
    act on, or None when there is no enabled target to decide anything about.
    """
    if not config.export.todoist.enabled:
        log.info("todoist export is disabled in the configuration")
        return None

    day = data_processing.day_or_today(today)
    decision = reconciliation.decide(state.load(state_path).upcoming(day), collections, config)
    verb = "would be queried" if decision.check_remote else "would not be touched"
    log.info("todoist %s: %s", verb, decision.reason)
    return decision


def todoist_client(config: Config) -> TodoistClient:
    """The client that talks to the Todoist API - see the Todoist setup feature."""
    raise NotImplementedError("data_export.todoist_client")
