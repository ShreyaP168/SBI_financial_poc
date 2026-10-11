from types import SimpleNamespace
import pytest
from underwriting_platform.llm.client import LLMClient, LLMConfig

@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['max_tokens','refusal'])
async def test_partial_or_refused_response_never_becomes_usable_extraction(reason,monkeypatch):
    class Stream:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get_final_message(self):
            return SimpleNamespace(stop_reason=reason,content=[SimpleNamespace(text='{"applicant": {}}')])
    client=LLMClient(LLMConfig('claude','test','local-test-placeholder',max_retries=1,initial_backoff_s=0))
    monkeypatch.setattr(client,'_client',lambda:SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw:Stream())))
    async def no_wait(*args):pass
    monkeypatch.setattr('underwriting_platform.llm.client.asyncio.sleep',no_wait)
    with pytest.raises(RuntimeError,match='MODEL_REQUEST_REJECTED'):
        await client.extract_json('schema',[])
