"""Loading and validation of the TOML configuration file."""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

#: Preferred place for the Todoist API token; the environment beats the file.
TOKEN_ENV_VAR = "GCA_TODOIST_TOKEN"

#: Same for the mijnafvalwijzer.nl app key. It is not a per-user secret - the
#: public clients all send the same one - but it is still someone else's key and
#: an installation-level setting, so it is configured rather than compiled in.
API_KEY_ENV_VAR = "GCA_AFVALWIJZER_API_KEY"

#: The waste streams mijnafvalwijzer.nl knows, keyed by the type code it publishes,
#: with the Dutch label we use in a todo. Not every municipality collects all of them.
WASTE_TYPES = {
    "restafval": "Restafval",
    "papier": "Papier",
    "gft": "GFT",
    "pd": "PMD",
    "glas": "Glas",
    "textiel": "Textiel",
    "kca": "KCA",
    "kerstbomen": "Kerstbomen",
}

#: The schedule calls plastic "pd" but labels it "pmd"; accept what people will write.
_TYPE_ALIASES = {"pmd": "pd"}

#: Sensible for a household: the three streams that need a container at the kerb.
DEFAULT_WASTE_TYPES = ("restafval", "papier", "gft")

_POSTCODE_RE = re.compile(r"^([1-9][0-9]{3})\s*([A-Za-z]{2})$")
_DUE_TIME_RE = re.compile(r"^([0-9]{1,2}):([0-9]{2})$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """The configuration file is missing, malformed or invalid."""


@dataclass(frozen=True)
class AddressConfig:
    postcode: str
    house_number: str
    addition: str = ""


@dataclass(frozen=True)
class CollectionConfig:
    #: The key the schedule API expects; see API_KEY_ENV_VAR. Empty until it is
    #: configured, and data_collection is where that is refused.
    api_key: str = ""
    lookahead_days: int = 30
    #: The source publishes dates without a time, so the due moment comes from here.
    due_time: time = time(7, 0)
    types: tuple[str, ...] = DEFAULT_WASTE_TYPES
    timeout_seconds: int = 15
    retries: int = 1


@dataclass(frozen=True)
class TodoistExportConfig:
    enabled: bool = False
    token: str = ""
    project: str = "Home"
    remind_days_before: int = 1


@dataclass(frozen=True)
class ExportConfig:
    todoist: TodoistExportConfig = field(default_factory=TodoistExportConfig)


@dataclass(frozen=True)
class WebConfig:
    """The local page: whether it is served, and where it listens."""

    enabled: bool = False
    #: A loopback address, always - see _web() for why it cannot be anything else.
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class Config:
    address: AddressConfig
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load(path: Path) -> Config:
    """Read *path* and return the validated configuration.

    Secrets may be supplied through the environment instead of the file; see
    ``GCA_TODOIST_TOKEN`` and ``GCA_AFVALWIJZER_API_KEY`` in config.example.toml.

    Raises ``ConfigError`` with a message meant for the log, never a traceback.
    """
    return from_data(read_toml(path))


def _address(section: dict) -> AddressConfig:
    label = "address"
    _reject_unknown(section, label, {"postcode", "house_number", "addition"})

    raw = _required_str(section, label, "postcode")
    match = _POSTCODE_RE.match(raw.strip())
    if match is None:
        raise ConfigError(f"[{label}] postcode '{raw}' is not a Dutch postcode, e.g. '1234AB'")
    postcode = f"{match.group(1)}{match.group(2).upper()}"

    house_number = _house_number(section, label)
    return AddressConfig(
        postcode=postcode,
        house_number=house_number,
        addition=_optional_str(section, label, "addition", "").strip(),
    )


def _house_number(section: dict, label: str) -> str:
    # TOML has real integers, so `house_number = 56` is at least as likely as "56".
    value = section.get("house_number")
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        if "house_number" not in section:
            raise ConfigError(f"[{label}] is missing required key 'house_number'")
        raise ConfigError(f"[{label}] house_number must be a number or a non-empty string")
    if not value.strip().isdigit():
        raise ConfigError(
            f"[{label}] house_number '{value}' must be digits only; put any letter in 'addition'"
        )
    return value.strip()


def _collection(section: dict) -> CollectionConfig:
    label = "collection"
    _reject_unknown(
        section,
        label,
        {"api_key", "lookahead_days", "due_time", "types", "timeout_seconds", "retries"},
    )
    days = _optional_int(section, label, "lookahead_days", 30)
    if not 1 <= days <= 365:
        raise ConfigError(f"[{label}] lookahead_days must be between 1 and 365, got {days}")
    timeout = _optional_int(section, label, "timeout_seconds", 15)
    if not 1 <= timeout <= 60:
        raise ConfigError(f"[{label}] timeout_seconds must be between 1 and 60, got {timeout}")
    retries = _optional_int(section, label, "retries", 1)
    if not 0 <= retries <= 3:
        raise ConfigError(f"[{label}] retries must be between 0 and 3, got {retries}")
    return CollectionConfig(
        # Unlike the Todoist token there is no "enabled" to hang this off: every
        # run needs it. Refusing it here would leave a fresh install with a file
        # nothing can load, including the page that is meant to fill it in - so
        # a blank key fails at the fetch instead, where it can say where to put it.
        api_key=(
            os.environ.get(API_KEY_ENV_VAR) or _optional_str(section, label, "api_key", "")
        ).strip(),
        lookahead_days=days,
        due_time=_due_time(section, label),
        types=_waste_types(section, label),
        timeout_seconds=timeout,
        retries=retries,
    )


def _due_time(section: dict, label: str) -> time:
    raw = _optional_str(section, label, "due_time", "07:00")
    match = _DUE_TIME_RE.match(raw.strip())
    if match is None:
        raise ConfigError(f"[{label}] due_time '{raw}' must look like '07:00'")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ConfigError(f"[{label}] due_time '{raw}' is not a real time of day")
    return time(hour, minute)


def _waste_types(section: dict, label: str) -> tuple[str, ...]:
    value = section.get("types", list(DEFAULT_WASTE_TYPES))
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f'[{label}] types must be a non-empty list, e.g. ["restafval", "papier", "gft"]'
        )
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[{label}] types must be a list of strings")

    normalised = (item.strip().lower() for item in value)
    types = tuple(dict.fromkeys(_TYPE_ALIASES.get(item, item) for item in normalised))
    unknown = sorted(set(types) - set(WASTE_TYPES))
    if unknown:
        raise ConfigError(
            f"[{label}] types has unknown waste type(s): {', '.join(unknown)}; "
            f"valid codes are {', '.join(sorted(WASTE_TYPES))}"
        )
    return types


def _todoist(section: dict) -> TodoistExportConfig:
    label = "export.todoist"
    _reject_unknown(section, label, {"enabled", "token", "project", "remind_days_before"})
    remind = _optional_int(section, label, "remind_days_before", 1)
    if remind < 0:
        raise ConfigError(f"[{label}] remind_days_before cannot be negative, got {remind}")

    enabled = _optional_bool(section, label, "enabled", False)
    token = (os.environ.get(TOKEN_ENV_VAR) or _optional_str(section, label, "token", "")).strip()
    if enabled and not token:
        # Every other key fails the run when it is wrong; a blank token would
        # instead fail at the first API call, hours later and in the cron log.
        raise ConfigError(
            f"[{label}] enabled is true but no token was given; "
            f"set {TOKEN_ENV_VAR} or [{label}] token"
        )

    return TodoistExportConfig(
        enabled=enabled,
        token=token,
        project=_optional_str(section, label, "project", "Home"),
        remind_days_before=remind,
    )


def _web(section: dict) -> WebConfig:
    label = "web"
    _reject_unknown(section, label, {"enabled", "host", "port"})

    port = _optional_int(section, label, "port", 8080)
    if not 1024 <= port <= 65535:
        # Below 1024 only root may listen, and the service user is not root; that
        # is worth failing on here rather than at the bind, once a day, in a log.
        raise ConfigError(f"[{label}] port must be between 1024 and 65535, got {port}")

    return WebConfig(
        enabled=_optional_bool(section, label, "enabled", False),
        host=_host(section, label),
        port=port,
    )


def _host(section: dict, label: str) -> str:
    """The address to listen on, which may only ever be a loopback one.

    The page has no login and shows both the API key and the Todoist token, so it
    is not something to put on a network; reach it from another machine through
    an ssh tunnel.
    """
    raw = _optional_str(section, label, "host", "127.0.0.1").strip()
    host = "127.0.0.1" if raw.lower() == "localhost" else raw
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            f"[{label}] host '{raw}' is not an IP address; use '127.0.0.1' or '::1'"
        ) from None
    if not address.is_loopback:
        raise ConfigError(
            f"[{label}] host '{raw}' is not a loopback address; the page is "
            f"served on localhost only - reach it from elsewhere over an ssh "
            f"tunnel, see the README"
        )
    return host


def _logging(section: dict) -> LoggingConfig:
    label = "logging"
    _reject_unknown(section, label, {"level"})
    level = _optional_str(section, label, "level", "INFO").upper()
    if level not in _LOG_LEVELS:
        raise ConfigError(f"[{label}] level '{level}' is not one of {', '.join(_LOG_LEVELS)}")
    return LoggingConfig(level=level)


def _section(data: dict, key: str, label: str, *, required: bool = False) -> dict:
    value = data.get(key)
    if value is None:
        if required:
            raise ConfigError(f"missing required section [{label}]")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{label}] must be a table")
    return value


def _reject_unknown(section: dict, label: str, allowed: set[str]) -> None:
    """Fail on typos and leftovers rather than silently ignoring them."""
    unknown = sorted(set(section) - allowed)
    if unknown:
        where = f"[{label}]" if label else "the configuration file"
        raise ConfigError(f"{where} has unknown key(s): {', '.join(unknown)}")


def _required_str(section: dict, label: str, key: str) -> str:
    if key not in section:
        raise ConfigError(f"[{label}] is missing required key '{key}'")
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{label}] {key} must be a non-empty string")
    return value


def _optional_str(section: dict, label: str, key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"[{label}] {key} must be a string")
    return value


def _optional_int(section: dict, label: str, key: str, default: int) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"[{label}] {key} must be a whole number")
    return value


def _optional_bool(section: dict, label: str, key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{label}] {key} must be true or false")
    return value


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# --- writing it back ------------------------------------------------------------------
#
# The web interface saves the form; every other writer is a person with an editor.
# A save re-renders the whole document rather than patching lines in it, so the
# comments below are the file's documentation and survive every save - a config
# that loses its annotations the first time it is saved is worse than one nobody
# can edit at all.


def render(config: Config, *, token: str = "", api_key: str = "") -> str:
    """The configuration as an annotated TOML document, ready to be written.

    *token* is what lands in ``[export.todoist] token`` and *api_key* what lands
    in ``[collection] api_key``; both default to nothing on purpose. ``load()``
    prefers the environment over the file for either, so a ``Config`` read on a
    host that sets one carries a value that belongs to the environment. Copying
    that into the file would duplicate it, and the copy would then be the one
    that goes stale. Pass them only when they came from the file.
    """
    collection = config.collection
    todoist = config.export.todoist
    web = config.web
    return f"""\
# Configuration for garbage-collection-automation.
# Written by the web interface, and safe to edit by hand - the next save
# re-renders this file from top to bottom, comments included.

[address]
# Looked up at mijnafvalwijzer.nl; the addition is the letter or suffix, if any.
postcode = {_toml_str(config.address.postcode)}
house_number = {_toml_str(config.address.house_number)}
addition = {_toml_str(config.address.addition)}

[collection]
# The key the schedule API expects: a fixed one the public afvalwijzer clients
# all send, so not a per-user secret - but not ours to compile in either.
# {API_KEY_ENV_VAR} wins over this key when it is set, exactly as the Todoist
# token below does; the README says where to find the value.
api_key = {_toml_str(api_key)}
# How far ahead to look when building to-dos.
lookahead_days = {collection.lookahead_days}
# The schedule publishes dates without a time, so this is the to-dos due moment
# (Europe/Amsterdam, on the collection day itself).
due_time = {_toml_str(collection.due_time.isoformat("minutes"))}
# Which waste streams to track - not every municipality collects all of them.
# Valid codes: {", ".join(sorted(WASTE_TYPES))}
types = {_toml_list(collection.types)}
# Be a polite guest on someone else's server.
timeout_seconds = {collection.timeout_seconds}
retries = {collection.retries}

[export.todoist]
# Enabling this requires a token, from either source below; a run with neither
# fails immediately rather than at the first API call.
enabled = {_toml_bool(todoist.enabled)}
# Prefer {TOKEN_ENV_VAR} over storing the token here. A cron run gets it from
# the env file next to this one, which run-job.sh sources before every run; when
# that variable is set it wins, and the web interface leaves this key alone.
token = {_toml_str(token)}
project = {_toml_str(todoist.project)}
# How long before the collection moment the to-dos reminder goes off, in days.
# The to-dos themselves are created for the whole lookahead window above.
remind_days_before = {todoist.remind_days_before}

[web]
# The local page: a small server that hands out the interface in ui/ and the
# handful of endpoints it drives itself with. Switch it on here, then restart it:
#   systemctl restart garbage-collection-automation-web
enabled = {_toml_bool(web.enabled)}
# It listens on the loopback interface only - the page has no login and shows the
# secrets, so only a loopback address is accepted here. Reach it from another
# machine over an ssh tunnel; the README has the command.
host = {_toml_str(web.host)}
port = {web.port}

[logging]
level = {_toml_str(config.logging.level)}
"""


def save(path: Path, config: Config, *, token: str = "", api_key: str = "") -> None:
    """Write *config* to *path* atomically, and only if it can be read back.

    The rendered document is parsed and validated before it replaces anything:
    a file this program cannot load would stop every run from here on, and the
    caller can still be told no while the old one is untouched.

    Raises ``ConfigError``; see ``render()`` for what *token* and *api_key* mean.
    """
    document = render(config, token=token, api_key=api_key)
    try:
        from_data(tomllib.loads(document))
    except (tomllib.TOMLDecodeError, ConfigError) as exc:
        # Not the caller's mistake to fix: the values were validated on the way
        # in, so getting here means render() and load() no longer agree.
        raise ConfigError(
            f"refusing to write a configuration that cannot be read back: {exc}"
        ) from exc

    # A rename over the file would let anyone who may write the *directory*
    # replace a file they may not write. That is not what the mode on the file
    # means to the person who set it, and it is what the page asks about before
    # it offers the button, so honour it here.
    if path.exists() and not os.access(path, os.W_OK):
        raise ConfigError(f"cannot write {path}: it is not writable")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.access(path.parent, os.W_OK | os.X_OK):
            _write_by_rename(path, document)
        else:
            # The installed layout: /etc/<app> belongs to root and is not the
            # service user's to create anything in - it holds the env file - so
            # the one file that user may write, it writes in place. The document
            # was parsed above before a byte of it was written, which is what
            # makes the moment the file is short less of a risk than a directory
            # this program may add files to.
            _write_in_place(path, document)
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}") from exc


def _write_by_rename(path: Path, document: str) -> None:
    """Replace *path* through a temp file next to it, so no reader sees it half-written."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(document, encoding="utf-8")
        # Take the mode of the file being replaced: the installer decides who may
        # read the config, and a save must not quietly widen that.
        with contextlib.suppress(OSError):
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        tmp.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _write_in_place(path: Path, document: str) -> None:
    """Rewrite *path* itself, keeping its owner and mode - and its directory shut."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write(document)
        handle.flush()
        os.fsync(handle.fileno())


def from_data(data: dict) -> Config:
    """Validate a parsed TOML document and return the configuration it describes.

    ``load()`` minus the file. The web interface saves a form by overlaying it
    on the document already on disk and handing the result to this, so a value
    typed into the page is refused in exactly the words the file would be.
    """
    _reject_unknown(data, "", {"address", "collection", "export", "web", "logging"})

    export = _section(data, "export", "export")
    _reject_unknown(export, "export", {"todoist"})

    return Config(
        address=_address(_section(data, "address", "address", required=True)),
        collection=_collection(_section(data, "collection", "collection")),
        export=ExportConfig(todoist=_todoist(_section(export, "todoist", "export.todoist"))),
        web=_web(_section(data, "web", "web")),
        logging=_logging(_section(data, "logging", "logging")),
    )


def _toml_str(value: str) -> str:
    """A TOML basic string. Every value here is validated, but quoting is not optional."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for character, escape in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        escaped = escaped.replace(character, escape)
    return f'"{escaped}"'


def _toml_list(values) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
