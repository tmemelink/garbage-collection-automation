"""What the page asks for: the payloads, the run lock, and what a save may write.

The HTTP around this is tests/test_web.py; nothing here opens a socket. Nothing
here reaches the network either - the source is stubbed and Todoist is the same
in-memory fake the reconciliation tests use.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from garbage_collection_automation import api, configuration, data_collection, data_processing
from garbage_collection_automation.configuration import TodoistExportConfig

from .conftest import make_config
from .test_application import SCHEDULE
from .test_reconciliation import (
    GFT,
    RESTAFVAL,
    TODAY,
    FakeTodoist,
    config,
    record,
    remote,
    write_state,
)

MINIMAL_CONFIG = """
[address]
postcode = "1234AB"
house_number = "21"

[collection]
lookahead_days = 30
types = ["restafval", "gft"]

[export.todoist]
enabled = false
project = "Home"
"""


@pytest.fixture(autouse=True)
def source(monkeypatch):
    """Every test here runs on TODAY, against the stubbed schedule, off the network."""
    monkeypatch.setattr(data_processing, "today", lambda: TODAY)
    monkeypatch.setattr(data_collection, "collect", lambda config: SCHEDULE)


@pytest.fixture
def paths(tmp_path):
    """A config, a place for the state file, and no crontab - which is a checkout."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG)
    return api.Paths(
        config=config_path,
        state=tmp_path / "state.json",
        cron=tmp_path / "no-such-crontab",
    )


# --- what the page draws itself from --------------------------------------------------


def test_the_state_payload_carries_the_configuration_the_form_shows(paths):
    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["config"]["postcode"] == "1234AB"
    assert payload["config"]["house_number"] == "21"
    assert payload["config"]["types"] == ["restafval", "gft"]
    assert payload["config"]["config_path"] == str(paths.config)
    assert payload["config"]["writable"] is True


def test_every_setting_the_file_has_is_on_the_page(paths):
    """The page is there to show what the headless run is configured with.

    A key the form has no name for is one nobody sees without an ssh session, so
    what the renderer writes and what the payload carries are checked against
    each other rather than kept in step by hand.
    """
    api.save_config({}, paths)  # render() writes every section, defaults included
    written = configuration.read_toml(paths.config)
    shown = api.state_payload(configuration.load(paths.config), paths)["config"]

    missing = [
        f"[{section}] {key}"
        for section, keys in _flatten(written).items()
        for key in keys
        if not any(name in shown for name in _payload_names(section, key))
    ]
    assert not missing, f"config.toml keys the page never draws: {', '.join(missing)}"


def _flatten(document: dict, prefix: str = "") -> dict[str, list[str]]:
    """``{"collection": ["api_key", ...], "export.todoist": [...]}`` out of a document."""
    sections: dict[str, list[str]] = {}
    for key, value in document.items():
        if isinstance(value, dict):
            sections |= _flatten(value, f"{prefix}{key}.")
        else:
            sections.setdefault(prefix.rstrip("."), []).append(key)
    return sections


def _payload_names(section: str, key: str) -> tuple[str, ...]:
    """What the form could plausibly be calling ``[section] key``."""
    return (key, f"{section}_{key}", f"{section.split('.')[-1]}_{key}")


def test_the_form_may_write_every_setting_it_is_shown(paths):
    """Showing a key read-only that the file lets you change is a page to ssh past."""
    api.save_config({}, paths)
    written = configuration.read_toml(paths.config)

    unwritable = [
        f"[{section}] {key}"
        for section, keys in _flatten(written).items()
        for key in keys
        if not set(_payload_names(section, key)) & api.FORM_FIELDS
    ]
    assert not unwritable, f"config.toml keys the form cannot save: {', '.join(unwritable)}"


