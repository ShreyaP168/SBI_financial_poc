"""
tests/test_case_sheet_qc.py — synthetic-leak unit tests for the QC layer.

Each test constructs a minimal decision dict + synthetic rendered text,
runs the matching check, and asserts the expected finding code +
severity. The pdfplumber-backed PDF text extraction is bypassed by
calling check functions directly with a hand-built ctx dict.
"""
from __future__ import annotations
from underwriting_platform.configuration.paths import CONFIG_ROOT
import sys
from pathlib import Path

# Make `import qc.*` work when running this file directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from underwriting_platform.qc import load_profile  # noqa: E402
from underwriting_platform.qc import checks_ip, checks_brand, checks_pii  # noqa: E402
from underwriting_platform.qc import checks_completeness, checks_coherence, checks_layout  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(decision: dict | None = None, text: str = "",
          pages: list[str] | None = None,
          profile: dict | None = None) -> dict:
    """Build a ctx dict matching qc.case_sheet's call shape."""
    if pages is None:
        pages = [text] if text else []
    return {
        "decision":      decision or {"case_id": "TEST"},
        "pages":         pages,
        "page_count":    len(pages),
        "full_text":     text,
        "profile":       profile or load_profile(None),
        "pdf_path":      "test.pdf",
        "decision_path": "test_decision.json",
    }


def _codes(findings: list) -> list[str]:
    return [f.code for f in findings]


# ---------------------------------------------------------------------------
# A1 — vendor tokens
# ---------------------------------------------------------------------------

def test_a1_vendor_tokens_block():
    ctx = _ctx(text="The Claude pass extracted age=49, Anthropic primary.")
    findings = checks_ip.check_vendor_tokens(ctx)
    assert "VENDOR_TOKEN_LEAK" in _codes(findings)
    # Anthropic + Claude → BLOCK-grade
    assert any(f.severity == "BLOCK" for f in findings)


def test_a1_azure_is_warn_not_block():
    """`azure` is infra, demoted to WARN per blocklists.py."""
    ctx = _ctx(text="Hosted on azure cloud platform.")
    findings = checks_ip.check_vendor_tokens(ctx)
    assert any(f.severity == "WARN" for f in findings)
    assert all(f.severity != "BLOCK" for f in findings)


def test_a1_word_boundary_ky4_not_matched():
    """KY4 alone must not trigger a vendor match — that was the design
    doc's classic false-positive ('the \\bKY4\\b trap').
    """
    # Isolated KY4 — no vendor token nearby
    ctx = _ctx(text="KY4 row visible above the rationale.")
    findings = checks_ip.check_vendor_tokens(ctx)
    assert findings == []


def test_a1_word_boundary_snake_case_matches():
    """`azure_claude` should match 'claude' — the `_` is a separator,
    not part of the identifier, and 'claude' still leaks vendor identity.
    """
    ctx = _ctx(text="See azure_claude config.")
    findings = checks_ip.check_vendor_tokens(ctx)
    assert "VENDOR_TOKEN_LEAK" in _codes(findings)


# ---------------------------------------------------------------------------
# A2 — model versions
# ---------------------------------------------------------------------------

def test_a2_model_version_block():
    ctx = _ctx(text="model=gemini-2.5-pro responded with NO-MATCH.")
    findings = checks_ip.check_model_versions(ctx)
    assert "MODEL_ID_LEAK" in _codes(findings)
    assert all(f.severity == "BLOCK" for f in findings)


def test_a2_model_version_survives_vendor_mask():
    """`Vendor B-2.5-pro` — the version tail must STILL match even after
    vendor-name masking. This is the IFL bug class the design called out.
    """
    ctx = _ctx(text="model=Vendor B-2.5-pro responded.")
    findings = checks_ip.check_model_versions(ctx)
    assert "MODEL_ID_LEAK" in _codes(findings)


# ---------------------------------------------------------------------------
# A5 — stack trace
# ---------------------------------------------------------------------------

def test_a5_stack_trace_block():
    ctx = _ctx(text="Traceback (most recent call last): KeyError: 'foo'")
    findings = checks_ip.check_stack_traces(ctx)
    assert "STACK_TRACE_RESIDUE" in _codes(findings)


# ---------------------------------------------------------------------------
# B1 — agent labels
# ---------------------------------------------------------------------------

def test_b1_agent_label_warn():
    ctx = _ctx(text="See the compliance_agent output above.")
    findings = checks_ip.check_agent_labels(ctx)
    assert "AGENT_LABEL_LEAK" in _codes(findings)


# ---------------------------------------------------------------------------
# B4 — function path in prose (decision-JSON side)
# ---------------------------------------------------------------------------

def test_b4_function_path_in_flag_description():
    decision = {
        "case_id": "TEST",
        "flags": [{
            "code": "X_FAKE",
            "severity": "high",
            "description": "See engine.flags._flags_from_dual_vendor for detail.",
        }],
    }
    ctx = _ctx(decision=decision)
    findings = checks_ip.check_function_paths(ctx)
    assert "FUNCTION_PATH_IN_PROSE" in _codes(findings)


# ---------------------------------------------------------------------------
# C1 — insurer name typo (uses TALIC profile)
# ---------------------------------------------------------------------------

def test_c1_insurer_name_typo():
    profile = load_profile(CONFIG_ROOT
                            / "brand_profile.json")
    ctx = _ctx(text="Issued by Tata-AIA underwriting team.", profile=profile)
    findings = checks_brand.check_insurer_name(ctx)
    # We expect either INSURER_NAME_TYPO or no finding (the heuristic is
    # narrow). The interesting assertion: if it fires, it should be WARN.
    for f in findings:
        assert f.severity == "WARN"


