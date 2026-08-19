"""Tests for reading and validating config.toml."""

from __future__ import annotations

import tomllib
from datetime import time

import pytest

from garbage_collection_automation import configuration
from garbage_collection_automation.configuration import ConfigError

from .conftest import MINIMAL, REPO_ROOT


def test_example_config_is_valid():
    """The shipped template is what a fresh install runs with; keep it loadable."""
    config = configuration.load(REPO_ROOT / "config" / "config.example.toml")

    assert config.address.postcode == "1234AB"
    assert config.address.house_number == "56"
    assert config.collection.lookahead_days == 30
    assert config.collection.due_time == time(7, 0)
    assert config.collection.types == ("restafval", "papier", "gft")
    assert config.export.todoist.enabled is False, "on by default, but the template has no token"
    assert config.export.todoist.project == "Home"
    assert config.logging.level == "INFO"


def test_defaults_apply_when_only_the_address_is_given(write_config):
    config = configuration.load(write_config(MINIMAL))

    assert config.collection == configuration.CollectionConfig()
    assert config.export.todoist == configuration.TodoistExportConfig(enabled=False), (
        "on by default, but this file carries no token"
    )
    assert config.logging == configuration.LoggingConfig()


@pytest.mark.parametrize(
    ("written", "expected"),
    [("1234 ab", "1234AB"), (" 1234ab ", "1234AB"), ("1234AB", "1234AB")],
)
def test_postcode_is_normalised(write_config, written, expected):
    path = write_config(f'[address]\npostcode = "{written}"\nhouse_number = 56\n')

    assert configuration.load(path).address.postcode == expected


def test_house_number_may_be_a_toml_integer(write_config):
    path = write_config('[address]\npostcode = "1234AB"\nhouse_number = 56\n')

    assert configuration.load(path).address.house_number == "56"


@pytest.mark.parametrize(
    "body",
    [
        "[address]\nhouse_number = 56\n",  # no postcode
        '[address]\npostcode = "1234AB"\n',  # no house number
        '[address]\npostcode = "nope"\nhouse_number = 56\n',  # not a postcode
        '[address]\npostcode = "1234AB"\nhouse_number = "56a"\n',  # letter, not addition
        "[collection]\nlookahead_days = 30\n",  # no [address] at all
    ],
)
def test_invalid_address_is_rejected(write_config, body):
    with pytest.raises(ConfigError):
        configuration.load(write_config(body))


def test_unknown_keys_are_rejected(write_config):
    """Typos and leftovers (e.g. the removed calendar export) must not pass silently."""
    path = write_config(MINIMAL + '\n[export.calendar]\npath = "/tmp/x.ics"\n')

    with pytest.raises(ConfigError, match="unknown key"):
        configuration.load(path)


def test_unknown_key_inside_a_section_is_rejected(write_config):
    path = write_config(MINIMAL + "\n[collection]\nlookahead_dayz = 10\n")

    with pytest.raises(ConfigError, match="lookahead_dayz"):
        configuration.load(path)


@pytest.mark.parametrize("days", [0, -1, 400])
def test_lookahead_days_out_of_range_is_rejected(write_config, days):
    with pytest.raises(ConfigError, match="lookahead_days"):
        configuration.load(write_config(MINIMAL + f"\n[collection]\nlookahead_days = {days}\n"))


def test_log_level_is_upper_cased(write_config):
    path = write_config(MINIMAL + '\n[logging]\nlevel = "debug"\n')

    assert configuration.load(path).logging.level == "DEBUG"


def test_unknown_log_level_is_rejected(write_config):
    with pytest.raises(ConfigError, match="level"):
        configuration.load(write_config(MINIMAL + '\n[logging]\nlevel = "chatty"\n'))


def test_the_api_key_comes_from_the_file_when_the_environment_is_empty(write_config):
    path = write_config(MINIMAL + '\n[collection]\napi_key = "from-file"\n')

    assert configuration.load(path).collection.api_key == "from-file"


def test_the_environment_api_key_wins_over_the_file(write_config, monkeypatch):
    monkeypatch.setenv(configuration.API_KEY_ENV_VAR, "from-env")
    path = write_config(MINIMAL + '\n[collection]\napi_key = "from-file"\n')

    assert configuration.load(path).collection.api_key == "from-env"


