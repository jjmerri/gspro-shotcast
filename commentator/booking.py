"""The sim's bookings on SuperSaaS, as a hint whether a round is league or practice.

Every booking picks "Practice" or "League Week N" from a mandatory dropdown. A round played in a
practice booking is practice, whatever the course and players. A league booking doesn't say which
week (a group may play two weeks in one block), so it, and play with no booking at all, is left to
the usual schedule matching. Read-only: fetched at startup (so a restart mid-round still knows) and
as each setup screen comes up. While a fetch fails the booth decides as it would without bookings.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)

_API = "https://www.supersaas.com/api/range/{schedule}.json"


@dataclass
class Booking:
    start: datetime     # the schedule's local time, as the booth's clock reads it
    finish: datetime
    kind: str           # the dropdown's text: "Practice", "League Week 3", or "" when unset
    name: str = ""

    @property
    def practice(self) -> bool:
        return self.kind.strip().lower().startswith("practice")


class Bookings:
    def __init__(self, account: str, schedule_id: int, api_key: str, type_field: str = "field_1_r",
                 slack_minutes: float = 30.0, timeout: float = 10.0) -> None:
        self.account, self.schedule_id, self.api_key = account, schedule_id, api_key
        self.type_field = type_field
        self.slack = timedelta(minutes=slack_minutes)
        self.timeout = timeout
        self.bookings: list[Booking] | None = None   # None until the first good fetch

    async def _fetch(self, start: datetime, end: datetime) -> list[Booking]:
        """The bookings between start and end, deleted ones dropped. Raises when the fetch fails."""
        params = {"from": f"{start:%Y-%m-%d %H:%M:%S}", "to": f"{end:%Y-%m-%d %H:%M:%S}", "limit": 500}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(_API.format(schedule=self.schedule_id), params=params,
                                    auth=(self.account, self.api_key))
            resp.raise_for_status()
            rows = resp.json().get("bookings") or []
        bookings = []
        for row in rows:
            if row.get("deleted"):
                continue
            try:
                b_start, finish = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["finish"])
            except (KeyError, TypeError, ValueError):
                continue
            bookings.append(Booking(b_start, finish, str(row.get(self.type_field) or ""),
                                    str(row.get("full_name") or row.get("created_by") or "").strip()))
        return sorted(bookings, key=lambda b: b.start)

    async def refresh(self, now: datetime | None = None, why: str = "") -> None:
        started = time.monotonic()
        day = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            bookings = await self._fetch(day - timedelta(days=1), day + timedelta(days=2))
        except Exception as e:  # SuperSaaS down or the key revoked: never take the booth off air
            log.warning("SuperSaaS bookings fetch%s failed after %.2fs: %s", f" ({why})" if why else "",
                        time.monotonic() - started, e)
            self.bookings = None    # unknown, not stale: a booking may have changed since the last read
            return
        log.info("SuperSaaS bookings%s in %.2fs: %s", f" ({why})" if why else "", time.monotonic() - started,
                 "; ".join(f"{b.start:%a %H:%M}-{b.finish:%H:%M} {b.kind or 'no type'}" for b in bookings) or "none")
        self.bookings = bookings

    async def upcoming(self, days: int = 14, now: datetime | None = None) -> list[Booking]:
        """The bookings from now (one under way included) to `days` ahead, for the stats page's
        schedule. Raises when the fetch fails."""
        now = now or datetime.now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [b for b in await self._fetch(day, day + timedelta(days=days + 1))
                if b.finish > now and b.start < now + timedelta(days=days)]

    def at(self, now: datetime | None = None) -> Booking | None:
        """The booking the sim is in now: one running now, else the nearest within the slack either
        side (a group starts early or runs over). None with no booking near, or no list yet."""
        now = now or datetime.now()
        best, gap = None, None
        for b in self.bookings or []:
            if b.start - self.slack <= now <= b.finish + self.slack:
                off = max(b.start - now, now - b.finish, timedelta(0))
                if gap is None or off < gap:
                    best, gap = b, off
        return best


def schedule_json(bookings: list[Booking], now: datetime | None = None) -> bytes:
    """The stats page's schedule.json: each booking's local start and finish (no time zone: the
    page shows them as the sim's clock reads them), its type and the booker's name (never an email
    address, which is what SuperSaaS has for a booking with no name)."""
    import json

    return json.dumps({"updated": (now or datetime.now()).isoformat(timespec="seconds"),
                       "bookings": [{"start": b.start.isoformat(timespec="minutes"),
                                     "finish": b.finish.isoformat(timespec="minutes"),
                                     "kind": b.kind, "name": "" if "@" in b.name else b.name}
                                    for b in bookings]}).encode()
