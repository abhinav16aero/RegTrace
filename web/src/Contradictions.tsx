import {useNames,useSelection,Back,CreateAction,Tabs,Badge,Modal,useQuery,Technical} from './presentation';
import {useEffect, useState, type FormEvent} from 'react';

type Run = {id:string; name:string; status:string; error_code?:string|null};
type Citation = {id:string; document_id:string; start:number; end:number; text:string; page?:number|null};
type DateEvidence = {value:string; literal:string; citation_id:string};
type Period = {start:DateEvidence|null; end:DateEvidence|null; open_end_citation_id:string|null; reason:string};
type Side = {assessment_id:string; states:Record<string,string>; spans:Citation[]; additional_context:Citation[]};
type Pair = {pair_id:string; finding:string; gate_reason:string; period_states:Record<string,string>; left:Side; right:Side;
  proposal:{relation:string; rationale:string; scope:string; scope_reason:string; unresolved_context:string[]; left_period:Period; right_period:Period};
  verification:{scope_supported:boolean; periods_supported:boolean; relation_supported:boolean; reason:string}};
type Review = {pair_id:string; revision:number; decision:string; note:string; actor_id:string};
type Snapshot = {entities:{id:string; kind:string; title?:string}[]};
type Detail = {job:Run; name:string; note:string; as_of:string; comparison_id:string; instruments:string[]; instrument_identity:string;
  source_stale:boolean; source_review_changed:boolean; snapshots:Snapshot[]; reviews:Review[]; model_calls:unknown[];
  result:{pairs:Pair[]; interpretation:string; extraction_unresolved_context:string[][]}|null};
