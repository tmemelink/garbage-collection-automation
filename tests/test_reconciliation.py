"""Reconciliation: ask Todoist only when the local record no longer fits, then fix it."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, time

import pytest

from garbage_collection_automation import reconciliation, state
from garbage_collection_automation.configuration import TodoistExportConfig
from garbage_collection_automation.data_collection import RawSchedule
from garbage_collection_automation.data_processing import Collection
from garbage_collection_automation.reconciliation import Change, Delta, RemoteTask
from garbage_collection_automation.state import ExportState, TaskRecord

from .conftest import make_config

TODAY = date(2026, 8, 19)

RESTAFVAL = Collection(date(2026, 8, 20), "restafval")
GFT = Collection(date(2026, 8, 27), "gft")
PAPIER = Collection(date(2026, 9, 3), "papier")


class FakeTodoist:
    """A Todoist that keeps its books in memory; it implements TodoistClient.

    The books are real - a created todo shows up in the next ``list_tasks()``
    and a removed one is gone - so a second run can be handed what the first
    one actually left behind. Pass one of the ``refuse`` arguments to make a
    single call fail, which is how a half-finished export is staged.
    """

    def __init__(
        self,
        *tasks: RemoteTask,
        refuse: str | None = None,
        refuse_update: str | None = None,
        refuse_delete: str | None = None,
    ):
        self.tasks = list(tasks)
        self.refuse = refuse
        self.refuse_update = refuse_update
        self.refuse_delete = refuse_delete
        self.listed = 0
        self.created: list[Collection] = []
        self.updated: list[tuple[str, Collection]] = []
        self.deleted: list[str] = []
        self._ids = itertools.count(1)

    def list_tasks(self) -> list[RemoteTask]:
        self.listed += 1
        return list(self.tasks)

    def create_task(self, collection: Collection) -> str:
        if self.refuse == collection.key:
            raise RuntimeError("todoist refused")
        self.created.append(collection)
        task_id = f"id-{next(self._ids)}"
        self.tasks.append(RemoteTask(task_id, collection.date, collection.code))
        return task_id

    def update_task(self, task_id: str, collection: Collection) -> None:
        if self.refuse_update == task_id:
            raise RuntimeError("todoist refused")
        self.updated.append((task_id, collection))

    def delete_task(self, task_id: str) -> None:
        if self.refuse_delete == task_id:
            raise RuntimeError("todoist refused")
        self.deleted.append(task_id)
        self.tasks = [task for task in self.tasks if task.task_id != task_id]


class UnreachableTodoist:
    """Any call at all is the failure this whole feature exists to avoid."""

    def list_tasks(self):
        raise AssertionError("todoist was queried when the local record already fitted")

    create_task = update_task = delete_task = list_tasks


def config(**todoist):
    return make_config(todoist=TodoistExportConfig(enabled=True, token="secret", **todoist))


def remote(collection: Collection, task_id: str) -> RemoteTask:
    return RemoteTask(task_id, collection.date, collection.code)


def record(collection: Collection, task_id: str) -> TaskRecord:
    return TaskRecord(collection.date, collection.code, task_id)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


def write_state(path, *tasks: TaskRecord, cfg=None, **overrides) -> None:
    state.save(
        path,
        ExportState(
            known=True,
            signature=reconciliation.signature(cfg or config()),
            updated_at=datetime(2026, 8, 18, 4, 30, tzinfo=UTC),
            tasks=tasks,
            **overrides,
        ),
    )


# --- the signature: what the to-dos themselves look like -----------------------------


def test_the_signature_covers_what_shapes_a_todo():
    assert reconciliation.signature(config()) == reconciliation.signature(config())
    assert reconciliation.signature(config()) != reconciliation.signature(config(project="Huis"))
    assert reconciliation.signature(config()) != reconciliation.signature(
        config(section="Terugkerend")
    )
    assert reconciliation.signature(config()) != reconciliation.signature(
        config(remind_days_before=2)
    )
    assert reconciliation.signature(config()) != reconciliation.signature(
        make_config(due_time=time(18, 0), todoist=TodoistExportConfig(enabled=True, token="secret"))
    )


def test_a_wider_window_is_not_a_format_change():
    """More to-dos, but the same to-dos: that shows up as new dates instead."""
    wide = make_config(lookahead_days=90, todoist=TodoistExportConfig(enabled=True))

    assert reconciliation.signature(wide) == reconciliation.signature(config())


# --- deciding whether Todoist has to be asked at all ---------------------------------


def test_without_a_local_record_todoist_must_be_asked():
    decision = reconciliation.decide(ExportState(), [RESTAFVAL], config())

    assert decision.check_remote is True
    assert "no local record" in decision.reason


def test_an_unchanged_schedule_asks_nothing():
    known = ExportState(
        known=True,
        signature=reconciliation.signature(config()),
        tasks=(record(RESTAFVAL, "a"), record(GFT, "b")),
    )

    decision = reconciliation.decide(known, [RESTAFVAL, GFT], config())

    assert decision.check_remote is False
    assert "2 todo(s)" in decision.reason


def test_a_moved_schedule_is_worth_a_query():
    known = ExportState(
        known=True, signature=reconciliation.signature(config()), tasks=(record(RESTAFVAL, "a"),)
    )

    decision = reconciliation.decide(known, [RESTAFVAL, GFT], config())

    assert decision.check_remote is True
    assert "1 new date(s), 0 gone" in decision.reason


def test_an_export_that_stopped_halfway_is_worth_a_query():
    """Its keys can look perfect while a todo we never reached is still out there."""
    known = ExportState(
        known=True,
        complete=False,
        signature=reconciliation.signature(config()),
        tasks=(record(RESTAFVAL, "a"),),
    )

    decision = reconciliation.decide(known, [RESTAFVAL], config())

    assert decision.check_remote is True
    assert "stopped halfway" in decision.reason


def test_a_changed_todo_format_is_worth_a_query():
    known = ExportState(
        known=True,
        signature="todoist project=Elders due=07:00 remind=1",
        tasks=(record(RESTAFVAL, "a"),),
    )

    decision = reconciliation.decide(known, [RESTAFVAL], config())

    assert decision.check_remote is True
    assert "format changed" in decision.reason


# --- planning the delta ---------------------------------------------------------------


def test_an_empty_todoist_needs_every_collection():
    delta = reconciliation.plan([RESTAFVAL, GFT], [], today=TODAY)

    assert delta.create == (RESTAFVAL, GFT)
    assert delta.delete == ()
    assert delta.update == ()


def test_what_already_matches_is_left_alone():
    delta = reconciliation.plan([RESTAFVAL], [remote(RESTAFVAL, "a")], today=TODAY)

    assert delta.is_empty


def test_a_date_that_is_no_longer_collected_is_removed():
    delta = reconciliation.plan([GFT], [remote(RESTAFVAL, "a"), remote(GFT, "b")], today=TODAY)

    assert delta.delete == (remote(RESTAFVAL, "a"),)
    assert delta.create == ()


def test_the_past_is_never_touched():
    """Yesterday's todo is history - ticked off or not, it is not ours to tidy."""
    gone_by = RemoteTask("old", date(2026, 8, 1), "gft")

    delta = reconciliation.plan([RESTAFVAL], [gone_by], today=TODAY)

    assert delta.delete == ()
    assert delta.create == (RESTAFVAL,)


