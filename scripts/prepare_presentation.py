#!/usr/bin/env python3
"""Prepare deterministic, labelled inputs and additive local demonstration accounts."""
import csv,hashlib,io,json,secrets,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'scripts'))
from pypdf import PdfReader
from dev import write_private
P=ROOT/'data/presentation';P.mkdir(exist_ok=True)
def write(name,text): (P/name).write_text(text)
profile={'name':'Meridian Demo Bank','jurisdiction':'India','entity_type':'scheduled commercial bank','products':['savings accounts'],'channels':['branch'],'customer_categories':['resident individuals'],'unknown_facts':[]}
write('profile.json',json.dumps(profile,indent=2)+'\n')
context={'producer':'Meridian Demo Bank · fictional operations export','scope':'40 fictional resident-individual savings account activations at branch MUM-01 during September 2026; approval timestamps only, not identity authenticity.','collected_at':'2026-10-01T06:30:00Z','period_start':'2026-09-01T00:00:00Z','period_end':'2026-09-30T23:59:59Z'}
write('evidence-context.json',json.dumps(context,indent=2)+'\n')
rows=[]
for i in range(1,41):
 activation=datetime(2026,9,1,tzinfo=timezone.utc)+timedelta(days=(i-1)//2,hours=9+i%2)
 approval=activation-timedelta(minutes=30+i)
 if i in [7,14,21,28]:approval=activation+timedelta(minutes=15)
 if i==35:approval=activation
 rows.append({'case_id':f'MDB-2026-{i:04d}','branch':'MUM-01','product':'savings','customer_category':'resident individual','review_at':approval.isoformat().replace('+00:00','Z'),'activation_at':activation.isoformat().replace('+00:00','Z'),'reviewer':['Priya Shah','Rohan Iyer','Neha Kulkarni'][i%3]})
# Forty wholly fictional records; exact and late timestamps are intentional test cases.
for name,correct in [('onboarding-original.csv',False),('onboarding-corrected.csv',True)]:
 out=io.StringIO();writer=csv.DictWriter(out,fieldnames=list(rows[0]));writer.writeheader()
 for r in rows:
  r=dict(r)
  if correct and r['case_id'] in [f'MDB-2026-{i:04d}' for i in [7,14,21,28,35]]:
   t=datetime.fromisoformat(r['activation_at'].replace('Z','+00:00'))-timedelta(minutes=45)
   r['review_at']=t.isoformat().replace('+00:00','Z')
  writer.writerow(r)
 write(name,out.getvalue())
write('simulation-operational-standard.txt','SIMULATED OPERATIONAL STANDARD — NOT LAW. This is an imaginary requirement for demonstrating sample testing only.\nFor every branch savings account opened for a resident individual by a scheduled commercial bank in India, the bank must record review_at (approval timestamp) strictly earlier than activation_at (activation timestamp).\n')
write('meridian-operational-policy.txt','MERIDIAN DEMO BANK — FICTIONAL INTERNAL POLICY\nControl name: Record review_at strictly earlier than activation_at.\nScope: For every branch savings account opened for a resident individual by a scheduled commercial bank in India, the bank records review_at (approval timestamp) strictly earlier than activation_at (activation timestamp).\nOwner: Branch Operations. Frequency: every account activation.\nExpected evidence: the fictional onboarding CSV with unique case_id, review_at and activation_at, scoped to the declared reporting period. The sample test compares review_at strictly earlier than activation_at for every row. No assertion of identity authenticity or institution-wide compliance is made.\n')
write('simulation-approval-standard-v1.txt','''SIMULATED REGULATORY INSTRUMENT — NOT LAW.
Meridian presentation approval-timing standard, version 1.
Effective from 2026-09-01 until 2026-12-31. Scope: scheduled commercial banks in India, branch savings accounts for resident individuals.
A scheduled commercial bank in India must record an approval timestamp strictly earlier than the activation timestamp for every branch savings account opened for a resident individual.
''')
write('simulation-approval-standard-v2.txt','''SIMULATED REGULATORY INSTRUMENT — NOT LAW.
Meridian presentation approval-timing standard, version 2. This is a complete simulated replacement of version 1.
Effective from 2026-10-01 until 2026-12-31. Scope: scheduled commercial banks in India, branch savings accounts for resident individuals.
A scheduled commercial bank in India must record an approval timestamp strictly earlier than the activation timestamp and retain the approving employee name for every branch savings account opened for a resident individual.
''')
write('simulation-independent-review-standard.txt','''SIMULATED INDEPENDENT REGULATORY INSTRUMENT — NOT LAW.
Meridian presentation post-activation review standard. This is a separate imaginary instrument, not a version of the approval-timing standard.
Effective from 2026-09-01 until 2026-12-31. Scope: scheduled commercial banks in India, branch savings accounts for resident individuals.
A scheduled commercial bank in India must record an approval timestamp strictly later than the activation timestamp for every branch savings account opened for a resident individual.
''')
write('meridian-onboarding-policy.txt','''MERIDIAN DEMO BANK — FICTIONAL INTERNAL POLICY
Branch savings onboarding control, approved 2026-08-25. Applies to scheduled commercial banking in India, branch savings accounts for resident individuals.
Control: Approval timing gate. Owner: Branch Operations. Frequency: every account activation.
Operations records an approval timestamp strictly earlier than the activation timestamp for every branch savings account opened for a resident individual. The onboarding system blocks activation until the approval timestamp is recorded. The export contains case_id, review_at (approval timestamp), activation_at (activation timestamp), branch, product, customer_category and reviewer (approving employee name). Expected evidence is the complete activation export for the supplied sample period. This document describes a fictional control; its operation must be tested using evidence.
''')
write('meridian-onboarding-policy-v2.txt','MERIDIAN DEMO BANK — FICTIONAL INTERNAL POLICY, VERSION 2\nEffective from 2026-09-01 until 2026-12-31.\nControl name: Record an approval timestamp strictly earlier than activation.\nControl scope: For every branch savings account opened for a resident individual by a scheduled commercial bank in India, Operations records an approval timestamp strictly earlier than the activation timestamp. This control operates from 2026-09-01 until 2026-12-31.\nControl owner: Branch Operations. Frequency: every account activation.\nExpected evidence: a CSV export for the supplied period containing case_id, review_at (approval timestamp), activation_at (activation timestamp), branch, product, customer_category, and reviewer (approving employee name). Compare the two timestamps for each case; approval must be strictly earlier than activation. The policy describes a fictional control. Its operation must be established from the sample, not this document alone.\n')
write('meridian-kyc-policy-gap.txt','''MERIDIAN DEMO BANK — FICTIONAL INTERNAL POLICY
Customer identification procedure, version 1. Owner: Branch Operations.
The branch opens resident-individual savings accounts using a customer-submitted application form. Operations reviews the customer identification document within two working days after commencement of the account relationship. No pre-commencement identification evidence is supplied with this policy. Other KYC requirements and channels are outside this demonstration excerpt.
''')
# Preserve an exact, bounded official paragraph; original bytes and page mapping remain available.
manifest=json.loads((ROOT/'data/manifest.json').read_text());original=next(s for s in manifest['sources'] if s['id']=='rbi-commercial-banks-kyc')
raw=(ROOT/original['artifact_path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==original['sha256']
reader=PdfReader(io.BytesIO(raw));needle='21. The bank shall undertake identification of customers in the following cases:'
for n,page in enumerate(reader.pages,1):
 text=page.extract_text()
 if needle in text:
  start=text.index(needle);end=text.index('(2)',start);quote=text[start:end].rstrip();break
else:raise RuntimeError('Official paragraph not found')
write('rbi-customer-identification-excerpt.txt',quote+'\n')
provenance={'label':'Official RBI excerpt · pinned snapshot','publisher':'Reserve Bank of India','original_sha256':original['sha256'],'original_page':str(n),'original_start':str(start),'original_end':str(start+len(quote)),'quote':quote,'source_url':original['landing_url'],'retrieved_at':original['retrieved_at'],'current_law_status':'Pinned 2026-09-29 snapshot; legal currency not exhaustively verified','scope':'Paragraph 21 introduction and clause (1) only; other triggers and the rest of the directions are outside this excerpt.'}
write('official-provenance.json',json.dumps(provenance,indent=2)+'\n')
write('DATA-DICTIONARY.md','''# Meridian presentation evidence

All bank records and people in the evidence CSV are fictional. No customer identifiers or real account data are present.

The 40-row September 2026 sample covers branch MUM-01, savings, resident individuals. `case_id` is a stable fictional case reference. `review_at` and `activation_at` are ISO timestamps with UTC timezone. `reviewer` is a fictional employee name. These records test timestamp order, not identity authenticity or bank-wide compliance.

The original sample deliberately contains four late approvals (0007, 0014, 0021, 0028) and one equal timestamp (0035). All five fail a strictly-earlier procedure. Corrected evidence retains every case and moves those five approvals 45 minutes before activation. Expected counts must be checked against the actual stored procedure and executor result; they are not prefilled assessment outcomes.

The two simulated instruments deliberately impose incompatible timestamp ordering in the same date range. They are fictional teaching examples, not RBI regulations. The official excerpt is separately labelled and maps to the original stored RBI PDF through official-provenance.json. Do not infer that all RBI KYC requirements have been assessed.
''')
users_path=ROOT/'.local/demo-users.json';users=json.loads(users_path.read_text())
for username,display,tenant,role in [('maya.analyst','Maya Nair','meridian-demo','analyst'),('arjun.operations','Arjun Rao','meridian-demo','analyst'),('kavya.reviewer','Kavya Menon','meridian-demo','reviewer'),('boundary.reviewer','Boundary Reviewer','meridian-boundary','reviewer')]:
 if not any(u['username']==username for u in users):users.append({'username':username,'display_name':display,'tenant_id':tenant,'role':role,'password':secrets.token_urlsafe(18)})
write_private(users_path,json.dumps(users,indent=2)+'\n')
files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(P.iterdir()) if p.is_file() and p.name!='manifest.json'}
write('manifest.json',json.dumps({'version':'meridian-v1','tenant':'meridian-demo','files':files,'expected_original_rows':40,'expected_timestamp_failures':5,'data_classification':'Fictional bank data; official excerpt separately attributed; regulatory simulations are not law.'},indent=2)+'\n')
print('Prepared versioned input pack and additive demo accounts. Credentials remain in .local/demo-users.json.')
