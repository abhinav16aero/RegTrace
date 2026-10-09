"""M1 acceptance tests against real PostgreSQL and the HTTP application."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from dev import migrate_and_seed
from regtrace.api import create_app
from regtrace.settings import Settings
from regtrace.storage import transaction, ArtifactStore
from regtrace import service, worker

@pytest.fixture(scope='module')
def database_server():
    name='regtrace-m1-test-'+uuid.uuid4().hex[:10]
    image=json.loads((ROOT/'config/runtime.json').read_text())['postgres_image']
    def docker(*args):return subprocess.run(['docker',*args],check=True,capture_output=True,text=True)
    docker('run','-d','--rm','--name',name,'--tmpfs','/var/lib/postgresql/data',
           '-e','POSTGRES_HOST_AUTH_METHOD=trust','-p','127.0.0.1::5432',image)
    try:
        for _ in range(80):
            ready=subprocess.run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres'],capture_output=True)
            if ready.returncode==0:break
            time.sleep(.2)
        port=json.loads(docker('inspect',name).stdout)[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
        yield f'127.0.0.1:{port}'
    finally:docker('rm','-f',name)

@pytest.fixture
def world(database_server,tmp_path):
    name='test_'+uuid.uuid4().hex
    with psycopg.connect(f'postgresql://postgres@{database_server}/postgres',autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    admin=f'postgresql://postgres@{database_server}/{name}'
    users=[dict(username=u,tenant_id=t,role=r,password='test-password-only') for u,t,r in
           [('analyst','demo-bank','analyst'),('reviewer','demo-bank','reviewer'),('other-reviewer','other-bank','reviewer')]]
    migrate_and_seed(admin,'test-app-only','test-auth-only',users)
    settings=Settings(f'postgresql://regtrace_app:test-app-only@{database_server}/{name}',
                      f'postgresql://regtrace_auth:test-auth-only@{database_server}/{name}',tmp_path/'artifacts')
    clients=[]
    def client(username=None):
        c=TestClient(create_app(settings));c.headers['origin']=settings.origin;clients.append(c)
        if username:
            response=c.post('/api/login',json={'username':username,'password':'test-password-only'})
            assert response.status_code==200,response.text
            c.headers['x-csrf-token']=response.json()['csrf_token']
        return c
    yield settings,admin,client,tmp_path
    for c in clients:c.close()
    with psycopg.connect(f'postgresql://postgres@{database_server}/postgres',autocommit=True) as conn:
        conn.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def load(settings,client):
    result=client.post('/api/jobs');assert result.status_code==202,result.text
    assert worker.tick(settings)
    return result.json()['id']


def test_tenant_roles_sessions_csrf_and_artifact_boundaries(world):
    settings,admin,client,_=world
    anon=client();assert anon.get('/api/assessments').status_code==401
    analyst=client('analyst');job=load(settings,analyst)
    other=client('other-reviewer')
    assert other.get('/api/assessments').json()==[]
    assert other.get('/api/jobs').json()==[]
    assert other.get('/api/audit').json()==[]
    assert other.get('/api/assessments/A1-v1').status_code==404
    assert other.get('/api/entities/EV1-v1/artifact').status_code==404
    assert other.post('/api/jobs/'+job+'/cancel').status_code==404
    body={'decision':'approved','note':'Accept finding','expected_revision':0}
    assert analyst.post('/api/assessments/A1-v1/reviews',json=body).status_code==403
    reviewer=client('reviewer');reviewer.headers['x-csrf-token']='wrong'
    assert reviewer.post('/api/assessments/A1-v1/reviews',json=body).status_code==403
    reviewer.headers['origin']='https://untrusted.example'
    assert reviewer.post('/api/login',json={'username':'reviewer','password':'test-password-only'}).status_code==403
    assert analyst.get('/api/entities/EV1-v1/artifact').text.startswith('case_id,')
    assert analyst.get('/api/entities/%2e%2e%2f/artifact').status_code==404
    assert analyst.post('/api/logout').status_code==200
    assert analyst.get('/api/me').status_code==401
    with transaction(settings.app_dsn,'other-bank') as conn:
        assert conn.execute("SELECT count(*) AS n FROM entity_versions WHERE tenant_id='demo-bank'").fetchone()['n']==0
        role=conn.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        assert not role['rolsuper'] and not role['rolbypassrls']


def test_idempotency_concurrent_requests_and_full_provenance(world):
    settings,admin,client,_=world
    c=client('analyst');bundle,h,config=service.fixture_bundle(settings,'demo-bank')
    def submit(_):
        with transaction(settings.app_dsn,'demo-bank') as conn:
            return service.enqueue(conn,'demo-bank','analyst',bundle,h,config)['id']
    with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(submit,range(8)))
    assert len(set(ids))==1
    assert worker.tick(settings)
    assert c.post('/api/jobs').json()['id']==ids[0]
    assert not worker.tick(settings)
    assert len(c.get('/api/assessments').json())==2
    detail=c.get('/api/assessments/A1-v1').json()
    expected=json.loads((ROOT/'data/fixtures/onboarding.json').read_text())['expected_queries']['assessment_provenance']
    assert sorted(e['id'] for e in detail['provenance'])==expected
    assert {e['kind'] for e in detail['followups']}=={'gap','remediation'}
    with transaction(settings.app_dsn,'demo-bank') as conn:
        assert conn.execute('SELECT count(*) AS n FROM entity_versions').fetchone()['n']==22
        assert conn.execute('SELECT count(*) AS n FROM dependencies').fetchone()['n']==31
        assert conn.execute("SELECT count(*) AS n FROM audit_events WHERE action='fixture.imported'").fetchone()['n']==1


def test_review_and_session_survive_new_api_and_worker_process(world):
    settings,admin,client,tmp_path=world
    c=client('reviewer');job=c.post('/api/jobs').json()['id']
    claimed=worker.claim(settings,'demo-bank')
    assert claimed['status']=='running'
    # Simulate crash after claim: the worker lease expires without a completion.
    with psycopg.connect(admin) as conn:
        conn.execute("UPDATE workflow_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(job,))
    config=tmp_path/'runtime.json'
    config.write_text(json.dumps({'app_dsn':settings.app_dsn,'auth_dsn':settings.auth_dsn,
             'artifact_root':str(settings.artifact_root),'origin':settings.origin}))
    subprocess.run([sys.executable,'-I',str(ROOT/'scripts/run.py'),'worker','--once'],
                   env=os.environ|{'REGTRACE_CONFIG':str(config)},check=True,capture_output=True,text=True)
    assert not worker.finish(settings,claimed) # Stale lease cannot publish again.
    result=c.post('/api/assessments/A1-v1/reviews',json={'decision':'approved','note':'Failure verified; remediation remains needed.','expected_revision':0})
    assert result.status_code==200,result.text
    assert result.json()['current_states']['test']=='fail'
    assert not result.json()['approved_effective']
    restarted=client();restarted.cookies.update(c.cookies)
    record=restarted.get('/api/assessments/A1-v1').json()
    assert record['current_states']['review']=='approved'
    assert record['snapshot']['states']['review']=='pending'
    assert record['review_revision']==1 and record['reviews'][0]['note'].startswith('Failure verified')
    job_state=restarted.get('/api/jobs').json()[0]
    assert job_state['attempts']==2 and job_state['status']=='awaiting_review'
    assert len(record['provenance'])==13
    stale=c.post('/api/assessments/A1-v1/reviews',json={'decision':'rejected','note':'Stale form','expected_revision':0})
    assert stale.status_code==409
    assert c.post('/api/assessments/A2-v1/reviews',json={'decision':'approved','note':'Synthetic sample reviewed.','expected_revision':0}).status_code==200
    assert c.get('/api/jobs').json()[0]['status']=='completed'
    assert c.get('/api/assessments/A1-v1').json()['current_states']['test']=='fail'
    assert any(e['action']=='job.completed' for e in c.get('/api/audit').json())
    assert c.post('/api/assessments/A1-v1/reviews',json={'decision':'changes_requested','note':'Additional evidence requested.','expected_revision':1}).status_code==200
    assert c.get('/api/jobs').json()[0]['status']=='awaiting_review'
    assert c.get('/api/assessments/A1-v1').json()['current_states']['test']=='fail'


def test_cancel_is_durable_and_retry_does_not_duplicate(world):
    settings,admin,client,_=world;c=client('analyst')
    job=c.post('/api/jobs').json()['id']
    assert c.post('/api/jobs/'+job+'/cancel').json()['status']=='cancelled'
    assert not worker.tick(settings)
    assert c.get('/api/assessments').json()==[]
    assert c.post('/api/jobs/'+job+'/retry').status_code==200
    claimed=worker.claim(settings,'demo-bank')
    assert c.post('/api/jobs/'+job+'/cancel').json()['cancel_requested']
    worker.finish(settings,claimed)
    assert c.get('/api/jobs').json()[0]['status']=='cancelled'
    assert c.get('/api/assessments').json()==[]
    c.post('/api/jobs/'+job+'/retry');worker.tick(settings)
    assert len(c.get('/api/assessments').json())==2
    assert c.post('/api/jobs/'+job+'/cancel').status_code==409


def test_corrupt_bundle_rolls_back_all_entities_and_records_failure(world):
    settings,admin,client,_=world;c=client('analyst')
    bundle,h,config=service.fixture_bundle(settings,'demo-bank')
    # Extra invalid edge is rejected before publication.
    bundle['dependencies'].append({'tenant_id':'demo-bank','dependent_id':'A1-v1','input_id':'BANK-v1','relation':'maps_control'})
    h=hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with transaction(settings.app_dsn,'demo-bank') as conn:service.enqueue(conn,'demo-bank','analyst',bundle,h,config)
    worker.tick(settings)
    assert c.get('/api/assessments').json()==[]
    assert c.get('/api/jobs').json()[0]['status']=='failed'
    assert any(e['action']=='job.failed' for e in c.get('/api/audit').json())
    with transaction(settings.app_dsn,'demo-bank') as conn:
        assert conn.execute('SELECT count(*) AS n FROM entity_versions').fetchone()['n']==0


def test_hash_verified_artifacts_and_append_only_audit(world):
    settings,admin,client,_=world;c=client('reviewer');load(settings,c)
    ev=json.loads((ROOT/'data/fixtures/onboarding.json').read_text())['entities']
    digest=next(e['sha256'] for e in ev if e['kind']=='evidence')
    store=ArtifactStore(settings.artifact_root)
    with pytest.raises(ValueError):store.put(b'changed',digest)
    path=store.path(digest);path.chmod(0o644);path.write_bytes(b'corrupt')
    assert c.get('/api/entities/EV1-v1/artifact').status_code==409
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with transaction(settings.app_dsn,'demo-bank') as conn:conn.execute('DELETE FROM audit_events')


def test_session_expiry_and_account_lockout(world):
    settings,admin,client,_=world;c=client('reviewer')
    with psycopg.connect(admin) as conn:conn.execute("UPDATE sessions SET expires_at=now()-interval '1 second'")
    assert c.get('/api/me').status_code==401
    a=client()
    for _ in range(5):assert a.post('/api/login',json={'username':'analyst','password':'wrong'}).status_code==401
    assert a.post('/api/login',json={'username':'analyst','password':'test-password-only'}).status_code==401

# These use a labelled mocked model. They validate the new application path,
# not live provider quality or problem-statement completion.
def document_upload(client,reg='Banks must verify identity.',policy='Operations verifies identity.',evidence=None):
    files={'regulation':('reg.txt',reg.encode(),'text/plain'),'policy':('policy.txt',policy.encode(),'text/plain')}
    if evidence is not None:files['evidence']=('evidence.csv',evidence,'text/csv')
    return client.post('/api/extractions',files=files,data={'profile':json.dumps({
       'name':'Synthetic test bank','jurisdiction':'IN','entity_type':'bank','products':['deposits'],
       'customer_categories':['adult'],'channels':['branch'],'unknown_facts':[]}), 'synthetic':'true'})


def mock_extractor(config,role,excerpt,repair=None):
    from regtrace import extraction as ex
    common={'quotes':[excerpt]}
    result=ex.Extracted(obligations=[],controls=[],unresolved_context=['Applicability has not been evaluated.'])
    if role=='regulation':result.obligations.append(ex.Obligation(actor='Banks',action=excerpt,object='identity',conditions=[],exceptions=[],timing='',**common))
    else:result.controls.append(ex.Control(name=excerpt,owner_role='Operations',scope='',frequency='',expected_evidence=[],**common))
    return result,{'provider':'mock-groq','model':'test-double','latency_ms':1,'usage':{'total_tokens':10}},None


def test_document_extraction_changes_with_inputs_and_is_tenant_isolated(world,monkeypatch):
    from regtrace import extraction as ex
    settings,admin,client,_=world;c=client('analyst');other=client('other-reviewer')
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex,'call_model',mock_extractor)
    upload=document_upload(c,evidence=b'case,reviewer\n1,A\n');assert upload.status_code==202,upload.text
    id=upload.json()['id'];assert document_upload(c,evidence=b'case,reviewer\n1,A\n').json()['id']==id
    assert worker.tick(settings)
    result=c.get('/api/extractions/'+id).json()
    assert result['complete'] and len(result['items'])==2 and len(result['model_calls'])==2
    assert result['inputs']['evidence']['execution_status']=='not_tested'
    assert result['assessment_status']=='not_assessed' and c.get('/api/assessments').json()==[]
    for item in result['items']:
        citation=item['citations'][0]
        path='/api/extractions/'+id+'/sources/'+citation['document_id']
        source=c.get(path).text
        assert source[citation['start']:citation['end']]==citation['text']
        assert other.get(path).status_code==404
    assert other.get('/api/extractions/'+id).status_code==404
    second=document_upload(c,'Banks must record reviewer identity.','Supervisor records reviewer name.').json()['id']
    worker.tick(settings)
    changed=c.get('/api/extractions/'+second).json()
    assert changed['items']!=result['items'] and changed['inputs']!=result['inputs']
    with transaction(settings.app_dsn,'other-bank') as conn:
        assert conn.execute('SELECT count(*) AS n FROM document_sets').fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']==0


def test_extraction_missing_key_and_bad_citations_never_publish_fixture(world,monkeypatch):
    from regtrace import extraction as ex
    settings,admin,client,_=world;c=client('analyst')
    monkeypatch.delenv('GROQ_API_KEY',raising=False);monkeypatch.delenv('GROQ_API_KEY_FILE',raising=False)
    id=document_upload(c).json()['id'];worker.tick(settings)
    result=c.get('/api/extractions/'+id).json()
    assert result['job']['error_code']=='provider_key_missing' and result['items']==[]
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key')
    def invalid(*args,**kwargs):
        parsed,meta,error=mock_extractor(*args,**kwargs)
        parsed.obligations[0].quotes=['Fabricated source text']
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',invalid)
    c.post('/api/jobs/'+id+'/retry');worker.tick(settings)
    result=c.get('/api/extractions/'+id).json()
    assert not result['complete'] and result['items']==[] and len(result['model_calls'])==2
    assert result['job']['error_code']=='citation_not_unique_or_not_found'
    assert c.get('/api/assessments').json()==[]
    # Explicit user retry starts a new bounded budget after fixing configuration/provider.
    monkeypatch.setattr(ex,'call_model',mock_extractor)
    assert c.post('/api/jobs/'+id+'/retry').status_code==200
    worker.tick(settings)
    assert c.get('/api/extractions/'+id).json()['complete']


def test_extraction_repairs_citation_and_resumes_saved_steps(world,monkeypatch):
    from regtrace import extraction as ex
    settings,admin,client,_=world;c=client('analyst');monkeypatch.setenv('GROQ_API_KEY','offline-test-key')
    attempts=[]
    def repair(config,role,excerpt,error=None):
        attempts.append((role,error))
        parsed,meta,err=mock_extractor(config,role,excerpt,error)
        if role=='regulation' and error is None:parsed.obligations[0].quotes=['unsupported text']
        if role=='policy':raise RuntimeError('Simulated worker interruption after regulation checkpoint')
        return parsed,meta,err
    monkeypatch.setattr(ex,'call_model',repair)
    id=document_upload(c).json()['id'];worker.tick(settings)
    partial=c.get('/api/extractions/'+id).json()
    assert not partial['complete'] and partial['checked_chunks']==1 and len(partial['model_calls'])==2
    assert attempts[1][1]=='citation_not_unique_or_not_found'
    monkeypatch.setattr(ex,'call_model',mock_extractor);worker.tick(settings)
    completed=c.get('/api/extractions/'+id).json()
    assert completed['complete'] and completed['checked_chunks']==2 and len(completed['model_calls'])==3


def test_extraction_cancellation_and_stale_worker_cannot_publish(world,monkeypatch):
    from regtrace import extraction as ex
    settings,admin,client,_=world;c=client('analyst');monkeypatch.setenv('GROQ_API_KEY','offline-test-key')
    id=document_upload(c).json()['id'];claimed=worker.claim(settings,'demo-bank')
    def cancel(*args):
        assert c.post('/api/jobs/'+id+'/cancel').status_code==200
        return mock_extractor(*args)
    monkeypatch.setattr(ex,'call_model',cancel);worker.finish(settings,claimed)
    result=c.get('/api/extractions/'+id).json()
    assert result['job']['status']=='cancelled' and not result['items']
    assert worker.finish(settings,claimed) is False


def test_computed_assessment_executes_csv_and_changes_outcome(world,monkeypatch):
    from regtrace import extraction as ex, assessment_pipeline as ap
    settings,admin,client,_=world;c=client('analyst');monkeypatch.setenv('GROQ_API_KEY','offline-test-key')
    context={'producer':'Synthetic onboarding system','collected_at':'2026-09-03T00:00:00Z',
      'period_start':'2026-09-01T00:00:00Z','period_end':'2026-09-03T00:00:00Z','scope':'Retail deposit account activation sample for Synthetic test bank'}
    def model(config,role,excerpt,repair=None,client=None,output_type=None):
        if role in ('regulation','policy'):return mock_extractor(config,role,excerpt,repair)
        data=json.loads(excerpt)
        if role=='plan':
            obligation=next(i for i in data['items'] if i['kind']=='obligation');control=next(i for i in data['items'] if i['kind']=='control')
            result=ap.Plan(judgments=[ap.Judgment(obligation_id=obligation['id'],applicability='applicable',reason='Synthetic deposit scope matches the supplied bank.',missing_facts=[],
              mappings=[ap.Candidate(control_id=control['id'],clauses=[ap.Coverage(clause=k,status='full',rationale='The cited policy covers this requirement.') for k in data['clauses'][obligation['id']]])],
              evidence_relevance='relevant',evidence_reason='The supplied metadata declares the same activation sample.',
              procedure=ap.Procedure(control_id=control['id'],kind='timestamp_order',case_id_column='case_id',first_column='review_at',second_column='activation_at',rationale='Review must precede activation.'))])
        elif role=='remedies':
            result=ap.Remedies(remedies=[ap.Remedy(gap_id=g['gap_id'],action='Add a pre-activation check and correct the failed sample cases.',owner_role='Operations',acceptance_criteria=['Re-run the declared timestamp test on corrected evidence.']) for g in data['gaps']])
        else:result=ap.Verification(checks=[ap.Challenge(obligation_id=r['obligation_id'],supported=True,reason='Synthetic test verifier supports the stated sample-only interpretation.') for r in data['computed']])
        return result,{'provider':'mock-groq','model':'test-double','latency_ms':1},None
    monkeypatch.setattr(ex,'call_model',model)
    def assess(csv):
        upload=document_upload(c,'Banks must review identity before account activation.','Operations reviews identity before account activation.',csv)
        id=upload.json()['id'];worker.tick(settings)
        response=c.post('/api/extractions/'+id+'/assess',json={'evidence_context':context})
        assert response.status_code==202,response.text
        job_id=response.json()['id'];worker.tick(settings)
        job=next(j for j in c.get('/api/jobs').json() if j['id']==job_id)
        assert job['status']=='awaiting_review',job
        return c.get('/api/assessments/'+job['output_ids'][0]).json()
    failed=assess(b'case_id,review_at,activation_at\nC1,2026-09-02T11:00:00Z,2026-09-02T10:00:00Z\n')
    assert failed['current_states']['test']=='fail'
    assert failed['run_context']['evidence_context']==context
    assert {m['step'] for m in failed['run_context']['model_calls']}=={'plan','remedies','verify'}
    assert any(e['kind']=='gap' and e['gap_type']=='execution_failure' for e in failed['followups'])
    assert any(e['kind']=='remediation' for e in failed['followups'])
    passed=assess(b'case_id,review_at,activation_at\nC1,2026-09-02T09:00:00Z,2026-09-02T10:00:00Z\n')
    assert passed['current_states']['test']=='pass'
    assert not any(e['kind']=='gap' and e['gap_type']=='execution_failure' for e in passed['followups'])
    assert failed['snapshot']['id']!=passed['snapshot']['id'] and not passed['approved_effective']
    assert c.get('/api/assessments/'+failed['snapshot']['id']).json()['current_states']['test']=='fail'
    other=client('other-reviewer')
    assert other.get('/api/assessments/'+passed['snapshot']['id']).status_code==404
