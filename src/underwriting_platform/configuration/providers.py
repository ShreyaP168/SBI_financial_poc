"""Provider settings shared by extraction clients; secrets stay out of repr."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    backend: str
    model: str
    api_key: str = field(repr=False)
    endpoint: str | None = None
    api_version: str | None = None
    max_tokens: int = 16_000
    max_retries: int = 5
    initial_backoff_s: float = 1.0
    token_budget_per_case: int = 200_000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        values = os.environ if env is None else env
        backend = values.get("LLM_BACKEND", "azure_claude")
        common = {
            "max_retries": int(values.get("LLM_MAX_RETRIES", "5")),
            "token_budget_per_case": int(values.get("LLM_TOKEN_BUDGET_PER_CASE", "200000")),
        }
        if backend == "azure_claude":
            config = cls(
                backend=backend,
                model=values.get("AZURE_CLAUDE_DEPLOYMENT", "claude-sonnet-4-6"),
                api_key=values.get("AZURE_CLAUDE_API_KEY", ""),
                endpoint=values.get("AZURE_CLAUDE_ENDPOINT", ""),
                api_version=values.get("AZURE_API_VERSION", "2025-04-01-preview"),
                **common,
            )
        elif backend == "claude":
            config = cls(
                backend=backend,
                model=values.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                api_key=values.get("ANTHROPIC_API_KEY", ""),
                **common,
            )
        else:
            raise ValueError(f"Unsupported LLM_BACKEND: {backend}")
        if not config.api_key:
            raise RuntimeError(
                "Provider API key missing; configure environment injection or Key Vault"
            )
        if config.max_retries < 1:
            raise ValueError("LLM_MAX_RETRIES must be positive")
        if config.token_budget_per_case < 1:
            raise ValueError("LLM_TOKEN_BUDGET_PER_CASE must be positive")
        return config