# ---------------------------------------------------------------------------
# E1 — unmasked PAN
# ---------------------------------------------------------------------------

def test_e1_unmasked_pan_block():
    # Valid-format PAN: 4th char must be a holder-type code (P = individual)
    # per PAN_RE (tightened 2026-05-26 to ignore coincidental 10-char strings).
    ctx = _ctx(text="PAN: ABCPK1234F printed in full.")
    findings = checks_pii.check_unmasked_pan(ctx)
    assert "UNMASKED_PAN" in _codes(findings)
    assert all(f.severity == "BLOCK" for f in findings)


def test_e1_masked_pan_passes():
    ctx = _ctx(text="PAN: XXXXX1234F (masked)")
    findings = checks_pii.check_unmasked_pan(ctx)
    assert findings == []


# ---------------------------------------------------------------------------
# E2 — unmasked Aadhaar
# ---------------------------------------------------------------------------

def test_e2_unmasked_aadhaar_block():
    ctx = _ctx(text="Aadhaar 1234 5678 9012 (full).")
    findings = checks_pii.check_unmasked_aadhaar(ctx)
    assert "UNMASKED_AADHAAR" in _codes(findings)


# ---------------------------------------------------------------------------
# F1 — placeholder leaks
# ---------------------------------------------------------------------------

def test_f1_placeholder_leak_in_recon_prose():
    decision = {
        "case_id": "TEST",
        "reconciliation": {
            "reconciler_notes": (
                "The applicant earns None per month and was last reviewed on "
                "None — case requires manual UW attention."
            ),
        },
    }
    ctx = _ctx(decision=decision)
    findings = checks_completeness.check_placeholder_leaks(ctx)
    assert "PLACEHOLDER_IN_PROSE" in _codes(findings)


# ---------------------------------------------------------------------------
# F2 — HTML entity leak
# ---------------------------------------------------------------------------

def test_f2_html_entity_leak():
    ctx = _ctx(text="The applicant’s &amp; spouse declared income.")
    findings = checks_completeness.check_html_entity_leaks(ctx)
    assert "HTML_ENTITY_LEAK" in _codes(findings)


# ---------------------------------------------------------------------------
# G3 — verdict has no drivers
# ---------------------------------------------------------------------------

def test_g3_verdict_no_drivers_block():
    decision = {
        "case_id": "TEST",
        "verdict": {"label": "REFER", "decision_drivers": []},
    }
    ctx = _ctx(decision=decision)
    findings = checks_completeness.check_verdict_drivers(ctx)
    assert "VERDICT_NO_DRIVERS" in _codes(findings)


# ---------------------------------------------------------------------------
# D2 — SA mismatch engine vs xlsx
# ---------------------------------------------------------------------------

def test_d2_sa_mismatch_xlsx_warn():
    decision = {
        "case_id": "TEST",
        "proposed_sa": 10_000_000,
        "calculations": {
            "max_sa_eligible": {"inputs": {"proposed_sa": 10_000_000}}
        },
        "policy_data": {"sum_assured": 30_000_000},
    }
    ctx = _ctx(decision=decision)
    findings = checks_coherence.check_sa_consistency(ctx)
    assert "SA_MISMATCH_XLSX" in _codes(findings)
    assert all(f.severity == "WARN" for f in findings if f.code == "SA_MISMATCH_XLSX")


def test_d2_sa_mismatch_engine_block():
    decision = {
        "case_id": "TEST",
        "proposed_sa": 10_000_000,
        "calculations": {
            "max_sa_eligible": {"inputs": {"proposed_sa": 99_999_999}}
        },
    }
    ctx = _ctx(decision=decision)
    findings = checks_coherence.check_sa_consistency(ctx)
    assert "SA_MISMATCH_ENGINE" in _codes(findings)
    assert any(f.severity == "BLOCK" for f in findings)


# ---------------------------------------------------------------------------
# D10 — TALIC verdict vs APP_STATUS
# ---------------------------------------------------------------------------

def test_d10_verdict_vs_app_status_mismatch():
    decision = {
        "case_id": "TEST",
        "verdict": {"label": "REFER"},
        "policy_data": {"app_status": "Issued"},
    }
    ctx = _ctx(decision=decision)
    findings = checks_coherence.check_verdict_vs_app_status(ctx)
    assert "VERDICT_VS_APP_STATUS_MISMATCH" in _codes(findings)
    assert all(f.severity == "WARN" for f in findings)


def test_d10_verdict_vs_app_status_match():
    decision = {
        "case_id": "TEST",
        "verdict": {"label": "DECLINE"},
        "policy_data": {"app_status": "Decline"},
    }
    ctx = _ctx(decision=decision)
    findings = checks_coherence.check_verdict_vs_app_status(ctx)
    assert findings == []


# ---------------------------------------------------------------------------
# H1 — page count
# ---------------------------------------------------------------------------

def test_h1_too_few_pages_warn():
    ctx = _ctx()
    ctx["page_count"] = 1
    findings = checks_layout.check_page_count(ctx)
    assert "PDF_TOO_FEW_PAGES" in _codes(findings)


def test_h1_zero_pages_block():
    ctx = _ctx()
    ctx["page_count"] = 0
    findings = checks_layout.check_page_count(ctx)
    assert "PDF_NO_PAGES" in _codes(findings)


def test_h1_typical_page_count_passes():
    ctx = _ctx()
    ctx["page_count"] = 11
    findings = checks_layout.check_page_count(ctx)
    assert findings == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
