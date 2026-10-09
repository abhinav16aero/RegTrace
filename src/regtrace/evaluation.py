"""Versioned evaluation inputs and reviewer-adjudicated metrics; no automatic semantic gold."""
import hashlib
import json
import math
import time
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

ROOT=Path(__file__).resolve().parents[2]
PROTOCOL=ROOT/'data/evaluation/protocol.json'

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(value):return hashlib.sha256(value).hexdigest()
def load(path):return json.loads(Path(path).read_text())
def save_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as handle:handle.write(json.dumps(value,indent=2)+'\n')

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
class Input(Strict):
    path:str
    sha256:str=Field(pattern='^[a-f0-9]{64}$')
class Case(Strict):
    id:str=Field(pattern='^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$')
    family_id:str=Field(min_length=1)
    material:Literal['synthetic','real_source_synthetic_bank','real_bank']
    split:Literal['development','heldout']
    critical_categories:list[str]
    inputs:dict[str,Input]
class Manifest(Strict):
    schema_version:Literal['1.0']
    protocol_sha256:str
    frozen_at:str
    cases:list[Case]
class Review(Strict):
    status:Literal['draft','reviewed','ambiguous']
    author:str=Field(min_length=1)
    reviewer:str|None=None
    second_reviewer:str|None=None
    reviewed_at:str|None=None
    note:str
class Gold(Strict):
    id:str
    text:str=Field(min_length=1)
    quote:str=Field(min_length=1)
class Decision(Strict):
    id:str
    obligation_id:str
    applicability:Literal['applicable','not_applicable','unknown']
    coverage:Literal['full','partial','none','unassessed']
    evidence:Literal['sufficient','insufficient','conflicting','unassessed']
    test:Literal['pass','fail','inconclusive','not_run']
    gap_types:list[str]
class Label(Strict):
    case_id:str
    review:Review
    obligations:list[Gold]
    decisions:list[Decision]
class Labels(Strict):
    manifest_hash:str
    cases:list[Label]
class Match(Strict):
    predicted_id:str
    gold_id:str
class Alignment(Strict):
    decision_id:str
    assessment_id:str|None
class Citation(Strict):
    item_id:str
    citation_index:int=Field(ge=0)
    supported:bool
class Adjudication(Strict):
    case_id:str
    record_hash:str
    review:Review
    matches:list[Match]
    alignments:list[Alignment]
    citations:list[Citation]
class Adjudications(Strict):
    manifest_hash:str
    cases:list[Adjudication]

def unique(values,name):
    if len(values)!=len(set(values)):raise ValueError('Duplicate '+name)

def read_manifest(path):
    path=Path(path).resolve();raw=load(path);m=Manifest.model_validate(raw)
    if m.protocol_sha256!=digest(PROTOCOL.read_bytes()):raise ValueError('Frozen protocol hash mismatch')
    unique([c.id for c in m.cases],'case IDs')
    protocol=load(PROTOCOL);families={c.family_id for c in m.cases}
    novel=sorted(families-set(protocol['development_families']),key=lambda f:digest((protocol['protocol_id']+f).encode()))
    heldout=set(novel[:math.ceil(.3*len(novel))])
    files={};seen_inputs={}
    for c in m.cases:
        if c.split!=('heldout' if c.family_id in heldout else 'development'):raise ValueError('Family split violates frozen protocol: '+c.id)
        if not set(c.critical_categories)<=set(protocol['critical_cases']):raise ValueError('Unknown critical category')
        if not {'regulation','policy','profile'}<=c.inputs.keys() or not c.inputs.keys()<={'regulation','policy','profile','evidence','context'}:raise ValueError('Invalid case input roles')
        if 'context' in c.inputs and 'evidence' not in c.inputs:raise ValueError('Context requires evidence')
        files[c.id]={}
        for role,item in c.inputs.items():
            file=(path.parent/item.path).resolve()
            if not file.is_relative_to(ROOT):raise ValueError('Inputs must remain inside the repository')
            raw_file=file.read_bytes()
            if digest(raw_file)!=item.sha256:raise ValueError('Input hash mismatch: '+c.id+'/'+role)
            files[c.id][role]=file
            if role=='regulation':
                previous=seen_inputs.setdefault(item.sha256,(c.family_id,c.split))
                if previous!=(c.family_id,c.split):raise ValueError('Same regulatory content assigned to different families')
    return m,files,digest(canonical(raw))

