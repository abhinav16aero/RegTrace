import json
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from regtrace import assessment_pipeline as ap, extraction as ex
from regtrace.storage import ArtifactStore

CONTEXT=ap.EvidenceContext.model_validate_json(json.dumps({'producer':'Synthetic source','scope':'Sample',
 'collected_at':'2026-09-03T00:00:00Z','period_start':'2026-09-01T00:00:00Z','period_end':'2026-09-03T00:00:00Z'}))

def procedure(kind='timestamp_order'):
    return ap.Procedure(control_id='c',kind=kind,case_id_column='id',first_column='review',second_column='active',rationale='Synthetic test procedure')

@pytest.mark.parametrize(('rows','outcome'),[
 ('a,2026-09-02T09:00:00Z,2026-09-02T10:00:00Z','pass'),
 ('a,2026-09-02T10:00:00Z,2026-09-02T10:00:00Z','fail'),
 ('a,2026-09-02T11:00:00Z,2026-09-02T10:00:00Z','fail'),
 ('a,2026-09-02T09:00:00,2026-09-02T10:00:00Z','inconclusive'),
 ('a,2026-09-04T09:00:00Z,2026-09-04T10:00:00Z','inconclusive'),
 ('a,bad,2026-09-02T10:00:00Z','inconclusive'),
 ('a,2026-09-02T11:00:00Z,2026-09-02T10:00:00Z\nb,bad,bad','fail')])
def test_timestamp_outcomes_are_computed(rows,outcome):
    result=ap.execute_csv(('id,review,active\n'+rows+'\n').encode(),procedure(),CONTEXT)
    assert result['outcome']==outcome


def test_blank_duplicate_ids_and_missing_metadata_abstain():
    assert ap.execute_csv(b'id,review\na,\n',procedure('required_value'),CONTEXT)['outcome']=='fail'
    assert ap.execute_csv(b'id,review\na,John\na,John\n',procedure('required_value'),CONTEXT)['outcome']=='inconclusive'
    assert ap.execute_csv(b'id,review\na,John\n',procedure('required_value'),None)['outcome']=='not_run'
    assert ap.execute_csv(None,procedure(),CONTEXT)['outcome']=='not_run'


def test_unknown_applicability_partial_mapping_irrelevant_and_missing_evidence(tmp_path):
    store=ArtifactStore(tmp_path);docs=[ex.document(b'Banks verify identity.','reg.txt','regulation',store),ex.document(b'Operations checks names.','pol.txt','policy',store)]
    items=[]
    for doc in docs:
        text=store.read(doc['text_sha256']).decode()
        parsed=ex.Extracted(obligations=[ex.Obligation(actor='Banks',action='verify',object='identity',conditions=[],exceptions=[],timing='',quotes=[text])] if doc['role']=='regulation' else [],
          controls=[ex.Control(name='Name check',owner_role='Operations',scope='Retail',frequency='Every account',expected_evidence=[],quotes=[text])] if doc['role']=='policy' else [],unresolved_context=[])
        items+=ex.locate(parsed,doc,text,0,text)['items']
    o,c=items
    source={'items':items,'inputs':{'synthetic':True,'documents':docs,'profile':{'name':'Synthetic','jurisdiction':'IN','entity_type':'bank','products':[],'customer_categories':[],'channels':[],'unknown_facts':[]},'evidence':None}}
    job={'id':'test-job','tenant_id':'demo-bank','input_payload':{'evidence_context':None}}
    judgment=ap.Judgment(obligation_id=o['id'],applicability='applicable',reason='Synthetic assessment',missing_facts=[],mappings=[],
      evidence_relevance='unknown',evidence_reason='No evidence',procedure=procedure('unsupported'))
    entities,results,gaps=ap.build_bundle(job,source,ap.Plan(judgments=[judgment]),store)
    assert {g['gap_type'] for g in gaps}=={'missing_control','missing_evidence'}
    assert results[0]['test']['outcome']=='not_run'
    judgment.mappings=[ap.Candidate(control_id=c['id'],clauses=[ap.Coverage(clause=k,status='partial',rationale='Name check is incomplete identity verification.') for k in ap.clauses(o)])]
    entities,results,gaps=ap.build_bundle(job,source,ap.Plan(judgments=[judgment]),store)
    assert 'partial_coverage' in {g['gap_type'] for g in gaps}
    judgment.missing_facts=['Bank licence category']
    entities,results,gaps=ap.build_bundle(job,source,ap.Plan(judgments=[judgment]),store)
    assert results[0]['applicability']=='unknown' and {g['gap_type'] for g in gaps}=={'unknown_applicability'}
    judgment.mappings[0].clauses=[]
    with pytest.raises(ex.ExtractionError,match='clause_coverage_incomplete'):ap.validate_plan(ap.Plan(judgments=[judgment]),items,None)
