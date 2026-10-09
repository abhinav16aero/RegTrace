"""Cross-instrument reasoning uses mocked model judgments and real persisted source graphs."""
import json
import pytest
from regtrace import cross_regulation as cr, extraction as ex, worker
from regtrace.storage import transaction, ArtifactStore
from test_m1_integration import world, database_server, document_upload
from test_remediation import model as assessment_model, CONTEXT, PASS, FAIL

REG_A='Banks must review identity before activation for retail accounts.'
REG_B='Banks must review identity before activation for corporate accounts except exempt entities.'
POLICY='Operations reviews identity before account activation.'


def model(config,role,excerpt,*args,**kwargs):
    if role=='regulation' and excerpt in (REG_A,REG_B):
        item=ex.Obligation(actor='Banks',action='review',object='identity',conditions=['retail accounts' if excerpt==REG_A else 'corporate accounts'],exceptions=[] if excerpt==REG_A else ['exempt entities'],timing='before activation',quotes=[excerpt])
        return ex.Extracted(obligations=[item],controls=[],unresolved_context=[]),{'provider':'mock-groq','model':'test-double'},None
    if role not in ('cross_compare','cross_verify'):return assessment_model(config,role,excerpt,*args,**kwargs)
    data=json.loads(excerpt)
    if role=='cross_compare':
        result=cr.Analysis(pairs=[cr.Pair(pair_id=p['pair_id'],relation='overlap',common_purpose='Identity review before activation.',rationale='The supplied duties share a review purpose with distinct account scopes.',scope_and_timing_notes=['Retail and corporate scope and the exemption remain separate.'],left_span_ids=[p['left']['spans'][0]['id']],right_span_ids=[p['right']['spans'][0]['id']]) for p in data['pairs']])
    else:result=cr.Verification(checks=[cr.Check(pair_id=p['pair_id'],supported=True,reason='Test verifier preserves separate scope and results.') for p in data['pairs']])
    return result,{'provider':'mock-groq','model':'test-double','latency_ms':1},None


def assess(settings,c,reg,csv=FAIL,policy=POLICY):
    response=document_upload(c,reg,policy,csv);assert response.status_code==202,response.text
    extraction=response.json()['id'];worker.tick(settings)
    r=c.post('/api/extractions/'+extraction+'/assess',json={'evidence_context':CONTEXT});assert r.status_code==202,r.text
    worker.tick(settings);job=c.get('/api/jobs/'+r.json()['id']).json()['job'];assert job['status']=='awaiting_review',job
    return job['id'],extraction

@pytest.fixture
def comparison(world,monkeypatch):
    settings,admin,client,tmp=world;c=client('analyst');r=client('reviewer')
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex,'call_model',model)
    left,left_ex=assess(settings,c,REG_A,FAIL);right,right_ex=assess(settings,c,REG_B,PASS)
    body={'name':'Two synthetic instruments','note':'Inspect shared review controls without transferring outcomes.','left_job_id':left,'right_job_id':right,'left_instrument':'Synthetic retail instrument','right_instrument':'Synthetic corporate instrument','distinct_confirmed':True}
    return settings,admin,c,r,client,body,left_ex,right_ex


def run(settings,c,body):
    response=c.post('/api/cross-regulation',json=body);assert response.status_code==202,response.text
    id=response.json()['id'];worker.tick(settings)
    result=c.get('/api/cross-regulation/'+id).json();assert result['job']['status']=='completed',result['job']
    return id,result


