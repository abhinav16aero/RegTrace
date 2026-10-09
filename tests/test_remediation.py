"""Step 4: mocked planning, real PostgreSQL, actual CSV execution and reviewer gates."""
import json
import pytest
import psycopg
from regtrace import extraction as ex, assessment_pipeline as ap, worker
from regtrace.storage import transaction, ArtifactStore
from test_m1_integration import world, database_server, document_upload, mock_extractor

CONTEXT={'producer':'Synthetic onboarding system','collected_at':'2026-09-03T00:00:00Z','period_start':'2026-09-01T00:00:00Z','period_end':'2026-09-03T00:00:00Z','scope':'Synthetic retail activation sample'}
FAIL=b'case_id,review_at,activation_at\nC1,2026-09-02T11:00:00Z,2026-09-02T10:00:00Z\n'
PASS=FAIL.replace(b'T11:',b'T09:')

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
@pytest.fixture
def scenario(world,monkeypatch):
    settings,admin,client,tmp=world
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex,'call_model',model)
    c=client('analyst');r=client('reviewer')
    upload=document_upload(c,'Banks must review identity before account activation.','Operations reviews identity before account activation.',FAIL)
    extraction=upload.json()['id'];worker.tick(settings)
    queued=c.post('/api/extractions/'+extraction+'/assess',json={'evidence_context':CONTEXT});assert queued.status_code==202,queued.text
    worker.tick(settings)
    tasks=c.get('/api/remediations').json();task=next(t for t in tasks if t['gap']['gap_type']=='execution_failure')
    return settings,admin,c,r,client,task['remediation']['id'],extraction

def detail(c,id):return c.get('/api/remediations/'+id).json()
def action(c,id,action,**extra):
    return c.post('/api/remediations/'+id+'/actions',json={'action':action,'expected_revision':detail(c,id)['revision'],'note':'Test correction and review.',**extra})
def start(c,r,id):
    owner=c.get('/api/me').json()['id']
    for response in [action(c,id,'assign',owner_id=owner),action(r,id,'accept'),action(c,id,'start')]:assert response.status_code==200,response.text

def retest(c,id,csv=PASS):
    return c.post('/api/remediations/'+id+'/retests',data={'expected_revision':str(detail(c,id)['revision']),'context':json.dumps(CONTEXT),'note':'Corrected case C1 with retained scope.'},files={'evidence':('revised.csv',csv,'text/csv')})
def finish(settings,c,id,csv=PASS):
    response=retest(c,id,csv);assert response.status_code==202,response.text
    worker.tick(settings)
    jobs=c.get('/api/jobs').json();job=next(j for j in jobs if j['id']==response.json()['id']);assert job['status']=='completed',job
    return detail(c,id)

def test_retest_closure_preserves_original_and_later_failure_reopens(scenario):
    settings,admin,c,r,clients,id,extraction=scenario
    initial=detail(c,id);assert initial['priority']['score'] is None and initial['retest_available']
    assert action(r,id,'close',criteria_confirmed=True).status_code==409
    start(c,r,id);passed=finish(settings,c,id)
    assert passed['state']['latest_retest_outcome']=='pass' and passed['state']['status']=='awaiting_retest'
    assert action(c,id,'close',criteria_confirmed=True).status_code==403
    assert action(r,id,'close').status_code==409
    assert action(r,id,'close',criteria_confirmed=True).json()['state']['status']=='closed'
    finding=c.get('/api/assessments/'+initial['assessment_id']).json()
    assert finding['current_states']['test']=='fail'
    linked=next(t for t in finding['linked_remediations'] if t['id']==id)
    assert linked['state']['status']=='closed' and linked['state']['latest_retest_outcome']=='pass'
    evidence=passed['retests'][0]['payload']['evidence'][0]['id']
    assert c.get('/api/entities/'+evidence+'/artifact').content==PASS
    other=clients('other-reviewer');assert other.get('/api/remediations').json()==[]
    assert other.get('/api/remediations/'+id).status_code==404
    assert other.get('/api/entities/'+evidence+'/artifact').status_code==404
    failed=finish(settings,c,id,FAIL)
    assert failed['state']['status']=='reopened' and failed['state']['latest_retest_outcome']=='fail'
    assert action(r,id,'close',criteria_confirmed=True).status_code==409
    assert [h['revision'] for h in failed['history']]==list(range(1,failed['revision']+1))
    with pytest.raises(psycopg.Error),transaction(settings.app_dsn,'demo-bank') as conn:
        conn.execute("UPDATE remediation_events SET note='rewrite' WHERE tenant_id='demo-bank'")

@pytest.mark.parametrize('csv',[PASS.replace(b'C1',b'C2'),PASS.replace(b'2026-09-02T09:00:00Z',b'invalid')])
def test_omitted_failures_or_invalid_evidence_cannot_pass(scenario,csv):
    settings,admin,c,r,clients,id,_=scenario;start(c,r,id)
    result=finish(settings,c,id,csv)
    assert result['state']['latest_retest_outcome']=='inconclusive'
    assert action(r,id,'close',criteria_confirmed=True).status_code==409