def test_the_settings_the_form_never_used_to_reach_are_reachable_now(paths):
    payload = api.state_payload(configuration.load(paths.config), paths)["config"]

    assert payload["timeout_seconds"] == 15
    assert payload["retries"] == 1
    assert payload["web_enabled"] is False
    assert payload["web_host"] == "127.0.0.1"
    assert payload["web_port"] == 8080
    assert payload["logging_level"] == "INFO"
    assert payload["known_levels"] == list(configuration.LOG_LEVELS)


# --- config.toml, as the panel shows it -----------------------------------------------


def test_the_file_is_shown_as_it_is_on_disk(paths):
    """Comments and all: the panel is the file, not a second rendering of it."""
    paths.config.write_text(MINIMAL_CONFIG + "\n# a note somebody left here\n")

    shown = api.state_payload(configuration.load(paths.config), paths)["config_file"]

    assert shown["text"] == paths.config.read_text()
    assert "# a note somebody left here" in shown["text"]
    assert shown["path"] == str(paths.config)
    assert shown["error"] is None
    assert shown["masked"] is False, "there is no secret in this one to mask"
    assert shown["writable"] is True


def test_neither_secret_in_the_file_is_in_the_panel(paths):
    paths.config.write_text(
        MINIMAL_CONFIG.replace(
            "[export.todoist]", '[export.todoist]\ntoken = "todoist-secret-value"'
        ).replace("[collection]", '[collection]\napi_key = "afvalwijzer-secret-value"')
    )

    shown = api.state_payload(configuration.load(paths.config), paths)["config_file"]

    assert "todoist-secret-value" not in shown["text"]
    assert "afvalwijzer-secret-value" not in shown["text"]
    assert shown["masked"] is True
    # Enough of the tail to tell the key you meant from some other key.
    assert "\u2022" * api.MASK_WIDTH + "alue" in shown["text"]


def test_a_secret_written_twice_is_masked_both_times(paths):
    """Why the value is looked for rather than the key: a file is not a schema."""
    paths.config.write_text(
        MINIMAL_CONFIG.replace(
            "[collection]", '[collection]\napi_key = "shared-secret"\n# also used as: shared-secret'
        )
    )

    shown = api.state_payload(configuration.load(paths.config), paths)["config_file"]

    assert "shared-secret" not in shown["text"]


def test_a_short_secret_keeps_none_of_itself(paths):
    """A tail of a short value is a real part of it, which is not a mask."""
    assert api._mask("abcdefgh") == "\u2022" * api.MASK_WIDTH
    assert api._mask("a-secret-long-enough-to-recognise").endswith("nise")


def test_the_mask_does_not_say_how_long_the_secret_is(paths):
    short, long = api._mask("0123456789ab"), api._mask("0123456789ab" * 8)

    assert len(short) == len(long)


def test_a_file_too_broken_to_parse_is_not_shown_at_all(paths):
    """Nothing can say which of an unparsable file is a secret, so none of it goes out."""
    paths.config.write_text('[collection]\napi_key = "still-a-secret"\nthis is not toml\n')

    shown = api._config_file_payload(paths)

    assert shown["text"] is None
    assert "still-a-secret" not in str(shown)
    assert "not valid TOML" in shown["error"]


def test_a_file_that_cannot_be_read_leaves_the_rest_of_the_page_standing(paths):
    shown = api._config_file_payload(api.Paths(config=paths.config.parent, state=paths.state))

    assert shown["text"] is None
    assert shown["error"].startswith("cannot read ")


def test_a_file_nobody_should_have_to_scroll_is_cut_short(paths, monkeypatch):
    monkeypatch.setattr(api, "MAX_CONFIG_BYTES", 120)

    shown = api._config_file_payload(paths)

    assert shown["truncated"] is True
    assert shown["text"].endswith("# ... the rest is not shown\n")


def test_the_panel_says_when_the_file_was_last_written(paths):
    saved = api.save_config({"lookahead_days": 45}, paths)

    assert saved["config_file"]["modified_at"] is not None
    assert "lookahead_days = 45" in saved["config_file"]["text"], (
        "a save answers with the file it just wrote, not the one the page loaded"
    )


