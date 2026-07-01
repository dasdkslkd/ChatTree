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
  ACTIVE_STREAM_IDLE_POLL_MS,
  ACTIVE_STREAM_VISIBLE_POLL_MS,
  CONVERSATION_ACTIVE_STREAM_HINTED_LOOKUPS,
  CONVERSATION_ACTIVE_STREAM_IDLE_LOOKUPS,
  getConversationActiveStreamLookupLimit,
  getActiveStreamPollingDelay,
} = require(path.join(__dirname, '../src/utils/activeStreamPolling.ts'));

function testUsesFastPollingWhenStreamsAreActive() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 2, documentHidden: false }), ACTIVE_STREAM_VISIBLE_POLL_MS);
}

function testUsesSlowPollingWhenIdle() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 0, documentHidden: false }), ACTIVE_STREAM_IDLE_POLL_MS);
}

function testPausesPollingWhenDocumentIsHidden() {
  assert.equal(getActiveStreamPollingDelay({ activeStreamCount: 2, documentHidden: true }), null);
}

function testUsesThreeShortLookupsWithoutActiveHint() {
  assert.equal(CONVERSATION_ACTIVE_STREAM_IDLE_LOOKUPS, 3);
  assert.equal(
    getConversationActiveStreamLookupLimit({ activeStreamHintCount: 0 }),
    CONVERSATION_ACTIVE_STREAM_IDLE_LOOKUPS,
  );
}

function testAllowsRetryWhenSelectedConversationHasActiveHint() {
  assert.equal(
    getConversationActiveStreamLookupLimit({ activeStreamHintCount: 1 }),
    CONVERSATION_ACTIVE_STREAM_HINTED_LOOKUPS,
  );
}

function main() {
  testUsesFastPollingWhenStreamsAreActive();
  testUsesSlowPollingWhenIdle();
  testPausesPollingWhenDocumentIsHidden();
  testUsesThreeShortLookupsWithoutActiveHint();
  testAllowsRetryWhenSelectedConversationHasActiveHint();
  console.log('activeStreamPolling tests passed');
}

main();
