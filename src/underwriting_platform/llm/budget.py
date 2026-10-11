"""Shared per-case input/output reservations prevent concurrent overspending."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


class TokenBudgetExceeded(RuntimeError):
    retryable = False
    code = "LLM_TOKEN_BUDGET_EXCEEDED"


@dataclass
class CaseBudget:
    limit: int
    spent: int = 0
    reserved: int = 0
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    on_change: object = field(default=None, repr=False)
    on_close: object = field(default=None, repr=False)
    usage: list[dict] = field(default_factory=list, repr=False)

    def persist(self):
        if self.on_change is not None:
            self.on_change(self.snapshot())

    async def reserve(self, amount: int) -> int:
        async with self._condition:
            while self.spent + self.reserved + amount > self.limit:
                if self.reserved == 0 or amount > self.limit:
                    raise TokenBudgetExceeded("LLM_TOKEN_BUDGET_EXCEEDED")
                await self._condition.wait()
            self.reserved += amount
            self.persist()
            return amount

    async def finish(self, reservation: int, charged: int) -> None:
        async with self._condition:
            self.reserved -= reservation
            self.spent += charged
            self.persist()
            self._condition.notify_all()

    def snapshot(self) -> dict:
        return {"limit": self.limit, "spent": self.spent, "reserved": self.reserved}


current_budget: ContextVar[CaseBudget | None] = ContextVar("case_token_budget", default=None)


@contextmanager
def case_scope(limit: int, *, spent: int = 0):
    existing = current_budget.get()
    if existing is not None:
        yield existing
        return
    budget = CaseBudget(limit=limit, spent=spent)
    token = current_budget.set(budget)
    try:
        yield budget
    finally:
        if budget.on_close is not None:
            budget.on_close()
        current_budget.reset(token)
