import base64
import time
from typing import Any

from anthropic import BadRequestError

from ..config import LLMConfig
from .base import LLMClient, LLMResult

# Models that accept server-side refusal fallbacks. A refused line just means
# silence here, but the fallback keeps an over-cautious classifier from muting the booth.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1")
# Anthropic rejects a structured-output schema with more than 16 union-typed (e.g. nullable) fields.
MAX_UNION_PARAMS = 16


def _union_count(node: Any) -> int:
    if isinstance(node, dict):
        own = isinstance(node.get("type"), list) or "anyOf" in node
        return int(own) + sum(_union_count(v) for v in node.values())
    if isinstance(node, list):
        return sum(_union_count(v) for v in node)
    return 0


def _fit_unions(node: Any) -> Any:
    """Turns nullable strings into plain strings, where "" stands for null. The booth's readers
    already skip an empty line exactly as they skip a null one, so the reply means the same."""
    if isinstance(node, dict):
        if node.get("type") == ["string", "null"]:
            return {**node, "type": "string", "description": "empty string for none"}
        return {k: _fit_unions(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_fit_unions(v) for v in node]
    return node


class AnthropicClient(LLMClient):
    def __init__(self, cfg: LLMConfig) -> None:
        super().__init__(cfg)
        from anthropic import AsyncAnthropic

        # Reads ANTHROPIC_API_KEY (or an `ant auth login` profile).
        self._client = AsyncAnthropic(timeout=cfg.timeout_seconds, max_retries=1)
        # How this model turns thinking off; None when it can't, so the lowest effort stands in.
        self._thinking_off: dict[str, str] | None = {"type": "disabled"}

    def _thinking_none(self, params: dict[str, Any]) -> None:
        if self._thinking_off:
            params["thinking"] = self._thinking_off
        else:
            params["output_config"]["effort"] = "low"

    async def _create(self, params: dict[str, Any]) -> Any:
        if self.cfg.model.startswith(FALLBACK_MODELS):
            return await self._client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
            )
        return await self._client.messages.create(**params)

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any], images: list[bytes] | None = None,
        cache: bool = True,
    ) -> LLMResult:
        content: list[dict[str, Any]] = []
        if images and self.cfg.vision:
            for img in images:
                content.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img).decode()},
                    }
                )
        content.append({"type": "text", "text": user})

        if _union_count(schema) > MAX_UNION_PARAMS:
            schema = _fit_unions(schema)
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            # Personas + league context are identical every shot, so cache them.
            "system": [{"type": "text", "text": system, **({"cache_control": {"type": "ephemeral"}} if cache else {})}],
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if self.cfg.effort == "none":
            # Claude has no effort "none"; turning thinking off is the same thing. Without it Haiku 5.5
            # can spend a short reply's whole token budget thinking and return no text.
            self._thinking_none(params)
        elif self.cfg.effort:
            params["output_config"]["effort"] = self.cfg.effort

        started = time.perf_counter()
        while True:
            try:
                resp = await self._create(params)
                break
            except BadRequestError as e:
                # Models differ in how thinking turns off: learn the right way once from the error, retry.
                if "thinking" not in params or "thinking" not in str(e):
                    raise
                tried = params.pop("thinking")
                between = {"type": "between_tools"}
                self._thinking_off = between if "between_tools" in str(e) and tried != between else None
                self._thinking_none(params)
        latency = time.perf_counter() - started

        text = ""
        if resp.stop_reason != "refusal":
            text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = resp.usage
        return LLMResult(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
            latency_seconds=latency,
        )
