#!/usr/bin/env python3
"""Resumeable demo seeding through authenticated production workflows, never mock inference."""
import hashlib,json,sys,time,fcntl
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'scripts'))
import httpx
from psycopg.types.json import Jsonb
from regtrace.settings import Settings
from regtrace.storage import transaction,ArtifactStore
from regtrace import presentation,assessment_pipeline,contradictions
from dev import write_private
P=ROOT/'data/presentation';TENANT='meridian-demo';STATE=ROOT/'.local/presentation-seed.json'
lock=(ROOT/'.local/presentation-seed.lock').open('a')
try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:raise SystemExit('Another presentation seed is running; wait for it to finish.')
settings=Settings.load();store=ArtifactStore(settings.artifact_root)
state=json.loads(STATE.read_text()) if STATE.exists() else {'version':'meridian-v1','cases':{}}
assert state['version']=='meridian-v1'
manifest=json.loads((P/'manifest.json').read_text())
for filename,digest in manifest['files'].items():assert hashlib.sha256((P/filename).read_bytes()).hexdigest()==digest,filename
users=json.loads((ROOT/'.local/demo-users.json').read_text())
def client(username):
 u=next(u for u in users if u['username']==username);c=httpx.Client(base_url=settings.origin,headers={'Origin':settings.origin},timeout=120)
 r=c.post('/api/login',json={'username':username,'password':u['password']});r.raise_for_status();c.headers['X-CSRF-Token']=r.json()['csrf_token'];return c
c=client('maya.analyst');reviewer=client('kavya.reviewer');operations=client('arjun.operations')
context=json.loads((P/'evidence-context.json').read_text())
def save():write_private(STATE,json.dumps(state,indent=2)+'\n')
def post(client,path,**kwargs):
 r=client.post('/api'+path,**kwargs)
 if r.status_code>=400:raise RuntimeError(f'{path}: {r.status_code}: {r.text[:500]}')
 return r.json()
def get(path):
 r=c.get('/api'+path);r.raise_for_status();return r.json()
def wait(id):
 last=None;deadline=time.monotonic()+1800
 while time.monotonic()<deadline:
  j=get('/jobs/'+id)['job']
  if j['status']!=last:print(j['kind'],j['status'],flush=True);last=j['status']
  if j['status'] in ('completed','awaiting_review'):return j
  if j['status'] in ('failed','cancelled'):raise RuntimeError('Run stopped: '+str(j['error_code']))
  time.sleep(6)
 raise RuntimeError('Run is still pending; rerun seed to resume')
def curate(id,title,provenance=None):
 with transaction(settings.app_dsn,TENANT) as conn:
  presentation.catalogue(conn,TENANT)
  conn.execute('UPDATE presentation_records SET title=%s,curated=true,provenance=%s WHERE tenant_id=%s AND id=%s',(title,Jsonb(provenance or {}),TENANT,id))
def catalogue():
 cases=[]
 for key,label,description in [('operations','Approval before activation','Inspect a real computed timestamp test on 40 fictional records. Review late and equal timestamps, assign a correction and retest the same procedure.'),('official','Official RBI requirement and policy gap','Read the pinned official paragraph in its original PDF, then inspect a fictional policy that defers identification until after the account relationship begins.'),('independent','Independent simulated instrument','Inspect a separate imaginary post-activation requirement. It exists only to exercise comparison and contradiction workflows.')]:
  s=state['cases'].get(key,{})
  cases.append({'name':label,'description':description,'route':'assessments?record='+s['assessment_id'] if s.get('assessment_id') else 'documents','status':s.get('status','not_prepared')})
 for key,name,desc,page in [('remediation','Correction and reviewer closure','Scripted demo actions are attributed to the seeded personas. The original failed result is preserved.','remediations'),('scenario','What if the evidence changed?','Hypothetical corrected evidence compared with an immutable baseline. Unpaired duties remain explicit.','scenarios'),('cross','Cross-regulation comparison','Compare independent simulated requirements without merging their evidence or test outcomes.','cross'),('contradiction','Conflict, scope and effective dates','A deliberately incompatible simulated pair; inspect actual verifier and period gates before interpreting a candidate.','contradictions'),('track','Version change and affected findings','A complete simulated replacement; inspect source changes, stale findings and linked reassessment.','changes')]:
  value=state.get(key,{})
  cases.append({'name':name,'description':desc,'route':page+('?record='+value['id'] if value.get('id') else ''),'status':value.get('status','not_prepared')})
 cases.append({'name':'Permissions and decision trail','description':'Use Maya for analysis, Arjun for assigned work and Kavya for reviewer decisions. The boundary reviewer belongs to an empty separate tenant.','route':'activity','status':'available'})
 with transaction(settings.app_dsn,TENANT) as conn:
  conn.execute('INSERT INTO presentation_workspaces(tenant_id,name,is_demo,manifest) VALUES (%s,%s,true,%s) ON CONFLICT(tenant_id) DO UPDATE SET manifest=excluded.manifest',(TENANT,'Meridian Demo Bank',Jsonb({'version':state['version'],'cases':cases})))
 save()
