"""Hypothetical runs execute the real pipeline without publishing live entities."""
import json
import pytest
from concurrent.futures import ThreadPoolExecutor
from regtrace import worker, scenarios, extraction as ex
from regtrace.storage import transaction
from test_m1_integration import world, database_server
from test_remediation import scenario, PASS, CONTEXT


def create(c,**overrides):
    baseline=next(j for j in c.get('/api/jobs').json() if j['kind']=='assess_documents')['id']
    fields={'baseline_job_id':baseline,'name':'Hypothetical corrected sample','note':'Correct the sample without changing the baseline.','context':json.dumps(CONTEXT)}
    files={'evidence':('revised.csv',PASS,'text/csv')}
    fields.update(overrides.pop('data',{}));files.update(overrides.pop('files',{}))
    return c.post('/api/scenarios',data=fields,files=files)

def complete(settings,c,id):
    for _ in range(4):
        worker.tick(settings)
        result=c.get('/api/scenarios/'+id).json()
        if result['status'] in ('completed','failed','cancelled'):return result
    return result


def test_recomputes_hypothetical_pass_without_mutating_baseline_or_live_queues(scenario):
    settings,admin,c,r,clients,remedy,extraction=scenario
    before=c.get('/api/assessments').json();tasks=c.get('/api/remediations').json()
    response=create(c);assert response.status_code==202,response.text
    id=response.json()['id'];result=complete(settings,c,id)
    assert result['status']=='completed',result
    rows=result['result']['comparison']['rows'];assert len(rows)==1
    assert rows[0]['before']['states']['test']=='fail' and rows[0]['after']['states']['test']=='pass'
    assert rows[0]['changed_states']==['test'] or set(rows[0]['changed_states'])=={'test'}
    assert c.get('/api/assessments').json()==before
    assert c.get('/api/remediations').json()==tasks
    assert not any(j['kind']=='assess_scenario' or j['id']==result['extraction_job']['id'] for j in c.get('/api/jobs').json())
    hypothetical=next(e for e in result['result']['entities'] if e['kind']=='assessment')
    assert c.get('/api/assessments/'+hypothetical['id']).status_code==404
    assert r.post('/api/assessments/'+hypothetical['id']+'/reviews',json={'decision':'approved','note':'Cannot promote','expected_revision':0}).status_code==404
    evidence=next(e for e in result['result']['entities'] if e['kind']=='evidence')
    assert c.get('/api/scenarios/'+id+'/artifacts/'+evidence['id']).content==PASS
    assert c.get('/api/entities/'+evidence['id']+'/artifact').status_code==404
    other=clients('other-reviewer');assert other.get('/api/scenarios').json()==[]
    assert other.get('/api/scenarios/'+id).status_code==404
    assert other.get('/api/scenarios/'+id+'/artifacts/'+evidence['id']).status_code==404
    assert c.post('/api/extractions/'+result['extraction_job']['id']+'/assess',json={}).status_code==409
    assert c.post('/api/regulatory-tracks',json={'name':'Cannot promote','extraction_id':result['extraction_job']['id'],'synthetic':True}).status_code==409
    baseline_id=before[0]['snapshot']['id']
    assert r.post('/api/assessments/'+baseline_id+'/reviews',json={'decision':'approved','note':'Review baseline later','expected_revision':0}).status_code==200
    later=c.get('/api/scenarios/'+id).json();assert later['baseline_changed_since_creation']
    assert later['baseline']==result['baseline'] and later['result']==result['result']


def test_noop_and_missing_evidence_metadata_rejected(scenario):
    settings,admin,c,r,clients,remedy,extraction=scenario
    job=next(j for j in c.get('/api/jobs').json() if j['kind']=='assess_documents')['id']
    fields={'baseline_job_id':job,'name':'No-op','note':'Unchanged inputs'}
    assert c.post('/api/scenarios',data=fields).status_code==409
    assert c.post('/api/scenarios',data=fields,files={'evidence':('revised.csv',PASS,'text/csv')}).status_code==409
    assert c.post('/api/scenarios',data=fields|{'baseline_job_id':'missing'}).status_code==404
    assert c.post('/api/scenarios',data=fields|{'clear_evidence':'nonsense'}).status_code==422


