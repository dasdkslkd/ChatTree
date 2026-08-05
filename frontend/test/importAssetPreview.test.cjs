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
  }).outputText;
  module._compile(output, filename);
};

const {
  ImportAssetPreviewCache,
} = require('../src/runtime/importAssetPreview.ts');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function objectUrlFixture() {
  let next = 0;
  const created = [];
  const revoked = [];
  return {
    api: {
      createObjectURL(value) {
        created.push(value);
        next += 1;
        return `blob:preview-${next}`;
      },
      revokeObjectURL(value) {
        revoked.push(value);
      },
    },
    created,
    revoked,
  };
}

async function testCacheCoalescesSameAssetAndPublishesObjectUrl() {
  const pending = deferred();
  const signals = [];
  const urls = objectUrlFixture();
  const cache = new ImportAssetPreviewCache(
    async (_conversationId, _filename, signal) => {
      signals.push(signal);
      return pending.promise;
    },
    urls.api,
  );
  let notifications = 0;
  cache.subscribe(() => {
    notifications += 1;
  });

  const first = cache.load('conv-1', 'image.png');
  const second = cache.load('conv-1', 'image.png');
  assert.equal(first, second);
  assert.equal(signals.length, 1);

  const blob = new Blob(['image']);
  pending.resolve(blob);
  assert.equal(await first, 'blob:preview-1');
  assert.equal(cache.peek('conv-1', 'image.png'), 'blob:preview-1');
  assert.deepEqual(urls.created, [blob]);
  assert.equal(notifications, 1);
}

async function testRemovingPendingAssetAbortsAndSuppressesLateResult() {
  const pending = deferred();
  let signal;
  const urls = objectUrlFixture();
  const cache = new ImportAssetPreviewCache(
    async (_conversationId, _filename, ownerSignal) => {
      signal = ownerSignal;
      return pending.promise;
    },
    urls.api,
  );

  const load = cache.load('conv-1', 'late.png');
  cache.remove('conv-1', 'late.png');
  assert.equal(signal.aborted, true);
  pending.resolve(new Blob(['late']));

  assert.equal(await load, null);
  assert.equal(cache.peek('conv-1', 'late.png'), null);
  assert.deepEqual(urls.created, []);
}

(async () => {
  await testCacheCoalescesSameAssetAndPublishesObjectUrl();
  await testRemovingPendingAssetAbortsAndSuppressesLateResult();
  console.log('importAssetPreview tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
