"""The installer's file handling, exercised step by step without touching the host.

install.sh only runs `main` when it is executed, so a test can source it, point
every path at a temporary directory, and call one step at a time. The steps that
need root - chown, and uv itself - are stubbed; what is under test here is which
files end up in the install directory, which is where a promise in the README is
either kept or quietly broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest

from .conftest import REPO_ROOT

INSTALLER = REPO_ROOT / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_step(
    step: str, tmp_path, source, *, detached: bool = False, **overrides
) -> subprocess.CompletedProcess:
    """Source the installer, neutralise what needs root, and run one step.

    `detached` starts the step in a session of its own, which is how a step that
    looks for a terminal is given a host that has none.
    """
    fake_uv = tmp_path / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)

    settings = {
        "APP_USER": "nobody",
        "INSTALL_DIR": str(tmp_path / "opt"),
        "CONFIG_DIR": str(tmp_path / "etc"),
        "STATE_DIR": str(tmp_path / "var-lib"),
        "LOG_DIR": str(tmp_path / "var-log"),
        **overrides,
    }
    assignments = "\n".join(f'{key}="{value}"' for key, value in settings.items())

    script = textwrap.dedent(f"""
        set -euo pipefail
        source "{INSTALLER}"
        {assignments}
        SRC_DIR="{source}"
        UV_BIN="{fake_uv}"
        UV_PYTHON_INSTALL_DIR="{tmp_path / "absent-interpreter"}"
        chown() {{        # the host's users are none of this test's business,
            echo "chown $*" >> "{ownership_log(tmp_path)}"
        }}
        install() {{        # ... and neither is who the installed files belong to
            echo "install $*" >> "{ownership_log(tmp_path)}"
            local -a args=()
            while [ $# -gt 0 ]; do
                case "$1" in
                    -o|-g) shift 2 ;;
                    *) args+=("$1"); shift ;;
                esac
            done
            command install "${{args[@]}}"
        }}
        {step}
    """)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        start_new_session=detached,
        stdin=subprocess.DEVNULL if detached else None,
        timeout=60,
    )


def ownership_log(tmp_path):
    """Where the stubbed `install` and `chown` write down what they were asked.

    Neither can really change an owner in a test, and the ownership of the config
    file is what decides whether the page's save button works - so what the step
    asked for is the only thing left to assert.
    """
    return tmp_path / "ownership-calls"


def owner_of(tmp_path, name: str) -> str:
    """The user the last recorded call handed *name* to."""
    lines = [
        line for line in ownership_log(tmp_path).read_text().splitlines() if line.endswith(name)
    ]
    assert lines, f"nothing was recorded for {name}"
    fields = lines[-1].split()
    if fields[0] == "chown":
        return fields[1].split(":")[0]
    return fields[fields.index("-o") + 1]


@pytest.fixture
def source(tmp_path):
    """A source tree shaped like the one the bundle extracts to."""
    src = tmp_path / "src-tree"
    (src / "src" / "garbage_collection_automation").mkdir(parents=True)
    (src / "config").mkdir()
    (src / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (src / "uv.lock").write_text("# resolved at build time\n")
    (src / ".python-version").write_text("3.14\n")
    (src / "README.md").write_text("# readme\n")
    (src / "LICENSE").write_text("Apache-2.0\n")
    (src / "src" / "run-job.sh").write_text("#!/bin/sh\n")
    (src / "ui" / "static").mkdir(parents=True)
    (src / "ui" / "index.html").write_text("<!doctype html>\n")
    (src / "ui" / "static" / "app.css").write_text(".panel {}\n")
    (src / "ui" / "mockups").mkdir()
    (src / "ui" / "mockups" / "mockup-1.png").write_bytes(b"\x89PNG not really\n")
    (src / "src" / "garbage_collection_automation" / "__init__.py").write_text("")
    (src / "config" / "config.example.toml").write_text('[address]\npostcode = "1234AB"\n')
    return src


def test_the_lockfile_reaches_the_install_dir(tmp_path, source):
    """Without it `uv sync` re-resolves and the air-gapped install is not reproducible."""
    result = run_step("install_app", tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "opt" / "uv.lock").read_text() == "# resolved at build time\n"


def test_everything_uv_reads_reaches_the_install_dir(tmp_path, source):
    result = run_step("install_app", tmp_path, source)

    assert result.returncode == 0, result.stderr
    installed = tmp_path / "opt"
    assert (installed / "pyproject.toml").exists()
    assert (installed / ".python-version").exists()
    assert (installed / "src" / "garbage_collection_automation" / "__init__.py").exists()
    assert (installed / "bin" / "run-job.sh").exists()
    assert (installed / "LICENSE").exists()


def test_a_source_without_a_license_says_so_instead_of_failing_inside_uv(tmp_path, source):
    """pyproject.toml's license-files makes LICENSE part of the build, not a nicety."""
    (source / "LICENSE").unlink()

    result = run_step("install_app", tmp_path, source)

    assert result.returncode != 0
    assert "LICENSE" in result.stderr


