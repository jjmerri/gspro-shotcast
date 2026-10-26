import base64
import os
import time
from typing import Any

import httpx

from ..config import LLMConfig
from .base import LLMClient, LLMResult


class OpenAIClient(LLMClient):
    """OpenAI chat-completions client with strict JSON-schema output, for the live/segment calls that
    run on a cheap fast model (e.g. gpt-6-luna). Uses the same key/transport as the OpenAI TTS."""

    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url or "https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {key}"},
            timeout=cfg.timeout_seconds,
        )

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any], images: list[bytes] | None = None,
        cache: bool = True,
    ) -> LLMResult:
        if images and self.cfg.vision:
            content: Any = [{"type": "text", "text": user}]
            for img in images:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()},
                })
        else:
            content = user  # a plain string when there's nothing to see

        body: dict[str, Any] = {
            "model": self.cfg.model,
            # Reasoning tokens draw down this budget, so it's the ceiling on reasoning + answer.
            "max_completion_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "commentary", "strict": True, "schema": schema},
            },
        }
        # No temperature: gpt-6 reasoning models reject a non-default value. OpenAI caches the system
        # prefix automatically once it's long enough, so there's nothing to set for caching.
        if self.cfg.effort:
            body["reasoning_effort"] = self.cfg.effort
        if self.cfg.service_tier:  # "fast" = up to 2.5x quicker, steadier latency; cheap on gpt-6-luna
            body["service_tier"] = self.cfg.service_tier

        started = time.perf_counter()
        resp = await self._http.post("/chat/completions", json=body)
        latency = time.perf_counter() - started
        resp.raise_for_status()
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        # A refusal, a filter block, or a run that hit the token cap mid-answer returns no usable
        # line; treat it as silence rather than letting a half-written JSON string reach the parser.
        text = ""
        if finish not in ("content_filter", "length") and not (choice.get("message") or {}).get("refusal"):
            text = (choice.get("message") or {}).get("content") or ""

        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        return LLMResult(
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cache_read_tokens=details.get("cached_tokens"),
            cache_write_tokens=None,
            latency_seconds=latency,
        )
