"""Sets OBS up for the live stream (docs/tracking.md#live-stream), from the tray: downloads OBS's
portable build into the Shotcast folder (its settings stay in there, apart from any other OBS),
writes its settings so its recording output is the HLS stream the booth mirrors, and then, with OBS
running, adds a Game Capture of GSPro over obs-websocket.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable

from . import tls
from .live_stream import PORTABLE_OBS, Obs, ObsError, launch_obs

log = logging.getLogger(__name__)

OBS_VERSION = "33.0.0"
OBS_ZIP_URL = (f"https://github.com/obsproject/obs-studio/releases/download/{OBS_VERSION}/"
               f"OBS-Studio-{OBS_VERSION}-Windows-x64.zip")
OBS_ZIP_SHA256 = "3369a3ecd67b5669a8a08d77357f87cc786d1fba42fbbd1054d841a69f6271a7"
OBS_ZIP_MB = 224
FOLDER = "obs"
NAME = "Shotcast"
SOURCE = "GSPro"
GSPRO_WINDOW = "GSPro:UnityWndClass:GSpro.exe"   # title:class:exe; OBS matches the exe first
GAME_SOUND = "Game sound"
OUTPUT_CAPTURE = "wasapi_output_capture"
VERSION_FILE = "shotcast-obs-version.txt"
# As OBS writes it: MAKE_SEMANTIC_VERSION(33, 0, 0). Set, OBS skips its first-run wizard.
LAST_VERSION = 33 << 24
CANVAS_WIDTH, STREAM_WIDTH, FPS = 1920, 1280, 30
# HLS next to the playlist: 4 s segments, 5 in the playlist, a segment named only once it's whole.
HLS_MUXER = "hls_time=4 hls_list_size=5 hls_flags=delete_segments+temp_file"


def obs_folder(home: str | Path) -> Path:
    return Path(home) / FOLDER


def portable_exe(home: str | Path) -> Path:
    return Path(home) / PORTABLE_OBS


def installed(home: str | Path) -> bool:
    folder = obs_folder(home)
    try:
        return portable_exe(home).exists() and (folder / VERSION_FILE).read_text().strip() == OBS_VERSION
    except OSError:
        return False


def download(home: str | Path, url: str = OBS_ZIP_URL, sha256: str = OBS_ZIP_SHA256,
             opener: Callable = tls.urlopen) -> Path:
    """OBS's portable build into <home>/obs, checked against its published SHA-256. The debug
    symbols (.pdb, over half the size) are left out."""
    home = Path(home)
    part = home / "obs-download.zip.part"
    digest = hashlib.sha256()
    try:
        with opener(url, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
        if digest.hexdigest() != sha256:
            raise RuntimeError("The OBS download didn't match its published checksum; try again.")
        new = home / (FOLDER + ".new")
        shutil.rmtree(new, ignore_errors=True)
        with zipfile.ZipFile(part) as z:
            z.extractall(new, [n for n in z.namelist() if not n.lower().endswith(".pdb")])
        (new / VERSION_FILE).write_text(OBS_VERSION)
    finally:
        part.unlink(missing_ok=True)
    folder = obs_folder(home)
    if folder.exists():
        keep = folder / "config"   # an earlier setup's scenes and settings
        if keep.exists():
            shutil.move(str(keep), str(new / "config"))
        shutil.rmtree(folder)
    os.replace(new, folder)
    return folder


def _ini(path: Path, values: dict[str, dict[str, object]]) -> None:
    """Set these keys in one of OBS's .ini files, keeping the rest. OBS writes Key=Value, with a BOM."""
    ini = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    ini.optionxform = str   # OBS's keys are case-sensitive
    if path.exists():
        ini.read(path, encoding="utf-8-sig")
    for section, keys in values.items():
        if not ini.has_section(section):
            ini.add_section(section)
        for k, v in keys.items():
            ini.set(section, k, str(int(v)) if isinstance(v, bool) else str(v))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        ini.write(f, space_around_delimiters=False)


