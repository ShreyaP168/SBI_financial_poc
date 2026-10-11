from types import SimpleNamespace

import pytest

from underwriting_platform.llm.budget import TokenBudgetExceeded, case_scope
from underwriting_platform.llm.gemini_budget import generate


async def test_optional_gemini_generation_is_blocked_before_paid_request():
    paid = []
    client = SimpleNamespace(
        models=SimpleNamespace(
            count_tokens=lambda **kwargs: SimpleNamespace(total_tokens=1000),
            generate_content=lambda **kwargs: paid.append(kwargs),
        )
    )
    with case_scope(2000), pytest.raises(TokenBudgetExceeded):
        await generate(client, model="test-model", contents=["evidence"])
    assert not paid


async def test_gemini_system_instruction_is_counted_and_actual_usage_recorded():
    counted, sent = [], []

    def count(**kwargs):
        counted.append(kwargs)
        return SimpleNamespace(total_tokens=100)

    def call(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(
            text="{}",
            usage_metadata=SimpleNamespace(
                total_token_count=120, prompt_token_count=100, candidates_token_count=20
            ),
        )

    client = SimpleNamespace(models=SimpleNamespace(count_tokens=count, generate_content=call))
    with case_scope(20000) as budget:
        await generate(
            client,
            model="test-model",
            contents=["evidence"],
            system_instruction="system",
            max_output_tokens=100,
        )
        assert budget.spent == 120 and budget.reserved == 0
        assert budget.usage[0]["total_tokens"] == 120
    assert counted[0]["contents"] == ["system", "evidence"]
    assert sent[0]["config"]["system_instruction"] == "system"
