"""Carrier rule validation, shared by CLI and dependency-injected services."""

from __future__ import annotations

import json

from underwriting_platform.configuration.paths import CONFIG_ROOT

REQUIRED_SBI_SECTIONS = (
    "income_multiplier_table",
    "qualification_gate",
    "occupation_income_proof_gate",
    "cibil_gate",
    "iib_gate",
    "medical_grid",
)


def validate_sbi_rules(rules: dict) -> dict:
    for key in REQUIRED_SBI_SECTIONS:
        if not rules.get(key):
            raise ValueError(f"Required SBI rule configuration missing: {key}")
    return rules


def load_sbi_rules() -> dict:
    return validate_sbi_rules(
        json.loads((CONFIG_ROOT / "sbi_smart_shield_rules.json").read_text(encoding="utf8"))
    )
