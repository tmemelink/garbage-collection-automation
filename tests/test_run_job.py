"""The job wrapper must run straight from a checkout, writing only inside it."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess

import pytest

import garbage_collection_automation as gca

from .conftest import MINIMAL, REPO_ROOT

SCRIPT = REPO_ROOT / "src" / "run-job.sh"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock")


def run_wrapper(*args, **env_overrides) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env_overrides},
    )


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL)
    return path


@pytest.fixture
def echoing_install(tmp_path):
    """An install dir whose entry point only reports the arguments it was handed."""
    entry = tmp_path / "install" / ".venv" / "bin" / "garbage-collection-automation"
    entry.parent.mkdir(parents=True)
    entry.write_text('#!/bin/sh\necho "ARGS: $*"\n')
    entry.chmod(0o755)
    return entry.parent.parent.parent


def test_missing_config_points_at_the_example(tmp_path):
    result = run_wrapper(CONFIG_FILE=str(tmp_path / "absent.toml"))

    assert result.returncode == 1
    assert "config/config.example.toml" in result.stderr
    assert "/etc/" not in result.stderr


def test_checkout_run_uses_the_repository_entry_point(config_file, tmp_path):
    result = run_wrapper(
        "--version", CONFIG_FILE=str(config_file), LOCK_FILE=str(tmp_path / "run.lock")
    )

    assert result.returncode == 0, result.stderr
    assert gca.__version__ in result.stdout
    assert "starting garbage-collection-automation" in result.stdout
    assert "finished with exit code 0" in result.stdout


def test_lock_file_defaults_inside_the_repository(config_file):
    lock = REPO_ROOT / ".local" / "run.lock"
    lock.unlink(missing_ok=True)

    assert run_wrapper("--version", CONFIG_FILE=str(config_file)).returncode == 0
    assert lock.exists()


def test_a_checkout_run_keeps_its_state_inside_the_repository(
    config_file, echoing_install, tmp_path
):
    result = run_wrapper(
        CONFIG_FILE=str(config_file),
        LOCK_FILE=str(tmp_path / "run.lock"),
        INSTALL_DIR=str(echoing_install),
    )

    assert f"--state {REPO_ROOT}/.local/state.json" in result.stdout
    assert "/var/lib/" not in result.stdout


def test_an_installed_run_keeps_its_state_under_var_lib(config_file, echoing_install, tmp_path):
    """The same script outside a checkout: no repository to write to, so /var/lib it is."""
    installed = tmp_path / "opt" / "bin" / "run-job.sh"
    installed.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, installed)

    result = subprocess.run(
        [str(installed)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CONFIG_FILE": str(config_file),
            "LOCK_FILE": str(tmp_path / "run.lock"),
            "INSTALL_DIR": str(echoing_install),
        },
    )

    assert "--state /var/lib/garbage-collection-automation/state.json" in result.stdout


def test_a_run_that_overlaps_the_previous_one_is_skipped_quietly(config_file, tmp_path):
    """cron must not mail about an overlap, so a held lock is a success, not a failure."""
    lock = tmp_path / "run.lock"

    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_wrapper("--version", CONFIG_FILE=str(config_file), LOCK_FILE=str(lock))

    assert result.returncode == 0
    assert "another run is still in progress" in result.stdout
    assert "starting" not in result.stdout


def test_the_env_file_reaches_the_run(config_file, echoing_install, tmp_path):
    """The only way a token reaches a cron job, which inherits nothing from a shell."""
    env_file = tmp_path / "env"
    env_file.write_text("# a comment\nGCA_TODOIST_TOKEN=from-the-env-file\n")
    reporter = echoing_install / ".venv" / "bin" / "garbage-collection-automation"
    reporter.write_text('#!/bin/sh\necho "TOKEN: ${GCA_TODOIST_TOKEN:-unset}"\n')

    result = run_wrapper(
        CONFIG_FILE=str(config_file),
        ENV_FILE=str(env_file),
        LOCK_FILE=str(tmp_path / "run.lock"),
        INSTALL_DIR=str(echoing_install),
    )

    assert "TOKEN: from-the-env-file" in result.stdout


def test_a_run_without_an_env_file_is_not_a_failure(config_file, echoing_install, tmp_path):
    result = run_wrapper(
        CONFIG_FILE=str(config_file),
        ENV_FILE=str(tmp_path / "absent-env"),
        LOCK_FILE=str(tmp_path / "run.lock"),
        INSTALL_DIR=str(echoing_install),
    )

    assert result.returncode == 0, result.stderr


def test_an_installed_run_locks_outside_slash_tmp(config_file, echoing_install, tmp_path):
    """A world-writable lock lets any local user quietly stop every scheduled run."""
    installed = tmp_path / "opt" / "bin" / "run-job.sh"
    installed.parent.mkdir(parents=True)
    shutil.copy(SCRIPT, installed)
    state_dir = tmp_path / "var-lib"
    state_dir.mkdir()

    result = subprocess.run(
        [str(installed), "--version"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CONFIG_FILE": str(config_file),
            "STATE_DIR": str(state_dir),
            "INSTALL_DIR": str(echoing_install),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (state_dir / "run.lock").exists()
