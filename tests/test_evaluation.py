"""Evaluation math uses deliberately wrong predictions; reviewer identities are test doubles."""
import copy
import json
import pytest
from regtrace import evaluation as ev

@pytest.fixture
def pack(tmp_path,monkeypatch):
    monkeypatch.setattr(ev,'ROOT',tmp_path)
    texts={'regulation':'Banks must review identity. Banks must record reviewers.','policy':'Operations reviews identity.','profile':'{}'}
    inputs={}
    for role,text in texts.items():
        file=tmp_path/(role+'.txt');file.write_text(text);inputs[role]={'path':file.name,'sha256':ev.digest(file.read_bytes())}
    m={'schema_version':'1.0','protocol_sha256':ev.digest(ev.PROTOCOL.read_bytes()),'frozen_at':'2026-10-04T00:00:00Z',
       'cases':[{'id':'c1','family_id':'synthetic-onboarding-v1','material':'synthetic','split':'development','critical_categories':['failed_execution'],'inputs':inputs}]}
    mh=ev.digest(ev.canonical(m));review={'status':'reviewed','author':'test-draft','reviewer':'test-human-1','second_reviewer':'test-human-2','reviewed_at':'2026-10-04T01:00:00Z','note':'Explicit synthetic test label.'}
    expected={'applicability':'applicable','coverage':'partial','evidence':'sufficient','test':'fail','gap_types':['execution_failure','partial_coverage']}
    labels={'manifest_hash':mh,'cases':[{'case_id':'c1','review':review,'obligations':[{'id':'g1','text':'Review identity','quote':'Banks must review identity.'},{'id':'g2','text':'Record reviewers','quote':'Banks must record reviewers.'}],
       'decisions':[{'id':'d1','obligation_id':'g1',**expected},{'id':'d2','obligation_id':'g2',**expected}]}]}
    doc={'id':'reg','role':'regulation','sha256':inputs['regulation']['sha256'],'text_sha256':inputs['regulation']['sha256']}
    cite={'document_id':'reg','text_sha256':doc['text_sha256'],'start':0,'end':len('Banks must review identity.'),'text':'Banks must review identity.'}
    calls=[{'step':'plan','metadata':{'provider':'mock-groq','model':'test','usage':{'prompt_tokens':4,'completion_tokens':6,'total_tokens':10}}}]
    actual={'applicability':'applicable','coverage':'full','evidence':'sufficient','test':'pass'}
    assessment={'snapshot':{'id':'a1','applicability_id':'app','states':actual},'provenance':[{'id':'app','obligation_id':'D-123456789012-p1'}],'followups':[],
       'run_context':{'job_id':'123456789012','model_calls':calls}}
    record={'case_id':'c1','manifest_hash':mh,'input_hashes':{k:v['sha256'] for k,v in inputs.items()},'status':'completed','error':None,'elapsed_ms':10,
      'extraction_job_id':'ex','assessment_job_id':'123456789012','extraction':{'items':[{'id':'p1','kind':'obligation','citations':[cite]},{'id':'p2','kind':'obligation','citations':[]}],
      'inputs':{'documents':[doc]},'model_calls':[]},'assessments':[assessment]}
    adj={'manifest_hash':mh,'cases':[{'case_id':'c1','record_hash':ev.digest(ev.canonical(record)),'review':review,'matches':[{'predicted_id':'p1','gold_id':'g1'}],
      'alignments':[{'decision_id':'d1','assessment_id':'a1'},{'decision_id':'d2','assessment_id':None}],
      'citations':[{'item_id':'p1','citation_index':0,'supported':False}]}]}
    paths=[tmp_path/f for f in ['manifest.json','labels.json','records.json','adjudications.json']]
    def write():
        for p,data in zip(paths,[m,labels,[record],adj]):p.write_text(json.dumps(data))
    write();return m,labels,record,adj,paths,write

def test_known_denominators_wrong_predictions_and_unknown_cost(pack):
    m,l,r,a,paths,write=pack;report=ev.score(*paths);metrics=report['metrics']
    assert metrics['obligation_precision']==ev.rate(1,2)
    assert metrics['obligation_recall']==ev.rate(1,2)
    assert metrics['full_mapping_precision']==ev.rate(0,1)
    assert metrics['mapping_confusion']['partial']['full']==1
    assert metrics['mapping_confusion']['partial']['missing']==1
    assert metrics['critical_false_passes']==ev.rate(1,2)
    assert metrics['citation_resolution']==ev.rate(1,1)
    assert metrics['citation_support']==ev.rate(0,1)
    assert metrics['resolved_case_coverage']==ev.rate(1,2)
    assert report['token_usage']['total_tokens']==10 and report['cost_usd'] is None
    assert report['groups']['real_bank:heldout']['obligation_precision']['value'] is None

@pytest.mark.parametrize('mutation',['duplicate','hash','family','quote','self_review','result_hash','alignment','citation','missing_decision'])
def test_invalid_inputs_cannot_receive_metrics(pack,mutation):
    m,l,r,a,paths,write=pack
    if mutation=='duplicate':m['cases'].append(copy.deepcopy(m['cases'][0]))
    elif mutation=='hash':m['cases'][0]['inputs']['regulation']['sha256']='0'*64
    elif mutation=='family':m['cases'][0]['split']='heldout'
    elif mutation=='quote':l['cases'][0]['obligations'][0]['quote']='fabricated'
    elif mutation=='self_review':l['cases'][0]['review']['reviewer']='test-draft'
    elif mutation=='result_hash':r['elapsed_ms']=11
    elif mutation=='alignment':a['cases'][0]['alignments'][0]['assessment_id']='invented'
    elif mutation=='citation':a['cases'][0]['citations']=[]
    elif mutation=='missing_decision':l['cases'][0]['decisions']=[]
    write()
    with pytest.raises(ValueError):ev.score(*paths)

def test_drafts_and_blocked_runs_do_not_become_zero_accuracy(pack):
    m,l,r,a,paths,write=pack;l['cases'][0]['review']['status']='draft';write()
    report=ev.score(*paths);assert report['scored_cases']==0
    assert report['excluded'][0]['reason']=='missing_or_unreviewed_labels'
    assert report['metrics']['obligation_recall']['value'] is None
    r['status']='blocked';r['error']='provider_key_missing';write()
    report=ev.score(*paths);assert report['blocked_runs']==1 and report['run_failures']['numerator']==0

def test_unmatched_full_assessment_counts_against_precision_and_calls_deduplicate(pack):
    m,l,r,a,paths,write=pack
    extra=copy.deepcopy(r['assessments'][0]);extra['snapshot']['id']='a2';r['assessments'].append(extra)
    a['cases'][0]['record_hash']=ev.digest(ev.canonical(r));write()
    report=ev.score(*paths)
    assert report['metrics']['full_mapping_precision']==ev.rate(0,2)
    assert report['metrics']['unmatched_assessment_passes']==1
    assert report['model_calls']==1 and report['token_usage']['total_tokens']==10

def test_changed_files_and_existing_exports_are_rejected(pack):
    m,l,r,a,paths,write=pack
    with pytest.raises(FileExistsError):ev.save_new(paths[0],{})
    (paths[0].parent/'regulation.txt').write_text('changed')
    with pytest.raises(ValueError,match='Input hash'):ev.read_manifest(paths[0])
