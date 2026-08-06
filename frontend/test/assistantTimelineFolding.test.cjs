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

const { formatProcessedDuration } = require(path.join(__dirname, '../src/utils/time.ts'));

function readSource(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function testFormatsProcessedDurationWithHours() {
  assert.equal(formatProcessedDuration(5_550_000), '1h32m30s');
  assert.equal(formatProcessedDuration(4_202_000), '1h10m2s');
  assert.equal(formatProcessedDuration(62_000), '1m2s');
  assert.equal(formatProcessedDuration(900), '1s');
  assert.equal(formatProcessedDuration(null), null);
}

function testProcessedFoldTracksStreaming() {
  const source = readSource('src/components/transcript/TranscriptList.tsx');
  assert.match(source, /items\.some\(\(item\) => \(item as \{ status\?: string \}\)\.status === 'running'\)/);
  assert.match(source, /useState\(streaming\)/);
  assert.match(source, /if \(!streaming\) setExpanded\(false\)/);
  assert.match(source, /useEffect\(\(\) =>/);
}

testFormatsProcessedDurationWithHours();
testProcessedFoldTracksStreaming();
console.log('assistantTimelineFolding tests passed');
