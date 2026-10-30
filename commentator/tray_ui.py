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

# (tab, label, config keys, kind, note). Kinds: bool, text, int, choice:<a>|<b>, file.
CONFIG_FIELDS: list[tuple[str, str, tuple[str, ...], str, str]] = [
    ("Booth", "AI commentary", ("commentary", "enabled"), "bool",
     "The booth talks through each shot (needs API keys). Off: shots and rounds are still recorded."),
    ("Booth", "ON AIR badge over GSPro", ("overlay", "enabled"), "bool",
     "Click the badge to take the booth off air and back on."),
    ("Booth", "Voices", ("tts", "provider"), "choice:openai|google",
     "openai uses OPENAI_API_KEY; google uses GOOGLE_TTS_API_KEY."),
    ("League", "League file", ("league_file",), "file", "Players, handicaps and the schedule (optional)."),
    ("League", "Live handicaps from Squabbit", ("squabbit", "enabled"), "bool", ""),
    ("League", "SuperSaaS bookings", ("booking", "enabled"), "bool", "Practice bookings make a round practice."),
    ("League", "SuperSaaS account", ("booking", "account"), "text", ""),
    ("League", "SuperSaaS schedule ID", ("booking", "schedule_id"), "int", ""),
    ("Sharing", "Put the stats page online after each round", ("publish", "enabled"), "bool",
     "Needs the S3 bucket from docs/tracking.md#sharing-the-stats-page."),
    ("Sharing", "S3 bucket", ("publish", "bucket"), "text", ""),
    ("Sharing", "AWS region", ("publish", "region"), "text", ""),
    ("Sharing", "Copy backups to the S3 bucket too", ("backup", "s3"), "bool", ""),
    ("Sharing", "Live stream on the stats page", ("live", "enabled"), "bool",
     "OBS streams each round to the page's Live tab: set it up per docs/tracking.md#live-stream."),
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
    ("Sharing", "ntfy topic (phone pushes)", "NTFY_TOPIC", True, True),
    ("Sharing", "OBS WebSocket password (live stream)", "OBS_WEBSOCKET_PASSWORD", True, True),
]

TABS = ["Booth", "API keys", "GSPro", "League", "Sharing", "Advanced"]


def _get(data: Any, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


def config_value(cfg, keys: tuple[str, ...]) -> Any:
    obj = cfg
    for k in keys:
        obj = getattr(obj, k)
    return obj


def apply_changes(config_text: str, env_text: str, config_changes: dict[tuple[str, ...], Any],
                  env_changes: dict[str, str], gspro: str | None) -> tuple[str, str]:
    """The new config.yaml and .env texts. Raises ValueError if the config wouldn't load."""
    from .config import Config

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
        self.nb = ttk.Notebook(w)
        self.nb.pack(fill="both", expand=True, padx=self._px(10), pady=(self._px(10), 0))
        self.frames = {}
        for name in TABS:
            f = ttk.Frame(self.nb, padding=self._px(12))
            f.columnconfigure(1, weight=1)
            self.nb.add(f, text=name)
            self.frames[name] = f
        self.rows = {name: 0 for name in TABS}

        for tab_name, label, keys, kind, note in CONFIG_FIELDS:
            self._config_row(tab_name, label, keys, kind, note)
        self._note("API keys", "Keys stay on this PC, in .env in the Shotcast folder. "
                               "Check tries one against its service.")
        for tab_name, label, key, secret, checkable in ENV_FIELDS:
            self._env_row(tab_name, label, key, secret, checkable)
        self._link("API keys", "Where to get them", KEYS_HELP)
        self._gspro_tab()
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
        value = config_value(self.cfg, keys)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(f, text=label, variable=var).grid(row=r, column=0, columnspan=3, sticky="w", pady=(self._px(6), 0))
        else:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=self._px(4), padx=(0, self._px(8)))
            var = tk.StringVar(value="" if value is None else str(value))
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
            text = "\u26a0 There's no Core\\GSP\\GSPro.exe in this folder."
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
            elif kind == "bool":
                config_changes[k] = bool(v)
            elif kind == "int":
                v = v.strip()
                if v and not v.isdigit():
                    raise ValueError(f"{k[-1]} should be a number")
                config_changes[k] = int(v) if v else None
            else:
                config_changes[k] = v.strip() or (None if config_value(self.cfg, k) is None else "")
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
        if messagebox.askyesno("Shotcast settings", "Saved. Restart the booth now to use them?\n\n"
                               "(It takes a few seconds; not in the middle of a shot.)", parent=self.ui.root):
            threading.Thread(target=self.tray.restart_booth, daemon=True).start()
