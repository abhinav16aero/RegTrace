import hashlib
import secrets
from datetime import datetime,timezone
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pwdlib import PasswordHash
from . import service, extraction, assessment_pipeline, regulatory_changes, remediation, scenarios, cross_regulation, contradictions
from . import presentation
from .settings import ROOT, Settings
from .storage import ArtifactStore, transaction

password_hash=PasswordHash.recommended()
DUMMY_HASH=password_hash.hash('never-a-valid-user-password')

class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username: str=Field(min_length=1,max_length=100)
    password: str=Field(min_length=1,max_length=256)

class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision: Literal['approved','rejected','changes_requested']
    note: str=Field(min_length=1,max_length=2000)
    expected_revision: int=Field(ge=0)


class AssessmentRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    evidence_context: assessment_pipeline.EvidenceContext | None = None


class TrackRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=200)
    extraction_id: str
    source_id: str | None = None
    interval_seconds: int = Field(default=86400,ge=3600,le=604800)
    synthetic: bool = False

class CrossRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=200)
    note:str=Field(min_length=1,max_length=3000)
    left_job_id:str
    right_job_id:str
    left_instrument:str=Field(min_length=1,max_length=200)
    right_instrument:str=Field(min_length=1,max_length=200)
    distinct_confirmed:bool

class MonitorRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: bool
    interval_seconds: int = Field(ge=3600,le=604800)
    check_now: bool = False


