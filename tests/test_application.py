"""One run, end to end: source -> schedule -> Todoist -> the local record."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import httpx
import pytest

from garbage_collection_automation import (
    application,
    data_collection,
    data_export,
    data_processing,
    state,
)
from garbage_collection_automation.application import Status
from garbage_collection_automation.configuration import TodoistExportConfig
from garbage_collection_automation.data_collection import RawSchedule
from garbage_collection_automation.reconciliation import Decision, Report

from .conftest import make_config, read_fixture
from .test_reconciliation import (
    GFT,
    RESTAFVAL,
    TODAY,
    FakeTodoist,
    UnreachableTodoist,
    config,
    record,
    remote,
    write_state,
)

#: The schedule the stubbed source hands back: the two collections above, raw.
SCHEDULE = RawSchedule(
    url="https://example.invalid/",
    fetched_at=datetime(2026, 8, 19, 4, 30, tzinfo=UTC),
    address="Voorbeeldstraat 21, Voorbeeldstad",
    data_version="1786701549",
    entries=(
        {"type": "restafval", "date": "2026-08-20"},
        {"type": "gft", "date": "2026-08-27"},
        {"type": "restafval", "date": "2026-07-30"},  # already collected
        {"type": "glas", "date": "2026-08-21"},  # not a configured type
    ),
)


#: The real fetch, taken before the fixture below replaces it with a stub.
FETCH = data_collection.collect


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture(autouse=True)
def source(monkeypatch):
    """No test here touches the network, and every one of them runs on TODAY."""
    monkeypatch.setattr(data_processing, "today", lambda: TODAY)
    monkeypatch.setattr(data_collection, "collect", lambda config: SCHEDULE)


@pytest.fixture
def unreachable_source(monkeypatch):
    def refuse(config):
        raise data_collection.CollectionError("could not reach api.mijnafvalwijzer.nl")

    monkeypatch.setattr(data_collection, "collect", refuse)


# --- the run itself -------------------------------------------------------------------


def test_a_first_run_exports_every_upcoming_collection(state_path):
    client = FakeTodoist()

    result = application.run(config(), state_path=state_path, client=client, today=TODAY)

    assert result.status is Status.OK
    assert result.ok
    assert result.collections == (RESTAFVAL, GFT)
    assert client.created == [RESTAFVAL, GFT]
    assert result.delta.create == (RESTAFVAL, GFT)


def test_the_run_processes_the_schedule_it_collected(state_path):
    """The past and the streams we did not ask for never reach Todoist."""
    client = FakeTodoist()

    result = application.run(config(), state_path=state_path, client=client, today=TODAY)

    assert [str(entry) for entry in result.collections] == [
        "2026-08-20 Restafval",
        "2026-08-27 GFT",
    ]


def test_the_day_the_caller_fixes_reaches_both_halves_of_the_run(state_path, monkeypatch):
    """`today=` decides the window as well as the past, or the two halves disagree."""
    monkeypatch.setattr(data_processing, "today", lambda: date(2000, 1, 1))
    client = FakeTodoist()

    result = application.run(config(), state_path=state_path, client=client, today=TODAY)

    assert result.collections == (RESTAFVAL, GFT)
    assert client.created == [RESTAFVAL, GFT]


def test_the_run_records_what_it_exported(state_path):
    application.run(config(), state_path=state_path, client=FakeTodoist(), today=TODAY)

    assert state.load(state_path).keys == {RESTAFVAL.key, GFT.key}


def test_an_unchanged_schedule_costs_no_api_call(state_path):
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))

    result = application.run(
        config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert result.status is Status.OK
    assert result.delta.is_empty
    assert result.decision.check_remote is False


def test_a_moved_schedule_is_reconciled_against_todoist(state_path):
    """Yesterday's record says one thing, today's schedule another: Todoist decides."""
    write_state(state_path, record(RESTAFVAL, "a"))
    client = FakeTodoist(remote(RESTAFVAL, "a"))

    result = application.run(config(), state_path=state_path, client=client, today=TODAY)

    assert client.listed == 1
    assert result.delta.create == (GFT,)
    assert result.decision.check_remote is True


def test_a_disabled_target_leaves_todoist_alone(state_path):
    result = application.run(
        make_config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert result.status is Status.OK
    assert result.collections == (RESTAFVAL, GFT)
    assert result.delta.is_empty
    assert not state_path.exists()


def test_the_schedule_version_reaches_the_record(state_path):
    """It is the signal that explains a morning's changes; it must not be dropped."""
    application.run(config(), state_path=state_path, client=FakeTodoist(), today=TODAY)

    assert state.load(state_path).data_version == SCHEDULE.data_version
    assert state.load(state_path).address == SCHEDULE.address