def test_the_page_is_told_every_waste_type_it_could_offer(paths):
    """The switches are built from this; a code the file does not use still needs one."""
    payload = api.state_payload(configuration.load(paths.config), paths)

    codes = [item["code"] for item in payload["config"]["known_types"]]
    assert codes == list(configuration.WASTE_TYPES)


def test_a_read_only_config_file_is_reported_as_such(paths):
    """The installer decides this; the page grays the save button out rather than lying."""
    paths.config.chmod(0o444)

    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["config"]["writable"] is False


def test_a_config_that_is_not_there_yet_is_still_a_config_a_save_can_write(tmp_path):
    """save() creates one rather than refusing, so the page must not gray Save out."""
    assert api._writable(tmp_path / "not-created-yet.toml") is True


def test_the_page_is_told_when_the_environment_owns_the_token(paths, monkeypatch):
    monkeypatch.setenv(configuration.TOKEN_ENV_VAR, "from-the-env")

    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["config"]["token_from_environment"] is True
    assert payload["config"]["todoist_token"] == "from-the-env"


def test_the_page_is_told_when_the_environment_owns_the_api_key(paths, monkeypatch):
    monkeypatch.setenv(configuration.API_KEY_ENV_VAR, "key-from-the-env")

    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["config"]["api_key_from_environment"] is True
    assert payload["config"]["api_key"] == "key-from-the-env"


def test_the_api_key_field_is_empty_and_the_page_may_write_it(paths):
    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["config"]["api_key"] == ""
    assert payload["config"]["api_key_from_environment"] is False


def test_the_last_export_is_shown_before_any_button_is_pressed(paths):
    write_state(paths.state, record(RESTAFVAL, "id-1"), record(GFT, "id-2"))

    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["last_run"]["known"] is True
    assert payload["last_run"]["complete"] is True
    assert [task["task_id"] for task in payload["last_run"]["tasks"]] == ["id-1", "id-2"]
    assert payload["last_run"]["tasks"][0]["waste_type"] == "Restafval"


def test_an_export_that_stopped_halfway_is_reported_as_such(paths):
    """The page has to be able to say the record is not the whole story."""
    write_state(paths.state, record(RESTAFVAL, "id-1"), complete=False)

    payload = api.state_payload(configuration.load(paths.config), paths)

    assert payload["last_run"]["known"] is True
    assert payload["last_run"]["complete"] is False


def test_a_checkout_has_no_crontab_and_the_page_says_so(paths):
    assert api.state_payload(configuration.load(paths.config), paths)["schedule"] == {
        "cron": None,
        "path": str(paths.cron),
    }


def test_the_schedule_shown_is_the_one_cron_will_use(paths, tmp_path):
    """Five fields out of the installed crontab - and never the settings above it."""
    (tmp_path / "crontab").write_text(
        '# a comment\nSHELL=/bin/bash\nMAILTO=""\n\n0 4 * * 6  gca  /opt/x/bin/run-job.sh\n'
    )

    line = api._cron_line(tmp_path / "crontab")

    assert line == "0 4 * * 6"


# --- the three buttons ----------------------------------------------------------------


def test_collect_reports_the_schedule_and_writes_nothing(paths):
    payload = api.collect(configuration.load(paths.config), paths)

    assert payload["result"]["ok"] is True
    assert payload["result"]["dry_run"] is True
    assert [row["code"] for row in payload["result"]["rows"]] == ["restafval", "gft"]
    assert not paths.state.exists()


def test_a_row_carries_everything_its_column_needs(paths):
    payload = api.collect(configuration.load(paths.config), paths)

    row = payload["result"]["rows"][0]
    assert row["date"] == "2026-08-20"
    assert row["weekday"] == "Thursday"
    assert row["waste_type"] == "Restafval"
    # The offset and not just the clock: the page hands these to the browser's
    # own Date, which reads one without an offset in the reader's zone instead.
    assert row["due_at"] == "2026-08-20T07:00:00+02:00"
    assert row["remind_at"] == "2026-08-19T07:00:00+02:00"
    assert row["state"] == "pending"
    assert row["task_id"] is None


