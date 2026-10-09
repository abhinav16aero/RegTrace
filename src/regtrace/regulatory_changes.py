"""Version lineage, conservative obligation differences, graph impact and bounded monitoring."""
from contextlib import nullcontext
import json
import time
import uuid
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb
from . import extraction as ex, assessment_pipeline as ap
from .service import audit, Conflict, Missing
from .settings import ROOT
from .storage import ArtifactStore, transaction


def normalized(text):
    # Preserve punctuation/case/digits: those can change a legal requirement.
    return ' '.join(text.split())


def signature(item,fields=('actor','action','object','conditions','exceptions','timing')):
    return tuple(tuple(normalized(v) for v in item.get(k,[])) if isinstance(item.get(k),list)
                 else normalized(item.get(k) or '') for k in fields)


def compare(old,new):
    before=[i for i in old if i['kind']=='obligation'];after=[i for i in new if i['kind']=='obligation']
    unchanged=[];remaining=list(after);removed=[]
    for previous in before:
        match=next((i for i in remaining if signature(i)==signature(previous)),None)
        if match:
            remaining.remove(match);unchanged.append({'before':previous['id'],'after':match['id']})
        else:removed.append(previous)
    changes=[]
    # Only pair unique actor/action/object anchors. Ambiguous matches stay added/removed.
    for previous in list(removed):
        anchor=signature(previous,('actor','action','object'))
        matches=[i for i in remaining if signature(i,('actor','action','object'))==anchor]
        if len(matches)==1 and sum(signature(i,('actor','action','object'))==anchor for i in removed)==1:
            following=matches[0];remaining.remove(following);removed.remove(previous)
            changes.append({'type':'modified','before':previous,'after':following,
              'changed_fields':[k for k in ('conditions','exceptions','timing') if signature(previous,(k,))!=signature(following,(k,))]})
    changes.extend({'type':'removed','before':i,'after':None,'changed_fields':[]} for i in removed)
    changes.extend({'type':'added','before':None,'after':i,'changed_fields':[]} for i in remaining)
    return {'changes':changes,'unchanged':unchanged,'interpretation':'Potential duty changes from extracted fields; semantic/legal review pending.',
            'scope_expansion':bool(remaining),'requires_review':True}


def available_sources():
    return [{'id':s['id'],'url':s['retrieval_url'],'title':s['id'],'sha256':s['sha256']}
            for s in json.loads((ROOT/'data/manifest.json').read_text())['sources']
            if s['source_status']=='selected_official_consolidated_snapshot']


