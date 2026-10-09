"""Verify persistence across a real API/worker/PostgreSQL stop-start of the local demo."""
import json
import subprocess
import sys
from datetime import datetime,timezone
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
users=json.loads((ROOT/'.local/demo-users.json').read_text())
u=next(u for u in users if u['username']=='reviewer')
with httpx.Client(base_url='http://127.0.0.1:8000',headers={'origin':'http://127.0.0.1:8000'}) as client:
    login=client.post('/api/login',json={'username':u['username'],'password':u['password']});login.raise_for_status()
    before=client.get('/api/assessments').json()
    jobs_before=client.get('/api/jobs').json()
    assert before, 'Load the demo fixture before checking persistence'
    subprocess.run([sys.executable,'-I',str(ROOT/'scripts/dev.py'),'stop'],check=True)
    subprocess.run([sys.executable,'-I',str(ROOT/'scripts/dev.py'),'start'],check=True)
    response=client.get('/api/assessments');response.raise_for_status()
    after=response.json();jobs_after=client.get('/api/jobs').json()
    assert before==after,'Assessment or review state changed across restart'
    assert jobs_before==jobs_after,'Job state changed across restart'
    assert client.get('/api/assessments/A1-v1').json()['current_states']['test']=='fail'
    report={'passed':True,'checked_at':datetime.now(timezone.utc).isoformat(),
            'restarted':['api','worker','postgresql_container'],
            'preserved':['login_session','assessment_snapshots','review_history','job_ledger','failed_test_result'],
            'assessment_count':len(after),'job_count':len(jobs_after)}
    (ROOT/'artifacts/m1/restart-check.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
