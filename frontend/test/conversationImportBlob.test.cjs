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
const leaseFetchPath = path.join(__dirname, '../src/api/leaseFetch.ts');
const conversationPath = path.join(__dirname, '../src/api/conversation.ts');
let fetchHandler;

require.cache[require.resolve(clientPath)] = {
  id: clientPath,
  filename: clientPath,
  loaded: true,
  exports: {
    apiClient: {
      get: async () => ({ data: {} }),
      post: async () => ({ data: {} }),
      patch: async () => ({ data: {} }),
      delete: async () => ({ data: {} }),
    },
  },
};
require.cache[require.resolve(leaseFetchPath)] = {
  id: leaseFetchPath,
  filename: leaseFetchPath,
  loaded: true,
  exports: {
    leaseGuardedFetch: (...args) => fetchHandler(...args),
  },
};

const { conversationApi } = require(conversationPath);
const { ChatTreeApiError } = require('../src/api/errors.ts');

async function testFetchImportBlobUsesLeaseFetchAndCallerSignal() {
  const blob = new Blob(['asset'], { type: 'image/png' });
  const controller = new AbortController();
  const calls = [];
  fetchHandler = async (...args) => {
    calls.push(args);
    return new Response(blob, { status: 200 });
  };

  const result = await conversationApi.fetchImportBlob(
    'conv-1',
    'image one.png',
    controller.signal,
  );

  assert.equal(await result.text(), 'asset');
  assert.deepEqual(calls, [[
    '/conversations/conv-1/imports/image%20one.png',
    { signal: controller.signal },
  ]]);
}

async function testFetchImportBlobPreservesModernErrorEnvelope() {
  fetchHandler = async () => new Response(JSON.stringify({
    error: {
      code: 'import_not_found',
      message: 'Import asset does not exist',
      retryable: false,
      request_id: 'import-tree-1',
    },
  }), {
    status: 404,
    headers: { 'Content-Type': 'application/json' },
  });

  await assert.rejects(
    conversationApi.fetchImportBlob('conv-1', 'missing.png'),
    (error) => (
      error instanceof ChatTreeApiError
      && error.status === 404
      && error.code === 'import_not_found'
      && error.retryable === false
      && error.requestId === 'import-tree-1'
    ),
  );
}

(async () => {
  await testFetchImportBlobUsesLeaseFetchAndCallerSignal();
  await testFetchImportBlobPreservesModernErrorEnvelope();
  console.log('conversationImportBlob tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
