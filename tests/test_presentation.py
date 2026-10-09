"""Display references must be stable, isolated and independent of immutable results."""
from concurrent.futures import ThreadPoolExecutor
from test_m1_integration import database_server,world,load
from regtrace.storage import transaction
from regtrace import presentation


def test_names_rename_and_tenant_boundary(world):
    settings,_,client,_=world
    c=client('analyst');load(settings,c)
    original=c.get('/api/assessments/A1-v1').json()
    first=c.get('/api/presentation');assert first.status_code==200,first.text
    data=first.json();ref=data['records']['A1-v1']['reference']
    assert ref.startswith('ASM-') and data['records']['A1-v1']['title']!='Assessment'
    assert c.post('/api/presentation/A1-v1/rename',json={'title':'Identity review before activation'}).status_code==200
    updated=c.get('/api/presentation').json()['records']['A1-v1']
    assert updated['reference']==ref and updated['title']=='Identity review before activation'
    assert c.get('/api/assessments/A1-v1').json()['snapshot']==original['snapshot']
    other=client('other-reviewer')
    assert other.get('/api/presentation').json()['records']=={}
    assert other.post('/api/presentation/A1-v1/rename',json={'title':'Cross-tenant attempt'}).status_code==404
    assert c.post('/api/presentation/A1-v1/rename',json={'title':'   '}).status_code==422


def test_concurrent_reference_allocation(world):
    settings,_,_,_=world
    def register(i):
        with transaction(settings.app_dsn,'demo-bank') as conn:
            presentation.register(conn,'demo-bank','record-'+str(i%6),'source','Source '+str(i%6))
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(register,range(24)))
    with transaction(settings.app_dsn,'demo-bank') as conn:
        rows=conn.execute('SELECT reference FROM presentation_records').fetchall()
    assert len(rows)==6 and len({r['reference'] for r in rows})==6


def test_demo_download_and_rehearsal_are_workspace_scoped(world):
    settings,_,client,_=world
    c=client('analyst')
    assert c.post('/api/demo/rehearsals',json={'case':'operations'}).status_code==404
    assert c.get('/api/demo/files/onboarding-original.csv').status_code==404
    with transaction(settings.app_dsn,'demo-bank') as conn:
        conn.execute("INSERT INTO presentation_workspaces(tenant_id,name,is_demo) VALUES ('demo-bank','Test demonstration',true)")
    original=c.get('/api/demo/files/onboarding-original.csv')
    assert original.status_code==200 and original.content.startswith(b'case_id,')
    assert c.get('/api/demo/files/not-in-manifest.txt').status_code==404
    first=c.post('/api/demo/rehearsals',json={'case':'operations'})
    second=c.post('/api/demo/rehearsals',json={'case':'operations'})
    assert first.status_code==second.status_code==202
    assert first.json()['id']!=second.json()['id']
    assert client('other-reviewer').get('/api/demo/files/onboarding-original.csv').status_code==404
    assert client('other-reviewer').get('/api/jobs/'+first.json()['id']).status_code==404


def test_library_deduplicates_documents_preserves_usage_and_evidence_isolation(world):
    from test_m1_integration import document_upload
    from regtrace.storage import ArtifactStore
    settings,_,client,_=world
    c=client('analyst');other=client('other-reviewer')
    csv=b'case_id,status\nC1,complete\n'
    first=document_upload(c,'Banks must verify identity.','Operations verifies identity.',csv).json()['id']
    import json
    payload=c.get('/api/extractions/'+first).json()['inputs']
    profile=payload['profile'] | {'name':'Another test profile'}
    second=c.post('/api/extractions',data={'profile':json.dumps(profile),'synthetic':'true'},files={
        'regulation':('reg.txt',b'Banks must verify identity.','text/plain'),
        'policy':('policy.txt',b'Operations verifies identity.','text/plain'),
        'evidence':('evidence.csv',csv,'text/csv')}).json()['id']
    assert first!=second
    library=c.get('/api/presentation').json()['documents']
    assert len(library)==3
    for document in library:
        assert document['usage_count']==2
        assert {u['job_id'] for u in document['usages']}=={first,second}
        assert document['job_id']==second
    assert other.get('/api/presentation').json()['documents']==[]
    evidence=c.get('/api/extractions/'+first+'/evidence')
    assert evidence.status_code==200 and evidence.content==csv
    assert evidence.headers['content-type'].startswith('text/csv')
    assert other.get('/api/extractions/'+first+'/evidence').status_code==404
    changed=document_upload(c,'Banks must verify identity and retain proof.','Operations verifies identity.').json()['id']
    assert c.get('/api/extractions/'+changed+'/evidence').status_code==404
    assert len(c.get('/api/presentation').json()['documents'])==4
    digest=next(d['sha256'] for d in library if d['role']=='evidence')
    # Corrupt the isolated fixture's stored artifact, never the live demo.
    store=ArtifactStore(settings.artifact_root)
    path=store.path(digest)
    path.chmod(0o644)
    path.write_bytes(b'corrupt')
    assert c.get('/api/extractions/'+first+'/evidence').status_code==409
