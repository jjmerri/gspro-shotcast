import asyncio
import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass
class Audio:
    samples: np.ndarray  # float32 mono, -1..1
    sample_rate: int


class StreamedAudio:
    """A clip still arriving from a streaming voice: playback starts on its first block while the
    rest downloads. The download task pushes blocks and always calls finish(); the player thread
    reads them through blocks()."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.parts: list[np.ndarray] = []
        self.ok = False            # the stream ran to its end
        self.played = 0            # samples written to the speakers (by the player thread)
        self.prebuffer = 0.0       # seconds of audio to have in hand before playback starts
        self.task: asyncio.Task | None = None   # the download
        self._q: queue.Queue = queue.Queue()
        self.started = asyncio.Event()     # first audio arrived
        self.finished = asyncio.Event()

    def push(self, block: np.ndarray) -> None:
        if len(block):
            self.parts.append(block)
            self._q.put(block)
            self.started.set()

    def finish(self, ok: bool) -> None:
        if not self.finished.is_set():
            self.ok = ok
            self._q.put(None)
            self.finished.set()

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    @property
    def seconds(self) -> float:
        return sum(len(p) for p in self.parts) / self.sample_rate

    @property
    def samples(self) -> np.ndarray:
        return np.concatenate(self.parts) if self.parts else np.zeros(0, dtype="float32")

    def blocks(self, stop: threading.Event):
        """Blocks in order until the stream ends or `stop` is set (for the player thread)."""
        while not stop.is_set():
            try:
                block = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            if block is None:
                return
            yield block


class TTS(ABC):
    # True when synthesising several lines at once is a win (a network provider): the pre-shot
    # voicing of all flight outcomes can then fire in parallel. False for a local-CPU model
    # (Kokoro), where concurrent synths just fight for the same cores and starve OCR.
    concurrent: bool = False
    # True for a network provider whose requests can stall; the speech queue then caps each request
    # with a timeout and retries when there's time. A local model gets no timeout (it doesn't stall,
    # and cancelling its await wouldn't stop the CPU work anyway).
    remote: bool = False

    @abstractmethod
    async def synthesize(self, text: str, voice: str, instructions: str | None = None) -> Audio | None:
        """Return audio, or None for providers that don't produce sound (console). `instructions`
        steers delivery (tone/pacing) on providers that support it; others ignore it."""