def create_app(settings=None):
    settings=settings or Settings.load()
    app=FastAPI(title='RegTrace',docs_url=None,redoc_url=None,openapi_url=None)
    app.state.settings=settings

    @app.exception_handler(service.Missing)
    async def missing(_request,error):return JSONResponse({'detail':str(error)},status_code=404)

    @app.exception_handler(service.Conflict)
    async def conflict(_request,error):return JSONResponse({'detail':str(error)},status_code=409)

    @app.middleware('http')
    async def security_headers(request,call_next):
        if request.url.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('origin')!=settings.origin:
                return JSONResponse({'detail':'Same-origin request required'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):response.headers['Cache-Control']='no-store'
        return response

    def principal(request:Request):
        token=request.cookies.get('regtrace_session')
        if not token:raise HTTPException(401,'Please sign in')
        digest=hashlib.sha256(token.encode()).hexdigest()
        with transaction(settings.auth_dsn) as conn:
            user=conn.execute('''SELECT u.id,u.tenant_id,u.username,u.role,u.display_name,s.csrf_token FROM sessions s
              JOIN app_users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at>now() AND NOT u.disabled''',(digest,)).fetchone()
        if not user:raise HTTPException(401,'Session expired; please sign in')
        if request.method not in ('GET','HEAD'):
            if not secrets.compare_digest(request.headers.get('x-csrf-token',''),user['csrf_token']):
                raise HTTPException(403,'Invalid request token')
        return user

    @app.get('/api/health')
    def health():
        with transaction(settings.app_dsn) as conn:conn.execute('SELECT 1')
        return {'status':'ok','mode':'documents_and_fixture'}

    @app.post('/api/login')
    def login(body:Login,response:Response):
        valid=False
        with transaction(settings.auth_dsn) as conn:
            user=conn.execute('SELECT * FROM app_users WHERE username=%s FOR UPDATE',(body.username,)).fetchone()
            correct=password_hash.verify(body.password,user['password_hash'] if user else DUMMY_HASH)
            locked=user and user['locked_until'] and user['locked_until']>datetime.now(timezone.utc)
            if user and not locked and not user['disabled'] and correct:
                valid=True;token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
                conn.execute('UPDATE app_users SET failed_attempts=0,locked_until=NULL WHERE id=%s',(user['id'],))
                conn.execute("INSERT INTO sessions VALUES (%s,%s,%s,now()+interval '8 hours')",(hashlib.sha256(token.encode()).hexdigest(),user['id'],csrf))
            elif user and not locked:
                conn.execute("""UPDATE app_users SET failed_attempts=CASE WHEN locked_until<=now() THEN 1 ELSE failed_attempts+1 END,
                  locked_until=CASE WHEN locked_until<=now() THEN NULL WHEN failed_attempts>=4 THEN now()+interval '1 minute' ELSE NULL END
                  WHERE id=%s""",(user['id'],))
        if not valid:raise HTTPException(401,'Invalid credentials or temporarily locked account')
        response.set_cookie('regtrace_session',token,max_age=28800,httponly=True,secure=settings.secure_cookie,samesite='strict',path='/')
        return {k:user[k] for k in ('id','tenant_id','username','role','display_name')}|{'csrf_token':csrf}

    @app.get('/api/me')
    def me(user=Depends(principal)):return user

    @app.post('/api/logout')
    def logout(request:Request,response:Response,user=Depends(principal)):
        digest=hashlib.sha256(request.cookies['regtrace_session'].encode()).hexdigest()
        with transaction(settings.auth_dsn) as conn:conn.execute('DELETE FROM sessions WHERE token_hash=%s',(digest,))
        response.delete_cookie('regtrace_session',path='/');return {'signed_out':True}

    @app.get('/api/presentation')
    def presentation_catalogue(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            result=presentation.catalogue(conn,user['tenant_id'])
        with transaction(settings.auth_dsn) as conn:
            result['people']={r['id']:r['display_name'] or r['username'] for r in conn.execute(
                'SELECT id,username,display_name FROM app_users WHERE tenant_id=%s AND NOT disabled',(user['tenant_id'],))}
        return result

    class Rename(BaseModel):
        title:str=Field(min_length=1,max_length=240)

    @app.get('/api/presentation/{id}/original')
    def original_source(id:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            row=conn.execute('SELECT provenance FROM presentation_records WHERE tenant_id=%s AND id=%s',(user['tenant_id'],id)).fetchone()
        if not row or not row['provenance'].get('original_sha256'):raise HTTPException(404,'Original source not available')
        try:raw=ArtifactStore(settings.artifact_root).read(row['provenance']['original_sha256'])
        except (OSError,ValueError):raise HTTPException(409,'Original source integrity check failed') from None
        return Response(raw,media_type='application/pdf',headers={'Content-Disposition':'inline; filename="official-source.pdf"'})

    class Rehearsal(BaseModel):
        case:Literal['operations','official']='operations'

    @app.post('/api/demo/rehearsals',status_code=202)
    def start_rehearsal(body:Rehearsal,user=Depends(principal)):
        import json,uuid
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            workspace=conn.execute('SELECT is_demo FROM presentation_workspaces WHERE tenant_id=%s',(user['tenant_id'],)).fetchone()
        if not workspace or not workspace['is_demo']:raise HTTPException(404,'Demo pack not available')
        root=ROOT/'data/presentation';manifest=json.loads((root/'manifest.json').read_text())
        def read(name):
            raw=(root/name).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=manifest['files'][name]:raise HTTPException(409,'Demo input integrity check failed')
            return raw
        reg,policy=('simulation-operational-standard.txt','meridian-operational-policy.txt') if body.case=='operations' else ('rbi-customer-identification-excerpt.txt','meridian-kyc-policy-gap.txt')
        store=ArtifactStore(settings.artifact_root)
        docs=[extraction.document(read(reg),reg,'regulation',store),extraction.document(read(policy),policy,'policy',store)]
        ev=extraction.evidence(read('onboarding-original.csv'),'onboarding-original.csv',store) if body.case=='operations' else None
        return extraction.enqueue(settings,user,docs,extraction.Profile.model_validate_json(read('profile.json')),ev,True,presentation_run_id=str(uuid.uuid4()))

    @app.get('/api/demo/files/{filename}')
    def demo_file(filename:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            workspace=conn.execute('SELECT is_demo FROM presentation_workspaces WHERE tenant_id=%s',(user['tenant_id'],)).fetchone()
        if not workspace or not workspace['is_demo']:raise HTTPException(404,'Demo pack not available')
        import json
        root=ROOT/'data/presentation'
        manifest=json.loads((root/'manifest.json').read_text())
        if filename not in manifest['files']:raise HTTPException(404,'File not found')
        raw=(root/filename).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=manifest['files'][filename]:raise HTTPException(409,'Demo file integrity check failed')
        return Response(raw,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="'+filename+'"'})

    @app.post('/api/presentation/{id}/rename')
    def rename_record(id:str,body:Rename,user=Depends(principal)):
        if not body.title.strip():raise HTTPException(422,'A name is required')
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return presentation.rename(conn,user,id,body.title.strip())

    @app.get('/api/assessments')
    def assessments(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            rows=conn.execute("SELECT id FROM entity_versions WHERE tenant_id=%s AND kind='assessment' ORDER BY id",(user['tenant_id'],)).fetchall()
            return [service.assessment(conn,user['tenant_id'],r['id']) for r in rows]

    @app.get('/api/assessments/{id}')
    def detail(id:str,user=Depends(principal)):
        tenant=user['tenant_id']
        with transaction(settings.app_dsn,tenant) as conn:
            result=service.assessment(conn,tenant,id)
            rows=conn.execute('''WITH RECURSIVE inputs(id) AS (
              SELECT id FROM entity_versions WHERE tenant_id=%s AND id=%s
              UNION SELECT d.input_id FROM dependencies d JOIN inputs i ON d.dependent_id=i.id WHERE d.tenant_id=%s)
              SELECT e.payload FROM entity_versions e JOIN inputs i ON e.id=i.id WHERE e.tenant_id=%s ORDER BY e.kind,e.id''',(tenant,id,tenant,tenant)).fetchall()
            ids=[r['payload']['id'] for r in rows]
            deps=conn.execute('SELECT dependent_id,input_id,relation FROM dependencies WHERE tenant_id=%s AND dependent_id=ANY(%s) AND input_id=ANY(%s)',(tenant,ids,ids)).fetchall()
            followups=conn.execute("""SELECT payload FROM entity_versions WHERE tenant_id=%s AND
              ((kind='gap' AND payload->>'assessment_id'=%s) OR (kind='remediation' AND payload->>'gap_id' IN
                (SELECT id FROM entity_versions WHERE tenant_id=%s AND kind='gap' AND payload->>'assessment_id'=%s))) ORDER BY kind,id""",(tenant,id,tenant,id)).fetchall()
            run=conn.execute("SELECT id,input_payload FROM workflow_jobs WHERE tenant_id=%s AND kind='assess_documents' AND output_ids ? %s ORDER BY created_at DESC LIMIT 1",(tenant,id)).fetchone()
            context=None
            if run:
                calls=conn.execute('SELECT step,metadata FROM model_calls WHERE tenant_id=%s AND job_id=%s ORDER BY created_at',(tenant,run['id'])).fetchall()
                context={'job_id':run['id'],'extraction_job_id':run['input_payload']['extraction_job_id'],
                         'evidence_context':run['input_payload']['evidence_context'],'model_calls':calls}
            return result|{'provenance':[r['payload'] for r in rows],'dependencies':deps,
                           'followups':[r['payload'] for r in followups],'run_context':context,
                           'linked_remediations':[{'id':r['payload']['id'],'state':remediation.detail(conn,tenant,r['payload']['id'])['state']} for r in followups if r['payload']['kind']=='remediation']}

    @app.post('/api/assessments/{id}/reviews')
    def review(id:str,body:Review,user=Depends(principal)):
        if user['role']!='reviewer':raise HTTPException(403,'Reviewer role required')
        if not body.note.strip():raise HTTPException(422,'Review note is required')
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return service.review(conn,user['tenant_id'],user['id'],id,body.decision,body.note.strip(),body.expected_revision)

    @app.get('/api/entities/{id}/artifact',response_class=PlainTextResponse)
    def artifact(id:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            data=service.entity(conn,user['tenant_id'],id)
        if data['kind'] not in ('source','evidence'):raise HTTPException(404,'Artifact not found')
        try:content=ArtifactStore(settings.artifact_root).read(data.get('text_sha256',data['sha256']))
        except (OSError,ValueError):raise HTTPException(409,'Artifact missing or hash verification failed')
        return PlainTextResponse(content,headers={'Content-Disposition':'inline'})

    @app.post('/api/jobs',status_code=202)
    def enqueue(user=Depends(principal)):
        bundle,input_hash,config_hash=service.fixture_bundle(settings,user['tenant_id'])
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return service.enqueue(conn,user['tenant_id'],user['id'],bundle,input_hash,config_hash)

    @app.get('/api/jobs')
    def jobs(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return [service.public_job(j) for j in conn.execute("SELECT j.* FROM workflow_jobs j WHERE j.tenant_id=%s AND j.kind!='assess_scenario' AND NOT EXISTS(SELECT 1 FROM scenarios s WHERE s.tenant_id=j.tenant_id AND s.extraction_job_id=j.id) ORDER BY j.created_at DESC LIMIT 100",(user['tenant_id'],)).fetchall()]

    @app.get('/api/jobs/{id}')
    def job_detail(id:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            job=conn.execute('SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s',(user['tenant_id'],id)).fetchone()
            if not job:raise service.Missing('Job not found')
            calls=conn.execute('SELECT step,metadata,created_at FROM model_calls WHERE tenant_id=%s AND job_id=%s ORDER BY created_at',(user['tenant_id'],id)).fetchall()
            return {'job':service.public_job(job),'model_calls':calls}

    @app.post('/api/jobs/{id}/cancel')
    def cancel(id:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return service.cancel(conn,user['tenant_id'],user['id'],id)

    @app.post('/api/jobs/{id}/retry')
    def retry(id:str,user=Depends(principal)):
        tenant=user['tenant_id']
        with transaction(settings.app_dsn,tenant) as conn:
            remediation.lock_for_job(conn,tenant,id)
            job=conn.execute('SELECT * FROM workflow_jobs WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,id)).fetchone()
            if not job:raise service.Missing('Job not found')
            if job['status'] not in ('failed','cancelled'):raise service.Conflict('Only failed or cancelled jobs can be retried')
            remediation.before_retry(conn,user,job)
            conn.execute("UPDATE workflow_jobs SET status='queued',attempts=0,cancel_requested=false,error_code=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=now() WHERE tenant_id=%s AND id=%s",(tenant,id))
            service.audit(conn,tenant,user['id'],'job.retried',id,{'previous_status':job['status']})
        return {'id':id,'status':'queued'}

    @app.get('/api/model')
    def model(user=Depends(principal)):
        return extraction.provider_status()

    @app.post('/api/extractions',status_code=202)
    async def submit_documents(request:Request,user=Depends(principal)):
        # Limits apply while multipart files are spooled as well as when parsed.
        size=request.headers.get('content-length')
        if size and int(size)>26*1024*1024:raise HTTPException(413,'Upload exceeds 26 MB')
        async with request.form(max_files=3,max_fields=3,max_part_size=64*1024) as form:
            store=ArtifactStore(settings.artifact_root)
            async def read_file(name,required=True):
                file=form.get(name)
                if file is None or not getattr(file,'filename',None):
                    if required:raise HTTPException(422,name+' file is required')
                    return None
                raw=await file.read(extraction.MAX_BYTES+1)
                if len(raw)>extraction.MAX_BYTES:raise HTTPException(413,name+' exceeds 8 MB')
                return raw,file.filename
            try:
                profile=extraction.Profile.model_validate_json(str(form.get('profile','')))
                if len(str(form.get('profile','')))>16000:raise ValueError()
                synthetic=str(form.get('synthetic','true'))
                if synthetic not in ('true','false'):raise ValueError()
                reg=await read_file('regulation');policy=await read_file('policy')
                attached=await read_file('evidence',False)
                # PDF parsing is CPU work; keep it off the ASGI event loop.
                from starlette.concurrency import run_in_threadpool
                docs=[await run_in_threadpool(extraction.document,*reg,'regulation',store),
                      await run_in_threadpool(extraction.document,*policy,'policy',store)]
                ev=extraction.evidence(*attached,store) if attached else None
                return extraction.enqueue(settings,user,docs,profile,ev,synthetic=='true')
            except extraction.ExtractionError as error:raise HTTPException(422,str(error)) from None
            except ValueError:raise HTTPException(422,'Bank profile or input format is invalid') from None

    @app.post('/api/extractions/{id}/assess',status_code=202)
    def assess_documents(id:str,body:AssessmentRequest,user=Depends(principal)):
        return assessment_pipeline.enqueue(settings,user,id,body.evidence_context)

    @app.get('/api/extractions/{id}')
    def extracted(id:str,user=Depends(principal)):
        return extraction.result(settings,user['tenant_id'],id)

    @app.get('/api/extractions/{id}/evidence',response_class=PlainTextResponse)
    def extraction_evidence(id:str,user=Depends(principal)):
        result=extraction.result(settings,user['tenant_id'],id)
        attached=result['inputs'].get('evidence')
        if not attached:raise HTTPException(404,'No evidence attached')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(attached['sha256']),media_type='text/csv')
        except (OSError,ValueError):raise HTTPException(409,'Evidence integrity check failed') from None

    @app.get('/api/extractions/{id}/sources/{document_id}',response_class=PlainTextResponse)
    def extracted_source(id:str,document_id:str,user=Depends(principal)):
        result=extraction.result(settings,user['tenant_id'],id)
        doc=next((d for d in result['inputs']['documents'] if d['id']==document_id),None)
        if not doc:raise HTTPException(404,'Document not found')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(doc['text_sha256']))
        except (OSError,ValueError):raise HTTPException(409,'Source integrity check failed') from None

    @app.get('/api/regulatory-sources')
    def regulatory_sources(user=Depends(principal)):
        return regulatory_changes.available_sources()

    @app.post('/api/regulatory-tracks',status_code=201)
    def create_track(body:TrackRequest,user=Depends(principal)):
        return regulatory_changes.create_track(settings,user,body.name,body.extraction_id,body.source_id,body.interval_seconds,body.synthetic)

    @app.get('/api/regulatory-tracks')
    def tracks(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return conn.execute('SELECT * FROM regulatory_tracks WHERE tenant_id=%s ORDER BY created_at DESC',(user['tenant_id'],)).fetchall()

    @app.get('/api/regulatory-tracks/{id}')
    def track_detail(id:str,user=Depends(principal)):
        return regulatory_changes.detail(settings,user['tenant_id'],id)

    @app.post('/api/regulatory-tracks/{id}/monitor')
    def configure_monitor(id:str,body:MonitorRequest,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            track=regulatory_changes.get_track(conn,user['tenant_id'],id,True)
            if body.enabled and not track['source_id']:raise HTTPException(409,'This track has no verified source association')
            row=conn.execute('''UPDATE regulatory_tracks SET monitor_enabled=%s,interval_seconds=%s,
              next_check_at=CASE WHEN %s THEN now() ELSE next_check_at END WHERE tenant_id=%s AND id=%s RETURNING *''',
              (body.enabled,body.interval_seconds,body.check_now,user['tenant_id'],id)).fetchone()
            service.audit(conn,user['tenant_id'],user['id'],'monitor.configured',id,body.model_dump())
            return row

    @app.post('/api/regulatory-tracks/{id}/versions',status_code=202)
    async def submit_version(id:str,request:Request,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:regulatory_changes.get_track(conn,user['tenant_id'],id)
        async with request.form(max_files=1,max_fields=1,max_part_size=1024) as form:
            file=form.get('regulation');kind=str(form.get('kind','full_snapshot'))
            if kind not in ('full_snapshot','synthetic_snapshot','amendment'):raise HTTPException(422,'Choose a full snapshot, synthetic snapshot or amendment')
            if not getattr(file,'filename',None):raise HTTPException(422,'A regulatory file is required')
            raw=await file.read(extraction.MAX_BYTES+1)
            from starlette.concurrency import run_in_threadpool
            try:
                doc=await run_in_threadpool(extraction.document,raw,file.filename,'regulation',ArtifactStore(settings.artifact_root))
                return await run_in_threadpool(regulatory_changes.register_version,settings,user,id,doc,kind)
            except extraction.ExtractionError as error:raise HTTPException(422,str(error)) from None

    @app.get('/api/regulatory-tracks/{id}/versions/{version_id}/source',response_class=PlainTextResponse)
    def version_source(id:str,version_id:str,user=Depends(principal)):
        detail=regulatory_changes.detail(settings,user['tenant_id'],id)
        version=next((v for v in detail['versions'] if v['id']==version_id),None)
        if not version:raise HTTPException(404,'Version not found')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(version['document']['text_sha256']))
        except (OSError,ValueError):raise HTTPException(409,'Source integrity check failed') from None

    @app.post('/api/cross-regulation',status_code=202)
    def cross_create(body:CrossRequest,user=Depends(principal)):
        return cross_regulation.enqueue(settings,user,**body.model_dump())

    @app.get('/api/cross-regulation')
    def cross_list(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return conn.execute("SELECT id,status,error_code,input_payload->>'name' AS name,created_at FROM workflow_jobs WHERE tenant_id=%s AND kind='cross_regulation' ORDER BY created_at DESC",(user['tenant_id'],)).fetchall()

    @app.get('/api/cross-regulation/{id}')
    def cross_detail(id:str,user=Depends(principal)):
        return cross_regulation.detail(settings,user,id)

    @app.post('/api/cross-regulation/{id}/pairs/{pair_id}/reviews')
    def cross_review(id:str,pair_id:str,body:Review,user=Depends(principal)):
        if user['role']!='reviewer':raise HTTPException(403,'Reviewer role required')
        return cross_regulation.review(settings,user,id,pair_id,body.decision,body.note,body.expected_revision)

    @app.get('/api/cross-regulation/{id}/artifacts/{entity_id}',response_class=PlainTextResponse)
    def cross_artifact(id:str,entity_id:str,user=Depends(principal)):
        d=cross_regulation.detail(settings,user,id)
        entity=next((e for s in d['snapshots'] for e in s['entities'] if e['id']==entity_id and e['kind'] in ('source','evidence')),None)
        if not entity:raise service.Missing('Comparison artifact not found')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(entity.get('text_sha256',entity['sha256'])))
        except (OSError,ValueError):raise service.Conflict('Comparison artifact failed integrity verification') from None

    @app.post('/api/contradictions',status_code=202)
    def contradiction_create(body:contradictions.Request,user=Depends(principal)):
        return contradictions.enqueue(settings,user,body)

    @app.get('/api/contradictions')
    def contradiction_list(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return conn.execute("SELECT id,status,error_code,input_payload->>'name' AS name,created_at FROM workflow_jobs WHERE tenant_id=%s AND kind='detect_contradictions' ORDER BY created_at DESC",(user['tenant_id'],)).fetchall()

    @app.get('/api/contradictions/{id}')
    def contradiction_detail(id:str,user=Depends(principal)):
        return contradictions.detail(settings,user,id)

    @app.post('/api/contradictions/{id}/pairs/{pair_id}/reviews')
    def contradiction_review(id:str,pair_id:str,body:Review,user=Depends(principal)):
        if user['role']!='reviewer':raise HTTPException(403,'Reviewer role required')
        return contradictions.review(settings,user,id,pair_id,body.decision,body.note,body.expected_revision)

    @app.get('/api/contradictions/{id}/artifacts/{entity_id}',response_class=PlainTextResponse)
    def contradiction_artifact(id:str,entity_id:str,user=Depends(principal)):
        d=contradictions.detail(settings,user,id)
        entity=next((e for s in d['snapshots'] for e in s['entities'] if e['id']==entity_id and e['kind'] in ('source','evidence')),None)
        if not entity:raise service.Missing('Contradiction artifact not found')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(entity.get('text_sha256',entity['sha256'])))
        except (OSError,ValueError):raise service.Conflict('Contradiction artifact failed integrity verification') from None

    @app.get('/api/scenarios')
    def scenario_list(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return conn.execute("""SELECT s.id,s.name,s.created_at,s.baseline_job_id,
              CASE WHEN r.scenario_id IS NOT NULL THEN 'completed' ELSE COALESCE(a.status,CASE WHEN e.status='completed' THEN 'awaiting_assessment' ELSE e.status END) END AS status
              FROM scenarios s JOIN workflow_jobs e ON e.tenant_id=s.tenant_id AND e.id=s.extraction_job_id
              LEFT JOIN scenario_assessment_links l ON l.tenant_id=s.tenant_id AND l.scenario_id=s.id
              LEFT JOIN workflow_jobs a ON a.tenant_id=l.tenant_id AND a.id=l.job_id
              LEFT JOIN scenario_results r ON r.tenant_id=s.tenant_id AND r.scenario_id=s.id
              WHERE s.tenant_id=%s ORDER BY s.created_at DESC""",(user['tenant_id'],)).fetchall()

    @app.post('/api/scenarios',status_code=202)
    async def create_scenario(request:Request,user=Depends(principal)):
        size=request.headers.get('content-length')
        if size and int(size)>26*1024*1024:raise HTTPException(413,'Upload exceeds 26 MB')
        async with request.form(max_files=3,max_fields=7,max_part_size=16384) as form:
            try:
                name=str(form.get('name',''));note=str(form.get('note',''));job_id=str(form.get('baseline_job_id',''))
                if len(name)>200 or len(note)>3000:raise ValueError()
                profile=extraction.Profile.model_validate_json(str(form['profile'])) if form.get('profile') else None
                context=assessment_pipeline.EvidenceContext.model_validate_json(str(form['context'])) if form.get('context') else None
                clear=str(form.get('clear_evidence','false'))
                if clear not in ('true','false'):raise ValueError()
                uploads={}
                for role in ('regulation','policy','evidence'):
                    file=form.get(role)
                    if getattr(file,'filename',None):uploads[role]=(await file.read(extraction.MAX_BYTES+1),file.filename)
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(scenarios.create,settings,user,job_id,name,note,uploads,profile,context,clear=='true')
            except extraction.ExtractionError as error:raise HTTPException(422,str(error)) from None
            except ValueError:raise HTTPException(422,'Invalid scenario input or metadata') from None

    @app.get('/api/scenarios/{id}')
    def scenario_detail(id:str,user=Depends(principal)):
        return scenarios.detail(settings,user,id)

    @app.get('/api/scenarios/{id}/artifacts/{entity_id}',response_class=PlainTextResponse)
    def scenario_artifact(id:str,entity_id:str,side:Literal['baseline','result']='result',user=Depends(principal)):
        result=scenarios.detail(settings,user,id)
        entities=(result[side] or {}).get('entities',[])
        entity=next((e for e in entities if e['id']==entity_id and e['kind'] in ('source','evidence')),None)
        if not entity:raise service.Missing('Scenario artifact not found')
        try:return PlainTextResponse(ArtifactStore(settings.artifact_root).read(entity.get('text_sha256',entity['sha256'])))
        except (OSError,ValueError):raise service.Conflict('Scenario artifact failed integrity verification') from None

    @app.get('/api/assignees')
    def assignees(user=Depends(principal)):
        with transaction(settings.auth_dsn) as conn:
            return conn.execute('SELECT id,username,role FROM app_users WHERE tenant_id=%s AND NOT disabled ORDER BY username',(user['tenant_id'],)).fetchall()

    @app.get('/api/remediations')
    def remediation_queue(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            rows=conn.execute("SELECT id FROM entity_versions WHERE tenant_id=%s AND kind='remediation' ORDER BY id",(user['tenant_id'],)).fetchall()
            results=[remediation.detail(conn,user['tenant_id'],r['id']) for r in rows]
        return sorted(results,key=lambda r:(r['state']['status']=='closed',r['priority']['score'] is not None,-(r['priority']['score'] or 0),r['remediation']['id']))

    @app.get('/api/remediations/{id}')
    def remediation_detail(id:str,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:return remediation.detail(conn,user['tenant_id'],id)

    @app.post('/api/remediations/{id}/actions')
    def remediation_action(id:str,body:remediation.Action,user=Depends(principal)):
        if body.action in ('accept','close','prioritize') and user['role']!='reviewer':raise HTTPException(403,'Reviewer role required')
        return remediation.act(settings,user,id,body)

    @app.post('/api/remediations/{id}/retests',status_code=202)
    async def retest_remediation(id:str,request:Request,user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:remediation.records(conn,user['tenant_id'],id)
        async with request.form(max_files=1,max_fields=3,max_part_size=16384) as form:
            file=form.get('evidence')
            if not getattr(file,'filename',None):raise HTTPException(422,'Revised CSV evidence is required')
            try:
                context=assessment_pipeline.EvidenceContext.model_validate_json(str(form.get('context','')))
                revision=int(str(form.get('expected_revision','')))
                note=str(form.get('note',''))
                if revision<0 or len(note)>3000:raise ValueError()
                raw=await file.read(extraction.MAX_BYTES+1)
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(remediation.request_retest,settings,user,id,raw,file.filename,context,revision,note)
            except extraction.ExtractionError as error:raise HTTPException(422,str(error)) from None
            except ValueError:raise HTTPException(422,'Invalid evidence context, revision or CSV') from None

    @app.get('/api/audit')
    def events(user=Depends(principal)):
        with transaction(settings.app_dsn,user['tenant_id']) as conn:
            return conn.execute('SELECT * FROM audit_events WHERE tenant_id=%s ORDER BY created_at DESC,id LIMIT 200',(user['tenant_id'],)).fetchall()

    dist=ROOT/'web/dist'
    if dist.exists():app.mount('/',StaticFiles(directory=dist,html=True),name='ui')
    return app
