"""A venue bay's claim cards (shotcast-cloud's docs/dev/claims.md): as a round ends, a card for each
player with a QR code. A customer scans theirs with their phone and the round is added to their
Shotcast account. Only where the cloud says the bay shows them (`claim_cards` in its answer about
this PC: a venue's bays), and only while the booth runs paired (cloud_booth starts it).

Each player's code is made on this PC and kept with the round (cloud_rounds.claim_codes); the round's
upload carries its hash, never the code. The cards go up the moment the round is over, usually
before the round has been sent: a code scanned early is kept by the cloud until the round arrives.
While they show, the cloud is asked who has claimed (`GET /api/devices/rounds/{id}/claims`) and a
claimed card says so, with the claimer's short name ("Mike R.").

They come down when someone closes them, when anyone plays again (a round or practice), a while
after every player has claimed, after SHOW_S, or when the season file can't be read (what the bay
is doing can't be told). A round that was already over when the booth started gets no
cards: staff can make a fresh code on the website.

The window is the booth's own (popup.py's kind): it never takes focus from GSPro and is kept out of
screen capture, so the codes aren't in the stream or the booth's own screen reads. The codes are
drawn only once Windows confirms that; where it can't, the cards say to ask the staff. Every wait is
counted in elapsed seconds.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic as _monotonic
from typing import Any

from . import cloud, cloud_rounds

log = logging.getLogger(__name__)

LOOK_S = 2.0        # how often the season file is looked at for a round that has just ended
POLL_S = 5.0        # how often the cloud is asked who has claimed, while cards show
ASK_S = 60.0        # how long till the cloud is asked again whether this bay shows cards, after no answer
SHOW_S = 600.0      # the longest the cards stay up
CLAIMED_S = 15.0    # how long they stay once every player has claimed
MAX_CARDS = 8       # (more players than fit on a screen: the first ones by seat)


def claim_url(api: str, code: str) -> str:
    """What a card's QR opens: the claim page, on the site the PC is paired with."""
    return f"{api.rstrip('/')}/claim/{code}"


def matrix(text: str) -> list[list[bool]]:
    """A QR code for `text`, as rows of dark (True) and light modules, without its quiet border."""
    import segno

    return [[bool(cell) for cell in row] for row in segno.make(text, error="m", micro=False).matrix]


@dataclass
class Card:
    slot_id: str
    name: str
    url: str
    claimed: bool = False
    by: str | None = None    # the claimer's short name, as the cloud gives it


@dataclass
class Shown:
    """The cards on the screen: what the window draws from. `lock` guards `cards` (the booth's loop
    changes them, the window's thread reads them)."""
    uuid: str
    cards: list[Card]
    since: float
    all_claimed_at: float | None = None
    asked_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: threading.Event = field(default_factory=threading.Event)   # someone closed the window
    down: threading.Event = field(default_factory=threading.Event)     # the booth takes it down

    def snapshot(self) -> list[Card]:
        with self.lock:
            return [Card(c.slot_id, c.name, c.url, c.claimed, c.by) for c in self.cards]


def state(db: Any) -> tuple[set[str], set[str], bool]:
    """(the rounds that are over, everything in progress, whether a round is in progress), from the
    season file. In progress counts practice too: someone hitting balls is the next customer."""
    rows = db.execute("SELECT uuid, kind, state FROM round_records").fetchall()
    live = {r["uuid"] for r in rows if r["state"] == "in_progress"}
    return ({r["uuid"] for r in rows if r["kind"] == "round" and r["state"] != "in_progress"}, live,
            any(r["kind"] == "round" and r["state"] == "in_progress" for r in rows))


def cards_for(db: Any, uuid: str, api: str) -> list[Card]:
    """A card for each of the round's players, in seat order, each with its own code."""
    codes = cloud_rounds.claim_codes(db, uuid)
    slots = db.execute("SELECT slot_id, name FROM round_slots WHERE round_uuid = ? ORDER BY seat, slot_id",
                       (uuid,)).fetchall()
    return [Card(s["slot_id"], s["name"] or "Player", claim_url(api, codes[s["slot_id"]]))
            for s in slots if s["slot_id"] in codes][:MAX_CARDS]


def apply(shown: Shown, claims: Any) -> None:
    """Take the cloud's answer about who has claimed: `[{slot_id, claimed, name}]`. Anything else in
    the answer is ignored (it's shown on a screen: only a short name is taken, cut to fit)."""
    if not isinstance(claims, list):
        return
    got = {c.get("slot_id"): c for c in claims if isinstance(c, dict)}
    with shown.lock:
        for card in shown.cards:
            c = got.get(card.slot_id)
            if c is None:
                continue
            card.claimed = c.get("claimed") is True
            name = c.get("name")
            card.by = name.strip()[:40] if card.claimed and isinstance(name, str) and name.strip() else None


