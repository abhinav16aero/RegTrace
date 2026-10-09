#!/usr/bin/env python3
"""Submit documents through the same authenticated API as the UI; no fixture answers."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--regulation',type=Path,required=True)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--evidence',type=Path)
    parser.add_argument('--synthetic',action='store_true')
    parser.add_argument('--wait',type=int,default=0,help='Poll at most this many seconds; default returns the queued job')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    # Local demo credentials are read only into the authenticated request, never printed.
    users=json.loads((ROOT/'.local/demo-users.json').read_text())
    user=next(u for u in users if u['username']=='analyst')
    origin='http://127.0.0.1:8000'
    with httpx.Client(base_url=origin,headers={'Origin':origin},timeout=120) as client:
        login=client.post('/api/login',json={'username':user['username'],'password':user['password']})
        login.raise_for_status();client.headers['X-CSRF-Token']=login.json()['csrf_token']
        with ExitStack() as stack:
            files={name:(path.name,stack.enter_context(path.open('rb'))) for name,path in
                   [('regulation',args.regulation),('policy',args.policy),('evidence',args.evidence)] if path}
            response=client.post('/api/extractions',files=files,data={'profile':args.profile.read_text(),
                                  'synthetic':str(args.synthetic).lower()})
        if response.status_code!=202:raise SystemExit('Upload failed: '+response.text)
        job=response.json();print(json.dumps({'job_id':job['id'],'status':job['status']}),flush=True)
        deadline=time.monotonic()+args.wait
        while True:
            response=client.get('/api/extractions/'+job['id']);response.raise_for_status();result=response.json()
            if result['job']['status'] in ('completed','failed','cancelled') or time.monotonic()>=deadline:break
            time.sleep(2)
        if args.report:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'status':result['job']['status'],'error_code':result['job']['error_code'],
                          'items':len(result['items']),'model_calls':len(result['model_calls']),
                          'complete':result['complete']}))
        if result['job']['status'] in ('failed','cancelled'):raise SystemExit(1)

if __name__=='__main__':main()
