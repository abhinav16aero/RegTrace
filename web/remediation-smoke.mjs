// Isolated synthetic finding: mocked model, real CSV retest and database.
import {chromium,expect} from '@playwright/test';
import {resolve} from 'node:path';
import {mkdirSync,writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const origin=process.env.REGTRACE_TEST_ORIGIN;
if(!origin||!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)||origin.endsWith(':8000'))throw new Error('Isolated test origin required');
const out=resolve(import.meta.dirname,'../artifacts/remediation');mkdirSync(out,{recursive:true});
const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
try{
 await page.goto(origin);await page.getByLabel('Username',{exact:true}).fill('reviewer');await page.getByLabel('Password',{exact:true}).fill('test-password-only');await page.getByRole('button',{name:'Sign in'}).click();
 await page.getByRole('link',{name:'Remediation',exact:true}).click();
 await page.getByRole('button',{name:'Correct sample failures',exact:true}).click();
 async function openAction(label){await page.getByRole('button',{name:label,exact:true}).click();await page.getByLabel('Decision or correction note').fill('Reviewed synthetic correction and all acceptance criteria.')}
 await openAction('Assign owner');
 await page.getByLabel('Task owner',{exact:true}).selectOption({label:'reviewer'});await page.getByRole('dialog').getByRole('button',{name:'Assign owner',exact:true}).click();
 await expect(page.locator('.record-heading')).toContainText('Owner: reviewer');
 for(const label of ['Accept task','Start work']){await openAction(label);await page.getByRole('dialog').getByRole('button',{name:label,exact:true}).click();await expect(page.getByRole('dialog')).toHaveCount(0)}
 await openAction('Run retest');
 await page.getByLabel('Revised CSV').setInputFiles({name:'corrected.csv',mimeType:'text/csv',buffer:Buffer.from('case_id,review_at,activation_at\nC1,2026-09-02T09:00:00Z,2026-09-02T10:00:00Z\n')});
 await page.getByLabel('Evidence producer').fill('Synthetic browser sample');await page.getByLabel('Evidence scope').fill('Corrected C1 activation sample');
 for(const [label,value] of [['Collected at','2026-09-03T00:00:00Z'],['Period start','2026-09-01T00:00:00Z'],['Period end','2026-09-03T00:00:00Z']])await page.getByLabel(label,{exact:true}).fill(value);
 await page.getByRole('dialog').getByRole('button',{name:'Run retest',exact:true}).click();await page.getByRole('button',{name:'Close task',exact:true}).waitFor({timeout:30000});await openAction('Close task');
 await page.getByLabel('I reviewed the revised evidence and confirm every acceptance criterion.').check();await page.getByRole('dialog').getByRole('button',{name:'Close task',exact:true}).click();await expect(page.locator('.outcome-strip')).toContainText('closed');
 await page.getByText('Finding and priority',{exact:true}).click();await openAction('Set priority');
 for(const [key,value] of [['severity','5'],['exposure','2'],['urgency','4']]){
  await page.getByLabel(key+' value',{exact:true}).selectOption(value);await page.getByLabel(key+' reason',{exact:true}).fill('Reviewer triage of the cited sample.');
  const select=page.getByLabel(key+' supporting records',{exact:true}),source=await select.locator('option').first().getAttribute('value');await select.selectOption(source);
 }
 await page.getByRole('button',{name:'Save priority'}).click();await page.getByText('78/100 · medium',{exact:true}).waitFor();
 await page.screenshot({path:resolve(out,'desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:resolve(out,'mobile.png'),fullPage:true});
 const tasks=await (await page.request.get(origin+'/api/remediations')).json(),task=tasks.find(t=>t.gap.gap_type==='execution_failure');
 const original=await (await page.request.get(origin+'/api/assessments/'+task.assessment_id)).json();assert.equal(original.current_states.test,'fail');assert.deepEqual(errors,[]);
 writeFileSync(resolve(out,'browser.json'),JSON.stringify({provider:'mock-groq',liveProviderTested:false,checks:['assignment and acceptance','actual revised CSV execution','reviewer closure','explained priority 78/100','original failure preserved','desktop and mobile layout'],pageErrors:errors},null,2));
}finally{await browser.close()}