def get_track(conn,tenant,id,lock=False):
    row=conn.execute('SELECT * FROM regulatory_tracks WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ''),(tenant,id)).fetchone()
    if not row:raise Missing('Regulatory track not found')
    return row


def create_track(settings,user,name,extraction_id,source_id=None,interval=86400,synthetic=False):
    from .scenarios import assert_production
    with transaction(settings.app_dsn,user['tenant_id']) as conn:assert_production(conn,user['tenant_id'],extraction_id)
    result=ex.result(settings,user['tenant_id'],extraction_id)
    if not result['complete']:raise Conflict('Complete the baseline extraction first')
    document=next(d for d in result['inputs']['documents'] if d['role']=='regulation')
    if source_id:
        source=next((s for s in available_sources() if s['id']==source_id),None)
        if not source or source['sha256']!=document['sha256']:
            raise Conflict('A monitored baseline must match the selected pinned official source')
        if synthetic:raise Conflict('A synthetic baseline cannot enable official-source monitoring')
    tenant=user['tenant_id'];track_id=str(uuid.uuid4())
    with transaction(settings.app_dsn,tenant) as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant+':regulatory_tracks',))
        if conn.execute('SELECT count(*) AS n FROM regulatory_tracks WHERE tenant_id=%s',(tenant,)).fetchone()['n']>=10:
            raise Conflict('At most ten regulatory tracks per workspace')
        existing=conn.execute('SELECT * FROM regulatory_tracks WHERE tenant_id=%s AND baseline_extraction_id=%s',(tenant,extraction_id)).fetchone()
        if existing:return existing
        row=conn.execute('''INSERT INTO regulatory_tracks(tenant_id,id,name,baseline_extraction_id,current_extraction_id,source_id,monitor_enabled,interval_seconds)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(tenant,track_id,name,extraction_id,extraction_id,source_id,bool(source_id),interval)).fetchone()
        conn.execute('''INSERT INTO regulatory_versions(tenant_id,id,track_id,sequence,previous_extraction_id,extraction_id,document,kind,status,origin)
          VALUES (%s,%s,%s,1,%s,%s,%s,%s,'baseline','baseline')''',
          (tenant,str(uuid.uuid4()),track_id,extraction_id,extraction_id,Jsonb(document),'synthetic_snapshot' if synthetic else 'full_snapshot'))
        audit(conn,tenant,user['id'],'regulation.tracked',track_id,{'name':name,'source_id':source_id,'synthetic':synthetic})
        return row


def register_version(settings,user,track_id,document,kind,origin='upload',conn=None):
    tenant=user['tenant_id'];store=ArtifactStore(settings.artifact_root)
    with (nullcontext(conn) if conn is not None else transaction(settings.app_dsn,tenant)) as conn:
        track=get_track(conn,tenant,track_id,True)
        if kind=='synthetic_snapshot' and track['source_id']:raise Conflict('Keep synthetic scenarios separate from an official-source track')
        previous=conn.execute('SELECT * FROM regulatory_versions WHERE tenant_id=%s AND track_id=%s ORDER BY sequence DESC LIMIT 1',(tenant,track_id)).fetchone()
        if previous['document']['sha256']==document['sha256']:return previous
        pending=conn.execute("SELECT 1 FROM regulatory_versions WHERE tenant_id=%s AND track_id=%s AND status='processing'",(tenant,track_id)).fetchone()
        if pending:raise Conflict('A version is still processing; complete or retry its extraction first')
        baseline=ex.result(settings,tenant,track['current_extraction_id'])
        old_doc=next(d for d in baseline['inputs']['documents'] if d['role']=='regulation')
        old_text=store.read(old_doc['text_sha256']).decode();new_text=store.read(document['text_sha256']).decode()
        same=normalized(old_text)==normalized(new_text)
        version_id=str(uuid.uuid4());extraction_id=None
        if kind=='amendment':status='needs_review'
        elif same:status='formatting_only';extraction_id=track['current_extraction_id']
        else:
            inputs=baseline['inputs'];docs=[document if d['role']=='regulation' else d for d in inputs['documents']]
            job=ex.enqueue(settings,user,docs,ex.Profile.model_validate(inputs['profile']),inputs['evidence'],inputs['synthetic'],conn=conn,revision_id=version_id)
            extraction_id=job['id'];status='processing'
        row=conn.execute('''INSERT INTO regulatory_versions(tenant_id,id,track_id,sequence,previous_extraction_id,extraction_id,document,kind,status,origin)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
          (tenant,version_id,track_id,previous['sequence']+1,track['current_extraction_id'],extraction_id,Jsonb(document),kind,status,origin)).fetchone()
        audit(conn,tenant,user['id'],'regulation.version_received',version_id,{'track_id':track_id,'kind':kind,'status':status,'origin':origin})
        if status!='processing':
            report={'changes':[],'unchanged':[],'scope_expansion':False,'impact_all':kind=='amendment',
              'reason':'Amendment retained separately; a consolidated full snapshot is required before reassessment.' if kind=='amendment' else 'Only whitespace/layout changed; no obligation analysis or staleness triggered.',
              'requires_review':kind=='amendment','status':status,'previous_extraction_id':track['current_extraction_id'],'new_extraction_id':extraction_id}
            conn.execute('INSERT INTO regulatory_change_reports(tenant_id,version_id,payload) VALUES (%s,%s,%s)',(tenant,version_id,Jsonb(report)))
            if kind=='amendment':apply_impact(settings,conn,user,row,report)
        return row