def test_shared_policy_passage_preserves_sources_scopes_outcomes_and_review(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    before=c.get('/api/assessments').json();tasks=c.get('/api/remediations').json()
    id,result=run(settings,c,body);pair=result['result']['pairs'][0]
    assert pair['relation']=='overlap' and len(pair['shared_policy_passages'])==1
    assert pair['left']['states']['test']=='fail' and pair['right']['states']['test']=='pass'
    assert pair['right']['obligation']['conditions']==['corporate accounts']
    assert pair['right']['obligation']['exceptions']==['exempt entities']
    assert pair['duty_differences'] and REG_A in pair['left']['spans'][0]['text'] and REG_B in pair['right']['spans'][0]['text']
    assert {m['step'] for m in result['model_calls']}=={'cross_compare','cross_verify'}
    review={'decision':'approved','note':'Reviewed both scopes and their separate sample findings.','expected_revision':0}
    url='/api/cross-regulation/'+id+'/pairs/'+pair['pair_id']+'/reviews'
    assert c.post(url,json=review).status_code==403
    approved=r.post(url,json=review);assert approved.status_code==200,approved.text
    assert r.post(url,json=review).status_code==409
    assert c.get('/api/assessments').json()==before and c.get('/api/remediations').json()==tasks
    assert c.post('/api/cross-regulation',json=body).json()['id']==id
    other=clients('other-reviewer');assert other.get('/api/cross-regulation').json()==[]
    assert other.get('/api/cross-regulation/'+id).status_code==404
    assert other.post(url,json=review).status_code==404
    source=next(e for e in result['snapshots'][0]['entities'] if e['kind']=='source')
    assert c.get('/api/cross-regulation/'+id+'/artifacts/'+source['id']).status_code==200
    assert other.get('/api/cross-regulation/'+id+'/artifacts/'+source['id']).status_code==404


def test_identity_profile_and_declared_distinction_gates(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    for change in [{'distinct_confirmed':False},{'right_instrument':body['left_instrument']},{'right_job_id':body['left_job_id']}]:
        assert c.post('/api/cross-regulation',json=body|change).status_code==409
    assert c.post('/api/cross-regulation',json=body|{'right_job_id':'missing'}).status_code==404
    duplicate,_=assess(settings,c,REG_A,PASS)
    assert c.post('/api/cross-regulation',json=body|{'right_job_id':duplicate}).status_code==409
    # Explicit fixture runs cannot masquerade as independent computed sources.
    fixture=c.post('/api/jobs').json()['id'];worker.tick(settings)
    assert c.post('/api/cross-regulation',json=body|{'right_job_id':fixture}).status_code==404


def test_invalid_cross_citations_fail_after_bounded_repair(comparison,monkeypatch):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    def invalid(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='cross_compare':parsed.pairs[0].left_span_ids=['invented-span']
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',invalid)
    id=c.post('/api/cross-regulation',json=body).json()['id'];worker.tick(settings)
    result=c.get('/api/cross-regulation/'+id).json()
    assert result['job']['status']=='failed' and result['job']['error_code']=='cross_citation_invalid'
    assert result['result'] is None and len(result['model_calls'])==2


def test_verifier_rejection_retains_uncertainty_and_blocks_approval(comparison,monkeypatch):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    def challenge(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='cross_verify':parsed.checks[0].supported=False
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',challenge);id,result=run(settings,c,body)
    assert result['result']['pairs'][0]['relation']=='uncertain'
    assert r.post('/api/cross-regulation/'+id+'/pairs/pair-1/reviews',json={'decision':'approved','note':'Cannot approve unsupported comparison','expected_revision':0}).status_code==409


def test_regulatory_staleness_blocks_comparison_review_and_new_runs(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison;id,result=run(settings,c,body)
    track=c.post('/api/regulatory-tracks',json={'name':'Synthetic instrument A','extraction_id':left_ex,'synthetic':True}).json()['id']
    response=c.post('/api/regulatory-tracks/'+track+'/versions',data={'kind':'synthetic_snapshot'},files={'regulation':('changed.txt',b'Banks must record supervisor approval for retail accounts.','text/plain')});assert response.status_code==202
    worker.tick(settings);worker.tick(settings)
    assert c.get('/api/cross-regulation/'+id).json()['source_stale']
    assert r.post('/api/cross-regulation/'+id+'/pairs/pair-1/reviews',json={'decision':'approved','note':'Stale source','expected_revision':0}).status_code==409
    assert c.post('/api/cross-regulation',json=body).status_code==409


def test_cancel_and_retry_reuse_safe_checkpoints(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    id=c.post('/api/cross-regulation',json=body).json()['id'];job=worker.claim(settings,'demo-bank')
    assert c.post('/api/jobs/'+id+'/cancel').status_code==200
    worker.finish(settings,job)
    assert c.get('/api/cross-regulation/'+id).json()['job']['status']=='cancelled'
    assert c.post('/api/jobs/'+id+'/retry').status_code==200
    worker.tick(settings);assert c.get('/api/cross-regulation/'+id).json()['job']['status']=='completed'


def test_browser_cross_regulation_workflow_on_isolated_server(comparison):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from regtrace.api import create_app
    settings,admin,c,r,client,body,left_ex,right_ex=comparison
    root=Path(__file__).resolve().parents[1]
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    from dataclasses import replace
    web_settings=replace(settings,origin=f'http://127.0.0.1:{port}')
    server=uvicorn.Server(uvicorn.Config(create_app(web_settings),log_level='warning'))
    serving=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    stop=threading.Event();errors=[]
    def work():
        while not stop.wait(.15):
            try:worker.tick(web_settings)
            except Exception as error:errors.append(type(error).__name__);return
    working=threading.Thread(target=work,daemon=True)
    serving.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started;working.start()
        completed=subprocess.run([str(root/'.tools/node_modules/.bin/node'),str(root/'web/cross-regulation-smoke.mjs')],
          env=os.environ|{'REGTRACE_TEST_ORIGIN':web_settings.origin},capture_output=True,text=True,timeout=90)
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert not errors,errors
    finally:
        stop.set();server.should_exit=True
        if working.is_alive():working.join(5)
        serving.join(5);sock.close()






def test_differing_bank_profiles_cannot_be_silently_combined(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    source=ex.result(settings,'demo-bank',right_ex)['inputs']
    profile=ex.Profile.model_validate(source['profile']|{'name':'Another synthetic bank'})
    job=ex.enqueue(settings,c.get('/api/me').json(),source['documents'],profile,source['evidence'],True)
    worker.tick(settings);a=c.post('/api/extractions/'+job['id']+'/assess',json={'evidence_context':CONTEXT});assert a.status_code==202
    worker.tick(settings)
    assert c.post('/api/cross-regulation',json=body|{'right_job_id':a.json()['id']}).status_code==409


def test_distinct_duties_and_different_policy_passages_are_retained(comparison,monkeypatch):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    right,_=assess(settings,c,'Banks must retain closure records for seven years.',PASS,'The records team checks the archive register.')
    def distinct(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='cross_compare':
            parsed.pairs[0].relation='distinct';parsed.pairs[0].common_purpose='';parsed.pairs[0].rationale='Identity review and record retention are different requirements.'
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',distinct)
    id,result=run(settings,c,body|{'right_job_id':right})
    pair=result['result']['pairs'][0]
    assert pair['relation']=='distinct' and pair['shared_policy_passages']==[]


def test_corrupted_source_blocks_review(comparison):
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison;id,result=run(settings,c,body)
    entity=next(e for e in result['snapshots'][0]['entities'] if e['kind']=='source')
    path=ArtifactStore(settings.artifact_root).path(entity['sha256']);path.chmod(0o644);path.write_bytes(b'corrupted')
    assert r.post('/api/cross-regulation/'+id+'/pairs/pair-1/reviews',json={'decision':'approved','note':'Cannot approve corrupt sources','expected_revision':0}).status_code==409


def test_known_same_instrument_lineage_is_rejected(comparison):
    from psycopg.types.json import Jsonb
    import uuid
    settings,admin,c,r,clients,body,left_ex,right_ex=comparison
    track=c.post('/api/regulatory-tracks',json={'name':'Known same instrument','extraction_id':left_ex,'synthetic':True}).json()['id']
    doc=next(d for d in ex.result(settings,'demo-bank',right_ex)['inputs']['documents'] if d['role']=='regulation')
    # Explicit test metadata representing two source runs already known to be one lineage.
    with transaction(admin,'demo-bank') as conn:
        conn.execute("""INSERT INTO regulatory_versions(tenant_id,id,track_id,sequence,previous_extraction_id,extraction_id,document,kind,status,origin)
          VALUES ('demo-bank',%s,%s,2,%s,%s,%s,'synthetic_snapshot','baseline','upload')""",(str(uuid.uuid4()),track,left_ex,right_ex,Jsonb(doc)))
    response=c.post('/api/cross-regulation',json=body)
    assert response.status_code==409 and 'same tracked instrument' in response.text
