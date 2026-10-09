"""Engineering acceptance with labelled model doubles; not live legal-quality evaluation."""
import json
import re

import pytest
from regtrace import contradictions as cd, extraction as ex, worker
from regtrace.storage import ArtifactStore, transaction
from test_m1_integration import world, database_server
from test_cross_regulation import assess, run as run_comparison, model as cross_model
from test_remediation import PASS, FAIL

DUTY_A = 'Banks must review identity before activation for retail accounts.'
DUTY_B = 'Banks must not review identity before activation for retail accounts.'
DATES_A = 'This duty is effective from 2025-01-01 through 2026-12-31.'
DATES_B = 'This duty is effective from 2026-01-01 through 2027-12-31.'


def model(config, role, excerpt, *args, **kwargs):
    if role == 'regulation' and excerpt.startswith('Banks must') and '\nThis duty' in excerpt:
        duty = excerpt.split('\n')[0]
        item = ex.Obligation(actor='Banks', action='not review' if 'must not' in duty else 'review', object='identity',
            conditions=['corporate accounts' if 'corporate' in duty else 'retail accounts'], exceptions=[], timing='before activation', quotes=[duty])
        return ex.Extracted(obligations=[item], controls=[], unresolved_context=[]), {'provider':'mock-groq','model':'test-double'}, None
    if role not in ('conflict_analyze', 'conflict_verify'):
        parsed,meta,error=cross_model(config, role, excerpt, *args, **kwargs)
        if role=='policy':parsed.unresolved_context=[]
        return parsed,meta,error
    data = json.loads(excerpt)
    if role == 'conflict_analyze':
        proposals = []
        for p in data['pairs']:
            periods = {}
            for side in ('left', 'right'):
                quotes = p[side]['additional_context']
                if quotes:
                    cite = quotes[0]; dates = re.findall(r'\d{4}-\d{2}-\d{2}', cite['text'])
                    bounds = [cd.DateEvidence(value=d, literal=d, citation_id=cite['id']) for d in dates]
                    periods[side+'_period'] = cd.Period(start=bounds[0], end=bounds[1] if len(bounds)>1 else None,
                        open_end_citation_id=cite['id'] if 'until further notice' in cite['text'] else None, reason='Explicit operative dates for this duty in the source context.')
                else:
                    periods[side+'_period'] = cd.Period(start=None,end=None,open_end_citation_id=None,reason='No operative-date citation supplied.')
            disjoint = p['left']['obligation']['conditions'] != p['right']['obligation']['conditions']
            incompatible = p['left']['obligation']['action'] != p['right']['obligation']['action']
            proposals.append(cd.Pair(pair_id=p['pair_id'],relation='incompatible' if incompatible else 'compatible',
                rationale='The same review is both required and prohibited.' if incompatible else 'Both review duties can be met together.',
                scope='disjoint' if disjoint else 'overlapping', scope_reason='Account categories differ.' if disjoint else 'Both govern identity review before activation of retail accounts.',
                unresolved_context=[], left_span_ids=[p['left']['spans'][0]['id']], right_span_ids=[p['right']['spans'][0]['id']], **periods))
        parsed = cd.Analysis(pairs=proposals)
    else:
        parsed = cd.Verification(checks=[cd.Check(pair_id=p['pair_id'],scope_supported=True,periods_supported=True,relation_supported=True,reason='Synthetic verifier checks the supplied scope, dates and duties.') for p in data['pairs']])
    return parsed, {'provider':'mock-groq','model':'test-double','latency_ms':1}, None


def prepare(world, monkeypatch, duty_b=DUTY_B, dates_b=DATES_B):
    settings, admin, client, tmp = world; c=client('analyst'); r=client('reviewer')
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex,'call_model',model)
    left,left_ex=assess(settings,c,DUTY_A+'\n'+DATES_A,FAIL)
    right,right_ex=assess(settings,c,duty_b+'\n'+dates_b,PASS)
    comparison,_=run_comparison(settings,c,{'name':'Synthetic source comparison','note':'Keep the two supplied instruments separate.',
        'left_job_id':left,'right_job_id':right,'left_instrument':'Synthetic instrument A','right_instrument':'Synthetic instrument B','distinct_confirmed':True})
    body={'comparison_id':comparison,'name':'Synthetic contradiction check','note':'Check joint compliance on the chosen date.',
        'as_of':'2026-06-01','left_context':DATES_A,'right_context':dates_b}
    return settings,admin,c,r,client,body,left_ex,right_ex


