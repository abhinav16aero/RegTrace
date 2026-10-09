"""Evaluation CLI; immutable outputs, same authenticated pipeline, no inferred gold labels."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from regtrace import evaluation as ev
import httpx


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['validate','run','review-template','report'])
    p.add_argument('--manifest',type=Path,default=ev.ROOT/'data/evaluation/development-manifest.json')
    p.add_argument('--labels',type=Path,default=ev.ROOT/'data/evaluation/development-labels.json')
    p.add_argument('--records',type=Path)
    p.add_argument('--adjudications',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--origin',default='http://127.0.0.1:8000')
    p.add_argument('--credentials-file',type=Path,default=ev.ROOT/'.local/demo-users.json')
    p.add_argument('--username',default='analyst')
    p.add_argument('--timeout',type=int,default=180,help='Maximum polling seconds per case; worker continues after timeout')
    a=p.parse_args();m,files,mh=ev.read_manifest(a.manifest)
    labels=ev.validate_labels(m,files,mh,a.labels)
    if a.command=='validate':
        print(json.dumps({'manifest_hash':mh,'cases':len(m.cases),'reviewed_cases':sum(ev.reviewed(l.review,bool(next(c for c in m.cases if c.id==l.case_id).critical_categories)) for l in labels.values()),'heldout_cases':sum(c.split=='heldout' for c in m.cases),'missing_labels':sorted({c.id for c in m.cases}-labels.keys())},indent=2));return
    if not a.output:p.error('--output is required')
    if a.output.exists():p.error('Output exists; use a new versioned path')
    if a.command=='run':
        if a.timeout<=0:p.error('--timeout must be positive')
        users=ev.load(a.credentials_file);user=next(u for u in users if u['username']==a.username)
        records=[]
        with httpx.Client(base_url=a.origin,headers={'Origin':a.origin},timeout=120) as client:
            response=client.post('/api/login',json={'username':user['username'],'password':user['password']})
            if not response.is_success:raise ValueError('Login failed: HTTP '+str(response.status_code))
            client.headers['X-CSRF-Token']=response.json()['csrf_token']
            try:
                response=client.get('/api/model');response.raise_for_status()
                provider=response.json()
                for case in m.cases:
                    if not provider['configured']:
                        record={'case_id':case.id,'manifest_hash':mh,'input_hashes':{k:v.sha256 for k,v in case.inputs.items()},'status':'blocked','error':'provider_key_missing','elapsed_ms':0,'extraction':None,'assessment_job':None,'assessments':[]}
                    else:record=ev.run_case(client,case,files[case.id],mh,a.timeout)
                    records.append(record)
                    # Preserve each completed case if a later case/process fails.
                    ev.save_new(a.output.parent/(a.output.stem+'-'+case.id+'.json'),record)
                    print(json.dumps({'case_id':case.id,'status':record['status'],'error':record['error']}),flush=True)
            finally:client.post('/api/logout')
        ev.save_new(a.output,records)
        if any(r['status']!='completed' for r in records):return 2
    elif a.command=='review-template':
        if not a.records:p.error('--records is required')
        entries=[]
        for r in ev.load(a.records):
            label=labels.get(r['case_id'])
            entries.append({'case_id':r['case_id'],'record_hash':ev.digest(ev.canonical(r)),
              'review':{'status':'draft','author':'evaluation-template','reviewer':None,'second_reviewer':None,'reviewed_at':None,'note':'Review original sources, material qualifiers, matches, decision alignment and every citation. False values are unreviewed placeholders.'},
              'matches':[],'alignments':[{'decision_id':d.id,'assessment_id':None} for d in label.decisions] if label else [],
              'citations':[{'item_id':i['id'],'citation_index':n,'supported':False} for i in (r.get('extraction') or {}).get('items',[]) for n,q in enumerate(i.get('citations',[]))]})
        ev.save_new(a.output,{'manifest_hash':mh,'cases':entries})
    else:
        if not a.records or not a.adjudications:p.error('--records and --adjudications are required')
        ev.save_new(a.output,ev.score(a.manifest,a.labels,a.records,a.adjudications))
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,KeyError,OSError,httpx.HTTPError) as error:
        # Do not render HTTP URLs, DSNs or credential file contents in failures.
        print('Evaluation failed: '+(str(error) if isinstance(error,ValueError) else type(error).__name__),file=sys.stderr);sys.exit(1)
