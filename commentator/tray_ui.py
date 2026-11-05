"""The Windows app's windows: message boxes for what the tray menu does, and the Settings window.

Tk runs on one thread of its own (pystray has the main one); everything else hands it work with
`Ui.call`. Settings are saved into config.yaml and .env with small line edits, so their comments
and anything not shown here stay as they were.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import queue
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

import yaml

from . import gspro_dir
from .settings_files import env_value, set_env, set_yaml

log = logging.getLogger(__name__)

TITLE = "Shotcast"
KEYS_HELP = "https://github.com/jjmerri/gspro-shotcast/blob/main/docs/setup.md"


class Ui:
    """A Tk root (hidden) on its own thread. Without tkinter, messages fall back to `fallback`."""

    def __init__(self, fallback: Callable[[str], None] = log.info) -> None:
        self._q: queue.Queue = queue.Queue()
        self._fallback = fallback
        self._ready = threading.Event()
        self.root = None
        self.scale = 1.0
        self.settings_window: SettingsWindow | None = None
        threading.Thread(target=self._run, name="ui", daemon=True).start()
        self._ready.wait(10)

    def _run(self) -> None:
        try:
            import tkinter as tk

            if os.name == "nt":
                from .overlay import _dpi_aware

                _dpi_aware()   # sharp text at 150%/200% display scaling (Tk then sizes its fonts for it)
            self.root = tk.Tk()
            self.root.withdraw()
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
            self.root.title(TITLE)
            self._icon = tk.PhotoImage(data=_logo_png(64))
            self.root.iconphoto(True, self._icon)
        except Exception:
            log.warning("No windows (tkinter didn't start); messages go to the tray log", exc_info=True)
            self.root = None
            self._ready.set()
            return
        self._ready.set()
        self.root.after(100, self._pump)
        self.root.mainloop()

    def _pump(self) -> None:
        while True:
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("A window failed")
        self.root.after(100, self._pump)

    def px(self, n: int) -> int:
        """n pixels at 100% display scaling, in this screen's pixels."""
        return round(n * self.scale)

    @property
    def ok(self) -> bool:
        return self.root is not None

    def call(self, fn: Callable[[], Any]) -> None:
        if self.ok:
            self._q.put(fn)

    def _box(self, kind: str, text: str, title: str) -> Any:
        from tkinter import messagebox

        self.root.attributes("-topmost", True)
        try:
            return getattr(messagebox, kind)(title, text, parent=self.root)
        finally:
            self.root.attributes("-topmost", False)

    def info(self, text: str, title: str = TITLE) -> None:
        log.info("Notice: %s", text)
        if self.ok:
            self.call(lambda: self._box("showinfo", text, title))
        else:
            self._fallback(text)

    def code(self, text: str, code: str, after: str = "", title: str = TITLE) -> None:
        """`text`, a code to pass on in big letters with a button that copies it (message boxes
        can't be selected), then `after`."""
        log.info("Notice: %s %s %s", text, code, after)
        if self.ok:
            self.call(lambda: self._code_box(text, code, after, title))
        else:
            self._fallback(f"{text} {code}\n\n{after}")

    def _code_box(self, text: str, code: str, after: str, title: str) -> Any:
        import tkinter as tk
        from tkinter import ttk

        w = tk.Toplevel(self.root)
        w.title(title)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        f = ttk.Frame(w, padding=self.px(14))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=text, wraplength=self.px(380), justify="left").pack(anchor="w")
        ttk.Label(f, text=code, font=("Consolas", 18, "bold")).pack(pady=self.px(10))
        if after:
            ttk.Label(f, text=after, wraplength=self.px(380), justify="left").pack(anchor="w")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(self.px(12), 0))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.root.update()
            copy_btn.configure(text="Copied")

        ttk.Button(bar, text="OK", command=w.destroy).pack(side="right")
        copy_btn = ttk.Button(bar, text="Copy code", command=copy)
        copy_btn.pack(side="right", padx=self.px(6))
        w.update_idletasks()
        w.geometry(f"+{max(0, (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2)}"
                   f"+{max(0, (w.winfo_screenheight() - w.winfo_reqheight()) // 3)}")
        w.focus_force()
        return copy

    def error(self, text: str, title: str = TITLE) -> None:
        log.warning("Error shown: %s", text)
        if self.ok:
            self.call(lambda: self._box("showerror", text, title))
        else:
            self._fallback(text)

    def ask(self, text: str, title: str = TITLE) -> bool:
        """Yes or no, waiting for the answer (call it from any thread but Tk's)."""
        if not self.ok:
            self._fallback(text)
            return False
        done, answer = threading.Event(), []

        def go():
            try:
                answer.append(self._box("askyesno", text, title))
            finally:
                done.set()
        self.call(go)
        done.wait()
        return bool(answer and answer[0])

    def countdown(self, text: str, seconds: int, go: str, wait: str, title: str = TITLE) -> bool:
        """`text` (its "{s}" the seconds left) counting down on top of everything, waiting for it: True
        when it ran out or `go` was pressed, False for `wait` or the window closed. Call it from any
        thread but Tk's; without windows it's False."""
        log.info("Countdown shown: %s", text.replace("{s}", str(seconds)))
        if not self.ok:
            return False
        done, answer = threading.Event(), []

        def finish(value: bool, w) -> None:
            if not done.is_set():
                answer.append(value)
                done.set()
                w.destroy()

        def show():
            import tkinter as tk
            from tkinter import ttk

            w = tk.Toplevel(self.root)
            w.title(title)
            w.resizable(False, False)
            w.attributes("-topmost", True)
            w.protocol("WM_DELETE_WINDOW", lambda: finish(False, w))
            f = ttk.Frame(w, padding=self.px(14))
            f.pack(fill="both", expand=True)
            label = ttk.Label(f, wraplength=self.px(380), justify="left")
            label.pack(anchor="w")
            bar = ttk.Frame(f)
            bar.pack(fill="x", pady=(self.px(12), 0))
            ttk.Button(bar, text=wait, command=lambda: finish(False, w)).pack(side="right")
            ttk.Button(bar, text=go, command=lambda: finish(True, w)).pack(side="right", padx=self.px(6))

            def tick(left: int) -> None:
                if done.is_set():
                    return
                if left <= 0:
                    finish(True, w)
                    return
                label.configure(text=text.replace("{s}", str(left)))
                w.after(1000, tick, left - 1)
            tick(seconds)
            w.update_idletasks()
            w.geometry(f"+{max(0, (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2)}"
                       f"+{max(0, (w.winfo_screenheight() - w.winfo_reqheight()) // 3)}")
            w.focus_force()

        def go_safely():
            try:
                show()
            except Exception:
                done.set()
                raise
        self.call(go_safely)
        done.wait(seconds + 30)   # a Tk that's stopped answering can't hold the tray up
        return bool(answer and answer[0])

    def open_settings(self, tray, tab: str | None = None) -> None:
        def go():
            if self.settings_window is not None and self.settings_window.alive():
                self.settings_window.show(tab)
            else:
                self.settings_window = SettingsWindow(self, tray, tab)
        if self.ok:
            self.call(go)
        else:
            from .tray import edit_file

            edit_file(tray.home / "config.yaml")