@pytest.fixture
def prepared(world,monkeypatch):
    return prepare(world,monkeypatch)


def run(settings,c,body):
    response=c.post('/api/contradictions',json=body);assert response.status_code==202,response.text
    id=response.json()['id'];worker.tick(settings)
    d=c.get('/api/contradictions/'+id).json();assert d['job']['status']=='completed',d
    return id,d


def review_body(revision=0):
    return {'decision':'approved','note':'Reviewed the cited scope and effective periods; retain both duties for resolution.','expected_revision':revision}


def test_conflict_candidate_sources_dates_review_and_isolation(prepared):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    before=c.get('/api/assessments').json();tasks=c.get('/api/remediations').json()
    id,d=run(settings,c,body);p=d['result']['pairs'][0]
    assert p['finding']=='conflict_candidate' and p['period_states']=={'left':'active','right':'active'},p['gate_reason']
    assert p['left']['spans'][0]['text']==DUTY_A and p['right']['spans'][0]['text']==DUTY_B
    assert p['left']['additional_context'][0]['text']==DATES_A
    assert p['left']['states']['test']=='fail' and p['right']['states']['test']=='pass'
    assert {m['step'] for m in d['model_calls']}=={'conflict_analyze','conflict_verify'}
    assert c.post('/api/contradictions',json=body).json()['id']==id
    url='/api/contradictions/'+id+'/pairs/pair-1/reviews'
    assert c.post(url,json=review_body()).status_code==403
    assert r.post(url,json=review_body()).status_code==200
    assert r.post(url,json=review_body()).status_code==409
    assert c.get('/api/assessments').json()==before and c.get('/api/remediations').json()==tasks
    other=clients('other-reviewer');assert other.get('/api/contradictions').json()==[]
    assert other.get('/api/contradictions/'+id).status_code==404
    assert other.post('/api/contradictions',json=body).status_code==404
    assert other.post(url,json=review_body()).status_code==404
    source=next(e for e in d['snapshots'][0]['entities'] if e['kind']=='source')
    path='/api/contradictions/'+id+'/artifacts/'+source['id']
    assert c.get(path).status_code==200 and other.get(path).status_code==404
    with transaction(settings.app_dsn,'other-bank') as conn:
        assert conn.execute('SELECT count(*) AS n FROM contradiction_results').fetchone()['n']==0
        assert conn.execute('SELECT count(*) AS n FROM contradiction_reviews').fetchone()['n']==0
    with pytest.raises(Exception,match='immutable'):
        with transaction(admin,'demo-bank') as conn:
            conn.execute('UPDATE contradiction_results SET payload=payload WHERE job_id=%s',(id,))


@pytest.mark.parametrize('duty,dates,expected',[
    (DUTY_B.replace('retail','corporate'),DATES_B,'different_scopes'),
    (DUTY_B,'This duty is effective from 2027-01-01 through 2028-12-31.','outside_effective_period'),
    (DUTY_A,DATES_B,'compatible'),
])
def test_false_conflicts_explained_by_actual_source_scope_dates_or_compatibility(world,monkeypatch,duty,dates,expected):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepare(world,monkeypatch,duty,dates)
    id,d=run(settings,c,body)
    assert d['result']['pairs'][0]['finding']==expected