def video_encoder() -> tuple[str, str]:
    """(FFmpeg encoder, its options): the NVIDIA chip if there is one, so GSPro keeps the processor."""
    nvidia = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
    return ("h264_nvenc", "preset=p4") if nvidia.exists() else ("libx264", "preset=veryfast")


def write_settings(home: str | Path, password: str, playlist: Path, port: int = 4455,
                   encoder: tuple[str, str] | None = None) -> None:
    """OBS's settings for the stream, in its portable config folder. OBS mustn't be running: it writes
    its own over these when it exits."""
    folder = obs_folder(home)
    (folder / "portable_mode.txt").touch()
    (folder / "disable_updater.txt").touch()   # this copy is updated with Shotcast's setup
    cfg = folder / "config" / "obs-studio"
    _ini(cfg / "global.ini", {"General": {"LastVersion": LAST_VERSION}})
    _ini(cfg / "user.ini", {"Basic": {"Profile": NAME, "ProfileDir": NAME,
                                      "SceneCollection": NAME, "SceneCollectionFile": NAME},
                            "General": {"ConfirmOnExit": False}})
    venc, vopts = encoder or video_encoder()
    _ini(cfg / "basic" / "profiles" / NAME / "basic.ini", {
        "General": {"Name": NAME},
        "Output": {"Mode": "Advanced"},
        "AdvOut": {
            # Custom Output (FFmpeg) to a URL, the playlist: the recording is the stream.
            "RecType": "FFmpeg", "FFOutputToFile": False, "FFURL": str(playlist), "FFFormat": "hls",
            "FFFormatMimeType": "application/vnd.apple.mpegurl", "FFMCustom": HLS_MUXER,
            "FFVEncoder": venc, "FFVEncoderId": 27, "FFVCustom": vopts, "FFVBitrate": 2500,
            "FFVGOPSize": FPS * 4, "FFRescale": False, "FFIgnoreCompat": True,
            "FFAEncoder": "aac", "FFAEncoderId": 86018, "FFACustom": "", "FFABitrate": 128,
            "FFAudioMixes": 1,
        },
        "Video": {"FPSType": 0, "FPSCommon": FPS},
    })
    ws = cfg / "plugin_config" / "obs-websocket" / "config.json"
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.write_text(json.dumps({"first_load": False, "server_enabled": True, "server_port": port,
                              "alerts_enabled": False, "auth_required": True, "server_password": password},
                             indent=2), encoding="utf-8")


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def configure(obs: Obs) -> None:
    """With OBS running: a 1080p canvas shaped like the screen, a 720p stream, GSPro filling it, and
    the microphone muted (the booth's voices come through the desktop sound)."""
    video = obs.call("GetVideoSettings")
    screen_w, screen_h = video["baseWidth"], video["baseHeight"]   # OBS starts at the screen's size
    base_w = min(screen_w, CANVAS_WIDTH)
    base_h = _even(screen_h * base_w / screen_w)
    out_w = min(base_w, STREAM_WIDTH)
    obs.call("SetVideoSettings", {"fpsNumerator": FPS, "fpsDenominator": 1, "baseWidth": base_w,
                                  "baseHeight": base_h, "outputWidth": out_w,
                                  "outputHeight": _even(base_h * out_w / base_w)})
    scene = obs.call("GetCurrentProgramScene")["currentProgramSceneName"]
    inputs = {i["inputName"] for i in obs.call("GetInputList")["inputs"]}
    if SOURCE in inputs:
        item = obs.call("GetSceneItemId", {"sceneName": scene, "sourceName": SOURCE})["sceneItemId"]
    else:
        item = obs.call("CreateInput", {
            "sceneName": scene, "inputName": SOURCE, "inputKind": "game_capture", "sceneItemEnabled": True,
            "inputSettings": {"capture_mode": "window", "window": GSPRO_WINDOW, "priority": 2,
                              "capture_cursor": False}})["sceneItemId"]
    obs.call("SetSceneItemTransform", {"sceneName": scene, "sceneItemId": item, "sceneItemTransform": {
        "positionX": 0, "positionY": 0, "alignment": 5, "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsAlignment": 0, "boundsWidth": base_w, "boundsHeight": base_h}})
    if mic := obs.call("GetSpecialInputs").get("mic1"):
        obs.call("SetInputMute", {"inputName": mic, "inputMuted": True})


