"""
engine/policy_data.py — read TALIC's Policy_Data.xlsx as the system-of-
record for fields the agents cannot reliably get from PDFs alone.

The xlsx is the authoritative source for things like nominee details,
bureau scores, agent code, and the policy-system view of insured income
and sum-assured. The agents extract the same fields from PDFs where they
appear, but for cases where the CAF / Customer Information Sheet isn't
in the doc pack (95% of ITR-1 cases) the xlsx is the only source.

Lookup is by `PLCY_NUM` — the same case-id format the batch runner uses
after stripping the `NN_` numeric prefix from folder names.

Path resolution order:
  1. Explicit `path=` argument to `load_policy_data`.
  2. `POLICY_DATA_XLSX` environment variable.
  3. None — `lookup()` then returns `None` for every case_id and the
     orchestrator silently skips the join. This keeps the pipeline
     runnable on machines that don't have the xlsx.

As of 2026-05-15 we surface the full policy-system view: nominee,
sum-assured grid (SAR/MSAR/FSAR/TSAR/TRM_TSAR), seven bureau ACM
scores (ECM, PB, AUREUS, CRIF, EXP, TU, PAYU — each with income
min/max/avg where present), rider opted flag, adverse-health flag,
channel/agent context, husband-housewife linkage, app status, and the
issued/postponed UW outcome. Whatever isn't directly displayed lives
under `case_json["policy_data"]` for the reconciler + renderer.
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Optional

from .reference_cache import source_revision

_loaded_revisions = {}
logger = logging.getLogger(__name__)


# Case-id-keyed cache. We only ever load the xlsx once per process —
# 102 rows × 68 cols is tiny and the file rarely changes during a batch.
_INDEX: Optional[dict[str, dict]] = None
_LOADED_PATH: Optional[Path] = None


def _resolve_path(path: Optional[str | Path] = None) -> Optional[Path]:
    if path:
        p = Path(path)
        return p if p.exists() else None
    env_p = os.environ.get("POLICY_DATA_XLSX")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    return None


def _coerce_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.upper() == "NULL":
        return None
    return s


def _coerce_int(v) -> Optional[int]:
    s = _coerce_str(v)
    if s is None:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def load_policy_data(path: Optional[str | Path] = None) -> dict[str, dict]:
    """Load and index the xlsx. Returns `{PLCY_NUM: row_dict}`.

    Idempotent — re-loading from the same path returns the cached index.
    Re-loading from a different path rebuilds it.
    """
    global _INDEX, _LOADED_PATH
    resolved = _resolve_path(path)
    if resolved is None:
        logger.info("[policy_data] no xlsx path resolved — lookups disabled")
        _INDEX = {}
        _LOADED_PATH = None
        return _INDEX
    revision = source_revision(resolved)
    if (
        _INDEX is not None
        and _LOADED_PATH == resolved
        and _loaded_revisions.get("_LOADED_PATH") == revision
    ):
        return _INDEX

    try:
        import openpyxl
    except ImportError:
        logger.warning("[policy_data] openpyxl not installed — lookups disabled")
        _INDEX = {}
        _LOADED_PATH = None
        return _INDEX

    try:
        wb = openpyxl.load_workbook(str(resolved), data_only=True, read_only=True)
    except Exception as e:
        logger.warning("[policy_data] could not open %s: %s", resolved, e)
        _INDEX = {}
        _LOADED_PATH = None
        return _INDEX

    sheet = wb[wb.sheetnames[0]]
    rows = sheet.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        wb.close()
        _INDEX = {}
        _LOADED_PATH = resolved
        _loaded_revisions["_LOADED_PATH"] = revision
        return _INDEX

    # Header aliases — when the canonical name isn't in the sheet, try
    # these fallbacks. Added 2026-05-26 for the 403_POC_cases.xlsx
    # variant which uses `ASSESD_INCOM` instead of `INSURED_INCOME` and
    # carries operational columns (IIB_FLAG, MED_STAT, FIN_STAT, etc.)
    # absent from the earlier sample.
    _HEADER_ALIASES: dict[str, tuple[str, ...]] = {
        "INSURED_INCOME": ("ASSESD_INCOM", "ASSESSED_INCOM", "ASSESSED_INCOME"),
        "SUBMSION_DT": ("SUBMSSON_DT",),
        "ECM_SCORE": ("ECR_SCORE",),
        "CURR_COUNTRY": ("COUNT_COUNTRY",),
        "PLAN_SUB_TYPE": ("SUB_TYPE",),
    }

    # Map header → column index for the small subset we care about.
    # Tolerant of alias variants per `_HEADER_ALIASES`.
    def col(name: str) -> Optional[int]:
        try:
            return headers.index(name)
        except ValueError:
            pass
        for alt in _HEADER_ALIASES.get(name, ()):
            try:
                return headers.index(alt)
            except ValueError:
                continue
        return None

    # Pre-resolve every column index we read below — one dict-lookup per
    # column-name string, then plain tuple-indexing per row.
    cols = {
        name: col(name)
        for name in (
            "PLCY_NUM",
            "NOMINEE_NAME",
            "NOMINEE_RELATION",
            "INSURED_NAME",
            "INSURED_INCOME",
            "SUM_ASURD",
            # Sum-assured-at-risk grid (TALIC system-of-record):
            "SAR",
            "MSAR",
            "FSAR",
            "TSAR",
            "TRM_TSAR",
            "FINANCL_WAVER_FLG",
            # ACM bureau scores (the seven bureaus referenced in the
            # 2026-05-15 underwriter feedback):
            "ECM_SCORE",
            "PB_RISK_SCORE",
            "AUREUS_RISK_SCORE",
            "CRIF_SCORE",
            "CRIF_MIN_INC",
            "CRIF_MAX_INC",
            "CRIF_AVG_INC",
            "EXP_SCORE",
            "EXP_MIN_INC",
            "EXP_MAX_INC",
            "EXP_AVG_INC",
            "TU_SCORE",
            "TU_MIN_INC",
            "TU_MAX_INC",
            "TU_AVG_INC",
            "PAYU_SCORE",
            "PAYU_MIN_INC",
            "PAYU_MAX_INC",
            "PAYU_AVG_INC",
            # Rider + adverse-health declaration:
            "Health_Rider_Flag",
            "ADVRS_HLTH_FLG",
            "IS_URE_ELIGIBLE",
            # Husband-housewife linkage:
            "HUSBAND_HOUSEWIFE_PLCY_FLG",
            "HUSBAND_HOUSEWIFE_PLCY_NUM",
            # Channel / agent / campaign context:
            "CHNL_NM",
            "Channel",
            "BUSINESS_TYP",
            "SRC_SYSTEM",
            "AGNT_CD",
            "AGNT_NM",
            "PASA",
            "CAMPNG_CD",
            "CUST_SEGMT",
            "CUST_CLS",
            # Application status:
            "APP_STATUS",
            "APP_STAGE_STATUS",
            # Premium / term:
            "ANNUAL_PREM",
            "PLCY_TRM",
            "PREM_PAY_TRM",
            "PREM_PAY_MODE",
            # Plan:
            "PLN_CD",
            "PLANNAME",
            "PLAN_SUB_TYPE",
            "PLCY_PACK_ID",
            # Identity-ish (cross-check):
            "AGE_AT_ENTRY",
            "DT_OF_BIRTH",
            "GENDER",
            "NATIONALITY",
            "PINCD",
            "CURR_STATE",
            "CURR_COUNTRY",
            "RES_STAT",
            "EDUCATION",
            "OCCUPATION",
            "SUBMSION_DT",
            # 403_POC_cases.xlsx operational columns (2026-05-26):
            "MED_STAT",
            "FIN_STAT",
            "EFF_DT",
            "IIB_FLAG",
            "TEST_NM",
            "med type",
        )
    }

    def _s(row, name):  # str
        i = cols.get(name)
        return _coerce_str(row[i]) if i is not None and i < len(row) else None

    def _i(row, name):  # int
        i = cols.get(name)
        return _coerce_int(row[i]) if i is not None and i < len(row) else None

    idx_plcy = cols["PLCY_NUM"]

    index: dict[str, dict] = {}
    for row in rows:
        if idx_plcy is None:
            break
        plcy = _coerce_str(row[idx_plcy]) if idx_plcy < len(row) else None
        if not plcy:
            continue
        index[plcy] = {
            "case_id": plcy,
            # Nominee (already shown in the Applicant section)
            "nominee_name": _s(row, "NOMINEE_NAME"),
            "nominee_relation": _s(row, "NOMINEE_RELATION"),
            "insured_name": _s(row, "INSURED_NAME"),
            "insured_income": _i(row, "INSURED_INCOME"),
            "sum_assured": _i(row, "SUM_ASURD"),
            # Sum-assured-at-risk grid — the TALIC underwriter's primary
            # HLV viability cross-check.
            "sar": _i(row, "SAR"),
            "msar": _i(row, "MSAR"),
            "fsar": _i(row, "FSAR"),
            "tsar": _i(row, "TSAR"),
            "trm_tsar": _i(row, "TRM_TSAR"),
            "financial_waiver": _s(row, "FINANCL_WAVER_FLG"),
            # Bureau ACM scores (seven feeds; not all populated for every row)
            "bureau_scores": {
                "ecm": {"score": _s(row, "ECM_SCORE")},
                "pb": {"score": _i(row, "PB_RISK_SCORE")},
                "aureus": {"score": _s(row, "AUREUS_RISK_SCORE")},
                "crif": {
                    "score": _i(row, "CRIF_SCORE"),
                    "min_inc": _i(row, "CRIF_MIN_INC"),
                    "max_inc": _i(row, "CRIF_MAX_INC"),
                    "avg_inc": _i(row, "CRIF_AVG_INC"),
                },
                "exp": {
                    "score": _i(row, "EXP_SCORE"),
                    "min_inc": _i(row, "EXP_MIN_INC"),
                    "max_inc": _i(row, "EXP_MAX_INC"),
                    "avg_inc": _i(row, "EXP_AVG_INC"),
                },
                "tu": {
                    "score": _i(row, "TU_SCORE"),
                    "min_inc": _i(row, "TU_MIN_INC"),
                    "max_inc": _i(row, "TU_MAX_INC"),
                    "avg_inc": _i(row, "TU_AVG_INC"),
                },
                "payu": {
                    "score": _i(row, "PAYU_SCORE"),
                    "min_inc": _i(row, "PAYU_MIN_INC"),
                    "max_inc": _i(row, "PAYU_MAX_INC"),
                    "avg_inc": _i(row, "PAYU_AVG_INC"),
                },
            },
            # Rider + adverse-health declaration
            "health_rider_flag": _s(row, "Health_Rider_Flag"),
            "adverse_health": _s(row, "ADVRS_HLTH_FLG"),
            "ure_eligible": _s(row, "IS_URE_ELIGIBLE"),
            # Husband-housewife linkage
            "hhw_flag": _s(row, "HUSBAND_HOUSEWIFE_PLCY_FLG"),
            "hhw_plcy_num": _s(row, "HUSBAND_HOUSEWIFE_PLCY_NUM"),
            # Channel / agent / campaign
            "channel_name": _s(row, "CHNL_NM"),
            "channel": _s(row, "Channel"),
            "business_type": _s(row, "BUSINESS_TYP"),
            "src_system": _s(row, "SRC_SYSTEM"),
            "agent_code": _s(row, "AGNT_CD") or _i(row, "AGNT_CD"),
            "agent_name": _s(row, "AGNT_NM"),
            "pasa": _s(row, "PASA"),
            "campaign_code": _s(row, "CAMPNG_CD"),
            "cust_segment": _s(row, "CUST_SEGMT"),
            "cust_class": _s(row, "CUST_CLS"),
            # Application status (system-of-record UW outcome — ground truth)
            "app_status": _s(row, "APP_STATUS"),
            "app_stage_status": _s(row, "APP_STAGE_STATUS"),
            # Premium / term
            "annual_premium": _i(row, "ANNUAL_PREM"),
            "policy_term": _i(row, "PLCY_TRM"),
            "prem_pay_term": _i(row, "PREM_PAY_TRM"),
            "prem_pay_mode": _s(row, "PREM_PAY_MODE"),
            # Plan
            "plan_code": _s(row, "PLN_CD"),
            "plan_name": _s(row, "PLANNAME"),
            "plan_sub_type": _s(row, "PLAN_SUB_TYPE"),
            "plan_pack_id": _s(row, "PLCY_PACK_ID"),
            # Identity-ish cross-check
            "age_at_entry": _i(row, "AGE_AT_ENTRY"),
            "gender": _s(row, "GENDER"),
            "nationality": _s(row, "NATIONALITY"),
            "pin_code": _s(row, "PINCD") or _i(row, "PINCD"),
            "state": _s(row, "CURR_STATE"),
            "country": _s(row, "CURR_COUNTRY"),
            "res_status": _s(row, "RES_STAT"),
            "education": _s(row, "EDUCATION"),
            "occupation": _s(row, "OCCUPATION"),
            "submission_dt": str(row[cols["SUBMSION_DT"]])
            if cols.get("SUBMSION_DT") is not None
            and cols["SUBMSION_DT"] < len(row)
            and row[cols["SUBMSION_DT"]] is not None
            else None,
            # 403_POC_cases.xlsx operational columns
            "med_stat": _s(row, "MED_STAT"),
            "fin_stat": _s(row, "FIN_STAT"),
            "effective_dt": (
                str(row[cols["EFF_DT"]])
                if cols.get("EFF_DT") is not None
                and cols["EFF_DT"] < len(row)
                and row[cols["EFF_DT"]] is not None
                else None
            ),
            "iib_flag": _s(row, "IIB_FLAG"),
            "test_nm": _s(row, "TEST_NM"),
            "med_type": _s(row, "med type"),
            "_source_xlsx": resolved.name,
        }
        # ── Per-field provenance map. Records whether each canonical
        # field came from the xlsx, is derivable elsewhere, or is a
        # confirmed gap. The renderer surfaces "available"/"derive-from-X"
        # /"not on file" so the underwriter doesn't mistake a null for
        # bad extraction.
        rec_pd_columns_present = {name for name, idx in cols.items() if idx is not None}
        index[plcy]["_field_provenance"] = {
            # Available in this xlsx
            "available_from_xlsx": sorted(rec_pd_columns_present),
            # Fields the engine derives from other agents / configs
            "derived_in_engine": {
                "husband_housewife_flag": "engine: applicant.occupation == 'Homemaker' → 'Y'",
                "health_rider_flag": "engine: derive from plan_code + product master "
                "(rider opted vs not) — see engine.medical.compute_medical_uw_terminal_decision",
                "adverse_health_flag": "engine: derive from grid medicals — "
                "underwriting_platform.engine.medical.compute_medical_risk + lab_abnormalities",
                "curr_state": "engine: derive from PINCD via config/pincode_top_locations.json",
            },
            # Fields not present in this xlsx and not derivable
            "gaps_not_available": [
                # Bureau scores — only ECM_SCORE is in the xlsx; the
                # other six bureau feeds aren't shared with the POC.
                # Per client guidance (2026-05-26): for FINANCL_WAVER_FLG='N'
                # cases the bureau review applies but the scores are not
                # on file — renderer surfaces a placeholder.
                "PB_RISK_SCORE",
                "AUREUS_RISK_SCORE",
                "CRIF_SCORE",
                "CRIF_MIN_INC",
                "CRIF_MAX_INC",
                "CRIF_AVG_INC",
                "EXP_SCORE",
                "EXP_MIN_INC",
                "EXP_MAX_INC",
                "EXP_AVG_INC",
                "TU_SCORE",
                "TU_MIN_INC",
                "TU_MAX_INC",
                "TU_AVG_INC",
                "PAYU_SCORE",
                "PAYU_MIN_INC",
                "PAYU_MAX_INC",
                "PAYU_AVG_INC",
                # HHW policy number (we have the flag-derivation; not the link)
                "HUSBAND_HOUSEWIFE_PLCY_NUM",
                # Agent / channel context not in this xlsx
                "AGNT_CD",
                "AGNT_NM",
                "PASA",
                "CAMPNG_CD",
                "CUST_SEGMT",
                "CUST_CLS",
                # UW ground truth (issued/postponed) — not in this xlsx
                "APP_STATUS",
                "APP_STAGE_STATUS",
                # Misc fields not in this xlsx
                "SAR",
                "FSAR",
                "PLCY_PACK_ID",
                "BUSINESS_TYP",
                "SRC_SYSTEM",
                "Channel",
            ],
            "gaps_note": (
                "403_POC_cases.xlsx ships ~30 of the 68 columns the "
                "engine knows about. Six bureau scores, HHW link, "
                "agent/channel context, and UW ground truth (APP_STATUS) "
                "are not provided for this POC. Bureau-review applicability "
                "is inferred from FINANCL_WAVER_FLG='N'; rider + adverse-"
                "health flags are derived from medical extraction. "
                "Renderer must show 'Not on file (POC)' for the rest."
            ),
        }
        # ── Derive husband-housewife flag from applicant occupation when
        # we have it. The orchestrator can override this once the identity
        # agent has run; the policy_data row keeps a coarse default.
        if index[plcy].get("hhw_flag") is None:
            index[plcy]["hhw_flag"] = None  # left for orchestrator/identity to fill
        # ── Bureau-review applicability flag: when financial waiver = 'N',
        # bureau review is in scope even if individual scores aren't here.
        fw = (index[plcy].get("financial_waiver") or "").strip().upper()
        index[plcy]["bureau_review_applicable"] = fw == "N"
    wb.close()

    _INDEX = index
    _LOADED_PATH = resolved
    _loaded_revisions["_LOADED_PATH"] = revision
    logger.info("[policy_data] indexed %d rows from %s", len(index), resolved.name)
    return index


def lookup(case_id: str, path: Optional[str | Path] = None) -> Optional[dict]:
    """Return the Policy_Data row for `case_id`, or None if absent."""
    if not case_id:
        return None
    load_policy_data(path)
    return (_INDEX or {}).get(case_id.strip())