# --- the dry run ----------------------------------------------------------------------


def test_a_dry_run_writes_nothing_anywhere(state_path):
    result = application.run(
        config(), state_path=state_path, client=UnreachableTodoist(), dry_run=True, today=TODAY
    )

    assert result.status is Status.OK
    assert result.dry_run
    assert result.delta is None
    assert not state_path.exists()


def test_a_dry_run_says_whether_todoist_would_be_queried(state_path):
    without_record = application.run(config(), state_path=state_path, dry_run=True, today=TODAY)
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))
    with_record = application.run(config(), state_path=state_path, dry_run=True, today=TODAY)

    assert without_record.decision.check_remote is True
    assert with_record.decision.check_remote is False


def test_a_dry_run_lists_what_it_would_export(state_path, caplog):
    with caplog.at_level("INFO"):
        application.run(config(), state_path=state_path, dry_run=True, today=TODAY)

    assert "would export 2026-08-20 Restafval" in caplog.text


# --- when a step cannot do its job ----------------------------------------------------


def test_an_unreachable_source_ends_the_run(state_path, unreachable_source):
    result = application.run(
        config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert result.status is Status.COLLECTION_ERROR
    assert "could not reach" in result.summary
    assert result.collections == ()


def test_a_schedule_without_a_wanted_collection_is_not_a_failure(state_path, monkeypatch):
    """An empty window is normal in a quiet fortnight - and it clears stale to-dos."""
    quiet = replace(SCHEDULE, entries=({"type": "restafval", "date": "2020-01-01"},))
    monkeypatch.setattr(data_collection, "collect", lambda config: quiet)
    client = FakeTodoist(remote(RESTAFVAL, "a"))
    write_state(state_path, record(RESTAFVAL, "a"))

    result = application.run(config(), state_path=state_path, client=client, today=TODAY)

    assert result.status is Status.OK
    assert result.collections == ()
    assert client.deleted == ["a"]


def test_an_unrecordable_export_is_reported_as_such(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")

    result = application.run(
        config(), state_path=blocked / "state.json", client=FakeTodoist(), today=TODAY
    )

    assert result.status is Status.EXPORT_ERROR
    assert "state.json" in result.summary


def test_a_missing_todoist_client_is_reported_as_such(state_path):
    """Until the Todoist module lands, a real run stops here without a traceback."""
    result = application.run(config(), state_path=state_path, today=TODAY)

    assert result.status is Status.NOT_IMPLEMENTED
    assert "todoist_client" in result.summary


def test_a_todoist_that_breaks_mid_delta_is_not_swallowed(state_path):
    """Their API failing is not one of our outcomes; the run fails loudly."""
    client = FakeTodoist(refuse=GFT.key)

    with pytest.raises(RuntimeError, match="todoist refused"):
        application.run(config(), state_path=state_path, client=client, today=TODAY)


# --- the whole chain, on a real response ----------------------------------------------


def test_two_runs_on_the_captured_response_cost_one_todoist_query(state_path, monkeypatch):
    """Every module in its real form, from the HTTP body down to the state file.

    Only Todoist is stood in for - and the second morning, it is not called at all.
    """
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=read_fixture("afvalwijzer_1234ab_21.json"))
        )
    )
    monkeypatch.setattr(data_collection, "collect", lambda cfg: FETCH(cfg, client=http))
    todoist = FakeTodoist()

    first = application.run(config(), state_path=state_path, client=todoist, today=TODAY)
    second = application.run(
        config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert [str(entry) for entry in first.collections] == [
        "2026-08-24 GFT",
        "2026-08-28 Restafval",
        "2026-08-31 GFT",
        "2026-09-01 Papier",
        "2026-09-07 GFT",
        "2026-09-14 GFT",
    ]
    assert todoist.created == list(first.collections)
    assert todoist.listed == 1
    assert second.delta.is_empty
    assert state.load(state_path).keys == {entry.key for entry in first.collections}


# --- the one-line summary -------------------------------------------------------------


def test_the_summary_says_what_the_run_did(state_path):
    result = application.run(config(), state_path=state_path, client=FakeTodoist(), today=TODAY)

    assert "2 collection(s)" in result.summary
    assert "2 to add" in result.summary


def test_the_summary_of_a_quiet_run_says_nothing_changed(state_path):
    write_state(state_path, record(RESTAFVAL, "a"), record(GFT, "b"))

    result = application.run(
        config(), state_path=state_path, client=UnreachableTodoist(), today=TODAY
    )

    assert "nothing to change" in result.summary


def test_the_summary_of_a_dry_run_says_it_wrote_nothing(state_path):
    result = application.run(config(), state_path=state_path, dry_run=True, today=TODAY)

    assert "dry run" in result.summary


def test_the_run_is_logged_as_one_readable_line(state_path, caplog):
    with caplog.at_level("INFO"):
        result = application.run(config(), state_path=state_path, client=FakeTodoist(), today=TODAY)

    assert result.summary in caplog.text


# --- the pieces the CLI and a future web app both need --------------------------------


def test_the_default_day_is_the_dutch_one(state_path, monkeypatch):
    """Without an explicit today the run must still use Amsterdam's date, not the host's."""
    seen: list[date] = []

    def note_the_day(collections, config, **kwargs):
        seen.append(kwargs["today"])
        return Report(Decision(False, "stubbed"))

    monkeypatch.setattr(data_processing, "today", lambda: date(2026, 8, 19))
    monkeypatch.setattr(data_export, "export", note_the_day)

    application.run(config(), state_path=state_path, client=FakeTodoist())

    assert seen == [date(2026, 8, 19)]


def test_a_config_with_todoist_off_needs_no_token(state_path):
    """The disabled path must not trip over the client it never builds."""
    off = make_config(todoist=TodoistExportConfig(enabled=False))

    assert application.run(off, state_path=state_path, today=TODAY).ok


# --- check(): what a run would do, without doing any of it ----------------------------


def test_check_reports_the_delta_without_writing_a_single_todo(state_path):
    """The whole point of the button: an answer that costs nothing but an API read."""
    client = FakeTodoist()

    result = application.check(config(), state_path=state_path, client=client, today=TODAY)

    assert result.ok
    assert result.dry_run
    assert result.delta.create == (RESTAFVAL, GFT)
    assert client.created == []
    assert client.updated == []
    assert client.deleted == []
    assert not state_path.exists(), "check() must not write the local record"


def test_check_asks_todoist_even_when_the_local_record_already_fits(state_path):
    """A dry run would take the record's word for it; this is the button that does not."""
    write_state(state_path, record(RESTAFVAL, "id-1"), record(GFT, "id-2"))
    client = FakeTodoist(remote(RESTAFVAL, "id-1"), remote(GFT, "id-2"))

    result = application.check(config(), state_path=state_path, client=client, today=TODAY)

    assert client.listed == 1
    assert result.queried is True
    assert result.decision.check_remote is False, "the record fitted; it was asked anyway"
    assert result.delta.is_empty
    assert "already matches" in result.summary


def test_check_finds_what_the_local_record_cannot_know(state_path):
    """A todo deleted in Todoist by hand: the record still claims it, only the API knows."""
    write_state(state_path, record(RESTAFVAL, "id-1"), record(GFT, "id-2"))
    client = FakeTodoist(remote(RESTAFVAL, "id-1"))

    result = application.check(config(), state_path=state_path, client=client, today=TODAY)

    assert result.delta.create == (GFT,)
    assert "would need" in result.summary


def test_check_with_the_export_disabled_says_so_and_asks_nobody(state_path):
    off = make_config(todoist=TodoistExportConfig(enabled=False))

    result = application.check(off, state_path=state_path, client=UnreachableTodoist(), today=TODAY)

    assert result.ok
    assert result.delta is None
    assert "disabled" in result.summary


def test_check_reports_an_unreachable_source_the_way_a_run_does(state_path, unreachable_source):
    result = application.check(config(), state_path=state_path, client=FakeTodoist(), today=TODAY)

    assert result.status is Status.COLLECTION_ERROR
    assert result.dry_run


def test_check_reports_the_client_that_does_not_exist_yet_as_such(state_path):
    """Until data_export.todoist_client() is written, the page must say so plainly."""
    result = application.check(config(), state_path=state_path, today=TODAY)

    assert result.status is Status.NOT_IMPLEMENTED
    assert result.dry_run, "nothing was written, whatever else went wrong"
    assert result.collections == (RESTAFVAL, GFT)