def test_a_config_without_an_api_key_still_loads(write_config):
    """It is refused at the fetch, not here: the page that sets it has to load first."""
    assert configuration.load(write_config(MINIMAL)).collection.api_key == ""


def test_token_comes_from_the_file_when_the_environment_is_empty(write_config):
    path = write_config(MINIMAL + '\n[export.todoist]\nenabled = true\ntoken = "from-file"\n')

    assert configuration.load(path).export.todoist.token == "from-file"


def test_environment_token_wins_over_the_file(write_config, monkeypatch):
    monkeypatch.setenv(configuration.TOKEN_ENV_VAR, "from-env")
    path = write_config(MINIMAL + '\n[export.todoist]\ntoken = "from-file"\n')

    assert configuration.load(path).export.todoist.token == "from-env"


def test_an_enabled_export_without_a_token_is_rejected(write_config):
    """Every other key fails the run when it is wrong; a blank token has to as well."""
    path = write_config(MINIMAL + '\n[export.todoist]\nenabled = true\ntoken = "   "\n')

    with pytest.raises(ConfigError, match="no token"):
        configuration.load(path)


def test_the_environment_can_supply_the_token_an_enabled_export_needs(write_config, monkeypatch):
    monkeypatch.setenv(configuration.TOKEN_ENV_VAR, "from-env")
    path = write_config(MINIMAL + "\n[export.todoist]\nenabled = true\n")

    assert configuration.load(path).export.todoist.token == "from-env"


def test_a_disabled_export_needs_no_token(write_config):
    """A file that turns the export off says nothing about a token; it must load."""
    path = write_config(MINIMAL + "\n[export.todoist]\nenabled = false\n")

    assert configuration.load(path).export.todoist.token == ""


def test_the_export_is_on_when_a_token_is_all_that_is_given(write_config):
    """The default: a file that names a token and nothing else exports."""
    path = write_config(MINIMAL + '\n[export.todoist]\ntoken = "from-file"\n')

    assert configuration.load(path).export.todoist.enabled is True


def test_the_default_on_export_steps_aside_when_there_is_no_token(write_config, caplog):
    """Refusing the file would stop the collection too, over a target nobody set up."""
    with caplog.at_level("INFO"):
        config = configuration.load(write_config(MINIMAL))

    assert config.export.todoist.enabled is False
    assert "the export is skipped" in caplog.text


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        configuration.load(tmp_path / "absent.toml")


def test_broken_toml_raises_config_error(write_config):
    with pytest.raises(ConfigError, match="valid TOML"):
        configuration.load(write_config("[address\n"))


def test_wrong_type_raises_config_error(write_config):
    with pytest.raises(ConfigError, match="true or false"):
        configuration.load(write_config(MINIMAL + '\n[export.todoist]\nenabled = "yes"\n'))


def test_collection_defaults_cover_the_source_query(write_config):
    """The source publishes dates only, so the due time and the type filter live here."""
    collection = configuration.load(write_config(MINIMAL)).collection

    assert collection.due_time == time(7, 0)
    assert collection.types == ("restafval", "papier", "gft")
    assert collection.timeout_seconds == 15
    assert collection.retries == 1


@pytest.mark.parametrize(
    ("written", "expected"),
    [("07:00", time(7, 0)), ("6:45", time(6, 45)), ("19:30", time(19, 30))],
)
def test_due_time_is_parsed(write_config, written, expected):
    path = write_config(MINIMAL + f'\n[collection]\ndue_time = "{written}"\n')

    assert configuration.load(path).collection.due_time == expected


@pytest.mark.parametrize("written", ["7 uur", "25:00", "07:60", "0700", ""])
def test_invalid_due_time_is_rejected(write_config, written):
    with pytest.raises(ConfigError, match="due_time"):
        configuration.load(write_config(MINIMAL + f'\n[collection]\ndue_time = "{written}"\n'))


def test_types_are_normalised(write_config):
    path = write_config(MINIMAL + '\n[collection]\ntypes = ["GFT", " Papier "]\n')

    assert configuration.load(path).collection.types == ("gft", "papier")