type Comparison = {instruments:string[]; snapshots:Snapshot[]};
const human = (s:string) => s.replaceAll('_',' ');
async function api<T>(path:string, csrf:string, body?:unknown):Promise<T> {
  const r=await fetch('/api'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body?JSON.stringify(body):undefined});
  const d=await r.json(); if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'Check the analysis inputs'); return d;
}
function EffectivePeriod({period,state,prefix}:{period:Period;state:string;prefix:string}) {
  return <div><p><strong>Effective on analysis date: {human(state)}</strong></p>
    <p>From: {period.start?.value||'Unknown'} · Through: {period.end?.value||(period.open_end_citation_id?'Explicitly open-ended':'Unknown')}</p>
    <p>{period.reason}</p>
    {[period.start,period.end].filter((b):b is DateEvidence=>b!==null).map((b,i)=><p key={i}>Date evidence: “{b.literal}” → {b.value} · <a href={'#citation-'+prefix+b.citation_id} onClick={e=>{e.preventDefault();document.getElementById('citation-'+prefix+b.citation_id)?.scrollIntoView({block:'center'})}}>View date quotation</a></p>)}
    {period.open_end_citation_id&&<p>Open-ended evidence: <a href={'#citation-'+prefix+period.open_end_citation_id} onClick={e=>{e.preventDefault();document.getElementById('citation-'+prefix+period.open_end_citation_id)?.scrollIntoView({block:'center'})}}>View period quotation</a></p>}
  </div>;
}
export function Contradictions({csrf,role}:{csrf:string;role:string}) {
 const names=useNames();const [selected,setSelected]=useSelection();
  const [runs,setRuns]=useState<Run[]>([]),[comparisons,setComparisons]=useState<Run[]>([]),[comparisonId,setComparisonId]=useState('');
  const [comparison,setComparison]=useState<Comparison|null>(null),[detail,setDetail]=useState<Detail|null>(null);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{
    setDetail(null);setError('');let active=true;
    const refresh=async()=>{try{
      const [r,c]=await Promise.all([api<Run[]>('/contradictions',csrf),api<Run[]>('/cross-regulation',csrf)]);
      if(!active)return;setRuns(r);setComparisons(c.filter(v=>v.status==='completed'));
      if(selected){const d=await api<Detail>('/contradictions/'+selected,csrf);if(active)setDetail(d)}
    }catch(e){if(active)setError((e as Error).message)}};
    void refresh();const timer=setInterval(refresh,2500);return()=>{active=false;clearInterval(timer)};
  },[csrf,selected]);
  useEffect(()=>{
    let active=true;setComparison(null);
    if(comparisonId)void api<Comparison>('/cross-regulation/'+comparisonId,csrf).then(d=>{if(active)setComparison(d)}).catch(e=>{if(active)setError(e.message)});
    return()=>{active=false};
  },[comparisonId,csrf]);
  async function create(e:FormEvent<HTMLFormElement>) {
    e.preventDefault();const f=new FormData(e.currentTarget);const body:Record<string,unknown>={comparison_id:comparisonId};
    for(const key of ['name','note','as_of','left_context','right_context'])body[key]=f.get(key);
    setBusy(true);setError('');
    try{const j=await api<Run>('/contradictions',csrf,body);setSelected(j.id);setDetail(await api<Detail>('/contradictions/'+j.id,csrf))}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  async function review(e:FormEvent<HTMLFormElement>,pair:Pair,revision:number) {
    e.preventDefault();const f=new FormData(e.currentTarget);setBusy(true);setError('');
    try{setDetail(await api<Detail>('/contradictions/'+selected+'/pairs/'+pair.pair_id+'/reviews',csrf,{decision:f.get('decision'),note:f.get('note'),expected_revision:revision}))}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <div className="document-workspace">
    {error&&<p role="alert" className="error">{error}</p>}
    {!selected&&<><CreateAction error={error} label="Check potentially conflicting duties">
      <p>Select a completed cross-regulation comparison and an analysis date. Every obligation pair is checked, including pairs previously marked distinct or uncertain. Different scopes, nonconcurrent dates and stricter but compatible duties are considered separately.</p>
      {!comparisons.length&&<p>Complete a comparison in Cross-regulation first.</p>}
      <form className="document-form" onSubmit={create}>
        <label>Source comparison<select aria-label="Source comparison" value={comparisonId} required onChange={e=>setComparisonId(e.target.value)}><option value="">Select a completed comparison</option>{comparisons.map(c=><option value={c.id} key={c.id}>{c.name}</option>)}</select></label>
        <label>Analysis name<input name="name" required maxLength={200}/></label>
        <label>Analysis purpose<textarea name="note" required maxLength={3000}/></label>
        <label>Analysis date<input name="as_of" type="date" required/></label>
        <p>Optional: paste an exact passage from each regulation containing relevant effective dates, exceptions or definitions. Use a passage long enough to identify it uniquely. Missing operative dates remain unknown; publication dates do not establish when a duty applies.</p>
        <div className="document-fields">{['left','right'].map((side,n)=><div key={side}>
          <label>{n===0?'First':'Second'} instrument source context{comparison?' · '+comparison.instruments[n]:''}<textarea aria-label={(n===0?'First':'Second')+' instrument source context'} name={side+'_context'} maxLength={8000}/></label>
          {comparison?.snapshots[n].entities.filter(e=>e.kind==='source').map(e=><p key={e.id}><a target="_blank" rel="noreferrer" href={'/api/cross-regulation/'+comparisonId+'/artifacts/'+encodeURIComponent(e.id)}>Read {e.title||names.name(e.id,e.kind)}</a></p>)}
        </div>)}</div>
        <button className="primary" disabled={busy||!comparisonId}>Analyse contradictions</button>
      </form>
    </CreateAction>
    <section className="panel"><h2>Saved contradiction analyses</h2>{!runs.length&&<p>No contradiction analyses yet.</p>}
      <div className="document-runs">{runs.map(r=><button key={r.id} aria-pressed={r.id===selected} onClick={()=>{setSelected(r.id);setDetail(null)}}>{r.name} · {human(r.status)}</button>)}</div>
    </section>
    </>}{selected&&<Back onClick={()=>{setSelected('');setDetail(null)}}/>}
 {selected&&detail&&<>
      <section className="panel"><h1>{detail.name}</h1><p>Analysis date: {detail.as_of} · {detail.instruments.join(' ↔ ')}</p><small className="muted">{detail.instrument_identity}</small>
        <p>Status: {human(detail.job.status)}{detail.job.error_code?' · '+human(detail.job.error_code):''}</p>
        {detail.source_stale&&<p className="error">Source findings are stale. Historical reviews remain visible; acceptance is blocked.</p>}
        {detail.source_review_changed&&<p className="error">Source reviews changed since the comparison was captured. Create an updated comparison before accepting this finding.</p>}
        
      </section>
<Tabs items={[
 {id:'summary',label:'Summary',content:<>      {detail.result?.pairs.map(pair=>{
        const history=detail.reviews.filter(r=>r.pair_id===pair.pair_id),latest=history.at(-1);
        const blocked=detail.source_stale||detail.source_review_changed||pair.finding==='uncertain';
        return <section className="panel" key={pair.pair_id}><h2>{human(pair.finding)}</h2><p>{pair.gate_reason}</p>
<div className="table-wrap"><table><thead><tr><th>Basis</th><th>Result</th></tr></thead><tbody><tr><td>Shared scope</td><td>{human(pair.proposal.scope)} · {pair.verification.scope_supported?'supported':'unsupported'}</td></tr><tr><td>Effective periods</td><td>{detail.instruments.map((name,n)=><div key={name}>{name}: <Badge value={pair.period_states[n?'right':'left']}/></div>)}</td></tr><tr><td>Source verification</td><td>{[pair.left,pair.right].map((side,n)=><div key={n}>{detail.instruments[n]}: <Badge value={side.states.verification}/></div>)}</td></tr></tbody></table></div>
          <details><summary>Model proposal and verification</summary><p><strong>Model proposal: {human(pair.proposal.relation)}</strong> · {pair.proposal.rationale}</p>
          <p>Scope: {human(pair.proposal.scope)} · {pair.proposal.scope_reason}</p>
          <p>Verifier: scope {pair.verification.scope_supported?'supported':'unsupported'}, dates {pair.verification.periods_supported?'supported':'unsupported'}, relationship {pair.verification.relation_supported?'supported':'unsupported'}.</p><p>{pair.verification.reason}</p></details>
          {pair.proposal.unresolved_context.length>0&&<details><summary>Unresolved context ({new Set(pair.proposal.unresolved_context).size})</summary><ul>{Array.from(new Set(pair.proposal.unresolved_context)).map((v,i)=><li key={i}>{v}</li>)}</ul></details>}
        </section>;
      })}
</>},
 {id:'sources',label:'Source comparison',content:<>      {detail.result?.pairs.map(pair=>{
        const history=detail.reviews.filter(r=>r.pair_id===pair.pair_id),latest=history.at(-1);
        const blocked=detail.source_stale||detail.source_review_changed||pair.finding==='uncertain';
        return <section className="panel" key={pair.pair_id}><h2>{human(pair.finding)}</h2><p>{pair.gate_reason}</p>
          <div className="document-fields">{(['left','right'] as const).map((side,n)=><article key={side}>
            <h3>{detail.instruments[n]}</h3>
            <EffectivePeriod prefix={pair.pair_id+'-'} period={pair.proposal[side+'_period' as 'left_period'|'right_period']} state={pair.period_states[side]}/>
            {[...pair[side].spans,...pair[side].additional_context].map(c=><blockquote key={c.id} id={'citation-'+pair.pair_id+'-'+c.id}>{c.text}<small> · {c.page?'page '+c.page+' · ':''}characters {c.start}–{c.end} · exact source quotation</small></blockquote>)}
            <details><summary>Original assessment states</summary><dl>{Object.entries(pair[side].states).map(([k,v])=><div key={k}><dt>{human(k)}</dt><dd>{human(v)}</dd></div>)}</dl></details>
          </article>)}</div>
        </section>;
      })}
      <section className="panel"><h2>Original source artifacts</h2><p>{detail.note}</p><p>{detail.result?.interpretation}</p>        <details><summary>Source context needing review</summary>{detail.result?.extraction_unresolved_context.map((items,n)=>items.length>0&&<div key={n}><h3>Unresolved source context · {detail.instruments[n]}</h3><ul>{Array.from(new Set(items)).map((item,i)=><li key={i}>{item}</li>)}</ul></div>)}
        </details><details><summary>Model call metadata</summary><pre className="document-source">{JSON.stringify(detail.model_calls,null,2)}</pre></details>
{detail.snapshots.map((s,n)=><div key={n}><h3>{detail.instruments[n]}</h3>{s.entities.filter(e=>e.kind==='source').map(e=><p key={e.id}><a href={'/api/contradictions/'+selected+'/artifacts/'+encodeURIComponent(e.id)} target="_blank" rel="noreferrer">Read {e.title||names.name(e.id,e.kind)}</a></p>)}</div>)}</section>
</>},
 {id:'review',label:'Review',content:<>      {detail.result?.pairs.map(pair=>{
        const history=detail.reviews.filter(r=>r.pair_id===pair.pair_id),latest=history.at(-1);
        const blocked=detail.source_stale||detail.source_review_changed||pair.finding==='uncertain';
        return <section className="panel" key={pair.pair_id}><h2>{human(pair.finding)}</h2><p>{pair.gate_reason}</p>
          <h3>Finding review</h3><p>{blocked?'Acceptance unavailable':latest?human(latest.decision):'Pending human review'}</p>
          <p>Accepting this finding records your interpretation of the supplied sources. It does not resolve the conflict, set legal precedence or change compliance outcomes.</p>
          {role==='reviewer'&&<form className="document-form" onSubmit={e=>void review(e,pair,latest?.revision||0)}>
            <label>Finding decision<select aria-label="Finding decision" name="decision" defaultValue={blocked?'changes_requested':'approved'}><option value="approved" disabled={blocked}>Accept finding</option><option value="changes_requested">Request changes</option><option value="rejected">Reject finding</option></select></label>
            <label>Finding review note<textarea name="note" required maxLength={2000}/></label><button disabled={busy}>Save finding review</button>
          </form>}
          {history.map(r=><p key={r.revision}>{r.revision} · {human(r.decision)} · {r.note} · {names.person(r.actor_id)}</p>)}
        </section>;
      })}
</>}
 ]}/>
    </>}
  </div>;
}
