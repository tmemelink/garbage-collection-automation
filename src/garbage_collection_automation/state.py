"""The local record of the last export, so a run need not ask Todoist every time.

Asking Todoist every run what we ourselves put there is wasteful: the
schedule barely moves. So each run writes down which todo it created for which
collection, and the next run compares the fresh schedule against that file.
Only when the two disagree - or when the file is missing, unreadable, written
for to-dos of a different shape, or left behind by a run that stopped halfway -
does anyone call the API.

The file holds no authentication credentials, but it does contain the resolved
street address and Todoist task ids. It is private operational data, kept in the
service user's state directory rather than in the repository. It is disposable:
delete it and the next run rebuilds it from Todoist itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Bumped when the file's shape changes; an older or newer file is simply discarded.
STATE_VERSION = 1


class StateError(Exception):
    """The state file could not be written - the export happened but is unrecorded."""


@dataclass(frozen=True)
class TaskRecord:
    """One todo we created: the collection it stands for, and its Todoist id."""

    date: date
    code: str
    task_id: str

    @property
    def key(self) -> str:
        return f"{self.date.isoformat()}:{self.code}"


@dataclass(frozen=True)
class ExportState:
    """What the previous run left behind."""

    #: False when there is no usable file: we cannot then claim to know Todoist.
    known: bool = False
    #: False when the export that wrote this stopped halfway: the to-dos listed
    #: here exist, but Todoist may hold others we never got to. Only Todoist
    #: itself can settle that, so the next run has to ask - see decide().
    complete: bool = True
    #: The shape of the to-dos that were written; see reconciliation.signature().
    signature: str = ""
    #: Provenance, for the person reading this file - neither drives a decision.
    address: str = ""
    data_version: str = ""
    updated_at: datetime | None = None
    tasks: tuple[TaskRecord, ...] = ()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(task.key for task in self.tasks)

    def upcoming(self, today: date) -> ExportState:
        """The same state with the past dropped; we never touch a collection that was."""
        return replace(self, tasks=tuple(task for task in self.tasks if task.date >= today))


def load(path: Path) -> ExportState:
    """Read *path*, or return an unknown state when it cannot be trusted.

    Never raises: a missing or damaged file is not a failed run, it only means
    the next step has to ask Todoist instead of taking our word for it.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.debug("no export state at %s yet", path)
        return ExportState()
    except (OSError, ValueError) as exc:
        log.warning("ignoring the export state at %s: %s", path, exc)
        return ExportState()

    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        log.warning("ignoring the export state at %s: written by another version", path)
        return ExportState()

    try:
        tasks = tuple(_task(item) for item in payload["tasks"])
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ignoring the export state at %s: %s", path, exc)
        return ExportState()

    return ExportState(
        known=True,
        # A file written before this flag existed was written by a run that
        # finished, since an unfinished one had nothing to say about itself.
        complete=bool(payload.get("complete", True)),
        signature=str(payload.get("signature", "")),
        address=str(payload.get("address", "")),
        data_version=str(payload.get("data_version", "")),
        updated_at=_timestamp(payload.get("updated_at")),
        tasks=tasks,
    )


def save(path: Path, state: ExportState) -> None:
    """Write *state* to *path*, atomically - a half-written file would be worse than none."""
    payload = {
        "version": STATE_VERSION,
        "complete": state.complete,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "address": state.address,
        "data_version": state.data_version,
        "signature": state.signature,
        "tasks": [
            {"date": task.date.isoformat(), "code": task.code, "task_id": task.task_id}
            for task in state.tasks
        ],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise StateError(f"cannot write {path}: {exc}") from exc
    log.debug("recorded %d exported todo(s) in %s", len(state.tasks), path)


def _task(item: object) -> TaskRecord:
    if not isinstance(item, dict):
        raise TypeError(f"a task entry is {type(item).__name__}, not a table")
    return TaskRecord(
        date=date.fromisoformat(str(item["date"])),
        code=str(item["code"]),
        task_id=str(item["task_id"]),
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
