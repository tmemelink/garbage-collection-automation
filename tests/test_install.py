"""The installer's file handling, exercised step by step without touching the host.

install.sh only runs `main` when it is executed, so a test can source it, point
every path at a temporary directory, and call one step at a time. The steps that
need root - chown, and uv itself - are stubbed; what is under test here is which
files end up in the install directory, which is where a promise in the README is
either kept or quietly broken.
"""

from __future__ import annotations

import os
import shlex
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
    shutil.copy(REPO_ROOT / "config" / "config.example.toml", src / "config")
    return src


# --- where the source comes from ------------------------------------------------------


def fetch_step(tmp_path, source, *, stubs="", **overrides):
    """Run `fetch_source` with these stand-ins in place of curl and git.

    The tree it fetches lives in a temporary directory the step deletes on its way
    out, so the step is what looks inside: where it ended up and what it holds is
    printed for the test to read.
    """
    overrides.setdefault("HOME", str(tmp_path / "keyless-home"))
    step = (
        f"{stubs}\n"
        "fetch_source\n"
        'echo "src=[${SRC_DIR}]"\n'
        'find "$SRC_DIR" -mindepth 1 -maxdepth 1 -printf "holds=[%f]\\n"'
    )
    return run_step(step, tmp_path, source, **overrides)


def call_log(tmp_path):
    """What the stubbed curl and git were handed."""
    path = tmp_path / "fetch-calls"
    return path, path.read_text() if path.exists() else ""


def stub_curl(tmp_path, *, tarball=None):
    """A curl that fails like a 404 does, unless it is given a tarball to hand over."""
    log, _ = call_log(tmp_path)
    hands_over = f'cp "{tarball}" "${{TMPDIR_CLEANUP}}/src.tar.gz"' if tarball else "return 22"
    return f'curl() {{ echo "curl $*" >> "{log}"; {hands_over}; }}'


def stub_git(tmp_path, source, *, fails=False):
    """A git that says what it was asked, and archives the source tree for real."""
    log, _ = call_log(tmp_path)
    return textwrap.dedent(f"""
        git() {{
            echo "git $*" >> "{log}"
            case "$*" in
                *fetch*) {"return 128" if fails else ":"} ;;
                *archive*) tar -c -C "{source}" . ;;
            esac
        }}
    """)


def tarball_of(tmp_path, source):
    """The source tree packed the way codeload packs it: one directory at the top."""
    archive = tmp_path / "codeload.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(source.parent), source.name],
        check=True,
        timeout=60,
    )
    return archive


def test_a_download_that_cannot_get_in_says_what_the_ways_in_are(tmp_path, source):
    """A private repository 404s exactly like a missing ref; the error cannot guess."""
    result = fetch_step(tmp_path, source, stubs=stub_curl(tmp_path))

    assert result.returncode == 1
    assert "GITHUB_TOKEN" in result.stderr
    assert "--ssh" in result.stderr
    assert "--source" in result.stderr


def test_the_token_reaches_the_download_that_needs_it(tmp_path, source):
    """Without the header a private repository answers 404 and the install stops."""
    result = fetch_step(
        tmp_path,
        source,
        stubs=stub_curl(tmp_path, tarball=tarball_of(tmp_path, source)),
        GITHUB_TOKEN="s3cret",
    )

    assert result.returncode == 0, result.stderr
    _, calls = call_log(tmp_path)
    assert "Authorization: Bearer s3cret" in calls
    assert f"/download/{source.name}]" in result.stdout, "the tarball's own directory"
    assert "holds=[pyproject.toml]" in result.stdout


