"""Hypothetical recomputation; results never enter the production entity graph."""
import uuid
from collections import defaultdict
from copy import deepcopy
from psycopg.types.json import Jsonb
from fastapi.encoders import jsonable_encoder
from . import extraction as ex, assessment_pipeline as ap, service
from .bundle import validate_bundle
from .regulatory_changes import signature, publication_lock
from .storage import ArtifactStore, transaction


def get(conn,tenant,id):
    row=conn.execute('SELECT * FROM scenarios WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
    if not row:raise service.Missing('Scenario not found')
    return row


def assert_production(conn,tenant,extraction_id):
    if conn.execute('SELECT 1 FROM scenarios WHERE tenant_id=%s AND extraction_job_id=%s',(tenant,extraction_id)).fetchone():
        raise service.Conflict('Hypothetical inputs cannot become production assessments or regulatory tracks')


def baseline(settings,user,job_id):
    tenant=user['tenant_id']
    with transaction(settings.app_dsn,tenant) as conn:
        job=conn.execute("SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s AND kind='assess_documents'",(tenant,job_id)).fetchone()
        if not job:raise service.Missing('Document assessment run not found')
        if job['status'] not in ('awaiting_review','completed') or not job['output_ids']:raise service.Conflict('Choose a completed document assessment run')
        publication_lock(conn,tenant,job['input_payload']['extraction_job_id'])
        findings=[service.assessment(conn,tenant,id) for id in job['output_ids']]
        if any(f['current_states']['freshness']!='current' for f in findings):raise service.Conflict('Use the current reassessment as the scenario baseline')
        rows=conn.execute('''WITH RECURSIVE inputs(id) AS (
          SELECT id FROM entity_versions WHERE tenant_id=%s AND id=ANY(%s)
          UNION SELECT d.input_id FROM dependencies d JOIN inputs i ON d.dependent_id=i.id WHERE d.tenant_id=%s)
          SELECT e.payload FROM entity_versions e JOIN inputs i ON e.id=i.id WHERE e.tenant_id=%s''',(tenant,job['output_ids'],tenant,tenant)).fetchall()
        followups=conn.execute("SELECT payload FROM entity_versions WHERE tenant_id=%s AND kind='gap' AND payload->>'assessment_id'=ANY(%s)",(tenant,job['output_ids'])).fetchall()
    source=ex.result(settings,tenant,job['input_payload']['extraction_job_id'])
    return {'job':service.public_job(job),'findings':findings,'entities':[r['payload'] for r in rows+followups],'source':source,
            'evidence_context':job['input_payload']['evidence_context']}


def create(settings,user,job_id,name,note,uploads,profile=None,context=None,clear_evidence=False):
    if not name.strip() or not note.strip():raise service.Conflict('Scenario name and hypothesis are required')
    saved=jsonable_encoder(baseline(settings,user,job_id));inputs=deepcopy(saved['source']['inputs']);store=ArtifactStore(settings.artifact_root)
    changes=[]
    for role in ('regulation','policy'):
        if role in uploads:
            new=ex.document(*uploads[role],role,store);old=next(d for d in inputs['documents'] if d['role']==role)
            if new['sha256']!=old['sha256']:changes.append({'field':role,'before':old['sha256'],'after':new['sha256']})
            inputs['documents']=[new if d['role']==role else d for d in inputs['documents']]
    old_ev=inputs['evidence']
    if clear_evidence and 'evidence' in uploads:raise service.Conflict('Choose revised evidence or removal, not both')
    if 'evidence' in uploads:
        if context is None:raise service.Conflict('Revised evidence needs an explicit producer, scope and period')
        inputs['evidence']=ex.evidence(*uploads['evidence'],store)
    elif clear_evidence:inputs['evidence']=None
    if (inputs['evidence'] or {}).get('sha256')!=(old_ev or {}).get('sha256'):changes.append({'field':'evidence','before':old_ev,'after':inputs['evidence']})
    if profile is not None and profile.model_dump()!=inputs['profile']:
        changes.append({'field':'profile','before':inputs['profile'],'after':profile.model_dump()});inputs['profile']=profile.model_dump()
    ctx=context.model_dump(mode='json') if context else saved['evidence_context']
    if not inputs['evidence']:ctx=None
    if ctx!=saved['evidence_context']:changes.append({'field':'evidence_context','before':saved['evidence_context'],'after':ctx})
    if not changes:raise service.Conflict('Change a document, profile or evidence input to create a scenario')
    id=str(uuid.uuid4());tenant=user['tenant_id']
    with transaction(settings.app_dsn,tenant) as conn:
        job=ex.enqueue(settings,user,inputs['documents'],ex.Profile.model_validate(inputs['profile']),inputs['evidence'],True,conn=conn,revision_id='scenario:'+id)
        conn.execute('''INSERT INTO scenarios(tenant_id,id,name,note,baseline_job_id,extraction_job_id,baseline,changes,evidence_context,created_by)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(tenant,id,name.strip(),note.strip(),job_id,job['id'],Jsonb(saved),Jsonb(changes),Jsonb(ctx),user['id']))
        service.audit(conn,tenant,user['id'],'scenario.created',id,{'name':name.strip(),'note':note.strip(),'baseline_job_id':job_id,'changed_fields':[c['field'] for c in changes],'hypothetical':True})
    return {'id':id,'extraction_job_id':job['id'],'status':'queued','hypothetical':True}


def summarize(entities,findings=None):
    by_id={e['id']:e for e in entities};groups=defaultdict(list)
    overrides={f['snapshot']['id']:f['current_states'] for f in findings or []}
    for e in entities:
        if e['kind']!='assessment':continue
        obligation=by_id[by_id[e['applicability_id']]['obligation_id']]
        groups[signature(obligation)].append({'assessment_id':e['id'],'obligation':obligation,'states':overrides.get(e['id'],e['states']),
          'gap_types':[g['gap_type'] for g in entities if g['kind']=='gap' and g['assessment_id']==e['id']]})
    return groups


def comparison(saved,entities):
    before=summarize(saved['entities'],saved['findings']);after=summarize(entities);rows=[]
    for key in sorted(before.keys()|after.keys(),key=str):
        old=before.get(key,[]);new=after.get(key,[])
        if len(old)==len(new)==1:
            rows.append({'match':'same_extracted_duty','before':old[0],'after':new[0],
              'changed_states':[k for k in old[0]['states'] if k not in ('review','freshness') and old[0]['states'][k]!=new[0]['states'][k]]})
        else:
            rows.extend({'match':'baseline_only_or_ambiguous','before':v,'after':None,'changed_states':[]} for v in old)
            rows.extend({'match':'scenario_only_or_ambiguous','before':None,'after':v,'changed_states':[]} for v in new)
    return {'rows':rows,'interpretation':'Matches require unique identical extracted duty fields. Changed or ambiguous duties remain unmatched; semantic review is required. A hypothetical sample pass is not an approved effectiveness finding.'}


def advance(settings,tenant):
    with transaction(settings.app_dsn,tenant) as conn:
        pending=conn.execute('''SELECT s.* FROM scenarios s JOIN workflow_jobs j ON j.tenant_id=s.tenant_id AND j.id=s.extraction_job_id
          WHERE s.tenant_id=%s AND j.status='completed'
          AND NOT EXISTS(SELECT 1 FROM scenario_assessment_links l WHERE l.tenant_id=s.tenant_id AND l.scenario_id=s.id)
          AND NOT EXISTS(SELECT 1 FROM scenario_results r WHERE r.tenant_id=s.tenant_id AND r.scenario_id=s.id)
          ORDER BY s.created_at LIMIT 5''',(tenant,)).fetchall()
        for s in pending:
            locked=conn.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',(tenant+':scenario:'+s['id'],)).fetchone()['locked']
            if not locked:continue
            if conn.execute('SELECT 1 FROM scenario_assessment_links WHERE tenant_id=%s AND scenario_id=%s UNION ALL SELECT 1 FROM scenario_results WHERE tenant_id=%s AND scenario_id=%s',(tenant,s['id'],tenant,s['id'])).fetchone():continue
            source=ex.result(settings,tenant,s['extraction_job_id'])
            if not any(i['kind']=='obligation' for i in source['items']):
                payload={'entities':[],'dependencies':[],'comparison':comparison(s['baseline'],[]),'warning':'No obligations extracted; this is not evidence of compliance.','hypothetical':True}
                conn.execute('INSERT INTO scenario_results VALUES (%s,%s,%s,now())',(tenant,s['id'],Jsonb(payload)))
                service.audit(conn,tenant,'worker','scenario.no_obligations',s['id'],{})
                continue
            job=ap.enqueue(settings,{'tenant_id':tenant,'id':s['created_by']},s['extraction_job_id'],ap.EvidenceContext.model_validate(s['evidence_context']) if s['evidence_context'] else None,conn=conn,scenario_id=s['id'])
            conn.execute('INSERT INTO scenario_assessment_links VALUES (%s,%s,%s)',(tenant,s['id'],job['id']))


def run(settings,job):
    bundle=ap.run(settings,job)
    entities,deps=validate_bundle(bundle,job['tenant_id'],ArtifactStore(settings.artifact_root))
    records=[e.model_dump(mode='json') for e in entities]
    with transaction(settings.app_dsn,job['tenant_id']) as conn:
        if not ex.fenced(conn,job):return
        s=get(conn,job['tenant_id'],job['input_payload']['scenario_id'])
        link=conn.execute('SELECT job_id FROM scenario_assessment_links WHERE tenant_id=%s AND scenario_id=%s',(job['tenant_id'],s['id'])).fetchone()
        if not link or link['job_id']!=job['id']:raise ex.ExtractionError('scenario_job_mismatch')
        payload={'entities':records,'dependencies':[d.model_dump(mode='json') for d in deps],'comparison':comparison(s['baseline'],records),'hypothetical':True}
        conn.execute('INSERT INTO scenario_results VALUES (%s,%s,%s,now())',(job['tenant_id'],s['id'],Jsonb(payload)))
        conn.execute("UPDATE workflow_jobs SET status='completed',lease_token=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",(job['tenant_id'],job['id']))
        service.audit(conn,job['tenant_id'],'worker','scenario.completed',s['id'],{'baseline_job_id':s['baseline_job_id'],'hypothetical':True})


def detail(settings,user,id):
    tenant=user['tenant_id']
    with transaction(settings.app_dsn,tenant) as conn:
        s=get(conn,tenant,id)
        extraction=conn.execute('SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s',(tenant,s['extraction_job_id'])).fetchone()
        assessment=conn.execute('SELECT j.* FROM workflow_jobs j JOIN scenario_assessment_links l ON l.tenant_id=j.tenant_id AND l.job_id=j.id WHERE l.tenant_id=%s AND l.scenario_id=%s',(tenant,id)).fetchone()
        result=conn.execute('SELECT payload FROM scenario_results WHERE tenant_id=%s AND scenario_id=%s',(tenant,id)).fetchone()
        current=[service.assessment(conn,tenant,f['snapshot']['id']) for f in s['baseline']['findings']]
    return {**s,'hypothetical':True,'extraction_job':service.public_job(extraction),'assessment_job':service.public_job(assessment) if assessment else None,
      'status':'completed' if result else (assessment['status'] if assessment else ('awaiting_assessment' if extraction['status']=='completed' else extraction['status'])),'result':result['payload'] if result else None,
      'baseline_changed_since_creation':any(a['current_states']!=b['current_states'] or a['review_revision']!=b['review_revision'] for a,b in zip(current,s['baseline']['findings']))}
