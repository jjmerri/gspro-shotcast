import os
from xml.sax.saxutils import escape, quoteattr

import httpx
import numpy as np

from ..config import TTSConfig
from .base import TTS, Audio

PCM_SAMPLE_RATE = 24_000  # raw-24khz-16bit-mono-pcm: 16-bit signed LE mono, no header
_FORMAT = "raw-24khz-16bit-mono-pcm"


class QuotaExhausted(RuntimeError):
    """The provider's free allowance for the month is spent (Azure's free F0 tier stops serving)."""


def _pcm(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0


def ssml(text: str, voice: str, style: str | None = None, degree: float = 1.0) -> str:
    """The request body: the line in the voice, in a speaking style when one is given."""
    body = escape(text)
    if style:
        body = (f"<mstts:express-as style={quoteattr(style)} styledegree=\"{degree:g}\">"
                f"{body}</mstts:express-as>")
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{voice[:5]}">'
            f"<voice name={quoteattr(voice)}>{body}</voice></speak>")


def _check(status: int, body: str) -> None:
    # A spent free (F0) allowance answers 403 ("out of call volume quota"); 429 is normally the
    # 20-requests-a-minute rate limit, worth retrying, unless it says the quota's gone.
    if status == 403 or (status == 429 and "quota" in body.lower()):
        raise QuotaExhausted(f"Azure Speech {status}: {body[:200]}")
    raise RuntimeError(f"Azure Speech {status}: {body[:200]}")


class AzureTTS(TTS):
    """Microsoft Azure AI Speech neural voices over REST, streamed as raw PCM. Needs AZURE_SPEECH_KEY
    (a Speech resource's key) and tts.azure_region (the resource's region)."""

    concurrent = True
    remote = True
    expressive = True   # takes `excited` for a big-moment speaking style (tts.azure_styles)
    stream_sample_rate = PCM_SAMPLE_RATE

    def __init__(self, cfg: TTSConfig) -> None:
        key = os.environ.get("AZURE_SPEECH_KEY")
        if not key:
            raise RuntimeError("AZURE_SPEECH_KEY is not set")
        self.cfg = cfg
        self._http = httpx.AsyncClient(
            base_url=f"https://{cfg.azure_region}.tts.speech.microsoft.com",
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": _FORMAT, "User-Agent": "shotcast"},
            timeout=30,
        )

    def _body(self, text: str, voice: str, excited: bool) -> str:
        speaker = next((k for k, v in self.cfg.azure_voices.items() if v == voice), None)
        style = self.cfg.azure_styles.get(speaker) if excited and speaker else None
        return ssml(text, voice, style, self.cfg.azure_style_degree)

    async def synthesize(self, text: str, voice: str, instructions: str | None = None,
                         excited: bool = False) -> Audio:
        resp = await self._http.post("/cognitiveservices/v1", content=self._body(text, voice, excited).encode())
        if resp.status_code != 200:
            _check(resp.status_code, resp.text)
        return Audio(samples=_pcm(resp.content[: len(resp.content) // 2 * 2]), sample_rate=PCM_SAMPLE_RATE)

    async def stream(self, text: str, voice: str, instructions: str | None = None, excited: bool = False):
        """Yield the clip as float32 sample blocks as they arrive."""
        body = self._body(text, voice, excited).encode()
        async with self._http.stream("POST", "/cognitiveservices/v1", content=body) as resp:
            if resp.status_code != 200:
                _check(resp.status_code, (await resp.aread()).decode(errors="replace"))
            carry = b""
            async for chunk in resp.aiter_bytes():
                data = carry + chunk
                whole = len(data) - len(data) % 2
                carry = data[whole:]
                if whole:
                    yield _pcm(data[:whole])
