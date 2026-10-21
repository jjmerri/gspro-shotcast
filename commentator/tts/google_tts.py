import asyncio
import io
import os
import wave

import numpy as np

from ..config import TTSConfig
from .base import TTS, Audio

PCM_SAMPLE_RATE = 24_000  # streamed as 16-bit signed LE mono, no WAV header
_CALL_DEADLINE_SECONDS = 30.0  # a whole line streams in about a second


def _pcm(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0


class GoogleTTS(TTS):
    """Google Cloud Text-to-Speech, Chirp 3 HD voices (the only ones Google streams). Needs
    GOOGLE_TTS_API_KEY and the google-cloud-texttospeech package (the `cloud` extra)."""

    concurrent = True
    remote = True
    stream_sample_rate = PCM_SAMPLE_RATE

    def __init__(self, cfg: TTSConfig) -> None:
        self._key = os.environ.get("GOOGLE_TTS_API_KEY")
        if not self._key:
            raise RuntimeError("GOOGLE_TTS_API_KEY is not set")
        from google.cloud import texttospeech  # fail at startup, not on the first line, if missing

        self._tts = texttospeech
        self.cfg = cfg
        self._client = None

    def _get_client(self):
        # The gRPC channel binds to the running event loop, so it's made on first use, not in __init__.
        if self._client is None:
            self._client = self._tts.TextToSpeechAsyncClient(client_options={"api_key": self._key})
        return self._client

    def _voice(self, voice: str):
        return self._tts.VoiceSelectionParams(language_code=voice[:5], name=voice)

    async def synthesize(self, text: str, voice: str, instructions: str | None = None) -> Audio:
        t = self._tts
        resp = await self._get_client().synthesize_speech(
            input=t.SynthesisInput(text=text),
            voice=self._voice(voice),
            audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.LINEAR16, sample_rate_hertz=PCM_SAMPLE_RATE,
                                       speaking_rate=self.cfg.google_speaking_rate),
            timeout=_CALL_DEADLINE_SECONDS,
        )
        with wave.open(io.BytesIO(resp.audio_content)) as w:  # LINEAR16 comes wrapped in a WAV header
            return Audio(samples=_pcm(w.readframes(w.getnframes())), sample_rate=w.getframerate())

    async def stream(self, text: str, voice: str, instructions: str | None = None):
        """Yield the clip as float32 sample blocks as they arrive."""
        t = self._tts
        config = t.StreamingSynthesizeConfig(
            voice=self._voice(voice),
            streaming_audio_config=t.StreamingAudioConfig(
                audio_encoding=t.AudioEncoding.PCM, sample_rate_hertz=PCM_SAMPLE_RATE,
                speaking_rate=self.cfg.google_speaking_rate),
        )

        async def requests():
            yield t.StreamingSynthesizeRequest(streaming_config=config)
            yield t.StreamingSynthesizeRequest(input=t.StreamingSynthesisInput(text=text))

        # Awaiting the call waits for the connection, and cancelling that wait doesn't cancel the RPC.
        # So the wait runs shielded and a call that turns up after this stream was abandoned is
        # cancelled then; the deadline ends one whose connection never comes up.
        opening = asyncio.ensure_future(
            self._get_client().streaming_synthesize(requests=requests(), timeout=_CALL_DEADLINE_SECONDS))
        try:
            call = await asyncio.shield(opening)
        except asyncio.CancelledError:
            opening.add_done_callback(_cancel_call)
            raise
        carry = b""
        try:
            async for resp in call:
                data = carry + resp.audio_content
                whole = len(data) - len(data) % 2
                carry = data[whole:]
                if whole:
                    yield _pcm(data[:whole])
        finally:
            call.cancel()


def _cancel_call(opening: asyncio.Future) -> None:
    if not opening.cancelled() and opening.exception() is None:
        opening.result().cancel()