def reviewed(review,critical=False):
    return (review.status=='reviewed' and bool(review.reviewer and review.reviewed_at and review.note.strip())
            and review.reviewer!=review.author and (not critical or bool(review.second_reviewer and review.second_reviewer not in (review.author,review.reviewer))))

def validate_labels(m,files,manifest_hash,path):
    labels=Labels.model_validate(load(path))
    if labels.manifest_hash!=manifest_hash:raise ValueError('Labels belong to a different manifest')
    unique([l.case_id for l in labels.cases],'label case IDs');cases={c.id:c for c in m.cases}
    for l in labels.cases:
        if l.case_id not in cases:raise ValueError('Unknown label case')
        unique([o.id for o in l.obligations],'gold IDs');unique([d.id for d in l.decisions],'decision IDs')
        gold={o.id for o in l.obligations}
        if any(d.obligation_id not in gold for d in l.decisions):raise ValueError('Unknown gold obligation in decision')
        # Current scorer evaluates one aggregate assessment per atomic obligation.
        unique([d.obligation_id for d in l.decisions],'decision obligation IDs')
        source=files[l.case_id]['regulation']
        if source.suffix.lower()=='.pdf':
            from pypdf import PdfReader
            text='\n'.join(p.extract_text() or '' for p in PdfReader(source).pages)
        else:text=source.read_text()
        if any(o.quote not in text for o in l.obligations):raise ValueError('Gold quote does not resolve to supplied source')
        if l.review.status=='reviewed' and {d.obligation_id for d in l.decisions}!=gold:raise ValueError('Reviewed labels need a decision for every gold obligation')
        if l.review.status=='reviewed' and not reviewed(l.review,bool(cases[l.case_id].critical_categories)):
            raise ValueError('Reviewed labels require independent reviewers and review metadata')
    return {l.case_id:l for l in labels.cases}

def run_case(client,case,files,manifest_hash,timeout=180,wait=time.sleep):
    """Use authenticated production API. Workers run separately; wait is injectable in tests."""
    started=time.monotonic();record={'case_id':case.id,'manifest_hash':manifest_hash,'input_hashes':{k:v.sha256 for k,v in case.inputs.items()},
      'started_at':datetime.now(timezone.utc).isoformat(),'status':'failed','error':None,'extraction':None,'assessment_job':None,'assessments':[]}
    deadline=started+timeout
    def check(response):
        if not response.is_success:raise ValueError('api_http_'+str(response.status_code))
        return response.json()
    def poll(path,done):
        while True:
            result=check(client.get(path))
            if path.startswith('/api/extractions/'):record['extraction']=result
            else:
                record['assessment_job']=result['job'];record['assessment_model_calls']=result['model_calls']
            if done(result):return result
            if time.monotonic()>=deadline:raise TimeoutError('poll_timeout')
            wait(.2)
    try:
        upload={role:(files[role].name,files[role].read_bytes()) for role in ('regulation','policy','evidence') if role in files}
        job=check(client.post('/api/extractions',files=upload,data={'profile':files['profile'].read_text(),'synthetic':str(case.material=='synthetic').lower()}))
        record['extraction_job_id']=job['id']
        result=poll('/api/extractions/'+job['id'],lambda r:r['job']['status'] in ('completed','failed','cancelled'))
        record['extraction']=result
        if not result['complete']:raise ValueError(result['job']['error_code'] or result['job']['status'])
        if not any(i['kind']=='obligation' for i in result['items']):record['status']='completed';return record
        job=check(client.post('/api/extractions/'+job['id']+'/assess',json={'evidence_context':load(files['context']) if 'context' in files else None}))
        record['assessment_job_id']=job['id']
        result=poll('/api/jobs/'+job['id'],lambda r:r['job']['status'] in ('awaiting_review','completed','failed','cancelled'))
        job=result['job'];record['assessment_job']=job
        if job['status'] in ('failed','cancelled'):raise ValueError(job['error_code'] or job['status'])
        record['assessments']=[check(client.get('/api/assessments/'+id)) for id in job['output_ids']]
        record['status']='completed'
    except (ValueError,TimeoutError) as error:record['error']=str(error)
    except httpx.HTTPError:record['error']='api_transport_error'
    finally:record['elapsed_ms']=round((time.monotonic()-started)*1000)
    return record