def test_a_collection_in_the_past_is_not_re_created():
    delta = reconciliation.plan([Collection(date(2026, 8, 1), "gft")], [], today=TODAY)

    assert delta.is_empty


def test_a_second_todo_for_one_collection_is_cleaned_up():
    """An earlier run that died between creating and recording leaves a twin."""
    delta = reconciliation.plan(
        [RESTAFVAL], [remote(RESTAFVAL, "first"), remote(RESTAFVAL, "twin")], today=TODAY
    )

    assert delta.delete == (remote(RESTAFVAL, "twin"),)
    assert delta.create == ()


def test_a_new_format_rewrites_the_todos_that_stay():
    delta = reconciliation.plan(
        [RESTAFVAL, GFT], [remote(RESTAFVAL, "a")], today=TODAY, rewrite=True
    )

    assert delta.update == (Change("a", RESTAFVAL),)
    assert delta.create == (GFT,)


def test_a_delta_says_what_it_will_do():
    delta = Delta(create=(GFT,), delete=(remote(RESTAFVAL, "a"),))

    assert str(delta) == "1 to add, 0 to rewrite, 1 to remove"


# --- the whole reconciliation ---------------------------------------------------------


def test_a_matching_local_record_keeps_todoist_out_of_it(state_path):
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))

    report = reconciliation.reconcile(
        [RESTAFVAL, GFT],
        config(),
        state_path=state_path,
        client=UnreachableTodoist(),
        today=TODAY,
    )

    assert report.queried is False
    assert report.delta.is_empty
    assert state.load(state_path).keys == {RESTAFVAL.key, GFT.key}


