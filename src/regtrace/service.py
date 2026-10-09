import hashlib
import json
import uuid
from copy import deepcopy
from psycopg.types.json import Jsonb
from .contracts import ENTITY_ADAPTER
from .settings import ROOT
from .storage import ArtifactStore

class Conflict(Exception): pass
class Missing(Exception): pass

def audit(conn,tenant,actor,action,resource,details):
    conn.execute('INSERT INTO audit_events VALUES (%s,%s,%s,%s,%s,%s,now())',
                 (tenant,str(uuid.uuid4()),actor,action,resource,Jsonb(details)))

def fixture_bundle(settings,tenant):
    data=(ROOT/'data/fixtures/onboarding.json').read_bytes()
    config=json.loads((ROOT/'config/runtime.json').read_text())
    if hashlib.sha256(data).hexdigest()!=config['model']['fixture_sha256']:
        raise Conflict('Pinned fixture hash changed')
    bundle=json.loads(data)
    store=ArtifactStore(settings.artifact_root)
    for entity in bundle['entities']:
        entity['tenant_id']=tenant
        if entity['kind'] in ('source','evidence'):
            store.put((ROOT/entity['artifact_path']).read_bytes(),entity['sha256'])
        if entity['kind']=='source':
            store.put((ROOT/entity['text_path']).read_bytes(),entity['text_sha256'])
    for dep in bundle['dependencies']:dep['tenant_id']=tenant
    canonical=json.dumps(bundle,sort_keys=True,separators=(',',':')).encode()
    return bundle,hashlib.sha256(canonical).hexdigest(),hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()

def enqueue(conn,tenant,actor,bundle,input_hash,config_hash):
    key=hashlib.sha256(f'{tenant}:load_fixture:{input_hash}:{config_hash}'.encode()).hexdigest()
    result=conn.execute('''INSERT INTO workflow_jobs
      (tenant_id,id,idempotency_key,kind,status,input_hash,configuration_hash,input_payload,requested_by)
      VALUES (%s,%s,%s,'load_fixture','queued',%s,%s,%s,%s)
      ON CONFLICT(tenant_id,idempotency_key) DO NOTHING RETURNING *''',
      (tenant,str(uuid.uuid4()),key,input_hash,config_hash,Jsonb(bundle),actor)).fetchone()
    if result:
        audit(conn,tenant,actor,'job.queued',result['id'],{'input_hash':input_hash,'configuration_hash':config_hash,'mode':'fixture'})
    else:
        result=conn.execute('SELECT * FROM workflow_jobs WHERE tenant_id=%s AND idempotency_key=%s',(tenant,key)).fetchone()
    return public_job(result)

def public_job(job):
    return {k:v for k,v in job.items() if k not in ('input_payload','lease_token','idempotency_key')}

def entity(conn,tenant,id):
    row=conn.execute('SELECT payload FROM entity_versions WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
    if not row: raise Missing('Resource not found')
    return row['payload']

def assessment(conn,tenant,id):
    data=entity(conn,tenant,id)
    if data['kind']!='assessment': raise Missing('Assessment not found')
    reviews=conn.execute('SELECT * FROM review_events WHERE tenant_id=%s AND assessment_id=%s ORDER BY revision',(tenant,id)).fetchall()
    effective=deepcopy(data)
    if reviews:effective['states']['review']=reviews[-1]['decision']
    freshness=conn.execute('SELECT version_id,reason,created_at FROM assessment_freshness_events WHERE tenant_id=%s AND assessment_id=%s ORDER BY created_at',(tenant,id)).fetchall()
    if freshness:effective['states']['freshness']='stale'
    parsed=ENTITY_ADAPTER.validate_json(json.dumps(effective))
    return {'snapshot':data,'current_states':effective['states'],'approved_effective':parsed.approved_effective,
            'review_revision':reviews[-1]['revision'] if reviews else 0,'reviews':reviews,'freshness_events':freshness}

def review(conn,tenant,actor,id,decision,note,expected_revision):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant+':'+id,))
    current=assessment(conn,tenant,id)
    if current['review_revision']!=expected_revision:raise Conflict('Review changed; refresh before submitting')
    conn.execute('INSERT INTO review_events (tenant_id,id,assessment_id,revision,decision,note,actor_id) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                 (tenant,str(uuid.uuid4()),id,expected_revision+1,decision,note,actor))
    audit(conn,tenant,actor,'assessment.reviewed',id,{'decision':decision,'note':note,'revision':expected_revision+1,
          'test_outcome':current['snapshot']['states']['test']})
    jobs=conn.execute("SELECT * FROM workflow_jobs WHERE tenant_id=%s AND status IN ('awaiting_review','completed') ORDER BY id FOR UPDATE",(tenant,)).fetchall()
    for job in jobs:
        terminal=all(assessment(conn,tenant,ref)['current_states']['review'] in ('approved','rejected') for ref in job['output_ids'])
        status='completed' if terminal else 'awaiting_review'
        if status!=job['status']:
            conn.execute('UPDATE workflow_jobs SET status=%s,updated_at=now() WHERE tenant_id=%s AND id=%s',(status,tenant,job['id']))
            audit(conn,tenant,actor,'job.completed' if terminal else 'job.review_reopened',job['id'],
                  {'meaning':'processing and review status; not a compliance verdict'})
    return assessment(conn,tenant,id)

def cancel(conn,tenant,actor,id):
    from .remediation import lock_for_job
    lock_for_job(conn,tenant,id)
    job=conn.execute('SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,id)).fetchone()
    if not job:raise Missing('Job not found')
    if job['status'] in ('completed','failed','cancelled','awaiting_review'):
        raise Conflict('Job has no cancellable work')
    status='cancelled' if job['status']=='queued' else 'running'
    conn.execute('UPDATE workflow_jobs SET cancel_requested=true,status=%s,updated_at=now() WHERE tenant_id=%s AND id=%s',(status,tenant,id))
    if status=='cancelled':
        from .remediation import on_aborted
        on_aborted(conn,job,'Retest cancelled before execution.')
    audit(conn,tenant,actor,'job.cancel_requested',id,{'status':status})
    return {'id':id,'status':status,'cancel_requested':True}