def test_a_download_without_a_token_sends_no_header(tmp_path, source):
    """An empty header is not a header; curl would send `Authorization:` and mean it."""
    result = fetch_step(
        tmp_path, source, stubs=stub_curl(tmp_path, tarball=tarball_of(tmp_path, source))
    )

    assert result.returncode == 0, result.stderr
    _, calls = call_log(tmp_path)
    assert "Authorization" not in calls


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ssh_installs_the_ref_and_not_the_repository_around_it(tmp_path, source):
    """--ssh is the way into a private repository from a host GitHub knows by key.

    The remote here is a local repository rather than github.com - what is under
    test is that the fetch takes one ref and the install gets a source tree, not
    a checkout with a .git directory in it.
    """
    origin = tmp_path / "origin"
    shutil.copytree(source, origin)
    for command in (
        ["git", "init", "-q", "-b", "main", "."],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "one"],
    ):
        subprocess.run(command, cwd=origin, check=True, timeout=60)

    result = fetch_step(tmp_path, source, FETCH_SSH="1", SSH_URL=str(origin), REF="main")

    assert result.returncode == 0, result.stderr
    assert "/ssh/garbage-collection-automation]" in result.stdout
    assert "holds=[pyproject.toml]" in result.stdout
    assert "holds=[.git]" not in result.stdout, "the container has no use for the history"


def test_ssh_asks_for_the_repository_the_key_opens(tmp_path, source):
    """REPO is a GitHub path; over ssh that is a different URL for the same thing."""
    result = fetch_step(
        tmp_path,
        source,
        stubs=stub_git(tmp_path, source),
        FETCH_SSH="1",
        REPO="tmemelink/garbage-collection-automation",
        SSH_URL="git@github.com:tmemelink/garbage-collection-automation.git",
        REF="v0.1.0",
    )

    assert result.returncode == 0, result.stderr
    _, calls = call_log(tmp_path)
    assert (
        "fetch -q --depth=1 git@github.com:tmemelink/garbage-collection-automation.git v0.1.0"
        in calls
    )


def test_an_ssh_fetch_that_fails_names_the_command_that_says_why(tmp_path, source):
    """BatchMode turns a key problem into a failure rather than a prompt nobody sees."""
    result = fetch_step(
        tmp_path,
        source,
        stubs=stub_git(tmp_path, source, fails=True),
        FETCH_SSH="1",
        SSH_URL="git@github.com:tmemelink/private.git",
    )

    assert result.returncode == 1
    assert "git ls-remote git@github.com:tmemelink/private.git" in result.stderr


def test_a_failed_download_falls_back_to_the_key_this_host_holds(tmp_path, source):
    """The one-liner does not carry --ssh; a host with a key should still install."""
    home = tmp_path / "home-with-key"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("not really a key\n")

    result = fetch_step(
        tmp_path,
        source,
        stubs=stub_curl(tmp_path) + stub_git(tmp_path, source),
        HOME=str(home),
    )

    assert result.returncode == 0, result.stderr
    assert "trying ssh" in result.stderr
    assert "/ssh/garbage-collection-automation]" in result.stdout


def test_without_a_key_nothing_is_attempted_over_ssh(tmp_path, source):
    """A public install with a mistyped ref should fail on the ref, not on ssh."""
    result = fetch_step(tmp_path, source, stubs=stub_curl(tmp_path) + stub_git(tmp_path, source))

    assert result.returncode == 1
    _, calls = call_log(tmp_path)
    assert "git -C" not in calls, "there was no key to try it with"


