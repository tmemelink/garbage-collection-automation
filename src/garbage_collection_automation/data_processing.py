"""Turning the raw schedule into the collection events we want a todo for."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .configuration import WASTE_TYPES, Config
from .data_collection import RawSchedule

#: The source is Dutch and so is the collection round; the host's clock is irrelevant.
TIMEZONE = ZoneInfo("Europe/Amsterdam")

log = logging.getLogger(__name__)


@dataclass(frozen=True, order=True)
class Collection:
    """A single collection event: one waste stream on one date."""

    date: date
    #: The source's own type code, e.g. "restafval" or "pd".
    code: str

    @property
    def key(self) -> str:
        """Stable identity of this collection, shared with the export state."""
        return f"{self.date.isoformat()}:{self.code}"

    @property
    def waste_type(self) -> str:
        """The Dutch label for the todo, e.g. "Restafval"."""
        return WASTE_TYPES.get(self.code, self.code.title())

    def due_at(self, time_of_day: time) -> datetime:
        """The moment the todo is due: the collection date at *time_of_day*, Dutch local."""
        return datetime.combine(self.date, time_of_day, tzinfo=TIMEZONE)

    def __str__(self) -> str:
        return f"{self.date.isoformat()} {self.waste_type}"


def process(raw: RawSchedule, config: Config, *, today: date | None = None) -> list[Collection]:
    """Parse *raw* into collections, ordered by date and limited to the lookahead.

    Pass *today* to fix the day the window starts from; it defaults to the real one.
    """
    start = day_or_today(today)
    horizon = start + timedelta(days=config.collection.lookahead_days)
    wanted = set(config.collection.types)

    collections: set[Collection] = set()
    for entry in raw.entries:
        code = str(entry.get("type", "")).strip().lower()
        if code not in wanted:
            continue
        day = _parse_date(entry)
        if day is None or not start <= day <= horizon:
            continue
        collections.add(Collection(date=day, code=code))

    _warn_about_streams_that_never_come(raw, wanted)

    result = sorted(collections)
    log.debug("%d of %d date(s) are upcoming and wanted", len(result), len(raw.entries))
    return result


def _warn_about_streams_that_never_come(raw: RawSchedule, wanted: set[str]) -> None:
    """Not every municipality collects every stream; a silent zero would look like a bug."""
    published = {str(entry.get("type", "")).strip().lower() for entry in raw.entries}
    missing = sorted(wanted - published)
    if missing:
        log.warning(
            "the schedule for %s never mentions %s; check [collection] types",
            raw.address,
            ", ".join(missing),
        )


def _parse_date(entry: dict) -> date | None:
    raw_date = str(entry.get("date", ""))
    try:
        return date.fromisoformat(raw_date)
    except ValueError:
        # One unreadable row must not cost us the rest of the year.
        log.warning("skipping a collection date the source wrote as %r", raw_date)
        return None


def today() -> date:
    """Today in Dutch terms - a run near midnight must not use the host's date.

    Also the seam the tests freeze, and where reconciliation gets its idea of
    "the past" from, so both halves of the pipeline agree on where it ends.
    """
    return datetime.now(TIMEZONE).date()


def day_or_today(day: date | None) -> date:
    """*day* when the caller fixed one, otherwise today.

    Every step that has an idea of where the past ends - processing, the export
    preview, reconciliation - resolves it here, so a caller that fixes the day
    fixes it for all of them at once.
    """
    return day if day is not None else today()
