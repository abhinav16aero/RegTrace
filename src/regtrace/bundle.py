import json
from .contracts import ENTITY_ADAPTER, Dependency

REFERENCE_RELATIONS = {
 'span': {'document_id':'derived_from'}, 'obligation': {'span_ids':'cites'},
 'control': {'span_ids':'cites'},
 'applicability': {'obligation_id':'applies_obligation','profile_id':'uses_profile'},
 'mapping': {'obligation_id':'maps_obligation','control_id':'maps_control'},
 'evidence': {'control_id':'evidences_control'},
 'test_run': {'definition_id':'uses_definition','control_id':'tests_control','evidence_ids':'uses_evidence'},
 'assessment': {'applicability_id':'assesses_applicability','mapping_ids':'assesses_mapping','evidence_ids':'uses_evidence','test_run_ids':'uses_test'},
 'gap': {'assessment_id':'gap_in'}, 'remediation': {'gap_id':'remediates','retest_ids':'uses_test'},
}

def validate_bundle(bundle,tenant,store):
    entities=[ENTITY_ADAPTER.validate_json(json.dumps(e)) for e in bundle['entities']]
    index={e.id:e for e in entities}
    if len(index)!=len(entities) or any(e.tenant_id!=tenant for e in entities):
        raise ValueError('Duplicate or cross-tenant entities')
    deps=[Dependency.model_validate_json(json.dumps(d)) for d in bundle['dependencies']]
    actual={(d.dependent_id,d.input_id,d.relation) for d in deps}
    if len(actual)!=len(deps) or any(d.tenant_id!=tenant for d in deps):
        raise ValueError('Duplicate or cross-tenant dependencies')
    expected=set()
    for entity in entities:
        for field,relation in REFERENCE_RELATIONS.get(entity.kind,{}).items():
            value=getattr(entity,field)
            for ref in value if isinstance(value,tuple) else (value,):
                if ref not in index: raise ValueError('Missing referenced entity')
                expected.add((entity.id,ref,relation))
        if entity.kind in ('source','evidence'): store.read(entity.sha256)
        if entity.kind=='source': store.read(entity.text_sha256)
        if entity.kind=='span':
            source=index[entity.document_id]
            text=store.read(source.text_sha256).decode('utf-8')
            if text[entity.start:entity.end]!=entity.text: raise ValueError('Invalid source span')
        if entity.kind=='assessment':
            if entity.states.applicability!=index[entity.applicability_id].status:
                raise ValueError('Assessment contradicts applicability input')
            tests=[index[i] for i in entity.test_run_ids]
            if entity.states.test=='pass' and (not tests or any(t.outcome!='pass' for t in tests)):
                raise ValueError('Assessment contradicts test inputs')
            if any(t.outcome=='fail' for t in tests) and entity.states.test!='fail':
                raise ValueError('Assessment hides a failed test')
    if actual!=expected: raise ValueError('Dependency edges disagree with typed references')
    return entities,deps