def affected(conn,tenant,previous_job,report):
    # Start from actual persisted obligations, then walk reverse dependencies.
    ids=['D-'+previous_job['id'][:12]+'-'+c['before']['id'] for c in report['changes'] if c.get('before')]
    if report.get('impact_all') or report.get('scope_expansion'):
        roots=conn.execute("SELECT id FROM entity_versions WHERE tenant_id=%s AND kind='assessment' AND id=ANY(%s)",
                           (tenant,previous_job['output_ids'])).fetchall()
        # A changed obligation set invalidates the completeness of this source-level run.
        assessment_ids=[r['id'] for r in roots]
        controls=conn.execute('''SELECT DISTINCT d.input_id AS id FROM dependencies d JOIN entity_versions e
          ON e.tenant_id=d.tenant_id AND e.id=d.dependent_id WHERE d.tenant_id=%s AND d.relation='maps_control'
          AND e.id LIKE %s''',(tenant,'D-'+previous_job['id'][:12]+'-%')).fetchall()
        return assessment_ids,[r['id'] for r in controls]
    descendants=conn.execute('''WITH RECURSIVE affected(id) AS (
      SELECT id FROM entity_versions WHERE tenant_id=%s AND id=ANY(%s)
      UNION SELECT d.dependent_id FROM dependencies d JOIN affected a ON a.id=d.input_id WHERE d.tenant_id=%s)
      SELECT e.id,e.kind FROM entity_versions e JOIN affected a ON a.id=e.id WHERE e.tenant_id=%s''',(tenant,ids,tenant,tenant)).fetchall()
    assessments=[r['id'] for r in descendants if r['kind']=='assessment']
    mappings=[r['id'] for r in descendants if r['kind']=='mapping']
    controls=conn.execute("SELECT DISTINCT input_id AS id FROM dependencies WHERE tenant_id=%s AND dependent_id=ANY(%s) AND relation='maps_control'",(tenant,mappings)).fetchall()
    return assessments,[r['id'] for r in controls]


def publication_lock(conn,tenant,extraction_id):
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant+':source-assessments:'+extraction_id,))


def apply_impact(settings,conn,user,version,report,only_job=None):
    tenant=user['tenant_id']
    lineage=conn.execute('SELECT DISTINCT extraction_id FROM regulatory_versions WHERE tenant_id=%s AND track_id=%s AND sequence<%s AND extraction_id IS NOT NULL',
                         (tenant,version['track_id'],version['sequence'])).fetchall()
    source_ids=sorted({r['extraction_id'] for r in lineage}|{version['previous_extraction_id']})
    if only_job:
        jobs=conn.execute("SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s AND kind='assess_documents'",(tenant,only_job)).fetchall()
    else:
        for source_id in source_ids:publication_lock(conn,tenant,source_id)
        jobs=conn.execute("SELECT * FROM workflow_jobs WHERE tenant_id=%s AND kind='assess_documents' AND input_payload->>'extraction_job_id'=ANY(%s)",(tenant,source_ids)).fetchall()
    for previous in jobs:
        impact_report=report
        source_id=previous['input_payload']['extraction_job_id']
        if source_id!=version['previous_extraction_id'] and not report.get('impact_all') and not report.get('scope_expansion'):
            # Unchanged older duties can remain current across revisions. Match their
            # semantic fields to the changed duty before traversing their actual graph.
            saved=conn.execute('SELECT payload FROM extraction_steps WHERE tenant_id=%s AND job_id=%s',(tenant,source_id)).fetchall()
            old_items=[i for row in saved for i in row['payload']['items'] if i['kind']=='obligation']
            changed_signatures={signature(c['before']) for c in report['changes'] if c.get('before')}
            impact_report=report|{'changes':[{'before':i} for i in old_items if signature(i) in changed_signatures]}
        assessments,controls=affected(conn,tenant,previous,impact_report)
        needs_run=bool(assessments) or bool(report.get('scope_expansion'))
        # Queued/running old work has no published graph yet; publication invokes this again.
        for id in assessments:
            created=conn.execute('''INSERT INTO assessment_freshness_events(tenant_id,assessment_id,version_id,reason)
              VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING assessment_id''',
              (tenant,id,version['id'],report.get('reason') or 'The source obligation or obligation set changed; reassessment is required.')).fetchone()
            if created:
                from .remediation import reopen_for_source
                reopen_for_source(conn,tenant,id)
                audit(conn,tenant,'worker','assessment.stale',id,{'version_id':version['id'],'control_ids':controls})
        if needs_run and report.get('new_obligation_count',1)>0 and version['extraction_id'] and version['extraction_id']!=version['previous_extraction_id']:
            context=previous['input_payload']['evidence_context']
            next_job=ap.enqueue(settings,user,version['extraction_id'],ap.EvidenceContext.model_validate(context) if context else None,conn=conn)
            conn.execute('''INSERT INTO reassessment_links(tenant_id,version_id,previous_job_id,new_job_id)
              VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(tenant,version['id'],previous['id'],next_job['id']))


def advance(settings,tenant):
    """Reconcile ready comparisons and late assessment publications, at most three versions/tick."""
    with transaction(settings.app_dsn,tenant) as conn:
        versions=conn.execute("SELECT v.* FROM regulatory_versions v JOIN workflow_jobs j ON j.tenant_id=v.tenant_id AND j.id=v.extraction_id WHERE v.tenant_id=%s AND v.status='processing' AND j.status='completed' ORDER BY v.created_at LIMIT 3",(tenant,)).fetchall()
    for version in versions:
        old=ex.result(settings,tenant,version['previous_extraction_id']);new=ex.result(settings,tenant,version['extraction_id'])
        report=compare(old['items'],new['items'])
        report.update({'new_obligation_count':sum(i['kind']=='obligation' for i in new['items']),'previous_extraction_id':version['previous_extraction_id'],'new_extraction_id':version['extraction_id'],
                       'status':'compared' if report['changes'] else 'needs_review','impact_all':not bool(report['changes'])})
        if not report['changes']:report['reason']='Source text changed but extracted duties did not. Review extraction completeness before relying on the old assessment.'
        with transaction(settings.app_dsn,tenant) as conn:
            track=get_track(conn,tenant,version['track_id'],True)
            current=conn.execute('SELECT * FROM regulatory_versions WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,version['id'])).fetchone()
            if current['status']!='processing':continue
            conn.execute('INSERT INTO regulatory_change_reports(tenant_id,version_id,payload) VALUES (%s,%s,%s)',(tenant,version['id'],Jsonb(report)))
            apply_impact(settings,conn,{'tenant_id':tenant,'id':'worker'},version,report)
            conn.execute('UPDATE regulatory_versions SET status=%s WHERE tenant_id=%s AND id=%s',(report['status'],tenant,version['id']))
            conn.execute('UPDATE regulatory_tracks SET current_extraction_id=%s WHERE tenant_id=%s AND id=%s',(version['extraction_id'],tenant,track['id']))
            audit(conn,tenant,'worker','regulation.compared',version['id'],{'change_count':len(report['changes']),'status':report['status']})


def on_publish(settings,conn,job):
    if job['kind']!='assess_documents':return
    # Reconcile old-version jobs that finished after a comparison. No race can restore currentness.
    reports=conn.execute('''SELECT v.*,r.payload AS report FROM regulatory_versions v JOIN regulatory_change_reports r
      ON r.tenant_id=v.tenant_id AND r.version_id=v.id WHERE v.tenant_id=%s
      AND v.status IN ('compared','needs_review') AND v.extraction_id IS DISTINCT FROM %s
      AND EXISTS(SELECT 1 FROM regulatory_versions h WHERE h.tenant_id=v.tenant_id AND h.track_id=v.track_id
        AND h.sequence<v.sequence AND h.extraction_id=%s)''',
      (job['tenant_id'],job['input_payload']['extraction_job_id'],job['input_payload']['extraction_job_id'])).fetchall()
    for version in reports:apply_impact(settings,conn,{'tenant_id':job['tenant_id'],'id':'worker'},version,version['report'],only_job=job['id'])


def schedule(settings,tenant):
    with transaction(settings.app_dsn,tenant) as conn:
        rows=conn.execute('''SELECT * FROM regulatory_tracks WHERE tenant_id=%s AND monitor_enabled AND next_check_at<=now()
          ORDER BY next_check_at FOR UPDATE SKIP LOCKED LIMIT 1''',(tenant,)).fetchall()
        for row in rows:
            if conn.execute("SELECT 1 FROM workflow_jobs WHERE tenant_id=%s AND kind='monitor_regulation' AND input_payload->>'track_id'=%s AND status IN ('queued','running')",(tenant,row['id'])).fetchone():continue
            payload={'track_id':row['id'],'source_id':row['source_id'],'scheduled_for':row['next_check_at'].isoformat()}
            h=ex.digest(ex.canonical(payload));id=str(uuid.uuid4())
            conn.execute('''INSERT INTO workflow_jobs(tenant_id,id,idempotency_key,kind,status,input_hash,configuration_hash,input_payload,requested_by)
              VALUES (%s,%s,%s,'monitor_regulation','queued',%s,%s,%s,'scheduler') ON CONFLICT DO NOTHING''',
              (tenant,id,'monitor:'+h,h,ex.digest(b'official-source-monitor-1'),Jsonb(payload)))
            conn.execute("UPDATE regulatory_tracks SET next_check_at=now()+(interval_seconds * interval '1 second') WHERE tenant_id=%s AND id=%s",(tenant,row['id']))
            audit(conn,tenant,'scheduler','monitor.scheduled',id,{'track_id':row['id']})


def download(source):
    url=source['url'];parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='rbidocs.rbi.org.in':raise ex.ExtractionError('monitor_source_not_allowed')
    started=time.monotonic();content=bytearray()
    try:
        with httpx.Client(timeout=httpx.Timeout(15,connect=10),follow_redirects=False) as client:
            with client.stream('GET',url,headers={'Accept':'application/pdf','User-Agent':'RegTrace-Local-Research/1.0'}) as response:
                if response.status_code!=200:raise ex.ExtractionError('monitor_http_'+str(response.status_code))
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content)>ex.MAX_BYTES:raise ex.ExtractionError('monitor_download_too_large')
                    if time.monotonic()-started>60:raise ex.ExtractionError('monitor_download_timeout')
    except httpx.HTTPError:raise ex.ExtractionError('monitor_connection_failed') from None
    if not content.startswith(b'%PDF'):raise ex.ExtractionError('monitor_response_not_pdf')
    return bytes(content)


def run_monitor(settings,job):
    if ex.digest(ex.canonical(job['input_payload']))!=job['input_hash']:raise ex.ExtractionError('monitor_input_hash_changed')
    tenant=job['tenant_id'];track_id=job['input_payload']['track_id']
    with transaction(settings.app_dsn,tenant) as conn:
        if not ex.fenced(conn,job):return
        track=get_track(conn,tenant,track_id)
        if not track['monitor_enabled']:
            conn.execute("UPDATE workflow_jobs SET status='cancelled',lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND id=%s",(tenant,job['id']));return
        conn.execute("UPDATE workflow_jobs SET lease_expires_at=now()+interval '180 seconds' WHERE tenant_id=%s AND id=%s",(tenant,job['id']))
    source=next((s for s in available_sources() if s['id']==track['source_id']),None)
    if not source:raise ex.ExtractionError('monitor_source_not_allowed')
    raw=download(source)
    document=ex.document(raw,source['id']+'.pdf','regulation',ArtifactStore(settings.artifact_root))
    with transaction(settings.app_dsn,tenant) as conn:
        if not ex.fenced(conn,job):return
        track=get_track(conn,tenant,track_id,True)
        if not track['monitor_enabled']:
            conn.execute("UPDATE workflow_jobs SET status='cancelled',lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=%s AND id=%s",(tenant,job['id']));return
        version=register_version(settings,{'tenant_id':tenant,'id':'monitor'},track_id,document,'full_snapshot','monitor',conn)
        conn.execute("UPDATE workflow_jobs SET status='completed',lease_token=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",(tenant,job['id']))
        audit(conn,tenant,'monitor','monitor.checked',job['id'],{'track_id':track_id,'version_id':version['id'],'sha256':document['sha256']})


def detail(settings,tenant,id):
    with transaction(settings.app_dsn,tenant) as conn:
        track=get_track(conn,tenant,id)
        versions=conn.execute('''SELECT v.*,r.payload AS report,j.status AS extraction_status,j.error_code AS extraction_error
          FROM regulatory_versions v LEFT JOIN regulatory_change_reports r ON r.tenant_id=v.tenant_id AND r.version_id=v.id
          LEFT JOIN workflow_jobs j ON j.tenant_id=v.tenant_id AND j.id=v.extraction_id
          WHERE v.tenant_id=%s AND v.track_id=%s ORDER BY sequence DESC''',(tenant,id)).fetchall()
        for version in versions:
            version['impacts']=conn.execute('SELECT assessment_id,reason FROM assessment_freshness_events WHERE tenant_id=%s AND version_id=%s',(tenant,version['id'])).fetchall()
            version['reassessments']=conn.execute('''SELECT l.previous_job_id,l.new_job_id,j.status,j.error_code,j.output_ids FROM reassessment_links l
              JOIN workflow_jobs j ON j.tenant_id=l.tenant_id AND j.id=l.new_job_id WHERE l.tenant_id=%s AND l.version_id=%s''',(tenant,version['id'])).fetchall()
    return {'track':track,'versions':versions}
