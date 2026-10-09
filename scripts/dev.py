#!/usr/bin/env python3
"""Local-only demo lifecycle; owns one labelled container and one named volume."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from urllib.parse import quote
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import psycopg
from psycopg import sql
from pwdlib import PasswordHash

LOCAL=ROOT/'.local'
CONTAINER='regtrace-demo-db'
VOLUME='regtrace-demo-pg'
LABEL='regtrace.owner=et-hackathon-local-demo'


def write_private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(data)
    os.chmod(path,0o600)


def docker(*args,check=True):
    return subprocess.run(['docker',*args],check=check,capture_output=True,text=True)


def assert_owned(kind,name):
    result=docker(kind,'inspect',name,check=False)
    if result.returncode:return None
    info=json.loads(result.stdout)[0]
    labels=info.get('Config',{}).get('Labels',{}) if kind=='container' else info.get('Labels',{})
    if (labels or {}).get('regtrace.owner')!='et-hackathon-local-demo':
        raise RuntimeError(f'Refusing to modify unowned {kind}: {name}')
    return info


def migrate_and_seed(admin_dsn,app_password,auth_password,users):
    with psycopg.connect(admin_dsn,autocommit=True) as conn:
        existing=conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
        done={r[0] for r in conn.execute('SELECT version FROM schema_migrations')} if existing else set()
        for file in sorted((ROOT/'db/migrations').glob('*.sql')):
            if file.name.split('_')[0] not in done:conn.execute(file.read_text())
        for name,password in [('regtrace_app',app_password),('regtrace_auth',auth_password)]:
            if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():
                conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS').format(sql.Identifier(name)))
            conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
        conn.execute('REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA public TO regtrace_app,regtrace_auth;')
        conn.execute('GRANT SELECT,INSERT ON entity_versions,dependencies,audit_events,review_events,document_sets,extraction_steps,model_calls,regulatory_change_reports,assessment_freshness_events,reassessment_links,remediation_events,remediation_retests,scenarios,scenario_assessment_links,scenario_results,cross_regulation_results,cross_regulation_reviews,contradiction_results,contradiction_reviews TO regtrace_app; GRANT SELECT,INSERT,UPDATE ON regulatory_tracks,regulatory_versions TO regtrace_app; GRANT SELECT ON relation_rules TO regtrace_app; GRANT SELECT,INSERT,UPDATE ON workflow_jobs TO regtrace_app;')
        conn.execute('GRANT SELECT,INSERT,UPDATE ON presentation_records,presentation_workspaces TO regtrace_app;')
        conn.execute('GRANT SELECT,UPDATE ON app_users TO regtrace_auth; GRANT SELECT,INSERT,DELETE ON sessions TO regtrace_auth;')
        hasher=PasswordHash.recommended()
        for user in users:
            conn.execute('''INSERT INTO app_users(id,tenant_id,username,password_hash,role) VALUES (%s,%s,%s,%s,%s)
              ON CONFLICT(username) DO NOTHING''',(user['username'],user['tenant_id'],user['username'],hasher.hash(user['password']),user['role']))
            if user.get('display_name'):
                conn.execute('UPDATE app_users SET display_name=%s WHERE username=%s',(user['display_name'],user['username']))


def setup():
    LOCAL.mkdir(exist_ok=True)
    bootstrap=LOCAL/'bootstrap.json'
    if bootstrap.exists():secret=json.loads(bootstrap.read_text())
    else:
        secret={k:secrets.token_urlsafe(24) for k in ('admin_password','app_password','auth_password')}
        write_private(bootstrap,json.dumps(secret,indent=2)+'\n')
    users_path=LOCAL/'demo-users.json'
    if users_path.exists():users=json.loads(users_path.read_text())
    else:
        users=[{'username':name,'tenant_id':tenant,'role':role,'password':secrets.token_urlsafe(18)} for name,tenant,role in
               [('analyst','demo-bank','analyst'),('reviewer','demo-bank','reviewer'),('other-reviewer','other-bank','reviewer')]]
        write_private(users_path,json.dumps(users,indent=2)+'\n')
    env=LOCAL/'postgres.env';write_private(env,'POSTGRES_PASSWORD='+secret['admin_password']+'\nPOSTGRES_DB=regtrace\n')
    image=json.loads((ROOT/'config/runtime.json').read_text())['postgres_image']
    info=assert_owned('container',CONTAINER)
    if info is None:
        assert_owned('volume',VOLUME)
        docker('volume','create','--label',LABEL,VOLUME)
        docker('run','-d','--name',CONTAINER,'--label',LABEL,'--env-file',str(env),
               '-p','127.0.0.1:55432:5432','-v',VOLUME+':/var/lib/postgresql/data',image)
    elif not info['State']['Running']:docker('start',CONTAINER)
    for _ in range(80):
        if docker('exec',CONTAINER,'pg_isready','-h','127.0.0.1','-U','postgres',check=False).returncode==0:break
        time.sleep(.25)
    else:raise RuntimeError('Demo database did not start')
    def dsn(role,password):return f'postgresql://{role}:{quote(password,safe="")}@127.0.0.1:55432/regtrace'
    migrate_and_seed(dsn('postgres',secret['admin_password']),secret['app_password'],secret['auth_password'],users)
    config={'app_dsn':dsn('regtrace_app',secret['app_password']),'auth_dsn':dsn('regtrace_auth',secret['auth_password']),
            'artifact_root':str(LOCAL/'artifacts'),'origin':'http://127.0.0.1:8000','secure_cookie':False,'tenants':sorted(set(['demo-bank','other-bank']+[u['tenant_id'] for u in users]))}
    write_private(LOCAL/'runtime.json',json.dumps(config,indent=2)+'\n')
    print('Database ready. Demo credentials: .local/demo-users.json (local-only file).')


def process_alive(entry):
    try:
        cmd=Path(f"/proc/{entry['pid']}/cmdline").read_bytes().replace(b'\x00',b' ').decode()
        return str(ROOT) in cmd and 'scripts/run.py' in cmd
    except (OSError,KeyError):return False


def start():
    setup()
    if not (ROOT/'web/dist/index.html').exists():raise RuntimeError('Build the web UI first: see README')
    pidpath=LOCAL/'processes.json'
    processes=json.loads(pidpath.read_text()) if pidpath.exists() else {}
    for mode in ('api','worker'):
        if mode in processes and process_alive(processes[mode]):continue
        with (LOCAL/f'{mode}.log').open('ab') as log:
            process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-I',str(ROOT/'scripts/run.py'),mode],
                cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
        processes[mode]={'pid':process.pid}
    write_private(pidpath,json.dumps(processes)+'\n')
    for _ in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=1) as r:
                if r.status==200:break
        except OSError:time.sleep(.2)
    else:raise RuntimeError('API did not start; see .local/api.log')
    if not all(process_alive(p) for p in processes.values()):raise RuntimeError('A process exited; inspect .local logs')
    print('RegTrace is running at http://127.0.0.1:8000')


def stop():
    pidpath=LOCAL/'processes.json'
    if pidpath.exists():
        for entry in json.loads(pidpath.read_text()).values():
            if process_alive(entry):os.kill(entry['pid'],signal.SIGTERM)
        pidpath.unlink()
    info=assert_owned('container',CONTAINER)
    if info and info['State']['Running']:docker('stop','-t','3',CONTAINER)
    print('Stopped. Database volume, sessions, review history and artifacts retained.')


def reset(confirmed):
    if not confirmed:raise RuntimeError('Reset deletes only this demo database; use reset --yes after reviewing README.')
    stop()
    if assert_owned('container',CONTAINER):docker('rm',CONTAINER)
    if assert_owned('volume',VOLUME):docker('volume','rm',VOLUME)
    print('Removed the labelled demo database volume. Local credentials and immutable artifacts retained; start seeds empty tenants.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['setup','start','stop','reset']);parser.add_argument('--yes',action='store_true');args=parser.parse_args()
    if args.command=='reset':reset(args.yes)
    else:globals()[args.command]()