def test_a_first_run_creates_everything_and_writes_it_down(state_path):
    client = FakeTodoist()

    report = reconciliation.reconcile(
        [RESTAFVAL, GFT], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.created == [RESTAFVAL, GFT]
    assert report.queried is True
    assert report.delta.create == (RESTAFVAL, GFT)
    recorded = state.load(state_path)
    assert recorded.known is True
    assert {task.task_id for task in recorded.tasks} == {"id-1", "id-2"}


def test_a_moved_date_costs_one_query_and_the_two_calls_it_implies(state_path):
    write_state(state_path, record(RESTAFVAL, "a"))
    client = FakeTodoist(remote(RESTAFVAL, "a"))

    report = reconciliation.reconcile(
        [GFT], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.listed == 1
    assert client.deleted == ["a"]
    assert client.created == [GFT]
    assert report.delta.update == ()
    assert state.load(state_path).keys == {GFT.key}


def test_todoist_is_believed_over_the_local_record(state_path):
    """Someone deleted the todo by hand; the query is what tells us so."""
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))
    client = FakeTodoist(remote(RESTAFVAL, "a"))

    reconciliation.reconcile(
        [RESTAFVAL, GFT, PAPIER], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.created == [GFT, PAPIER]
    assert client.deleted == []
    assert state.load(state_path).keys == {RESTAFVAL.key, GFT.key, PAPIER.key}


def test_a_new_due_time_rewrites_the_existing_todos(state_path):
    write_state(state_path, record(RESTAFVAL, "a"))
    client = FakeTodoist(remote(RESTAFVAL, "a"))
    later = make_config(
        due_time=time(18, 0), todoist=TodoistExportConfig(enabled=True, token="secret")
    )

    reconciliation.reconcile([RESTAFVAL], later, state_path=state_path, client=client, today=TODAY)

    assert client.updated == [("a", RESTAFVAL)]
    assert state.load(state_path).signature == reconciliation.signature(later)


def test_the_state_file_is_not_trusted_after_a_format_change(state_path):
    """We cannot know how the old to-dos were written, so all of them are refreshed."""
    client = FakeTodoist(remote(RESTAFVAL, "a"))

    reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.updated == [("a", RESTAFVAL)]


def test_a_half_finished_export_is_still_recorded(state_path):
    """What did get through is worth writing down - but not worth trusting."""
    client = FakeTodoist(refuse=PAPIER.key)

    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL, PAPIER], config(), state_path=state_path, client=client, today=TODAY
        )

    recorded = state.load(state_path)
    assert recorded.keys == {RESTAFVAL.key}
    assert recorded.complete is False


def test_the_run_after_a_half_finished_export_finishes_it(state_path):
    """It asks Todoist, so the todo that did get through is not created twice."""
    failed = FakeTodoist(refuse=PAPIER.key)
    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL, PAPIER], config(), state_path=state_path, client=failed, today=TODAY
        )

    client = FakeTodoist(*failed.tasks)
    report = reconciliation.reconcile(
        [RESTAFVAL, PAPIER], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.listed == 1
    assert client.created == [PAPIER]
    assert report.queried is True
    recorded = state.load(state_path)
    assert recorded.keys == {RESTAFVAL.key, PAPIER.key}
    assert recorded.complete is True


def test_a_todo_that_could_not_be_removed_is_not_written_off(state_path):
    """The old record marked it deleted before Todoist agreed, and then forgot it."""
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))
    failed = FakeTodoist(remote(RESTAFVAL, "a"), remote(GFT, "b"), refuse_delete="b")

    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL], config(), state_path=state_path, client=failed, today=TODAY
        )

    # The todo is still in Todoist, so it is still in the record.
    recorded = state.load(state_path)
    assert recorded.keys == {RESTAFVAL.key, GFT.key}
    assert recorded.complete is False

    client = FakeTodoist(*failed.tasks)
    reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.deleted == ["b"]
    assert state.load(state_path).keys == {RESTAFVAL.key}


