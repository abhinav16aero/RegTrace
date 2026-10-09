import {useNames,useSelection,Icon,Badge,request,Tabs,Modal,Back,useQuery,CreateAction,CsvTable} from './presentation';
import {useEffect,useState,type FormEvent} from 'react';

type Job={id:string;kind:string;status:string;error_code:string|null};
type Citation={document_id:string;start:number;end:number;pages:number[];text:string};
type Item={id:string;kind:string;citations:Citation[];[key:string]:unknown};
type Result={job:Job;complete:boolean;checked_chunks:number;items:Item[];unresolved_context:string[];
 inputs:{synthetic:boolean;documents:{id:string;filename:string;characters:number;pages:unknown[];sha256:string}[];evidence:{rows:number}|null};
 model_calls:{metadata:{provider:string;model:string;latency_ms:number;error:string|null;usage?:{total_tokens?:number}}}[]};

async function get<T>(path:string):Promise<T>{
 const r=await fetch('/api'+path);if(!r.ok)throw new Error('Unable to read document run');return r.json();
}
export function Documents({csrf,onAssessmentQueued}:{csrf:string;onAssessmentQueued:()=>void}){
 const names=useNames();const [selected,setSelected]=useSelection();
 const [jobs,setJobs]=useState<Job[]>([]),[result,setResult]=useState<Result|null>(null);
 const [provider,setProvider]=useState<{configured:boolean;model:string}|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [useEvidence,setUseEvidence]=useState(false);const [search,setSearch]=useQuery('search');const [type,setType]=useQuery('type');const [docId]=useQuery('document');const [preview,setPreview]=useState('');const [step,setStep]=useState(1);const [confirmation,setConfirmation]=useState<[string,string][]>([]);const [adding,setAdding]=useState(new URLSearchParams(location.hash.split('?')[1]).get('add')==='1');
 const [source,setSource]=useState<{id:string;name:string;text:string;start?:number;end?:number}|null>(null);
 useEffect(()=>{let active=true;const refresh=async()=>{try{
  const [j,p]=await Promise.all([get<Job[]>('/jobs'),get<{configured:boolean;model:string}>('/model')]);
  if(!active)return;setJobs(j.filter(v=>v.kind==='extract_documents'));setProvider(p);
  if(selected){const r=await get<Result>('/extractions/'+selected);if(active)setResult(r)}
 }catch(e){if(active)setError((e as Error).message)}};void refresh();const timer=setInterval(refresh,2000);return()=>{active=false;clearInterval(timer)}},[selected]);
 const documentRecord=names.documents.find(d=>d.id===docId&&d.usages.some(u=>u.job_id===selected));
 useEffect(()=>{setPreview('');if(!selected||!docId||!documentRecord)return;setError('');let active=true;void fetch('/api/extractions/'+selected+(documentRecord?.role==='evidence'?'/evidence':'/sources/'+docId)).then(async r=>{if(!r.ok)throw new Error('Source unavailable or integrity check failed');const text=await r.text();if(active)setPreview(text)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[selected,docId,documentRecord?.role]);
 function chooseDocument(job:string,id:string){setResult(null);const p=new URLSearchParams(location.hash.split('?')[1]||'');p.set('record',job);p.set('document',id);p.delete('tab');location.hash='documents?'+p.toString()}
 async function upload(event:FormEvent<HTMLFormElement>){
  event.preventDefault();const formElement=event.currentTarget;
  if(step<3){const invalid=Array.from(formElement.querySelectorAll<HTMLInputElement>('fieldset:not([hidden]) input')).find(input=>!input.checkValidity());if(invalid){invalid.reportValidity();return}if(step===2)setConfirmation(Array.from(new FormData(formElement).entries()).map(([key,v])=>[key,v instanceof File?v.name:String(v)] as [string,string]).filter(([,v])=>Boolean(v)));setStep(step+1);return}
  const form=new FormData(formElement);
  const list=(key:string)=>String(form.get(key)||'').split(',').map(v=>v.trim()).filter(Boolean);
  const profile={name:form.get('name'),jurisdiction:form.get('jurisdiction'),entity_type:form.get('entity_type'),
   products:list('products'),channels:list('channels'),customer_categories:list('customer_categories'),unknown_facts:list('unknown_facts')};
  for(const key of Object.keys(profile))form.delete(key);
  form.set('profile',JSON.stringify(profile));form.set('synthetic',form.get('synthetic')==='on'?'true':'false');
  setBusy(true);setError('');try{
   const r=await fetch('/api/extractions',{method:'POST',headers:{'X-CSRF-Token':csrf},body:form});const data=await r.json();
   if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Upload failed');setSelected(data.id);setResult(null);setAdding(false);setStep(1);
  }catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function assess(event:FormEvent<HTMLFormElement>){
  event.preventDefault();const form=new FormData(event.currentTarget);setBusy(true);setError('');
  try{
   const context=useEvidence?{producer:form.get('producer'),scope:form.get('scope'),
    collected_at:new Date(String(form.get('collected_at'))).toISOString(),
    period_start:new Date(String(form.get('period_start'))).toISOString(),period_end:new Date(String(form.get('period_end'))).toISOString()}:null;
   const response=await fetch('/api/extractions/'+selected+'/assess',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({evidence_context:context})});
   const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Check the evidence dates and fields.');
   onAssessmentQueued();
  }catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 async function view(docId:string,citation?:Citation){try{
  const r=await fetch('/api/extractions/'+selected+'/sources/'+docId);
  if(!r.ok)throw new Error('Source could not be verified');
  setSource({id:docId,name:names.name(docId,'Source document'),text:await r.text(),start:citation?.start,end:citation?.end});
 }catch(e){setError((e as Error).message)}}
 return <div className="document-workspace">{error&&!adding&&<p role="alert" className="error">{error}</p>}
 {!selected&&<><section className="panel table-panel"><div className="library-toolbar library-inset" ><input aria-label="Search documents" placeholder="Search documents…" value={search} onChange={e=>setSearch(e.target.value)}/><select aria-label="Document type" value={type} onChange={e=>setType(e.target.value)} ><option value="">All document types</option><option value="regulation">Regulations</option><option value="policy">Bank policies</option><option value="evidence">Evidence</option></select><button className="primary" onClick={()=>{setStep(1);setAdding(!adding)}}>{adding?'Close upload':'Add documents'}</button></div><div className="table-wrap"><table><thead><tr><th>Document</th><th>Type</th><th>Source provenance</th><th>Used in</th><th>Action</th></tr></thead><tbody>{names.documents.filter(d=>(!type||d.role===type)&&names.name(d.id,d.filename).toLowerCase().includes(search.toLowerCase())).map(d=><tr key={d.job_id+d.id}><td><div className="record-title"><span className="document-icon"><Icon name="documents"/></span><div><strong>{names.name(d.id,d.filename)}</strong><small>{names.ref(d.id)} · {d.rows?`${d.rows} records`:`${d.pages} pages`}</small></div></div></td><td>{d.role==='policy'?'Bank policy':d.role==='evidence'?'Evidence CSV':'Regulation'}</td><td><span className="small">{names.records[d.id]?.provenance?.label||(d.synthetic?'Demonstration inputs · source unverified':'User supplied · source unverified')}</span></td><td>{d.usage_count} run{d.usage_count===1?'':'s'}</td><td><button onClick={()=>chooseDocument(d.job_id,d.id)}>Open</button></td></tr>)}</tbody></table></div>{!names.documents.length&&<div className="empty"><h2>Your document library starts here</h2><p>Add a regulation and bank policy to build a source-linked assessment.</p></div>}</section>
 {adding&&<Modal title="Add documents" close={()=>setAdding(false)}><div className="upload-intro">{['Files','Bank profile','Confirm'].map((label,i)=><span key={label} aria-current={step===i+1?'step':undefined}>{i+1} · {label}</span>)}</div>{error&&<p role="alert" className="error">{error}</p>}
 <form onSubmit={upload} className="document-form" noValidate><fieldset hidden={step!==1}><legend>Source files</legend><div className="document-fields">
 <label>Regulatory document<input name="regulation" type="file" accept=".pdf,.txt,.md" required/></label>
 <label>Bank policy / controls<input name="policy" type="file" accept=".pdf,.txt,.md" required/></label>
 <label>Evidence CSV (optional)<input name="evidence" type="file" accept=".csv"/></label>
 </div></fieldset><fieldset hidden={step!==2}><legend>Bank profile</legend><div className="document-fields"><label>Bank name<input name="name" required maxLength={200} defaultValue={names.workspace.is_demo?names.workspace.name:undefined}/></label>
 <label>Jurisdiction<input name="jurisdiction" defaultValue={names.workspace.is_demo?"India":undefined} required maxLength={100} placeholder="e.g. India"/></label>
 <label>Entity type<input name="entity_type" defaultValue={names.workspace.is_demo?"scheduled commercial bank":undefined} required maxLength={200} placeholder="e.g. scheduled commercial bank"/></label>
 <label>Products (comma separated)<input name="products" defaultValue={names.workspace.is_demo?"savings accounts":undefined}/></label>
 <label>Customer categories<input name="customer_categories" defaultValue={names.workspace.is_demo?"resident individuals":undefined}/></label>
 <label>Channels<input name="channels" defaultValue={names.workspace.is_demo?"branch":undefined}/></label>
 <label>Unknown profile facts<input name="unknown_facts"/></label>
 </div><label className="document-check"><input name="synthetic" type="checkbox" defaultChecked/> Bank inputs are synthetic / demonstration data</label></fieldset>
 {step===3&&<><p>Extract requirements and controls from these inputs.</p><dl>{confirmation.map(([key,value])=><div key={key}><dt>{key.replaceAll('_',' ')}</dt><dd>{value}</dd></div>)}</dl><p className="small muted">Document text is sent to {provider?.model||'the configured model'}. {provider?.configured?'Connection configured.':'Model connection unavailable.'}</p></>}
 <div className="action-row">{step>1&&<button type="button" onClick={()=>setStep(step-1)}>Back</button>}<button className="primary" disabled={busy}>{step<3?'Continue':busy?'Uploading…':'Upload and extract'}</button></div></form></Modal>}
 <details className="panel"><summary>Extraction history</summary>{jobs.length===0?<p>No document runs yet.</p>:<div className="document-runs">{jobs.map(j=><button key={j.id} onClick={()=>{setSelected(j.id);setResult(null)}} aria-pressed={j.id===selected}>{names.name(j.id,'Document extraction')} · {j.status}{j.error_code?' · '+j.error_code.replaceAll('_',' '):''}</button>)}</div>}</details>
</>}{selected&&<Back label="Document library" onClick={()=>{setSelected('');setResult(null)}}/>}
 {selected&&result&&<><div className="record-heading"><div><small className="muted">{names.ref(docId||selected)}</small><h1>{names.name(docId||selected,'Document run')}</h1><span className="small muted">{docId?(names.records[docId]?.provenance?.label||'User supplied document'):'Extraction run'}</span></div></div><Tabs items={[...(docId?[{id:'preview',label:'Preview',content:<section className="panel">{documentRecord?.role==='evidence'?<CsvTable text={preview}/>:<><button onClick={()=>void view(docId)}>Source details & rename</button><pre className="document-source">{preview||'Loading source…'}</pre></>}</section>}]:[]),{id:'extracted',label:'Extracted items',content:<section className="panel"><h2>{result.complete?'Extracted obligations and controls':'Extraction progress'}</h2>
 <p>{result.checked_chunks} checked chunks · {result.items.length} extracted items · {result.inputs.synthetic?'Synthetic bank inputs':'User-supplied bank inputs'}</p>

 {!result.complete&&<p className="error">Partial or unavailable result: {result.job.error_code?.replaceAll('_',' ')||result.job.status}. Do not treat this as a complete extraction.</p>}
 <div className="document-runs">{result.inputs.documents.map(d=><button key={d.id} onClick={()=>void view(d.id)}>{d.filename} · {d.pages.length} page(s) · View source</button>)}</div>
 {result.inputs.evidence&&<p>{result.inputs.evidence.rows} evidence rows attached · not tested</p>}
 {result.items.filter(item=>!docId||item.citations.some(c=>c.document_id===docId)).map(item=><article className="extracted-item" key={item.id}><span className="eyebrow">{item.kind} · Review pending</span>
 <h3>{item.kind==='control'?String(item.name):[item.actor,item.action,item.object].filter(Boolean).join(' ')}</h3>
 <dl>{Object.entries(item).filter(([key])=>!['id','kind','citations','name','actor','action','object','review','citation_check'].includes(key)).map(([key,value])=><div key={key}><dt>{key.replaceAll('_',' ')}</dt><dd>{Array.isArray(value)?value.join('; ')||'Not specified':String(value)||'Not specified'}</dd></div>)}</dl>
 {item.citations.map((c,i)=><blockquote key={i}><p>{c.text}</p><small>{result.inputs.documents.find(d=>d.id===c.document_id)?.filename} · page(s) {c.pages.join(', ')} · characters {c.start}–{c.end} · exact match</small><button className="source-link" onClick={()=>void view(c.document_id,c)}>Locate in source</button></blockquote>)}</article>)}
 {result.unresolved_context.length>0&&<><h3>Context needing review</h3><ul>{result.unresolved_context.map((v,i)=><li key={i}>{v}</li>)}</ul></>}
 {result.complete&&<CreateAction error={error} label="Assess these documents"><form className="document-form" onSubmit={assess}>
 {result.inputs.evidence&&<><label className="document-check"><input type="checkbox" checked={useEvidence} onChange={e=>setUseEvidence(e.target.checked)}/> Supply evidence context for executable sample tests</label>
 {useEvidence&&<div className="document-fields"><label>Evidence producer<input name="producer" required defaultValue={names.workspace.is_demo?"Meridian Demo Bank operations":undefined}/></label><label>Evidence scope<input name="scope" required defaultValue={names.workspace.is_demo?"Fictional branch onboarding sample, September 2026":undefined} placeholder="Bank, activity and population represented"/></label><label>Collected at (local time)<input name="collected_at" type="datetime-local" required defaultValue={names.workspace.is_demo?"2026-10-01T12:00":undefined}/></label><label>Period start (local time)<input name="period_start" type="datetime-local" required defaultValue={names.workspace.is_demo?"2026-09-01T00:00":undefined}/></label><label>Period end (local time)<input name="period_end" type="datetime-local" required defaultValue={names.workspace.is_demo?"2026-09-30T23:59":undefined}/></label></div>}</>}
 <p className="small muted">Missing or irrelevant evidence remains unassessed. Supported tests check timestamp order and required values; other procedures remain unresolved. Findings and remedies need human review.</p>
 <button className="primary" disabled={busy||result.items.every(i=>i.kind!=='obligation')}>{busy?'Queuing…':'Assess documents and evidence'}</button></form></CreateAction>}
 <details><summary>Model call records ({result.model_calls.length})</summary>{result.model_calls.map((c,i)=><p key={i}>{c.metadata.provider} / {c.metadata.model} · {c.metadata.latency_ms} ms · {c.metadata.usage?.total_tokens??'Unknown'} tokens · {c.metadata.error||'Output validated'}</p>)}</details>
 </section>},{id:'runs',label:'Runs',content:<section className="panel"><h2>Document usage</h2>{documentRecord?documentRecord.usages.map(u=><p key={u.job_id}><button className="text-button" onClick={()=>chooseDocument(u.job_id,docId)}>{names.name(u.job_id,'Extraction run')}</button> · <Badge value={u.status}/> · {new Date(u.created_at).toLocaleDateString()}</p>):<p>Current extraction: <Badge value={result.job.status}/></p>}</section>} ]}/></>}
 {source&&<Modal title={source.name} close={()=>setSource(null)} wide><p className="small muted">{names.records[source.id]?.provenance?.label||'Uploaded document'} · Stored content verified</p>{names.records[source.id]?.provenance?.original_sha256&&<p><a target="_blank" rel="noreferrer" href={'/api/presentation/'+encodeURIComponent(source.id)+'/original#page='+names.records[source.id].provenance.original_page}>Original official PDF · page {names.records[source.id].provenance.original_page}</a><br/>{names.records[source.id].provenance.scope}</p>}<details><summary>Rename document</summary><form onSubmit={async e=>{e.preventDefault();const title=String(new FormData(e.currentTarget).get('title'));try{await request('/presentation/'+encodeURIComponent(source.id)+'/rename','POST',{title},csrf);setSource({...source,name:title});window.dispatchEvent(new Event('presentation-changed'))}catch(e){setError((e as Error).message)}}}><label>Display name<input name="title" defaultValue={source.name} maxLength={240} required/></label><button>Save name</button></form></details><pre className="document-source">{source.start!==undefined?<>{source.text.slice(0,source.start)}<mark>{source.text.slice(source.start,source.end)}</mark>{source.text.slice(source.end)}</>:source.text}</pre></Modal>}
 </div>
}
