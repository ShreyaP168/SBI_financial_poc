import json
from pathlib import Path
import pytest
from underwriting_platform.agents import IdentityAgent, IncomeAgent, AgentResult, BureauAgent
from underwriting_platform.llm import Document
from underwriting_platform.orchestrator import MasterOrchestrator
from underwriting_platform.orchestrator.master import _AGENT_REGISTRY

class Client:
    async def extract_json(self,system,blocks,**kwargs):
        return {'applicant':{'applicant_name':'Test'},'agent_meta':{'confidence':0.9,
                'notes':'Source proposal.pdf','contradictions':[],'docs_consumed':['proposal.pdf']}}

@pytest.mark.asyncio
async def test_real_agent_contract_consumes_documents():
    result=await IdentityAgent(Client()).extract([Document('proposal.pdf','Test applicant')],{}, {})
    assert result.block['applicant']['applicant_name']=='Test'
    assert result.docs_consumed==['proposal.pdf']

@pytest.mark.asyncio
async def test_malformed_agent_result_is_not_accepted():
    result=await IncomeAgent(Client()).extract([Document('proposal.pdf','Test')],{}, {})
    assert result.error

def test_all_dispatch_prompts_and_configuration_packaged():
    root=Path(__file__).parents[1]/'src/underwriting_platform/agents'
    for cls in set(_AGENT_REGISTRY.values()):
        assert (root/'prompts'/cls.PROMPT_FILE).is_file()
    assert (root/'prompts/master_survey.txt').is_file()
    assert (root/'prompts/reconciler.txt').is_file()

@pytest.mark.asyncio
async def test_empty_case_does_not_call_llm(tmp_path):
    with pytest.raises(ValueError,match='No source documents'):
        await MasterOrchestrator(Client(),rules={}).run(tmp_path,'EMPTY')
