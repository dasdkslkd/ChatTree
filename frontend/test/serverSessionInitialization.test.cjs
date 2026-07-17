const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  initializeServerSessionStores,
  ServerSessionInitializationOwner,
} = require('../src/runtime/serverSessionInitialization.ts');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function createScheduler() {
  let nextId = 1;
  const tasks = new Map();
  const delays = [];
  return {
    delays,
    tasks,
    setTimeout(callback, delay) {
      const id = nextId++;
      delays.push(delay);
      tasks.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      tasks.delete(id);
    },
    runNext() {
      const entry = tasks.entries().next().value;
      assert.ok(entry, 'expected a pending retry');
      const [id, callback] = entry;
      tasks.delete(id);
      callback();
    },
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

async function testStoreSuccessCriteria() {
  const missingConfigCalls = [];
  await assert.rejects(() => initializeServerSessionStores({
    async loadConfig() { missingConfigCalls.push('loadConfig'); },
    getConfig() { missingConfigCalls.push('getConfig'); return null; },
    async loadProviders() { missingConfigCalls.push('loadProviders'); },
    getError() { missingConfigCalls.push('getError'); return null; },
  }), /config initialization failed/i);
  assert.deepEqual(missingConfigCalls, ['loadConfig', 'getConfig']);

  const providerErrorCalls = [];
  const providerError = 'provider transport failed';
  await assert.rejects(() => initializeServerSessionStores({
    async loadConfig() { providerErrorCalls.push('loadConfig'); },
    getConfig() { providerErrorCalls.push('getConfig'); return {}; },
    async loadProviders() { providerErrorCalls.push('loadProviders'); },
    getError() { providerErrorCalls.push('getError'); return providerError; },
  }), new RegExp(providerError));
  assert.deepEqual(providerErrorCalls, ['loadConfig', 'getConfig', 'loadProviders', 'getError']);

  const successCalls = [];
  await initializeServerSessionStores({
    async loadConfig() { successCalls.push('loadConfig'); },
    getConfig() { successCalls.push('getConfig'); return {}; },
    async loadProviders() { successCalls.push('loadProviders'); },
    getError() { successCalls.push('getError'); return null; },
  });
  assert.deepEqual(successCalls, ['loadConfig', 'getConfig', 'loadProviders', 'getError']);
}

async function testOwnerIsSingleflightAndStopsAfterSuccess() {
  const scheduler = createScheduler();
  const attempt = deferred();
  let calls = 0;
  const owner = new ServerSessionInitializationOwner({
    initialize: () => { calls += 1; return attempt.promise; },
    scheduler,
    onError() {},
  });

  owner.start();
  owner.start();
  owner.setConnected(true);
  owner.setConnected(true);
  assert.equal(calls, 1);
  attempt.resolve();
  await settle();
  owner.setConnected(false);
  owner.setConnected(true);
  assert.equal(calls, 1, 'successful initialization is permanent for the mounted realm');
  assert.equal(scheduler.tasks.size, 0);
  owner.dispose();
}

async function testRetryOnlyWhileConnectedAndReconnectsImmediately() {
  const scheduler = createScheduler();
  let calls = 0;
  const failures = [];
  const owner = new ServerSessionInitializationOwner({
    initialize: async () => {
      calls += 1;
      if (calls < 3) throw new Error(`failure-${calls}`);
    },
    scheduler,
    onError(error) { failures.push(error.message); },
  });

  owner.start();
  owner.setConnected(true);
  await settle();
  assert.equal(calls, 1);
  assert.deepEqual(scheduler.delays, [30_000]);
  assert.equal(scheduler.tasks.size, 1);

  owner.setConnected(false);
  assert.equal(scheduler.tasks.size, 0, 'disconnect clears a pending retry');
  owner.setConnected(true);
  assert.equal(calls, 2, 'reconnect attempts immediately while idle');
  await settle();
  assert.equal(scheduler.tasks.size, 1);
  assert.deepEqual(scheduler.delays, [30_000, 30_000]);

  scheduler.runNext();
  assert.equal(calls, 3);
  await settle();
  assert.equal(scheduler.tasks.size, 0, 'success stops retries');
  assert.deepEqual(failures, ['failure-1', 'failure-2']);
  owner.dispose();
}

async function testFailureWhileDisconnectedDoesNotSchedule() {
  const scheduler = createScheduler();
  const attempt = deferred();
  const owner = new ServerSessionInitializationOwner({
    initialize: () => attempt.promise,
    scheduler,
    onError() {},
  });
  owner.start();
  owner.setConnected(true);
  owner.setConnected(false);
  attempt.reject(new Error('offline'));
  await settle();
  assert.equal(scheduler.tasks.size, 0);
  owner.dispose();
}

async function testNullRejectionIsStillAFailure() {
  const scheduler = createScheduler();
  const errors = [];
  const owner = new ServerSessionInitializationOwner({
    initialize: () => Promise.reject(null),
    scheduler,
    onError(error) { errors.push(error); },
  });
  owner.start();
  owner.setConnected(true);
  await settle();
  assert.deepEqual(errors, [null]);
  assert.deepEqual(scheduler.delays, [30_000]);
  assert.equal(scheduler.tasks.size, 1);
  owner.dispose();
}

async function testDisposeMakesLateCompletionInert() {
  for (const outcome of ['resolve', 'reject']) {
    const scheduler = createScheduler();
    const attempt = deferred();
    const errors = [];
    const owner = new ServerSessionInitializationOwner({
      initialize: () => attempt.promise,
      scheduler,
      onError(error) { errors.push(error); },
    });
    owner.start();
    owner.setConnected(true);
    owner.dispose();
    if (outcome === 'resolve') attempt.resolve();
    else attempt.reject(new Error('late'));
    await settle();
    assert.equal(scheduler.tasks.size, 0);
    assert.deepEqual(errors, []);
  }
}

async function main() {
  await testStoreSuccessCriteria();
  await testOwnerIsSingleflightAndStopsAfterSuccess();
  await testRetryOnlyWhileConnectedAndReconnectsImmediately();
  await testFailureWhileDisconnectedDoesNotSchedule();
  await testNullRejectionIsStillAFailure();
  await testDisposeMakesLateCompletionInert();
  console.log('server session initialization tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