def upload(key,label,reg,policy,evidence=None):
 s=state['cases'].setdefault(key,{})
 if not s.get('extraction'):
  files={'regulation':(reg,(P/reg).read_bytes(),'text/plain'),'policy':(policy,(P/policy).read_bytes(),'text/plain')}
  if evidence:files['evidence']=(evidence,(P/evidence).read_bytes(),'text/csv')
  s['extraction']=post(c,'/extractions',files=files,data={'profile':(P/'profile.json').read_text(),'synthetic':'true'})['id'];save()
 wait(s['extraction']);result=get('/extractions/'+s['extraction'])
 curate(s['extraction'],'Extract · '+label)
 for doc in result['inputs']['documents']:
  prov={'label':'Fictional bank policy'} if doc['role']=='policy' else {'label':'Simulated regulation · not law'}
  if key=='official' and doc['role']=='regulation':
   prov=json.loads((P/'official-provenance.json').read_text());raw=(ROOT/'data/sources/rbi-commercial-banks-kyc.pdf').read_bytes();store.put(raw,prov['original_sha256'])
  curate(doc['id'],label if doc['role']=='regulation' else ('Meridian onboarding policy' if key!='official' else 'Meridian customer identification policy'),prov)
 if not s.get('assessment'):
  s['assessment']=post(c,'/extractions/'+s['extraction']+'/assess',json={'evidence_context':context if evidence else None})['id'];save()
 job=wait(s['assessment']);s['status']='computed';s['assessment_id']=job['output_ids'][0];save();curate(s['assessment'],'Assess · '+label)
 # Label source entities individually, retaining immutable original payloads.
 for aid in job['output_ids']:
  detail=get('/assessments/'+aid)
  if key=='operations':curate(aid,'Approval must precede account activation')
  if key=='official':curate(aid,'Identify the customer when the account relationship begins')
  for e in detail['provenance']:
   if e['kind']=='source':
    doc=next((d for d in result['inputs']['documents'] if d['sha256']==e['sha256']),None)
    if doc:
     with transaction(settings.app_dsn,TENANT) as conn:
      record=conn.execute('SELECT title,provenance FROM presentation_records WHERE tenant_id=%s AND id=%s',(TENANT,doc['id'])).fetchone()
     curate(e['id'],record['title'],record['provenance'])
 catalogue();return s
