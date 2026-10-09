// Isolated test server only; model decisions are explicit mocks.
import {chromium,expect} from '@playwright/test';
import {resolve} from 'node:path';
import {mkdirSync,writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const origin=process.env.REGTRACE_TEST_ORIGIN;
if(!origin||!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)||origin.endsWith(':8000'))throw new Error('Isolated test origin required');
const out=resolve(import.meta.dirname,'../artifacts/scenarios');mkdirSync(out,{recursive:true});
const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
try{
 await page.goto(origin);await page.getByLabel('Username',{exact:true}).fill('analyst');await page.getByLabel('Password',{exact:true}).fill('test-password-only');await page.getByRole('button',{name:'Sign in'}).click();
 await page.getByRole('link',{name:'What-if scenarios',exact:true}).click();await page.getByText('Create a what-if scenario',{exact:true}).click();
 const before=await (await page.request.get(origin+'/api/assessments')).json();
 const option=page.getByLabel('Baseline assessment run',{exact:true}).locator('option').nth(1);await expect(option).toHaveCount(1);await page.getByLabel('Baseline assessment run',{exact:true}).selectOption(await option.getAttribute('value'));
 await page.getByLabel('Scenario name',{exact:true}).fill('Synthetic corrected evidence');await page.getByLabel('Hypothesis',{exact:true}).fill('Move C1 verification earlier; keep the original finding.');
 await page.getByLabel('Hypothetical evidence CSV',{exact:true}).setInputFiles({name:'corrected.csv',mimeType:'text/csv',buffer:Buffer.from('case_id,review_at,activation_at\nC1,2026-09-02T09:00:00Z,2026-09-02T10:00:00Z\n')});
 await page.getByText('Change bank profile or evidence metadata',{exact:true}).click();
 await page.getByLabel('Evidence producer',{exact:true}).fill('Synthetic browser sample');await page.getByLabel('Evidence scope',{exact:true}).fill('Synthetic corrected C1 sample');
 for(const [label,value] of [['Collected at','2026-09-03T00:00:00Z'],['Period start','2026-09-01T00:00:00Z'],['Period end','2026-09-03T00:00:00Z']])await page.getByLabel(label,{exact:true}).fill(value);
 await page.getByRole('button',{name:'Run hypothetical assessment',exact:true}).click();
 await page.getByRole('heading',{name:'Baseline and hypothetical outcomes',exact:true}).waitFor({timeout:30000});
 await expect(page.getByText('Hypothetical · completed',{exact:true})).toBeVisible();
 assert.deepEqual(await (await page.request.get(origin+'/api/assessments')).json(),before);
 const list=await (await page.request.get(origin+'/api/scenarios')).json(),result=await (await page.request.get(origin+'/api/scenarios/'+list[0].id)).json();
 const row=result.result.comparison.rows[0];assert.equal(row.before.states.test,'fail');assert.equal(row.after.states.test,'pass');
 const evidence=result.result.entities.find(e=>e.kind==='evidence');assert.equal((await page.request.get(origin+'/api/scenarios/'+list[0].id+'/artifacts/'+evidence.id)).status(),200);
 await page.screenshot({path:resolve(out,'desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:resolve(out,'mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);writeFileSync(resolve(out,'browser.json'),JSON.stringify({provider:'mock-groq',liveProviderTested:false,checks:['scenario creation','real CSV fail-to-pass recomputation','unchanged live baseline','scenario-only artifact access','desktop/mobile layout'],pageErrors:errors},null,2));
}finally{await browser.close()}