def test_a_source_without_a_lockfile_says_so_instead_of_reusing_a_stale_one(tmp_path, source):
    """A leftover lock would pin versions the new source never resolved against."""
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / "uv.lock").write_text("# from the install before this one\n")
    (source / "uv.lock").unlink()

    result = run_step("install_app", tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "opt" / "uv.lock").exists()
    assert "no uv.lock" in result.stderr


def test_a_missing_managed_interpreter_does_not_fail_the_install(tmp_path, source):
    """uv only downloads one when the host has no Python that fits; chmod must cope."""
    result = run_step("install_app", tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert "reused an interpreter" in result.stdout


def test_the_config_is_written_once_and_then_left_alone(tmp_path, source):
    first = run_step("install_config", tmp_path, source)
    assert first.returncode == 0, first.stderr

    config = tmp_path / "etc" / "config.toml"
    config.write_text('[address]\npostcode = "1234AB"  # edited by hand\n')
    second = run_step("install_config", tmp_path, source)

    assert second.returncode == 0, second.stderr
    assert "edited by hand" in config.read_text()
    assert "keeping existing" in second.stdout


def test_the_config_belongs_to_the_service_user_so_the_page_can_save_it(tmp_path, source):
    """A save is a temp file plus a rename, so the file and its directory both."""
    result = run_step("install_config", tmp_path, source)
    assert result.returncode == 0, result.stderr

    assert owner_of(tmp_path, "config.toml") == "nobody"
    mode = (tmp_path / "etc").stat().st_mode & 0o7777
    assert mode & 0o020, "the service user cannot create the temp file to rename"
    assert mode & 0o1000, "without the sticky bit it could also replace env"


def test_without_the_web_interface_the_config_stays_root_s(tmp_path, source):
    """Nothing but root writes it then, and the widening would buy nothing."""
    result = run_step("install_config", tmp_path, source, INSTALL_WEB="0")
    assert result.returncode == 0, result.stderr

    assert owner_of(tmp_path, "config.toml") == "root"
    assert (tmp_path / "etc").stat().st_mode & 0o7777 == 0o750


def test_an_upgrade_that_adds_the_page_hands_the_existing_config_over(tmp_path, source):
    """The contents are the admin's; who may write them is this install's call."""
    assert run_step("install_config", tmp_path, source, INSTALL_WEB="0").returncode == 0
    config = tmp_path / "etc" / "config.toml"
    config.write_text('[address]\npostcode = "1234AB"  # edited by hand\n')

    second = run_step("install_config", tmp_path, source)

    assert second.returncode == 0, second.stderr
    assert "edited by hand" in config.read_text(), "an upgrade must not rewrite it"
    assert owner_of(tmp_path, "config.toml") == "nobody"


def test_the_env_file_is_written_once_and_then_left_alone(tmp_path, source):
    """An upgrade that wiped the token would silently disable the export."""
    assert run_step("install_config", tmp_path, source).returncode == 0

    env_file = tmp_path / "etc" / "env"
    assert "GCA_TODOIST_TOKEN" in env_file.read_text()
    env_file.write_text("GCA_TODOIST_TOKEN=the-real-one\n")

    assert run_step("install_config", tmp_path, source).returncode == 0
    assert env_file.read_text() == "GCA_TODOIST_TOKEN=the-real-one\n"


def test_the_env_file_the_installer_writes_holds_no_token_of_its_own(tmp_path, source):
    assert run_step("install_config", tmp_path, source).returncode == 0

    written = (tmp_path / "etc" / "env").read_text()

    assert "#GCA_TODOIST_TOKEN=" in written, "the key has to be there to uncomment"
    assert not [
        line for line in written.splitlines() if line.strip() and not line.startswith("#")
    ], "the template must be comments only"


def test_the_page_reaches_the_install_dir(tmp_path, source):
    """The server serves <install dir>/ui; without this it would have nothing to serve."""
    result = run_step("install_app", tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "opt" / "ui" / "index.html").exists()
    assert (tmp_path / "opt" / "ui" / "static" / "app.css").exists()


def test_the_design_sources_stay_on_the_workstation(tmp_path, source):
    """A mockup is megabytes of PNG the container has no use for."""
    assert run_step("install_app", tmp_path, source).returncode == 0

    assert not (tmp_path / "opt" / "ui" / "mockups").exists()


def test_a_page_removed_from_the_source_does_not_survive_in_the_install(tmp_path, source):
    """Otherwise the server would keep serving the interface of the version before."""
    assert run_step("install_app", tmp_path, source).returncode == 0
    (tmp_path / "opt" / "ui" / "stale.html").write_text("<!doctype html>\n")

    assert run_step("install_app", tmp_path, source).returncode == 0

    assert not (tmp_path / "opt" / "ui" / "stale.html").exists()


def test_the_cron_entry_names_the_installed_wrapper(tmp_path, source):
    shutil.copytree(REPO_ROOT / "scheduling", source / "scheduling")
    cron_dir = tmp_path / "cron.d"
    cron_dir.mkdir()

    result = run_step(
        "install_schedule() { :; }\n"
        f'sed -e "s|@APP_USER@|nobody|g" -e "s|@INSTALL_DIR@|{tmp_path / "opt"}|g" '
        f'-e "s|@LOG_DIR@|{tmp_path / "var-log"}|g" '
        f'"$SRC_DIR/scheduling/garbage-collection-automation.cron" > "{cron_dir}/entry"',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    entry = (cron_dir / "entry").read_text()
    assert "@APP_USER@" not in entry and "@INSTALL_DIR@" not in entry
    assert f"{tmp_path / 'opt'}/bin/run-job.sh" in entry


def test_the_build_caches_do_not_survive_the_install(tmp_path, source):
    """On a 2 GiB container the wheels and package lists are a tenth of the disk."""
    recorder = tmp_path / "uv-calls"
    recording_uv = tmp_path / "recording-uv"
    recording_uv.write_text(f'#!/bin/sh\necho "uv $*" >> "{recorder}"\n')
    recording_uv.chmod(0o755)

    lists = tmp_path / "apt-lists"
    lists.mkdir()
    (lists / "deb.debian.org_dists_bookworm_InRelease").write_text("# package list\n")

    result = run_step(
        f'UV_BIN="{recording_uv}"\n'
        f'apt-get() {{ echo "apt-get $*" >> "{recorder}"; }}\n'
        "reclaim_space",
        tmp_path,
        source,
        APT_LISTS_DIR=str(lists),
    )

    assert result.returncode == 0, result.stderr
    called = recorder.read_text()
    assert "uv cache clean" in called
    assert "apt-get clean" in called
    assert list(lists.iterdir()) == []


def test_a_cache_that_will_not_clear_is_not_worth_failing_the_install_over(tmp_path, source):
    """The application is installed and working by this point; disk is the only loss."""
    stubborn_uv = tmp_path / "stubborn-uv"
    stubborn_uv.write_text("#!/bin/sh\nexit 1\n")
    stubborn_uv.chmod(0o755)

    result = run_step(
        f'UV_BIN="{stubborn_uv}"\napt-get() {{ :; }}\nreclaim_space',
        tmp_path,
        source,
        APT_LISTS_DIR=str(tmp_path / "apt-lists"),
    )

    assert result.returncode == 0, result.stderr
    assert "could not clear the uv cache" in result.stderr


def web_step(tmp_path, source, step="install_web_service", **overrides):
    """Run a web-service step with systemd stood in for by a recording stub."""
    shutil.copytree(REPO_ROOT / "scheduling", source / "scheduling", dirs_exist_ok=True)
    units = tmp_path / "systemd"
    units.mkdir(exist_ok=True)
    recorder = tmp_path / "systemctl-calls"

    result = run_step(
        f'systemctl() {{ echo "systemctl $*" >> "{recorder}"; }}\n{step}',
        tmp_path,
        source,
        SYSTEMD_DIR=str(units),
        **overrides,
    )
    called = recorder.read_text() if recorder.exists() else ""
    return result, units / "garbage-collection-automation-web.service", called


def test_the_web_unit_names_the_installed_command_and_config(tmp_path, source):
    """systemd is handed absolute paths; a leftover placeholder would fail at start."""
    result, unit_file, _called = web_step(tmp_path, source)

    assert result.returncode == 0, result.stderr
    unit = unit_file.read_text()
    for placeholder in ("@APP_USER@", "@INSTALL_DIR@", "@CONFIG_DIR@", "@STATE_DIR@"):
        assert placeholder not in unit, f"{placeholder} was left unsubstituted"
    assert f"ExecStart={tmp_path / 'opt'}/.venv/bin/garbage-collection-automation-web" in unit
    assert f"--config {tmp_path / 'etc'}/config.toml" in unit
    assert f"--ui-dir {tmp_path / 'opt'}/ui" in unit
    assert f"--state {tmp_path / 'var-lib'}/state.json" in unit
    assert f"EnvironmentFile=-{tmp_path / 'etc'}/env" in unit
    assert "User=nobody" in unit


def test_the_unit_may_write_the_config_and_the_state_and_nothing_else(tmp_path, source):
    """ProtectSystem=strict is the whole filesystem; the buttons write two places."""
    result, unit_file, _ = web_step(tmp_path, source)

    assert result.returncode == 0, result.stderr
    unit = unit_file.read_text()
    assert "ProtectSystem=strict" in unit
    writable = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert writable == [f"ReadWritePaths={tmp_path / 'etc'} {tmp_path / 'var-lib'}"]


def test_the_unit_is_enabled_and_restarted_so_an_upgrade_runs_the_new_code(tmp_path, source):
    """`enable` alone would leave yesterday's process serving today's files."""
    result, _, called = web_step(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert "systemctl daemon-reload" in called
    assert "systemctl enable garbage-collection-automation-web.service" in called
    assert "systemctl restart garbage-collection-automation-web.service" in called


def test_no_web_removes_a_unit_an_earlier_install_left(tmp_path, source):
    """--no-web is also how an install that had the interface gives it up again."""
    units = tmp_path / "systemd"
    units.mkdir()
    (units / "garbage-collection-automation-web.service").write_text("# from the install before\n")

    result, unit_file, called = web_step(
        tmp_path, source, step="INSTALL_WEB=0\ninstall_web_service"
    )

    assert result.returncode == 0, result.stderr
    assert "skipping the web interface" in result.stdout
    assert not unit_file.exists()
    assert "systemctl disable --now" in called


def test_a_host_without_systemd_says_so_instead_of_failing(tmp_path, source):
    """The installer must still finish: the job itself is cron, not a service."""
    result, unit_file, called = web_step(
        tmp_path, source, step="has_systemd() { return 1; }\ninstall_web_service"
    )

    assert result.returncode == 0, result.stderr
    assert "no systemd" in result.stderr
    assert not unit_file.exists()
    assert called == ""


def test_the_web_interface_is_installed_unless_it_is_declined(tmp_path, source):
    result = run_step(
        'parse_args; echo "default=${INSTALL_WEB}"\n'
        'parse_args --no-web; echo "declined=${INSTALL_WEB}"',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    assert "default=1" in result.stdout
    assert "declined=0" in result.stdout


def home_command(tmp_path, source, **overrides):
    """Write the by-hand command into a home directory of this test's own."""
    command = tmp_path / "root" / "run-garbage-collection.sh"
    result = run_step("install_home_command", tmp_path, source, HOME_CMD=str(command), **overrides)
    return result, command


def test_the_by_hand_command_lands_in_the_home_directory(tmp_path, source):
    """`pct enter` lands in root's home, so a command there needs no path to run."""
    result, command = home_command(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert command.exists(), "the installer left nothing to run by hand"
    assert os.access(command, os.X_OK)
    written = command.read_text()
    assert f"{tmp_path / 'opt'}/bin/run-job.sh" in written
    assert "--dry-run" in written, "the comment header is the only usage anyone will read"


def test_the_by_hand_command_runs_the_job_as_the_service_user(tmp_path, source):
    """The lock and state.json belong to that user; root would leave files cron cannot write."""
    _, command = home_command(tmp_path, source)
    (tmp_path / "opt" / "bin").mkdir(parents=True, exist_ok=True)
    job = tmp_path / "opt" / "bin" / "run-job.sh"
    job.write_text("#!/bin/sh\necho 'ran as root'\n")
    job.chmod(0o755)

    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "runuser").write_text('#!/bin/sh\necho "runuser $*"\n')
    (stub_dir / "runuser").chmod(0o755)

    result = subprocess.run(
        [str(command), "--dry-run"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "ran as root" not in result.stdout
    assert f"runuser -u nobody -- {job} --dry-run" in result.stdout


def test_the_by_hand_command_says_so_when_there_is_nothing_installed(tmp_path, source):
    """It outlives an --uninstall only if someone kept a copy; it should not be cryptic."""
    _, command = home_command(tmp_path, source)

    result = subprocess.run([str(command)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 1
    assert "not installed" in result.stderr


def test_the_run_at_the_end_can_be_answered_in_advance(tmp_path, source):
    result = run_step(
        'parse_args; echo "default=[${RUN_NOW}]"\n'
        'parse_args --run-now; echo "asked=[${RUN_NOW}]"\n'
        'parse_args --no-run-now; echo "declined=[${RUN_NOW}]"',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    assert "default=[]" in result.stdout, "an unanswered question is what makes it a question"
    assert "asked=[1]" in result.stdout
    assert "declined=[0]" in result.stdout


def test_the_run_at_the_end_goes_through_the_command_the_user_will_type(tmp_path, source):
    """Offer and documentation cannot drift apart while the offer is what is documented."""
    stand_in = tmp_path / "run-garbage-collection.sh"
    stand_in.write_text("#!/bin/sh\necho 'the by-hand command ran'\n")
    stand_in.chmod(0o755)

    result = run_step("RUN_NOW=1\nmaybe_run_now", tmp_path, source, HOME_CMD=str(stand_in))

    assert result.returncode == 0, result.stderr
    assert "the by-hand command ran" in result.stdout


def test_an_install_with_no_terminal_to_ask_on_does_not_wait_for_an_answer(tmp_path, source):
    """pct exec, cloud-init and CI have no tty; the installer has to finish, not hang."""
    result = run_step(
        'RUN_NOW=""\nmaybe_run_now',
        tmp_path,
        source,
        detached=True,
        HOME_CMD=str(tmp_path / "never-called.sh"),
    )

    assert result.returncode == 0, result.stderr
    assert "not running it now" in result.stdout


def test_a_run_that_fails_does_not_fail_the_install(tmp_path, source):
    """By then the install is done and verified; what failed is the configuration."""
    result = run_step("RUN_NOW=1\nmaybe_run_now", tmp_path, source, HOME_CMD="/bin/false")

    assert result.returncode == 0, result.stderr
    assert "the install itself is fine" in result.stderr
