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
  flushPerfEvents,
  flushPerfEventsSync,
  recordFrontendEvent,
  resetPerfForTests,
} = require(path.join(__dirname, '../src/perf/client.ts'));

async function testDisabledDoesNotSend() {
  let calls = 0;
  global.fetch = async () => {
    calls += 1;
    return { ok: true, json: async () => ({}) };
  };
  resetPerfForTests({ enabled: false });
  recordFrontendEvent({ type: 'mark', name: 'ignored' });
  await flushPerfEvents();
  assert.equal(calls, 0);
}

async function testEnabledBatchesEvents() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 1,
    max_attr_length: 4,
    max_batch_events: 10,
  });
  recordFrontendEvent({ type: 'mark', name: 'front.mark', attrs: { long: 'abcdef' } });
  await flushPerfEvents();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/perf/events');
  const body = JSON.parse(requests[0].init.body);
  assert.equal(body.events[0].name, 'front.mark');
  assert.equal(body.events[0].attrs.long, 'abcd...[len=6]');
}

function testSyncFlushUsesBeacon() {
  const beacons = [];
  const previousNavigator = globalThis.navigator;
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      sendBeacon: (url, body) => {
        beacons.push({ url, body });
        return true;
      },
    },
  });
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 1,
    max_attr_length: 64,
    max_batch_events: 10,
  });
  recordFrontendEvent({ type: 'mark', name: 'front.hide' });
  assert.equal(flushPerfEventsSync(), true);
  assert.equal(beacons.length, 1);
  assert.equal(beacons[0].url, '/api/perf/events');
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: previousNavigator,
  });
}

(async () => {
  await testDisabledDoesNotSend();
  await testEnabledBatchesEvents();
  testSyncFlushUsesBeacon();
  console.log('perf client tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