def _logo_png(size: int) -> str:
    from .tray import _logo

    buf = io.BytesIO()
    _logo(size).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---- checking keys ---------------------------------------------------------------------------

def _http_check(method: str, url: str, **kw) -> str:
    import httpx

    try:
        r = httpx.request(method, url, timeout=20, **kw)
    except httpx.HTTPError as e:
        return f"Couldn't reach it: {e}"
    if r.status_code < 300:
        return ""
    if r.status_code in (401, 403):
        return "It was turned down: check the key (and, for Google, that the Text-to-Speech API is on)."
    return f"It answered {r.status_code}: {r.text[:200]}"


def check_key(name: str, values: dict[str, str], cfg) -> str:
    """Try a key against its service. '' if it works, else what went wrong."""
    v = values.get(name, "").strip()
    if name == "AWS_ACCESS_KEY_ID":
        secret = values.get("AWS_SECRET_ACCESS_KEY", "").strip()
        if not (v and secret):
            return "Fill in both AWS keys first."
        try:
            import boto3

            boto3.client("sts", aws_access_key_id=v, aws_secret_access_key=secret,
                         region_name=cfg.publish.region or "us-east-1").get_caller_identity()
            return ""
        except Exception as e:
            return f"AWS turned them down: {e}"
    if not v:
        return "It's empty."
    if name == "OPENAI_API_KEY":
        return _http_check("GET", "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {v}"})
    if name == "ANTHROPIC_API_KEY":
        return _http_check("GET", "https://api.anthropic.com/v1/models",
                           headers={"x-api-key": v, "anthropic-version": "2023-06-01"})
    if name == "GOOGLE_TTS_API_KEY":
        return _http_check("GET", "https://texttospeech.googleapis.com/v1/voices",
                           params={"languageCode": "en-US", "key": v})
    if name == "OBS_WEBSOCKET_PASSWORD":
        from .live_stream import Obs

        try:
            Obs(cfg.live.obs_host, cfg.live.obs_port, password=v).live()
            return ""
        except Exception as e:
            return str(e)
    if name == "NTFY_TOPIC":
        server = (getattr(cfg.notify, "server", None) or "https://ntfy.sh").rstrip("/")
        return _http_check("POST", f"{server}/{v}", content="Test from Shotcast settings: pushes reach this phone.".encode(),
                           headers={"Title": "Shotcast"})
    return "There's no check for this one."


# ---- the Settings window ---------------------------------------------------------------------