def test_missing_dates_and_unsupported_model_check_remain_uncertain(prepared,monkeypatch):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    id,d=run(settings,c,body|{'left_context':'','right_context':''})
    assert d['result']['pairs'][0]['finding']=='uncertain'
    assert r.post('/api/contradictions/'+id+'/pairs/pair-1/reviews',json=review_body()).status_code==409
    def challenge(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='conflict_verify':parsed.checks[0].relation_supported=False
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',challenge)
    id,d=run(settings,c,body)
    assert d['result']['pairs'][0]['finding']=='uncertain'


@pytest.mark.parametrize('invalid,code',[
    ('citation','conflict_duty_citation_invalid'),('date','conflict_date_normalization_invalid'),('open','conflict_open_end_unsupported'),
])
def test_invalid_citations_dates_and_open_end_are_bounded_failures(prepared,monkeypatch,invalid,code):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    def broken(config,role,excerpt,*args,**kwargs):
        parsed,meta,error=model(config,role,excerpt,*args,**kwargs)
        if role=='conflict_analyze':
            p=parsed.pairs[0]
            if invalid=='citation':p.left_span_ids=p.right_span_ids
            if invalid=='date':p.left_period.start.value='2024-01-01'
            if invalid=='open':p.left_period.end=None;p.left_period.open_end_citation_id='context-left'
        return parsed,meta,error
    monkeypatch.setattr(ex,'call_model',broken)
    id=c.post('/api/contradictions',json=body).json()['id'];worker.tick(settings)
    d=c.get('/api/contradictions/'+id).json()
    assert d['job']['status']=='failed' and d['job']['error_code']==code
    assert d['result'] is None and len(d['model_calls'])==2


def test_input_validation_and_cancel_retry(prepared):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    for change in [{'as_of':'2026-02-30'},{'left_context':'Invented effective date'},{'name':'  '}]:
        assert c.post('/api/contradictions',json=body|change).status_code==409
    assert c.post('/api/contradictions',json=body|{'comparison_id':left_ex}).status_code==404
    id=c.post('/api/contradictions',json=body).json()['id'];job=worker.claim(settings,'demo-bank')
    assert c.post('/api/jobs/'+id+'/cancel').status_code==200
    worker.finish(settings,job)
    assert c.get('/api/contradictions/'+id).json()['job']['status']=='cancelled'
    assert c.post('/api/jobs/'+id+'/retry').status_code==200
    worker.tick(settings);assert c.get('/api/contradictions/'+id).json()['job']['status']=='completed'


def test_stale_and_corrupted_sources_block_acceptance(prepared):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    id,d=run(settings,c,body)
    track=c.post('/api/regulatory-tracks',json={'name':'Synthetic A','extraction_id':left_ex,'synthetic':True}).json()['id']
    response=c.post('/api/regulatory-tracks/'+track+'/versions',data={'kind':'synthetic_snapshot'},files={'regulation':('new.txt',b'Banks must record supervisor approval.','text/plain')});assert response.status_code==202
    worker.tick(settings);worker.tick(settings)
    assert c.get('/api/contradictions/'+id).json()['source_stale']
    assert r.post('/api/contradictions/'+id+'/pairs/pair-1/reviews',json=review_body()).status_code==409
    assert c.post('/api/contradictions',json=body).status_code==409


def test_source_integrity_and_changed_source_review_gates(prepared):
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    id,d=run(settings,c,body);assessment=d['result']['pairs'][0]['left']['assessment_id']
    source=next(e for e in d['snapshots'][1]['entities'] if e['kind']=='source')
    path=ArtifactStore(settings.artifact_root).path(source['sha256']);original=path.read_bytes()
    path.chmod(0o644);path.write_bytes(b'corrupted')
    assert r.post('/api/contradictions/'+id+'/pairs/pair-1/reviews',json=review_body()).status_code==409
    assert c.post('/api/contradictions',json=body).status_code==409
    path.write_bytes(original);path.chmod(0o444)
    assert r.post('/api/assessments/'+assessment+'/reviews',json=review_body()|{'decision':'rejected'}).status_code==200
    assert c.get('/api/contradictions/'+id).json()['source_review_changed']
    assert r.post('/api/contradictions/'+id+'/pairs/pair-1/reviews',json=review_body()).status_code==409
    assert c.post('/api/contradictions',json=body).status_code==409


def sample_proposal():
    bound=lambda v:cd.DateEvidence(value=v,literal=v,citation_id='source')
    period=cd.Period(start=bound('2025-01-01'),end=bound('2026-12-31'),open_end_citation_id=None,reason='Explicit source dates.')
    return cd.Pair(pair_id='p',relation='incompatible',rationale='Opposite duties for the same event.',scope='overlapping',scope_reason='Same actor/event/category.',
        unresolved_context=[],left_span_ids=['a'],right_span_ids=['b'],left_period=period,right_period=period.model_copy(deep=True))


@pytest.mark.parametrize('case,expected',[
    ('stricter_compatible','compatible'),('unknown_applicability','uncertain'),('inapplicable','not_jointly_applicable'),
    ('unknown_scope','uncertain'),('unknown_end','uncertain'),('unresolved_extraction','uncertain'),('unresolved_proposal','uncertain'),
    ('source_unsupported','uncertain'),('scope_unsupported','uncertain'),('dates_unsupported','uncertain'),
])
def test_classification_gates(case,expected):
    p=sample_proposal();pair={s:{'states':{'verification':'supported','applicability':'applicable'}} for s in ('left','right')}
    check=cd.Check(pair_id='p',scope_supported=True,periods_supported=True,relation_supported=True,reason='Source checked.');unresolved=[[],[]]
    if case=='stricter_compatible':p.relation='compatible';p.rationale='Performing within 5 days also satisfies a 10-day maximum.'
    if case=='unknown_applicability':pair['left']['states']['applicability']='unknown'
    if case=='inapplicable':pair['left']['states']['applicability']='not_applicable'
    if case=='unknown_scope':p.scope='unknown'
    if case=='unknown_end':p.left_period.end=None
    if case=='unresolved_extraction':unresolved=[['Missing definition'],[]]
    if case=='unresolved_proposal':p.unresolved_context=['Referenced exemption unavailable']
    if case=='source_unsupported':pair['left']['states']['verification']='unsupported'
    if case=='scope_unsupported':check.scope_supported=False
    if case=='dates_unsupported':check.periods_supported=False
    assert cd.classify(pair,p,check,'2026-06-01',unresolved)['finding']==expected


def test_inclusive_boundaries_explicit_open_end_and_date_normalization():
    p=sample_proposal().left_period
    assert cd.period_state(p,'2025-01-01')=='active' and cd.period_state(p,'2026-12-31')=='active'
    assert cd.period_state(p,'2024-12-31')=='inactive' and cd.period_state(p,'2027-01-01')=='inactive'
    p.end=None;p.open_end_citation_id='source'
    cd.validate_period(p,{'source':{'text':'Effective 2025-01-01 until further notice.'}})
    assert cd.period_state(p,'2027-01-01')=='active'
    assert cd.normalize_date('1 January 2025').isoformat()=='2025-01-01'
    assert cd.normalize_date('January 1, 2025').isoformat()=='2025-01-01'
    with pytest.raises(ex.ExtractionError):cd.normalize_date('01/02/2025')


def test_browser_contradiction_workflow_on_isolated_server(prepared):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from dataclasses import replace
    from regtrace.api import create_app
    settings,admin,c,r,clients,body,left_ex,right_ex=prepared
    root=Path(__file__).resolve().parents[1];sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    web_settings=replace(settings,origin=f'http://127.0.0.1:{port}')
    server=uvicorn.Server(uvicorn.Config(create_app(web_settings),log_level='warning'))
    serving=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);stop=threading.Event();errors=[]
    def work():
        while not stop.wait(.15):
            try:worker.tick(web_settings)
            except Exception as error:errors.append(type(error).__name__);return
    working=threading.Thread(target=work,daemon=True);serving.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started;working.start()
        completed=subprocess.run([str(root/'.tools/node_modules/.bin/node'),str(root/'web/contradictions-smoke.mjs')],
            env=os.environ|{'REGTRACE_TEST_ORIGIN':web_settings.origin},capture_output=True,text=True,timeout=90)
        assert completed.returncode==0,completed.stdout+completed.stderr
        assert not errors,errors
    finally:
        stop.set();server.should_exit=True
        if working.is_alive():working.join(5)
        serving.join(5);sock.close()