def test_unknown_waste_type_is_rejected(write_config):
    """A typo here would silently drop a whole waste stream; name the valid codes."""
    path = write_config(MINIMAL + '\n[collection]\ntypes = ["restafval", "plastic"]\n')

    with pytest.raises(ConfigError, match="plastic"):
        configuration.load(path)


@pytest.mark.parametrize("written", ["[]", '"restafval"', "[1, 2]"])
def test_types_must_be_a_non_empty_list_of_strings(write_config, written):
    with pytest.raises(ConfigError, match="types"):
        configuration.load(write_config(MINIMAL + f"\n[collection]\ntypes = {written}\n"))


@pytest.mark.parametrize(
    ("key", "value"),
    [("timeout_seconds", 0), ("timeout_seconds", 120), ("retries", -1), ("retries", 9)],
)
def test_query_limits_out_of_range_are_rejected(write_config, key, value):
    """Be a polite guest on someone else's server: no unbounded waits or retries."""
    with pytest.raises(ConfigError, match=key):
        configuration.load(write_config(MINIMAL + f"\n[collection]\n{key} = {value}\n"))


def test_pmd_is_accepted_as_a_waste_type():
    """The schedule publishes plastic as 'pd' but labels it 'pmd'; both must work."""
    from garbage_collection_automation.configuration import _waste_types

    assert _waste_types({"types": ["PMD"]}, "collection") == ("pd",)


# --- [web] ----------------------------------------------------------------------------


def test_the_web_interface_is_off_until_it_is_asked_for(write_config):
    """A config written before the interface existed must still load, and change nothing."""
    web = configuration.load(write_config(MINIMAL)).web

    assert web.enabled is False
    assert web.host == "127.0.0.1"
    assert web.port == 8080


def test_the_web_interface_is_read(write_config):
    path = write_config(MINIMAL + "\n[web]\nenabled = true\nport = 9000\n")

    web = configuration.load(path).web

    assert web.enabled is True
    assert web.port == 9000


def test_localhost_is_understood_as_the_address_it_means(write_config):
    path = write_config(MINIMAL + '\n[web]\nhost = "localhost"\n')

    assert configuration.load(path).web.host == "127.0.0.1"


def test_ipv6_loopback_is_a_loopback_address_too(write_config):
    path = write_config(MINIMAL + '\n[web]\nhost = "::1"\n')

    assert configuration.load(path).web.host == "::1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_the_page_cannot_be_put_on_a_network(write_config, host):
    """It has no login and shows the token; an ssh tunnel is the way in from elsewhere."""
    with pytest.raises(ConfigError, match="loopback"):
        configuration.load(write_config(MINIMAL + f'\n[web]\nhost = "{host}"\n'))


def test_a_host_that_is_not_an_address_at_all_is_rejected(write_config):
    with pytest.raises(ConfigError, match="not an IP address"):
        configuration.load(write_config(MINIMAL + '\n[web]\nhost = "gca.local"\n'))


@pytest.mark.parametrize("port", [80, 1023, 65536, 0])
def test_a_port_the_service_user_cannot_have_is_rejected(write_config, port):
    """The server runs unprivileged, so a port below 1024 would fail at the bind."""
    with pytest.raises(ConfigError, match="port"):
        configuration.load(write_config(MINIMAL + f"\n[web]\nport = {port}\n"))


def test_a_typo_in_the_web_section_is_rejected(write_config):
    with pytest.raises(ConfigError, match="prot"):
        configuration.load(write_config(MINIMAL + "\n[web]\nprot = 8080\n"))


# --- writing it back ------------------------------------------------------------------


def test_what_is_written_is_what_is_read_back(write_config):
    """The round trip is the whole contract: save() refuses anything that fails it."""
    path = write_config(
        '[address]\npostcode = "1234AB"\nhouse_number = 21\naddition = "a"\n'
        '[collection]\napi_key = "app-key"\nlookahead_days = 45\ndue_time = "6:30"\n'
        'types = ["pmd", "glas"]\ntimeout_seconds = 20\nretries = 2\n'
        '[export.todoist]\nenabled = true\ntoken = "s3cret"\nproject = "Huis"\n'
        "remind_days_before = 3\n"
        '[web]\nenabled = true\nhost = "::1"\nport = 9000\n'
        '[logging]\nlevel = "DEBUG"\n'
    )
    before = configuration.load(path)

    configuration.save(
        path, before, token=before.export.todoist.token, api_key=before.collection.api_key
    )

    assert configuration.load(path) == before