# (tab, label, config keys, kind, note). Kinds: bool, text, int, float, list (comma-separated),
# choice:<a>|<b> (an empty choice means unset), file, and heading (a section title; no keys).
# Feature settings only: screen calibration, timings and the per-role models stay in config.yaml.
CONFIG_FIELDS: list[tuple[str, str, tuple[str, ...], str, str]] = [
    ("Booth", "AI commentary", ("commentary", "enabled"), "bool",
     "The booth talks through each shot (needs API keys). Off: shots and rounds are still recorded."),
    ("Booth", "ON AIR badge over GSPro", ("overlay", "enabled"), "bool",
     "Click the badge to take the booth off air and back on."),
    ("Booth", "Badge size", ("overlay", "scale"), "float", "1.0 is the standard size."),
    ("Booth", "Language", ("commentary", "language"), "choice:en|fr|es|de|it|pt|nl",
     "What the booth speaks: en English, fr French, es Spanish, de German, it Italian, pt Portuguese, nl Dutch."),
    ("Booth", "Play-by-play commentator", ("personas", "pbp", "name"), "text", ""),
    ("Booth", "Colour commentator", ("personas", "color", "name"), "text", ""),
    ("Booth", "Voices", ("tts", "provider"), "choice:openai|google",
     "openai uses OPENAI_API_KEY; google uses GOOGLE_TTS_API_KEY."),
    ("Booth", "Speaking pace (OpenAI voices)", ("tts", "speed"), "float", "1.0 is normal; 1.1 a little quicker."),
    ("Booth", "Speaking pace (Google voices)", ("tts", "google_speaking_rate"), "float", "0.25 to 2.0; 1.0 is normal."),
    ("Booth", "What the booth calls", (), "heading", ""),
    ("Booth", "Opener at the first tee", ("commentary", "opener"), "bool", ""),
    ("Booth", "Early opener from GSPro's setup screen", ("commentary", "preround"), "bool",
     "Writes the opener while the course loads. Needs the setup-screen regions calibrated (calibrate --out setup)."),
    ("Booth", "Sign-off when the round ends", ("commentary", "closer"), "bool", ""),
    ("Booth", "Between-holes wrap", ("commentary", "segments_on_hole_change"), "bool", ""),
    ("Booth", "Lines while a player sets up", ("commentary", "preshot"), "bool", ""),
    ("Booth", "Share of setups that get a line", ("commentary", "preshot_rate"), "float", "0 to 1."),
    ("Booth", "Calls while the ball is in the air", ("commentary", "in_flight"), "bool", ""),
    ("Booth", "Instant calls when the ball stops", ("commentary", "pre_reactions"), "bool",
     "Written at setup, so routine shots get a call with no wait."),
    ("Booth", "Chance of a line on an ordinary shot", ("commentary", "routine_comment_rate"), "float", "0 to 1."),
    ("Booth", "Words per line", ("commentary", "max_words_per_line"), "int", ""),
    ("Booth", "Block repeated phrases", ("commentary", "block_repeats"), "bool",
     "Rewrites a line that reuses a phrase said earlier this season."),
    ("Booth", "Lag putt call from (feet)", ("commentary", "gimme_lag_feet"), "float",
     "A putt from this far that stops inside the gimme circle gets a word. 0: off."),
    ("Booth", "Round-form stats from (holes in)", ("commentary", "round_form_after_holes"), "int",
     "Three-putts, greens hit and the like, when well off a player's norm. 0: off."),
    ("Asides", "Golf news", (), "heading", ""),
    ("Asides", "Talk about golf headlines", ("news", "enabled"), "bool", "Real, current headlines from the feeds below."),
    ("Asides", "News feeds", ("news", "feeds"), "list", "RSS or Atom feed addresses, separated by commas."),
    ("Asides", "News asides per nine", ("news", "per_nine"), "int", ""),
    ("Asides", "Skip stories older than (days)", ("news", "max_age_days"), "float", ""),
    ("Asides", "Chance of an aside on a quiet shot", ("news", "banter_rate"), "float",
     "0 to 1. Shared by news, scores and course trivia."),
    ("Asides", "Weather", (), "heading", ""),
    ("Asides", "Mention the weather outside", ("weather", "enabled"), "bool", ""),
    ("Asides", "Location", ("weather", "location"), "text", "A town or postal code, like 03290 or Nottingham, NH."),
    ("Asides", "Country code", ("weather", "country"), "text", "Like US or GB; empty searches everywhere."),
    ("Asides", "Place name on air", ("weather", "place"), "text", "Empty: the town the location is in."),
    ("Asides", "Between-holes weather chance", ("weather", "between_holes_rate"), "float", "0 to 1."),
    ("Asides", "Live scores", (), "heading", ""),
    ("Asides", "Tour and local team scores", ("scores", "enabled"), "bool", ""),
    ("Asides", "Tours", ("scores", "tours"), "list", "pga, lpga, eur, champions-tour."),
    ("Asides", "Local teams", ("scores", "teams"), "list", "Names or ESPN abbreviations, like Bruins, Celtics, Red Sox."),
    ("Asides", "Score asides per nine", ("scores", "per_nine"), "int", ""),
    ("Asides", "Course trivia", (), "heading", ""),
    ("Asides", "Stories about the real course", ("trivia", "enabled"), "bool", "From the course's Wikipedia article."),
    ("Asides", "Trivia per round", ("trivia", "per_round"), "int", ""),
    ("Chat", "Live chat on the stats page", ("chat", "enabled"), "bool",
     "Viewers chat on the Live tab and the booth talks about it. Open only while the booth is on air in a round."),
    ("Chat", "Chat table", ("chat", "table"), "text", "The site stack's ChatTable output (docs/tracking.md#live-chat)."),
    ("Chat", "Moderation", ("chat", "moderation"), "choice:off|standard|strict",
     "off: trash talk and swearing are fair game. standard: no swearing or personal attacks. strict: only "
     "clean, kind messages. Links, spam and slurs never air."),
    ("Chat", "At most once a hole", ("chat", "once_per_hole"), "bool", ""),
    ("Chat", "At least this far apart (seconds)", ("chat", "min_gap_seconds"), "float", ""),
    ("Chat", "Words per line", ("chat", "max_words"), "int", ""),
    ("Chat", "Ignore messages older than (seconds)", ("chat", "max_age_seconds"), "float", ""),
    ("Chat", "Phone alerts", ("chat", "alerts"), "bool",
     "Alerts on phones that turned them on from the stats page: live, rounds, records, recaps (docs/tracking.md#alerts)."),
    ("GSPro", "Shot data from", ("source", "type"), "choice:connector_log|gspro_log|gspro_sniffer",
     "connector_log: GSPconnect's log (Uneekor and the other built-in connectors). gspro_log: GSPro's own log, "
     "any launch monitor, ball data only. gspro_sniffer: Open Connect traffic (needs Npcap)."),
    ("GSPro", "Hazard read before a shot", ("minimap", "provider"), "choice:llm|jev",
     "llm: the pre-shot model reads the screenshot. jev: TypeSafe's Jev reads the minimap (TYPESAFE_API_KEY)."),
    ("GSPro", "Read the break on putts", ("putt_read", "enabled"), "bool", "Needs GSPro's green grid showing."),
    ("League", "League file", ("league_file",), "file", "Players, handicaps and the schedule (optional)."),
    ("League", "Tonight's format", ("commentary", "round_format"), "choice:|scramble|best_ball|aggregate|alternate",
     "Empty: from the league schedule."),
    ("League", "Schedule week", ("commentary", "round_week"), "int",
     "Forces a week's format, nine and CTP holes when the course can't be matched. Empty: matched by course."),
    ("League", "Holes in a round", ("commentary", "round_holes"), "int", ""),
    ("League", "Live handicaps from Squabbit", ("squabbit", "enabled"), "bool", ""),
    ("League", "Season starts (month-day)", ("publish", "season_start"), "text",
     "Like 10-01 for a season from October: the stats page's records get a This season range. Empty: none."),
    ("League", "SuperSaaS bookings", ("booking", "enabled"), "bool", "Practice bookings make a round practice."),
    ("League", "SuperSaaS account", ("booking", "account"), "text", ""),
    ("League", "SuperSaaS schedule ID", ("booking", "schedule_id"), "int", ""),
    ("League", "Booking slack (minutes)", ("booking", "slack_minutes"), "float",
     "A round this close before or after a booking still counts as in it."),
    ("League", "Schedule tab days", ("booking", "schedule_days"), "int",
     "Bookings shown on the stats page's Schedule tab. 0: none."),
    ("Sharing", "Put the stats page online after each round", ("publish", "enabled"), "bool",
     "Needs the S3 bucket from docs/tracking.md#sharing-the-stats-page."),
    ("Sharing", "S3 bucket", ("publish", "bucket"), "text", ""),
    ("Sharing", "AWS region", ("publish", "region"), "text", ""),
    ("Sharing", "Publish as soon as a round ends", ("publish", "after_round"), "bool", ""),
    ("Sharing", "Site name", ("publish", "site_name"), "text", "The stats page's title and header."),
    ("Sharing", "Last tab's name", ("publish", "more_tab"), "text", ""),
    ("Sharing", "Backups", (), "heading", ""),
    ("Sharing", "Back up the season file", ("backup", "enabled"), "bool", "When the booth starts and after each round."),
    ("Sharing", "Backup folder", ("backup", "dir"), "text",
     "Empty: logs\\backups. A synced or other drive keeps them safe if this one dies."),
    ("Sharing", "Daily copies kept", ("backup", "daily"), "int", ""),
    ("Sharing", "Weekly copies kept", ("backup", "weekly"), "int", ""),
    ("Sharing", "Copy backups to the S3 bucket too", ("backup", "s3"), "bool", ""),
    ("Video", "Live stream on the stats page", ("live", "enabled"), "bool",
     "OBS streams each round to the page's Live tab: set it up per docs/tracking.md#live-stream."),
    ("Video", "Stream with each round", ("live", "with_rounds"), "bool",
     "Start OBS's output when a round starts and stop it after the sign-off."),
    ("Video", "Stop after the sign-off (seconds)", ("live", "stop_after_seconds"), "float", ""),
    ("Video", "Stop after no shots for (minutes)", ("live", "idle_minutes"), "float", "0: never."),
    ("Video", "Start OBS with the booth", ("live", "launch_obs"), "bool", ""),
    ("Video", "OBS program", ("live", "obs_path"), "text", "Empty: the one Shotcast set up, else Program Files."),
    ("Video", "GSPro's sound device", ("live", "game_sound_device"), "text",
     "The Windows output GSPro plays to, for its sound in the stream (docs/tracking.md#game-sound). Empty: none."),
    ("Video", "Game sound level (dB)", ("live", "game_sound_db"), "float", "Below the voices, like -10."),
    ("Video", "Keep round videos online (days)", ("live", "keep_days"), "int",
     "For the page's Round replays tab; about 1.2 GB an hour in the bucket. 0: not kept."),
    ("Video", "Keep round videos on this PC (days)", ("live", "local_keep_days"), "int",
     "Each stream OBS records (the tray's \"Set up the live stream\") as a .ts file; 0: not kept. "
     "About 1.2 GB an hour."),
    ("Video", "Recordings folder", ("live", "local_dir"), "text",
     "In the Shotcast folder unless it's a full path (like D:\\Golf videos)."),
    ("Alerts", "Health checks", ("health", "enabled"), "bool",
     "Warns on the ON AIR badge when the booth stops reading GSPro the way it should."),
    ("Alerts", "Phone pushes", ("notify", "provider"), "choice:ntfy|none", "ntfy sends them to NTFY_TOPIC."),
    ("Alerts", "Push levels", ("notify", "levels"), "list", "fail, warn."),
    ("Alerts", "Push when it's all cleared", ("notify", "cleared"), "bool", ""),
    ("Alerts", "Don't repeat a push for (hours)", ("notify", "repeat_hours"), "float", ""),
    ("Advanced", "Shotcast's CPU priority", ("process_priority",), "choice:below_normal|normal|idle",
     "below_normal keeps GSPro smooth."),
    ("Advanced", "CPU cores for Shotcast", ("max_cpu_cores",), "int", "Empty: all of them."),
]

