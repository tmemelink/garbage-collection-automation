"""The CLI: the command line, the config file, and the outcome-to-exit-code map.

What a run actually does is the application module's business, and is tested in
test_application.py; here it is stubbed out.
"""

from __future__ import annotations

import logging

import pytest

import garbage_collection_automation as gca
from garbage_collection_automation import application
from garbage_collection_automation.application import JobResult, Status

from .conftest import MINIMAL


@pytest.fixture
def config_path(write_config):
    return write_config(MINIMAL)


@pytest.fixture
def job(monkeypatch):
    """Stand in for the run, and remember how the CLI asked for it."""
    calls: list[dict] = []

    def run(config, *, state_path, dry_run):
        calls.append({"config": config, "state_path": state_path, "dry_run": dry_run})
        return JobResult(Status.OK, "2 collection(s) upcoming; nothing to change in todoist")

    monkeypatch.setattr(application, "run", run)
    return calls


@pytest.fixture
def failing_job(monkeypatch):
    """Let a test choose how the run ends."""

    def ending_with(status: Status, summary: str) -> None:
        monkeypatch.setattr(application, "run", lambda config, **kwargs: JobResult(status, summary))

    return ending_with


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        gca.main(["--version"])

    assert excinfo.value.code == 0
    assert gca.__version__ in capsys.readouterr().out


def test_the_paths_default_to_the_installed_ones():
    args = gca.build_parser().parse_args([])

    assert args.config == gca.DEFAULT_CONFIG
    assert args.state == gca.DEFAULT_STATE
    assert args.dry_run is False


def test_a_run_gets_the_config_and_the_state_path_it_was_given(config_path, tmp_path, job):
    state_path = tmp_path / "elsewhere.json"

    assert gca.main(["--config", str(config_path), "--state", str(state_path)]) == gca.EXIT_OK
    assert job[0]["state_path"] == state_path
    assert job[0]["config"].address.postcode == "1234AB"
    assert job[0]["dry_run"] is False


def test_dry_run_reaches_the_job(config_path, job):
    assert gca.main(["--config", str(config_path), "--dry-run"]) == gca.EXIT_OK
    assert job[0]["dry_run"] is True


def test_a_finished_run_is_not_reported_as_a_problem(config_path, job, caplog):
    gca.main(["--config", str(config_path)])

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_bad_config_reports_without_a_traceback(write_config, caplog):
    path = write_config("[address]\npostcode = 'nope'\nhouse_number = 1\n")

    assert gca.main(["--config", str(path)]) == gca.EXIT_CONFIG_ERROR
    assert "postcode" in caplog.text


def test_missing_config_reports_without_a_traceback(tmp_path, caplog):
    assert gca.main(["--config", str(tmp_path / "absent.toml")]) == gca.EXIT_CONFIG_ERROR
    assert "not found" in caplog.text


@pytest.mark.parametrize(
    ("status", "summary", "expected"),
    [
        (
            Status.COLLECTION_ERROR,
            "collection: no collection schedule for 1234AB 56",
            gca.EXIT_COLLECTION_ERROR,
        ),
        (
            Status.EXPORT_ERROR,
            "export: cannot write /var/lib/x/state.json: Read-only",
            gca.EXIT_EXPORT_ERROR,
        ),
        (
            Status.TODOIST_ERROR,
            "todoist: todoist refused the token (HTTP 401)",
            gca.EXIT_TODOIST_ERROR,
        ),
    ],
)
def test_a_failed_run_becomes_an_exit_code_and_a_line(
    config_path, failing_job, caplog, status, summary, expected
):
    """A scheduled run must leave a readable log, never a stack trace."""
    failing_job(status, summary)

    assert gca.main(["--config", str(config_path)]) == expected
    assert summary in caplog.text


def test_every_outcome_has_an_exit_code():
    """A new Status without an exit code would crash the run it was meant to describe."""
    assert set(gca.EXIT_CODES) == set(Status)


def test_the_http_client_does_not_narrate_the_run(config_path, job):
    """httpx would log the full query string on every request; keep the cron log clean."""
    gca.main(["--config", str(config_path)])

    assert logging.getLogger("httpx").level == logging.WARNING


def test_debug_logging_still_suppresses_http_query_strings(write_config, job):
    path = write_config(MINIMAL + '\n[logging]\nlevel = "debug"\n')

    gca.main(["--config", str(path)])

    assert logging.getLogger("httpx").level == logging.WARNING


def test_log_level_follows_the_config(write_config, job):
    path = write_config(MINIMAL + '\n[logging]\nlevel = "warning"\n')

    gca.main(["--config", str(path)])

    assert logging.getLogger().level == logging.WARNING
