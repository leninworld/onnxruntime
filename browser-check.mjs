// Private browser validation; no host browser control or security-disabling flags.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';

const [fixtureDir, lane, outputPath] = process.argv.slice(2);
assert(['reporter','base','candidate'].includes(lane));
assert(!fs.existsSync(outputPath));
const require = createRequire(path.join(path.resolve(fixtureDir), 'package.json'));
const {firefox,chromium} = require('playwright');
const sanitize = value => value.replace(/([?&](?:token|access_token|key)=)[^&\s]*/gi,'$1[REDACTED]');
const expected=[700,800,900,1580,1840,2100,2460,2880,3300];
const observations=[];
let failed=false;
try {
  for (const [name,type] of [['firefox',firefox],['chromium',chromium]]) {
    const browser=await type.launch({headless:true,timeout:30000,...(name==='chromium'?{chromiumSandbox:true}:{})});
    try {
      for (const [scheme,url] of [
        ['file',pathToFileURL(path.resolve(fixtureDir,'dist/index.html')).href],
        ['http','http://127.0.0.1:4173/'],
        ['dev','http://127.0.0.1:4174/'],
      ]) {
        const context=await browser.newContext();
        const page=await context.newPage();
        const errors=[]; const consoleErrors=[]; const requests=[]; const rawRequests=[];
        let observedPageError;
        const pageErrorEvent=new Promise(resolve=>{observedPageError=resolve;});
        page.on('pageerror',e=>{errors.push(String(e));observedPageError();});
        page.on('console',m=>{if(m.type()==='error') consoleErrors.push(m.text());});
        page.on('request',r=>{rawRequests.push(r.url());requests.push(sanitize(r.url()));});
        const observation={lane,browser:name,version:browser.version(),scheme,url,errors,consoleErrors,requests};
        try {
          await page.goto(url,{waitUntil:'load',timeout:30000});
          if(!errors.length) await Promise.race([page.waitForFunction(()=>['pass','error'].includes(globalThis.__ortResult?.status),undefined,{timeout:60000}),pageErrorEvent]).catch(e=>{observation.waitError=String(e);});
          observation.result=await page.evaluate(()=>globalThis.__ortResult);
          const text=[...errors,observation.result?.error??''].join('\n');
          const urlFailure=/URL constructor.*null.*(?:valid URL|base)|Invalid base URL|Failed to construct 'URL'.*Invalid base URL/i.test(text);
          if (lane!=='candidate' && name==='firefox' && scheme==='file') {
            assert(urlFailure,'Exact reported URL error not reproduced in Firefox baseline');
            observation.expectedBaselineFailure=true;
          } else if (lane!=='candidate' && name==='chromium' && scheme==='file' && urlFailure) {
            observation.baselineControlFailure='Opaque-origin URL error also observed in Chromium';
          } else {
            assert.equal(observation.result?.status,'pass',text);
            assert.deepEqual(observation.result.dims,[3,3]);
            assert.deepEqual(observation.result.data,expected);
            assert.equal(errors.length,0,'Unexpected page errors');
          }
          if(scheme==='file') assert(!rawRequests.some(u=>/^https?:/.test(u)),'Local-file fixture attempted network requests');
          observation.verified=true;
        } catch(error) {
          observation.verified=false;observation.failure=String(error);failed=true;
        } finally {
          observations.push(observation);await context.close();
        }
        if(failed) break;
      }
    } finally { await browser.close(); }
    if(failed) break;
  }
} finally {
  fs.writeFileSync(outputPath,JSON.stringify({lane,observations,passed:!failed,firefoxDisclosure:'Playwright automation-patched Firefox 142.0.1 revision1495; not a stock Windows or Mac verification.'},null,2),{flag:'wx'});
}
assert(!failed,'Browser validation failed; no retry');
