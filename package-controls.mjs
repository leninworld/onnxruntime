// Private adapter: actual npm packs and upstream Vite/Webpack consumption fixtures.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawn,spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
const [source,packet]=process.argv.slice(2);
const web=path.join(source,'js/web');
const e2e=path.join(web,'test/e2e');
const packed=path.join(packet,'packed');
fs.mkdirSync(packed);
const run=(args,cwd,timeout=180000)=>{
  const result=spawnSync(args[0],args.slice(1),{cwd,env:process.env,timeout,maxBuffer:16*1024*1024,encoding:'utf8'});
  process.stdout.write(result.stdout??'');process.stderr.write(result.stderr??'');
  assert(!result.error && result.status===0,`${args.join(' ')} failed`);
};
run(['npm','pack','--ignore-scripts','--pack-destination',packed],path.join(source,'js/common'));
const packagePath=path.join(web,'package.json');const original=fs.readFileSync(packagePath);
try {
  // Exact existing upstream prepack metadata transformation, without repeating the completed build.
  run(['node','./script/prepack'],web,30000);
  run(['npm','pack','--ignore-scripts','--pack-destination',packed],web);
} finally {fs.writeFileSync(packagePath,original);}
const packages=fs.readdirSync(packed).map(n=>path.join(packed,n));
assert.equal(packages.length,2);
run(['npm','install','--ignore-scripts','--package-lock-only','--save-dev',...packages],e2e);
const prices=JSON.parse(fs.readFileSync(path.join(packet,'price-result.json')));
const lock=JSON.parse(fs.readFileSync(path.join(e2e,'package-lock.json')));
for(const entry of Object.values(lock.packages)) {
  if(!entry.resolved?.startsWith('https://registry.npmjs.org/')) continue;
  const url=new URL(entry.resolved);
  const name=decodeURIComponent(url.pathname.split('/-/')[0].replace(/^\//,''));
  const price=prices.npm.package_prices[`${name}@${entry.version}`];
  assert(price,'Unpriced package-control dependency');
  assert(entry.integrity && entry.integrity.split(' ').some(d=>price.integrity.split(' ').includes(d)),'Package-control SRI mismatch');
}
run(['npm','ci','--ignore-scripts'],e2e,360000);
run(['npm','run','build:v'],e2e,180000);
run(['npm','run','build:w'],e2e,180000);
const observations=[];
try {
  const cases=[['webpack_esm_js','mjs','esm'],['webpack_umd_js','js','iife'],['vite_esm_js','mjs','esm'],['vite_umd_js','js','iife']];
  for(const [folder,extension,format] of cases) for(const shared of [false,true]) {
    const userData=path.join(packet,`package-profile-${folder}-${shared}`);fs.mkdirSync(userData);
    const args=['node','node_modules/karma/bin/karma','start','--single-run','--browsers','Chrome_headless','--self-host','--ort-main=',`--test-main=./dist/${folder}/ort-test-e2e.bundle.${extension}`,`--user-data=${userData}`,`--format=${format}`,...(shared?['--enable-shared-array-buffer']:[])];
    run(args,e2e,180000);
    observations.push({folder,sharedArrayBuffer:shared,harness:'unchanged upstream e2e karma configuration',passed:true});
  }
} finally {
  fs.writeFileSync(path.join(packet,'package-control-result.json'),JSON.stringify({observations,scope:'Actual packed common/web packages; upstream Vite/Webpack ESM+UMD consumption cases in actual upstream Karma harness, each with and without SharedArrayBuffer. Windows-specific export launcher, other bundlers and GPU not claimed.'},null,2),{flag:'wx'});
}
