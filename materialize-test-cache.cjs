// Private adapter for already downloaded, hash-verified public ONNX archives.
// Runs the unchanged upstream materializer and its actual extraction/deduplication.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const [source, packet] = process.argv.slice(2);
const prices = JSON.parse(fs.readFileSync(path.join(packet, 'price-result.json')));
const compiled=fs.readFileSync(path.join(source,'js/scripts/prepare-onnx-node-tests.js'),'utf8');
assert(/utils_1\.downloadZip/.test(compiled), 'Compiled materializer must call the replaceable actual export');
const utils = require(path.join(source, 'js/scripts/utils.js'));
const seen = new Set();
utils.downloadZip = async url => {
  const match = /^https:\/\/github\.com\/onnx\/onnx\/archive\/refs\/heads\/rel-([0-9.]+)\.zip$/.exec(url);
  assert(match && prices.onnx_test_archives[match[1]], 'Unexpected cache request');
  const version = match[1];
  assert(!seen.has(version), 'Repeated archive request');
  seen.add(version);
  const buffer = fs.readFileSync(path.join(packet, `onnx-${version}.zip`));
  assert.equal(crypto.createHash('sha256').update(buffer).digest('hex'), prices.onnx_test_archives[version].sha256);
  return buffer;
};
require(path.join(source, 'js/scripts/prepare-onnx-node-tests.js'));
process.on('beforeExit', () => {
  assert.equal(seen.size, 15, 'All fifteen real archives must be materialized');
  assert(fs.existsSync(path.join(source, 'js/test/data/node/__generated_onnx_node_tests')));
  fs.writeFileSync(path.join(packet, 'test-data-cache-receipt.json'), JSON.stringify({archives:[...seen],unchangedMaterializer:true,downloadTransport:'verified local archive cache; network downloader not tested'}, null, 2), {flag:'wx'});
});
