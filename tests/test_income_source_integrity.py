import pytest
from underwriting_platform.engine.calculator import compute_selected_income

def row(year,earned):
    return {'assessment_year':year,'earned_total':earned,'business_head':earned,'salary_head':0,'gross_total_income':earned}

def test_duplicate_itr_views_do_not_count_as_two_financial_years():
    case={'applicant':{'occupation':'Self-Employed Business'},'income':{'primary_source':'itr',
        'itr_years':[row('2025-26',500000),row('2025-26',500000),row('2024-25',300000)]}}
    calc=compute_selected_income(case)
    assert calc.value==400000
    assert calc.inputs['itr_income_audit']['years_used']==['2025-26','2024-25']

def test_conflicting_duplicate_itr_values_require_source_resolution():
    case={'income':{'primary_source':'itr','itr_years':[row('2025-26',500000),row('2025-26',600000)]}}
    with pytest.raises(ValueError,match='Conflicting ITR'):
        compute_selected_income(case)

def test_plural_salary_source_uses_documented_slips_not_reported_declaration():
    case={'income':{'primary_source':'salary_slips','annual_income':1300000,
        'salary_slips':[{'month_year':'2026-05','gross_salary':102331},
                        {'month_year':'2026-04','gross_salary':108712},
                        {'month_year':'2026-06','gross_salary':111533}]}}
    calc=compute_selected_income(case)
    assert calc.value==1290304
    assert calc.method.startswith('salary_slip')
