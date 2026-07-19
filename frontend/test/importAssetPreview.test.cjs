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
  ImportAssetMutationOwner,
  ImportAssetMutationQueue,
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

function testInstallRemoveAndClearRevokeOwnedUrls() {
  const urls = objectUrlFixture();
  const cache = new ImportAssetPreviewCache(async () => new Blob(), urls.api);
  cache.installFile('conv-1', 'first.png', new Blob(['first']));
  cache.installFile('conv-1', 'first.png', new Blob(['replacement']));
  cache.installFile('conv-1', 'second.png', new Blob(['second']));

  assert.deepEqual(urls.revoked, ['blob:preview-1']);
  cache.remove('conv-1', 'first.png');
  assert.deepEqual(urls.revoked, ['blob:preview-1', 'blob:preview-2']);
  cache.clear();
  assert.deepEqual(
    urls.revoked,
    ['blob:preview-1', 'blob:preview-2', 'blob:preview-3'],
  );
}

function testMutationOwnerPreventsOlderRenameFromReclaimingAsset() {
  const owner = new ImportAssetMutationOwner();
  const first = owner.begin('conv-1', 'old.png');
  const newer = owner.begin('conv-1', 'old.png');

  assert.equal(owner.owns(first), false);
  assert.equal(owner.claim(first, 'conv-1', 'new.png'), null);
  const claimed = owner.claim(newer, 'conv-1', 'new.png');
  assert.ok(claimed);
  assert.equal(owner.owns(claimed), true);
  owner.clear();
  assert.equal(owner.owns(claimed), false);
}

async function testMutationQueueSerializesOnlySameAsset() {
  const queue = new ImportAssetMutationQueue();
  const firstBlocked = deferred();
  const calls = [];
  const first = queue.run('conv-1', 'same.png', async () => {
    calls.push('first:start');
    await firstBlocked.promise;
    calls.push('first:end');
  });
  const second = queue.run('conv-1', 'same.png', async () => {
    calls.push('second');
  });
  const independent = queue.run('conv-1', 'other.png', async () => {
    calls.push('other');
  });

  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls, ['first:start', 'other']);
  firstBlocked.resolve();
  await Promise.all([first, second, independent]);
  assert.deepEqual(calls, ['first:start', 'other', 'first:end', 'second']);
}

(async () => {
  await testCacheCoalescesSameAssetAndPublishesObjectUrl();
  await testRemovingPendingAssetAbortsAndSuppressesLateResult();
  testInstallRemoveAndClearRevokeOwnedUrls();
  testMutationOwnerPreventsOlderRenameFromReclaimingAsset();
  await testMutationQueueSerializesOnlySameAsset();
  console.log('importAssetPreview tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