def test_priority_requires_reviewer_supported_reasons_and_revision(scenario):
    settings,admin,c,r,clients,id,_=scenario
    original=detail(c,id);ref=original['assessment_id']
    factors={k:{'value':v,'reason':'Reviewer assessment using this finding.','source_ids':[ref]} for k,v in [('severity',5),('exposure',2),('urgency',4)]}
    assert action(c,id,'prioritize',factors=factors).status_code==403
    result=action(r,id,'prioritize',factors=factors);assert result.status_code==200,result.text
    assert result.json()['priority']['score']==78
    assert action(r,id,'prioritize',factors=factors,expected_revision=0).status_code==409
    factors['severity']['source_ids']=['unrelated-source'];assert action(r,id,'prioritize',factors=factors).status_code==409
    factors['severity']['source_ids']=[ref];factors['severity']['value']=6
    assert action(r,id,'prioritize',factors=factors).status_code==422
    result=action(r,id,'prioritize',factors={});assert result.json()['priority']['score'] is None
    assert set(result.json()['priority']['missing_factors'])=={'severity','exposure','urgency'}

def test_cancel_retry_and_exhausted_lease_recover_task(scenario):
    settings,admin,c,r,clients,id,_=scenario;start(c,r,id)
    response=retest(c,id);job=response.json()['id']
    assert c.post('/api/jobs/'+job+'/cancel').status_code==200
    assert detail(c,id)['state']['active_retest_job_id'] is None
    assert c.post('/api/jobs/'+job+'/retry').status_code==200
    worker.tick(settings);assert detail(c,id)['state']['latest_retest_outcome']=='pass'
    response=retest(c,id);job=response.json()['id']
    with transaction(admin,'demo-bank') as conn:
        conn.execute("UPDATE workflow_jobs SET status='failed',error_code='lease_exhausted' WHERE id=%s",(job,))
    worker.tick(settings)
    assert detail(c,id)['state']['active_retest_job_id'] is None
    assert detail(c,id)['state']['status']=='reopened'

def test_source_change_reopens_closed_task(scenario):
    settings,admin,c,r,clients,id,extraction=scenario;start(c,r,id);finish(settings,c,id)
    assert action(r,id,'close',criteria_confirmed=True).status_code==200
    track=c.post('/api/regulatory-tracks',json={'name':'Synthetic retest instrument','extraction_id':extraction,'synthetic':True}).json()['id']
    response=c.post('/api/regulatory-tracks/'+track+'/versions',data={'kind':'synthetic_snapshot'},files={'regulation':('v2.txt',b'Banks must review identity and record a supervisor before account activation.','text/plain')})
    assert response.status_code==202,response.text
    worker.tick(settings);worker.tick(settings)
    changed=detail(c,id);assert changed['state']['status']=='reopened'
    assert not changed['retest_available'] and changed['history'][-1]['action']=='source_changed'
    assert action(r,id,'close',criteria_confirmed=True).status_code==409

def test_corrupted_retest_artifact_blocks_closure(scenario):
    settings,admin,c,r,clients,id,_=scenario;start(c,r,id);passed=finish(settings,c,id)
    sha=passed['retests'][0]['payload']['evidence'][0]['sha256']
    path=ArtifactStore(settings.artifact_root).path(sha);path.chmod(0o644);path.write_bytes(b'corrupted')
    assert action(r,id,'close',criteria_confirmed=True).status_code==409


def test_browser_remediation_workflow_on_isolated_server(scenario):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from regtrace.api import create_app
    settings,admin,c,r,client,id,extraction=scenario
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
        completed=subprocess.run([str(root/'.tools/node_modules/.bin/node'),str(root/'web/remediation-smoke.mjs')],
          env=os.environ|{'REGTRACE_TEST_ORIGIN':web_settings.origin},capture_output=True,text=True,timeout=90)
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert not errors,errors
    finally:
        stop.set();server.should_exit=True
        if working.is_alive():working.join(5)
        serving.join(5);sock.close()


def test_fixture_cannot_authorize_retest_closure(world):
    from test_m1_integration import load
    settings,admin,client,tmp=world;c=client('analyst');load(settings,c)
    tasks=c.get('/api/remediations').json();assert tasks
    assert all(not t['retest_available'] for t in tasks)
    assert any('fixture' in (t['retest_unavailable_reason'] or '') for t in tasks if t['gap']['gap_type']=='execution_failure')


def test_retest_lease_fencing_running_cancel_and_duplicate_submission(scenario):
    from regtrace import remediation
    settings,admin,c,r,clients,id,_=scenario;start(c,r,id)
    first=retest(c,id);assert first.status_code==202
    assert retest(c,id).status_code==409
    job=worker.claim(settings,'demo-bank')
    with transaction(admin) as conn:
        conn.execute("UPDATE workflow_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(job['id'],))
    assert worker.finish(settings,job) is False
    assert detail(c,id)['retests']==[]
    recovered=worker.claim(settings,'demo-bank');assert recovered['lease_token']!=job['lease_token']
    assert c.post('/api/jobs/'+job['id']+'/cancel').status_code==200
    worker.finish(settings,recovered)
    task=detail(c,id);assert task['state']['status']=='reopened' and task['state']['active_retest_job_id'] is None
    assert task['retests']==[]


def test_concurrent_task_edits_only_commit_once(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from regtrace import remediation, service
    settings,admin,c,r,clients,id,_=scenario
    user=r.get('/api/me').json()
    body=remediation.Action(action='accept',expected_revision=0,note='Accept proposal after review.')
    def commit():
        try:remediation.act(settings,user,id,body);return 'saved'
        except service.Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:commit(),range(2)))
    assert sorted(results)==['conflict','saved']
    assert detail(c,id)['revision']==1
