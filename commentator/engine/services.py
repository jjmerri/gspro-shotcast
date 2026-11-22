"""The booth's side jobs: the commentary switch, weather, bookings, the schedule and the league page.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import json
import os
import time

from .common import log
from ..board import Board
from ..booking import Bookings
from ..facts import ctp_text as _ctp_text
from ..models import Line
from ..news import GolfNews
from ..scores import LiveScores
from ..trivia import CourseTrivia
from ..weather import Weather


class ServicesMixin:
    """The booth's side jobs: the commentary switch, weather, bookings, the schedule and the league page."""

    def set_commentary(self, on: bool) -> None:
        """Put the running booth on or off air. Off air it stops talking at once (what's playing is cut
        and nothing more is voiced) and makes no model calls, but every shot and round is still read
        and recorded, so going back on air picks up mid-round with the full picture."""
        if on == self.commentary_on:
            return
        if on and self.commentary_unavailable:
            log.warning("Can't go on air: %s", self.commentary_unavailable)
            return
        self.commentary_on = on
        log.info("Booth %s", "on air" if on else "off air: recording shots and rounds only")
        if on and (card := getattr(self.round, "course_card", None)):   # read off air: no listing notes yet
            try:
                from .. import course_card
                course_card.add_notes(card)
            except Exception as e:
                log.info("Couldn't read the course listing's notes (%s)", e)
        if on and not self._minimap_tried:   # started off air: the hazard reader wasn't built
            self._minimap_tried = True
            self.minimap = self._make_minimap()
        if not on:
            for task in list(self._minimap_tasks):
                task.cancel()   # a Jev read is several paid calls: stop the rest
        if self.speech is None:
            return
        if not on:
            self._cancel_preshot()
            self.speech.interrupt()
            self.speech.muted = True
            return
        self.speech.muted = False
        asyncio.create_task(self.speech.prepare(Line(self.speakers[0], "Warming up.")))
        if self.news is None and self.cfg.news.enabled:
            self.news = GolfNews(self.cfg.news.feeds, self.cfg.news.limit, max_age_days=self.cfg.news.max_age_days)
            asyncio.create_task(self._news_loop(), name="news")
        w = self.cfg.weather
        if self.weather is None and w.enabled and (w.location or (w.latitude is not None and w.longitude is not None)):
            self.weather = Weather(w.latitude, w.longitude, w.place, location=w.location, country=w.country)
            asyncio.create_task(self._weather_loop(), name="weather")
        if self.scores is None and self.cfg.scores.enabled:
            self.scores = LiveScores(self.cfg.scores.tours, self.cfg.scores.teams)
        if self.trivia is None and self.cfg.trivia.enabled:
            self.trivia = CourseTrivia(self.cfg.trivia.pages)

    def _cancel_preshot(self) -> None:
        # Only the plan-building task. The model call it awaits is NOT cancelled here: the run loop
        # calls this right before starting handle_preshot for a new setup, and that setup's own model
        # call is already in flight (started in _emit_setup). Cancelling it would kill the plan we're
        # about to build. A superseded call just finishes unused - a cheap wasted request.
        if self._preshot_task and not self._preshot_task.done():
            self._preshot_task.cancel()

    async def _warm_ocr(self) -> None:
        """Load the OCR model with a throwaway read at startup, so the first hole's reads aren't cold
        (RapidOCR's first inference is slow and otherwise lands on the critical path)."""
        try:
            await self.screen.read_hud_async(["player_box"])
        except Exception:
            log.debug("OCR warmup read failed", exc_info=True)

    async def _weather_loop(self) -> None:
        while True:
            await self.weather.refresh()
            await asyncio.sleep(max(self.cfg.weather.refresh_minutes, 1.0) * 60)

    def _make_bookings(self) -> Bookings | None:
        b = self.cfg.booking
        if not b.enabled:
            return None
        key = os.environ.get("SUPERSAAS_API_KEY", "").strip()
        if not (key and b.account and b.schedule_id):
            log.warning("SuperSaaS bookings are on but %s; rounds are classified without them",
                        "SUPERSAAS_API_KEY isn't set" if not key else "booking.account or schedule_id isn't set")
            return None
        return Bookings(b.account, b.schedule_id, key, b.type_field, b.slack_minutes)

    def _fetch_bookings(self, why: str, fresh_within: float = 0.0) -> None:
        """Re-read the bookings in the background (at startup, so a restart mid-round knows, and at the
        course select or setup screen, whichever comes first). A fetch already running covers this one,
        as does one within `fresh_within` seconds."""
        if self.bookings is None or (self._bookings_task and not self._bookings_task.done()):
            return
        if time.monotonic() - self._bookings_fetched < fresh_within:
            return
        self._bookings_fetched = time.monotonic()
        self._bookings_task = asyncio.create_task(self.bookings.refresh(why=why), name="bookings")

    def _s3(self):
        from .. import backend

        return backend.s3(self.cfg.publish.region)

    def _schedule_key(self) -> str:
        p = self.cfg.publish.prefix.strip("/")
        return (p + "/" if p else "") + "schedule.json"

    def _remove_schedule(self) -> None:
        """No schedule (bookings or the tab turned off): take the bookings off the page."""
        try:
            self._s3().delete_objects(Bucket=self.cfg.publish.bucket, Delete={"Objects": [{"Key": self._schedule_key()}]})
        except Exception as e:
            log.info("Schedule: couldn't remove the stats page's schedule: %s", e)

    async def _schedule_loop(self) -> None:
        """Keep the stats page's Schedule tab (schedule.json) up to date with the next few weeks of
        bookings. A failed fetch leaves the last one up; the page says when it was read."""
        from ..booking import schedule_json

        p, b = self.cfg.publish, self.cfg.booking
        client = None
        while True:
            try:
                body = schedule_json(await self.bookings.upcoming(b.schedule_days))

                def put() -> None:
                    nonlocal client
                    if client is None:
                        client = self._s3()
                    client.put_object(Bucket=p.bucket, Key=self._schedule_key(), Body=body,
                                      ContentType="application/json", CacheControl="no-cache")
                await asyncio.to_thread(put)
                log.info("Schedule: uploaded %d upcoming bookings", len(json.loads(body)["bookings"]))
            except Exception as e:
                log.warning("Schedule: couldn't update the stats page's schedule: %s", e)
            await asyncio.sleep(b.schedule_refresh_minutes * 60)

    def _league_page_key(self) -> str:
        p = self.cfg.publish.prefix.strip("/")
        return (p + "/" if p else "") + "league.json"

    def _remove_league_page(self) -> None:
        """No Squabbit league: take the Home tab's league cards off the page."""
        try:
            self._s3().delete_objects(Bucket=self.cfg.publish.bucket,
                                      Delete={"Objects": [{"Key": self._league_page_key()}]})
        except Exception as e:
            log.info("League page: couldn't remove the stats page's league cards: %s", e)

    def _league_board(self) -> Board | None:
        """Squabbit's board with the sim's finished league rounds standing in for teams that haven't
        posted yet: what the stats page shows. None before a Squabbit read. Read fresh each time, so a
        round deleted or excluded on the stats page drops out."""
        if self.board is None:
            return None
        return self.board.with_sim(self.store.week_results(self.league.data.get("schedule") or []))

    def _publish_league_page(self) -> None:
        """Put the stats page's Home tab league cards (league.json: every due date's weeks with their
        closest-to-the-pin cards, the season standings) up. Written when they can change: after the
        round-start Squabbit read, when a league round finishes, and at midnight (a passed deadline
        makes a week final). Before a Squabbit read this run, the last one up stands. One upload at a
        time, the newest last."""
        board = self._league_board() if self._league_page_on else None
        if board is None:
            return
        page = board.page(time.strftime("%Y-%m-%d"))
        weeks = [w for d in page["dates"] for w in d["weeks"]] + ([page["week"]] if page.get("week") else [])
        for w in weeks:
            if "ctp" not in w:
                w["ctp"] = self._ctp_card(w["week"])
        self._league_body = json.dumps({**page, "updated": round(time.time())}).encode()
        if not self._league_uploading:
            self._league_uploading = True
            self._bg(self._upload_league_page())
        self._recap_soon()

    def _ctp_card(self, week: int) -> dict | None:
        """The stats page's closest-to-the-pin card for a league week: each CTP hole's closest tee shot
        on the green in the sim's league rounds (None for a hole nobody's hit yet), and the prize."""
        entry = self.league.week_entry(week) or {}
        holes = [int(h) for h in entry.get("ctp_holes") or []]
        if not holes:
            return None
        best = self._schedule_ctp(entry, [], holes)
        return {"prize": self.league.data.get("ctp_prize"),
                "holes": [{"hole": h, **({"player": best[h][0], "yards": round(best[h][1], 2),
                                          "distance": _ctp_text(best[h][1])} if h in best else {})}
                          for h in holes]}

    async def _upload_league_page(self) -> None:
        try:
            sent = None
            while self._league_body is not sent:
                sent = body = self._league_body
                try:
                    await asyncio.to_thread(self._s3().put_object, Bucket=self.cfg.publish.bucket,
                                            Key=self._league_page_key(), Body=body,
                                            ContentType="application/json", CacheControl="no-cache")
                except Exception as e:
                    log.warning("League page: couldn't update the stats page's league cards: %s", e)
        finally:
            self._league_uploading = False