class Cards:
    """The booth's loops for the cards: one looks at the season file (`look`), one asks the cloud who
    has claimed (`ask`), so a slow answer never keeps a finished group's cards up. `cloud_of`: this
    PC's pairing (cloud_booth.Uploads._cloud: it raises once the stored pairing isn't the booth's).
    `key`: the pairing the season file must be bound to (as the uploader checks; None: not checked).
    `window`: what shows a `Shown` (tests pass their own)."""

    def __init__(self, db_path: str, api: str, cloud_of: Callable[[], Any], key: tuple | None = None,
                 where: Callable[[], tuple[int, int, int, int]] | None = None,
                 window: Callable[[Shown, Any], Any] | None = None) -> None:
        self.db_path, self.api, self.cloud_of, self.key, self.where = db_path, api, cloud_of, key, where
        self.window = window or (lambda shown, where: Window(shown, where).show())
        self.asked_at: float | None = None        # when the cloud was last asked whether this bay shows cards
        self.done: set[str] | None = None         # the rounds that were over at the last look (None: not looked yet)
        self.live: set[str] = set()               # and what was in progress
        self.pending: tuple[str, float] | None = None   # a round that has just ended, and when: its cards aren't up yet
        self.shown: Shown | None = None
        self.lock = threading.Lock()              # guards `shown` being put up and taken down
        self.stopped = threading.Event()          # the booth is stopping: nothing more goes up

    def _wanted(self, now: float) -> bool | None:
        """Whether the cloud says this bay shows cards, asked as each round ends (a tenant can become
        a venue while the booth runs). None: no answer (asked again after ASK_S)."""
        if self.asked_at is not None and now - self.asked_at < ASK_S:
            return None
        try:
            got = self.cloud_of().me().get("claim_cards")
        except cloud.PairingChanged:
            raise
        except Exception:   # noqa: BLE001 - the network, a refresh refused: asked again later
            log.debug("Shotcast Cloud: couldn't ask whether this bay shows claim cards", exc_info=True)
            self.asked_at = now
            return None
        self.asked_at = None
        return got is True

    def look(self, now: float) -> None:
        """One look at the season file: what has ended, what has started, whether the cards up now
        should come down. Nothing here waits on the cloud while cards show."""
        try:
            db = cloud_rounds.connect(self.db_path)
            try:
                if self.key is not None and cloud_rounds.bound(db) != self.key:
                    raise cloud_rounds.NotThisPairing(f"{self.db_path} isn't bound to this pairing; no claim cards")
                done, live, playing = state(db)
                newest = None
                if self.done is not None and done - self.done:
                    fresh = sorted(done - self.done)
                    # (Two at once doesn't happen on a bay; if it did, the one that started last.)
                    newest = db.execute("SELECT uuid FROM round_records WHERE uuid IN (%s) "
                                        "ORDER BY started_at DESC, uuid" % ",".join("?" * len(fresh)),
                                        fresh).fetchone()["uuid"]
            finally:
                db.close()
        except Exception:
            self.take_down()   # (what the bay is doing can't be told: no codes stay up meanwhile)
            raise
        started = live - self.live if self.done is not None else set()
        self.done, self.live = done, live
        shown = self.shown
        if shown is not None and (started or newest or playing or self._finished(shown, now)):
            self.take_down()
        if started or playing:
            self.pending = None            # someone is playing again: the round before gets no cards now
        elif newest:
            self.pending = (newest, now)
        if self.pending is not None:
            self._put_up(now)

    def _put_up(self, now: float) -> None:
        """The pending round's cards, once the cloud has said this bay shows them. It stays pending
        (tried again at the next look) while the cloud can't be asked or the season file is busy,
        for as long as cards would have shown."""
        assert self.pending is not None
        uuid, since = self.pending
        if now - since >= SHOW_S:
            self.pending = None
            return
        wanted = self._wanted(now)
        if wanted is None:
            return
        if not wanted:
            self.pending = None
            return
        db = cloud_rounds.connect(self.db_path)
        try:
            cards = cards_for(db, uuid, self.api)
        finally:
            db.close()
        self.pending = None
        if not cards:
            return
        with self.lock:
            if self.stopped.is_set():
                return
            self.shown = Shown(uuid, cards, since)
            log.info("Shotcast Cloud: claim cards up for %s player(s) of round %s", len(cards), uuid)
            self.window(self.shown, self.where)

    def _finished(self, shown: Shown, now: float) -> bool:
        return (shown.closed.is_set() or now - shown.since >= SHOW_S
                or (shown.all_claimed_at is not None and now - shown.all_claimed_at >= CLAIMED_S))

    def ask(self, now: float) -> None:
        """Ask the cloud who has claimed, if cards show and it's time to."""
        shown = self.shown
        if shown is None or (shown.asked_at is not None and now - shown.asked_at < POLL_S):
            return
        shown.asked_at = now
        try:
            r = self.cloud_of().request("GET", f"/api/devices/rounds/{shown.uuid}/claims")
            if r.status_code != 200:   # (404: the round hasn't been sent yet)
                return
            apply(shown, r.json())
        except cloud.PairingChanged:
            raise
        except Exception:   # noqa: BLE001 - the network, an answer that isn't JSON: asked again
            log.debug("Shotcast Cloud: couldn't ask who has claimed round %s", shown.uuid, exc_info=True)
            return
        if all(c.claimed for c in shown.snapshot()):
            shown.all_claimed_at = shown.all_claimed_at if shown.all_claimed_at is not None else now
        else:
            shown.all_claimed_at = None

    def take_down(self) -> None:
        with self.lock:
            if self.shown is not None:
                self.shown.down.set()
                self.shown = None

    async def _every(self, seconds: float, step: Callable[[float], None]) -> None:
        while True:
            try:
                await asyncio.to_thread(step, _monotonic())
            except (cloud.NotPaired, cloud.PairingChanged, cloud_rounds.NotThisPairing):
                return   # (the upload loop says why, in the log and the tray)
            except asyncio.CancelledError:
                raise
            except Exception:   # noqa: BLE001 - a busy season file: looked at again
                log.debug("Shotcast Cloud: claim cards: %s failed", step.__name__, exc_info=True)
            await asyncio.sleep(seconds)

    async def run(self) -> None:
        asking = asyncio.create_task(self._every(1.0, self.ask), name="cloud-cards-claims")
        try:
            await self._every(LOOK_S, self.look)
        finally:
            self.stopped.set()   # (a look still in its thread puts nothing up after this)
            self.take_down()
            asking.cancel()
            await asyncio.gather(asking, return_exceptions=True)


