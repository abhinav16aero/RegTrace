"""Offline artifact and contract validation; does not call any model/provider."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from regtrace.contracts import ENTITY_ADAPTER, Dependency  # noqa: E402

REFERENCE_RELATIONS = {
    'span': {'document_id':'derived_from'}, 'obligation': {'span_ids':'cites'},
    'control': {'span_ids':'cites'},
    'applicability': {'obligation_id':'applies_obligation','profile_id':'uses_profile'},
    'mapping': {'obligation_id':'maps_obligation','control_id':'maps_control'},
    'evidence': {'control_id':'evidences_control'},
    'test_run': {'definition_id':'uses_definition','control_id':'tests_control','evidence_ids':'uses_evidence'},
    'assessment': {'applicability_id':'assesses_applicability','mapping_ids':'assesses_mapping',
                   'evidence_ids':'uses_evidence','test_run_ids':'uses_test'},
    'gap': {'assessment_id':'gap_in'}, 'remediation': {'gap_id':'remediates','retest_ids':'uses_test'},
}

def validate_fixture():
    fixture = json.loads((ROOT/'data/fixtures/onboarding.json').read_text())
    entities = [ENTITY_ADAPTER.validate_json(json.dumps(e)) for e in fixture['entities']]
    by_id = {(e.tenant_id,e.id):e for e in entities}
    assert len(by_id) == len(entities), 'Duplicate entity IDs'
    dependencies = [Dependency.model_validate_json(json.dumps(d)) for d in fixture['dependencies']]
    actual = {(d.tenant_id,d.dependent_id,d.input_id,d.relation) for d in dependencies}
    assert len(actual) == len(dependencies), 'Duplicate dependencies'
    expected = set()
    for entity in entities:
        for field, relation in REFERENCE_RELATIONS.get(entity.kind,{}).items():
            value = getattr(entity,field)
            for ref in (value if isinstance(value,tuple) else (value,)):
                assert (entity.tenant_id,ref) in by_id, f'Missing same-tenant input: {ref}'
                expected.add((entity.tenant_id,entity.id,ref,relation))
        if entity.kind in ('source','evidence'):
            assert hashlib.sha256((ROOT/entity.artifact_path).read_bytes()).hexdigest() == entity.sha256
        if entity.kind == 'source':
            assert hashlib.sha256((ROOT/entity.text_path).read_bytes()).hexdigest() == entity.text_sha256
        if entity.kind == 'span':
            doc = by_id[(entity.tenant_id,entity.document_id)]
            assert (ROOT/doc.text_path).read_text()[entity.start:entity.end] == entity.text
    assert actual == expected, 'Stored dependencies differ from typed references'
    return fixture, entities


def main():
    fixture, entities = validate_fixture()
    report = {'fixture_entities':len(entities),'dependency_edges':len(fixture['dependencies']),
              'fixture_valid':True,'official_artifacts_verified':0}
    manifest = ROOT/'data/manifest.json'
    if manifest.exists():
        for source in json.loads(manifest.read_text())['sources']:
            for field, digest in [('artifact_path','sha256'),('text_path','text_sha256')]:
                assert hashlib.sha256((ROOT/source[field]).read_bytes()).hexdigest() == source[digest]
            report['official_artifacts_verified'] += 1
    config = json.loads((ROOT/'config/runtime.json').read_text())
    assert config['model']['fixture_sha256'] == hashlib.sha256((ROOT/'data/fixtures/onboarding.json').read_bytes()).hexdigest()
    assert config['contracts_sha256'] == hashlib.sha256((ROOT/'config/contracts.schema.json').read_bytes()).hexdigest()
    expected_protocol_hash = (ROOT/'data/evaluation/protocol.sha256').read_text().split()[0]
    assert expected_protocol_hash == hashlib.sha256((ROOT/'data/evaluation/protocol.json').read_bytes()).hexdigest()
    report['configuration_and_protocol_hashes_valid'] = True
    print(json.dumps(report,indent=2))
    return report

if __name__ == '__main__':
    main()
