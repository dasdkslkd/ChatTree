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

const clientModule = path.join(__dirname, '../src/api/client.ts');
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const taskStateModule = path.join(__dirname, '../src/api/taskState.ts');

const CONTEXT_A = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

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

let getHandler;
let postHandler;
let requests;

require.cache[require.resolve(clientModule)] = {
  id: clientModule,
  filename: clientModule,
  loaded: true,
  exports: {
    apiClient: {
      get: (url, options) => {
        requests.push({ method: 'GET', url, options });
        return getHandler(url, options);
      },
      post: (url, data) => {
        requests.push({ method: 'POST', url, data });
        return postHandler(url, data);
      },
    },
  },
};

function loadFreshApi() {
  requests = [];
  getHandler = async () => ({ status: 200, data: snapshot('default'), headers: {} });
  postHandler = async () => ({ status: 200, data: snapshot('default'), headers: {} });
  delete require.cache[require.resolve(taskStateModule)];
  delete require.cache[require.resolve(epochModule)];
  const { connectionEpochRuntime, StaleConnectionEpochError } = require(epochModule);
  connectionEpochRuntime.install(CONTEXT_A);
  const taskState = require(taskStateModule);
  return { ...taskState, connectionEpochRuntime, StaleConnectionEpochError };
}

async function testFetchReusesCurrentCacheOn304() {
  const { taskStateApi, storeTaskState } = loadFreshApi();
  const cached = snapshot('v1');
  storeTaskState('conv-1', cached, '"v1"');
  getHandler = async () => ({ status: 304, data: null, headers: {} });

  const result = await taskStateApi.fetch('conv-1');

  assert.equal(result, cached);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].options.headers['If-None-Match'], '"v1"');
}

async function testClearedCacheIsNotReintroducedByDelayed304() {
  const { taskStateApi, storeTaskState } = loadFreshApi();
  storeTaskState('conv-1', snapshot('v1'), '"v1"');
  const pending = deferred();
  getHandler = () => pending.promise;

  const fetch = taskStateApi.fetch('conv-1');
  taskStateApi.clear('conv-1');
  pending.resolve({ status: 304, data: null, headers: {} });
  await assert.rejects(fetch, /cache changed/i);

  getHandler = async () => ({ status: 200, data: snapshot('v2'), headers: { etag: '"v2"' } });
  await taskStateApi.fetch('conv-1');
  assert.equal(requests[1].options.headers, undefined);
}

async function testBindAndDeletePopulateEtagCache() {
  const { taskStateApi } = loadFreshApi();
  postHandler = async (url) => {
    const version = url.endsWith('/bind') ? 'v-bind' : 'v-delete';
    return { status: 200, data: snapshot(version), headers: { etag: `"${version}"` } };
  };
  getHandler = async () => ({ status: 304, data: null, headers: {} });

  await taskStateApi.bind('conv-1', 'notification-1', 'node-1');
  const afterBind = await taskStateApi.fetch('conv-1');
  assert.equal(afterBind.version, 'v-bind');
  assert.equal(requests[1].options.headers['If-None-Match'], '"v-bind"');

  await taskStateApi.delete('conv-1', 'notification-1');
  const afterDelete = await taskStateApi.fetch('conv-1');
  assert.equal(afterDelete.version, 'v-delete');
  assert.equal(requests[3].options.headers['If-None-Match'], '"v-delete"');
}

async function testAllDelayedOperationsRejectAfterInvalidation() {
  const {
    taskStateApi,
    connectionEpochRuntime,
    StaleConnectionEpochError,
  } = loadFreshApi();
  const pendingFetch = deferred();
  const pendingBind = deferred();
  const pendingDelete = deferred();
  getHandler = () => pendingFetch.promise;
  postHandler = (url) => (
    url.endsWith('/bind') ? pendingBind.promise : pendingDelete.promise
  );

  const operations = [
    taskStateApi.fetch('conv-1'),
    taskStateApi.bind('conv-1', 'notification-1', 'node-1'),
    taskStateApi.delete('conv-1', 'notification-1'),
  ];
  connectionEpochRuntime.invalidate(connectionEpochRuntime.capture());
  pendingFetch.resolve({ status: 200, data: snapshot('stale-fetch'), headers: { etag: '"stale"' } });
  pendingBind.resolve({ status: 200, data: snapshot('stale-bind'), headers: { etag: '"stale"' } });
  pendingDelete.resolve({ status: 200, data: snapshot('stale-delete'), headers: { etag: '"stale"' } });

  for (const operation of operations) {
    await assert.rejects(operation, StaleConnectionEpochError);
  }
  await assert.rejects(() => taskStateApi.fetch('conv-1'), StaleConnectionEpochError);
  assert.equal(requests.length, 3);
}

async function testHeaderGetterInvalidationClosesPostResponseWindow() {
  const {
    taskStateApi,
    connectionEpochRuntime,
    StaleConnectionEpochError,
  } = loadFreshApi();
  getHandler = async () => ({
    status: 200,
    data: snapshot('must-not-store'),
    get headers() {
      connectionEpochRuntime.invalidate(connectionEpochRuntime.capture());
      return { etag: '"must-not-store"' };
    },
  });

  await assert.rejects(() => taskStateApi.fetch('conv-1'), StaleConnectionEpochError);
}

async function test304StatusGetterInvalidationPreventsReuse() {
  const {
    taskStateApi,
    storeTaskState,
    connectionEpochRuntime,
    StaleConnectionEpochError,
  } = loadFreshApi();
  storeTaskState('conv-1', snapshot('cached'), '"cached"');
  getHandler = async () => ({
    get status() {
      connectionEpochRuntime.invalidate(connectionEpochRuntime.capture());
      return 304;
    },
    data: null,
    headers: {},
  });

  await assert.rejects(() => taskStateApi.fetch('conv-1'), StaleConnectionEpochError);
}

(async () => {
  await testFetchReusesCurrentCacheOn304();
  await testClearedCacheIsNotReintroducedByDelayed304();
  await testBindAndDeletePopulateEtagCache();
  await testAllDelayedOperationsRejectAfterInvalidation();
  await testHeaderGetterInvalidationClosesPostResponseWindow();
  await test304StatusGetterInvalidationPreventsReuse();
  console.log('task state API tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
