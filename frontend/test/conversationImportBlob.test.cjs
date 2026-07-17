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

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
const leaseId = '11111111-1111-4111-8111-111111111111';

function deferred() {
  let resolve;
  const promise = new Promise((res) => { resolve = res; });
  return { promise, resolve };
}

function fakeResponse(status, blob) {
  let blobCalls = 0;
  let cancelCalls = 0;
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers({ 'X-ChatTree-Connection-Lease-ID': leaseId }),
    body: {
      async cancel() { cancelCalls += 1; },
    },
    async blob() {
      blobCalls += 1;
      return typeof blob === 'function' ? blob() : blob;
    },
    get blobCalls() { return blobCalls; },
    get cancelCalls() { return cancelCalls; },
  };
}

async function main() {
  globalThis.window = {
    __CHATTREE_BOOTSTRAP__: {
      profileId: 'profile-a',
      apiBase: '/p/profile-a/api/v1',
    },
    location: {
      href: 'http://127.0.0.1:18100/s/profile-a',
      pathname: '/s/profile-a',
    },
  };
  const { initializeFrontendBootstrap } = require('../src/runtime/frontendBootstrap.ts');
  initializeFrontendBootstrap();
  const {
    connectionEpochRuntime,
    StaleConnectionEpochError,
  } = require('../src/runtime/connectionEpoch.ts');
  connectionEpochRuntime.install({
    profileId: 'profile-a',
    apiBase: '/p/profile-a/api/v1',
    serverInstanceId: '22222222-2222-4222-8222-222222222222',
    connectionEpoch: 1,
    connectionLeaseId: leaseId,
  });
  const token = connectionEpochRuntime.capture();
  const { conversationApi } = require('../src/api/conversation.ts');

  let nextResponse;
  const requests = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ input, init });
    return nextResponse;
  };

  const missing = fakeResponse(404, new Blob(['not an image']));
  nextResponse = missing;
  await assert.rejects(
    () => conversationApi.fetchImportBlob('conversation-1', 'missing.png', token),
    /status 404/,
  );
  assert.equal(missing.blobCalls, 0);
  assert.equal(missing.cancelCalls, 1);

  const expectedBlob = new Blob(['image']);
  nextResponse = fakeResponse(200, expectedBlob);
  assert.equal(
    await conversationApi.fetchImportBlob('conversation-1', 'image.png', token),
    expectedBlob,
  );
  assert.equal(
    requests.at(-1).init.headers.get('X-ChatTree-Connection-Lease-ID'),
    leaseId,
  );
  assert.equal(
    requests.at(-1).input,
    '/p/profile-a/api/v1/conversations/conversation-1/imports/image.png',
  );

  const delayed = deferred();
  nextResponse = fakeResponse(200, () => delayed.promise);
  const staleRead = conversationApi.fetchImportBlob(
    'conversation-1',
    'stale.png',
    token,
  );
  await Promise.resolve();
  connectionEpochRuntime.invalidate(token);
  delayed.resolve(new Blob(['stale']));
  await assert.rejects(staleRead, StaleConnectionEpochError);

  console.log('PASS conversationImportBlob');
}

main()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(() => {
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });
