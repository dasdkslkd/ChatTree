const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  ACTIVE_STREAM_VISIBLE_POLL_MS,
  getActiveStreamPollingDelay,
} = require(path.join(__dirname, '../src/utils/streaming.ts'));

function testUsesFastPollingWhenStreamsAreActive() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 2, documentHidden: false }), ACTIVE_STREAM_VISIBLE_POLL_MS);
}

function testStopsPollingWhenIdle() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 0, documentHidden: false }), null);
}

function testPausesPollingWhenDocumentIsHidden() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 2, documentHidden: true }), null);
}

function main() {
  testUsesFastPollingWhenStreamsAreActive();
  testStopsPollingWhenIdle();
  testPausesPollingWhenDocumentIsHidden();
  console.log('activeStreamPolling tests passed');
}

main();
