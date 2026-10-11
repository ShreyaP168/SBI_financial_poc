"""Link calculation and gate inputs to retained extraction evidence."""


def attach_field_evidence(decision: dict, case: dict) -> dict:
    evidence = case.get("field_sources") or {}
    decision["field_sources"] = evidence
    selected = (decision.get("calculations") or {}).get("selected_income")
    if selected:
        method = str(selected.get("method") or "")
        prefix = (
            "income.itr_years"
            if "itr" in method
            else "income.salary_slips"
            if "salary_slip" in method
            else "income.bank_salary_credits"
            if "bank" in method
            else "applicant.declared_annual_income"
            if "declared" in method
            else "income.annual_income"
        )
        records = [
            {**record, "field_path": path, "agent": record.get("agent", "extraction")}
            for path, sources in evidence.items()
            if path.startswith(prefix)
            for record in sources
        ]
        if records:
            selected["sources"] = records
        for gate in decision.get("gating") or []:
            if gate.get("criterion") == "income_documented" and not gate.get("sources"):
                gate["sources"] = selected.get("sources") or []
    for item in list((decision.get("calculations") or {}).values()) + list(
        decision.get("gating") or []
    ):
        linked = []
        for source in item.get("sources") or []:
            path = source.get("field_path") or ""
            lookup = path.replace("applicant.declared_occupation", "applicant.occupation").replace(
                "applicant.declared_business_nature", "applicant.business_nature"
            )
            records = evidence.get(lookup) or []
            if records:
                linked.extend({**source, **record, "field_path": path} for record in records)
            else:
                linked.append(source)
        item["sources"] = linked
    return decision
