"""Read-only product-capability audit plus an isolated evidence-change probe.

Uses temporary artifacts only. Does not connect to the app database or any model API.
"""
import ast
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from regtrace.bundle import validate_bundle
from regtrace.extraction import provider_status
from regtrace.storage import ArtifactStore


def main():
    tree=ast.parse((ROOT/'src/regtrace/api.py').read_text())
    routes=[]
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Attribute)
                    and isinstance(decorator.func.value,ast.Name) and decorator.func.value.id=='app'
                    and decorator.func.attr in {'get','post','put','patch','delete'}):
                    routes.append({'method':decorator.func.attr.upper(),'path':ast.literal_eval(decorator.args[0])})
    bundle=json.loads((ROOT/'data/fixtures/onboarding.json').read_text())
    with tempfile.TemporaryDirectory(prefix='regtrace-objective-audit-') as tmp:
        store=ArtifactStore(Path(tmp))
        for e in bundle['entities']:
            if e['kind'] in ('source','evidence'):store.put((ROOT/e['artifact_path']).read_bytes(),e['sha256'])
            if e['kind']=='source':store.put((ROOT/e['text_path']).read_bytes(),e['text_sha256'])
        original,_=validate_bundle(bundle,'demo-bank',store)
        evidence=next(e for e in bundle['entities'] if e['id']=='EV1-v1')
        rows=list(csv.DictReader(io.StringIO(store.read(evidence['sha256']).decode())))
        for row in rows:
            if row['case_id']=='CASE-002':row['review_at']='2026-09-02T09:00:00Z'
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        changed=buffer.getvalue().encode();digest=hashlib.sha256(changed).hexdigest()
        store.put(changed,digest);evidence['sha256']=digest
        validated,_=validate_bundle(bundle,'demo-bank',store)
        recomputed_failures=[r['case_id'] for r in rows if datetime.fromisoformat(r['review_at'])>datetime.fromisoformat(r['activation_at'])]
        result=next(e for e in validated if e.id=='TR1-v1')
        assessment=next(e for e in validated if e.id=='A1-v1')
        probe={'scope':'isolated bundle validator; not a public upload/API test',
               'changed_evidence_passes_timestamp_check':not recomputed_failures,
               'bundle_validation_accepted':True,'retained_test_outcome':result.outcome,
               'retained_assessment_test_state':assessment.states.test,
               'finding':'Runtime bundle validation accepts recorded outcomes; it does not execute the control procedure against evidence.'}
    config=json.loads((ROOT/'config/runtime.json').read_text())
    report={'audited_at':datetime.now(timezone.utc).isoformat(),'specification_pages':[2,3,4,5,6],
            'fixture_model_configuration':config['model'],'document_model_configuration':provider_status(),
            'document_live_acceptance':'pending; integration tests use mocks','api_routes':routes,
            'fixture_entity_count':len(original),'evidence_change_probe':probe,
            'heldout_cases':json.loads((ROOT/'data/evaluation/protocol.json').read_text())['current_heldout_case_count'],
            'limitations':'Source audit and isolated probe, not a new full integration test or model-quality evaluation.'}
    (ROOT/'artifacts/problem1-audit/current-capabilities.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'route_count':len(routes),'mode':config['model']['mode'],'live_calls':config['model']['live_calls_enabled'],
                      'evidence_change_probe':probe,'heldout_cases':report['heldout_cases']},indent=2))

if __name__=='__main__':main()