def test_a_recorded_todo_shows_as_synced_with_its_id(paths):
    write_state(paths.state, record(RESTAFVAL, "id-1"))

    payload = api.collect(configuration.load(paths.config), paths)

    rows = {row["code"]: row for row in payload["result"]["rows"]}
    assert rows["restafval"]["state"] == "synced"
    assert rows["restafval"]["task_id"] == "id-1"
    assert rows["gft"]["state"] == "pending"


def test_check_shows_the_delta_todoist_would_need(paths, monkeypatch):
    # A record written for today's todo format: without one a check plans to
    # rewrite everything it finds, which is right but is another test.
    write_state(paths.state, record(RESTAFVAL, "id-1"))
    client = FakeTodoist(remote(RESTAFVAL, "id-1"))
    monkeypatch.setattr(api.application, "check", _with_client(client))

    payload = api.check(config(), paths)

    assert payload["result"]["queried"] is True
    assert [item["code"] for item in payload["result"]["delta"]["create"]] == ["gft"]
    assert payload["result"]["delta"]["update"] == []
    assert payload["result"]["delta"]["delete"] == []


def test_a_todo_with_no_collection_left_gets_a_row_of_its_own(paths, monkeypatch):
    """The thing a person opens this page to see: what is about to be removed."""
    stale = remote(GFT, "id-2")
    client = FakeTodoist(remote(RESTAFVAL, "id-1"), stale)
    monkeypatch.setattr(api.application, "check", _with_client(client))
    narrow = make_config(
        types=("restafval",),
        todoist=TodoistExportConfig(enabled=True, token="secret"),
    )

    payload = api.check(narrow, paths)

    rows = payload["result"]["rows"]
    removals = [row for row in rows if row["state"] == "remove"]
    assert [row["task_id"] for row in removals] == ["id-2"]
    assert removals[0]["remind_at"] is None, "a todo being removed has no reminder to show"
    # One row in another timezone than the rest of its own table is worse than none.
    assert removals[0]["due_at"] == "2026-08-27T07:00:00+02:00"
    assert {row["due_at"][-6:] for row in rows} == {"+02:00"}


def test_apply_writes_the_todos_and_records_them(paths, monkeypatch):
    client = FakeTodoist()
    monkeypatch.setattr(api.application, "run", _with_client(client, dry_run=False))

    payload = api.apply(config(), paths)

    assert payload["result"]["ok"] is True
    assert client.created == [RESTAFVAL, GFT]
    assert paths.state.exists()


def test_an_applied_run_shows_the_work_as_done_rather_than_promised(paths, monkeypatch):
    """The delta is what the run did; a table still saying "to add" would be lying."""
    write_state(paths.state, record(GFT, "id-2"))
    client = FakeTodoist(remote(GFT, "id-2"))
    monkeypatch.setattr(api.application, "run", _with_client(client, dry_run=False))
    narrow = make_config(
        types=("restafval",),
        todoist=TodoistExportConfig(enabled=True, token="secret"),
    )

    payload = api.apply(narrow, paths)

    assert payload["result"]["dry_run"] is False
    assert [item["code"] for item in payload["result"]["delta"]["create"]] == ["restafval"]
    assert client.deleted == ["id-2"]

    rows = payload["result"]["rows"]
    assert [(row["code"], row["state"]) for row in rows] == [("restafval", "synced")], (
        "the todo was written a moment ago, and the one that was deleted is gone"
    )
    assert rows[0]["task_id"] == "id-1", "and it carries the id it was just created with"


def test_the_rows_are_in_the_order_the_table_shows_them(paths, monkeypatch):
    client = FakeTodoist(remote(GFT, "id-2"))
    monkeypatch.setattr(api.application, "check", _with_client(client))
    narrow = make_config(
        types=("restafval",),
        todoist=TodoistExportConfig(enabled=True, token="secret"),
    )

    rows = api.check(narrow, paths)["result"]["rows"]

    assert [row["date"] for row in rows] == sorted(row["date"] for row in rows)


