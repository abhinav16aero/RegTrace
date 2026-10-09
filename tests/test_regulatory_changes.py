"""Real database/API/worker tests; model and HTTP-download responses are explicit test doubles."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
import psycopg
import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from test_m1_integration import world, database_server, document_upload
from regtrace import extraction as ex, assessment_pipeline as ap, regulatory_changes as rc, worker
from regtrace.storage import transaction, ArtifactStore


def fake_model(config,role,excerpt,repair=None,client=None,output_type=None):
    meta={'provider':'mock-groq','model':'regulatory-change-test-double','latency_ms':1}
    if role in ('regulation','policy'):
        obligations=[];controls=[]
        for line in excerpt.splitlines():
            line=line.strip()
            if role=='regulation' and line.startswith(('Banks must','Insurers must')):
                action='record' if 'record' in line else ('review' if 'review' in line else 'verify')
                obj='reviewer' if 'reviewer' in line else 'identity'
                obligations.append(ex.Obligation(actor='Insurers' if line.startswith('Insurers') else 'Banks',action=action,object=obj,
                  conditions=[],exceptions=[],timing='24 hours' if '24 hours' in line else ('48 hours' if '48 hours' in line else ''),quotes=[line]))
            if role=='policy' and line:
                controls.append(ex.Control(name=line,owner_role='Operations',scope='Bank accounts',frequency='Every account',expected_evidence=[],quotes=[line]))
        result=ex.Extracted(obligations=obligations,controls=controls,unresolved_context=[])
    else:
        data=json.loads(excerpt)
        if role=='plan':
            judgments=[]
            for item in [i for i in data['items'] if i['kind']=='obligation']:
                controls=[i for i in data['items'] if i['kind']=='control' and item['action'][:4] in i['name']]
                mappings=[ap.Candidate(control_id=c['id'],clauses=[ap.Coverage(clause=k,status='partial' if k=='timing' and item['timing'] not in c['name'] else 'full',rationale='Mock judgment over the cited synthetic policy.') for k in data['clauses'][item['id']]]) for c in controls]
                judgments.append(ap.Judgment(obligation_id=item['id'],applicability='not_applicable' if item['actor']=='Insurers' else 'applicable',reason='Declared bank profile used by test double.',missing_facts=[],mappings=mappings,
                  evidence_relevance='unknown',evidence_reason='No execution evidence in this test.',procedure=ap.Procedure(control_id='',kind='unsupported',case_id_column='',first_column='',second_column='',rationale='No executable sample supplied.')))
            result=ap.Plan(judgments=judgments)
        elif role=='remedies':
            result=ap.Remedies(remedies=[ap.Remedy(gap_id=g['gap_id'],action='Resolve the computed finding: '+g['description'],owner_role='Operations',acceptance_criteria=['Provide and retest the missing control or evidence.']) for g in data['gaps']])
        else:result=ap.Verification(checks=[ap.Challenge(obligation_id=r['obligation_id'],supported=True,reason='Explicit mocked verifier; not live semantic validation.') for r in data['computed']])
    return result,meta,None

@pytest.fixture
def scenario(world,monkeypatch):
    settings,admin,client,tmp=world
    monkeypatch.setenv('GROQ_API_KEY','test-key-not-live');monkeypatch.setattr(ex,'call_model',fake_model)
    return settings,admin,client('analyst'),client,tmp


def baseline(settings,c,reg='Banks must verify identity.',policy='Operations verifies identity.',assess=True):
    r=document_upload(c,reg,policy);assert r.status_code==202,r.text
    id=r.json()['id'];worker.tick(settings)
    assert c.get('/api/extractions/'+id).json()['complete']
    job=None
    if assess:
        r=c.post('/api/extractions/'+id+'/assess',json={});assert r.status_code==202,r.text
        job=r.json()['id'];worker.tick(settings)
    return id,job


def track(c,id,**extra):
    r=c.post('/api/regulatory-tracks',json={'name':'Synthetic test instrument','extraction_id':id,'synthetic':True}|extra)
    assert r.status_code==201,r.text
    return r.json()['id']


def revision(c,id,text,kind='synthetic_snapshot'):
    r=c.post('/api/regulatory-tracks/'+id+'/versions',files={'regulation':('revision.txt',text.encode(),'text/plain')},data={'kind':kind})
    assert r.status_code==202,r.text
    return r.json()


def job_record(c,id):
    return next(j for j in c.get('/api/jobs').json() if j['id']==id)


def test_whitespace_version_keeps_findings_current_and_tenant_private(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c);tid=track(c,id);original=c.get('/api/assessments/'+job_record(c,job)['output_ids'][0]).json()
    count=len(c.get('/api/jobs').json())
    v=revision(c,tid,'Banks  must\n verify identity.\n')
    assert v['status']=='formatting_only' and v['extraction_id']==id
    assert revision(c,tid,'Banks  must\n verify identity.\n')['id']==v['id']
    worker.tick(settings)
    assert len(c.get('/api/jobs').json())==count
    assert c.get('/api/assessments/'+original['snapshot']['id']).json()['current_states']['freshness']=='current'
    other=client('other-reviewer')
    assert other.get('/api/regulatory-tracks').json()==[]
    assert other.get('/api/regulatory-tracks/'+tid).status_code==404
    path='/api/regulatory-tracks/'+tid+'/versions/'+v['id']+'/source'
    assert c.get(path).text=='Banks  must\n verify identity.\n'
    assert other.get(path).status_code==404
    with pytest.raises(psycopg.errors.RaiseException):
        with transaction(settings.app_dsn,'demo-bank') as conn:
            conn.execute("UPDATE regulatory_versions SET document='{}' WHERE id=%s",(v['id'],))


def test_new_duty_marks_old_stale_and_computes_new_gap_and_remedy(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c);tid=track(c,id);old_id=job_record(c,job)['output_ids'][0]
    original=c.get('/api/assessments/'+old_id).json()['snapshot']
    v=revision(c,tid,'Banks must verify identity.\nBanks must record a reviewer.')
    worker.tick(settings);worker.tick(settings)
    detail=c.get('/api/regulatory-tracks/'+tid).json();latest=detail['versions'][0]
    assert latest['status']=='compared' and [r['type'] for r in latest['report']['changes']]==['added']
    assert latest['report']['changes'][0]['after']['citations'][0]['text']=='Banks must record a reviewer.'
    assert latest['impacts'][0]['assessment_id']==old_id and len(latest['reassessments'])==1
    old=c.get('/api/assessments/'+old_id).json()
    assert old['snapshot']==original and old['current_states']['freshness']=='stale' and not old['approved_effective']
    new_job=latest['reassessments'][0]['new_job_id'];new_ids=job_record(c,new_job)['output_ids'];assert len(new_ids)==2
    results=[c.get('/api/assessments/'+a).json() for a in new_ids]
    assert any(any(f['kind']=='gap' and f['gap_type']=='missing_control' for f in r['followups']) for r in results)
    assert any(any(f['kind']=='remediation' for f in r['followups']) for r in results)
    with transaction(settings.app_dsn,'demo-bank') as conn:
        new_payload=conn.execute('SELECT input_payload FROM workflow_jobs WHERE id=%s',(new_job,)).fetchone()['input_payload']
    assert new_payload['evidence_context'] is None
    new_ex=c.get('/api/extractions/'+v['extraction_id']).json()['inputs'];old_ex=c.get('/api/extractions/'+id).json()['inputs']
    assert new_ex['profile']==old_ex['profile'] and new_ex['evidence']==old_ex['evidence']
    assert next(d for d in new_ex['documents'] if d['role']=='policy')==next(d for d in old_ex['documents'] if d['role']=='policy')
    rc.advance(settings,'demo-bank');assert len(c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]['reassessments'])==1
    # Returning to earlier bytes must create fresh runs, not resurrect the stale original.
    reverted=revision(c,tid,'Banks must verify identity.')
    assert reverted['extraction_id']!=id
    worker.tick(settings);worker.tick(settings)
    latest=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    assert latest['reassessments'][0]['new_job_id']!=job
    reviewer=client('reviewer')
    reviewed=reviewer.post('/api/assessments/'+old_id+'/reviews',json={'decision':'approved','note':'Historical finding only','expected_revision':0}).json()
    assert reviewed['current_states']['freshness']=='stale'


def test_inapplicable_new_duty_does_not_fabricate_gap_or_touch_other_track(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c);tid=track(c,id)
    other_id,other_job=baseline(settings,c,policy='Separate operations team verifies identity.')
    other_track=track(c,other_id,name='Separate instrument')
    revision(c,tid,'Banks must verify identity.\nInsurers must record a reviewer.')
    worker.tick(settings);worker.tick(settings)
    latest=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    new_job=latest['reassessments'][0]['new_job_id']
    results=[c.get('/api/assessments/'+i).json() for i in job_record(c,new_job)['output_ids']]
    inapplicable=next(r for r in results if r['current_states']['applicability']=='not_applicable')
    assert inapplicable['followups']==[]
    assert c.get('/api/assessments/'+job_record(c,other_job)['output_ids'][0]).json()['current_states']['freshness']=='current'
    assert len(c.get('/api/regulatory-tracks/'+other_track).json()['versions'])==1


def test_amendment_does_not_replace_full_baseline_or_invent_deletions(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c);tid=track(c,id)
    amendment=revision(c,tid,'Paragraph 4 is amended: record a reviewer.','amendment')
    detail=c.get('/api/regulatory-tracks/'+tid).json()
    assert amendment['status']=='needs_review' and amendment['extraction_id'] is None
    assert detail['track']['current_extraction_id']==id
    assert detail['versions'][0]['report']['changes']==[] and detail['versions'][0]['reassessments']==[]
    assert c.get('/api/assessments/'+job_record(c,job)['output_ids'][0]).json()['current_states']['freshness']=='stale'


def test_modified_duty_finds_graph_dependants_even_when_old_job_finishes_late(scenario):
    settings,admin,c,client,_=scenario
    id,_=baseline(settings,c,'Banks must review identity within 48 hours.','Operations reviews identity within 48 hours.',assess=False)
    old_job=c.post('/api/extractions/'+id+'/assess',json={}).json()['id']
    claimed=worker.claim(settings,'demo-bank');assert claimed['id']==old_job
    tid=track(c,id);revision(c,tid,'Banks must review identity within 24 hours.')
    worker.tick(settings);rc.advance(settings,'demo-bank')
    latest=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    assert latest['report']['changes'][0]['type']=='modified' and not latest['reassessments']
    # Extend test lease so test-machine speed cannot accidentally fence this worker.
    with psycopg.connect(admin) as conn:conn.execute("UPDATE workflow_jobs SET lease_expires_at=now()+interval '5 minutes' WHERE id=%s",(old_job,))
    assert worker.finish(settings,claimed)
    old=c.get('/api/assessments/'+job_record(c,old_job)['output_ids'][0]).json()
    assert old['current_states']['freshness']=='stale'
    latest=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    assert len(latest['reassessments'])==1
    worker.tick(settings)
    new=latest['reassessments'][0]['new_job_id']
    result=c.get('/api/assessments/'+job_record(c,new)['output_ids'][0]).json()
    assert result['current_states']['coverage']=='partial'


def test_removed_all_duties_requires_review_without_blocking_worker(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c);tid=track(c,id)
    revision(c,tid,'No mandatory requirements in this synthetic replacement.')
    worker.tick(settings);worker.tick(settings)
    version=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    assert version['report']['new_obligation_count']==0 and version['reassessments']==[]
    assert c.get('/api/assessments/'+job_record(c,job)['output_ids'][0]).json()['current_states']['freshness']=='stale'


def tiny_pdf(text):
    writer=PdfWriter();page=writer.add_blank_page(width=600,height=300)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 10 200 Td ('+text+') Tj ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream);out=io.BytesIO();writer.write(out);return out.getvalue()


def test_persisted_monitor_schedule_is_bounded_and_uses_same_version_pipeline(scenario,monkeypatch):
    settings,admin,c,client,tmp=scenario;raw=tiny_pdf('Banks must verify identity.')
    store=ArtifactStore(settings.artifact_root)
    doc=ex.document(raw,'source.pdf','regulation',store)
    pol=ex.document(b'Operations verifies identity.','policy.txt','policy',store)
    profile=ex.Profile(name='Synthetic bank',jurisdiction='IN',entity_type='bank',products=[],customer_categories=[],channels=[],unknown_facts=[])
    id=ex.enqueue(settings,{'tenant_id':'demo-bank','id':'analyst'},[doc,pol],profile,None,True)['id'];worker.tick(settings)
    c.post('/api/extractions/'+id+'/assess',json={});worker.tick(settings)
    source={'id':'test-official-source','url':'https://rbidocs.rbi.org.in/test.pdf','sha256':doc['sha256'],'title':'Explicit test double'}
    monkeypatch.setattr(rc,'available_sources',lambda:[source])
    tid=track(c,id,source_id=source['id'],synthetic=False)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:rc.schedule(settings,'demo-bank'),range(4)))
    monitors=[j for j in c.get('/api/jobs').json() if j['kind']=='monitor_regulation'];assert len(monitors)==1
    calls=[]
    def download(s):calls.append(s);return tiny_pdf('Banks must record a reviewer.')
    monkeypatch.setattr(rc,'download',download)
    worker.tick(settings);worker.tick(settings);worker.tick(settings)
    assert len(calls)==1
    latest=c.get('/api/regulatory-tracks/'+tid).json()['versions'][0]
    assert latest['origin']=='monitor' and latest['status']=='compared' and len(latest['reassessments'])==1
    assert job_record(c,monitors[0]['id'])['status']=='completed'
    disabled=c.post('/api/regulatory-tracks/'+tid+'/monitor',json={'enabled':False,'interval_seconds':3600,'check_now':True})
    assert disabled.status_code==200
    rc.schedule(settings,'demo-bank')
    assert len([j for j in c.get('/api/jobs').json() if j['kind']=='monitor_regulation'])==1
    assert c.post('/api/regulatory-tracks/'+tid+'/monitor',json={'enabled':True,'interval_seconds':1}).status_code==422


def test_unchanged_historical_duty_becomes_stale_when_later_version_changes_it(scenario):
    settings,admin,c,client,_=scenario
    id,job=baseline(settings,c,'Banks must review identity within 48 hours.\nBanks must verify identity.',
                    'Operations reviews identity within 48 hours.\nOperations verifies identity.')
    tid=track(c,id)
    originals=[c.get('/api/assessments/'+i).json() for i in job_record(c,job)['output_ids']]
    unchanged=next(r for r in originals if any(e['kind']=='obligation' and e['action']=='verify' for e in r['provenance']))
    revision(c,tid,'Banks must review identity within 24 hours.\nBanks must verify identity.')
    worker.tick(settings);worker.tick(settings)
    assert c.get('/api/assessments/'+unchanged['snapshot']['id']).json()['current_states']['freshness']=='current'
    revision(c,tid,'Banks must review identity within 24 hours.')
    worker.tick(settings);worker.tick(settings)
    assert c.get('/api/assessments/'+unchanged['snapshot']['id']).json()['current_states']['freshness']=='stale'


def test_browser_change_workflow_on_isolated_server(scenario):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from regtrace.api import create_app
    settings,admin,c,client,tmp=scenario
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
        completed=subprocess.run([str(root/'.tools/node_modules/.bin/node'),str(root/'web/changes-smoke.mjs')],
          env=os.environ|{'REGTRACE_TEST_ORIGIN':web_settings.origin},capture_output=True,text=True,timeout=90)
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert not errors,errors
    finally:
        stop.set();server.should_exit=True
        if working.is_alive():working.join(5)
        serving.join(5);sock.close()
