// Isolated synthetic source pairs with mocked model roles; no live-provider claim.
import {chromium,expect} from '@playwright/test';
import {resolve} from 'node:path';
import {mkdirSync,writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const origin=process.env.REGTRACE_TEST_ORIGIN;
if(!origin||!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)||origin.endsWith(':8000'))throw new Error('Isolated test origin required');
const out=resolve(import.meta.dirname,'../artifacts/cross-regulation');mkdirSync(out,{recursive:true});
const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
page.on('pageerror',e=>errors.push(e.message));
try{
 await page.goto(origin);await page.getByLabel('Username',{exact:true}).fill('reviewer');await page.getByLabel('Password',{exact:true}).fill('test-password-only');await page.getByRole('button',{name:'Sign in'}).click();
 await page.getByRole('link',{name:'Cross-regulation',exact:true}).click();await page.getByText('Compare two regulatory instruments',{exact:true}).click();
 const before=await (await page.request.get(origin+'/api/assessments')).json();
 const options=page.getByLabel('First assessment run',{exact:true}).locator('option');await expect(options).toHaveCount(3);
 await page.getByLabel('First assessment run',{exact:true}).selectOption(await options.nth(1).getAttribute('value'));
 await page.getByLabel('Second assessment run',{exact:true}).selectOption(await options.nth(2).getAttribute('value'));
 await page.getByLabel('Comparison name',{exact:true}).fill('Synthetic distinct instruments');await page.getByLabel('Comparison purpose',{exact:true}).fill('Compare shared control passages while retaining account scopes.');
 await page.getByLabel('First instrument name',{exact:true}).fill('Synthetic instrument one');await page.getByLabel('Second instrument name',{exact:true}).fill('Synthetic instrument two');
 await page.getByLabel('I confirm these are distinct instruments, not versions of the same regulation.',{exact:true}).check();
 await page.getByRole('button',{name:'Compare regulations',exact:true}).click();await page.getByRole('heading',{name:'overlap candidate',exact:true}).waitFor({timeout:30000});
 await page.getByRole('tab',{name:'Shared controls',exact:true}).click();await page.getByRole('heading',{name:'Shared policy-control passages',exact:true}).waitFor();await page.getByText('Identical policy artifact and quoted offsets/text; coverage and outcomes stay separate.',{exact:false}).waitFor();
 await page.getByRole('tab',{name:'Review',exact:true}).click();await page.getByLabel('Relationship review note',{exact:true}).fill('Reviewed each instrument and preserved the independent sample outcomes.');await page.getByRole('button',{name:'Save relationship review',exact:true}).click();await page.getByText('approved',{exact:true}).waitFor();
 assert.deepEqual(await (await page.request.get(origin+'/api/assessments')).json(),before);
 const list=await (await page.request.get(origin+'/api/cross-regulation')).json(),d=await (await page.request.get(origin+'/api/cross-regulation/'+list[0].id)).json();
 assert.equal(d.reviews[0].decision,'approved');assert.deepEqual(new Set([d.result.pairs[0].left.states.test,d.result.pairs[0].right.states.test]),new Set(['pass','fail']));
 await page.screenshot({path:resolve(out,'desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:resolve(out,'mobile.png'),fullPage:true});
 assert.deepEqual(errors,[]);writeFileSync(resolve(out,'browser.json'),JSON.stringify({provider:'mock-groq',liveProviderTested:false,checks:['distinct instrument selection','source-linked overlap candidate','shared policy passage','independent fail/pass outcomes','reviewer relationship decision','unchanged original assessments','desktop/mobile layout'],pageErrors:errors},null,2));
}finally{await browser.close()}
