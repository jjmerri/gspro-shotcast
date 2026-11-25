"""A venue bay's claim card (shotcast-cloud's docs/dev/claims.md): as a round ends, one QR code in
a corner of the screen. A customer scans it with their phone, picks which player they
are, and the round is added to their Shotcast account. Only where the cloud says the bay shows it
(`claim_cards` in its answer about this PC: a venue's bays), and only while the booth runs paired
(cloud_booth starts it).

Each player has a code of their own, made on this PC and kept with the round
(cloud_rounds.claim_codes); the round's upload carries each code's hash, never the code. The one QR
is a link to the site's "which player are you?" page with every player's name and code after its
`#` (claim_link): that part of an address stays on the phone, so the names and codes reach the
cloud only when a player picks theirs. The card goes up the moment the round is over, usually before
the round has been sent: a code used early is kept by the cloud until the round arrives. While it
shows, the cloud is asked who has claimed (`GET /api/devices/rounds/{id}/claims`) and a claimed
player is ticked, with the claimer's short name ("Mike R.").

It comes down when someone closes it, when anyone plays again (a round or practice), a while after
every player has claimed, after SHOW_S, or when the season file can't be read (what the bay is doing
can't be told). A round that was already over when the booth started gets none: staff can make a
fresh code on the website.

The window is small and sits at the bottom right, over GSPro's minimap, so the scorecard GSPro
shows as a round ends stays readable; it can be dragged anywhere, and the next round's comes up where
the last was left. It is the booth's own (popup.py's kind): it never takes focus from GSPro and is kept out of
screen capture, so the code isn't in the stream or the booth's own screen reads. It's drawn only
once Windows confirms that; where it can't, the card says to ask the staff. Every wait is counted in
elapsed seconds.
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
from urllib.parse import quote

from . import cloud, cloud_rounds

log = logging.getLogger(__name__)

LOOK_S = 2.0        # how often the season file is looked at for a round that has just ended
POLL_S = 5.0        # how often the cloud is asked who has claimed, while cards show
ASK_S = 60.0        # how long till the cloud is asked again whether this bay shows cards, after no answer
SHOW_S = 600.0      # the longest the cards stay up
CLAIMED_S = 15.0    # how long they stay once every player has claimed
STALE_S = 30.0      # a round found over after the season file went unread this long gets no cards
MAX_PLAYERS = 12    # (more than that: the first ones by seat. The QR would be too fine to scan.)
NAME_BYTES = 36     # how much of a player's name goes in the link, as it's written there (percent-encoded:
                    # a letter outside English is 6 to 12 there, so this is four Korean syllables, six Cyrillic letters)
NAME_CUTS = (NAME_BYTES, 24, 16, 10, 6)   # tried in turn, till the link fits; then no names at all
LINK_BYTES = 550    # the longest link a QR is drawn for: an 89-module code at most
STATUS_CHARS = 44   # how much of the claimers' names the card's one status line lists
MODULE_PX = 4       # the finest a QR's modules are drawn (the code is made bigger on a small screen to keep it)


def labels(names: list[str]) -> list[str]:
    """What each player is called on the card and on the phone, in seat order: their name ("Player"
    for none) and, with more than one player, their seat's number: "Mike (1)", "Ann (2)",
    "Mike (3)". Every seat is numbered so that no two players ever read the same, whatever they
    typed and however much of a name the link has room for."""
    shown = [" ".join("".join(ch if ch.isprintable() else " " for ch in n).split()) or "Player" for n in names]
    return [f"{name} ({i})" for i, name in enumerate(shown, 1)] if len(shown) > 1 else shown


def _in_link(label: str, seat: int | None, most: int) -> str:
    """A label cut for the link: as much of its name as takes `most` characters once
    percent-encoded, and the seat number after it whole (it isn't counted against the name's room:
    the name is what a player knows themselves by)."""
    tail = f" ({seat})" if seat and label.endswith(f" ({seat})") else ""
    name = label.removesuffix(tail)
    while len(quote(name, safe="")) > most:
        name = name[:-1]
    return name + tail if name else ""   # (no room for any of it: the page says "Player 3")


def claim_link(api: str, players: list[tuple[str, str]]) -> str:
    """What the QR opens: the site's "which player are you?" page, with each player's (label, code)
    after the `#` as `<code>=<name>&...`. A browser keeps that part to itself, so the page (the
    cloud's claim.js) reads it on the phone and links each name to `/claim/<code>`. Names are cut,
    more for more players, so the link stays short enough to scan (LINK_BYTES); with no room for
    any, the codes go alone and the page calls them "Player 1", "Player 2"."""
    base = f"{api.rstrip('/')}/claim/#"
    seats = range(1, len(players) + 1) if len(players) > 1 else [None]
    for most in (*NAME_CUTS, 0):
        link = base + "&".join(f"{code}={quote(_in_link(name, seat, most), safe='()')}" if most else f"{code}="
                               for (name, code), seat in zip(players, seats))
        if len(link) <= LINK_BYTES:
            break
    return link


def code_side(screen_height: int, modules: int) -> int:
    """How many pixels across the QR's square is drawn: about a fifth of the screen's height (small
    enough to sit in a corner, clear of GSPro's scorecard), and more if that's what it takes for
    MODULE_PX a module (its light border counted)."""
    return min(max(round(screen_height * 0.2), MODULE_PX * (modules + 2 * QUIET)), round(screen_height * 0.9))


def matrix(text: str) -> list[list[bool]]:
    """A QR code for `text`, as rows of dark (True) and light modules, without its quiet border."""
    import segno

    return [[bool(cell) for cell in row] for row in segno.make(text, error="m", micro=False).matrix]


@dataclass
class Card:
    slot_id: str
    name: str
    code: str
    claimed: bool = False
    by: str | None = None    # the claimer's short name, as the cloud gives it


@dataclass
class Shown:
    """The cards on the screen: what the window draws from. `lock` guards `cards` (the booth's loop
    changes them, the window's thread reads them)."""
    uuid: str
    cards: list[Card]
    since: float
    url: str = ""            # what the QR opens (claim_link)
    all_claimed_at: float | None = None
    asked_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: threading.Event = field(default_factory=threading.Event)   # someone closed the window
    down: threading.Event = field(default_factory=threading.Event)     # the booth takes it down

    def snapshot(self) -> list[Card]:
        with self.lock:
            return [Card(c.slot_id, c.name, c.code, c.claimed, c.by) for c in self.cards]


def state(db: Any) -> tuple[set[str], set[str], bool, set[str]]:
    """(the rounds that are over, what is being played, whether a round is in progress, every
    round, and every practice record with a shot in it), from the season file. Being played: a round
    in progress, and practice once a shot has been hit in it (someone hitting balls is the next
    customer; a practice record with no shot is only where a late line of commentary was filed)."""
    rows = db.execute("SELECT r.uuid, r.kind, r.state, EXISTS (SELECT 1 FROM shots s WHERE s.record_uuid = r.uuid) "
                      "AS hit FROM round_records r").fetchall()
    live = {r["uuid"] for r in rows if r["state"] == "in_progress" and (r["kind"] == "round" or r["hit"])}
    return ({r["uuid"] for r in rows if r["kind"] == "round" and r["state"] != "in_progress"}, live,
            any(r["kind"] == "round" and r["state"] == "in_progress" for r in rows),
            {r["uuid"] for r in rows if r["kind"] == "round" or r["hit"]})


def cards_for(db: Any, uuid: str) -> list[Card]:
    """A card for each of the round's players, in seat order, each with its own code."""
    codes = cloud_rounds.claim_codes(db, uuid)
    slots = db.execute("SELECT slot_id, name FROM round_slots WHERE round_uuid = ? ORDER BY seat, slot_id",
                       (uuid,)).fetchall()
    if any(s["slot_id"] not in codes for s in slots):
        raise RuntimeError("a player joined as the codes were made")   # (tried again at the next look)
    slots = slots[:MAX_PLAYERS]
    return [Card(s["slot_id"], label, codes[s["slot_id"]])
            for s, label in zip(slots, labels([s["name"] or "" for s in slots]))]


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
            name = "".join(ch for ch in name if ch.isprintable()).strip()[:40] if isinstance(name, str) else ""
            card.by = name if card.claimed and name else None


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
        self.live: set[str] = set()               # and what was being played
        self.known: set[str] = set()              # and everything that had been played
        self.pending: tuple[str, float] | None = None   # a round that has just ended, and when: its cards aren't up yet
        self.wanted_for: str | None = None        # the pending round the cloud has said yes for
        self.looked_at: float | None = None       # the last look that could read the season file
        self.shown: Shown | None = None
        self.lock = threading.Lock()              # guards `shown` being put up and taken down
        self.stopped = threading.Event()          # the booth is stopping: nothing more goes up

    def _wanted(self, now: float) -> bool | None:
        """Whether the cloud says this bay shows cards, asked as each round ends (a tenant can become
        a venue while the booth runs). None: no answer, or not one that says (asked again after ASK_S)."""
        if self.asked_at is not None and now - self.asked_at < ASK_S:
            return None
        try:
            got = self.cloud_of().me().get("claim_cards")
        except (cloud.NotPaired, cloud.PairingChanged):
            raise
        except Exception:   # noqa: BLE001 - the network, a refresh refused: asked again later
            log.debug("Shotcast Cloud: couldn't ask whether this bay shows claim cards", exc_info=True)
            got = None
        self.asked_at = None if isinstance(got, bool) else now
        return got if isinstance(got, bool) else None

    def look(self, now: float) -> None:
        """One look at the season file: what has ended, what has started, whether the cards up now
        should come down. Nothing here waits on the cloud while cards show."""
        try:
            db = cloud_rounds.connect(self.db_path)
            try:
                if self.key is not None and cloud_rounds.bound(db) != self.key:
                    raise cloud_rounds.NotThisPairing(f"{self.db_path} isn't bound to this pairing; no claim cards")
                done, live, playing, known = state(db)
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
        ended = bool(newest)
        # (A round that ended while the file couldn't be read ended nobody knows when: no cards.)
        if newest and (self.looked_at is None or now - self.looked_at > STALE_S):
            newest = None
        self.done, self.live, self.known, self.looked_at = done, live, known, now
        shown = self.shown
        if shown is not None and (started or ended or playing or self._finished(shown, now)):
            self.take_down()
        if started or playing or ended:
            # Someone is playing again, or has played since (a round found over, even one too late
            # for a card of its own): the round before gets no card now.
            self.pending = None
        if newest and not (started or playing):
            self.pending, self.wanted_for = (newest, now), None
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
        if self.wanted_for != uuid:
            wanted = self._wanted(now)   # (may wait on the network: everything after is checked afresh)
            if wanted is None:
                return
            if not wanted:
                self.pending = None
                return
            self.wanted_for = uuid
        db = cloud_rounds.connect(self.db_path)
        try:
            # On the connection the codes are read from, now: still this pairing's file, the round
            # still over, and nobody playing since the look.
            if self.key is not None and cloud_rounds.bound(db) != self.key:
                raise cloud_rounds.NotThisPairing(f"{self.db_path} isn't bound to this pairing; no claim cards")
            cards = cards_for(db, uuid)   # (first: making the codes can wait on the booth's own writes)
            # Then, last of all: still over, and nobody has played since the look (anything played
            # that wasn't there then, even if they've stopped).
            done, live, _, known = state(db)
            if uuid not in done or live - self.live or known - self.known:
                self.pending = None
                return
        finally:
            db.close()
        self.pending = None
        if not cards:
            return
        with self.lock:
            if self.stopped.is_set():
                return
            self.shown = Shown(uuid, cards, since, claim_link(self.api, [(c.name, c.code) for c in cards]))
            log.info("Shotcast Cloud: claim card up for %s player(s) of round %s", len(cards), uuid)
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
        except (cloud.NotPaired, cloud.PairingChanged):
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
        loops = [asyncio.create_task(self._every(LOOK_S, self.look), name="cloud-cards-look"),
                 asyncio.create_task(self._every(1.0, self.ask), name="cloud-cards-claims")]
        try:
            # (Either ends only when the pairing isn't the booth's any more: then both do.)
            await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stopped.set()   # (a look still in its thread puts nothing up after this)
            self.take_down()
            for t in loops:
                t.cancel()
            await asyncio.gather(*loops, return_exceptions=True)


# ---- the window

WHITE = "#ffffff"
BLACK = "#000000"
TICK_GREEN = "#2e8b57"
REDRAW_MS = 500
QUIET = 4   # light modules around a QR code, which a scanner needs


_dragged_to: tuple[int, int] | None = None   # where on its monitor someone dragged the card to (till Shotcast restarts)


class Window:
    """The cards over GSPro, along the bottom of its monitor. Tk runs on the window's own thread for
    its whole life, as popup.Question's does."""

    def __init__(self, shown: Shown, where: Callable[[], tuple[int, int, int, int]] | None = None) -> None:
        self.shown, self.where = shown, where
        self.code_drawn = False                              # whether the QR was painted (not the staff message)
        self.rect: tuple[int, int, int, int] | None = None   # where the window is on the desktop (left, top, w, h)

    def show(self) -> "Window":
        threading.Thread(target=self._run, name="claim-cards", daemon=True).start()
        return self

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("Claim cards window failed")
        finally:
            self.shown.closed.set()   # (however it went: the booth's loop sees the card isn't up)
            gc.collect()   # the Tk interpreter is freed here, on its own thread

    def _loop(self) -> None:
        import tkinter as tk

        from .overlay import _dpi_aware, _end_loop

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
            _end_loop(root)

    def _show(self, tk: Any, root: Any, mon: tuple[int, int, int, int] | None) -> None:
        from .overlay import ON_TEXT, PANEL, PANEL_EDGE, _end_loop, _no_activate, _topmost
        from .popup import MUTED, NO_GREY

        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        left, top, sw, sh = mon or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        cards = self.shown.snapshot()
        unit = max(12, round(sh * 0.02))
        try:
            code: list[list[bool]] | None = matrix(self.shown.url)
        except Exception:   # (a link no QR holds: claim_link keeps it far shorter)
            log.exception("Claim card: couldn't make the QR")
            code = None
        # A small card: the code, a line saying what it's for and one saying who has claimed. The
        # players' names are on the phone, where they're picked; the screen is left to GSPro's scorecard.
        side = code_side(sh, len(code) if code else 0)
        frame = tk.Frame(root, bg=PANEL, highlightthickness=2, highlightbackground=PANEL_EDGE,
                         padx=unit // 2, pady=unit // 2)
        frame.pack()

        def close() -> None:
            self.shown.closed.set()
            _end_loop(root)
        head = tk.Frame(frame, bg=PANEL)
        head.pack(fill="x")
        title = tk.Label(head, text="Keep this round", bg=PANEL, fg=ON_TEXT, anchor="w",
                         font=("Segoe UI", -round(unit * 1.2), "bold"))
        title.pack(side="left")
        tk.Button(head, text="\u2715", bg=NO_GREY, fg=ON_TEXT, activebackground=NO_GREY, activeforeground=ON_TEXT,
                  relief="flat", bd=0, padx=unit // 2, cursor="hand2", font=("Segoe UI", -unit, "bold"),
                  command=close).pack(side="right")
        canvas = tk.Canvas(frame, width=side, height=side, bg=PANEL, highlightthickness=0)
        canvas.pack(pady=(unit // 3, unit // 3))
        status = tk.Label(frame, text="", bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=side,
                          font=("Segoe UI", -unit))
        status.pack(fill="x")
        said: list[str] = []   # what the status line says now

        def draw_status(cards: list[Card]) -> None:
            got = [c for c in cards if c.claimed]
            if not got:
                text, colour = "Scan with your phone's camera, then pick your name.", MUTED
            elif len(got) == len(cards):
                text, colour = "\u2713 Everyone has claimed this round.", TICK_GREEN
            else:
                # (The count, and as many of the names as fit two short lines: the card stays small.)
                names: list[str] = []
                for c in got:
                    if len(", ".join([*names, c.by or c.name])) > STATUS_CHARS:
                        names.append("\u2026")
                        break
                    names.append(c.by or c.name)
                text, colour = f"\u2713 {len(got)} of {len(cards)} claimed: " + ", ".join(names), TICK_GREEN
            if said != [text]:
                said[:] = [text]
                status.configure(text=text, fg=colour)
                place()

        def draw_code(hidden: bool) -> None:
            if not hidden or code is None:
                # (A code a stream or a screen grab could carry is a code anyone watching could use.)
                canvas.create_text(side // 2, side // 2, width=side - unit, fill=ON_TEXT, justify="center",
                                   text="Ask the staff for a code to keep this round",
                                   font=("Segoe UI", -round(unit * 1.1), "bold"))
                return
            rows = code or []
            self.code_drawn = True
            canvas.configure(bg=WHITE)
            px = max(1, side // (len(rows) + 2 * QUIET))
            off = (side - px * len(rows)) // 2
            for y, cells in enumerate(rows):
                for x, dark in enumerate(cells):
                    if dark:
                        canvas.create_rectangle(off + x * px, off + y * px, off + (x + 1) * px, off + (y + 1) * px,
                                                fill=BLACK, outline="")

        def place(to: tuple[int, int] | None = None) -> None:
            # Bottom right (over GSPro's minimap, beside its scorecard) till someone drags it; then
            # where it was dragged to, this card and the next ones. Always wholly on the monitor.
            global _dragged_to
            root.update_idletasks()
            w, h = root.winfo_reqwidth(), root.winfo_reqheight()
            if to is not None:
                _dragged_to = (to[0] - left, to[1] - top)
            x, y = (sw - w - round(sw * 0.02), sh - h - round(sh * 0.04)) if _dragged_to is None else _dragged_to
            x, y = left + max(0, min(x, sw - w)), top + max(0, min(y, sh - h))
            root.geometry(f"+{x}+{y}")
            self.rect = (x, y, w, h)

        grip: list[int] = []   # where on the card the mouse took hold of it

        def take(e: Any) -> None:
            grip[:] = [e.x_root - root.winfo_x(), e.y_root - root.winfo_y()]

        def drag(e: Any) -> None:
            if grip:
                place((e.x_root - grip[0], e.y_root - grip[1]))
        for part in (frame, head, title, canvas, status):   # (anywhere but the close button)
            part.bind("<ButtonPress-1>", take)
            part.bind("<B1-Motion>", drag)
            part.configure(cursor="fleur")

        place()
        if self.shown.down.is_set():
            return
        # The window goes up without the code, is taken out of screen capture, and only then is it drawn.
        root.deiconify()
        if sys.platform == "win32":
            root.update_idletasks()
            _no_activate(root)   # (never takes focus; kept out of screen capture)
            hidden = _out_of_capture(root)
            if not hidden:
                log.warning("Claim card: this Windows can't keep a window out of screen capture; no code shown")
        else:
            hidden = True   # (not a booth: only Windows runs one)
        draw_code(hidden)
        draw_status(cards)
        ticks = 0
        until = self.shown.since + SHOW_S + 2 * LOOK_S   # (the booth's own clock: Shown.since is on it)

        def tick() -> None:
            nonlocal ticks
            try:
                # (Its own deadline too: it comes down even if the booth's loop has stopped looking.)
                if self.shown.down.is_set() or self.shown.closed.is_set() or _monotonic() >= until:
                    _end_loop(root)
                    return
                draw_status(self.shown.snapshot())
                ticks += 1
                if ticks % 2 == 0:   # GSPro taking focus can push it above a plain topmost window
                    if sys.platform == "win32":
                        _topmost(root)
                    else:
                        root.attributes("-topmost", True)
                        root.lift()
                root.after(REDRAW_MS, tick)
            except Exception:
                # (Tk goes on after an error in a callback: the codes must not stay up with nothing redrawing.)
                log.exception("Claim cards window failed; taken down")
                _end_loop(root)

        root.after(0, tick)
        root.mainloop()


def _out_of_capture(root: Any) -> bool:
    """Ask Windows to keep this window out of screen capture, and whether it said it would (as
    overlay._hide_from_capture asks, which doesn't say; Windows before 10 2004 can't)."""
    import ctypes

    from .overlay import _hwnd

    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(_hwnd(root), 0x11))   # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        log.debug("Couldn't keep the claim cards window out of screen capture", exc_info=True)
        return False
