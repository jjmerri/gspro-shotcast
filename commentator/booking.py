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

    async def refresh(self, now: datetime | None = None, why: str = "") -> None:
        started = time.monotonic()
        day = (now or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        params = {"from": f"{day - timedelta(days=1):%Y-%m-%d %H:%M:%S}",
                  "to": f"{day + timedelta(days=2):%Y-%m-%d %H:%M:%S}", "limit": 200}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(_API.format(schedule=self.schedule_id), params=params,
                                        auth=(self.account, self.api_key))
                resp.raise_for_status()
                rows = resp.json().get("bookings") or []
        except Exception as e:  # SuperSaaS down or the key revoked: never take the booth off air
            log.warning("SuperSaaS bookings fetch%s failed after %.2fs: %s", f" ({why})" if why else "",
                        time.monotonic() - started, e)
            self.bookings = None    # unknown, not stale: a booking may have changed since the last read
            return
        bookings = []
        for row in rows:
            if row.get("deleted"):
                continue
            try:
                start, finish = datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["finish"])
            except (KeyError, TypeError, ValueError):
                continue
            bookings.append(Booking(start, finish, str(row.get(self.type_field) or ""),
                                    str(row.get("full_name") or row.get("created_by") or "").strip()))
        log.info("SuperSaaS bookings%s in %.2fs: %s", f" ({why})" if why else "", time.monotonic() - started,
                 "; ".join(f"{b.start:%a %H:%M}-{b.finish:%H:%M} {b.kind or 'no type'}" for b in bookings) or "none")
        self.bookings = bookings

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
