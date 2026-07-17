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
const coordinatorModule = path.join(__dirname, '../src/services/taskStateCoordinator.ts');

globalThis.window = {
  location: {
    href: 'http://127.0.0.1:5173/s/local',
    pathname: '/s/local',
  },
};
delete require.cache[require.resolve(bootstrapModule)];
delete require.cache[require.resolve(clientModule)];
delete require.cache[require.resolve(coordinatorModule)];
require(bootstrapModule).initializeFrontendBootstrap();

const { TaskStateCoordinator } = require(coordinatorModule);

const TOKEN_A = Object.freeze({
  profileId: 'local',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  generation: 1,
});

function epochSource() {
  let current = true;
  return {
    capture: () => {
      if (!current) throw new Error('stale capture');
      return TOKEN_A;
    },
    isCurrent: (token) => current && token === TOKEN_A,
    invalidate: () => {
      current = false;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function snapshot(version, overrides = {}) {
  return {
    conversation_id: 'conv-1',
    task: null,
    notifications: [],
    flags: { running: false, delivering: false, needsFollowup: false },
    version,
    ...overrides,
  };
}

async function testPublishesFetchedStateToSubscribers() {
  const seen = [];
  const epoch = epochSource();
  let receivedToken = null;
  const coordinator = new TaskStateCoordinator({
    fetch: async (_conversationId, token) => {
      receivedToken = token;
      return snapshot('v1');
    },
  }, epoch);
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));
  const state = await coordinator.refresh('conv-1');

  assert.equal(state.version, 'v1');
  assert.equal(receivedToken, TOKEN_A);
  assert.deepEqual(seen, ['v1']);
}

async function testCoalescesInvalidationDuringFetchIntoFreshRead() {
  const epoch = epochSource();
  const versions = [snapshot('v1'), snapshot('v2')];
  let releaseFirst;
  const firstBlocked = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const coordinator = new TaskStateCoordinator({
    fetch: async () => {
      const current = versions.shift();
      if (current?.version === 'v1') await firstBlocked;
      return current;
    },
  }, epoch);
  const seen = [];
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));

  const first = coordinator.refresh('conv-1');
  const second = coordinator.invalidate('conv-1');
  releaseFirst();
  await Promise.all([first, second]);

  assert.deepEqual(seen, ['v1', 'v2']);
}

async function testStaleFetchDoesNotPublishOrSchedule() {
  const epoch = epochSource();
  const pending = deferred();
  const seen = [];
  const coordinator = new TaskStateCoordinator({
    fetch: async () => pending.promise,
  }, epoch);
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));

  const refresh = coordinator.refresh('conv-1');
  epoch.invalidate();
  pending.resolve(snapshot('stale', {
    flags: { running: true, delivering: false, needsFollowup: false },
  }));
  await assert.rejects(refresh, /stale/i);
  assert.deepEqual(seen, []);
}

function testApplyRechecksAfterCacheWrite() {
  const epoch = epochSource();
  const seen = [];
  const writes = [];
  const coordinator = new TaskStateCoordinator({
    fetch: async () => snapshot('unused'),
  }, epoch, (conversationId, state, etag, token) => {
    writes.push({ conversationId, state, etag, token });
    epoch.invalidate();
    return state;
  });
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));

  assert.throws(() => coordinator.apply('conv-1', snapshot('v1')), /stale/i);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].token, TOKEN_A);
  assert.deepEqual(seen, []);
}

async function testTimerRefreshConsumesStaleCapture() {
  const epoch = epochSource();
  const scheduled = [];
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  globalThis.setTimeout = (callback) => {
    scheduled.push(callback);
    return scheduled.length;
  };
  globalThis.clearTimeout = () => {};
  try {
    const coordinator = new TaskStateCoordinator({
      fetch: async () => snapshot('v1', {
        flags: { running: true, delivering: false, needsFollowup: false },
      }),
    }, epoch);
    coordinator.subscribe('conv-1', () => {});
    await coordinator.refresh('conv-1');
    assert.equal(scheduled.length, 1);

    epoch.invalidate();
    scheduled[0]();
    await Promise.resolve();
    await Promise.resolve();
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
}

(async () => {
  await testPublishesFetchedStateToSubscribers();
  await testCoalescesInvalidationDuringFetchIntoFreshRead();
  await testStaleFetchDoesNotPublishOrSchedule();
  testApplyRechecksAfterCacheWrite();
  await testTimerRefreshConsumesStaleCapture();
  console.log('task state coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
