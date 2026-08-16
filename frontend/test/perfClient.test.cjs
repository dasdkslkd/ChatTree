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

const leaseFetchModule = path.join(__dirname, '../src/api/leaseFetch.ts');
require.cache[require.resolve(leaseFetchModule)] = {
  id: leaseFetchModule,
  filename: leaseFetchModule,
  loaded: true,
  exports: {
    leaseGuardedFetch: (url, init) => globalThis.fetch(
      `/p/local/api/v1${url}`,
      init,
    ),
  },
};
const {
  flushPerfEvents,
  flushPerfEventsSync,
  loadPerfConfig,
  recordFrontendEvent,
  resetPerfForTests,
} = require(path.join(__dirname, '../src/perf/client.ts'));

function nextTick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

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
  assert.equal(requests[0].url, '/p/local/api/v1/perf/events');
  const body = JSON.parse(requests[0].init.body);
  assert.equal(body.events[0].name, 'front.mark');
  assert.equal(body.events[0].attrs.long, 'abcd...[len=6]');
}

async function testDoesNotFlushBelowBatchLimit() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 1,
    max_attr_length: 64,
    max_batch_events: 3,
  });
  recordFrontendEvent({ type: 'mark', name: 'front.one' });
  recordFrontendEvent({ type: 'mark', name: 'front.two' });
  await nextTick();
  assert.equal(requests.length, 0);
  await flushPerfEvents();
  assert.equal(requests.length, 1);
  const body = JSON.parse(requests[0].init.body);
  assert.deepEqual(body.events.map((event) => event.name), ['front.one', 'front.two']);
}

async function testFlushesAtConfiguredBatchLimit() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 1,
    max_attr_length: 64,
    max_batch_events: 3,
  });
  recordFrontendEvent({ type: 'mark', name: 'front.one' });
  recordFrontendEvent({ type: 'mark', name: 'front.two' });
  recordFrontendEvent({ type: 'mark', name: 'front.three' });
  await nextTick();
  assert.equal(requests.length, 1);
  const body = JSON.parse(requests[0].init.body);
  assert.deepEqual(body.events.map((event) => event.name), ['front.one', 'front.two', 'front.three']);
}

async function testCriticalEventsBypassSampling() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 0,
    max_attr_length: 64,
    max_batch_events: 10,
  });
  recordFrontendEvent({ type: 'mark', name: 'stream.done' });
  await nextTick();
  assert.equal(requests.length, 1);
  const body = JSON.parse(requests[0].init.body);
  assert.equal(body.events[0].name, 'stream.done');
}

async function testPreInitEventsFlushAfterConfigLoads() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    if (url === '/p/local/api/v1/perf/config') {
      return {
        ok: true,
        json: async () => ({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
        }),
      };
    }
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({}, { initialized: false });

  recordFrontendEvent({ type: 'mark', name: 'stream.fetch', attrs: { phase: 'pre-init' } });
  await loadPerfConfig();
  await flushPerfEvents();

  const configRequests = requests.filter((request) => request.url === '/p/local/api/v1/perf/config');
  const eventRequests = requests.filter((request) => request.url === '/p/local/api/v1/perf/events');
  assert.equal(configRequests.length, 1);
  assert.equal(eventRequests.length, 1);
  const body = JSON.parse(eventRequests[0].init.body);
  assert.equal(body.events[0].name, 'stream.fetch');
  assert.equal(body.events[0].attrs.phase, 'pre-init');
}

async function testImmediatePreInitEventFlushesAfterConfigLoads() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    if (url === '/p/local/api/v1/perf/config') {
      return {
        ok: true,
        json: async () => ({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
        }),
      };
    }
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({}, { initialized: false });

  recordFrontendEvent({ type: 'mark', name: 'stream.done' });
  await loadPerfConfig();
  await nextTick();

  const eventRequests = requests.filter((request) => request.url === '/p/local/api/v1/perf/events');
  assert.equal(eventRequests.length, 1);
  const body = JSON.parse(eventRequests[0].init.body);
  assert.equal(body.events[0].name, 'stream.done');
}

async function testConfigLoadFailureRetriesAndKeepsPreInitEvents() {
  const requests = [];
  let configAttempts = 0;
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    if (url === '/p/local/api/v1/perf/config') {
      configAttempts += 1;
      if (configAttempts === 1) throw new Error('temporary config failure');
      return {
        ok: true,
        json: async () => ({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
        }),
      };
    }
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({}, { initialized: false });

  recordFrontendEvent({ type: 'mark', name: 'stream.fetch', attrs: { attempt: 1 } });
  await nextTick();
  recordFrontendEvent({ type: 'mark', name: 'stream.response_headers', attrs: { attempt: 2 } });
  await loadPerfConfig();
  await flushPerfEvents();

  const configRequests = requests.filter((request) => request.url === '/p/local/api/v1/perf/config');
  const eventRequests = requests.filter((request) => request.url === '/p/local/api/v1/perf/events');
  assert.equal(configRequests.length, 2);
  assert.equal(eventRequests.length, 1);
  const body = JSON.parse(eventRequests[0].init.body);
  assert.deepEqual(body.events.map((event) => event.name), ['stream.fetch', 'stream.response_headers']);
}

async function testSyncFlushUsesLeaseFetchKeepalive() {
  const requests = [];
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return { ok: true, json: async () => ({ accepted: 1 }) };
  };
  resetPerfForTests({
    enabled: true,
    perf_run_id: 'front-test',
    sample_rate: 1,
    max_attr_length: 64,
    max_batch_events: 10,
  });
  recordFrontendEvent({ type: 'mark', name: 'front.hide' });
  assert.equal(flushPerfEventsSync(), true);
  await nextTick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/p/local/api/v1/perf/events');
  assert.equal(requests[0].init.keepalive, true);
}

(async () => {
  await testDisabledDoesNotSend();
  await testEnabledBatchesEvents();
  await testDoesNotFlushBelowBatchLimit();
  await testFlushesAtConfiguredBatchLimit();
  await testCriticalEventsBypassSampling();
  await testPreInitEventsFlushAfterConfigLoads();
  await testImmediatePreInitEventFlushesAfterConfigLoads();
  await testConfigLoadFailureRetriesAndKeepsPreInitEvents();
  await testSyncFlushUsesLeaseFetchKeepalive();
  console.log('perf client tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
