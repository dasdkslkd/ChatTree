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

function testProcessedFoldShowsLiveDurationWhileStreaming() {
  // 运行中：以 totalDuration 为起点叠加本地秒级计时，标签实时刷新等待用时
  const source = readSource('src/components/transcript/TranscriptList.tsx');
  assert.match(source, /const \[durationMs, setDurationMs\] = useState\(totalDuration\);/);
  assert.match(source, /const baseAt = Date\.now\(\) - totalDuration;/);
  assert.match(source, /setInterval\(\(\) => \{\s*setDurationMs\(Math\.max\(totalDuration, Date\.now\(\) - baseAt\)\);\s*\}, 1000\)/);
  assert.match(source, /const duration = streaming \? durationMs : totalDuration;/);
  assert.match(source, /duration > 0 \? `已处理 \$\{formatProcessedDuration\(duration\) \?\? ''\}`\.trim\(\) : '已处理'/);
}

testFormatsProcessedDurationWithHours();
testProcessedFoldTracksStreaming();
testProcessedFoldShowsLiveDurationWhileStreaming();
console.log('assistantTimelineFolding tests passed');
