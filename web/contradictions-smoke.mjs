// Isolated synthetic inputs and mocked model judgments; never a live-provider claim.
import {chromium,expect} from '@playwright/test';
import {resolve} from 'node:path';
import {mkdirSync,writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const origin=process.env.REGTRACE_TEST_ORIGIN;
if(!origin||!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)||origin.endsWith(':8000'))throw new Error('Isolated test origin required');
const out=resolve(import.meta.dirname,'../artifacts/contradictions');mkdirSync(out,{recursive:true});
const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
try{
 await page.goto(origin);await page.getByLabel('Username',{exact:true}).fill('reviewer');await page.getByLabel('Password',{exact:true}).fill('test-password-only');await page.getByRole('button',{name:'Sign in'}).click();
 await page.getByRole('link',{name:'Contradictions',exact:true}).click();await page.getByText('Check potentially conflicting duties',{exact:true}).click();
 const before=await (await page.request.get(origin+'/api/assessments')).json();
 const options=page.getByLabel('Source comparison',{exact:true}).locator('option');await expect(options).toHaveCount(2);
 await page.getByLabel('Source comparison',{exact:true}).selectOption(await options.nth(1).getAttribute('value'));
 await page.getByLabel('Analysis name',{exact:true}).fill('Synthetic contradiction check');await page.getByLabel('Analysis purpose',{exact:true}).fill('Check the cited duties within their shared scope and operative periods.');
 await page.getByLabel('Analysis date',{exact:true}).fill('2026-06-01');
 await page.getByLabel('First instrument source context',{exact:true}).fill('This duty is effective from 2025-01-01 through 2026-12-31.');
 await page.getByLabel('Second instrument source context',{exact:true}).fill('This duty is effective from 2026-01-01 through 2027-12-31.');
 await page.getByRole('button',{name:'Analyse contradictions',exact:true}).click();
 await page.getByRole('heading',{name:'conflict candidate',exact:true}).waitFor({timeout:30000});
 await page.getByRole('tab',{name:'Source comparison',exact:true}).click();await expect(page.getByText('Effective on analysis date: active',{exact:true})).toHaveCount(2);
 await page.getByRole('tab',{name:'Review',exact:true}).click();await page.getByLabel('Finding review note',{exact:true}).fill('Reviewed the opposing duties, account scope and operative dates. Separate legal resolution is still required.');
 await page.getByRole('button',{name:'Save finding review',exact:true}).click();await page.getByText('approved',{exact:true}).waitFor();
 assert.deepEqual(await (await page.request.get(origin+'/api/assessments')).json(),before);
 const list=await (await page.request.get(origin+'/api/contradictions')).json();
 const d=await (await page.request.get(origin+'/api/contradictions/'+list[0].id)).json();
 assert.equal(d.result.pairs[0].finding,'conflict_candidate');assert.equal(d.reviews[0].decision,'approved');
 await page.screenshot({path:resolve(out,'desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:resolve(out,'mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);writeFileSync(resolve(out,'browser.json'),JSON.stringify({provider:'mock-groq',liveProviderTested:false,checks:['completed source comparison','explicit analysis date','exact additional source context','cited conflict candidate','separate operative periods','reviewer decision','unchanged original assessments','desktop/mobile layout'],pageErrors:errors},null,2));
}finally{await browser.close()}
