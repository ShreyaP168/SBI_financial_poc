"""Typed critical facts and audit structures; incomplete evidence stays explicit."""

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    document: StrictStr = Field(min_length=1)
    page: StrictInt | None = Field(default=None, gt=0)
    field_name: StrictStr | None = None
    json_pointer: StrictStr | None = None
    raw_value: Any

    @model_validator(mode="after")
    def locator(self):
        if not (self.page or self.field_name or self.json_pointer) or self.raw_value in (None, ""):
            raise ValueError("Evidence requires locator and original value")
        return self


class Applicant(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    applicant_name: StrictStr | None = None
    age: StrictInt | None = Field(default=None, gt=0, le=120)
    sum_assured: StrictInt | None = Field(default=None, gt=0)
    occupation: StrictStr | None = None
    education_level: StrictStr | None = None
    residential_status: StrictStr | None = None
    gender: StrictStr | None = None


class IncomeYear(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    assessment_year: StrictStr | None = None
    earned_total: StrictInt | StrictFloat | None = Field(default=None, ge=0)
    gross_total_income: StrictInt | StrictFloat | None = Field(default=None, ge=0)


class Income(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    annual_income: StrictInt | StrictFloat | None = Field(default=None, ge=0)
    primary_source: StrictStr | None = None
    itr_years: list[IncomeYear] = Field(default_factory=list)


class CanonicalCase(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    case_id: StrictStr
    applicant: Applicant = Field(default_factory=Applicant)
    income: Income = Field(default_factory=Income)
    field_sources: dict[str, list[Evidence]] = Field(default_factory=dict)
    processing_errors: list[dict[str, Any]] = Field(default_factory=list)
    compliance: dict[str, Any] = Field(default_factory=dict)
    sbi_bureau: dict[str, Any] | None = None


class AuditSource(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    document: StrictStr
    field_path: StrictStr


class Gate(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    criterion: StrictStr = Field(min_length=1)
    status: Literal["PASS", "WARN", "FAIL", "NA"]
    rule: StrictStr = ""
    evidence: StrictStr = ""
    sources: list[AuditSource] = Field(default_factory=list)


class Calculation(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    value: StrictInt | StrictFloat | StrictStr | bool | None
    rule: StrictStr = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)


class DecisionAudit(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    gating: list[Gate]
    calculations: dict[str, Calculation]


def case_issues(case: dict) -> list[str]:
    try:
        CanonicalCase.model_validate(case)
    except ValidationError as error:
        return sorted(
            {
                "CASE_SCHEMA:" + ".".join(map(str, item["loc"]))
                for item in error.errors(include_input=False)
            }
        )
    return []


def decision_issues(decision: dict) -> list[str]:
    try:
        audit = DecisionAudit.model_validate(decision)
    except ValidationError as error:
        return sorted(
            {
                "DECISION_SCHEMA:" + ".".join(map(str, item["loc"]))
                for item in error.errors(include_input=False)
            }
        )
    names = [gate.criterion for gate in audit.gating]
    issues = ["DUPLICATE_GATES"] if len(names) != len(set(names)) else []
    normalized = decision.get("_normalized_input")
    if isinstance(normalized, dict) and isinstance(normalized.get("applicant"), dict):
        expected = normalized["applicant"]
        reported = decision.get("applicant")
        if isinstance(reported, dict):
            for field in ("sum_assured",):
                if reported.get(field) != expected.get(field):
                    issues.append("DECISION_FACT_MISMATCH:" + field)
        age = audit.calculations.get("age")
        if (
            age
            and age.value is not None
            and isinstance(reported, dict)
            and age.value != reported.get("age")
        ):
            issues.append("CALCULATION_FACT_MISMATCH:age")
        if (
            age
            and age.value is not None
            and getattr(age, "method", "") == "from_declared_age"
            and expected.get("age") is not None
            and age.value != expected["age"]
        ):
            issues.append("DECLARED_AGE_MISMATCH")
    for name in ("age", "selected_income", "hlv_base", "max_sa_eligible"):
        calculation = audit.calculations.get(name)
        if (
            calculation
            and calculation.value is not None
            and (
                not isinstance(calculation.value, (int, float))
                or isinstance(calculation.value, bool)
            )
        ):
            issues.append("CALCULATION_TYPE:" + name)
    if (decision.get("verdict") or {}).get("label") in {
        "STANDARD_ISSUANCE",
        "STANDARD_ACCEPTANCE",
    } and any(gate.status == "FAIL" for gate in audit.gating):
        issues.append("ISSUANCE_WITH_FAILED_GATE")
    return issues