# ---- the window

WHITE = "#ffffff"
BLACK = "#000000"
TICK_GREEN = "#2e8b57"
REDRAW_MS = 500
QUIET = 4   # light modules around a QR code, which a scanner needs


class Window:
    """The cards over GSPro, along the bottom of its monitor. Tk runs on the window's own thread for
    its whole life, as popup.Question's does."""

    def __init__(self, shown: Shown, where: Callable[[], tuple[int, int, int, int]] | None = None) -> None:
        self.shown, self.where = shown, where

    def show(self) -> "Window":
        threading.Thread(target=self._run, name="claim-cards", daemon=True).start()
        return self

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("Claim cards window failed")
        finally:
            gc.collect()   # the Tk interpreter is freed here, on its own thread

    def _loop(self) -> None:
        import tkinter as tk

        from .overlay import _dpi_aware

        mon = None
        if self.where is not None:
            try:
                mon = self.where()
            except Exception:
                log.debug("Couldn't read GSPro's monitor for the claim cards", exc_info=True)
        if sys.platform == "win32":
            _dpi_aware()
        root = tk.Tk()
        try:
            self._show(tk, root, mon)
        finally:
            root.destroy()

    def _show(self, tk: Any, root: Any, mon: tuple[int, int, int, int] | None) -> None:
        from .overlay import ON_TEXT, PANEL, PANEL_EDGE, _no_activate, _topmost
        from .popup import MUTED, NO_GREY

        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        left, top, sw, sh = mon or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        cards = self.shown.snapshot()
        unit = max(12, round(sh * 0.018))
        side = min(round(sh * 0.2), (round(sw * 0.9) - unit * 4) // len(cards) - unit * 2)   # a QR's side
        frame = tk.Frame(root, bg=PANEL, highlightthickness=2, highlightbackground=PANEL_EDGE, padx=unit, pady=unit)
        frame.pack()
        tk.Label(frame, text="Keep this round", bg=PANEL, fg=ON_TEXT,
                 font=("Segoe UI", -round(unit * 1.4), "bold")).pack()
        tk.Label(frame, text="Scan your code with your phone's camera to add the round to your Shotcast account.",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", -unit)).pack(pady=(0, unit // 2))
        row = tk.Frame(frame, bg=PANEL)
        row.pack()
        drawn: list[tuple[Any, Any, tuple | None]] = []   # (canvas, the line under it, what it shows)
        for card in cards:
            cell = tk.Frame(row, bg=PANEL)
            cell.pack(side="left", padx=unit)
            tk.Label(cell, text=card.name, bg=PANEL, fg=ON_TEXT, font=("Segoe UI", -round(unit * 1.2), "bold"),
                     wraplength=side).pack()
            canvas = tk.Canvas(cell, width=side, height=side, bg=WHITE, highlightthickness=0)
            canvas.pack(pady=unit // 3)
            under = tk.Label(cell, text="", bg=PANEL, fg=MUTED, font=("Segoe UI", -unit), wraplength=side)
            under.pack()
            drawn.append((canvas, under, None))
        tk.Button(frame, text="Close", bg=NO_GREY, fg=ON_TEXT, activebackground=NO_GREY, activeforeground=ON_TEXT,
                  relief="flat", bd=0, padx=unit * 2, pady=unit // 3, cursor="hand2",
                  font=("Segoe UI", -unit, "bold"), command=self.shown.closed.set).pack(pady=(unit // 2, 0))

        hidden = False   # (set below, before any code is drawn: whether the window is out of screen capture)

        def draw(i: int, card: Card) -> None:
            canvas, under, was = drawn[i]
            now = (card.claimed, card.by)
            if was == now:
                return
            drawn[i] = (canvas, under, now)
            canvas.delete("all")
            if card.claimed:
                # (No code once it's claimed: it can't be used again.)
                canvas.configure(bg=PANEL)
                canvas.create_text(side // 2, side // 2, text="✓", fill=TICK_GREEN,
                                   font=("Segoe UI", -round(side * 0.6), "bold"))
                under.configure(text=f"Claimed by {card.by}" if card.by else "Claimed", fg=ON_TEXT)
                return
            if not hidden:
                # (A code a stream or a screen grab could carry is a code anyone watching could use.)
                canvas.configure(bg=PANEL)
                under.configure(text="Ask the staff for a code to keep this round", fg=ON_TEXT)
                return
            canvas.configure(bg=WHITE)
            rows = matrix(card.url)
            px = max(1, side // (len(rows) + 2 * QUIET))
            off = (side - px * len(rows)) // 2
            for y, cells in enumerate(rows):
                for x, dark in enumerate(cells):
                    if dark:
                        canvas.create_rectangle(off + x * px, off + y * px, off + (x + 1) * px, off + (y + 1) * px,
                                                fill=BLACK, outline="")
            under.configure(text="Scan to claim", fg=MUTED)

        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{left + (sw - w) // 2}+{top + sh - h - round(sh * 0.04)}")
        if self.shown.down.is_set():
            return
        # The window goes up empty, is taken out of screen capture, and only then are the codes drawn.
        root.deiconify()
        if sys.platform == "win32":
            root.update_idletasks()
            _no_activate(root)   # (never takes focus; kept out of screen capture)
            hidden = _out_of_capture(root)
            if not hidden:
                log.warning("Claim cards: this Windows can't keep a window out of screen capture; no codes shown")
        else:
            hidden = True   # (not a booth: only Windows runs one)
        for i, card in enumerate(cards):
            draw(i, card)
        ticks = 0
        born = _monotonic()

        def tick() -> None:
            nonlocal ticks
            # (Its own deadline too: it comes down even if the booth's loop has stopped looking.)
            if self.shown.down.is_set() or self.shown.closed.is_set() or _monotonic() - born >= SHOW_S + 30:
                root.quit()
                return
            for i, card in enumerate(self.shown.snapshot()):
                draw(i, card)
            ticks += 1
            if ticks % 2 == 0:   # GSPro taking focus can push it above a plain topmost window
                if sys.platform == "win32":
                    _topmost(root)
                else:
                    root.attributes("-topmost", True)
                    root.lift()
            root.after(REDRAW_MS, tick)

        root.after(0, tick)   # (from inside the loop: a quit before the loop runs would be lost)
        root.mainloop()


def _out_of_capture(root: Any) -> bool:
    """Whether Windows is keeping this window out of screen capture (overlay._hide_from_capture asked
    it to; Windows before 10 2004 can't)."""
    import ctypes
    from ctypes import wintypes

    from .overlay import _hwnd

    got = wintypes.DWORD(0)
    try:
        ok = ctypes.windll.user32.GetWindowDisplayAffinity(_hwnd(root), ctypes.byref(got))
    except Exception:
        log.debug("Couldn't read the claim cards window's display affinity", exc_info=True)
        return False
    return bool(ok) and got.value == 0x11   # WDA_EXCLUDEFROMCAPTURE
