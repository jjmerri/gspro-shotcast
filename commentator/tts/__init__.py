from ..config import TTSConfig
from .base import TTS, Audio, StreamedAudio


def build_tts(cfg: TTSConfig) -> TTS:
    if cfg.provider == "kokoro":
        from .kokoro_tts import KokoroTTS

        return KokoroTTS(cfg)
    if cfg.provider == "openai":
        from .openai_tts import OpenAITTS

        return OpenAITTS(cfg)
    from .console_tts import ConsoleTTS

    return ConsoleTTS()


__all__ = ["TTS", "Audio", "StreamedAudio", "build_tts"]
