"""Durable fixture-import worker. Every publication is one database transaction."""
import argparse
import hashlib
import json
import logging
import time
import uuid
from psycopg.types.json import Jsonb
from .bundle import validate_bundle
from . import extraction, assessment_pipeline, regulatory_changes, remediation, scenarios, cross_regulation, contradictions
from .service import audit
from .settings import Settings
from .storage import ArtifactStore, transaction

log=logging.getLogger(__name__)

def claim(settings,tenant):
    with transaction(settings.app_dsn,tenant) as conn:
        exhausted=conn.execute("""UPDATE workflow_jobs SET status='failed',error_code='attempts_exhausted',
          lease_token=NULL,lease_expires_at=NULL,updated_at=now()
          WHERE tenant_id=%s AND status='running' AND lease_expires_at<=now() AND attempts>=3
          RETURNING id""",(tenant,)).fetchall()
        for row in exhausted:audit(conn,tenant,'worker','job.failed',row['id'],{'error_code':'attempts_exhausted'})
        job=conn.execute("""SELECT * FROM workflow_jobs WHERE tenant_id=%s AND attempts<3
          AND (status='queued' OR (status='running' AND lease_expires_at<=now()))
          ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""",(tenant,)).fetchone()
        if not job:return None
        token=str(uuid.uuid4())
        result=conn.execute("""UPDATE workflow_jobs SET status='running',attempts=attempts+1,
          lease_token=%s,lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
          WHERE tenant_id=%s AND id=%s RETURNING *""",(token,settings.lease_seconds,tenant,job['id'])).fetchone()
        audit(conn,tenant,'worker','job.claimed',job['id'],{'attempt':result['attempts'],'recovered':job['status']=='running'})
        return result

def reconcile_cancellation(settings,job):
    # Checkpoint exceptions roll back their transaction, including a cancellation
    # observed by fenced(). Commit that cancellation separately, still lease-fenced.
    with transaction(settings.app_dsn,job['tenant_id']) as conn:
        remediation.lock_for_job(conn,job['tenant_id'],job['id'])
        try:
            if not extraction.fenced(conn,job):remediation.on_aborted(conn,job,'Retest cancelled during execution.')
        except extraction.LeaseLost:pass
    return False