# (tab, label, .env key, secret, checkable)
ENV_FIELDS: list[tuple[str, str, str, bool, bool]] = [
    ("API keys", "OpenAI (voices, live lines)", "OPENAI_API_KEY", True, True),
    ("API keys", "Anthropic (Claude)", "ANTHROPIC_API_KEY", True, True),
    ("API keys", "Google Text-to-Speech", "GOOGLE_TTS_API_KEY", True, True),
    ("API keys", "TypeSafe (Jev minimap read)", "TYPESAFE_API_KEY", True, False),
    ("League", "Squabbit email", "SQUABBIT_EMAIL", False, False),
    ("League", "Squabbit password", "SQUABBIT_PASSWORD", True, False),
    ("League", "SuperSaaS API key", "SUPERSAAS_API_KEY", True, False),
    ("Sharing", "AWS access key ID", "AWS_ACCESS_KEY_ID", False, True),
    ("Sharing", "AWS secret access key", "AWS_SECRET_ACCESS_KEY", True, False),
    ("Alerts", "ntfy topic (phone pushes)", "NTFY_TOPIC", True, True),
    ("Video", "OBS WebSocket password (live stream)", "OBS_WEBSOCKET_PASSWORD", True, True),
]

TABS = ["Booth", "Asides", "Chat", "API keys", "GSPro", "League", "Sharing", "Video", "Alerts", "Advanced"]


