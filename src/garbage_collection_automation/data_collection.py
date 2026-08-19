"""Retrieval of the collection schedule from the mijnafvalwijzer.nl JSON API.

The website is a front end for an undocumented JSON API that the official app and
web client both query; we ask it the same question instead of scraping HTML. One
request per run returns the whole year, so be a polite guest: short timeout,
few retries, and a user agent that says who is calling.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from . import __version__
from .configuration import API_KEY_ENV_VAR, Config

API_URL = "https://api.mijnafvalwijzer.nl/webservices/appsinput/"

USER_AGENT = (
    f"garbage-collection-automation/{__version__} "
    "(+https://github.com/tmemelink/garbage-collection-automation)"
)

#: Grows with each attempt; a scheduled run can afford to wait a moment.
_BACKOFF_SECONDS = 2.0

#: Worth trying again; anything else is our mistake, not a hiccup.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

log = logging.getLogger(__name__)


class CollectionError(Exception):
    """The schedule could not be retrieved, or the source does not know the address."""


class MissingApiKey(CollectionError):
    """No app key is configured, so the source cannot be asked anything at all."""

    def __init__(self) -> None:
        super().__init__(
            "no mijnafvalwijzer.nl API key is configured; set [collection] api_key in "
            f"the configuration file or {API_KEY_ENV_VAR} in the environment - the "
            "README says where to find the value"
        )


@dataclass(frozen=True)
class RawSchedule:
    """The collection dates for one address, still exactly as the source phrased them."""

    url: str
    fetched_at: datetime
    #: How the source resolved the address, e.g. "Voorbeeldstraat 21, Voorbeeldstad" - worth logging,
    #: because a wrong postcode resolves to somewhere else rather than failing.
    address: str
    #: ``info.afvaldataVersion``: bumped when the municipality changes the schedule.
    data_version: str
    entries: tuple[dict, ...]


def build_url(config: Config) -> str:
    """Return the API URL for the configured address.

    Raises ``MissingApiKey`` when no key is configured. The configuration file
    accepts a blank one - see ``_collection()`` there - so this is where an
    install that was never given a key finds out, before any request is made.
    """
    address = config.address
    api_key = config.collection.api_key
    if not api_key:
        raise MissingApiKey
    query = {
        "apikey": api_key,
        "method": "postcodecheck",
        "postcode": address.postcode,
        "street": "",
        "huisnummer": address.house_number,
        "toevoeging": address.addition,
        "app_name": "afvalwijzer",
        "platform": "web",
        "langs": "nl",
    }
    return f"{API_URL}?{urlencode(query)}"


def collect(config: Config, *, client: httpx.Client | None = None) -> RawSchedule:
    """Fetch the collection schedule for the configured address.

    Pass *client* to supply your own transport; otherwise one is opened and closed here.
    """
    url = build_url(config)
    if client is not None:
        return _collect(client, url, config)
    with httpx.Client() as owned:
        return _collect(owned, url, config)


def _collect(client: httpx.Client, url: str, config: Config) -> RawSchedule:
    # The query contains both an API key and a household address. Keep it out of
    # the log even at DEBUG; the endpoint is enough to identify the request.
    log.debug("GET %s", API_URL)
    response = _fetch(client, url, config)
    schedule = _parse(response.text, config)
    log.info(
        "collected %d date(s) for %s (schedule version %s)",
        len(schedule.entries),
        schedule.address,
        schedule.data_version,
    )
    return schedule


def _fetch(client: httpx.Client, url: str, config: Config) -> httpx.Response:
    attempts = config.collection.retries + 1
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=config.collection.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Some httpx exception messages include the request URL. That URL's
            # query is private, so retain the useful failure type but not its text.
            problem = f"could not reach {httpx.URL(url).host} ({type(exc).__name__})"
        else:
            if response.status_code == httpx.codes.OK:
                return response
            problem = f"mijnafvalwijzer.nl returned HTTP {response.status_code}"
            if response.status_code not in _RETRY_STATUSES:
                raise CollectionError(problem)

        if attempt == attempts:
            raise CollectionError(problem)
        log.warning("%s; retrying (%d/%d)", problem, attempt, attempts - 1)
        time.sleep(_BACKOFF_SECONDS * attempt)

    raise AssertionError("unreachable")  # pragma: no cover


def _parse(body: str, config: Config) -> RawSchedule:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise CollectionError(f"the response was not JSON: {exc}") from exc

    where = f"{config.address.postcode} {config.address.house_number}".strip()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        # An unknown house number answers {"response": "NOK", "data": [], "error": ...}.
        reason = payload.get("error") if isinstance(payload, dict) else "unexpected response"
        raise CollectionError(f"the source has no data for {where}: {reason}")

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    address = _resolved_address(info, config)

    entries = _entries(data)
    if not entries:
        # A postcode the source does not serve still answers OK, but with an empty
        # schedule for some fallback municipality - so an empty list is the real signal.
        raise CollectionError(
            f"no collection schedule for {where}; the source resolved that address to {address}"
        )

    return RawSchedule(
        # Retain provenance without leaving a credential-bearing query string
        # on an object that may later be logged or included in an error report.
        url=API_URL,
        fetched_at=datetime.now(UTC),
        address=address,
        data_version=str(info.get("afvaldataVersion", "")),
        entries=entries,
    )


def _entries(data: dict) -> tuple[dict, ...]:
    """This year's schedule plus next year's once the municipality publishes it."""
    entries: list[dict] = []
    for key in ("ophaaldagen", "ophaaldagenNext"):
        section = data.get(key)
        if not isinstance(section, dict) or section.get("response") != "OK":
            continue
        entries.extend(item for item in section.get("data") or [] if isinstance(item, dict))
    return tuple(entries)


def _resolved_address(info: dict, config: Config) -> str:
    street = str(info.get("straat", "")).strip()
    place = str(info.get("plaats", "")).strip()
    number = str(info.get("huisnummer", config.address.house_number)).strip()
    if not street and not place:
        return f"{config.address.postcode} {config.address.house_number}"
    return f"{street} {number}, {place}".strip()