def test_a_duplicate_that_could_not_be_removed_is_still_chased(state_path):
    """Nothing in the keys can show a twin, so only the unfinished mark saves it."""
    old_format = config(project="Elders")
    write_state(state_path, record(RESTAFVAL, "a"), cfg=old_format)
    failed = FakeTodoist(remote(RESTAFVAL, "a"), remote(RESTAFVAL, "twin"), refuse_delete="twin")

    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL], config(), state_path=state_path, client=failed, today=TODAY
        )

    recorded = state.load(state_path)
    assert recorded.keys == {RESTAFVAL.key}  # indistinguishable from a finished export
    assert recorded.complete is False

    client = FakeTodoist(*failed.tasks)
    reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.deleted == ["twin"]
    assert client.updated == [("a", RESTAFVAL)]
    assert state.load(state_path).tasks == (record(RESTAFVAL, "a"),)


def test_a_rewrite_that_failed_does_not_pass_for_the_new_format(state_path):
    """The signature is what the run was aiming for, not proof that it landed."""
    old_format = config(project="Elders")
    write_state(state_path, record(RESTAFVAL, "a"), cfg=old_format)
    failed = FakeTodoist(remote(RESTAFVAL, "a"), refuse_update="a")

    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL], config(), state_path=state_path, client=failed, today=TODAY
        )

    assert state.load(state_path).complete is False

    client = FakeTodoist(*failed.tasks)
    report = reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.updated == [("a", RESTAFVAL)]
    assert report.delta.update == (Change("a", RESTAFVAL),)
    assert state.load(state_path).complete is True


def test_removing_a_duplicate_keeps_the_todo_it_shares_a_date_with(state_path):
    """Both carry one key; the record has to keep the one that survives."""
    client = FakeTodoist(remote(RESTAFVAL, "keeper"), remote(RESTAFVAL, "twin"))

    reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.deleted == ["twin"]
    recorded = state.load(state_path)
    assert recorded.tasks == (record(RESTAFVAL, "keeper"),)
    assert recorded.complete is True


def test_a_partial_export_that_cannot_even_be_recorded_still_raises(
    state_path, caplog, monkeypatch
):
    """Losing the record is worth a line in the log, but the export failure is the story."""

    def unwritable(path, export_state):
        raise state.StateError(f"cannot write {path}")

    monkeypatch.setattr(state, "save", unwritable)
    client = FakeTodoist(refuse=PAPIER.key)

    with pytest.raises(RuntimeError):
        reconciliation.reconcile(
            [RESTAFVAL, PAPIER], config(), state_path=state_path, client=client, today=TODAY
        )

    assert "could not record the partial export" in caplog.text


def test_the_schedule_version_is_kept_and_a_change_is_explained(state_path, caplog):
    write_state(state_path, record(RESTAFVAL, "a"), data_version="111")
    schedule = RawSchedule(
        url="https://example.invalid/",
        fetched_at=datetime(2026, 8, 19, 4, 30, tzinfo=UTC),
        address="Voorbeeldstraat 21, Voorbeeldstad",
        data_version="222",
        entries=(),
    )

    with caplog.at_level("INFO"):
        reconciliation.reconcile(
            [RESTAFVAL],
            config(),
            state_path=state_path,
            client=UnreachableTodoist(),
            schedule=schedule,
            today=TODAY,
        )

    assert "changed the schedule (version 111 -> 222)" in caplog.text
    recorded = state.load(state_path)
    assert recorded.data_version == "222"
    assert recorded.address == "Voorbeeldstraat 21, Voorbeeldstad"


def test_a_collection_that_has_been_and_gone_leaves_the_record(state_path):
    """Yesterday's entry is pruned quietly; nothing is asked of Todoist for it."""
    write_state(
        state_path, record(Collection(date(2026, 8, 1), "gft"), "old"), record(RESTAFVAL, "a")
    )

    reconciliation.reconcile(
        [RESTAFVAL], config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert state.load(state_path).keys == {RESTAFVAL.key}
