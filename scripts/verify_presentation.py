#!/usr/bin/env python3
"""Read-only verification of actual saved demo results. No outcome substitution."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from regtrace.settings import Settings
from regtrace.storage import ArtifactStore,transaction
from regtrace import service,remediation,scenarios,cross_regulation,contradictions,regulatory_changes
settings=Settings.load();tenant='meridian-demo';state=json.loads((ROOT/'.local/presentation-seed.json').read_text());store=ArtifactStore(settings.artifact_root)
report={'version':state['version'],'tenant':tenant,'classification':'Real workflow execution over labelled fictional bank inputs; one pinned official excerpt; regulatory simulations are not law.','checks':{},'cases':{},'limitations':[]}
with transaction(settings.app_dsn,tenant) as conn:
 for key,value in state['cases'].items():
  if not value.get('assessment_id'):continue
  a=service.assessment(conn,tenant,value['assessment_id'])
  report['cases'][key]={'assessment_id':value['assessment_id'],'states':a['current_states'],'rationale':a['snapshot']['rationale']}
 main=service.assessment(conn,tenant,state['cases']['operations']['assessment_id']);tests=[service.entity(conn,tenant,id) for id in main['snapshot']['test_run_ids']]
 report['checks']['original_sample_has_40_rows_and_5_failures']=len(tests)==1 and tests[0]['sample_size']==40 and len(tests[0]['failed_case_ids'])==5 and tests[0]['outcome']=='fail'
 report['checks']['main_finding_supported_full_coverage']=main['current_states']['verification']=='supported' and main['current_states']['coverage']=='full'
 report['checks']['original_failure_preserved']=main['current_states']['test']=='fail'
 records=conn.execute('SELECT reference FROM presentation_records WHERE tenant_id=%s',(tenant,)).fetchall();report['checks']['unique_persisted_references']=len(records)==len({r['reference'] for r in records})
 provenance=json.loads((ROOT/'data/presentation/official-provenance.json').read_text());raw=store.read(provenance['original_sha256']);report['checks']['official_pdf_hash_verified']=hashlib.sha256(raw).hexdigest()==provenance['original_sha256']
 from pypdf import PdfReader
 import io
 text=PdfReader(io.BytesIO(raw)).pages[int(provenance['original_page'])-1].extract_text();report['checks']['official_excerpt_exact_page_mapping']=text[int(provenance['original_start']):int(provenance['original_end'])]==provenance['quote']
 jobs=conn.execute('SELECT id,kind,status,error_code FROM workflow_jobs WHERE tenant_id=%s ORDER BY created_at',(tenant,)).fetchall();report['jobs']=jobs
# API/service read views retain the same tenant boundaries.
with transaction(settings.app_dsn,tenant) as conn:
 task=remediation.detail(conn,tenant,state['remediation']['id'])
report['remediation']={'status':task['state']['status'],'retest_outcome':task['state']['latest_retest_outcome'],'history':task['history']}
report['checks']['reviewer_closed_after_passing_retest']=task['state']['status']=='closed' and task['state']['latest_retest_outcome']=='pass'
for key,module in [('scenario',scenarios),('cross',cross_regulation),('contradiction',contradictions),('track',regulatory_changes)]:
 if state.get(key,{}).get('id'):
  d=module.detail(settings,tenant if key=='track' else {'tenant_id':tenant,'id':'maya.analyst','role':'analyst'},state[key]['id']);report[key]={'seed_status':state[key]['status'],'has_result':bool(d.get('result')) if key!='track' else any(v.get('report') for v in d['versions'])}
  if key=='contradiction' and d.get('result'):report[key]['findings']=[p['finding'] for p in d['result']['pairs']]
  if key=='track':report[key]['versions']=[{'status':v['status'],'impacts':len(v['impacts']),'reassessments':len(v['reassessments'])} for v in d['versions']]
  if not report[key]['has_result']:report['limitations'].append(key+' has no completed saved result; inspect actual run status.')
with transaction(settings.app_dsn,'meridian-boundary') as conn:report['checks']['boundary_tenant_cannot_read_demo']=conn.execute('SELECT count(*) AS n FROM entity_versions WHERE tenant_id=%s',(tenant,)).fetchone()['n']==0
report['limitations']+=['Pinned official source is not an exhaustive current-law certification.','Comparison, contradiction and version-change examples use explicitly simulated instruments.','Model outputs retain uncertainty and can differ on a new live run.','Official monitoring is not represented by the simulated manual version upload.']
report['passed']=all(report['checks'].values())
out=ROOT/'artifacts/presentation/verification-report.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2,default=str)+'\n')
print(json.dumps({'passed':report['passed'],'checks':report['checks'],'advanced':{k:report.get(k) for k in ['scenario','cross','contradiction','track']},'report':str(out)},indent=2))
if not report['passed']:raise SystemExit(1)
