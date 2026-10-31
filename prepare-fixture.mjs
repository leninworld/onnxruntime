// Private validation fixture. Not part of the proposed upstream patch.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const [fixtureDir, modelPath, bundlePath] = process.argv.slice(2);
assert(fixtureDir && modelPath);
const model = fs.readFileSync(modelPath);
let entry=bundlePath && path.resolve(bundlePath);
if(!entry) {
  const packageRoot=path.join(fixtureDir,'node_modules/onnxruntime-web');
  const pkg=JSON.parse(fs.readFileSync(path.join(packageRoot,'package.json')));
  const root=typeof pkg.exports==='object' && Object.keys(pkg.exports).some(n=>n.startsWith('.')) ? pkg.exports['.'] : pkg.exports;
  const resolve=(value,conditions)=>{
    if(typeof value==='string') return value;
    assert(value && !Array.isArray(value),'Unsupported package entry contract');
    for(const [key,next] of Object.entries(value)) if(conditions.has(key)) {
      const found=resolve(next,conditions);if(found) return found;
    }
  };
  const prod=resolve(root,new Set(['browser','import','module','production','default']));
  const dev=resolve(root,new Set(['browser','import','module','development','default']));
  assert(prod && prod===dev && prod.startsWith('./'),'Ambiguous browser import entry');
  entry=path.resolve(packageRoot,prod);
  assert(entry.startsWith(path.resolve(packageRoot)+path.sep));
}
const bundleBytes=fs.readFileSync(entry);
const referenced=[...new Set(bundleBytes.toString('utf8').match(/ort-wasm-simd-threaded(?:\.[a-z0-9_-]+)?\.wasm/g)??[])];
assert.equal(referenced.length,1,'STOP_NO_RETRY: ambiguous bundle WASM contract');
const wasmPath=path.join(path.dirname(entry),referenced[0]);
const gluePath=wasmPath.replace(/\.wasm$/,'.mjs');
assert(fs.statSync(wasmPath).isFile() && fs.statSync(gluePath).isFile());
const wasm=fs.readFileSync(wasmPath);
assert(model.length<1024*1024 && wasm.length<64*1024*1024);
assert(WebAssembly.validate(wasm),'STOP_NO_RETRY: invalid matching WASM');
const pairing={resolvedEntry:entry,referencedName:referenced[0],wasmFile:wasmPath,glueFile:gluePath,bundleSha256:crypto.createHash('sha256').update(bundleBytes).digest('hex'),wasmSha256:crypto.createHash('sha256').update(wasm).digest('hex'),glueSha256:crypto.createHash('sha256').update(fs.readFileSync(gluePath)).digest('hex'),wasmValidated:true};
const put = (name, content) => {
  const target = path.join(fixtureDir, name);
  assert(!fs.existsSync(target));
  fs.writeFileSync(target, content, {flag: 'wx'});
};
put('index.html', `<!doctype html><html><head><meta charset="UTF-8"><title>ORT local-file validation</title></head><body><div id="root"></div><script>globalThis.__ortResult={status:'loading',origin:location.origin,href:location.href};</script><script type="module" src="/main.jsx"></script></body></html>`);
put('inputs.js', `export const model=${JSON.stringify(model.toString('base64'))};\nexport const wasm=${JSON.stringify(wasm.toString('base64'))};\n`);
put('main.jsx', `import React, {useEffect} from 'react';
import {createRoot} from 'react-dom/client';
import * as ort from 'onnxruntime-web';
import {model,wasm} from './inputs.js';
const decode = b => Uint8Array.from(atob(b), c => c.charCodeAt(0));
function App() {
  useEffect(() => {
    (async () => {
      ort.env.wasm.numThreads=1;
      ort.env.wasm.proxy=false;
      ort.env.wasm.wasmBinary=decode(wasm);
      const session=await ort.InferenceSession.create(decode(model),{executionProviders:['wasm']});
      try {
        const output=await session.run({
          a:new ort.Tensor('float32',Float32Array.from([1,2,3,4,5,6,7,8,9,10,11,12]),[3,4]),
          b:new ort.Tensor('float32',Float32Array.from([10,20,30,40,50,60,70,80,90,100,110,120]),[4,3])
        });
        globalThis.__ortResult={status:'pass',origin:location.origin,href:location.href,dims:Array.from(output.c.dims),data:Array.from(output.c.data),versions:ort.env.versions};
      } finally { await session.release(); }
    })().catch(error => {globalThis.__ortResult={status:'error',origin:location.origin,href:location.href,error:String(error),stack:error.stack};});
  }, []);
  return React.createElement('p', null, 'Real ONNX Runtime MatMul validation');
}
createRoot(document.getElementById('root')).render(React.createElement(App));
`);
let alias = {};
if (bundlePath) {
  const vendor = path.join(fixtureDir, 'vendor');
  fs.mkdirSync(vendor);
  const bytes = fs.readFileSync(bundlePath);
  const local = path.join(vendor, 'ort.bundle.min.mjs');
  fs.writeFileSync(local, bytes, {flag:'wx'});
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(local)).digest('hex'),crypto.createHash('sha256').update(bytes).digest('hex'));
  alias = {'onnxruntime-web': local};
}
put('vite.config.mjs', `import {defineConfig} from 'vite';\nimport {viteSingleFile} from 'vite-plugin-singlefile';\nexport default defineConfig({base:'./',plugins:[viteSingleFile()],resolve:{alias:${JSON.stringify(alias)}},build:{target:'esnext'},server:{host:'127.0.0.1',port:4174,strictPort:true}});\n`);
put('input-identities.json',JSON.stringify({model:{path:modelPath,bytes:model.length,sha256:crypto.createHash('sha256').update(model).digest('hex')},pairing},null,2));