def test_evidence_removal_and_policy_change_recompute(scenario):
    settings,admin,c,r,clients,remedy,extraction=scenario
    job=next(j for j in c.get('/api/jobs').json() if j['kind']=='assess_documents')['id']
    # Planner must declare missing evidence rather than invent CSV columns.
    from test_remediation import model
    # Use the genuine planner validation: unsupported test is required when no CSV exists.
    from regtrace import assessment_pipeline as ap
    from unittest.mock import patch
    def missing_model(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='plan':
            judgments=[j.model_copy(update={'procedure':ap.Procedure(control_id=j.procedure.control_id,kind='unsupported',case_id_column='',first_column='',second_column='',rationale='No supplied evidence.'),'evidence_relevance':'unknown'}) for j in parsed.judgments]
            parsed=ap.Plan(judgments=judgments)
        return parsed,meta,error
    response=c.post('/api/scenarios',data={'baseline_job_id':job,'name':'No evidence','note':'Remove sample and revise policy.','clear_evidence':'true'},files={'policy':('policy.txt',b'Operations records an identity check before activation.','text/plain')})
    assert response.status_code==202,response.text
    with patch.object(ex,'call_model',missing_model):result=complete(settings,c,response.json()['id'])
    assert result['status']=='completed',result
    row=result['result']['comparison']['rows'][0]
    assert row['after']['states']['test']=='not_run' and 'missing_evidence' in row['after']['gap_types']
    assert {v['field'] for v in result['changes']}=={'policy','evidence','evidence_context'}


def test_concurrent_stage_advance_and_cancel_retry_are_durable(scenario):
    settings,admin,c,r,clients,remedy,extraction=scenario
    response=create(c);id=response.json()['id'];ex_id=response.json()['extraction_job_id']
    assert c.post('/api/jobs/'+ex_id+'/cancel').status_code==200
    worker.tick(settings);assert c.get('/api/scenarios/'+id).json()['status']=='cancelled'
    assert c.post('/api/jobs/'+ex_id+'/retry').status_code==200
    worker.tick(settings)
    with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:scenarios.advance(settings,'demo-bank'),range(3)))
    result=c.get('/api/scenarios/'+id).json();job_id=result['assessment_job']['id']
    with transaction(settings.app_dsn,'demo-bank') as conn:
        assert conn.execute('SELECT count(*) AS n FROM scenario_assessment_links WHERE scenario_id=%s',(id,)).fetchone()['n']==1
    job=worker.claim(settings,'demo-bank');assert job['id']==job_id
    assert c.post('/api/jobs/'+job_id+'/cancel').status_code==200
    worker.finish(settings,job)
    assert c.get('/api/scenarios/'+id).json()['result'] is None
    assert c.post('/api/jobs/'+job_id+'/retry').status_code==200
    assert complete(settings,c,id)['status']=='completed'


def test_changed_duties_are_not_falsely_matched(scenario):
    settings,admin,c,r,clients,remedy,extraction=scenario
    response=create(c,files={'regulation':('what-if.txt',b'Banks must verify identity within 24 hours after activation.','text/plain')})
    result=complete(settings,c,response.json()['id']);assert result['status']=='completed',result
    rows=result['result']['comparison']['rows'];assert len(rows)==2
    assert {r['match'] for r in rows}=={'baseline_only_or_ambiguous','scenario_only_or_ambiguous'}


def test_browser_scenario_workflow_on_isolated_server(scenario):
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
        completed=subprocess.run([str(root/'.tools/node_modules/.bin/node'),str(root/'web/scenarios-smoke.mjs')],
          env=os.environ|{'REGTRACE_TEST_ORIGIN':web_settings.origin},capture_output=True,text=True,timeout=90)
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert not errors,errors
    finally:
        stop.set();server.should_exit=True
        if working.is_alive():working.join(5)
        serving.join(5);sock.close()




def test_no_obligations_is_explicit_review_case_not_clean_compliance(scenario,monkeypatch):
    from test_remediation import model
    settings,admin,c,r,clients,remedy,extraction=scenario
    def empty_reg(config,role,excerpt,*args,**kwargs):
        if role=='regulation':return ex.Extracted(obligations=[],controls=[],unresolved_context=['No duties found; review source.']),{'provider':'mock-groq','model':'test'},None
        return model(config,role,excerpt,*args,**kwargs)
    monkeypatch.setattr(ex,'call_model',empty_reg)
    response=create(c,files={'regulation':('hypothesis.txt',b'No mandatory duties in this hypothetical document.','text/plain')})
    result=complete(settings,c,response.json()['id'])
    assert result['status']=='completed' and result['assessment_job'] is None
    assert result['result']['entities']==[] and 'not evidence of compliance' in result['result']['warning']
    assert c.get('/api/assessments').json()[0]['current_states']['test']=='fail'


def test_profile_hypothesis_reaches_planner_without_changing_baseline(scenario,monkeypatch):
    from test_remediation import model
    from regtrace import assessment_pipeline as ap
    settings,admin,c,r,clients,remedy,extraction=scenario
    source=c.get('/api/extractions/'+extraction).json();profile=source['inputs']['profile']|{'unknown_facts':['Licensing status unconfirmed']}
    def unknown(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='plan':
            assert json.loads(excerpt)['profile']==profile
            parsed=ap.Plan(judgments=[j.model_copy(update={'applicability':'unknown','missing_facts':profile['unknown_facts'],'reason':'Licensing status is unknown.'}) for j in parsed.judgments])
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',unknown)
    response=create(c,data={'profile':json.dumps(profile)})
    result=complete(settings,c,response.json()['id']);assert result['status']=='completed',result
    assert result['result']['comparison']['rows'][0]['after']['states']['applicability']=='unknown'
    assert c.get('/api/extractions/'+extraction).json()['inputs']['profile']==source['inputs']['profile']
