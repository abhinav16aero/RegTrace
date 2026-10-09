"""Auditable task lifecycle and trusted retests. Closing a task never overwrites an assessment."""
import csv
import io
import json
import uuid
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb
from . import assessment_pipeline as ap, extraction as ex, service
from .contracts import ENTITY_ADAPTER
from .storage import ArtifactStore, transaction

class Factor(BaseModel):
    model_config=ConfigDict(extra='forbid')
    value: int = Field(ge=1,le=5,strict=True)
    reason: str = Field(min_length=1,max_length=2000)
    source_ids: list[str] = Field(min_length=1,max_length=30)

class Factors(BaseModel):
    model_config=ConfigDict(extra='forbid')
    severity: Factor | None = None
    exposure: Factor | None = None
    urgency: Factor | None = None

class Action(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['assign','accept','start','close','reopen','prioritize']
    expected_revision: int = Field(ge=0)
    note: str = Field(min_length=1,max_length=3000)
    owner_id: str | None = None
    criteria_confirmed: bool = False
    factors: Factors | None = None


def score(factors):
    weights={'severity':.5,'exposure':.3,'urgency':.2}
    missing=[name for name in weights if not factors.get(name)]
    points=None if missing else round(20*sum(weights[k]*factors[k]['value'] for k in weights),1)
    return {'score':points,'band':'needs_triage' if missing else ('high' if points>=80 else 'medium' if points>=50 else 'low'),
            'missing_factors':missing,'weights':weights,'formula':'20 × (0.5 severity + 0.3 exposure + 0.2 urgency)',
            'rule_version':'reviewer-triage-1','factors':factors,
            'meaning':'Reviewer-assigned triage; not a regulatory risk rating or inferred legal deadline.'}


def lock(conn,tenant,id):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant+':remediation:'+id,))


def records(conn,tenant,id):
    remedy=service.entity(conn,tenant,id)
    if remedy['kind']!='remediation':raise service.Missing('Remediation not found')
    gap=service.entity(conn,tenant,remedy['gap_id'])
    assessment=service.assessment(conn,tenant,gap['assessment_id'])
    return remedy,gap,assessment


def state(conn,tenant,id):
    remedy,gap,assessment=records(conn,tenant,id)
    row=conn.execute('SELECT * FROM remediation_events WHERE tenant_id=%s AND remediation_id=%s ORDER BY revision DESC LIMIT 1',(tenant,id)).fetchone()
    current=deepcopy(row['state']) if row else {'status':'proposed','owner_id':None,'active_retest_job_id':None,
                    'latest_retest_job_id':None,'latest_retest_outcome':None,'factors':{}}
    return current,row['revision'] if row else 0,remedy,gap,assessment


