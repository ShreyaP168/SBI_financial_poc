"""
llm/client.py — single LLM abstraction for v9.

Backends:
  - azure_claude  : Azure AI Foundry (default; uses AnthropicBedrock-style helper)
  - claude        : Anthropic direct API
  - gemini        : Google Gemini (placeholder; not yet wired in v9)

The client exposes:
  - chat(system, blocks, max_tokens) → str         (returns raw text)
  - extract_json(system, blocks, schema_hint) → dict   (returns parsed dict; coerces list-of-one)

Concurrency + retry handled inline (no shared global rate limiter at Phase 0;
add when we hit 429s).
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import random
import uuid
from dataclasses import dataclass
from underwriting_platform.configuration.providers import LLMConfig
from pathlib import Path
from typing import Any, Optional
from underwriting_platform.llm.budget import TokenBudgetExceeded, current_budget

logger = logging.getLogger(__name__)


class LLMResponseRejected(RuntimeError):
    """Identical requests cannot repair refusal, truncation or authentication."""

    retryable = False


class LLMTransientFailure(RuntimeError):
    retryable = True


class LLMJSONInvalid(ValueError):
    retryable = False


@dataclass
class UsageRecord:
    """Per-call token usage. Aggregated by LLMClient.usage_totals."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


class LLMClient:
    """Anthropic-style client with multimodal (text + image_b64) support.

    All v9 LLM traffic goes through here. Backend swap = config change.
    Tracks token usage per call so the batch runner can report cumulative
    spend (input/output/cache tokens, total calls, per-model breakdown).
    """

    def __init__(self, config: LLMConfig):
        self.cfg = config
        self._anthropic = None  # lazy-init the SDK client
        self._sem = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT_LLM", "5")))
        # Token usage accumulator — totals across the process lifetime.
        # Reset via reset_usage(); snapshot via get_usage_summary().
        self._usage_lock = asyncio.Lock()
        self._usage_records: list[UsageRecord] = []

    def _client(self):
        if self._anthropic is not None:
            return self._anthropic
        if self.cfg.backend == "azure_claude":
            # AsyncAnthropicFoundry takes only api_key + base_url. The base_url
            # MUST end with /anthropic/ for the routing to work.
            from anthropic import AsyncAnthropicFoundry

            endpoint = (self.cfg.endpoint or "").rstrip("/")
            if not endpoint.endswith("/anthropic"):
                endpoint = endpoint + "/anthropic"
            endpoint = endpoint + "/"
            self._anthropic = AsyncAnthropicFoundry(
                api_key=self.cfg.api_key,
                base_url=endpoint,
            )
        elif self.cfg.backend == "claude":
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(api_key=self.cfg.api_key)
        else:
            raise ValueError(f"Unknown LLM backend: {self.cfg.backend}")
        return self._anthropic

    async def chat(
        self,
        system: str,
        blocks: list[dict],
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        """Send a multimodal chat completion. `blocks` is a list of content blocks
        (text or image) in Anthropic message-content format.

        `model` overrides the client's default deployment for this single call —
        used by the vital re-OCR pass to route a focused crop through a
        cheaper deployment (e.g. Haiku) when one is configured.
        """
        async with self._sem:
            return await self._chat_with_retry(
                system,
                blocks,
                max_tokens or self.cfg.max_tokens,
                model=model,
            )

    async def _chat_with_retry(
        self, system: str, blocks: list[dict], max_tokens: int, model: Optional[str] = None
    ) -> str:
        chosen_model = model or self.cfg.model
        cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        last_err: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries):
            reservation = 0
            charged = 0
            rejected_before_generation = False
            budget = current_budget.get()
            try:
                client = self._client()
                if budget is not None:
                    count = await client.messages.count_tokens(
                        model=chosen_model,
                        system=cached_system,
                        messages=[{"role": "user", "content": blocks}],
                    )
                    # Count APIs provide estimates. Leave a safety margin and reserve
                    # the entire allowed output before generation starts.
                    reservation = await budget.reserve(
                        int(count.input_tokens)
                        + max_tokens
                        + max(2048, int(count.input_tokens * 0.05))
                    )
                # Use streaming — Anthropic requires it for any request that
                # may take >10 minutes (large multimodal payloads).
                async with client.messages.stream(
                    model=chosen_model,
                    max_tokens=max_tokens,
                    system=cached_system,
                    messages=[{"role": "user", "content": blocks}],
                ) as stream:
                    final_msg = await stream.get_final_message()
                # Record token usage (Anthropic SDK exposes `.usage`)
                try:
                    u = getattr(final_msg, "usage", None)
                    if u is not None:
                        rec = UsageRecord(
                            model=chosen_model,
                            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                            cache_creation_tokens=int(
                                getattr(u, "cache_creation_input_tokens", 0) or 0
                            ),
                            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
                        )
                        async with self._usage_lock:
                            self._usage_records.append(rec)
                        charged = (
                            rec.input_tokens
                            + rec.output_tokens
                            + rec.cache_creation_tokens
                            + rec.cache_read_tokens
                        )
                        if budget is not None:
                            from underwriting_platform.llm.telemetry import request_stage

                            budget.usage.append(
                                {
                                    "provider": "azure"
                                    if self.cfg.backend == "azure_claude"
                                    else "anthropic",
                                    "model": chosen_model,
                                    "stage": request_stage.get(),
                                    "input_tokens": rec.input_tokens,
                                    "output_tokens": rec.output_tokens,
                                    "cache_creation_tokens": rec.cache_creation_tokens,
                                    "cache_read_tokens": rec.cache_read_tokens,
                                    "total_tokens": charged,
                                    "images": sum(b.get("type") == "image" for b in blocks),
                                    "text_chars": sum(
                                        len(b.get("text", ""))
                                        for b in blocks
                                        if b.get("type") == "text"
                                    ),
                                }
                            )
                except Exception:
                    pass  # tracking failure must never break the call
                if getattr(final_msg, "stop_reason", None) in ("max_tokens", "refusal"):
                    raise LLMResponseRejected("MODEL_RESPONSE_REJECTED")
                parts = [b.text for b in final_msg.content if hasattr(b, "text")]
                return "\n".join(parts)
            except TokenBudgetExceeded:
                raise
            except Exception as e:
                last_err = e
                status = getattr(e, "status_code", None)
                rejected_before_generation = status in {400, 401, 403, 404, 413, 422, 429}
                retryable = status in {408, 409, 429} or (isinstance(status, int) and status >= 500)
                retryable = (
                    retryable
                    or isinstance(e, (TimeoutError, ConnectionError))
                    or type(e).__name__ in {"APITimeoutError", "APIConnectionError"}
                )
                from underwriting_platform.application.context import request_id

                if not retryable:
                    logger.error(
                        "LLM request rejected",
                        extra={
                            "request_id": request_id.get() or str(uuid.uuid4()),
                            "error_code": "MODEL_REQUEST_REJECTED",
                        },
                    )
                    raise LLMResponseRejected("MODEL_REQUEST_REJECTED") from None
                if attempt + 1 == self.cfg.max_retries:
                    break
                wait_s = self.cfg.initial_backoff_s * (2**attempt) + random.random()
                logger.warning(
                    "LLM dependency retry",
                    extra={
                        "request_id": request_id.get() or str(uuid.uuid4()),
                        "error_code": "MODEL_DEPENDENCY_UNAVAILABLE",
                        "attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(wait_s)
            finally:
                if budget is not None and reservation:
                    # An interrupted/ambiguous provider request may be billable.
                    # Conservatively retain its full reservation; never retry free.
                    await budget.finish(
                        reservation,
                        charged or (0 if rejected_before_generation else reservation),
                    )
        raise LLMTransientFailure("MODEL_DEPENDENCY_UNAVAILABLE") from None

    def get_usage_summary(self) -> dict:
        """Snapshot of cumulative token usage since process start (or
        last reset_usage()). Safe to call from sync code — does not
        acquire the lock; readers see a consistent-at-an-instant view
        because list.append is GIL-atomic.
        """
        records = list(self._usage_records)  # cheap shallow copy
        total = {
            "call_count": len(records),
            "input_tokens": sum(r.input_tokens for r in records),
            "output_tokens": sum(r.output_tokens for r in records),
            "cache_creation_tokens": sum(r.cache_creation_tokens for r in records),
            "cache_read_tokens": sum(r.cache_read_tokens for r in records),
        }
        total["total_tokens"] = (
            total["input_tokens"]
            + total["output_tokens"]
            + total["cache_creation_tokens"]
            + total["cache_read_tokens"]
        )
        by_model: dict[str, dict] = {}
        for r in records:
            m = by_model.setdefault(
                r.model,
                {
                    "call_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                },
            )
            m["call_count"] += 1
            m["input_tokens"] += r.input_tokens
            m["output_tokens"] += r.output_tokens
            m["cache_creation_tokens"] += r.cache_creation_tokens
            m["cache_read_tokens"] += r.cache_read_tokens
        for m in by_model.values():
            m["total_tokens"] = (
                m["input_tokens"]
                + m["output_tokens"]
                + m["cache_creation_tokens"]
                + m["cache_read_tokens"]
            )
        total["by_model"] = by_model
        return total

    def reset_usage(self) -> None:
        """Clear the cumulative usage tally."""
        self._usage_records.clear()

    async def extract_json(
        self, system: str, blocks: list[dict], max_tokens: Optional[int] = None
    ) -> dict:
        """chat() + tolerant JSON parsing.

        Recovers from common LLM quirks:
          - markdown ```json fences
          - prose preamble before the JSON object
          - prose epilogue after the JSON object
          - list-of-one-dict wrapping
        """
        raw = await self.chat(system, blocks, max_tokens)
        return self._parse_tolerant(raw)

    @staticmethod
    def _parse_tolerant(raw: str) -> dict:
        text = raw.strip()
        # 1. Strip markdown fences.
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[:-3].rstrip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip("\n")
        # 2. Direct parse first.
        try:
            data = json.loads(text)
            return _coerce_to_dict(data)
        except json.JSONDecodeError:
            pass
        # 3. Look for the OUTERMOST top-level {...} block (handles nested braces).
        start = text.find("{")
        if start >= 0:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                elif ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            return _coerce_to_dict(json.loads(candidate))
                        except json.JSONDecodeError:
                            break  # try next strategy
        # 4. Look for [...] (list of objects).
        list_start = text.find("[")
        if list_start >= 0:
            list_end = text.rfind("]")
            if list_end > list_start:
                try:
                    return _coerce_to_dict(json.loads(text[list_start : list_end + 1]))
                except json.JSONDecodeError:
                    pass
        from underwriting_platform.application.context import request_id

        logger.error(
            "LLM response parsing failed",
            extra={
                "request_id": request_id.get() or str(uuid.uuid4()),
                "error_code": "MODEL_JSON_INVALID",
            },
        )
        raise LLMJSONInvalid("MODEL_JSON_INVALID")


def _coerce_to_dict(data) -> dict:
    """Coerce list-of-one-dict and reject other non-dict shapes."""
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    if not isinstance(data, dict):
        raise LLMJSONInvalid("MODEL_JSON_INVALID")
    return data


def build_client(env: Optional[dict] = None) -> LLMClient:
    """Build an LLM client from environment variables."""
    return LLMClient(LLMConfig.from_env(env))