def _with_client(client, *, dry_run=None):
    """The real action with the fake Todoist wired in, since api.* builds its own."""
    from garbage_collection_automation import application

    real = application.run if dry_run is not None else application.check

    def action(config, *, state_path, **kwargs):
        kwargs.pop("client", None)
        if dry_run is not None:
            kwargs["dry_run"] = dry_run
        return real(config, state_path=state_path, client=client, today=TODAY, **kwargs)

    return action


# --- what the console shows -----------------------------------------------------------


def test_an_action_hands_back_what_the_run_said(paths):
    payload = api.collect(configuration.load(paths.config), paths)

    messages = [line["message"] for line in payload["log"]]
    assert "would export 2026-08-20 Restafval" in messages
    assert payload["result"]["summary"] in messages, "the headline is a line of the log too"
    assert {line["level"] for line in payload["log"]} <= {"DEBUG", "INFO", "WARNING", "ERROR"}


def test_the_console_is_not_a_place_the_rest_of_the_process_leaks_into(paths):
    """Two runs at once must not each show the other's lines; the filter is the thread."""
    import logging

    def chatter():
        for _ in range(20):
            logging.getLogger("garbage_collection_automation.elsewhere").info("not this run")

    noise = threading.Thread(target=chatter)
    noise.start()
    payload = api.collect(configuration.load(paths.config), paths)
    noise.join()

    assert not [line for line in payload["log"] if line["message"] == "not this run"]


def test_a_pathological_run_cannot_fill_the_response(paths, monkeypatch):
    import logging

    monkeypatch.setattr(api, "MAX_LOG_LINES", 5)

    real = api.application.run

    def shout(config, **kwargs):
        for index in range(50):
            logging.getLogger("garbage_collection_automation.loud").info("line %d", index)
        return real(config, **kwargs)

    monkeypatch.setattr(api.application, "run", shout)

    assert len(api.collect(configuration.load(paths.config), paths)["log"]) == 5


# --- the run lock ---------------------------------------------------------------------


def test_the_lock_is_the_one_the_cron_wrapper_takes(paths):
    """run-job.sh flocks <state dir>/run.lock; sharing it is what excludes the cron run."""
    assert paths.lock == paths.state.parent / "run.lock"