def test_git_is_carried_only_by_the_host_that_fetches_over_ssh(tmp_path, source):
    """Everything installed here outlives the install; the tarball route needs neither."""
    log, _ = call_log(tmp_path)
    result = run_step(
        f'apt-get() {{ :; }}\napt_install() {{ echo "apt $*" >> "{log}"; }}\ninstall_prereqs',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    _, calls = call_log(tmp_path)
    assert "curl" in calls and "cron" in calls
    assert "git" not in calls, "an install from a tarball never runs git"


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


def test_the_config_is_group_writable_so_the_page_can_save_it(tmp_path, source):
    """A save rewrites the file itself, so the interface costs one bit on one file."""
    result = run_step("install_config", tmp_path, source)
    assert result.returncode == 0, result.stderr

    assert owner_of(tmp_path, "config.toml") == "root"
    assert (tmp_path / "etc" / "config.toml").stat().st_mode & 0o777 == 0o660


def test_the_config_directory_stays_root_s_whoever_may_write_the_file(tmp_path, source):
    """The env file lives there; the service user creating things next to it is the risk."""
    for interface in ("1", "0"):
        result = run_step("install_config", tmp_path, source, INSTALL_WEB=interface)
        assert result.returncode == 0, result.stderr

        assert owner_of(tmp_path, str(tmp_path / "etc")) == "root"
        assert (tmp_path / "etc").stat().st_mode & 0o7777 == 0o750


def test_without_the_web_interface_nothing_but_root_may_write_the_config(tmp_path, source):
    """Nothing else writes it then, and the widening would buy nothing."""
    result = run_step("install_config", tmp_path, source, INSTALL_WEB="0")
    assert result.returncode == 0, result.stderr

    assert owner_of(tmp_path, "config.toml") == "root"
    assert (tmp_path / "etc" / "config.toml").stat().st_mode & 0o777 == 0o640


def test_an_upgrade_that_adds_the_page_hands_the_existing_config_over(tmp_path, source):
    """The contents are the admin's; who may write them is this install's call."""
    assert run_step("install_config", tmp_path, source, INSTALL_WEB="0").returncode == 0
    config = tmp_path / "etc" / "config.toml"
    config.write_text('[address]\npostcode = "1234AB"  # edited by hand\n')

    second = run_step("install_config", tmp_path, source)

    assert second.returncode == 0, second.stderr
    assert "edited by hand" in config.read_text(), "an upgrade must not rewrite it"
    assert config.stat().st_mode & 0o777 == 0o660


def test_an_upgrade_that_drops_the_page_takes_the_write_bit_back(tmp_path, source):
    """--no-web is the other direction of the same decision."""
    assert run_step("install_config", tmp_path, source).returncode == 0

    second = run_step("install_config", tmp_path, source, INSTALL_WEB="0")

    assert second.returncode == 0, second.stderr
    assert (tmp_path / "etc" / "config.toml").stat().st_mode & 0o777 == 0o640


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


def test_the_env_file_is_handed_back_to_root_on_every_install(tmp_path, source):
    """Re-running the installer is the repair for a file an interrupted one left."""
    assert run_step("install_config", tmp_path, source).returncode == 0
    env_file = tmp_path / "etc" / "env"
    env_file.chmod(0o666)

    assert run_step("install_config", tmp_path, source).returncode == 0

    assert owner_of(tmp_path, "env") == "root", "a kept file is still chowned back"
    assert env_file.stat().st_mode & 0o777 == 0o640


def test_a_config_file_root_cannot_write_is_reported_rather_than_hidden(tmp_path, source):
    """An editor that will not save is what storage squashing root looks like from here.

    None of the causes show up in the mode line, so the check opens the files
    the way an editor would and prints what the kernel says back.
    """
    assert run_step("install_config", tmp_path, source).returncode == 0
    config = tmp_path / "etc" / "config.toml"
    config.chmod(0o444)
    try:
        result = run_step("verify_config_is_editable", tmp_path, source)
    finally:
        config.chmod(0o600)

    assert result.returncode == 0, "the install is finished by then; this is a warning"
    assert "cannot write" in result.stderr
    assert "Permission denied" in result.stderr
    assert "-r--r--r--" in result.stderr, "the modes as they actually are, not as intended"


def test_config_files_that_can_be_written_are_passed_over_in_silence(tmp_path, source):
    """It is the last thing an install prints; a clean one has nothing to say here."""
    assert run_step("install_config", tmp_path, source).returncode == 0

    result = run_step("verify_config_is_editable", tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert "cannot write" not in result.stderr


# --- the two answers a first install cannot guess --------------------------------------


def test_the_answers_land_in_the_config_the_installer_writes(tmp_path, source):
    """The example file is the template, so the comments around them have to survive."""
    result = run_step(
        "install_config",
        tmp_path,
        source,
        POSTCODE="1234ab",
        HOUSE_NUMBER="56",
        ADDITION="A",
        API_KEY="the-app-key",
    )
    assert result.returncode == 0, result.stderr

    written = (tmp_path / "etc" / "config.toml").read_text()
    assert 'postcode = "1234AB"' in written, "a postcode is stored the way the app stores it"
    assert 'house_number = "56"' in written
    assert 'addition = "A"' in written
    assert 'api_key = "the-app-key"' in written
    assert "# Looked up at mijnafvalwijzer.nl" in written


def test_a_key_holding_a_character_sed_reads_lands_intact(tmp_path, source):
    """What an API key may hold is the API's business, not the substitution's."""
    result = run_step("install_config", tmp_path, source, API_KEY="a&b|c")
    assert result.returncode == 0, result.stderr

    assert 'api_key = "a&b|c"' in (tmp_path / "etc" / "config.toml").read_text()


def test_an_install_that_answered_nothing_writes_the_example_unchanged(tmp_path, source):
    """--no-prompt and a container with no terminal both end here; it must still be a config."""
    result = run_step("install_config", tmp_path, source)
    assert result.returncode == 0, result.stderr

    written = (tmp_path / "etc" / "config.toml").read_text()
    assert written == (source / "config" / "config.example.toml").read_text()


def test_the_answers_can_be_given_on_the_command_line(tmp_path, source):
    """`pct exec` has no terminal to ask on, so the flags are the whole answer there."""
    result = run_step(
        'parse_args --postcode "1234 ab" --house-number 56 --addition A --api-key k3y-.~\n'
        'echo "[${POSTCODE}][${HOUSE_NUMBER}][${ADDITION}][${API_KEY}][${ASK_CONFIG}]"\n'
        'parse_args --no-prompt; echo "declined=[${ASK_CONFIG}]"',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    assert "[1234ab][56][A][k3y-.~][1]" in result.stdout, "a pasted space is not part of a postcode"
    assert "declined=[0]" in result.stdout


@pytest.mark.parametrize(
    ("flag", "value", "complaint"),
    [
        ("--postcode", "12AB", "not a Dutch postcode"),
        ("--house-number", "56A", "digits only"),
        ("--addition", "a b/c", "letters and digits"),
        ("--api-key", 'say "what"', "config.toml can hold"),
    ],
)
def test_an_answer_the_application_would_reject_stops_the_install(
    tmp_path, source, flag, value, complaint
):
    """Caught here, or by the first run a week later - the flag is where it is cheap."""
    result = run_step(f"parse_args {flag} {shlex.quote(value)}", tmp_path, source)

    assert result.returncode != 0
    assert complaint in result.stderr


def test_an_upgrade_does_not_ask_for_answers_it_already_has(tmp_path, source):
    """The config file it keeps holds them, and asking again would look like it does not."""
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "config.toml").write_text('[address]\npostcode = "1234AB"\n')

    result = run_step(
        'have_terminal() { return 0; }\nask_field() { echo "asked for $2"; }\nask_config',
        tmp_path,
        source,
    )

    assert result.returncode == 0, result.stderr
    assert "asked for" not in result.stdout


def test_an_install_with_no_terminal_writes_the_example_rather_than_hanging(tmp_path, source):
    """pct exec, cloud-init and CI have no tty; the installer has to finish, not wait."""
    result = run_step(
        'ask_config; echo "answered=[${POSTCODE}]"',
        tmp_path,
        source,
        detached=True,
    )

    assert result.returncode == 0, result.stderr
    assert "answered=[]" in result.stdout


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


def schedule_step(tmp_path, source, step="install_schedule", **overrides):
    """Run the schedule step with the init system stood in for by recording stubs."""
    shutil.copytree(REPO_ROOT / "scheduling", source / "scheduling", dirs_exist_ok=True)
    cron_dir = tmp_path / "cron.d"
    cron_dir.mkdir(exist_ok=True)
    recorder = tmp_path / "init-calls"

    result = run_step(
        f'systemctl() {{ echo "systemctl $*" >> "{recorder}"; }}\n'
        f'service() {{ echo "service $*" >> "{recorder}"; }}\n'
        f"{step}",
        tmp_path,
        source,
        CRON_DIR=str(cron_dir),
        **overrides,
    )
    called = recorder.read_text() if recorder.exists() else ""
    return result, cron_dir / "garbage-collection-automation", called


def test_the_cron_entry_names_the_installed_wrapper(tmp_path, source):
    result, entry_file, _called = schedule_step(tmp_path, source)

    assert result.returncode == 0, result.stderr
    entry = entry_file.read_text()
    assert "@APP_USER@" not in entry and "@INSTALL_DIR@" not in entry
    assert f"{tmp_path / 'opt'}/bin/run-job.sh" in entry


def test_the_cron_daemon_is_never_restarted(tmp_path, source):
    """cron re-reads /etc/cron.d every minute, so the entry above is live on its own.

    Restarting it was the old behaviour and it is what could leave two daemons
    racing; this runs the real cron_is_running against this host's /proc.
    """
    result, entry_file, called = schedule_step(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert entry_file.exists()
    assert "restart" not in called
    # --now is a start in disguise, and on a systemd host it is the whole of it.
    assert "--now" not in called


def test_a_running_daemon_is_left_alone(tmp_path, source):
    """Starting a second cron next to the first is what prints, and then dies with,

    "cron: can't lock /var/run/crond.pid, otherpid may be 87" - so it is not done.
    """
    result, entry_file, called = schedule_step(
        tmp_path, source, step="cron_is_running() { return 0; }\ninstall_schedule"
    )

    assert result.returncode == 0, result.stderr
    assert entry_file.exists()
    assert "start" not in called and "--now" not in called


def test_a_daemon_that_is_not_running_is_started(tmp_path, source):
    """A container whose cron was never started would otherwise never run the job."""
    result, _entry_file, called = schedule_step(
        tmp_path,
        source,
        step="has_systemd() { return 0; }\ncron_is_running() { return 1; }\ninstall_schedule",
    )

    assert result.returncode == 0, result.stderr
    assert "systemctl enable cron" in called
    assert "systemctl start cron" in called


def test_without_systemd_the_daemon_is_started_rather_than_restarted(tmp_path, source):
    """`service cron restart` next to a daemon the init script cannot see is the same
    double start, so this path only ever starts a cron that is missing."""
    result, _entry_file, called = schedule_step(
        tmp_path,
        source,
        step="has_systemd() { return 1; }\ncron_is_running() { return 1; }\ninstall_schedule",
    )

    assert result.returncode == 0, result.stderr
    assert "service cron start" in called
    assert "restart" not in called


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
    home = tmp_path / "root"
    command = home / "garbage-collection" / "run-garbage-collection.sh"
    result = run_step(
        "install_home_command",
        tmp_path,
        source,
        HOME_BASE_DIR=str(home),
        HOME_CMD=str(command),
        **overrides,
    )
    return result, command


def test_the_by_hand_command_lands_in_a_folder_of_its_own(tmp_path, source):
    """`pct enter` lands in root's home; one folder there is what the user finds."""
    result, command = home_command(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert command.exists(), "the installer left nothing to run by hand"
    assert command.parent.name == "garbage-collection", "the folder is the whole point"
    assert os.access(command, os.X_OK)
    written = command.read_text()
    assert f"{tmp_path / 'opt'}/bin/run-job.sh" in written
    assert "--dry-run" in written, "the comment header is the only usage anyone will read"


def test_the_by_hand_command_says_how_to_type_its_own_path(tmp_path, source):
    """The header is read inside the container, where the folder is under ~."""
    _, command = home_command(tmp_path, source)

    written = command.read_text()
    assert "~/garbage-collection/run-garbage-collection.sh" in written
    assert f"#   {tmp_path}" not in written, "a path from the installing host means nothing there"


def test_an_upgrade_takes_away_the_loose_command_of_an_older_install(tmp_path, source):
    """Two copies and only one of them kept up to date is how the stale one gets run."""
    home = tmp_path / "root"
    home.mkdir(parents=True, exist_ok=True)
    loose = home / "run-garbage-collection.sh"
    loose.write_text("#!/bin/sh\necho 'the install before the folder'\n")

    result, command = home_command(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert command.exists()
    assert not loose.exists(), "the copy nothing rewrites is the copy that goes stale"


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


def home_web_command(tmp_path, source, **overrides):
    """Write the web interface's by-hand command into a home directory of this test's own."""
    home = tmp_path / "root"
    command = home / "garbage-collection" / "run-web-interface.sh"
    result = run_step(
        "install_home_web_command",
        tmp_path,
        source,
        HOME_BASE_DIR=str(home),
        HOME_WEB_CMD=str(command),
        **overrides,
    )
    return result, command


def stub_server(tmp_path):
    """A stand-in for the installed server, which says how it was called."""
    binary = tmp_path / "opt" / ".venv" / "bin" / "garbage-collection-automation-web"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!/bin/sh\necho "server $*"\necho "token=[${GCA_TODOIST_TOKEN:-unset}]"\n')
    binary.chmod(0o755)
    return binary


def stub_commands(tmp_path, **scripts):
    """An environment whose PATH finds these stand-ins before the host's own."""
    directory = tmp_path / "stub-bin"
    directory.mkdir(exist_ok=True)
    for name, body in scripts.items():
        (directory / name).write_text(body)
        (directory / name).chmod(0o755)
    return {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"}


#: A service that is not running, so the by-hand command may have the port.
NOTHING_SERVING = "#!/bin/sh\nexit 3\n"

#: runuser, saying what it was handed before becoming the thing it was handed.
#: The three it drops are `-u`, the user and the `--` that ends its own options.
PASS_THROUGH = '#!/bin/sh\necho "runuser argv: $*"\nshift 3\nexec "$@"\n'


def test_the_web_interface_gets_a_by_hand_command_of_its_own(tmp_path, source):
    """The service survives a reboot; this is for watching the page answer."""
    result, command = home_web_command(tmp_path, source)

    assert result.returncode == 0, result.stderr
    assert command.exists(), "the interface can only be started through systemd otherwise"
    assert command.parent.name == "garbage-collection", "both commands, one folder"
    assert os.access(command, os.X_OK)
    written = command.read_text()
    assert "~/garbage-collection/run-web-interface.sh" in written
    assert "ctrl-c" in written, "a foreground server is only useful if it says how to stop"


def test_the_web_command_serves_as_the_service_user_with_the_unit_s_paths(tmp_path, source):
    """Config, state and the page: the same three files the service reads and writes."""
    _, command = home_web_command(tmp_path, source)
    stub_server(tmp_path)

    result = subprocess.run(
        [str(command)],
        capture_output=True,
        text=True,
        env=stub_commands(tmp_path, runuser=PASS_THROUGH, systemctl=NOTHING_SERVING),
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "runuser argv: -u nobody" in result.stdout
    assert f"server --config {tmp_path / 'etc' / 'config.toml'}" in result.stdout
    assert f"--state {tmp_path / 'var-lib' / 'state.json'}" in result.stdout
    assert f"--ui-dir {tmp_path / 'opt' / 'ui'}" in result.stdout


def test_the_web_command_passes_what_it_is_given_to_the_server(tmp_path, source):
    """`--version`, `--help` and the rest are the server's arguments, not this wrapper's."""
    _, command = home_web_command(tmp_path, source)
    stub_server(tmp_path)

    result = subprocess.run(
        [str(command), "--version"],
        capture_output=True,
        text=True,
        env=stub_commands(tmp_path, runuser=PASS_THROUGH, systemctl=NOTHING_SERVING),
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    served = next(line for line in result.stdout.splitlines() if line.startswith("server "))
    assert served.endswith("--version"), "it goes to the server, after the paths the unit passes"


def test_the_web_command_hands_the_token_over_where_ps_cannot_read_it(tmp_path, source):
    """The page shows the secrets, so it needs them - but every argument is public."""
    _, command = home_web_command(tmp_path, source)
    stub_server(tmp_path)
    (tmp_path / "etc").mkdir(exist_ok=True)
    (tmp_path / "etc" / "env").write_text(
        "GCA_TODOIST_TOKEN=from-the-env-file\n"  # pragma: allowlist secret
    )

    result = subprocess.run(
        [str(command)],
        capture_output=True,
        text=True,
        env=stub_commands(tmp_path, runuser=PASS_THROUGH, systemctl=NOTHING_SERVING),
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "token=[from-the-env-file]" in result.stdout, "the buttons export nothing without it"
    handed_over = next(
        line for line in result.stdout.splitlines() if line.startswith("runuser argv:")
    )
    assert "from-the-env-file" not in handed_over, "an argument list is readable by everyone"


def test_the_web_command_stops_while_the_service_holds_the_port(tmp_path, source):
    """Two servers, one port: "already in use" is true and says nothing useful."""
    _, command = home_web_command(tmp_path, source)
    stub_server(tmp_path)

    result = subprocess.run(
        [str(command)],
        capture_output=True,
        text=True,
        env=stub_commands(tmp_path, runuser=PASS_THROUGH, systemctl="#!/bin/sh\nexit 0\n"),
        timeout=60,
    )

    assert result.returncode == 1
    assert "server " not in result.stdout, "starting it anyway is what this check prevents"
    assert "already serving" in result.stderr
    assert "systemctl stop garbage-collection-automation-web.service" in result.stderr


def test_declining_the_web_interface_takes_its_command_away(tmp_path, source):
    """--no-web removes the unit; the command in front of it cannot outlive that."""
    _, command = home_web_command(tmp_path, source)
    assert command.exists()

    result, _ = home_web_command(tmp_path, source, INSTALL_WEB="0")

    assert result.returncode == 0, result.stderr
    assert not command.exists(), "a command for an interface that is not installed"


def uninstall_step(tmp_path, source, *, home_cmd, home_base):
    """Run --uninstall with everything it removes pointed inside this test.

    APP_NAME is a name of this test's own so the two paths uninstall hardcodes -
    /etc/cron.d and /etc/logrotate.d - cannot name a file the host has, and the
    uv cache is redirected for the same reason.
    """
    step = textwrap.dedent("""
        require_root() { :; }
        systemctl() { :; }
        userdel() { :; }
        uninstall
    """)
    return run_step(
        step,
        tmp_path,
        source,
        APP_NAME=f"gca-under-test-{tmp_path.name}",
        SYSTEMD_DIR=str(tmp_path / "absent-systemd"),
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        HOME_BASE_DIR=str(home_base),
        HOME_CMD=str(home_cmd),
        HOME_WEB_CMD=str(home_cmd.parent / "run-web-interface.sh"),
    )


def test_uninstalling_takes_the_folder_with_the_command(tmp_path, source):
    """The folder is this installer's doing, so an --uninstall should not leave it behind."""
    _, command = home_command(tmp_path, source)

    result = uninstall_step(tmp_path, source, home_cmd=command, home_base=tmp_path / "root")

    assert result.returncode == 0, result.stderr
    assert not command.exists()
    assert not command.parent.exists(), "an empty folder is still something to clean up by hand"


def test_uninstalling_takes_the_web_command_with_it(tmp_path, source):
    """Both commands are this installer's doing, and both stop working without it."""
    _, command = home_command(tmp_path, source)
    _, web_command = home_web_command(tmp_path, source)

    result = uninstall_step(tmp_path, source, home_cmd=command, home_base=tmp_path / "root")

    assert result.returncode == 0, result.stderr
    assert not web_command.exists()
    assert not command.parent.exists(), "the folder held nothing else"


def test_uninstalling_leaves_a_folder_that_is_not_empty_alone(tmp_path, source):
    """Whatever else is in there is someone's own; only the command was ours."""
    _, command = home_command(tmp_path, source)
    kept = command.parent / "notes.txt"
    kept.write_text("mine\n")

    result = uninstall_step(tmp_path, source, home_cmd=command, home_base=tmp_path / "root")

    assert result.returncode == 0, result.stderr
    assert not command.exists()
    assert kept.read_text() == "mine\n"


def test_uninstalling_never_removes_the_home_directory_itself(tmp_path, source):
    """HOME_CMD can point straight into a home; removing that is not on the menu."""
    home = tmp_path / "root"
    home.mkdir(parents=True, exist_ok=True)
    command = home / "run-garbage-collection.sh"
    command.write_text("#!/bin/sh\nexit 0\n")

    result = uninstall_step(tmp_path, source, home_cmd=command, home_base=home)

    assert result.returncode == 0, result.stderr
    assert not command.exists()
    assert home.is_dir(), "the home directory was never the installer's to remove"


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
