"""Exercise extraction->engine->PDF persistence without external API calls."""
from types import SimpleNamespace
from pathlib import Path
import json
import pytest
import pymupdf
from underwriting_platform.pipeline import run as pipeline
from underwriting_platform.engine import identity_verification
from underwriting_platform.orchestrator import MasterOrchestrator
from underwriting_platform.orchestrator.master import _AGENT_REGISTRY
from underwriting_platform.agents import AgentResult

@pytest.mark.asyncio
async def test_single_case_failure_writes_incomplete_and_nonzero_exit(tmp_path,monkeypatch):
    class Orch:
        def __init__(self,*a,**kw):pass
        async def run(self,*a):
            return SimpleNamespace(case_json={'case_id':'FAIL','insurer':'SBI Life',
                    'processing_errors':[{'stage':'income'}]},extraction_report={},survey={},agent_outputs={})
    async def identity(*a):pass
    monkeypatch.setattr(pipeline,'_load_env',lambda:None)
    monkeypatch.setattr(pipeline,'build_client',lambda:object())
    monkeypatch.setattr(pipeline,'MasterOrchestrator',Orch)
    monkeypatch.setattr(identity_verification,'run_identity_verification',identity)
    args=SimpleNamespace(case_dir=str(tmp_path),case_id='FAIL',output_dir=str(tmp_path/'out'),
                         proposed_sa=10000000,no_engine=False,no_pdf=False,push_db=False)
    assert await pipeline.main_async(args)==1
    dest=tmp_path/'out/FAIL'
    data=json.loads((dest/'FAIL_decision.json').read_text(encoding='utf8'))
    assert data['verdict']['label']=='INCOMPLETE'
    pdf=pymupdf.open(dest/'FAIL_case_sheet.pdf')
    assert 'INCOMPLETE' in pdf[0].get_text()
    assert (dest/'FAIL_qc_report.json').exists()

@pytest.mark.asyncio
async def test_orchestrator_agent_exception_reaches_engine_failure_state(tmp_path,monkeypatch):
    import underwriting_platform.orchestrator.master as master
    from underwriting_platform.llm import Document
    monkeypatch.setattr(master,'_collect_case_files',lambda *a:[tmp_path/'proposal.pdf'])
    monkeypatch.setattr(master,'prepare_doc',lambda *a:Document('proposal.pdf','Applicant evidence'))
    class BadAgent:
        OUTPUT_BLOCK='applicant'
        def __init__(self,*a):pass
        async def extract(self,*a,**kw):raise RuntimeError('Injected extraction failure')
    for name in ('identity','income','compliance'):
        monkeypatch.setitem(_AGENT_REGISTRY,name,BadAgent)
    class Medical:
        OUTPUT_BLOCK='medical'
        def __init__(self,*a):pass
        async def extract(self,*a,**kw):return AgentResult({'medical':{'documents_present':{}}},confidence=1.0)
    monkeypatch.setitem(_AGENT_REGISTRY,'medical',Medical)
    import underwriting_platform.engine.handwritten_reocr as reocr
    legacy_calls=[]
    async def forbidden_reocr(*a,**kw):
        legacy_calls.append(True)
        raise AssertionError('TALIC medical re-OCR must not run for SBI')
    monkeypatch.setattr(reocr,'reocr_all_sections_for_case',forbidden_reocr)
    orch=MasterOrchestrator(object(),rules={},reconcile=False)
    async def survey(*a):return {'files':[],'agents_to_dispatch':['identity','income','compliance','medical']}
    monkeypatch.setattr(orch,'_survey',survey)
    result=await orch.run(tmp_path,'FAIL')
    assert {e['stage'] for e in result.case_json['processing_errors']}=={'identity','income','compliance'}
    assert legacy_calls==[]

def test_incomplete_pdf_never_claims_verified_income(tmp_path):
    from underwriting_platform.engine import run_engine
    from underwriting_platform.renderer import render_triage_sheet
    decision=run_engine({'case_id':'MISSING','insurer':'SBI Life','processing_errors':[{'stage':'source'}]}, {},10000000)
    path=tmp_path/'missing.pdf'; render_triage_sheet(decision,path)
    doc=pymupdf.open(path); text=doc[0].get_text()
    assert 'INCOMPLETE' in text
    assert 'VERIFIED 2YR AVG' not in text
    assert 'ITR assumed' not in text

@pytest.mark.asyncio
async def test_reconciler_cannot_erase_declared_disallowed_trade(tmp_path,monkeypatch):
    monkeypatch.setenv("UW_EXTRACTION_MODE", "domain")
    import underwriting_platform.orchestrator.master as master
    from underwriting_platform.orchestrator.reconciler import ReconciliationResult
    from underwriting_platform.llm import Document
    monkeypatch.setattr(master,'_collect_case_files',lambda *a:[tmp_path/'proposal.pdf'])
    monkeypatch.setattr(master,'prepare_doc',lambda *a:Document('proposal.pdf','Applicant declaration'))
    class Identity:
        OUTPUT_BLOCK='applicant'
        def __init__(self,*a):pass
        async def extract(self,*a,**kw):return AgentResult({'applicant':{'age':35,'sum_assured':10000000,'occupation':'Self-Employed Business',
            'business_nature':'Sweet business','business_petty_trade_flag':'Yes'}},confidence=1.0)
    class Income:
        OUTPUT_BLOCK='income'
        def __init__(self,*a):pass
        async def extract(self,*a,**kw):return AgentResult({'income':{'primary_source':'itr','annual_income':2000000}},confidence=1.0)
    class Compliance:
        OUTPUT_BLOCK='compliance'
        def __init__(self,*a):pass
        async def extract(self,*a,**kw):return AgentResult({'compliance':{}},confidence=1.0)
    for name,cls in [('identity',Identity),('income',Income),('compliance',Compliance)]:monkeypatch.setitem(_AGENT_REGISTRY,name,cls)
    class Reconciler:
        async def reconcile(self,*a):return ReconciliationResult(field_overrides={
            'applicant.age':25,'applicant.sum_assured':5000000,'income.annual_income':9000000,
            'applicant.business_nature':'Private coaching from ITR',
            'applicant.business_petty_trade_flag':'Under Review',
            'applicant.declared_business_nature':'Coaching',
            'applicant':{'occupation':'Salaried'}})
    orch=MasterOrchestrator(object(),rules={},reconcile=False);orch.reconciler=Reconciler()
    async def survey(*a):return {'files':[],'agents_to_dispatch':['identity','income','compliance']}
    monkeypatch.setattr(orch,'_survey',survey)
    result=await orch.run(tmp_path,'PROFILE')
    assert result.case_json['applicant']['business_nature']=='Sweet business'
    assert result.case_json['applicant']['declared_business_nature']=='Sweet business'
    assert result.case_json['applicant']['business_petty_trade_flag']=='Yes'
    assert result.case_json['applicant']['age']==35
    assert result.case_json['applicant']['sum_assured']==10000000
    assert result.case_json['income']['annual_income']==2000000
