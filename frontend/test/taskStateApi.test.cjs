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
    },
  }).outputText;
  module._compile(output, filename);
};

const clientPath = path.join(__dirname, '../src/api/client.ts');
const taskStatePath = path.join(__dirname, '../src/api/taskState.ts');
const requests = [];
let getHandler;
let postHandler;

require.cache[require.resolve(clientPath)] = {
  id: clientPath,
  filename: clientPath,
  loaded: true,
  exports: {
    apiClient: {
      get(url, options) {
        requests.push({ method: 'GET', url, options });
        return getHandler(url, options);
      },
      post(url, data) {
        requests.push({ method: 'POST', url, data });
        return postHandler(url, data);
      },
    },
  },
};

const { taskStateApi, storeTaskState } = require(taskStatePath);

function snapshot(version, overrides = {}) {
  return {
    conversation_id: 'conv-1',
    task: null,
    flags: { running: false },
    version,
    ...overrides,
  };
}

async function testFetchUsesEtagAndReusesCachedSnapshotOn304() {
  requests.length = 0;
  taskStateApi.clear('conv/1');
  const cached = snapshot('v1', { conversation_id: 'conv/1' });
  storeTaskState('conv/1', cached, '"v1"');
  getHandler = async () => ({ status: 304, data: null, headers: {} });

  const result = await taskStateApi.fetch('conv/1');

  assert.equal(result, cached);
  assert.equal(requests[0].url, '/conversations/conv%2F1/task-state');
  assert.equal(requests[0].options.headers['If-None-Match'], '"v1"');
  assert.equal(requests[0].options.validateStatus(304), true);
}

async function testFreshFetchNormalizesAndStoresResponseEtag() {
  requests.length = 0;
  taskStateApi.clear('conv-2');
  getHandler = async () => ({
    status: 200,
    data: { flags: { running: 1 }, notifications: null },
    headers: { etag: '"v2"' },
  });

  const result = await taskStateApi.fetch('conv-2');
  assert.deepEqual(result, {
    conversation_id: 'conv-2',
    task: null,
    flags: { running: true },
    version: '',
  });

  getHandler = async () => ({ status: 304, data: null, headers: {} });
  assert.equal(await taskStateApi.fetch('conv-2'), result);
}

(async () => {
  await testFetchUsesEtagAndReusesCachedSnapshotOn304();
  await testFreshFetchNormalizesAndStoresResponseEtag();
  console.log('taskStateApi tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
