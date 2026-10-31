// Private serial validation driver. Requires only the pinned read-only Node runtime.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
const P='/work', S='/work/source', started=Date.now(), limit=110*60*1000;
const env={...process.env,PATH:'/node/bin:'+process.env.PATH,NODE_OPTIONS:'--max-old-space-size=4096',ELECTRON_SKIP_BINARY_DOWNLOAD:'1',PUPPETEER_SKIP_DOWNLOAD:'1',npm_config_fetch_retries:'0',npm_config_fetch_timeout:'20000',npm_config_maxsockets:'4',npm_config_audit:'false',npm_config_fund:'false',npm_config_cache:'/work/npm-cache',PLAYWRIGHT_BROWSERS_PATH:'/work/browsers'};
delete env.GH_TOKEN;delete env.GITHUB_TOKEN;
const result={status:'RUNNING',steps:[],claims:{windows:false,gpu:false,stockFirefox:false}};
const owned=[];
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const save=()=>fs.writeFileSync(P+'/execution-result.json',JSON.stringify(result,null,2));
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function stop(proc) {
  if(proc.exitCode!==null || proc.signalCode!==null) return;
  try {process.kill(-proc.pid,'SIGTERM');} catch(e) {if(e.code!=='ESRCH') throw e;}
  for(let i=0;i<50 && proc.exitCode===null && proc.signalCode===null;i++) await delay(100);
  if(proc.exitCode===null && proc.signalCode===null) {
    try {process.kill(-proc.pid,'SIGKILL');} catch(e) {if(e.code!=='ESRCH') throw e;}
  }
}
async function run(name,args,cwd=S,seconds=600,expectedFailure=false) {
  assert(Date.now()-started<limit);
  const log=path.join(P,name+'.log'), fd=fs.openSync(log,'wx');
  const clock=Date.now();
  const proc=spawn(args[0],args.slice(1),{cwd,env,stdio:['ignore',fd,fd],detached:true});
  let spawnError;proc.on('error',e=>{spawnError=e;});
  try {
    while(proc.exitCode===null && proc.signalCode===null && !spawnError) {
      assert(Date.now()-clock<seconds*1000 && Date.now()-started<limit,'STEP_TIME_CAP');
      assert(fs.statSync(log).size<16*1024*1024,'STEP_LOG_CAP');
      const total=fs.readdirSync(P).filter(n=>n.endsWith('.log')).reduce((a,n)=>a+fs.statSync(path.join(P,n)).size,0);
      assert(total<128*1024*1024,'TOTAL_LOG_CAP');await delay(500);
    }
    assert(!spawnError,String(spawnError));
  } catch(e) {await stop(proc);throw e;}
  finally {fs.closeSync(fd);}
  const text=fs.readFileSync(log,'utf8');
  result.steps.push({name,args,cwd,exit:proc.exitCode,signal:proc.signalCode,seconds:(Date.now()-clock)/1000,logSha256:sha(text)});save();
  if(expectedFailure) assert(proc.exitCode!==0 && /Invalid (?:base )?URL/.test(text) && text.includes('resolveScriptSrc'),'MISSING_BASELINE_URL_REGRESSION');
  else assert.equal(proc.exitCode,0,'STEP_FAILED_'+name);
}
function service(name,args,cwd) {
  const fd=fs.openSync(path.join(P,name+'.log'),'wx');
  const proc=spawn(args[0],args.slice(1),{cwd,env,stdio:['ignore',fd,fd],detached:true});
  proc.on('error',e=>{proc.serviceError=e;});owned.push({proc,fd});return proc;
}
async function fixture(lane,bundle) {
  const dir=path.join(P,'fixture-'+lane);fs.mkdirSync(dir);
  fs.symlinkSync(P+'/fixture/node_modules',dir+'/node_modules','dir');fs.copyFileSync(P+'/fixture/package.json',dir+'/package.json',fs.constants.COPYFILE_EXCL);
  await run(lane+'-prepare',['node',P+'/prepare-fixture.mjs',dir,S+'/js/web/test/e2e/model.onnx',...(bundle?[bundle]:[])],S,30);
  await run(lane+'-vite-build',['node',dir+'/node_modules/vite/bin/vite.js','build'],dir,300);
  const prod=service(lane+'-prod-server',['node',dir+'/node_modules/vite/bin/vite.js','preview','--host','127.0.0.1','--port','4173','--strictPort'],dir);
  const dev=service(lane+'-dev-server',['node',dir+'/node_modules/vite/bin/vite.js'],dir);
  try {
    const end=Date.now()+20000;
    for(const port of [4173,4174]) while(true) {
      assert(!prod.serviceError && !dev.serviceError && prod.exitCode===null && dev.exitCode===null,'FIXTURE_SERVICE_FAILED');
      try {assert.equal((await fetch('http://127.0.0.1:'+port,{signal:AbortSignal.timeout(1000)})).status,200);break;}
      catch(e) {assert(Date.now()<end,'FIXTURE_SERVICE_TIMEOUT');await delay(100);}
    }
    await run(lane+'-real-browsers',['node',P+'/browser-check.mjs',dir,lane,P+'/'+lane+'-browser-result.json'],dir,600);
  } finally {await stop(prod);await stop(dev);}
}
try {
  assert.notEqual(process.getuid(),0,'NONROOT_BROWSER_REQUIRED');
  await run('node-identity',['node','--version'],S,10);
  await run('npm-identity',['npm','--version'],S,10);
  await run('root-npm-ci',['npm','ci'],S+'/js',600);
  await run('common-npm-ci',['npm','ci'],S+'/js/common',600);
  await run('common-tests',['npm','test'],S+'/js/common',300);
  await run('common-float16-tests',['npm','run','test:f16'],S+'/js/common',300);
  await run('web-npm-ci',['npm','ci'],S+'/js/web',600);
  await run('fixture-npm-ci',['npm','ci','--ignore-scripts'],P+'/fixture',600);
  const loader=S+'/js/web/lib/wasm/wasm-utils-import.ts';
  const identities=JSON.parse(fs.readFileSync(P+'/frozen-loaders.json'));
  for(const name of ['baseline','candidate']) assert.equal(sha(fs.readFileSync(P+'/'+name+'-loader.ts')),identities[name]);
  fs.copyFileSync(P+'/baseline-loader.ts',loader);
  await run('compile-source-regression',['node','../node_modules/typescript/bin/tsc','--build','./script'],S+'/js/web',180);
  await run('baseline-source-regression',['node','./script/test-wasm-utils-import'],S+'/js/web',30,true);
  await run('baseline-web-types',['node','../node_modules/typescript/bin/tsc','-p','.','--noEmit'],S+'/js/web',180);
  await run('baseline-web-worker-types',['node','../node_modules/typescript/bin/tsc','-p','lib/wasm/proxy-worker','--noEmit'],S+'/js/web',180);
  await run('baseline-production-build',['node','./script/build'],S+'/js/web',900);
  await fixture('reporter',null);
  await fixture('base',S+'/js/web/dist/ort.bundle.min.mjs');
  fs.copyFileSync(P+'/candidate-loader.ts',loader);
  await run('candidate-web-prebuild',['npm','run','prebuild'],S+'/js/web',300);
  await run('candidate-root-lint',['npm','run','lint'],S+'/js',300);
  const sourceHashes=()=>Object.fromEntries(['js','docs'].flatMap(root=>fs.readdirSync(path.join(S,root),{recursive:true,withFileTypes:true})).filter(x=>x.isFile() && /\.(jsonc?|[cm]?js|[cm]?ts|md)$/.test(x.name) && !/(?:^|\/)node_modules\//.test(path.relative(S+'/js',path.join(x.parentPath,x.name)))).map(x=>{const file=path.join(x.parentPath,x.name);return [path.relative(S+'/js',file),sha(fs.readFileSync(file))];}));
  const beforeFormat=sourceHashes();await run('candidate-format',['npm','run','format'],S+'/js',300);assert.deepEqual(sourceHashes(),beforeFormat,'FORMAT_CHANGED_SOURCE');
  await run('common-typedoc',['npx','--no-install','typedoc','--emit','none','--treatWarningsAsErrors'],S+'/js/common',180);
  const beforeDocs=sourceHashes();await run('web-generate-docs',['npm','run','build:doc'],S+'/js/web',180);assert.deepEqual(sourceHashes(),beforeDocs,'GENERATED_DOCS_CHANGED');
  await run('candidate-production-build',['npm','run','build'],S+'/js/web',900);
  await fixture('candidate',S+'/js/web/dist/ort.bundle.min.mjs');
  await run('materialize-real-test-cache',['node',P+'/materialize-test-cache.cjs',S,P],S,600);
  await run('package-consumption-controls',['node',P+'/package-controls.mjs',S,P],S,1500);
  await run('full-wasm-default-suite',['npm','test','--','-e=chrome','-b=wasm'],S+'/js/web',1800);
  await run('full-wasm-proxy-suite',['npm','test','--','--wasm.proxy','-b=wasm','-e=chrome'],S+'/js/web',1800);
  result.status='PASS';
} catch(e) {result.status='STOP_NO_RETRY';result.error=String(e);throw e;}
finally {
  for(const item of owned) {try {await stop(item.proc);} catch(e) {result.cleanupError=String(e);}finally {fs.closeSync(item.fd);}}
  result.elapsedSeconds=(Date.now()-started)/1000;save();
}
