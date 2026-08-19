"""The export seam: it decides whether to run at all, and hands over the client."""

from __future__ import annotations

import pytest

from garbage_collection_automation import data_export, reconciliation, state
from garbage_collection_automation.configuration import TodoistExportConfig

from .conftest import make_config
from .test_reconciliation import (
    GFT,
    RESTAFVAL,
    TODAY,
    FakeTodoist,
    UnreachableTodoist,
    config,
    record,
    write_state,
)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


def test_a_disabled_target_is_left_completely_alone(state_path, caplog):
    disabled = make_config(todoist=TodoistExportConfig(enabled=False))

    with caplog.at_level("INFO"):
        report = data_export.export(
            [RESTAFVAL],
            disabled,
            state_path=state_path,
            client=UnreachableTodoist(),
            today=TODAY,
        )

    assert report.delta.is_empty
    assert report.queried is False
    assert not state_path.exists()
    assert "disabled" in caplog.text


def test_an_enabled_target_is_reconciled(state_path):
    client = FakeTodoist()

    report = data_export.export(
        [RESTAFVAL, GFT], config(), state_path=state_path, client=client, today=TODAY
    )

    assert client.created == [RESTAFVAL, GFT]
    assert report.delta.create == (RESTAFVAL, GFT)


def test_without_a_client_the_todoist_module_is_still_missing(state_path):
    """Until the Todoist setup feature lands, a real run stops here - and says so."""
    with pytest.raises(NotImplementedError, match="todoist_client"):
        data_export.export([RESTAFVAL], config(), state_path=state_path, today=TODAY)


def test_a_preview_reports_the_decision_without_making_it(state_path, caplog):
    write_state(state_path, record(RESTAFVAL, "a"))
    before = state_path.read_text()

    with caplog.at_level("INFO"):
        data_export.preview([RESTAFVAL], config(), state_path=state_path, today=TODAY)

    assert "would not be touched" in caplog.text
    assert state_path.read_text() == before


def test_a_preview_says_when_todoist_would_be_queried(state_path, caplog):
    with caplog.at_level("INFO"):
        data_export.preview([RESTAFVAL], config(), state_path=state_path, today=TODAY)

    assert "would be queried" in caplog.text
    assert not state_path.exists()


def test_a_preview_of_a_disabled_target_says_so(state_path, caplog):
    off = make_config(todoist=TodoistExportConfig(enabled=False))

    with caplog.at_level("INFO"):
        data_export.preview([RESTAFVAL], off, state_path=state_path, today=TODAY)

    assert "disabled" in caplog.text


def test_the_client_the_reconciler_wants_is_the_one_export_supplies():
    """One protocol, so the future Todoist module has a single shape to satisfy."""
    assert isinstance(FakeTodoist(), reconciliation.TodoistClient)


def test_an_unwritable_state_file_is_reported_as_such(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")

    with pytest.raises(state.StateError):
        data_export.export(
            [RESTAFVAL],
            config(),
            state_path=blocked / "state.json",
            client=FakeTodoist(),
            today=TODAY,
        )
