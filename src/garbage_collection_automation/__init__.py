"""Collects garbage collection dates and creates to-dos."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

__version__ = "0.1.0"

# Below the version on purpose: data_collection puts it in the User-Agent it sends,
# so the name has to exist by the time these modules are imported.
from . import application, configuration
from .application import Status

#: Where the installed service keeps its config. A checkout passes --config.
DEFAULT_CONFIG = Path("/etc/garbage-collection-automation/config.toml")

#: Where the installed service remembers what it exported. A checkout passes --state.
DEFAULT_STATE = Path("/var/lib/garbage-collection-automation/state.json")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_COLLECTION_ERROR = 4
EXIT_EXPORT_ERROR = 5
#: The web interface only; every code above belongs to the job.
EXIT_WEB_ERROR = 6

#: How a run's outcome reaches cron; see the Exit codes table in the README.
EXIT_CODES = {
    Status.OK: EXIT_OK,
    Status.NOT_IMPLEMENTED: EXIT_NOT_IMPLEMENTED,
    Status.COLLECTION_ERROR: EXIT_COLLECTION_ERROR,
    Status.EXPORT_ERROR: EXIT_EXPORT_ERROR,
}

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garbage-collection-automation",
        description=__doc__,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to the configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help="path to the local record of what was exported (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and process, but do not write any to-dos",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def quiet_the_http_client() -> None:
    """Keep httpx from narrating requests, including when application logging is DEBUG.

    httpx announces every request at INFO, query string and all - and
    ``data_collection.build_url()`` puts the schedule API's key, the postcode
    and the house number in that query string. A debug log must not become a
    second credentials-and-address store, so third-party request logging stays
    muted at every application log level.

    Every entry point that runs the pipeline calls this: the cron job through
    :func:`main` and the web interface through :func:`web.main`, which reaches
    the same code from a button press.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``garbage-collection-automation`` console script.

    Everything the job does lives in :mod:`application`; this only reads the
    command line and the config file, and turns the outcome into an exit code.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        config = configuration.load(args.config)
    except configuration.ConfigError as exc:
        log.error("configuration: %s", exc)
        return EXIT_CONFIG_ERROR
    logging.getLogger().setLevel(config.logging.level)
    quiet_the_http_client()

    result = application.run(config, state_path=args.state, dry_run=args.dry_run)
    if not result.ok:
        # A source that is unreachable, an address it does not know, a record that
        # cannot be written: ordinary outcomes for a scheduled run, so they get a
        # line and a useful exit code rather than a traceback.
        log.error("%s", result.summary)
    return EXIT_CODES[result.status]


if __name__ == "__main__":
    sys.exit(main())
