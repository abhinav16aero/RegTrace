"""Rate-limit handling uses real checkpoints and mocked HTTP/model responses."""
import pytest
from regtrace import extraction as ex, worker
from test_m1_integration import world, database_server, document_upload, mock_extractor
from test_remediation import model as assessment_model, CONTEXT, FAIL


def test_extraction_throttle_preserves_semantic_repair_budget(world,monkeypatch):
    settings,admin,clients,tmp=world;c=clients('analyst');calls={};waits=[]
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex.time,'sleep',waits.append)
    def model(config,role,excerpt,*args,**kwargs):
        calls[role]=calls.get(role,0)+1
        if role=='regulation' and calls[role]==1:
            return None,{'provider':'mock-groq','retry_after_seconds':3},'provider_http_429'
        result,meta,error=mock_extractor(config,role,excerpt)
        if role=='regulation' and calls[role]==2:result.obligations[0].quotes=['Invented source text']
        return result,meta,error
    monkeypatch.setattr(ex,'call_model',model)
    id=document_upload(c).json()['id'];worker.tick(settings)
    d=c.get('/api/extractions/'+id).json()
    assert d['complete'] and waits==[5]
    errors=[m['metadata']['error'] for m in d['model_calls'] if m['step'].startswith('regulation-')]
    assert errors==['provider_http_429','citation_not_unique_or_not_found',None]


@pytest.mark.parametrize('delay,expected_calls,expected_waits',[(4,3,[6,6]),(120,1,[])])
def test_assessment_rate_limits_are_bounded_and_respect_long_waits(world,monkeypatch,delay,expected_calls,expected_waits):
    settings,admin,clients,tmp=world;c=clients('analyst');waits=[];calls=[]
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex.time,'sleep',waits.append)
    def model(config,role,excerpt,*args,**kwargs):
        if role=='plan':
            calls.append(role)
            return None,{'provider':'mock-groq','retry_after_seconds':delay},'provider_http_429'
        return assessment_model(config,role,excerpt,*args,**kwargs)
    monkeypatch.setattr(ex,'call_model',model)
    id=document_upload(c,'Banks must review identity before activation.','Operations checks identity before activation.',FAIL).json()['id'];worker.tick(settings)
    assessment=c.post('/api/extractions/'+id+'/assess',json={'evidence_context':CONTEXT}).json()['id'];worker.tick(settings)
    d=c.get('/api/jobs/'+assessment).json()
    assert d['job']['status']=='failed' and d['job']['error_code']=='provider_http_429'
    assert len(calls)==expected_calls and waits==expected_waits
    assert len(d['model_calls'])==expected_calls
