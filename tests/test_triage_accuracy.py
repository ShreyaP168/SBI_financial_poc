"""Regression inputs for contradictions reported in the seven-case review."""
from underwriting_platform.renderer.triage_sheet import _build_financials_col, _row_existing_cover, _build_docs_col, _build_risk_rows, _build_iib_col, _primary_decision_criterion
from underwriting_platform.engine.types import Calculation, GatingItem
from underwriting_platform.engine.verdict import make_verdict
from underwriting_platform.qc.checks_layout import check_page_count

def test_declared_income_not_labelled_verified_and_hlv_not_assumed():
    decision={'applicant':{'declared_annual_income':500000},'proposed_sa':10000000}
    calcs={'selected_income':{'value':500000,'method':'declared'},'hlv_base':{'value':12500000},
           'max_sa_eligible':{'value':12400000,'inputs':{'existing_cover':100000}}}
    rows,note=_build_financials_col(decision,calcs)
    assert rows[0][0]=='DECLARED ONLY'
    assert 'provisional' in note.lower()
    assert 'ITR assumed' not in note

def test_exceeds_hlv_cannot_render_within_hlv():
    decision={'applicant':{},'proposed_sa':10000000,
              'gating':[{'criterion':'sbi_qualification','status':'FAIL','evidence':'Income below required ₹500,000'}]}
    calcs={'selected_income':{'value':253020,'method':'salary_slip'},'hlv_base':{'value':6325500},
           'max_sa_eligible':{'value':6325500,'inputs':{'existing_cover':0}}}
    rows,note=_build_financials_col(decision,calcs)
    applied=next(v for k,v,t in rows if k=='SA APPLIED')
    assert 'exceeds remaining eligibility' in applied
    assert 'within' not in applied
    assert rows[0][0]=='SALARY ANNUALISED'

def test_qualification_failure_not_described_as_income_failure_or_low_risk():
    decision={'applicant':{'education_level':'HSC'},'proposed_sa':10000000,
              'gating':[{'criterion':'sbi_qualification','label':'Qualification','status':'FAIL',
                         'evidence':'HSC is not an acceptable combination for Agriculturist.'}]}
    rows,note=_build_financials_col(decision,{})
    assert not any(k=='INCOME PROOF BASIS' for k,v,t in rows)
    assert not any(row[0]=='Education' for row in _build_risk_rows(decision)['LOW'])

def test_declared_cover_not_called_undeclared():
    decision={'sbi_bureau':{'iib_policies':[{'status':'Inforce','sum_assured_inr':11000000}]},
              'extraction_blocks':{'compliance':{'existing_cover_declared':{'total_life_cover_other':15000000}}}}
    row=_row_existing_cover(decision,{'evidence':'Existing policy signal'})
    assert 'undeclared' not in row[1]
    assert '1,50,00,000' in row[1] and '1,10,00,000' in row[1]

def test_missing_liability_evidence_not_displayed_as_zero_foir():
    rows=_build_docs_col({'calculations':{'foir':{'value':0}}})
    assert 'Not assessed' in next(v for k,v,t in rows if k=='FOIR')

def test_all_failed_financial_gates_render_even_with_four_failures():
    decision={'applicant':{},'gating':[{'criterion':f'gate_{i}','label':f'Gate {i}','status':'FAIL',
                                     'evidence':'Mandatory unmet'} for i in range(4)]}
    assert len(_build_risk_rows(decision)['HIGH'])==4

def test_unreadable_uid_uses_plain_status():
    rows=_build_iib_col({'applicant':{'photo_matches_uid':'Unverified','photo_match':{'reasoning':'Image 1 is a blurred gradient; no discernible features.'}}})
    value=next(v for k,v,t in rows if k=='UID / AADHAAR MATCH')
    assert value=='Unable to verify: unreadable image'

def test_compact_sbi_pdf_is_not_falsely_flagged_as_stub():
    assert check_page_count({'page_count':1,'decision':{'routing':{'rule_set':'sbi_smart_shield_plus'}}})==[]

def test_referred_case_does_not_have_standard_rate_final_cell():
    verdict=make_verdict({'insurer':'SBI Life'}, {}, [GatingItem('sbi_iib_gate','IIB','FAIL')], [],10000000,[])
    assert verdict.label=='REFER'
    assert verdict.final_decision_cell=='Refer for Manual UW'

def test_decline_explanation_follows_actual_driver_before_missing_bureau():
    occupation={'criterion':'sbi_occupation_income_proof','status':'FAIL',
                'evidence':'Kirana store is a disallowed petty-business profile.'}
    decision={'gating':[{'criterion':'sbi_bureau_evidence','status':'FAIL','evidence':'Missing IIB'},occupation],
              'verdict':{'label':'DECLINE','decision_drivers':['Occupation is disallowed: '+occupation['evidence']]}}
    assert _primary_decision_criterion(decision)[0]=='sbi_occupation_income_proof'

def test_account_details_alone_do_not_prove_bank_statement_available():
    decision={'calculations':{'foir':{'value':0}},'extraction_blocks':{'assets_liabilities':{'bank_statements':[
        {'bank_name':'Bank','account_number':'masked','period_from':None,'closing_balance':None}]}}}
    rows=_build_docs_col(decision)
    assert 'Not provided' in next(v for k,v,t in rows if k=='BANK STMT')
    assert 'Not assessed' in next(v for k,v,t in rows if k=='FOIR')

def test_sbi_itr_does_not_raise_talic_only_audited_pl_requirement():
    from underwriting_platform.qc.checks_coherence import check_audited_pl_for_business
    decision={'routing':{'rule_set':'sbi_smart_shield_plus'},'applicant':{'occupation':'Self-Employed Business'},
              'extraction_blocks':{'income':{'primary_source':'itr'}}}
    assert check_audited_pl_for_business({'decision':decision})==[]

def test_absent_diagnostic_details_do_not_leak_none_into_flags():
    from underwriting_platform.engine.flags import _flags_from_identity_verification
    flags=_flags_from_identity_verification({'applicant':{}})
    assert all('None' not in f.description for f in flags if f.code=='DC_DISTANCE_NOT_DETERMINED')

def test_aml_declaration_not_presented_as_completed_external_screening():
    rows=_build_iib_col({'routing':{'rule_set':'sbi_smart_shield_plus'},'sbi_bureau':{'drc_category':'Standard'},
                        'extraction_blocks':{'compliance':{'aml_status':'Compliant',
                            'aml_evidence':{'sanctions_screening_clear':True,'pep_self_declaration':'No'}}}})
    value=next(v for k,v,t in rows if k=='AML DECLARATION')
    assert 'independent screening not evidenced' in value
    assert 'screening clear' not in value

def test_incomplete_evidence_does_not_claim_zero_cover_or_clear_prior_history():
    rows,note=_build_financials_col({'proposed_sa':10000000},{})
    assert next(v for k,v,t in rows if k=='EXISTING SA')=='Not Available'
    assert next(v for k,v,t in rows if k=='TFSAR')=='Not Available'
    history=_build_iib_col({})
    assert 'Not assessed' in next(v for k,v,t in history if k=='PRIOR DECLINE')
