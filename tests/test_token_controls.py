"""Paid generation is bounded and cheap representation retains source evidence."""

import asyncio
from types import SimpleNamespace

import pytest

from underwriting_platform.agents import AgentResult
from underwriting_platform.configuration.extraction import cache_revision
from underwriting_platform.llm.budget import CaseBudget, TokenBudgetExceeded, case_scope
from underwriting_platform.llm.client import LLMClient, LLMConfig
from underwriting_platform.llm.pdf_utils import Document
from underwriting_platform.orchestrator.checkpoints import StageStore


async def test_budget_rejects_request_before_paid_generation(monkeypatch):
    generations = []

    async def count(**kwargs):
        return SimpleNamespace(input_tokens=190000)

    client = LLMClient(LLMConfig("claude", "test", "dummy", max_tokens=32000))
    sdk = SimpleNamespace(
        messages=SimpleNamespace(count_tokens=count, stream=lambda **kw: generations.append(1))
    )
    monkeypatch.setattr(client, "_client", lambda: sdk)
    with case_scope(200000), pytest.raises(TokenBudgetExceeded):
        await client.chat("prompt", [])
    assert not generations


async def test_concurrent_reservations_wait_then_recheck_actual_spend():
    budget = CaseBudget(100)
    first = await budget.reserve(80)
    waiting = asyncio.create_task(budget.reserve(60))
    await asyncio.sleep(0)
    assert not waiting.done()
    await budget.finish(first, 20)
    second = await waiting
    await budget.finish(second, 60)
    assert budget.spent == 80 and budget.reserved == 0
    with pytest.raises(TokenBudgetExceeded):
        await budget.reserve(30)


async def test_actual_usage_replaces_reserved_output_and_scope_is_per_case(monkeypatch):
    async def count(**kwargs):
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        return SimpleNamespace(input_tokens=100)

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(text="{}")],
                usage=SimpleNamespace(input_tokens=100, output_tokens=20),
            )

    client = LLMClient(LLMConfig("claude", "test", "dummy", max_tokens=100))
    monkeypatch.setattr(
        client,
        "_client",
        lambda: SimpleNamespace(
            messages=SimpleNamespace(count_tokens=count, stream=lambda **kw: Stream())
        ),
    )
    with case_scope(10000) as first:
        await client.chat("prompt", [])
        assert first.spent == 120 and first.reserved == 0
    with case_scope(10000) as second:
        assert second.spent == 0


def test_native_financial_pages_keep_full_text_without_duplicate_images():
    doc = Document(
        "statement.pdf",
        "--- page 1 ---\n" + "transactions " * 100,
        images_b64=["native-image", "scan-image"],
        page_count=2,
        native_text_pages={1},
    )
    blocks = doc.to_anthropic_blocks()
    assert doc.text in [block.get("text") for block in blocks]
    assert [block["source"]["data"] for block in blocks if block["type"] == "image"] == [
        "scan-image"
    ]
    doc.force_vision = True
    assert sum(block["type"] == "image" for block in doc.to_anthropic_blocks()) == 2


def test_frontend_edit_does_not_invalidate_paid_extraction(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "rules.json").write_text("{}")
    package = tmp_path / "package"
    for name in ("agents", "renderer", "services"):
        (package / name).mkdir(parents=True)
    agent = package / "agents/a.py"
    agent.write_text("one")
    first = cache_revision("same-inputs", config, package_root=package)
    (package / "renderer/pdf.py").write_text("renderer changed")
    (package / "services/gateway.py").write_text("gateway changed")
    assert cache_revision("same-inputs", config, package_root=package) == first
    agent.write_text("two")
    assert cache_revision("same-inputs", config, package_root=package) != first


def test_successful_stage_reused_but_transient_failure_not_cached(tmp_path):
    store = StageStore(tmp_path)
    successful = AgentResult({"income": {"annual_income": 1}}, confidence=1)
    store.save_agent("income:0", successful)
    assert store.agent("income:0").block == successful.block
    failed = AgentResult({"income": {}}, error="NetworkError", retryable=True)
    store.save_agent("income:1", failed)
    assert store.agent("income:1") is None
