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

const bootstrapModule = path.join(__dirname, '../src/runtime/frontendBootstrap.ts');
const clientModule = path.join(__dirname, '../src/api/client.ts');
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const leaseFetchModule = path.join(__dirname, '../src/api/leaseFetch.ts');
const perfModule = path.join(__dirname, '../src/perf/client.ts');

const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const CONTEXT_A = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: LEASE_A,
});

globalThis.window = {
  location: {
    href: 'http://127.0.0.1:5173/s/local',
    pathname: '/s/local',
  },
};
delete require.cache[require.resolve(bootstrapModule)];
delete require.cache[require.resolve(clientModule)];
delete require.cache[require.resolve(epochModule)];
delete require.cache[require.resolve(leaseFetchModule)];
delete require.cache[require.resolve(perfModule)];
require(bootstrapModule).initializeFrontendBootstrap();
require(epochModule).connectionEpochRuntime.install(CONTEXT_A);

const {
  flushPerfEvents,
  flushPerfEventsSync,
  getPerfConfig,
  loadPerfConfig,
  recordFrontendEvent,
  resetPerfForTests,
} = require(perfModule);

function nextTick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function perfResponse(data = {}, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'X-ChatTree-Connection-Lease-ID': LEASE_A,
    },
  });
}

async function testDisabledDoesNotSend() {
  let calls = 0;
  global.fetch = async () => {
    calls += 1;
    return perfResponse({});
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
    return perfResponse({ accepted: 1 });
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
    return perfResponse({ accepted: 1 });
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
    return perfResponse({ accepted: 1 });
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
    return perfResponse({ accepted: 1 });
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
      return perfResponse({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
      });
    }
    return perfResponse({ accepted: 1 });
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
      return perfResponse({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
      });
    }
    return perfResponse({ accepted: 1 });
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
      return perfResponse({
          enabled: true,
          perf_run_id: 'front-test',
          sample_rate: 1,
          max_attr_length: 64,
          max_batch_events: 10,
      });
    }
    return perfResponse({ accepted: 1 });
  };
  resetPerfForTests({}, { initialized: false });

  recordFrontendEvent({ type: 'mark', name: 'stream.fetch', attrs: { attempt: 1 } });
  await nextTick();
  assert.equal(getPerfConfig().enabled, false);
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

async function testSyncFlushUsesGuardedKeepaliveFetch() {
  const requests = [];
  const previousNavigator = globalThis.navigator;
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      sendBeacon: () => assert.fail('sendBeacon cannot carry the connection lease'),
    },
  });
  global.fetch = async (url, init) => {
    requests.push({ url, init });
    return perfResponse({ accepted: 1 });
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
  assert.equal(requests[0].init.headers.get('X-ChatTree-Connection-Lease-ID'), LEASE_A);
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: previousNavigator,
  });
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function loadFreshPerfHarness() {
  for (const modulePath of [perfModule, leaseFetchModule, clientModule, epochModule, bootstrapModule]) {
    delete require.cache[require.resolve(modulePath)];
  }
  require(bootstrapModule).initializeFrontendBootstrap();
  const { connectionEpochRuntime } = require(epochModule);
  connectionEpochRuntime.install(CONTEXT_A);
  return {
    perf: require(perfModule),
    connectionEpochRuntime,
  };
}

async function testConfigAndFlushCompletionsAreNeutralAfterInvalidation() {
  const configRequest = deferred();
  global.fetch = () => configRequest.promise;
  let harness = loadFreshPerfHarness();
  harness.perf.resetPerfForTests({}, { initialized: false });
  const configLoad = harness.perf.loadPerfConfig();
  harness.connectionEpochRuntime.invalidate(harness.connectionEpochRuntime.capture());
  configRequest.resolve(perfResponse({ enabled: true, perf_run_id: 'stale' }));
  await configLoad;
  assert.equal(harness.perf.getPerfConfig().enabled, false);

  const flushRequest = deferred();
  global.fetch = () => flushRequest.promise;
  harness = loadFreshPerfHarness();
  harness.perf.resetPerfForTests({ enabled: true, max_batch_events: 10 });
  harness.perf.recordFrontendEvent({ type: 'mark', name: 'front.stale' });
  const flush = harness.perf.flushPerfEvents();
  harness.connectionEpochRuntime.invalidate(harness.connectionEpochRuntime.capture());
  flushRequest.resolve(perfResponse({ accepted: 1 }));
  await flush;
  await harness.perf.flushPerfEvents();
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
  await testSyncFlushUsesGuardedKeepaliveFetch();
  await testConfigAndFlushCompletionsAreNeutralAfterInvalidation();
  console.log('perf client tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
