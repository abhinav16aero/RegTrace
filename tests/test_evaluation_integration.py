"""The evaluation runner uses the real app pipeline; only model responses are mocked."""
import json
from regtrace import evaluation as ev, extraction as ex, worker
from test_m1_integration import world, database_server
from test_remediation import model


def test_runner_exports_real_computed_failure_and_scorer_reads_actual_shapes(world,monkeypatch):
    settings,admin,client,tmp=world;c=client('analyst')
    monkeypatch.setenv('GROQ_API_KEY','offline-test-key');monkeypatch.setattr(ex,'call_model',model)
    manifest=ev.load(ev.ROOT/'data/evaluation/development-manifest.json')
    manifest['cases']=manifest['cases'][:1]
    for item in manifest['cases'][0]['inputs'].values():item['path']=str((ev.ROOT/'data/evaluation'/item['path']).resolve())
    manifest_path=tmp/'manifest.json';ev.save_new(manifest_path,manifest)
    m,files,mh=ev.read_manifest(manifest_path)
    record=ev.run_case(c,m.cases[0],files[m.cases[0].id],mh,timeout=30,wait=lambda _:worker.tick(settings))
    assert record['status']=='completed',record['error']
    assert client('other-reviewer').get('/api/jobs/'+record['assessment_job_id']).status_code==404
    assert c.get('/api/jobs/'+record['assessment_job_id']).json()['model_calls']
    assert record['assessments'][0]['snapshot']['states']['test']=='fail'
    assert record['extraction']['job']['configuration_hash'] and record['assessment_job']['configuration_hash']
    review={'status':'reviewed','author':'synthetic-test-label','reviewer':'test-reviewer-1','second_reviewer':'test-reviewer-2','reviewed_at':'2026-10-04T01:00:00Z','note':'Explicit test double; not human gold.'}
    obligation=next(i for i in record['extraction']['items'] if i['kind']=='obligation')
    label={'case_id':m.cases[0].id,'review':review,'obligations':[{'id':'gold','text':'Synthetic requirement','quote':obligation['citations'][0]['text']}],
           'decisions':[{'id':'decision','obligation_id':'gold','applicability':'applicable','coverage':'full','evidence':'sufficient','test':'fail','gap_types':['execution_failure']}]}
    adjudication={'case_id':label['case_id'],'record_hash':ev.digest(ev.canonical(record)),'review':review,
      'matches':[{'predicted_id':obligation['id'],'gold_id':'gold'}],
      'alignments':[{'decision_id':'decision','assessment_id':record['assessments'][0]['snapshot']['id']}],
      'citations':[{'item_id':i['id'],'citation_index':n,'supported':True} for i in record['extraction']['items'] for n,q in enumerate(i['citations'])]}
    lp,rp,ap=[tmp/f for f in ['labels.json','records.json','adjudications.json']]
    ev.save_new(lp,{'manifest_hash':mh,'cases':[label]});ev.save_new(rp,[record]);ev.save_new(ap,{'manifest_hash':mh,'cases':[adjudication]})
    report=ev.score(manifest_path,lp,rp,ap)
    assert report['metrics']['assessment_exact_accuracy']==ev.rate(1,1)
    assert report['metrics']['citation_resolution']['value']==1
    assert report['providers']==['mock-groq'] and report['readiness']['tier_achieved'] is None


def test_runner_records_missing_key_failure_without_mock_output(world,monkeypatch):
    settings,admin,client,tmp=world;c=client('analyst')
    monkeypatch.delenv('GROQ_API_KEY',raising=False);monkeypatch.delenv('GROQ_API_KEY_FILE',raising=False)
    m,files,mh=ev.read_manifest(ev.ROOT/'data/evaluation/development-manifest.json')
    result=ev.run_case(c,m.cases[0],files[m.cases[0].id],mh,timeout=15,wait=lambda _:worker.tick(settings))
    assert result['status']=='failed' and result['error']=='provider_key_missing'
    assert result['assessments']==[]
    assert result['extraction']['model_calls']==[]  # Preflight fails before any provider call.
