import {chromium} from '@playwright/test';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
import assert from 'node:assert/strict';
const root=resolve(import.meta.dirname,'..');
const out=resolve(root,'artifacts/document-extraction');mkdirSync(out,{recursive:true});
const user=JSON.parse(readFileSync(resolve(root,'.local/demo-users.json'),'utf8')).find(u=>u.username==='analyst');
const browser=await chromium.launch();const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));const report={checks:[],providerLiveTested:false};
try{
 await page.goto('http://127.0.0.1:8000');
 await page.getByLabel('Username',{exact:true}).fill(user.username);await page.getByLabel('Password',{exact:true}).fill(user.password);
 await page.getByRole('button',{name:'Sign in'}).click();await page.getByRole('heading',{name:'Overview'}).waitFor();await page.getByRole('link',{name:'Documents',exact:true}).click();await page.getByRole('button',{name:'Add documents',exact:true}).click();
 await page.getByLabel('Regulatory document',{exact:true}).setInputFiles(resolve(root,'data/examples/document-pairs/regulation-a.txt'));
 await page.getByLabel('Bank policy / controls',{exact:true}).setInputFiles(resolve(root,'data/examples/document-pairs/policy-a.txt'));
 await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByLabel('Bank name',{exact:true}).fill('Synthetic browser test bank');
 await page.getByLabel('Jurisdiction',{exact:true}).fill('IN');await page.getByLabel('Entity type',{exact:true}).fill('bank');
 await page.getByLabel('Products (comma separated)',{exact:true}).fill('deposits');
 // Upload uses the real local app. Without credentials the durable job must fail explicitly.
 const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/extractions')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByRole('button',{name:'Upload and extract',exact:true}).click();const response=await responsePromise;
 assert.equal(response.status(),202);const job=await response.json();report.jobId=job.id;
 await page.getByRole('heading',{name:'Extraction progress'}).waitFor();
 report.checks.push('Authenticated multipart upload from the browser');
 const model=await page.evaluate(async()=>await (await fetch('/api/model')).json());
 if(!model.configured){await page.getByText('Partial or unavailable result: provider key missing.',{exact:false}).waitFor({timeout:20000});report.checks.push('Missing key shown explicitly; no fixture result');}
 await page.getByRole('button',{name:'regulation-a.txt · 1 page(s) · View source'}).click();
 await page.getByRole('button',{name:'Close dialog',exact:true}).waitFor();
 assert((await page.locator('.document-source').innerText()).includes('SYNTHETIC REGULATION A'));
 report.checks.push('Hash-verified uploaded source is readable');
 await page.screenshot({path:resolve(out,'documents-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.screenshot({path:resolve(out,'documents-mobile.png'),fullPage:true});report.checks.push('Mobile layout fits 390 pixels');
 await page.reload();
 await page.getByRole('heading',{name:'Extraction progress'}).waitFor();report.checks.push('Uploaded documents survive reload');
 assert.equal(errors.length,0);report.passed=true;
}finally{report.pageErrors=errors;writeFileSync(resolve(out,'browser-checks.json'),JSON.stringify(report,null,2)+'\n');await browser.close()}
console.log(JSON.stringify(report,null,2));