def append(conn,tenant,id,actor,action,note,current,revision):
    conn.execute('INSERT INTO remediation_events(tenant_id,remediation_id,revision,action,actor_id,note,state) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                 (tenant,id,revision+1,action,actor,note,Jsonb(current)))
    service.audit(conn,tenant,actor,'remediation.'+action,id,{'revision':revision+1,'status':current['status'],'note':note})


def source_lock(conn,tenant,id):
    remedy=service.entity(conn,tenant,id)
    if remedy['kind']!='remediation':raise service.Missing('Remediation not found')
    gap=service.entity(conn,tenant,remedy['gap_id'])
    job=conn.execute("SELECT input_payload FROM workflow_jobs WHERE tenant_id=%s AND kind='assess_documents' AND output_ids ? %s",(tenant,gap['assessment_id'])).fetchone()
    if job:
        from .regulatory_changes import publication_lock
        publication_lock(conn,tenant,job['input_payload']['extraction_job_id'])


def detail(conn,tenant,id):
    current,revision,remedy,gap,assessment=state(conn,tenant,id)
    history=conn.execute('SELECT revision,action,actor_id,note,created_at,state FROM remediation_events WHERE tenant_id=%s AND remediation_id=%s ORDER BY revision',(tenant,id)).fetchall()
    retests=conn.execute('SELECT job_id,payload,created_at FROM remediation_retests WHERE tenant_id=%s AND remediation_id=%s ORDER BY created_at',(tenant,id)).fetchall()
    related=conn.execute('''WITH RECURSIVE inputs(id) AS (
      SELECT id FROM entity_versions WHERE tenant_id=%s AND id=%s
      UNION SELECT d.input_id FROM dependencies d JOIN inputs i ON d.dependent_id=i.id WHERE d.tenant_id=%s)
      SELECT e.id,e.kind FROM entity_versions e JOIN inputs i ON e.id=i.id WHERE e.tenant_id=%s ORDER BY e.kind,e.id''',
      (tenant,gap['assessment_id'],tenant,tenant)).fetchall()
    counts=[]
    for run_id in assessment['snapshot']['test_run_ids']:
        test=service.entity(conn,tenant,run_id)
        counts.append({'test_run_id':run_id,'outcome':test['outcome'],'sample_size':test['sample_size'],'failed_cases':len(test['failed_case_ids'])})
    try:protocols(conn,tenant,gap,assessment);retest_available=True;reason=None
    except service.Conflict as error:retest_available=False;reason=str(error)
    return {'remediation':remedy,'gap':gap,'assessment_id':gap['assessment_id'],'assessment_states':assessment['current_states'],
            'state':current,'revision':revision,'priority':score(current['factors']),'history':history,'retests':retests,
            'source_records':related,'evidence_signals':counts,'retest_available':retest_available,'retest_unavailable_reason':reason}


def protocols(conn,tenant,gap,assessment):
    if gap['gap_type']!='execution_failure':raise service.Conflict('This gap requires a new source assessment; a CSV retest alone cannot resolve it')
    s=assessment['current_states']
    if s['freshness']!='current':raise service.Conflict('Regulation changed; use the current reassessment before retesting or closing')
    if s['applicability']!='applicable' or s['coverage']!='full' or s['verification']!='supported':
        raise service.Conflict('Supported applicability and full verified coverage are required for this retest')
    result=[]
    for id in assessment['snapshot']['test_run_ids']:
        run=service.entity(conn,tenant,id);definition=service.entity(conn,tenant,run['definition_id'])
        try:procedure=ap.Procedure.model_validate_json(definition['procedure'])
        except ValueError:raise service.Conflict('No executable document-derived procedure; fixture outcomes cannot authorize closure') from None
        if procedure.kind=='unsupported':raise service.Conflict('The original assessment requires an unsupported test')
        result.append({'original_test_id':id,'definition_id':definition['id'],'control_id':run['control_id'],
                       'procedure':procedure.model_dump(),'required_case_ids':run['failed_case_ids']})
    if not result:raise service.Conflict('No original executable tests')
    return result


def can_work(user,current):
    if user['role']!='reviewer' and user['id']!=current['owner_id']:
        raise service.Conflict('Only the assigned owner or a reviewer can change execution status or retest')


def act(settings,user,id,body):
    tenant=user['tenant_id']
    if not body.note.strip():raise service.Conflict('A meaningful note is required')
    if body.action in ('accept','close','prioritize') and user['role']!='reviewer':
        raise service.Conflict('Reviewer role required for acceptance, closure and prioritization')
    if body.action=='assign':
        with transaction(settings.auth_dsn) as auth:
            owner=auth.execute('SELECT id FROM app_users WHERE tenant_id=%s AND id=%s AND NOT disabled',(tenant,body.owner_id)).fetchone()
        if not owner:raise service.Conflict('Choose an active owner in this workspace')
    with transaction(settings.app_dsn,tenant) as conn:
        source_lock(conn,tenant,id);lock(conn,tenant,id)
        current,revision,remedy,gap,assessment=state(conn,tenant,id)
        if revision!=body.expected_revision:raise service.Conflict('Task changed; refresh before submitting')
        status=current['status']
        if body.action=='assign':
            if status=='closed':raise service.Conflict('Reopen before reassigning a closed task')
            current['owner_id']=body.owner_id
        elif body.action=='accept':
            if status!='proposed':raise service.Conflict('Only a proposed task can be accepted')
            current['status']='accepted'
        elif body.action=='start':
            can_work(user,current)
            if status not in ('accepted','reopened') or not current['owner_id']:raise service.Conflict('Assign and accept the task before starting work')
            current['status']='in_progress'
        elif body.action=='reopen':
            can_work(user,current)
            if status!='closed':raise service.Conflict('Only a closed task can be reopened manually')
            current['status']='reopened'
        elif body.action=='close':
            protocols(conn,tenant,gap,assessment)
            if status!='awaiting_retest' or current['active_retest_job_id'] or not current['owner_id'] or not body.criteria_confirmed:
                raise service.Conflict('Assign an owner, complete a retest and confirm every acceptance criterion before closing')
            retest=conn.execute('SELECT payload FROM remediation_retests WHERE tenant_id=%s AND job_id=%s AND remediation_id=%s',
                                (tenant,current['latest_retest_job_id'],id)).fetchone()
            if not retest or retest['payload']['outcome']!='pass':raise service.Conflict('The latest completed retest must pass')
            try:
                for evidence in retest['payload']['evidence']:ArtifactStore(settings.artifact_root).read(evidence['sha256'])
            except (OSError,ValueError):raise service.Conflict('Retest artifact is missing or failed hash verification') from None
            current['status']='closed'
        elif body.action=='prioritize':
            if body.factors is None:raise service.Conflict('Supply risk factors, leaving unknown values unset')
            allowed={r['id'] for r in detail(conn,tenant,id)['source_records']}
            allowed|={remedy['id'],gap['id']}
            for row in conn.execute('SELECT payload FROM remediation_retests WHERE tenant_id=%s AND remediation_id=%s',(tenant,id)):
                allowed|={e['id'] for e in row['payload']['evidence']}
            for factor in body.factors.model_dump().values():
                if factor and (not factor['reason'].strip() or not set(factor['source_ids'])<=allowed):
                    raise service.Conflict('Each factor needs a reason and supporting records from this finding')
            current['factors']=body.factors.model_dump()
        append(conn,tenant,id,user['id'],body.action,body.note.strip(),current,revision)
        return detail(conn,tenant,id)


def request_retest(settings,user,id,raw,filename,context,expected_revision,note):
    tenant=user['tenant_id'];store=ArtifactStore(settings.artifact_root)
    if not note.strip():raise service.Conflict('Describe the correction and how the evidence relates to this finding')
    attached=ex.evidence(raw,filename,store)
    with transaction(settings.app_dsn,tenant) as conn:
        source_lock(conn,tenant,id);lock(conn,tenant,id)
        current,revision,remedy,gap,assessment=state(conn,tenant,id)
        if revision!=expected_revision:raise service.Conflict('Task changed; refresh before submitting')
        can_work(user,current)
        if current['status'] not in ('in_progress','awaiting_retest','reopened','closed') or not current['owner_id'] or current['active_retest_job_id']:
            raise service.Conflict('Start assigned work and wait for any active retest before submitting evidence')
        rules=protocols(conn,tenant,gap,assessment)
        for rule in rules:
            p=rule['procedure'];columns={p['case_id_column'],p['first_column']}
            if p['kind']=='timestamp_order':columns.add(p['second_column'])
            if not columns<=set(attached['columns']):raise service.Conflict('Revised CSV is missing columns required by the original procedure')
        payload={'remediation_id':id,'assessment_id':gap['assessment_id'],'evidence':attached,'context':context.model_dump(mode='json'),
                 'protocols':rules,'requested_revision':revision,'note':note.strip()}
        h=ex.digest(ex.canonical(payload));job_id=str(uuid.uuid4())
        conn.execute('''INSERT INTO workflow_jobs(tenant_id,id,idempotency_key,kind,status,input_hash,configuration_hash,input_payload,requested_by)
          VALUES (%s,%s,%s,'retest_remediation','queued',%s,%s,%s,%s)''',
          (tenant,job_id,'retest:'+h,h,ex.digest(b'remediation-retest-1'),Jsonb(payload),user['id']))
        current['status']='awaiting_retest';current['active_retest_job_id']=job_id
        append(conn,tenant,id,user['id'],'retest_requested',note.strip(),current,revision)
        return {'id':job_id,'status':'queued'}


def on_aborted(conn,job,reason):
    if job['kind']!='retest_remediation':return
    tenant=job['tenant_id'];id=job['input_payload']['remediation_id'];lock(conn,tenant,id)
    current,revision,*_=state(conn,tenant,id)
    if current['active_retest_job_id']!=job['id']:return
    current['active_retest_job_id']=None;current['status']='reopened'
    append(conn,tenant,id,'worker','retest_aborted',reason,current,revision)


def before_retry(conn,user,job):
    if job['kind']!='retest_remediation':return
    tenant=user['tenant_id'];id=job['input_payload']['remediation_id'];lock(conn,tenant,id)
    current,revision,remedy,gap,assessment=state(conn,tenant,id);can_work(user,current)
    if current['active_retest_job_id'] or current['status']=='closed':raise service.Conflict('A closed task or active retest cannot reuse this job')
    protocols(conn,tenant,gap,assessment)
    current['active_retest_job_id']=job['id'];current['status']='awaiting_retest'
    append(conn,tenant,id,user['id'],'retest_requested','Explicit retry of the same immutable evidence.',current,revision)


def reopen_for_source(conn,tenant,assessment_id):
    rows=conn.execute("""SELECT r.id FROM entity_versions r JOIN entity_versions g ON g.tenant_id=r.tenant_id AND g.id=r.payload->>'gap_id'
      WHERE r.tenant_id=%s AND r.kind='remediation' AND g.payload->>'assessment_id'=%s ORDER BY r.id""",(tenant,assessment_id)).fetchall()
    for row in rows:
        id=row['id'];lock(conn,tenant,id);current,revision,*_=state(conn,tenant,id)
        if current['status']=='closed':
            current['status']='reopened'
            append(conn,tenant,id,'worker','source_changed','The source assessment became stale; closure must be reassessed.',current,revision)


def run(settings,job):
    tenant=job['tenant_id'];payload=job['input_payload'];id=payload['remediation_id'];store=ArtifactStore(settings.artifact_root)
    if ex.digest(ex.canonical(payload))!=job['input_hash']:raise ex.ExtractionError('retest_input_hash_changed')
    raw=store.read(payload['evidence']['sha256']);context=ap.EvidenceContext.model_validate(payload['context'])
    tests=[];evidence=[];entities=[];deps=[]
    for index,rule in enumerate(payload['protocols']):
        procedure=ap.Procedure.model_validate(rule['procedure'])
        result=ap.execute_csv(raw,procedure,context)
        cases={row.get(procedure.case_id_column,'').strip() for row in csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))}
        omitted=set(rule['required_case_ids'])-cases
        if omitted:
            if result['outcome']=='pass':result['outcome']='inconclusive'
            result['limitations'].append('Previously failed cases missing from retest: '+', '.join(sorted(omitted)))
        base={'schema_version':'1.0','tenant_id':tenant,'version':1}
        eid='RE-'+job['id']+'-'+str(index);tid='RT-'+job['id']+'-'+str(index)
        ev=base|{'id':eid,'lineage_id':eid,'kind':'evidence','control_id':rule['control_id'],
          'artifact_path':'sha256:'+payload['evidence']['sha256'],'sha256':payload['evidence']['sha256'],
          'producer':context.producer,'collected_at':context.collected_at.isoformat(),'period_start':context.period_start.isoformat(),'period_end':context.period_end.isoformat()}
        test=base|{'id':tid,'lineage_id':tid,'kind':'test_run','definition_id':rule['definition_id'],'control_id':rule['control_id'],'evidence_ids':[eid]}|result
        entities.extend([ev,test]);evidence.append(ev);tests.append(test)
        deps.extend([(eid,rule['control_id'],'evidences_control'),(tid,rule['control_id'],'tests_control'),(tid,rule['definition_id'],'uses_definition'),(tid,eid,'uses_evidence')])
    outcome='fail' if any(t['outcome']=='fail' for t in tests) else ('pass' if tests and all(t['outcome']=='pass' for t in tests) else 'inconclusive')
    with transaction(settings.app_dsn,tenant) as conn:
        source_lock(conn,tenant,id);lock(conn,tenant,id)
        if not ex.fenced(conn,job):on_aborted(conn,job,'Retest cancelled.');return
        current,revision,remedy,gap,assessment=state(conn,tenant,id)
        if current['active_retest_job_id']!=job['id']:raise ex.ExtractionError('retest_no_longer_active')
        # Original procedures and evidence are immutable; only current source freshness may have changed.
        for rule in payload['protocols']:
            definition=service.entity(conn,tenant,rule['definition_id']);original=service.entity(conn,tenant,rule['original_test_id'])
            if json.loads(definition['procedure'])!=rule['procedure'] or original['definition_id']!=rule['definition_id'] or original['control_id']!=rule['control_id'] or original['failed_case_ids']!=rule['required_case_ids']:
                raise ex.ExtractionError('retest_protocol_mismatch')
        for record in entities:
            record['synthetic']=remedy['synthetic'];parsed=ENTITY_ADAPTER.validate_json(json.dumps(record));data=parsed.model_dump(mode='json')
            conn.execute('INSERT INTO entity_versions(tenant_id,id,lineage_id,version,kind,payload) VALUES (%s,%s,%s,1,%s,%s)',
                         (tenant,data['id'],data['lineage_id'],data['kind'],Jsonb(data)))
        for dependent,input_id,relation in deps:conn.execute('INSERT INTO dependencies VALUES (%s,%s,%s,%s)',(tenant,dependent,input_id,relation))
        record={'outcome':outcome,'tests':tests,'evidence':evidence,'context':payload['context'],
                'original_assessment_id':payload['assessment_id'],'note':payload['note'],'scope':'Original failed cases and supplied sample only.'}
        conn.execute('INSERT INTO remediation_retests(tenant_id,job_id,remediation_id,payload) VALUES (%s,%s,%s,%s)',(tenant,job['id'],id,Jsonb(record)))
        current.update({'active_retest_job_id':None,'latest_retest_job_id':job['id'],'latest_retest_outcome':outcome,
                        'status':'awaiting_retest' if outcome=='pass' and assessment['current_states']['freshness']=='current' else 'reopened'})
        append(conn,tenant,id,'worker','retest_'+outcome,'Executed the original declared procedures against revised evidence; reviewer closure is separate.',current,revision)
        conn.execute("UPDATE workflow_jobs SET status='completed',lease_token=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",(tenant,job['id']))


