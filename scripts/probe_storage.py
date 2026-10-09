"""Execute three domain queries in a disposable isolated PostgreSQL container."""
import json
import sys
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_m0 import validate_fixture

ROOT = Path(__file__).resolve().parents[1]

def run_probe():
    fixture, entities = validate_fixture()
    config = json.loads((ROOT/'config/runtime.json').read_text())
    name = 'et-m0-probe-' + uuid.uuid4().hex[:10]
    def docker(*args, text=None, check=True):
        return subprocess.run(['docker',*args],input=text,text=True,capture_output=True,check=check)
    def sql(text,check=True):
        return docker('exec','-i',name,'psql','-X','-U','postgres','-d','postgres','-At',
                      '-v','ON_ERROR_STOP=1','-v','tenant=demo-bank',
                      '-v','assessment=A1-v1','-v','obligation=O1-v1',text=text,check=check)
    def quote(value):return "'"+str(value).replace("'","''")+"'"
    report = {'executed_at':datetime.now(timezone.utc).isoformat(),'image':config['postgres_image'],
              'queries':{},'constraint_checks':{}}
    created=False
    try:
        docker('run','-d','--rm','--name',name,'--network','none','--tmpfs','/var/lib/postgresql/data',
               '-e','POSTGRES_HOST_AUTH_METHOD=trust',config['postgres_image'])
        created=True
        for _ in range(50):
            if docker('exec',name,'pg_isready','-h','127.0.0.1','-U','postgres',check=False).returncode == 0:break
            time.sleep(.2)
        else:raise RuntimeError('Database did not become ready')
        report['server_version']=sql('SHOW server_version;').stdout.strip()
        sql((ROOT/'db/migrations/0001_versioned_domain.sql').read_text())
        statements=['BEGIN;']
        for e in fixture['entities']:
            vals=[e['tenant_id'],e['id'],e['lineage_id'],e['version'],e['kind'],json.dumps(e)]
            statements.append('INSERT INTO entity_versions(tenant_id,id,lineage_id,version,kind,payload) VALUES ('+','.join(quote(v) for v in vals)+');')
        for d in fixture['dependencies']:
            vals=[d[k] for k in ('tenant_id','dependent_id','input_id','relation')]
            statements.append('INSERT INTO dependencies VALUES ('+','.join(quote(v) for v in vals)+');')
        statements.append('COMMIT;');sql('\n'.join(statements))
        for query,expected in fixture['expected_queries'].items():
            start=time.perf_counter()
            output=sql((ROOT/f'db/queries/{query}.sql').read_text()).stdout.strip().splitlines()
            actual=[line.split('|') for line in output]
            comparable=actual if query=='shared_controls' else [row[0] for row in actual]
            assert comparable==expected,(query,comparable,expected)
            report['queries'][query]={'passed':True,'rows':actual,'client_elapsed_ms':round((time.perf_counter()-start)*1000,2)}
        cases={
            'immutable_version':"UPDATE entity_versions SET payload=payload WHERE tenant_id='demo-bank' AND id='A1-v1';",
            'immutable_dependency':"DELETE FROM dependencies WHERE tenant_id='demo-bank';",
            'invalid_relationship':"INSERT INTO dependencies VALUES ('demo-bank','A1-v1','BANK-v1','maps_control');",
            'cross_tenant_reference':"INSERT INTO dependencies VALUES ('other-bank','A1-v1','MAP1-v1','assesses_mapping');",
        }
        for label,statement in cases.items():
            result=sql('BEGIN;\n'+statement+'\nROLLBACK;',check=False)
            assert result.returncode!=0,(label,'unexpectedly allowed')
            assert 'ERROR:' in result.stderr,result.stderr
            report['constraint_checks'][label]=True
        # Runtime roles must not be owners/superusers: verify RLS under such a role.
        sql('CREATE ROLE m0_reader NOLOGIN NOSUPERUSER NOBYPASSRLS; GRANT SELECT ON entity_versions,dependencies TO m0_reader;')
        for tenant,expected in [('demo-bank',len(entities)),('other-bank',0)]:
            result=sql("SET ROLE m0_reader; SET app.tenant_id="+quote(tenant)+"; SELECT count(*) FROM entity_versions;")
            assert int(result.stdout.strip().splitlines()[-1])==expected
        result=sql('SET ROLE m0_reader; SELECT count(*) FROM entity_versions;')
        assert int(result.stdout.strip().splitlines()[-1])==0
        report['constraint_checks']['row_security_fail_closed']=True
        report['passed']=True
    finally:
        if created:docker('rm','-f',name,check=False)
    (ROOT/'artifacts/m0/storage-probe.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    run_probe()
