"""
llm/gemini_client.py — Gemini-backed mirror of LLMClient for dual-vendor passes.

Implements the same `chat()` and `extract_json()` interface as LLMClient
so any agent can be instantiated with either client transparently:

    claude_client  = build_client()                 # Anthropic / Azure-Claude
    gemini_client  = build_gemini_client()          # Google Gemini
    medical_claude = MedicalAgent(claude_client)
    medical_gemini = MedicalAgent(gemini_client)
    # Run both in parallel, reconcile field-level disagreements.

The translation layer converts Anthropic-style content blocks
({"type":"text"|"image", ...}) to Google's `genai.types.Part` objects.
System prompt rides in the SDK's `system_instruction` slot, NOT inlined
into the user message — keeps the schema separation clean.

Note on cost: Gemini 2.5 Pro is ~3× cheaper per input token than Claude
Sonnet 4.6. A full medical-agent pass (14-page MER with vision) on
Gemini runs ~$0.02-0.03 vs ~$0.05 on Claude.
"""

from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import random
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-pro"
_DEFAULT_MAX_OUTPUT_TOKENS = 16_384


class GeminiLLMClient:
    """Drop-in replacement for LLMClient backed by Google Gemini.

    Surface:
      - chat(system, blocks, max_tokens, model) -> str
      - extract_json(system, blocks, max_tokens) -> dict

    Concurrency limited via asyncio.Semaphore to mirror LLMClient.
    Retries with exponential backoff on transient failures.
    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        max_concurrent: int = 5,
        max_retries: int = 4,
        initial_backoff_s: float = 1.0,
    ):
        if not api_key:
            raise ValueError("GeminiLLMClient: api_key is required")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.initial_backoff_s = initial_backoff_s
        self._sem = asyncio.Semaphore(
            int(os.environ.get("MAX_CONCURRENT_GEMINI", str(max_concurrent)))
        )
        # Lazy SDK init (verified once on first call).
        self._sdk_ok: bool | None = None

    # ── Public API ─────────────────────────────────────────────────

    async def chat(
        self,
        system: str,
        blocks: list[dict],
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> str:
        async with self._sem:
            return await self._call_with_retry(
                system,
                blocks,
                model=model or self.model,
                max_tokens=max_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
            )

    async def extract_json(
        self, system: str, blocks: list[dict], max_tokens: Optional[int] = None
    ) -> dict:
        raw = await self.chat(system, blocks, max_tokens=max_tokens)
        return _parse_tolerant(raw)

    # ── Internals ──────────────────────────────────────────────────

    async def _call_with_retry(
        self, system: str, blocks: list[dict], model: str, max_tokens: int
    ) -> str:
        from underwriting_platform.llm.budget import current_budget

        if current_budget.get() is not None:
            from google import genai
            from google.genai import types as genai_types
            from underwriting_platform.llm.gemini_budget import generate

            response = await generate(
                genai.Client(api_key=self.api_key),
                model=model,
                contents=self._blocks_to_gemini_parts(blocks, genai_types),
                max_output_tokens=max_tokens,
                system_instruction=system,
            )
            return response.text or ""
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return await asyncio.to_thread(self._call_sync, system, blocks, model, max_tokens)
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait_s = self.initial_backoff_s * (2**attempt) + random.random()
                logger.warning(
                    "Gemini call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self.max_retries,
                    type(e).__name__,
                    wait_s,
                )
                await asyncio.sleep(wait_s)
        raise RuntimeError("GEMINI_REQUEST_FAILED")

    def _call_sync(self, system: str, blocks: list[dict], model: str, max_tokens: int) -> str:
        from google import genai
        from google.genai import types as genai_types

        contents = self._blocks_to_gemini_parts(blocks, genai_types)
        config = genai_types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            # No response_mime_type=application/json — we use tolerant
            # parsing to handle markdown-fenced JSON anyway.
        )
        client = genai.Client(api_key=self.api_key)
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        text = getattr(resp, "text", None) or ""
        if not text:
            # Some Gemini responses come back as parts; flatten.
            try:
                cands = getattr(resp, "candidates", None) or []
                pieces: list[str] = []
                for c in cands:
                    parts = getattr(getattr(c, "content", None), "parts", None) or []
                    for p in parts:
                        t = getattr(p, "text", None)
                        if t:
                            pieces.append(t)
                text = "\n".join(pieces)
            except Exception:
                pass
        return text

    @staticmethod
    def _blocks_to_gemini_parts(blocks: list[dict], genai_types) -> list:
        """Anthropic content blocks → list of strings + Part objects.

        google.genai accepts a heterogeneous list — strings are auto-wrapped.
        """
        out: list = []
        for b in blocks:
            btype = b.get("type")
            if btype == "text":
                txt = b.get("text") or ""
                if txt:
                    out.append(txt)
            elif btype == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    try:
                        data = base64.b64decode(src.get("data") or "")
                        mime = src.get("media_type") or "image/jpeg"
                        out.append(genai_types.Part.from_bytes(data=data, mime_type=mime))
                    except Exception as e:
                        logger.warning("Gemini block translate (image) failed: %s", e)
        return out


# ── Tolerant JSON parser (mirrors LLMClient._parse_tolerant) ──────────


def _parse_tolerant(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
    try:
        return _coerce(json.loads(text))
    except json.JSONDecodeError:
        pass
    # Find outermost {...} block, depth-tracked.
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return _coerce(json.loads(candidate))
                    except json.JSONDecodeError:
                        break
    # Try [ ... ]
    list_start = text.find("[")
    if list_start >= 0:
        list_end = text.rfind("]")
        if list_end > list_start:
            try:
                return _coerce(json.loads(text[list_start : list_end + 1]))
            except json.JSONDecodeError:
                pass
    logger.error("Gemini response parsing failed", extra={"error_code": "MODEL_JSON_INVALID"})
    raise ValueError("Gemini did not return valid JSON")


def _coerce(data) -> dict:
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return data[0]
    if not isinstance(data, dict):
        raise ValueError(f"Gemini returned non-dict JSON: type={type(data).__name__}")
    return data


# ── Builder ────────────────────────────────────────────────────────


def build_gemini_client(env: Optional[dict] = None) -> GeminiLLMClient | None:
    """Build a GeminiLLMClient from environment. Returns None if no key.

    Env knobs:
      GEMINI_API_KEY              required to enable Gemini at all
      GEMINI_MEDICAL_MODEL        override default model (default: gemini-2.5-pro)
      MAX_CONCURRENT_GEMINI       per-process concurrency cap (default: 5)
    """
    env = env if env is not None else os.environ
    api_key = (env.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None
    model = env.get("GEMINI_MEDICAL_MODEL") or env.get("GEMINI_VITAL_REOCR_MODEL") or _DEFAULT_MODEL
    return GeminiLLMClient(api_key=api_key, model=model)