def _get(data: Any, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


def config_value(cfg, keys: tuple[str, ...]) -> Any:
    obj = cfg
    for i, k in enumerate(keys):
        if isinstance(obj, dict):
            if k not in obj and keys[:i] == ("personas",):
                obj = type(cfg)().personas   # a commentator config.yaml leaves out: their default
            obj = obj.get(k)
        else:
            obj = getattr(obj, k)
    return obj


def field_text(kind: str, value: Any) -> str:
    """How a setting's value shows in its box."""
    if value is None:
        return ""
    if kind == "list":
        return ", ".join(str(v) for v in value)
    return str(value)


def parse_field(kind: str, label: str, text: str, current: Any) -> Any:
    """A box's text as the setting's value. Raises ValueError, naming the setting, if it doesn't read."""
    if kind == "bool":
        return bool(text)
    v = text.strip()
    if kind == "list":
        return [x.strip() for x in v.split(",") if x.strip()]
    if kind in ("int", "float"):
        if not v:
            return None
        try:
            return int(v) if kind == "int" else float(v)
        except ValueError:
            raise ValueError(f"{label} should be a {'whole ' if kind == 'int' else ''}number") from None
    if kind.startswith("choice:"):
        return v or None
    return v or (None if current is None else "")


def apply_changes(config_text: str, env_text: str, config_changes: dict[tuple[str, ...], Any],
                  env_changes: dict[str, str], gspro: str | None) -> tuple[str, str]:
    """The new config.yaml and .env texts. Raises ValueError if the config wouldn't load."""
    from .config import Config

    if any(keys[0] == "personas" for keys in config_changes):
        # A name alone isn't a persona, and a personas block replaces the defaults whole: a commentator
        # config.yaml leaves out gets their default written in full first.
        have = (yaml.safe_load(config_text) or {}).get("personas")
        have = have if isinstance(have, dict) else {}
        for role, persona in Config().personas.items():
            if not have or (role not in have and any(k[:2] == ("personas", role) for k in config_changes)):
                for field, value in persona.model_dump().items():
                    config_text = set_yaml(config_text, ("personas", role, field), value)
    for keys, value in config_changes.items():
        config_text = set_yaml(config_text, keys, value)
    if gspro is not None:
        config_text = gspro_dir.apply(config_text, gspro)
    try:
        Config.model_validate(yaml.safe_load(config_text) or {})
    except Exception as e:
        raise ValueError(f"Those settings don't fit together: {e}") from e
    for key, value in env_changes.items():
        env_text = set_env(env_text, key, value)
    return config_text, env_text


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".saving")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SettingsWindow:
    def __init__(self, ui: Ui, tray, tab: str | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        from .config import load_config

        self.ui, self.tray, self.home = ui, tray, tray.home
        self.cfg = load_config(self.home / "config.yaml")
        env_path = self.home / ".env"
        self.env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        self.win = w = tk.Toplevel(ui.root)
        w.title("Shotcast settings")
        w.minsize(self._px(560), self._px(420))
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.vars: dict[Any, Any] = {}
        self.orig: dict[Any, Any] = {}
        self.labels: dict[Any, str] = {}
        self.nb = ttk.Notebook(w)
        self.nb.pack(fill="both", expand=True, padx=self._px(10), pady=(self._px(10), 0))
        self.frames = {}
        bg = ttk.Style().lookup("TFrame", "background") or None
        for name in TABS:
            self.nb.add(self._scrolling(name, bg), text=name)
        self.rows = {name: 0 for name in TABS}

        self._gspro_tab()
        for tab_name, label, keys, kind, note in CONFIG_FIELDS:
            self._config_row(tab_name, label, keys, kind, note)
        self._note("API keys", "Keys stay on this PC, in .env in the Shotcast folder. "
                               "Check tries one against its service.")
        for tab_name, label, key, secret, checkable in ENV_FIELDS:
            self._env_row(tab_name, label, key, secret, checkable)
        self._link("API keys", "Where to get them", KEYS_HELP)
        self._advanced_tab()

        bar = ttk.Frame(w, padding=self._px(10))
        bar.pack(fill="x")
        ttk.Button(bar, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=self._px(6))
        w.update_idletasks()
        w.geometry(f"+{max(0, (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2)}"
                   f"+{max(0, (w.winfo_screenheight() - w.winfo_reqheight()) // 3)}")
        self.show(tab)

    # -- layout helpers
    def _scrolling(self, name: str, bg: str | None):
        """A tab whose rows scroll when they don't fit; its rows go in self.frames[name]."""
        import tkinter as tk
        from tkinter import ttk

        outer = ttk.Frame(self.nb)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, width=self._px(600), height=self._px(460),
                           **({"background": bg} if bg else {}))
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        f = ttk.Frame(canvas, padding=self._px(12))
        f.columnconfigure(1, weight=1)
        item = canvas.create_window((0, 0), window=f, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        f.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))

        def wheel(e):
            if canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        outer.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", wheel))
        outer.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.frames[name] = f
        return outer

    def _px(self, n: int) -> int:
        return self.ui.px(n)

    def _next(self, tab: str) -> int:
        r = self.rows[tab]
        self.rows[tab] += 1
        return r

    def _note(self, tab: str, text: str) -> None:
        from tkinter import ttk

        ttk.Label(self.frames[tab], text=text, wraplength=self._px(520), foreground="#555").grid(
            row=self._next(tab), column=0, columnspan=4, sticky="w", pady=(0, self._px(8)))

    def _link(self, tab: str, text: str, url: str) -> None:
        from tkinter import ttk

        lbl = ttk.Label(self.frames[tab], text=text, foreground="#0a58ca", cursor="hand2")
        lbl.grid(row=self._next(tab), column=0, columnspan=4, sticky="w", pady=(self._px(8), 0))
        lbl.bind("<Button-1>", lambda _: webbrowser.open(url))

    def _config_row(self, tab: str, label: str, keys: tuple[str, ...], kind: str, note: str) -> None:
        import tkinter as tk
        from tkinter import filedialog, ttk

        f, r = self.frames[tab], self._next(tab)
        if kind == "heading":
            ttk.Label(f, text=label, font=("Segoe UI", 10, "bold")).grid(
                row=r, column=0, columnspan=4, sticky="w", pady=(self._px(14), self._px(2)))
            return
        value = config_value(self.cfg, keys)
        if kind == "list" and not isinstance(value, list):
            # Set per league in config.yaml ({nhl: [BOS]}): shown, but only editable there.
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self._px(4), padx=(0, self._px(8)))
            ttk.Label(f, text=f"{value} (edit in config.yaml)", foreground="#555").grid(row=r, column=1, sticky="w")
            return
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(f, text=label, variable=var).grid(row=r, column=0, columnspan=3, sticky="w", pady=(self._px(6), 0))
        else:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self._px(4), padx=(0, self._px(8)))
            var = tk.StringVar(value=field_text(kind, value))
            if kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split("|")
                if var.get() and var.get() not in options:
                    options.append(var.get())
                ttk.Combobox(f, textvariable=var, values=options, state="readonly", width=20).grid(
                    row=r, column=1, sticky="w")
            else:
                ttk.Entry(f, textvariable=var).grid(row=r, column=1, sticky="ew")
            if kind == "file":
                def browse(var=var):
                    path = filedialog.askopenfilename(parent=self.win, initialdir=str(self.home),
                                                      filetypes=[("League file", "*.json *.yaml *.yml"), ("All", "*.*")])
                    if path:
                        p = Path(path).resolve()
                        try:
                            p = p.relative_to(self.home.resolve())
                        except ValueError:
                            pass
                        var.set(str(p))
                ttk.Button(f, text="Browse...", command=browse).grid(row=r, column=2, padx=(self._px(6), 0))
        if note:
            ttk.Label(f, text=note, wraplength=self._px(500), foreground="#555").grid(
                row=self._next(tab), column=0, columnspan=4, sticky="w", padx=(self._px(22) if kind == "bool" else 0, 0))
        self.vars[keys] = (var, kind)
        self.orig[keys] = var.get()
        self.labels[keys] = label

    def _env_row(self, tab: str, label: str, key: str, secret: bool, checkable: bool) -> None:
        import tkinter as tk
        from tkinter import ttk

        f, r = self.frames[tab], self._next(tab)
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self._px(4), padx=(0, self._px(8)))
        var = tk.StringVar(value=env_value(self.env_text, key))
        entry = ttk.Entry(f, textvariable=var, show="\u2022" if secret else "")
        entry.grid(row=r, column=1, sticky="ew")
        col = 2
        if secret:
            shown = tk.BooleanVar(value=False)
            ttk.Checkbutton(f, text="Show", variable=shown,
                            command=lambda: entry.configure(show="" if shown.get() else "\u2022")).grid(
                row=r, column=col, padx=(self._px(6), 0))
            col += 1
        if checkable:
            ttk.Button(f, text="Send test" if key == "NTFY_TOPIC" else "Check",
                       command=lambda: self._check(key)).grid(row=r, column=col, padx=(self._px(6), 0))
        self.vars[key] = (var, "env")
        self.orig[key] = var.get()

    def _check(self, key: str) -> None:
        values = {k: v[0].get() for k, v in self.vars.items() if isinstance(k, str)}

        def work():
            problem = check_key(key, values, self.cfg)
            if problem:
                self.ui.error(f"{key}: {problem}")
            else:
                self.ui.info(f"{key} works." if key != "NTFY_TOPIC" else "Sent: check your phone.")
        threading.Thread(target=work, daemon=True).start()

    def _gspro_tab(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, ttk

        f, tab = self.frames["GSPro"], "GSPro"
        self._note(tab, "The folder GSPro is installed in (the one with GSPLauncher.exe; usually C:\\GSProV1). "
                        "Shotcast reads GSPconnect's shot log and the course files from it.")
        r = self._next(tab)
        current = gspro_dir.folder_of(self.cfg.source.connector_log) or ""
        self.gspro = tk.StringVar(value=current)
        self.gspro_orig = current
        ttk.Label(f, text="GSPro folder").grid(row=r, column=0, sticky="w", padx=(0, self._px(8)))
        ttk.Entry(f, textvariable=self.gspro).grid(row=r, column=1, sticky="ew")

        def browse():
            path = filedialog.askdirectory(parent=self.win, initialdir=self.gspro.get() or "C:\\")
            if path:
                self.gspro.set(str(Path(path)))
        ttk.Button(f, text="Browse...", command=browse).grid(row=r, column=2, padx=(self._px(6), 0))
        self.gspro_status = ttk.Label(f, text="", wraplength=self._px(500))
        self.gspro_status.grid(row=self._next(tab), column=0, columnspan=4, sticky="w", pady=(self._px(6), 0))

        def find():
            self.gspro_status.configure(text="Looking...")

            def work():
                found = gspro_dir.find()
                self.ui.call(lambda: (self.gspro.set(found) if found else None, self._gspro_note(found is None)))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Find GSPro", command=find).grid(row=self._next(tab), column=0, sticky="w", pady=(self._px(6), 0))
        self.gspro.trace_add("write", lambda *_: self._gspro_note())
        self._gspro_note()

    def _gspro_note(self, not_found: bool = False) -> None:
        folder = self.gspro.get().strip()
        if not_found:
            text = "Couldn't find GSPro on this PC: browse to its folder."
        elif not folder:
            text = "Choose GSPro's folder (Find GSPro or Browse)."
        elif gspro_dir.looks_like_gspro(folder):
            text = "\u2714 GSPro is in this folder."
        else:
            text = "\u26a0 There's no GSPro in this folder (Core\\GSP or Core\\GSProPublicBeta)."
        self.gspro_status.configure(text=text)

    def _advanced_tab(self) -> None:
        from tkinter import ttk

        from .tray import edit_file, open_file

        f, tab = self.frames["Advanced"], "Advanced"
        self._note(tab, "Everything else is in config.yaml (each setting is explained there). "
                        "Save or cancel this window before editing the files by hand.")
        for text, fn in (("Edit config.yaml", lambda: edit_file(self.home / "config.yaml")),
                         ("Edit .env (API keys)", lambda: edit_file(self.home / ".env")),
                         ("Open the Shotcast folder", lambda: open_file(self.home)),
                         ("Open logs", lambda: open_file(self.home / "logs"))):
            ttk.Button(f, text=text, command=fn, width=28).grid(row=self._next(tab), column=0, sticky="w", pady=self._px(3))

    # -- window
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def show(self, tab: str | None = None) -> None:
        if tab in TABS:
            self.nb.select(TABS.index(tab))
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.after(300, lambda: self.win.attributes("-topmost", False))
        self.win.focus_force()

    def close(self) -> None:
        self.win.destroy()
        self.ui.settings_window = None

    def changes(self) -> tuple[dict, dict, str | None]:
        config_changes, env_changes = {}, {}
        for k, (var, kind) in self.vars.items():
            v = var.get()
            if v == self.orig[k]:
                continue
            if kind == "env":
                env_changes[k] = v.strip()
            else:
                config_changes[k] = parse_field(kind, self.labels[k], v, config_value(self.cfg, k))
        gspro = self.gspro.get().strip()
        if not gspro and self.gspro_orig:
            raise ValueError("The GSPro folder is empty: choose GSPro's folder (or Cancel to keep it as it was).")
        return config_changes, env_changes, gspro if gspro and not gspro_dir.same(gspro, self.gspro_orig) else None

    def save(self) -> None:
        from tkinter import messagebox

        try:
            config_changes, env_changes, gspro = self.changes()
            if not (config_changes or env_changes or gspro):
                self.close()
                return
            config_path, env_path = self.home / "config.yaml", self.home / ".env"
            config_text, env_text = apply_changes(config_path.read_text(encoding="utf-8"),
                                                  env_path.read_text(encoding="utf-8") if env_path.exists() else "",
                                                  config_changes, env_changes, gspro)
        except ValueError as e:
            messagebox.showerror("Shotcast settings", str(e), parent=self.win)
            return
        write_atomic(config_path, config_text)
        write_atomic(env_path, env_text)
        for key, value in env_changes.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        if any(k.startswith("AWS_") for k in env_changes):
            try:
                import boto3

                boto3.DEFAULT_SESSION = None   # Back up now's S3 upload picks up the new keys
            except ImportError:
                pass
        if gspro:
            from .tray import remember_gspro_dir

            remember_gspro_dir(gspro)
        log.info("Settings saved: %s", ", ".join([".".join(k) for k in config_changes] + list(env_changes)
                                                  + (["GSPro folder"] if gspro else [])))
        self.tray.settings_saved()
        self.close()
        if self.tray.booth.state in ("stopped", "crashed"):
            return   # it reads them when it next starts
        if self.tray.round_in_progress() is False:
            # Known to be between rounds: nothing to interrupt.
            log.info("Settings saved between rounds: restarting the booth to use them")
            threading.Thread(target=self.tray.restart_booth, daemon=True).start()
            return
        if messagebox.askyesno("Shotcast settings", "Saved. The booth uses them once it restarts, and a round "
                               "may be in progress.\n\nRestart the booth now? (It takes a few seconds; "
                               "No keeps the round going, and they apply at the next restart.)",
                               parent=self.ui.root):
            threading.Thread(target=self.tray.restart_booth, daemon=True).start()
