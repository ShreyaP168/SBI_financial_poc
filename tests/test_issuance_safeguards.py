"""Issuance invariants and injected mandatory-processing failures."""
import copy
import importlib
import json
from pathlib import Path
from underwriting_platform.configuration.paths import CONFIG_ROOT
import pytest
from underwriting_platform.engine.run import run_engine
from underwriting_platform.engine.routing import is_sbi_case
from underwriting_platform.engine.verdict import make_verdict
from underwriting_platform.engine.types import Calculation, GatingItem
from underwriting_platform.engine.requirements import compute_requirements_to_raise, affordability_advisory
from underwriting_platform.engine.sbi_financial import check_student_agriculturist_riders, check_keyman_partnership_ee_age_cap

ROOT = Path(__file__).parents[1]
RULES = json.loads((CONFIG_ROOT/'uw_rules.json').read_text(encoding='utf8'))
SBI = json.loads((CONFIG_ROOT/'sbi_smart_shield_rules.json').read_text(encoding='utf8'))

def case():
    return {'case_id':'TEST', 'insurer':'SBI Life', 'product':'Smart Shield Plus',
            'applicant':{'age':35,'occupation':'Salaried','education_level':'Graduate',
                         'residential_status':'Resident','sum_assured':10000000,
                         'declared_annual_income':2000000,'annual_premium':10000},
            'income':{'primary_source':'salary_slip','annual_income':2000000},
            'compliance':{'kyc_status':'Verified','existing_cover_declared':{'total_life_cover_other':0}},
            'sbi_bureau':{'cibil_score':750,'drc_category':'Standard','profile_score':'Green','iib_policies':[]}}

@pytest.mark.parametrize('criterion',['sbi_student_agriculturist_riders','sbi_keyman_partnership_ee_age_cap',
    'sbi_iib_gate','sbi_qualification','sa_within_hlv','kyc','sbi_hlv_evidence','sbi_bureau_evidence',
    'sbi_piwc_requirement','future_hard_gate'])
def test_failed_gate_never_issues_even_without_requirements(criterion):
    result = make_verdict(case(), {}, [GatingItem(criterion,'Blocking eligibility','FAIL',evidence='Not eligible')], [],10000000,[])
    assert result.label not in ('STANDARD_ISSUANCE','STANDARD_ACCEPTANCE')

def test_student_adb_and_keyman_66_are_terminal():
    student=case(); student['applicant'].update(occupation='Student',adb_sum_assured=1000000)
    keyman=case(); keyman['applicant'].update(occupation='Keyman',age=66)
    for c,g in [(student,check_student_agriculturist_riders(student,SBI)),
                (keyman,check_keyman_partnership_ee_age_cap(keyman,SBI))]:
        assert g.status=='FAIL'
        assert make_verdict(c,{},[g],[],10000000,[]).label=='DECLINE'

@pytest.mark.parametrize('payload',[None,{}, {'cibil_score':750}])
def test_missing_bureau_stays_sbi_and_blocks_issuance(payload,monkeypatch):
    c=case(); c['sbi_bureau']=payload
    module=importlib.import_module('underwriting_platform.engine.run')
    monkeypatch.setattr(module,'compute_hlv_base',lambda *a,**kw:pytest.fail('Generic HLV used for SBI'))
    result=run_engine(c,RULES,10000000)
    assert result['routing']['rule_set']=='sbi_smart_shield_plus'
    assert result['verdict']['label'] not in ('STANDARD_ISSUANCE','STANDARD_ACCEPTANCE','INCOMPLETE')
    assert any(g['criterion']=='sbi_bureau_evidence' and g['status']=='FAIL' for g in result['gating'])

def test_absent_bureau_key_stays_sbi():
    c=case(); del c['sbi_bureau']
    result=run_engine(c,RULES,10000000)
    assert result['routing']['rule_set']=='sbi_smart_shield_plus'
    assert result['verdict']['label']!='STANDARD_ISSUANCE'

@pytest.mark.parametrize('module,name',[('underwriting_platform.engine.run','run_gating'),('underwriting_platform.engine.sbi_financial','run_sbi_gating'),
    ('underwriting_platform.engine.sbi_financial','compute_sbi_hlv'),('underwriting_platform.engine.sbi_financial','_aggregate_iib_sa'),
    ('underwriting_platform.engine.requirements','compute_requirements_to_raise'),('underwriting_platform.engine.run','compute_selected_income'),
    ('underwriting_platform.engine.run','derive_all_flags'),('underwriting_platform.engine.run','make_verdict'),
    ('underwriting_platform.engine.bank_statement_analyzer','attach_to_case')])