def lock_for_job(conn,tenant,job_id):
    job=conn.execute('SELECT kind,input_payload FROM workflow_jobs WHERE tenant_id=%s AND id=%s',(tenant,job_id)).fetchone()
    if job and job['kind']=='retest_remediation':
        id=job['input_payload']['remediation_id'];source_lock(conn,tenant,id);lock(conn,tenant,id)


def reconcile_aborted(settings,tenant):
    with transaction(settings.app_dsn,tenant) as conn:
        jobs=conn.execute('''SELECT j.* FROM workflow_jobs j JOIN LATERAL (
          SELECT state FROM remediation_events e WHERE e.tenant_id=j.tenant_id AND e.remediation_id=j.input_payload->>'remediation_id'
          ORDER BY revision DESC LIMIT 1) e ON e.state->>'active_retest_job_id'=j.id
          WHERE j.tenant_id=%s AND j.kind='retest_remediation' AND j.status IN ('failed','cancelled') LIMIT 20''',(tenant,)).fetchall()
        for job in jobs:
            lock_for_job(conn,tenant,job['id'])
            status=conn.execute('SELECT status FROM workflow_jobs WHERE tenant_id=%s AND id=%s',(tenant,job['id'])).fetchone()['status']
            if status in ('failed','cancelled'):on_aborted(conn,job,'Retest ended without a published result: '+status)
