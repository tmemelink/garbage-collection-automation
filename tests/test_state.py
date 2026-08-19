"""The local record of the last export: written whole, read defensively."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from garbage_collection_automation import state
from garbage_collection_automation.state import ExportState, StateError, TaskRecord


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state.json"


def make_state(*tasks: TaskRecord, **overrides) -> ExportState:
    fields = {
        "known": True,
        "signature": "todoist project=Home due=07:00 remind=1",
        "address": "Voorbeeldstraat 21, Voorbeeldstad",
        "data_version": "1786701549",
        "updated_at": datetime(2026, 8, 19, 4, 30, tzinfo=UTC),
        "tasks": tasks,
    }
    return ExportState(**{**fields, **overrides})


def test_a_missing_file_is_not_an_error(path, caplog):
    """The first run ever has nothing to read; that is not a problem to report."""
    loaded = state.load(path)

    assert loaded.known is False
    assert loaded.tasks == ()
    assert caplog.records == []


def test_what_was_saved_is_what_comes_back(path):
    written = make_state(TaskRecord(date(2026, 8, 20), "restafval", "6X4h2Q"))

    state.save(path, written)

    assert state.load(path) == written


def test_an_unfinished_export_says_so_when_it_is_read_back(path):
    """The whole point of the mark is that it outlives the run that made it."""
    state.save(path, make_state(TaskRecord(date(2026, 8, 20), "gft", "1"), complete=False))

    assert state.load(path).complete is False


def test_a_file_written_before_the_mark_existed_counts_as_finished(path):
    """An unfinished run could not have written that file, so it was a finished one."""
    path.write_text(json.dumps({"version": 1, "tasks": []}))

    assert state.load(path).complete is True


def test_a_damaged_file_is_ignored_rather_than_fatal(path, caplog):
    """A truncated write must cost one Todoist query, not the whole run."""
    path.write_text('{"version": 1, "tasks": [{"date": "2026-')

    loaded = state.load(path)

    assert loaded.known is False
    assert "ignoring the export state" in caplog.text


def test_a_file_from_another_version_is_ignored(path, caplog):
    path.write_text(json.dumps({"version": 99, "tasks": []}))

    assert state.load(path).known is False
    assert "another version" in caplog.text


def test_a_task_the_file_cannot_describe_is_ignored(path, caplog):
    path.write_text(json.dumps({"version": 1, "tasks": [{"date": "someday", "code": "gft"}]}))

    assert state.load(path).known is False
    assert "ignoring the export state" in caplog.text


def test_the_past_is_pruned(path):
    yesterday = TaskRecord(date(2026, 8, 18), "gft", "old")
    tomorrow = TaskRecord(date(2026, 8, 20), "restafval", "new")

    upcoming = make_state(yesterday, tomorrow).upcoming(date(2026, 8, 19))

    assert upcoming.tasks == (tomorrow,)
    assert upcoming.known is True


def test_today_itself_is_not_the_past(path):
    """The bin still has to go out this morning; its todo stays ours."""
    todays = TaskRecord(date(2026, 8, 19), "gft", "today")

    assert make_state(todays).upcoming(date(2026, 8, 19)).tasks == (todays,)


def test_keys_identify_a_collection(path):
    recorded = make_state(TaskRecord(date(2026, 8, 20), "restafval", "6X4h2Q"))

    assert recorded.keys == {"2026-08-20:restafval"}


def test_saving_creates_the_directory(tmp_path):
    path = tmp_path / "var" / "lib" / "state.json"

    state.save(path, make_state())

    assert json.loads(path.read_text())["version"] == state.STATE_VERSION


def test_saving_leaves_no_scratch_file_behind(path):
    state.save(path, make_state(TaskRecord(date(2026, 8, 20), "gft", "1")))

    assert [entry.name for entry in path.parent.iterdir()] == ["state.json"]


def test_a_write_that_cannot_happen_is_reported(tmp_path):
    """The to-dos were created; failing to record that is worth an exit code."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")

    with pytest.raises(StateError, match="cannot write"):
        state.save(blocked / "state.json", make_state())