def test_a_run_held_by_another_process_is_reported_not_waited_for(paths):
    """A button that hangs until the weekly run finishes is a broken button."""
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,time\n"
            "h=open(sys.argv[1],'w')\n"
            "fcntl.flock(h, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.readline()\n",
            str(paths.lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"

        with pytest.raises(api.Busy, match="scheduled run"):
            api.collect(configuration.load(paths.config), paths)
    finally:
        holder.stdin.write("\n")
        holder.stdin.close()
        holder.wait(timeout=10)
        holder.stdout.close()


def test_two_requests_to_this_process_do_not_both_get_in(paths):
    inside = threading.Event()
    release = threading.Event()

    def slow(config, **kwargs):
        inside.set()
        release.wait(timeout=10)
        return api.application.check(config, state_path=kwargs["state_path"], today=TODAY)

    first: list = []

    def run_first():
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(api.application, "run", slow)
            try:
                api.collect(configuration.load(paths.config), paths)
            except Exception as exc:  # pragma: no cover - only on a failed run
                first.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert inside.wait(timeout=10), "the first action never started"

        with pytest.raises(api.Busy, match="another action"):
            api.collect(configuration.load(paths.config), paths)
    finally:
        release.set()
        thread.join(timeout=10)
    assert not first


def test_a_stop_can_wait_for_the_run_that_is_already_underway(paths):
    """What web.serve() does on SIGTERM: a process that goes away between the
    to-dos and the state file that records them leaves work nothing knows about."""
    inside = threading.Event()
    release = threading.Event()

    def hold():
        with api._locked(paths):
            inside.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert inside.wait(timeout=10), "the run never started"
        assert api.wait_for_the_pipeline(0.1) is False, "it must not claim an idle pipeline"
    finally:
        release.set()
        thread.join(timeout=10)

    assert api.wait_for_the_pipeline(10) is True


def test_the_lock_is_released_when_the_run_fails(paths):
    """A run that raises must not leave the page unable to try again."""

    def explode(config, **kwargs):
        raise RuntimeError("boom")

    # Its own context, not the test's monkeypatch: undoing that one would take
    # the autouse stub of the source with it and put this test on the network.
    with pytest.MonkeyPatch.context() as failing:
        failing.setattr(api.application, "run", explode)
        with pytest.raises(RuntimeError):
            api.collect(configuration.load(paths.config), paths)

    assert api.collect(configuration.load(paths.config), paths)["result"]["ok"]


# --- saving the form ------------------------------------------------------------------


def test_a_save_writes_the_fields_it_was_sent_and_leaves_the_others_alone(paths):
    """The form may reach every key; a save still only moves the ones it carried."""
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\nport = 9001\n")

    api.save_config(
        {"lookahead_days": 45, "todoist_project": "Huis", "todoist_section": "Terugkerend"}, paths
    )

    after = configuration.load(paths.config)
    assert after.collection.lookahead_days == 45
    assert after.export.todoist.project == "Huis"
    assert after.export.todoist.section == "Terugkerend"
    assert after.web.enabled is True, "not sent, so not changed"
    assert after.web.port == 9001


def test_the_sections_the_form_never_used_to_have_are_saved_too(paths):
    api.save_config(
        {
            "timeout_seconds": 30,
            "retries": 3,
            "web_enabled": True,
            "web_host": "::1",
            "web_port": 9100,
            "logging_level": "WARNING",
        },
        paths,
    )

    after = configuration.load(paths.config)
    assert (after.collection.timeout_seconds, after.collection.retries) == (30, 3)
    assert (after.web.enabled, after.web.host, after.web.port) == (True, "::1", 9100)
    assert after.logging.level == "WARNING"


def test_an_address_the_page_must_not_be_served_on_is_refused_here_too(paths):
    """[web] host is the form's to change, which is exactly why it is validated."""
    with pytest.raises(api.ApiError, match="not a loopback address"):
        api.save_config({"web_host": "0.0.0.0"}, paths)


def test_a_value_the_file_would_refuse_is_refused_in_the_same_words(paths):
    before = paths.config.read_text()

    with pytest.raises(api.ApiError, match="not a Dutch postcode"):
        api.save_config({"postcode": "nope"}, paths)

    assert paths.config.read_text() == before, "nothing is written until all of it passes"


def test_a_field_the_form_does_not_have_is_refused(paths):
    """The cron line is on the page and is not the service user's to write."""
    with pytest.raises(api.ApiError, match="unknown field"):
        api.save_config({"cron": "0 4 * * 6"}, paths)


def test_the_token_is_saved_when_the_file_is_where_it_lives(paths):
    api.save_config({"todoist_token": "  s3cret  "}, paths)

    assert configuration.load(paths.config).export.todoist.token == "s3cret"


def test_the_token_in_the_environment_is_never_copied_into_the_file(paths, monkeypatch):
    """The variable wins everywhere else; writing it here would put a second copy on disk."""
    monkeypatch.setenv(configuration.TOKEN_ENV_VAR, "from-the-env")

    api.save_config({"todoist_token": "typed-into-the-page", "todoist_enabled": True}, paths)

    monkeypatch.delenv(configuration.TOKEN_ENV_VAR)
    assert 'token = ""' in paths.config.read_text()


def test_the_api_key_is_saved_when_the_file_is_where_it_lives(paths):
    api.save_config({"api_key": "  an-app-key  "}, paths)

    assert configuration.load(paths.config).collection.api_key == "an-app-key"


def test_the_api_key_in_the_environment_is_never_copied_into_the_file(paths, monkeypatch):
    """Same as the token: the variable wins, so a copy on disk is one that goes stale."""
    monkeypatch.setenv(configuration.API_KEY_ENV_VAR, "key-from-the-env")

    api.save_config({"api_key": "typed-into-the-page"}, paths)

    monkeypatch.delenv(configuration.API_KEY_ENV_VAR)
    assert 'api_key = ""' in paths.config.read_text()


def test_saving_the_api_key_leaves_the_todoist_token_alone(paths):
    """Two secrets in one form; a save of either must not blank the other."""
    api.save_config({"todoist_token": "s3cret"}, paths)

    api.save_config({"api_key": "an-app-key"}, paths)

    config = configuration.load(paths.config)
    assert config.export.todoist.token == "s3cret"
    assert config.collection.api_key == "an-app-key"


def test_enabling_the_export_without_a_token_is_refused(paths):
    with pytest.raises(api.ApiError, match="no token was given"):
        api.save_config({"todoist_enabled": True}, paths)


def test_a_save_answers_with_the_configuration_now_on_disk(paths):
    payload = api.save_config({"types": ["pmd", "glas"]}, paths)

    assert payload["saved"] is True
    assert payload["config"]["types"] == ["pd", "glas"], "normalised, as the file normalises it"


def test_a_file_that_cannot_be_written_is_reported_rather_than_half_saved(paths):
    paths.config.chmod(0o444)

    with pytest.raises(api.ApiError, match="cannot write"):
        api.save_config({"lookahead_days": 45}, paths)


# --- switching the page off -----------------------------------------------------------


def test_stopping_switches_the_page_off_in_the_file(paths):
    """The kill is web.py's half; this half is what keeps it off after a reboot."""
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\nport = 9001\n")

    payload = api.stop_web(paths)

    assert payload["stopping"] is True
    assert payload["config"]["web_enabled"] is False
    assert configuration.load(paths.config).web.enabled is False


def test_stopping_changes_nothing_else_about_the_configuration(paths):
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\nport = 9001\n")

    api.stop_web(paths)

    after = configuration.load(paths.config)
    assert after.web.port == 9001, "the address it was served on is still the file's"
    assert after.address.postcode == "1234AB"
    assert after.collection.lookahead_days == 30


def test_a_stop_that_cannot_be_written_down_is_refused(paths):
    """Killing the server without the file would bring the page back at the next boot."""
    paths.config.write_text(MINIMAL_CONFIG + "\n[web]\nenabled = true\n")
    paths.config.chmod(0o444)

    with pytest.raises(api.ApiError, match="cannot write"):
        api.stop_web(paths)

    assert configuration.load(paths.config).web.enabled is True


def test_a_save_and_then_a_run_use_the_same_configuration(paths):
    """The point of reloading per action: the button acts on what was just saved."""
    api.save_config({"types": ["restafval"]}, paths)

    payload = api.collect(configuration.load(paths.config), paths)

    assert [row["code"] for row in payload["result"]["rows"]] == ["restafval"]


def test_the_console_fills_even_when_the_journal_is_configured_quiet(paths, caplog):
    """The page's console is why those lines exist; a quiet [logging] must not empty it."""
    import logging

    logging.getLogger("garbage_collection_automation").setLevel(logging.ERROR)
    try:
        payload = api.collect(configuration.load(paths.config), paths)
    finally:
        logging.getLogger("garbage_collection_automation").setLevel(logging.NOTSET)

    assert payload["log"], "the console would have been empty"
    assert payload["result"]["summary"] in [line["message"] for line in payload["log"]]


def test_the_level_it_lowered_is_the_one_it_puts_back(paths):
    import logging

    logger = logging.getLogger("garbage_collection_automation")
    logger.setLevel(logging.ERROR)
    try:
        api.collect(configuration.load(paths.config), paths)
        assert logger.level == logging.ERROR
    finally:
        logger.setLevel(logging.NOTSET)
