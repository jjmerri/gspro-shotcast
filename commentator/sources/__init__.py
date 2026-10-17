import logging
from pathlib import Path

from ..config import Config
from .base import ShotSource

log = logging.getLogger(__name__)


def _connector_log_present(path: str) -> bool:
    p = Path(path)
    try:
        return any(p.parent.glob(p.name + "*"))
    except OSError:
        return False


def build_source(cfg: Config) -> ShotSource:
    if cfg.source.type == "replay":
        from .replay import ReplaySource

        return ReplaySource(cfg.source.replay_file, cfg.source.replay_speed)
    if cfg.source.type == "connector_log" and _connector_log_present(cfg.source.connector_log):
        from .connector_log import ConnectorLogSource
        from .fallback import ConnectorWithLogFallback
        from .gspro_log_shots import GsproLogShotSource

        return ConnectorWithLogFallback(ConnectorLogSource(cfg.source.connector_log),
                                        GsproLogShotSource(cfg.source.gspro_log, backup=True))
    if cfg.source.type in ("connector_log", "gspro_log"):
        if cfg.source.type == "connector_log":
            log.warning("No GSPconnect log at %s; reading shots from GSPro's own log instead "
                        "(ball data only, no club data). Set source.type: gspro_log to silence this.",
                        cfg.source.connector_log)
        from .gspro_log_shots import GsproLogShotSource

        return GsproLogShotSource(cfg.source.gspro_log)
    from .gspro_sniffer import GSProSniffer

    return GSProSniffer(port=cfg.source.port, iface=cfg.source.iface)
