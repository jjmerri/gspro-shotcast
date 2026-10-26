import base64
import logging
import time
from typing import Any

import httpx

from ..config import LLMConfig
from .base import LLMClient, LLMResult

log = logging.getLogger(__name__)


class OllamaClient(LLMClient):
    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        self.base_url = (cfg.base_url or "http://localhost:11434").rstrip("/")
        self._http = httpx.AsyncClient(timeout=cfg.timeout_seconds)

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any], images: list[bytes] | None = None,
        cache: bool = True,
    ) -> LLMResult:
        user_msg: dict[str, Any] = {"role": "user", "content": user}
        if images and self.cfg.vision:
            user_msg["images"] = [base64.b64encode(i).decode() for i in images]
        options: dict[str, Any] = {"num_predict": self.cfg.max_tokens}
        if self.cfg.temperature is not None:
            options["temperature"] = self.cfg.temperature
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, user_msg],
            "format": schema,
            "stream": False,
            # Stay resident in VRAM; reloading mid-round causes a long stall.
            "keep_alive": -1,
            "options": options,
            "think": self.cfg.ollama_think,
        }
        started = time.perf_counter()
        resp = await self._http.post(f"{self.base_url}/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content", "")
        if not text and data.get("done_reason") == "length":
            log.warning("%s hit max_tokens (%s) with no output; raise llm max_tokens", self.cfg.model, self.cfg.max_tokens)
        return LLMResult(
            text=text,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            latency_seconds=time.perf_counter() - started,
        )

    async def list_models(self) -> list[str]:
        resp = await self._http.get(f"{self.base_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]
