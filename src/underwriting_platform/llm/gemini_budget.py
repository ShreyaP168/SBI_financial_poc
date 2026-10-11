"""Apply the active case budget to Gemini classification and photo matching."""

import asyncio
import json

from underwriting_platform.llm.budget import current_budget


async def generate(
    client,
    *,
    model,
    contents,
    max_output_tokens=8192,
    system_instruction=None,
    json_output=False,
    json_schema=None,
):
    budget = current_budget.get()
    reservation = 0
    charged = 0
    try:
        if budget is not None:
            count_contents = (
                [system_instruction, *contents] if system_instruction else list(contents)
            )
            if json_schema:
                count_contents.insert(0, json.dumps(json_schema, separators=(",", ":")))
            count = await asyncio.to_thread(
                client.models.count_tokens,
                model=model,
                contents=count_contents,
            )
            reservation = await budget.reserve(
                int(count.total_tokens)
                + max_output_tokens
                + max(4096, int(count.total_tokens * 0.05))
            )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=contents,
            config={
                "max_output_tokens": max_output_tokens,
                **({"system_instruction": system_instruction} if system_instruction else {}),
                **({"response_mime_type": "application/json"} if json_output else {}),
                **({"response_schema": json_schema} if json_schema else {}),
            },
        )
        usage = getattr(response, "usage_metadata", None)
        charged = int(getattr(usage, "total_token_count", 0) or 0)
        if budget is not None:
            budget.usage.append(
                {
                    "provider": "gemini",
                    "model": model,
                    "stage": "identity_verification",
                    "total_tokens": charged,
                    "input_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
                    "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
                }
            )
        return response
    finally:
        if budget is not None and reservation:
            await budget.finish(reservation, charged or reservation)
