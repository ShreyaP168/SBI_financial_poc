from pathlib import Path

from underwriting_platform.extraction.coordinator import extract_local_first
from underwriting_platform.llm.pdf_utils import Document
from underwriting_platform.ocr.provision_paddle import prepare_configuration


def test_provision_only_active_local_models(tmp_path):
    config = {
        "SubModules": {
            "LayoutDetection": {"model_name": "layout", "model_dir": None},
            "ChartRecognition": {"model_name": "chart", "model_dir": None},
            "RegionDetection": {"model_name": "region", "model_dir": None},
        },
        "SubPipelines": {
            "GeneralOCR": {"model_name": "ocr", "model_dir": None},
            "TableRecognition": {"model_name": "table", "model_dir": None},
            "FormulaRecognition": {"model_name": "formula", "model_dir": None},
        },
    }
    seen = []

    def resolve(name):
        seen.append(name)
        return tmp_path / name

    result = prepare_configuration(config, resolve)
    assert seen == ["layout", "ocr", "table"]
    assert not result["use_formula_recognition"]
    assert Path(result["SubModules"]["LayoutDetection"]["model_dir"]).is_absolute()


async def test_fallback_recovers_business_evidence_and_skips_malformed_fields(monkeypatch):
    monkeypatch.setenv("UW_TARGETED_LLM_FALLBACK", "true")
    calls = []

    class Client:
        async def extract_json(self, system, blocks, **kwargs):
            calls.append(kwargs)
            assert "applicant.business_nature" in system
            return {
                "fields": [
                    None,
                    {
                        "path": "applicant.age",
                        "raw_value": "not a number",
                        "value": 30,
                        "document": "business.pdf",
                        "page": 1,
                    },
                    {
                        "path": "applicant.business_nature",
                        "raw_value": "Kirana Shop",
                        "value": "Kirana Shop",
                        "document": "business.pdf",
                        "page": 1,
                    },
                ]
            }

    result, report = await extract_local_first(
        Client(), [Document("business.pdf", "Business trade described as Kirana Shop")]
    )
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 3500
    assert result.block["applicant"]["business_nature"] == "Kirana Shop"
    assert result.field_sources["applicant.business_nature"][0]["document"] == "business.pdf"
    assert report["fallback_error"] is None


async def test_provider_error_body_never_enters_report(monkeypatch):
    monkeypatch.setenv("UW_TARGETED_LLM_FALLBACK", "true")

    class Client:
        async def extract_json(self, *args, **kwargs):
            error = RuntimeError("Applicant Example PAN ABCDE1234F")
            error.code = "Applicant Example PAN ABCDE1234F"
            raise error

    result, report = await extract_local_first(Client(), [Document("p.pdf", "Applicant unknown")])
    assert report["fallback_error"] == "TARGETED_PROCESSING_FAILED"
    assert "ABCDE1234F" not in str(report)
    assert "ABCDE1234F" not in str(result.source_findings)