def rate(n,d):return {'numerator':n,'denominator':d,'value':n/d if d else None}

def metrics(rows):
    tp=sum(r['tp'] for r in rows);pred=sum(r['predicted'] for r in rows);gold=sum(r['gold'] for r in rows)
    matrix={a:{p:0 for p in ('full','partial','none','unassessed','missing')} for a in ('full','partial','none','unassessed','unmatched')}
    decisions=[d for r in rows for d in r['decisions']]
    for d in decisions:matrix[d['expected']['coverage']][d['actual'].get('coverage','missing')]+=1
    for r in rows:
        for actual in r.get('unmatched_assessments',[]):matrix['unmatched'][actual['coverage']]+=1
    full=sum(matrix[a]['full'] for a in matrix)
    critical=[d for r in rows if r['critical'] for d in r['decisions']]
    def resolved_state(s):return bool(s) and (s.get('applicability')=='not_applicable' or (s.get('applicability')=='applicable' and s.get('coverage')!='unassessed' and s.get('evidence')!='unassessed' and s.get('test') in ('pass','fail')))
    resolved=sum(resolved_state(d['actual']) for d in decisions)
    predictions=[d['actual'] for d in decisions if d['actual']]+[a for r in rows for a in r.get('unmatched_assessments',[])]
    return {'cases':len(rows),'unmatched_assessment_passes':sum(a['test']=='pass' for r in rows for a in r.get('unmatched_assessments',[])),'obligation_precision':rate(tp,pred),'obligation_recall':rate(tp,gold),
      'mapping_confusion':matrix,'full_mapping_precision':rate(matrix['full']['full'],full),
      'assessment_exact_accuracy':rate(sum(d['correct'] for d in decisions),len(decisions)),
      'critical_false_passes':rate(sum(d['actual'].get('test')=='pass' and d['expected']['test']!='pass' for d in critical),sum(d['expected']['test']!='pass' for d in critical)),
      'abstention':rate(sum(not resolved_state(s) for s in predictions),len(predictions)),
      'missing_decisions':sum(not d['actual'] for d in decisions),
      'resolved_case_coverage':rate(resolved,len(decisions)),'unresolved_or_missing':rate(len(decisions)-resolved,len(decisions)),
      'citation_resolution':rate(sum(r['citations_resolved'] for r in rows),sum(r['citation_count'] for r in rows)),
      'citation_support':rate(sum(r['citations_supported'] for r in rows),sum(r['citation_count'] for r in rows))}

