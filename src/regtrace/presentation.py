"""Mutable display metadata, deliberately separate from immutable assurance records."""
from psycopg.types.json import Jsonb
from . import service

PREFIXES={'assessment':'ASM','source':'REG','policy':'POL','obligation':'OBL','control':'CTL',
          'evidence':'EVD','remediation':'REM','gap':'GAP','job':'RUN','span':'CIT','profile':'BNK',
          'mapping':'MAP','test_run':'TST','test_definition':'PRC','applicability':'APP','track':'TRK'}

def human(value):
    return str(value).replace('_',' ').replace('-',' ').strip()

def title(entity):
    if entity.get('title') or entity.get('name'):
        return entity.get('title') or entity['name']
    if entity.get('kind')=='obligation':
        return ' '.join(str(entity.get(k) or '') for k in ('action','object')).strip().capitalize()
    if entity.get('kind')=='remediation':return entity.get('action','Proposed correction')
    if entity.get('kind')=='gap':return human(entity.get('gap_type','Gap')).capitalize()
    if entity.get('kind')=='test_run':return 'Evidence sample test'
    if entity.get('kind')=='test_definition':return 'Evidence testing procedure'
    if entity.get('kind')=='span':return 'Source quotation'
    return human(entity.get('kind','record')).capitalize()

def register(conn,tenant,id,kind,label):
    existing=conn.execute('SELECT * FROM presentation_records WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
    if existing:
        if not existing['curated'] and label and existing['title']!=label:
            conn.execute('UPDATE presentation_records SET title=%s WHERE tenant_id=%s AND id=%s',(label[:240],tenant,id))
        return
    prefix=PREFIXES.get(kind,'REC')
    # Tenant lock serializes allocation across workers and concurrent first visits.
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('presentation:'+tenant,))
    if conn.execute('SELECT 1 FROM presentation_records WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone():return
    n=conn.execute('SELECT count(*) AS n FROM presentation_records WHERE tenant_id=%s AND reference LIKE %s',(tenant,prefix+'-%')).fetchone()['n']+1
    ref=f'{prefix}-{n:03d}'
    conn.execute('INSERT INTO presentation_records(tenant_id,id,kind,reference,title) VALUES (%s,%s,%s,%s,%s)',
                 (tenant,id,kind,ref,(label or human(kind).capitalize()+' '+ref)[:240]))

def catalogue(conn,tenant):
    sets={r['id']:r['payload'] for r in conn.execute('SELECT id,payload FROM document_sets WHERE tenant_id=%s ORDER BY created_at,id',(tenant,))}
    roles={d['sha256']:d['role'] for payload in sets.values() for d in payload['documents']}
    entities={r['payload']['id']:r['payload'] for r in conn.execute('SELECT payload FROM entity_versions WHERE tenant_id=%s ORDER BY id',(tenant,))}
    for id,e in entities.items():
        label=title(e)
        if e['kind']=='assessment':
            applicability=entities.get(e.get('applicability_id'),{})
            obligation=entities.get(applicability.get('obligation_id'),{})
            label=title(obligation) if obligation else 'Assessment finding'
        register(conn,tenant,id,'policy' if e['kind']=='source' and roles.get(e.get('sha256'))=='policy' else e['kind'],label)
    for payload in sets.values():
        for doc in payload['documents']:
            register(conn,tenant,doc['id'],'policy' if doc['role']=='policy' else 'source',doc['filename'].rsplit('.',1)[0].replace('_',' '))
    jobs=conn.execute('SELECT id,kind,status,input_payload,created_at,error_code FROM workflow_jobs WHERE tenant_id=%s ORDER BY created_at,id',(tenant,)).fetchall()
    indexed={j['id']:j for j in jobs}
    library=[]
    for job in jobs:
        payload=job['input_payload']; source=job
        if payload.get('extraction_job_id'):source=indexed.get(payload['extraction_job_id'],job)
        documents=sets.get(source['input_payload'].get('document_set_id'),{}).get('documents',[])
        label=payload.get('name') or human(job['kind']).capitalize()
        if documents:label+=' · '+documents[0]['filename'].rsplit('.',1)[0].replace('_',' ')
        register(conn,tenant,job['id'],'job',label)
        if job['kind']=='extract_documents':
            attached=sets[source['input_payload']['document_set_id']].get('evidence')
            if attached:
                eid='evidence-'+attached['sha256']
                register(conn,tenant,eid,'evidence',attached['filename'].rsplit('.',1)[0].replace('-',' '))
                library.append({'id':eid,'job_id':job['id'],'role':'evidence','filename':attached['filename'],'pages':0,
                                'rows':attached['rows'],'status':job['status'],'created_at':job['created_at'],'sha256':attached['sha256'],
                                'synthetic':sets[source['input_payload']['document_set_id']]['synthetic']})
            for doc in documents:
                library.append({'id':doc['id'],'job_id':job['id'],'role':doc['role'],'filename':doc['filename'],
                                'pages':len(doc.get('pages',[])),'status':job['status'],'created_at':job['created_at'],
                                'sha256':doc['sha256'],'synthetic':sets[source['input_payload']['document_set_id']]['synthetic']})
    grouped={}
    for item in library:
        key=(item['id'],item['role'])
        usage={k:item[k] for k in ('job_id','status','created_at')}
        if key not in grouped:grouped[key]=item|{'usages':[]}
        # Ascending job order makes the newest run the explicit preview context.
        grouped[key].update({k:item[k] for k in ('job_id','status','created_at')})
        grouped[key]['usages'].append(usage)
    library=[item|{'usage_count':len(item['usages'])} for item in grouped.values()]
    library.sort(key=lambda item:item['created_at'],reverse=True)
    records={r['id']:{k:v for k,v in r.items() if k!='tenant_id'} for r in conn.execute('SELECT * FROM presentation_records WHERE tenant_id=%s',(tenant,))}
    workspace=conn.execute('SELECT name,is_demo,manifest FROM presentation_workspaces WHERE tenant_id=%s',(tenant,)).fetchone()
    return {'records':records,'documents':library,'workspace':workspace or {'name':human(tenant).title(),'is_demo':False,'manifest':{}}}

def rename(conn,user,id,label):
    record=conn.execute('SELECT title FROM presentation_records WHERE tenant_id=%s AND id=%s FOR UPDATE',(user['tenant_id'],id)).fetchone()
    if not record:raise service.Missing('Record not found')
    conn.execute('UPDATE presentation_records SET title=%s,curated=true WHERE tenant_id=%s AND id=%s',(label,user['tenant_id'],id))
    service.audit(conn,user['tenant_id'],user['id'],'record.renamed',id,{'previous_title':record['title'],'title':label})
    return {'id':id,'title':label}
