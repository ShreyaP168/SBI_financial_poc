"""A synthetic extraction service for integration tests, with no provider calls."""

from underwriting_platform.configuration.settings import Settings
from underwriting_platform.contracts.v1 import ExtractionResponse
from underwriting_platform.services.extraction import build_app


def synthetic_case(case_id: str) -> dict:
    case = {
        "case_id": case_id,
        "insurer": "SBI Life",
        "product": "Smart Shield Plus",
        "applicant": {
            "applicant_name": "Synthetic Applicant",
            "gender": "Male",
            "age": 66,
            "occupation": "Keyman",
            "education_level": "Graduate",
            "residential_status": "Resident",
            "sum_assured": 10000000,
            "declared_annual_income": 2000000,
            "annual_premium": 10000,
        },
        "income": {
            "primary_source": "itr",
            "annual_income": 2000000,
            "itr_years": [
                {
                    "assessment_year": "2025-26",
                    "earned_total": 2000000,
                    "gross_total_income": 2000000,
                },
                {
                    "assessment_year": "2024-25",
                    "earned_total": 2000000,
                    "gross_total_income": 2000000,
                },
            ],
        },
        "compliance": {
            "kyc_status": "Verified",
            "existing_cover_declared": {"total_life_cover_other": 0},
        },
        "sbi_bureau": {
            "cibil_score": 750,
            "drc_category": "Standard",
            "profile_score": "Green",
            "iib_policies": [],
        },
    }
    case["_processing_stages"] = {"extraction": "completed", "identity": "completed"}
    case["field_sources"] = {
        "applicant." + key: [{"document": "synthetic-proposal.pdf", "page": 1, "raw_value": value}]
        for key, value in case["applicant"].items()
    }
    case["field_sources"]["income.itr_years"] = [
        {"document": "synthetic-itr.pdf", "page": 1, "raw_value": case["income"]["itr_years"]}
    ]
    return case


class SyntheticExtractor:
    async def extract(self, request):
        case = synthetic_case(request.case_id)
        if request.case_id.startswith("E2E_"):
            app = case["applicant"]
            app.update(age=35, occupation="Salaried", photo_match={"match_result": "match"})
            if request.case_id == "E2E_REFER":
                app["sum_assured"] = 2000000
            elif request.case_id == "E2E_KIRANA":
                app.update(occupation="Self-Employed Business", business_nature="Kirana store")
            elif request.case_id == "E2E_STUDENT_ADB":
                app.update(age=25, occupation="Student", adb_sum_assured=1000000)
            elif request.case_id == "E2E_INCOMPLETE":
                app["age"] = None
            for key, value in app.items():
                case["field_sources"]["applicant." + key] = [
                    {"document": "synthetic-proposal.pdf", "page": 1, "raw_value": value}
                ]
            if app["age"] is None:
                case["field_sources"].pop("applicant.age")
        return ExtractionResponse(
            case_id=request.case_id,
            input_fingerprint="synthetic-fixture-v1",
            case_json=case,
            extraction_report={"synthetic": True},
        )


def create_app():
    return build_app(Settings.from_env(), SyntheticExtractor())
