"""A venue bay's claim cards (shotcast-cloud's docs/dev/claims.md): as a round ends, a card for each
player with a QR code. A customer scans theirs with their phone and the round is added to their
Shotcast account. Only where the cloud says the bay shows them (`claim_cards` in its answer about
this PC: a venue's bays), and only while the booth runs paired (cloud_booth starts it).

Each player's code is made on this PC and kept with the round (cloud_rounds.claim_codes); the round's
upload carries its hash, never the code. The cards go up the moment the round is over, usually
before the round has been sent: a code scanned early is kept by the cloud until the round arrives.
While they show, the cloud is asked who has claimed (`GET /api/devices/rounds/{id}/claims`) and a
claimed card says so, with the claimer's short name ("Mike R.").

They come down when someone closes them, when a round is in progress again, a while after every
player has claimed, or after SHOW_S. A round that was already over when the booth started gets no
cards: staff can make a fresh code on the website.

The window is the booth's own (popup.py's kind): it never takes focus from GSPro and is kept out of
screen capture, so the codes aren't in the stream or the booth's own screen reads. Every wait is
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


def over(db: Any) -> tuple[set[str], bool]:
    """(the rounds that are over, whether any round is in progress), from the season file."""
    rows = db.execute("SELECT uuid, state FROM round_records WHERE kind = 'round'").fetchall()
    return {r["uuid"] for r in rows if r["state"] != "in_progress"}, any(r["state"] == "in_progress" for r in rows)


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
    """The booth's loop for the cards. `cloud_of`: this PC's pairing (cloud_booth.Uploads._cloud:
    it raises once the stored pairing isn't the booth's). `window`: what shows a `Shown` (tests pass
    their own)."""

    def __init__(self, db_path: str, api: str, cloud_of: Callable[[], Any],
                 where: Callable[[], tuple[int, int, int, int]] | None = None,
                 window: Callable[[Shown, Any], Any] | None = None) -> None:
        self.db_path, self.api, self.cloud_of, self.where = db_path, api, cloud_of, where
        self.window = window or (lambda shown, where: Window(shown, where).show())
        self.wanted: bool | None = None     # whether the cloud says this bay shows cards (None: not known yet)
        self.asked_at: float | None = None
        self.seen: set[str] | None = None   # the rounds that were over at the last look (None: not looked yet)
        self.shown: Shown | None = None

    def _wanted(self, now: float) -> bool:
        if self.wanted is None and (self.asked_at is None or now - self.asked_at >= ASK_S):
            self.asked_at = now
            try:
                self.wanted = self.cloud_of().me().get("claim_cards") is True
            except cloud.PairingChanged:
                raise
            except Exception:   # noqa: BLE001 - the network, a refresh refused: asked again later
                log.debug("Shotcast Cloud: couldn't ask whether this bay shows claim cards", exc_info=True)
        return bool(self.wanted)

    def look(self, now: float) -> None:
        """One look at the season file and, while cards show, at the cloud."""
        if not self._wanted(now):
            return
        db = cloud_rounds.connect(self.db_path)
        try:
            done, playing = over(db)
            fresh = done - self.seen if self.seen is not None else set()
            self.seen = done
            if self.shown is not None and (playing or fresh or self._finished(self.shown, now)):
                self.take_down()
            if fresh and not playing:
                # (Two at once doesn't happen on a bay; if it did, the one that started last.)
                uuid = db.execute("SELECT uuid FROM round_records WHERE uuid IN (%s) ORDER BY started_at DESC, uuid"
                                  % ",".join("?" * len(fresh)), sorted(fresh)).fetchone()["uuid"]
                cards = cards_for(db, uuid, self.api)
                if cards:
                    self.shown = Shown(uuid, cards, now)
                    log.info("Shotcast Cloud: claim cards up for %s player(s) of round %s", len(cards), uuid)
                    self.window(self.shown, self.where)
        finally:
            db.close()
        if self.shown is not None:
            self._ask(self.shown, now)

    def _finished(self, shown: Shown, now: float) -> bool:
        return (shown.closed.is_set() or now - shown.since >= SHOW_S
                or (shown.all_claimed_at is not None and now - shown.all_claimed_at >= CLAIMED_S))

    def _ask(self, shown: Shown, now: float) -> None:
        if shown.asked_at is not None and now - shown.asked_at < POLL_S:
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
        if self.shown is not None:
            self.shown.down.set()
            self.shown = None

    async def run(self) -> None:
        try:
            while True:
                try:
                    await asyncio.to_thread(self.look, _monotonic())
                except (cloud.NotPaired, cloud.PairingChanged, cloud_rounds.NotThisPairing):
                    return   # (the upload loop says why, in the log and the tray)
                except asyncio.CancelledError:
                    raise
                except Exception:   # noqa: BLE001 - a busy season file: looked at again
                    log.debug("Shotcast Cloud: claim cards: look failed", exc_info=True)
                await asyncio.sleep(LOOK_S)
        finally:
            self.take_down()


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

        from .overlay import ON_TEXT, PANEL, PANEL_EDGE, _dpi_aware, _no_activate, _topmost
        from .popup import MUTED, NO_GREY

        mon = None
        if self.where is not None:
            try:
                mon = self.where()
            except Exception:
                log.debug("Couldn't read GSPro's monitor for the claim cards", exc_info=True)
        if sys.platform == "win32":
            _dpi_aware()
        root = tk.Tk()
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

        for i, card in enumerate(cards):
            draw(i, card)
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        root.geometry(f"+{left + (sw - w) // 2}+{top + sh - h - round(sh * 0.04)}")
        root.deiconify()
        if sys.platform == "win32":
            root.update_idletasks()
            _no_activate(root)   # (never takes focus; kept out of screen capture)
        ticks = 0

        def tick() -> None:
            nonlocal ticks
            if self.shown.down.is_set() or self.shown.closed.is_set():
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

        tick()
        root.mainloop()
        root.destroy()
