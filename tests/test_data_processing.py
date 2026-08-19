"""Tests for turning the raw schedule into the collections we care about."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time

import pytest

from garbage_collection_automation import data_processing
from garbage_collection_automation.data_collection import RawSchedule
from garbage_collection_automation.data_processing import Collection

from .conftest import make_config, read_fixture


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Freeze "now" so the fixture's 2026 schedule stays meaningful."""
    frozen = date(2026, 8, 19)
    monkeypatch.setattr(data_processing, "today", lambda: frozen)
    return frozen


def raw(*entries: dict) -> RawSchedule:
    return RawSchedule(
        url="https://example.invalid/",
        fetched_at=datetime(2026, 8, 19, 4, 30, tzinfo=UTC),
        address="Voorbeeldstraat 21, Voorbeeldstad",
        data_version="1786701549",
        entries=entries,
    )


def entry(day: str, code: str = "restafval") -> dict:
    return {"nameType": code, "type": code, "date": day}


def test_the_past_is_dropped_and_today_is_kept():
    """We only ever create to-dos for what still has to happen."""
    result = data_processing.process(
        raw(entry("2026-08-18"), entry("2026-08-19"), entry("2026-08-20")), make_config()
    )

    assert [c.date for c in result] == [date(2026, 8, 19), date(2026, 8, 20)]


def test_the_caller_can_fix_the_day_the_window_starts_from(today):
    """A caller that pins the day must pin it here too, not just for the export half."""
    schedule = raw(entry("2026-08-20"), entry("2026-10-01"))

    result = data_processing.process(schedule, make_config(), today=date(2026, 9, 25))

    assert [c.date for c in result] == [date(2026, 10, 1)]
    assert today == date(2026, 8, 19), "the frozen clock must not be what decided that"


def test_the_lookahead_window_is_inclusive():
    config = make_config(lookahead_days=7)

    result = data_processing.process(raw(entry("2026-08-26"), entry("2026-08-27")), config)

    assert [c.date for c in result] == [date(2026, 8, 26)]


def test_only_the_configured_types_survive():
    config = make_config(types=("gft",))

    result = data_processing.process(
        raw(entry("2026-08-24", "gft"), entry("2026-08-25", "pd")), config
    )

    assert [c.code for c in result] == ["gft"]


def test_streams_we_never_asked_about_are_ignored():
    """Municipalities publish extra rows (kerstbomen, milieustraat); they must not crash us."""
    result = data_processing.process(
        raw(entry("2026-08-24", "milieustraat"), entry("2026-08-25", "restafval")), make_config()
    )

    assert [c.code for c in result] == ["restafval"]


def test_an_unreadable_date_is_skipped_not_fatal(caplog):
    entries = raw(entry("wanneer dan ook"), entry("2026-08-20"))

    result = data_processing.process(entries, make_config(types=("restafval",)))

    assert [c.date for c in result] == [date(2026, 8, 20)]
    assert "wanneer dan ook" in caplog.text


def test_a_stream_this_address_never_gets_is_flagged(caplog):
    """An empty result for a whole stream is more likely a config typo than a quiet month."""
    result = data_processing.process(
        raw(entry("2026-08-20", "restafval")), make_config(types=("restafval", "papier"))
    )

    assert [c.code for c in result] == ["restafval"]
    assert "never mentions papier" in caplog.text


def test_duplicates_are_collapsed_and_the_result_is_ordered():
    result = data_processing.process(
        raw(
            entry("2026-08-25", "papier"),
            entry("2026-08-20", "restafval"),
            entry("2026-08-20", "restafval"),
            entry("2026-08-20", "gft"),
        ),
        make_config(),
    )

    assert [str(c) for c in result] == [
        "2026-08-20 GFT",
        "2026-08-20 Restafval",
        "2026-08-25 Papier",
    ]


def test_the_label_is_dutch_and_readable():
    assert Collection(date(2026, 8, 25), "pd").waste_type == "PMD"
    assert str(Collection(date(2026, 8, 25), "gft")) == "2026-08-25 GFT"


@pytest.mark.parametrize(
    ("day", "offset"),
    [(date(2026, 8, 24), "+02:00"), (date(2026, 12, 21), "+01:00")],
)
def test_due_at_is_dutch_local_time_across_dst(day, offset):
    """Cron runs in whatever the host thinks the time is; the todo must not drift."""
    due = Collection(day, "gft").due_at(time(7, 0))

    assert due.isoformat() == f"{day.isoformat()}T07:00:00{offset}"


def test_the_captured_schedule_yields_the_real_upcoming_collections():
    payload = json.loads(read_fixture("afvalwijzer_1234ab_21.json"))
    entries = tuple(payload["data"]["ophaaldagen"]["data"])

    result = data_processing.process(raw(*entries), make_config(lookahead_days=14))

    assert [str(c) for c in result] == [
        "2026-08-24 GFT",
        "2026-08-28 Restafval",
        "2026-08-31 GFT",
        "2026-09-01 Papier",
    ]