def score(manifest_path,labels_path,records_path,adjudications_path):
    m,files,mh=read_manifest(manifest_path);labels=validate_labels(m,files,mh,labels_path)
    records=load(records_path);unique([r['case_id'] for r in records],'result case IDs');records={r['case_id']:r for r in records}
    adj=Adjudications.model_validate(load(adjudications_path))
    if adj.manifest_hash!=mh:raise ValueError('Adjudications belong to another manifest')
    unique([a.case_id for a in adj.cases],'adjudication case IDs');adjs={a.case_id:a for a in adj.cases}
    case_ids={c.id for c in m.cases}
    if not records.keys()<=case_ids or not adjs.keys()<=case_ids:raise ValueError('Unknown result/adjudication case')
    rows=[];excluded=[];latency=[];calls=[];seen_calls=set()
    for c in m.cases:
        r=records.get(c.id);l=labels.get(c.id);a=adjs.get(c.id)
        if r:
            if r['manifest_hash']!=mh or r['input_hashes']!={k:v.sha256 for k,v in c.inputs.items()}:raise ValueError('Result input hashes do not match manifest')
            latency.append({'case_id':c.id,'elapsed_ms':r['elapsed_ms']})
            groups=[(r.get('extraction_job_id'),(r.get('extraction') or {}).get('model_calls',[]))]
            groups += [(r.get('assessment_job_id'),r.get('assessment_model_calls',[]))]
            groups += [(v.get('run_context',{}).get('job_id'),v.get('run_context',{}).get('model_calls',[])) for v in r['assessments'] if v.get('run_context')]
            for job,entries in groups:
                for index,call in enumerate(entries):
                    key=(job,index)
                    if key not in seen_calls:calls.append(call['metadata']);seen_calls.add(key)
        reason='missing_run' if not r else ('run_'+r['status'] if r['status']!='completed' else ('missing_or_unreviewed_labels' if not l or not reviewed(l.review,bool(c.critical_categories)) else ('missing_or_unreviewed_adjudication' if not a or not reviewed(a.review,bool(c.critical_categories)) else None)))
        if reason:excluded.append({'case_id':c.id,'reason':reason,'error':r.get('error') if r else None});continue
        if a.record_hash!=digest(canonical(r)):raise ValueError('Adjudication does not bind to exact result')
        items=r['extraction']['items'];pred={i['id'] for i in items if i['kind']=='obligation'};gold={o.id for o in l.obligations}
        unique([x.predicted_id for x in a.matches],'predicted matches');unique([x.gold_id for x in a.matches],'gold matches')
        if any(x.predicted_id not in pred or x.gold_id not in gold for x in a.matches):raise ValueError('Invalid semantic match reference')
        assessments={v['snapshot']['id']:v for v in r['assessments']}
        unique([x.decision_id for x in a.alignments],'decision alignments');unique([x.assessment_id for x in a.alignments if x.assessment_id],'assessment alignments')
        if {x.decision_id for x in a.alignments}!={d.id for d in l.decisions}:raise ValueError('Every gold decision needs alignment or explicit missing prediction')
        decisions=[];aligned={x.decision_id:x.assessment_id for x in a.alignments}
        semantic={x.gold_id:x.predicted_id for x in a.matches}
        for d in l.decisions:
            id=aligned[d.id];v=assessments.get(id) if id else None
            if id and not v:raise ValueError('Unknown assessment alignment')
            if v:
                expected_pred=semantic.get(d.obligation_id)
                # Domain obligation IDs include the assessment job prefix.
                if not expected_pred or next(e for e in v['provenance'] if e['id']==v['snapshot']['applicability_id'])['obligation_id']!='D-'+r['assessment_job_id'][:12]+'-'+expected_pred:raise ValueError('Decision alignment disagrees with semantic obligation match')
            actual={k:v['snapshot']['states'][k] for k in ('applicability','coverage','evidence','test')} if v else {}
            if v:actual['gap_types']=sorted(e['gap_type'] for e in v['followups'] if e['kind']=='gap')
            expected={k:getattr(d,k) for k in ('applicability','coverage','evidence','test')};expected['gap_types']=sorted(d.gap_types)
            decisions.append({'expected':expected,'actual':actual,'correct':expected==actual})
        citations={(i['id'],n):q for i in items for n,q in enumerate(i.get('citations',[]))}
        unique([(x.item_id,x.citation_index) for x in a.citations],'citation adjudications')
        if {(x.item_id,x.citation_index) for x in a.citations}!=citations.keys():raise ValueError('Every output citation requires semantic adjudication')
        docs={d['role']:d for d in r['extraction']['inputs']['documents']}
        resolved=0;source_text={}
        for role,doc in docs.items():
            if doc['sha256']!=c.inputs[role].sha256:raise ValueError('Exported source differs from manifest')
            path=files[c.id][role]
            if path.suffix.lower()=='.pdf':
                from pypdf import PdfReader
                text='\n\f\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
            else:text=path.read_bytes().decode('utf-8-sig')
            if digest(text.encode())!=doc['text_sha256']:raise ValueError('Exported parser text hash mismatch')
            source_text[role]=text
        for i in items:
            doc=docs['regulation' if i['kind']=='obligation' else 'policy']
            for q in i.get('citations',[]):
                resolved+=bool(q['document_id']==doc['id'] and q['text_sha256']==doc['text_sha256'] and 0<=q['start']<q['end']<=len(source_text[doc['role']]) and source_text[doc['role']][q['start']:q['end']]==q['text'])
        rows.append({'case_id':c.id,'material':c.material,'split':c.split,'critical':bool(c.critical_categories),'tp':len(a.matches),'predicted':len(pred),'gold':len(gold),'decisions':decisions,'unmatched_assessments':[v['snapshot']['states'] for id,v in assessments.items() if id not in aligned.values()],'citation_count':len(citations),'citations_resolved':resolved,'citations_supported':sum(q.supported for q in a.citations)})
    usage_keys=('prompt_tokens','completion_tokens','total_tokens')
    totals={k:sum(x.get('usage',{}).get(k,0) for x in calls) if calls and all(isinstance(x.get('usage',{}).get(k),int) for x in calls) else None for k in usage_keys}
    protocol=load(PROTOCOL)
    reviewed_labels=[(c,labels[c.id]) for c in m.cases if c.id in labels and reviewed(labels[c.id].review,bool(c.critical_categories))]
    counts={'independent_families':len({c.family_id for c,l in reviewed_labels}),
      'total_obligation_cases':sum(len(l.obligations) for c,l in reviewed_labels),
      'total_mapping_evidence_decisions':sum(len(l.decisions) for c,l in reviewed_labels),
      'heldout_obligation_cases':sum(len(l.obligations) for c,l in reviewed_labels if c.split=='heldout'),
      'heldout_mapping_evidence_decisions':sum(len(l.decisions) for c,l in reviewed_labels if c.split=='heldout')}
    minimums={k:{'actual':n,'required':protocol['minimum_'+k],'met':n>=protocol['minimum_'+k]} for k,n in counts.items()}
    return {'schema_version':'1.0','manifest_hash':mh,'claim':'Evaluation tooling output; no automatic feature or depth tier promotion.',
      'total_cases':len(m.cases),'scored_cases':len(rows),'excluded':excluded,'metrics':metrics(rows),
      'groups':{material+':'+split:metrics([r for r in rows if r['material']==material and r['split']==split]) for material in ('synthetic','real_source_synthetic_bank','real_bank') for split in ('development','heldout')},
      'critical_metrics':metrics([r for r in rows if r['critical']]),'case_results':rows,
      'run_failures':rate(sum(r['status']=='failed' for r in records.values()),len(records)),
      'blocked_runs':sum(r['status']=='blocked' for r in records.values()),
      'critical_categories_missing':[k for k in load(PROTOCOL)['critical_cases'] if not any(k in c.critical_categories and c.id in {r['case_id'] for r in rows} for c in m.cases)],
      'latency':latency,'model_calls':len(calls),'providers':sorted({x.get('provider','unknown') for x in calls}),
      'token_usage':totals,'cost_usd':None,'cost_reason':'No verified price schedule attached; unknown is not zero.',
      'label_disagreements_or_ambiguities':sum(l.review.status=='ambiguous' for l in labels.values()),
      'readiness':{'reviewed_dataset_minimums':minimums,'dataset_minimums_met':all(v['met'] for v in minimums.values()),'human_reviewed_cases':sum(reviewed(l.review,bool(next(c for c in m.cases if c.id==l.case_id).critical_categories)) for l in labels.values()),'independent_families':len({c.family_id for c in m.cases}),'heldout_cases':sum(c.split=='heldout' for c in m.cases),'protocol_targets':load(PROTOCOL),'tier_achieved':None}}
