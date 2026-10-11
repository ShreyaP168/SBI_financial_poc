from underwriting_platform.engine.requirements import compute_requirements_to_raise
from underwriting_platform.engine.types import Calculation, GatingItem
from underwriting_platform.qc.checks_completeness import check_placeholder_leaks
from underwriting_platform.qc.checks_pii import check_bank_account_in_prose

def test_finance_promoter_is_not_sufficient_exact_business_description():
    case={'applicant':{'occupation':'Self-Employed Business','employer_name':'Finance',
                       'business_nature':'Finance (Promoter)'}}
    requirements=compute_requirements_to_raise(case,{'selected_income':Calculation('selected_income',500000)},[])
    assert any('Exact nature of business' in r['text'] for r in requirements)

def test_specific_finance_business_does_not_require_same_clarification():
    case={'applicant':{'occupation':'Self-Employed Business','employer_name':'Example Finance LLP',
                       'business_nature':'Registered loan brokerage'}}
    assert not any('Exact nature of business' in r['text'] for r in compute_requirements_to_raise(case,{},[]))

def test_natural_english_none_is_not_python_placeholder_but_literal_none_still_fails():
    good={'reconciliation':{'reconciler_notes':'Loan details are required and none are on file; none of the declarations is verified.'}}
    bad={'reconciliation':{'reconciler_notes':'The applicant earns None per month.'}}
    assert check_placeholder_leaks({'decision':good})==[]
    assert check_placeholder_leaks({'decision':bad})

def test_policy_number_not_mistaken_for_bank_account():
    policy={'reconciliation':{'reconciler_notes':'Existing policy (Policy No. 105325482, Sum Assured Rs 100000).'}}
    bank={'reconciliation':{'reconciler_notes':'Premium paid from bank account 105325482.'}}
    assert check_bank_account_in_prose({'decision':policy})==[]
    assert check_bank_account_in_prose({'decision':bank})

def test_supplementary_rider_exposure_cannot_create_issuance_requirement():
    from underwriting_platform.engine.verdict import make_verdict
    case={'insurer':'SBI Life','applicant':{}}
    gates=[GatingItem('sbi_rider_stacked_exposure','Supplementary exposure','WARN')]
    requirements=compute_requirements_to_raise(case,{},gates)
    assert requirements==[]
    assert make_verdict(case,{},gates,[],10000000,requirements).label=='STANDARD_ISSUANCE'

def test_pending_policy_reference_not_mistaken_for_mobile_number():
    from underwriting_platform.qc.checks_pii import check_mobile_in_prose
    policy={'reconciliation':{'risk_signals':[{'description':'IIB policy/workflow 9394820309 remains pending for underwriting.'}]}}
    mobile={'reconciliation':{'risk_signals':[{'description':'Customer mobile 9394820309 is recorded in the form.'}]}}
    assert check_mobile_in_prose({'decision':policy})==[]
    assert check_mobile_in_prose({'decision':mobile})