def finish(settings,job):
    if job['kind']=='detect_contradictions':
        try:return contradictions.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    if job['kind']=='cross_regulation':
        try:return cross_regulation.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    if job['kind']=='assess_scenario':
        try:return scenarios.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    if job['kind']=='retest_remediation':
        try:return remediation.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    if job['kind']=='monitor_regulation':
        try:return regulatory_changes.run_monitor(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    if job['kind']=='extract_documents':
        try:return extraction.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    bundle=job['input_payload']
    if job['kind']=='assess_documents':
        try:bundle=assessment_pipeline.run(settings,job)
        except extraction.LeaseLost:return reconcile_cancellation(settings,job)
    tenant=job['tenant_id']
    raw=json.dumps(job['input_payload'],sort_keys=True,separators=(',',':')).encode()
    if job['kind']=='load_fixture' and hashlib.sha256(raw).hexdigest()!=job['input_hash']:raise ValueError('Job input hash changed')
    entities,deps=validate_bundle(bundle,tenant,ArtifactStore(settings.artifact_root))
    with transaction(settings.app_dsn,tenant) as conn:
        if job['kind']=='assess_documents':regulatory_changes.publication_lock(conn,tenant,job['input_payload']['extraction_job_id'])
        current=conn.execute('''SELECT *,lease_expires_at>now() AS valid_lease FROM workflow_jobs
          WHERE tenant_id=%s AND id=%s FOR UPDATE''',(tenant,job['id'])).fetchone()
        if not current or current['status']!='running' or current['lease_token']!=job['lease_token'] or not current['valid_lease']:
            return False  # Fenced worker has no authority to publish.
        if current['cancel_requested']:
            conn.execute("UPDATE workflow_jobs SET status='cancelled',lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",(tenant,job['id']))
            audit(conn,tenant,'worker','job.cancelled',job['id'],{})
            return True
        for entity in entities:
            payload=entity.model_dump(mode='json')
            conn.execute('''INSERT INTO entity_versions(tenant_id,id,lineage_id,version,kind,payload)
              VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,id) DO NOTHING''',
              (tenant,entity.id,entity.lineage_id,entity.version,entity.kind,Jsonb(payload)))
            stored=conn.execute('SELECT payload FROM entity_versions WHERE tenant_id=%s AND id=%s',(tenant,entity.id)).fetchone()
            if stored['payload']!=payload:raise ValueError('Immutable entity ID reused with changed content')
        for dep in deps:
            conn.execute('INSERT INTO dependencies VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (tenant,dep.dependent_id,dep.input_id,dep.relation))
        ids=[e.id for e in entities]
        stored=conn.execute('SELECT dependent_id,input_id,relation FROM dependencies WHERE tenant_id=%s AND dependent_id=ANY(%s)',(tenant,ids)).fetchall()
        if {(d['dependent_id'],d['input_id'],d['relation']) for d in stored}!={(d.dependent_id,d.input_id,d.relation) for d in deps}:
            raise ValueError('Stored dependencies differ from the validated bundle')
        outputs=[e.id for e in entities if e.kind=='assessment']
        conn.execute("""UPDATE workflow_jobs SET status='awaiting_review',output_ids=%s,error_code=NULL,
          lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s""",(Jsonb(outputs),tenant,job['id']))
        regulatory_changes.on_publish(settings,conn,job)
        audit(conn,tenant,'worker','fixture.imported' if job['kind']=='load_fixture' else 'assessment.computed',job['id'],{'entity_ids':ids,'assessment_ids':outputs,
              'input_hash':job['input_hash'],'configuration_hash':job['configuration_hash'],'mode':'fixture' if job['kind']=='load_fixture' else 'document_assessment'})
        return True

def failed(settings,job,error):
    with transaction(settings.app_dsn,job['tenant_id']) as conn:
        remediation.lock_for_job(conn,job['tenant_id'],job['id'])
        status='failed' if isinstance(error,ValueError) or job['attempts']>=3 else 'queued'
        code=str(error) if isinstance(error,extraction.ExtractionError) else ('invalid_input' if isinstance(error,ValueError) else 'execution_error')
        row=conn.execute('''UPDATE workflow_jobs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE %s END,
          error_code=%s,lease_token=NULL,lease_expires_at=NULL,updated_at=now()
          WHERE tenant_id=%s AND id=%s AND lease_token=%s AND status='running' RETURNING status''',
          (status,code,job['tenant_id'],job['id'],job['lease_token'])).fetchone()
        if row:
            if row['status'] in ('failed','cancelled'):remediation.on_aborted(conn,job,'Retest job '+row['status']+': '+code)
            audit(conn,job['tenant_id'],'worker','job.'+row['status'],job['id'],{'error_code':code})

def tick(settings):
    worked=False
    for tenant in settings.tenants:
        scenarios.advance(settings,tenant)
        remediation.reconcile_aborted(settings,tenant)
        regulatory_changes.advance(settings,tenant)
        regulatory_changes.schedule(settings,tenant)
        job=claim(settings,tenant)
        if job:
            worked=True
            try:finish(settings,job)
            except Exception as error:
                log.error('Job %s failed (%s)',job['id'],type(error).__name__)
                failed(settings,job,error)
    return worked

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');args=parser.parse_args()
    settings=Settings.load();logging.basicConfig(level=logging.INFO)
    while True:
        try:tick(settings)
        except Exception:log.exception('Worker tick failed; persisted leases remain recoverable')
        if args.once:break
        time.sleep(1)

if __name__=='__main__':main()