def game_sound(obs: Obs, device: str, db: float = -10.0) -> bool:
    """A "Game sound" source recording the Windows output named `device` (that name exactly, else the
    one name containing it), added if it's missing and pointed back at it if it moved. Its fader is set
    only when it's added, so a level changed in OBS stays."""
    kinds = {i["inputName"]: i.get("unversionedInputKind") or i.get("inputKind")
             for i in obs.call("GetInputList")["inputs"]}
    if kinds.get(GAME_SOUND, OUTPUT_CAPTURE) != OUTPUT_CAPTURE:
        log.warning("Game sound: OBS already has a %r that isn't an Audio Output Capture; rename it", GAME_SOUND)
        return False
    inputs = {n for n, k in kinds.items() if k == OUTPUT_CAPTURE}
    probe = GAME_SOUND if GAME_SOUND in inputs else obs.call("GetSpecialInputs").get("desktop1")
    if probe is None:
        log.warning("Game sound: OBS has no Desktop Audio to list its outputs from")
        return False
    items = [i for i in obs.call("GetInputPropertiesListPropertyItems",
                                 {"inputName": probe, "propertyName": "device_id"})["propertyItems"]
             if i["itemValue"] != "default"]
    want = device.strip().lower()
    found = ([i for i in items if i["itemName"].lower() == want] or
             [i for i in items if want in i["itemName"].lower()])
    if len(found) != 1:
        log.warning("Game sound: %s output called %r (OBS has: %s)", "no" if not found else "more than one",
                    device, ", ".join(i["itemName"] for i in items))
        return False
    dev, name = found[0]["itemValue"], found[0]["itemName"]
    scene = obs.call("GetCurrentProgramScene")["currentProgramSceneName"]
    if GAME_SOUND in inputs:
        if obs.call("GetInputSettings", {"inputName": GAME_SOUND})["inputSettings"].get("device_id") != dev:
            obs.call("SetInputSettings", {"inputName": GAME_SOUND, "inputSettings": {"device_id": dev}})
            log.info("Game sound: now recording %s", name)
        try:
            obs.call("GetSceneItemId", {"sceneName": scene, "sourceName": GAME_SOUND})
        except ObsError:   # not in the scene on air: put it there too
            obs.call("CreateSceneItem", {"sceneName": scene, "sourceName": GAME_SOUND, "sceneItemEnabled": True})
        return True
    obs.call("CreateInput", {"sceneName": scene, "inputName": GAME_SOUND, "inputKind": OUTPUT_CAPTURE,
                             "inputSettings": {"device_id": dev}, "sceneItemEnabled": True})
    obs.call("SetInputVolume", {"inputName": GAME_SOUND, "inputVolumeDb": db})
    log.info("Game sound: recording %s at %+.0f dB", name, db)
    return True


def wait_for(obs: Obs, seconds: float = 90, clock=time.monotonic, sleep=time.sleep) -> None:
    end = clock() + seconds
    while True:
        try:
            obs.call("GetVersion")
            return
        except ObsError:
            if clock() >= end:
                raise
            sleep(2)


def launch(home: str | Path) -> None:
    if not launch_obs(portable_exe(home)):
        raise RuntimeError("OBS didn't start (is another OBS running?)")


def new_password() -> str:
    return secrets.token_urlsafe(18)
