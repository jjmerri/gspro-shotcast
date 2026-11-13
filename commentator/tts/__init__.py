from ..config import TTSConfig
from .base import TTS, Audio, StreamedAudio


def build_tts(cfg: TTSConfig) -> TTS:
    """The configured voice: `provider` alone, or with fallbacks, all of them behind a TieredTTS that
    picks one per round."""
    if not cfg.fallback or cfg.provider not in ("openai", "google", "azure"):
        # One provider (a local or print-only one never falls back): tts.paid and the allowances don't apply.
        return _one(cfg, cfg.provider)
    providers = list(dict.fromkeys([*cfg.tiers, cfg.paid_provider]))
    from .tiered import TieredTTS

    return TieredTTS(cfg, {p: _one(cfg, p) for p in providers})


def _one(cfg: TTSConfig, provider: str) -> TTS:
    if provider == "kokoro":
        from .kokoro_tts import KokoroTTS

        return KokoroTTS(cfg)
    if provider == "openai":
        from .openai_tts import OpenAITTS

        return OpenAITTS(cfg)
    if provider == "google":
        from .google_tts import GoogleTTS

        return GoogleTTS(cfg)
    if provider == "azure":
        from .azure_tts import AzureTTS

        return AzureTTS(cfg)
    from .console_tts import ConsoleTTS

    return ConsoleTTS()


__all__ = ["TTS", "Audio", "StreamedAudio", "build_tts"]