try:
 # A configuration change creates new actual results; earlier findings remain immutable.
 for key,revision,container in [('operations',assessment_pipeline.configuration()['revision'],state['cases']),('contradiction',contradictions.configuration()['revision'],state)]:
  value=container.get(key,{})
  jid=value.get('assessment') if key=='operations' else value.get('id')
  if jid:
   with transaction(settings.app_dsn,TENANT) as conn:
    old=conn.execute('SELECT input_payload FROM workflow_jobs WHERE tenant_id=%s AND id=%s',(TENANT,jid)).fetchone()
   if old and old['input_payload']['configuration']['revision']!=revision:
    container[key+'-previous']=value
    container[key]={'extraction':value['extraction']} if key=='operations' else {}
    save()
 catalogue()
 a=upload('operations','Operational approval timing · simulation','simulation-operational-standard.txt','meridian-operational-policy.txt','onboarding-original.csv')
 # Leave the initial executable failure inspectable; use the same frozen procedure for correction.
 tasks=get('/remediations');task=next((t for t in tasks if t['assessment_id']==a['assessment_id'] and t['gap']['gap_type']=='execution_failure'),None)
 if task and task['retest_available'] and not state.get('remediation',{}).get('status')=='closed':
  rid=task['remediation']['id'];state['remediation']=(state.get('remediation') if state.get('remediation',{}).get('id')==rid else {'id':rid,'status':'preparing'});save()
  def action(who,verb,**extra):
   current=get('/remediations/'+rid)
   return post(who,'/remediations/'+rid+'/actions',json={'action':verb,'expected_revision':current['revision'],'note':'Scripted demonstration setup: '+verb+'. Fictional evidence; not a human gold-standard decision.',**extra})
  if not task['state']['owner_id']:action(c,'assign',owner_id='arjun.operations')
  task=get('/remediations/'+rid)
  if task['state']['status']=='proposed':action(reviewer,'accept')
  task=get('/remediations/'+rid)
  if task['state']['status']=='accepted':action(operations,'start')
  if task['state']['status']!='closed':
   if not state['remediation'].get('retest'):
    state['remediation']['retest']=post(operations,'/remediations/'+rid+'/retests',files={'evidence':('onboarding-corrected.csv',(P/'onboarding-corrected.csv').read_bytes(),'text/csv')},data={'expected_revision':str(get('/remediations/'+rid)['revision']),'context':json.dumps(context),'note':'Scripted demonstration: five fictional approvals corrected; all 40 original cases retained.'})['id'];save()
   wait(state['remediation']['retest']);task=get('/remediations/'+rid)
   if task['state']['latest_retest_outcome']=='pass':action(reviewer,'close',criteria_confirmed=True)
  state['remediation']['status']=get('/remediations/'+rid)['state']['status'];catalogue()
 if task and not task['retest_available']:
  state['remediation']={'id':task['remediation']['id'],'status':'blocked_by_verification','reason':task['retest_unavailable_reason']};catalogue()
 a_cross=upload('approval','Approval timing standard · simulation','simulation-approval-standard-v1.txt','meridian-onboarding-policy-v2.txt','onboarding-original.csv')
 # Advance independent and authentic cases only after the operational demonstration is durable.
 b=upload('independent','Post-activation review standard · simulation','simulation-independent-review-standard.txt','meridian-onboarding-policy-v2.txt','onboarding-original.csv')
 o=upload('official','RBI customer identification · paragraph 21(1)','rbi-customer-identification-excerpt.txt','meridian-kyc-policy-gap.txt')
 if not state.get('cross',{}).get('id'):
  j=post(c,'/cross-regulation',json={'name':'Approval timing across two simulated standards','note':'Distinct fictional instruments, not law. Preserve each independent requirement and sample outcome.','left_job_id':a_cross['assessment'],'right_job_id':b['assessment'],'left_instrument':'Simulated approval-timing standard','right_instrument':'Simulated post-activation review standard','distinct_confirmed':True});state['cross']={'id':j['id'],'status':'queued'};catalogue()
 wait(state['cross']['id']);state['cross']['status']='computed';catalogue()
 if not state.get('contradiction',{}).get('id'):
  j=post(c,'/contradictions',json={'comparison_id':state['cross']['id'],'name':'Incompatible approval timing · simulated example','note':'Teaching example only. Evaluate exact scope and effective dates; a candidate is not a legal resolution.','as_of':'2026-09-15','left_context':(P/'simulation-approval-standard-v1.txt').read_text().strip(),'right_context':(P/'simulation-independent-review-standard.txt').read_text().strip()});state['contradiction']={'id':j['id'],'status':'queued'};catalogue()
 try:
  wait(state['contradiction']['id']);state['contradiction']['status']='computed'
 except RuntimeError as error:
  state['contradiction']['status']='failed';state['contradiction']['reason']=str(error)
 catalogue()
 if not state.get('scenario',{}).get('id'):
  j=post(c,'/scenarios',data={'baseline_job_id':a['assessment'],'name':'What if all approval timestamps preceded activation?','note':'Hypothetical corrected CSV, all 40 fictional cases retained. Baseline remains unchanged; extraction differences may be unpaired.','context':json.dumps(context)},files={'evidence':('onboarding-corrected.csv',(P/'onboarding-corrected.csv').read_bytes(),'text/csv')});state['scenario']={'id':j['id'],'status':'queued'};catalogue()
 deadline=time.monotonic()+1800
 while time.monotonic()<deadline:
  s=get('/scenarios/'+state['scenario']['id'])
  if s.get('result'):break
  if s['status'] in ('failed','cancelled'):raise RuntimeError('Scenario stage stopped; inspect Runs')
  time.sleep(6)
 state['scenario']['status']=s['status'];catalogue()
 # Change last: baseline/cross/scenario keep their prior captured states visible.
 if not state.get('track',{}).get('id'):
  j=post(c,'/regulatory-tracks',json={'name':'Approval timing standard · simulated version history','extraction_id':a_cross['extraction'],'synthetic':True});state['track']={'id':j['id'],'status':'tracked'};catalogue()
 if not state['track'].get('version'):
  v=post(c,'/regulatory-tracks/'+state['track']['id']+'/versions',data={'kind':'synthetic_snapshot'},files={'regulation':('simulation-approval-standard-v2.txt',(P/'simulation-approval-standard-v2.txt').read_bytes(),'text/plain')});state['track']['version']=v['id'];save()
 state['track']['status']='processing';catalogue()
 deadline=time.monotonic()+1800
 while time.monotonic()<deadline:
  tracked=get('/regulatory-tracks/'+state['track']['id']);version=tracked['versions'][0]
  # The API orders versions newest first; find the exact uploaded version explicitly.
  version=next(v for v in tracked['versions'] if v['id']==state['track']['version'])
  if version['status']!='processing':
   for reassessment in version['reassessments']:wait(reassessment['new_job_id'])
   state['track']['status']=version['status'];catalogue();break
  time.sleep(6)
 state.pop('last_error',None);save()
 print('Demo inputs, computed findings and advanced workflows prepared. Inspect the saved results and verification report before presenting.',flush=True)
except Exception as error:
 state['last_error']=str(error);catalogue();print('Preparation paused: '+str(error),flush=True);raise
finally:
 for client_ in [c,reviewer,operations]:client_.close()