def test_a_saved_file_still_explains_itself(write_config):
    """A config that loses its comments the first time it is saved is a worse file."""
    path = write_config(MINIMAL)

    configuration.save(path, configuration.load(path))
    written = path.read_text()

    assert "# Configuration for garbage-collection-automation." in written
    assert "mijnafvalwijzer.nl" in written
    assert configuration.TOKEN_ENV_VAR in written
    assert configuration.API_KEY_ENV_VAR in written
    assert "loopback" in written


def test_the_api_key_is_written_when_it_is_asked_for(write_config):
    path = write_config(MINIMAL)

    configuration.save(path, configuration.load(path), api_key="the-app-key")

    assert configuration.load(path).collection.api_key == "the-app-key"


def test_the_api_key_is_left_out_unless_it_is_asked_for(write_config):
    """Same reason as the token: the environment's copy does not belong in the file."""
    path = write_config(MINIMAL + '\n[collection]\napi_key = "from-file"\n')

    configuration.save(path, configuration.load(path))

    assert 'api_key = ""' in path.read_text()


def test_the_token_is_left_out_unless_it_is_asked_for(write_config):
    """load() prefers the environment; copying that into the file would duplicate it."""
    path = write_config(MINIMAL)
    config = configuration.load(path)

    configuration.save(path, config)

    assert 'token = ""' in path.read_text()


def test_a_token_with_quotes_in_it_survives_the_round_trip(write_config):
    """Every value here is validated, but a string still has to be escaped."""
    path = write_config(MINIMAL)
    awkward = 'a "quoted" \\ token'

    configuration.save(path, configuration.load(path), token=awkward)

    assert configuration.load(path).export.todoist.token == awkward


def test_saving_does_not_widen_who_may_read_the_file(write_config):
    """The installer decides that; a save through the page must not change it."""
    path = write_config(MINIMAL)
    path.chmod(0o600)

    configuration.save(path, configuration.load(path))

    assert path.stat().st_mode & 0o777 == 0o600


def test_a_failed_write_leaves_the_old_file_alone(write_config):
    path = write_config(MINIMAL)
    before = path.read_text()

    with pytest.raises(ConfigError, match="cannot write"):
        configuration.save(path / "not-a-directory" / "config.toml", configuration.load(path))

    assert path.read_text() == before


def test_a_save_rewrites_the_file_itself_when_the_directory_is_closed_to_it(tmp_path):
    """The installed /etc directory is root's; the one file the page may write, it writes."""
    path = tmp_path / "sub" / "config.toml"
    path.parent.mkdir()
    path.write_text(MINIMAL)
    path.parent.chmod(0o500)  # readable and enterable, not writable
    try:
        configuration.save(path, configuration.load(path), api_key="the-app-key")

        assert 'api_key = "the-app-key"' in path.read_text()
        assert sorted(item.name for item in path.parent.iterdir()) == ["config.toml"]
    finally:
        path.parent.chmod(0o700)


def test_a_config_nobody_may_write_is_refused_rather_than_replaced(tmp_path):
    """A rename would go around the file's own mode; that mode is the answer.

    It is also what the page asks before it offers the button, so the two have
    to agree - and nothing may linger next to a save that was turned down.
    """
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL)
    path.chmod(0o444)
    try:
        with pytest.raises(ConfigError, match="not writable"):
            configuration.save(path, configuration.load(path))

        assert path.read_text() == MINIMAL
        assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]
    finally:
        path.chmod(0o600)


def test_the_document_a_save_writes_is_the_one_load_accepts():
    """render() and load() drifting apart is the failure save() exists to catch."""
    example = configuration.load(REPO_ROOT / "config" / "config.example.toml")

    document = configuration.render(example)

    assert configuration.from_data(tomllib.loads(document)) == example


def test_every_waste_code_is_named_in_the_file_it_writes():
    """The list of valid codes is a comment; a new code must not be able to miss it."""
    document = configuration.render(
        configuration.load(REPO_ROOT / "config" / "config.example.toml")
    )

    for code in configuration.WASTE_TYPES:
        assert code in document