def test_processing_exception_fails_closed(module,name,monkeypatch):
    def injected(*a,**kw):raise RuntimeError('Injected failure')
    monkeypatch.setattr(importlib.import_module(module),name,injected)
    result=run_engine(case(),RULES,10000000)
    assert result['verdict']['label']=='INCOMPLETE'
    assert result['processing_errors']
    assert result['requirements_to_raise'][0]['critical']

def test_corrupt_rule_file_fails_closed(monkeypatch):
    original=Path.read_text
    def read(path,*a,**kw):
        return '{}' if path.name=='sbi_smart_shield_rules.json' else original(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',read)
    assert run_engine(case(),RULES,10000000)['verdict']['label']=='INCOMPLETE'

def test_affordability_heuristic_is_advisory_not_requirement():
    c=case(); c['applicant']['annual_premium']=400000
    calcs={'selected_income':Calculation('selected_income',2000000,method='salary_slip')}
    items=compute_requirements_to_raise(c,calcs,[])
    assert not any('source of funds' in r['text'].lower() for r in items)
    assert affordability_advisory(c,calcs)[0]['advisory_only'] is True
    assert make_verdict(c,calcs,[],[],10000000,items).label=='STANDARD_ISSUANCE'

def test_upstream_extraction_failure_blocks_engine():
    c=case(); c['processing_errors']=[{'stage':'income'}]
    assert run_engine(c,RULES,10000000)['verdict']['label']=='INCOMPLETE'

def test_empty_eligibility_evaluations_fail_closed(monkeypatch):
    monkeypatch.setattr(importlib.import_module('underwriting_platform.engine.sbi_financial'),'run_sbi_gating',lambda *a,**kw:[])
    assert run_engine(case(),RULES,10000000)['verdict']['label']=='INCOMPLETE'

@pytest.mark.parametrize('criterion',['sbi_qualification','sbi_occupation_income_proof','kyc','sbi_piwc_requirement'])
def test_unresolved_mandatory_evidence_never_issues(criterion):
    assert make_verdict(case(),{},[GatingItem(criterion,'Evidence pending','WARN')],[],10000000,[]).label=='REFER'

def test_sbi_clean_control_and_requirement_control():
    assert make_verdict(case(),{},[GatingItem('sbi_qualification','Qualified','PASS')],[],10000000,[]).label=='STANDARD_ISSUANCE'
    assert make_verdict(case(),{},[],[],10000000,[{'text':'Income proof'}]).label=='FURTHER_REQUIREMENT'

def test_terminal_occupation_is_recorded_as_driver_even_with_other_critical_flags():
    from underwriting_platform.engine.types import Flag
    gate=GatingItem('sbi_occupation_income_proof','Occupation','FAIL',evidence='Kirana is a disallowed petty-business profile.')
    verdict=make_verdict(case(),{},[gate],[Flag('OTHER','CRITICAL','Critical','Other finding')],10000000,requirements_to_raise=[])
    assert verdict.label=='DECLINE'
    assert 'Occupation is in' in verdict.decision_drivers[0]

def test_current_proposal_correspondence_not_treated_as_prior_concealment():
    c=case();c['reconciliation']={'risk_signals':[{'code':'OTHER','severity':'critical',
            'description':'Prior-decline question answered No; correspondence confirms proposal TEST was declined.'}]}
    result=run_engine(c,RULES,10000000)
    signal=result['reconciliation']['risk_signals'][0]
    assert signal['code']=='CURRENT_PROPOSAL_HISTORY'
    assert signal['severity']=='medium'

def test_declared_disallowed_trade_blocks_even_when_assessed_income_activity_differs():
    from underwriting_platform.engine.sbi_financial import check_occupation_income_proof
    c=case();c['applicant'].update(occupation='Self-Employed Business',
                                 declared_business_nature='OWNER OF SWEET BUSINESS KOLKAT',
                                 business_nature='Private coaching and commission',business_petty_trade_flag='Under Review')
    gate=check_occupation_income_proof(c,SBI,10000000)
    assert gate.status=='FAIL'
    assert 'SWEET BUSINESS' in gate.evidence

@pytest.mark.parametrize('bad',[None,[],{'insurer':'SBI Life','applicant':['invalid']}])
def test_malformed_case_fails_closed(bad):
    assert run_engine(bad,RULES,10000000)['verdict']['label']=='INCOMPLETE'

@pytest.mark.parametrize('c,expected',[
    ({'product':'Smart Shield Plus'},True),({'insurer':'SBI Life'},True),
    ({'sbi_bureau':{}},True),({'sbi_bureau':None},True),
    ({'insurer':'TATA AIA','sbi_bureau':{}},False),({'insurer':'TATA AIA'},False)])
def test_routing_is_independent_of_evidence(c,expected):
    assert is_sbi_case(c)==expected
