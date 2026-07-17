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
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  ImportAssetMutationOwner,
  ImportAssetMutationQueue,
  ImportAssetPreviewCache,
} = require('../src/runtime/importAssetPreview.ts');
const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const conversationSource = fs.readFileSync(path.join(__dirname, '../src/api/conversation.ts'), 'utf8');

const TOKEN_A = Object.freeze({
  profileId: 'profile-a',
  serverInstanceId: 'server-a',
  connectionEpoch: 1,
  connectionLeaseId: '11111111-1111-4111-8111-111111111111',
  generation: 1,
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function createHarness(fetchBlob) {
  let current = true;
  let nextUrl = 0;
  const created = [];
  const revoked = [];
  const cache = new ImportAssetPreviewCache(
    fetchBlob,
    { isCurrent: (token) => current && token.generation === TOKEN_A.generation },
    {
      createObjectURL(value) {
        created.push(value);
        nextUrl += 1;
        return `blob:test-${nextUrl}`;
      },
      revokeObjectURL(url) {
        revoked.push(url);
      },
    },
  );
  return { cache, created, revoked, invalidate: () => { current = false; } };
}

async function testDeduplicatesByTokenValue() {
  const work = deferred();
  let calls = 0;
  const harness = createHarness(() => {
    calls += 1;
    return work.promise;
  });
  const equivalentToken = Object.freeze({ ...TOKEN_A });
  const first = harness.cache.load('conversation-1', 'image.png', TOKEN_A);
  const second = harness.cache.load('conversation-1', 'image.png', equivalentToken);
  assert.equal(first, second);
  work.resolve(new Blob(['image']));
  assert.equal(await first, 'blob:test-1');
  assert.equal(calls, 1);
  assert.equal(harness.cache.peek('conversation-1', 'image.png'), 'blob:test-1');
}

async function testStaleCompletionCannotInstall() {
  const work = deferred();
  const harness = createHarness(() => work.promise);
  const pending = harness.cache.load('conversation-1', 'old.png', TOKEN_A);
  harness.invalidate();
  work.resolve(new Blob(['old']));
  assert.equal(await pending, null);
  assert.equal(harness.created.length, 0);
  assert.equal(harness.cache.peek('conversation-1', 'old.png'), null);
}

async function testLocalFileReplacesPendingRemoteLoad() {
  const work = deferred();
  let observedSignal;
  const harness = createHarness((_conversationId, _filename, _token, signal) => {
    observedSignal = signal;
    return work.promise;
  });
  const pending = harness.cache.load('conversation-1', 'image.png', TOKEN_A);
  const localFile = new Blob(['local']);
  assert.equal(harness.cache.installFile('conversation-1', 'image.png', localFile, TOKEN_A), 'blob:test-1');
  assert.equal(observedSignal.aborted, true);
  work.resolve(new Blob(['remote']));
  assert.equal(await pending, null);
  assert.equal(harness.cache.peek('conversation-1', 'image.png'), 'blob:test-1');
  assert.deepEqual(harness.created, [localFile]);
}

async function testFailureCreatesNoUrlAndCanRetry() {
  let calls = 0;
  const harness = createHarness(async () => {
    calls += 1;
    if (calls === 1) throw new Error('404');
    return new Blob(['retry']);
  });
  await assert.rejects(
    () => harness.cache.load('conversation-1', 'missing.png', TOKEN_A),
    /404/,
  );
  assert.equal(harness.created.length, 0);
  assert.equal(await harness.cache.load('conversation-1', 'missing.png', TOKEN_A), 'blob:test-1');
}

async function testRemoveAndClearRevokeOwnedUrlsOnce() {
  const harness = createHarness(async (conversationId) => new Blob([conversationId]));
  let notifications = 0;
  const unsubscribe = harness.cache.subscribe(() => { notifications += 1; });
  await harness.cache.load('conversation-1', 'one.png', TOKEN_A);
  await harness.cache.load('conversation-2', 'two.png', TOKEN_A);
  harness.cache.remove('conversation-1', 'one.png');
  harness.cache.remove('conversation-1', 'one.png');
  harness.cache.clear();
  harness.cache.clear();
  unsubscribe();
  assert.deepEqual(harness.revoked, ['blob:test-1', 'blob:test-2']);
  assert.equal(notifications, 4);
}

function testDomPreviewSourcesAreLeaseFetchedBlobUrlsOnly() {
  assert.doesNotMatch(
    mainPageSource,
    /serverApiUrl\(`\/conversations\/\$\{conversationId\}\/imports/,
    'browser image sources must not use an unheadered proxy URL',
  );
  assert.match(
    mainPageSource,
    /url: getImportAssetPreviewUrl\(ref\.filename\)/,
    'composer images should only receive URLs owned by the preview cache',
  );
  assert.match(
    mainPageSource,
    /importAssetPreviewCache\.installFile\(convId, res\.filename, file, token\)/,
    'newly uploaded local images should replace any pending remote preview',
  );
  assert.match(
    mainPageSource,
    /const uploadMutation = importAssetMutationOwner\.begin\(convId, file\.name\);[\s\S]*conversationApi\.uploadImport\(convId, file\);[\s\S]*importAssetMutationOwner\.claim\(\s*uploadMutation,\s*convId,\s*res\.filename,?\s*\)/,
    'upload ownership must be ordered at user action time and claimed for the server filename',
  );
  assert.match(
    mainPageSource,
    /const mutation = importAssetMutationOwner\.begin\(conversationId, filename\);[\s\S]*importAssetPreviewCache\.remove\(conversationId, filename\);[\s\S]*importAssetMutationOwner\.owns\(mutation\)/,
    'remove should cancel previews immediately and reject superseded completions',
  );
  assert.match(
    mainPageSource,
    /importAssetMutationQueue\.run\(\s*convId,\s*file\.name,[\s\S]*conversationApi\.uploadImport\(convId, file\)/,
    'same-name uploads must be serialized before reaching the server',
  );
  assert.match(
    mainPageSource,
    /importAssetMutationQueue\.run\(\s*conversationId,\s*filename,[\s\S]*conversationApi\.deleteImport\(conversationId, filename\)/,
    'delete must share the same remote mutation queue as upload',
  );
  assert.match(
    conversationSource,
    /if \(!response\.ok\) \{[\s\S]*throw new Error/,
    'ordinary non-2xx responses must reject before blob consumption',
  );
  assert.match(
    conversationSource,
    /const blob = await response\.blob\(\);[\s\S]*assertCurrent\(token\)/,
    'blob consumption must be followed by a token check',
  );
}

function testMutationOwnerRejectsLateSameNameCompletion() {
  const owner = new ImportAssetMutationOwner();
  const olderUpload = owner.begin('conversation-1', 'same.png');
  const newerUpload = owner.begin('conversation-1', 'same.png');
  const newerClaim = owner.claim(newerUpload, 'conversation-1', 'same.png');
  const olderClaim = owner.claim(olderUpload, 'conversation-1', 'same.png');
  assert.ok(newerClaim);
  assert.equal(olderClaim, null, 'response order must not let the older upload win');
  assert.equal(owner.owns(newerClaim), true);

  const renamedUpload = owner.begin('conversation-1', 'local-name.png');
  const remove = owner.begin('conversation-1', 'server-name.png');
  assert.equal(
    owner.claim(renamedUpload, 'conversation-1', 'server-name.png'),
    null,
    'a newer remove on the server filename must supersede a pending upload',
  );
  assert.equal(owner.owns(remove), true);

  const otherConversation = owner.begin('conversation-2', 'same.png');
  const otherClaim = owner.claim(otherConversation, 'conversation-2', 'same.png');
  assert.ok(otherClaim);
  assert.equal(owner.owns(otherClaim), true);
  owner.clear();
  assert.equal(owner.owns(newerClaim), false);
  assert.equal(owner.owns(otherClaim), false);
}

async function testMutationQueuePreservesActionOrderAndRecoversAfterFailure() {
  const queue = new ImportAssetMutationQueue();
  const first = deferred();
  const calls = [];
  const oldUpload = queue.run('conversation-1', 'same.png', async () => {
    calls.push('old:start');
    await first.promise;
    calls.push('old:end');
    return 'old';
  });
  const newerUpload = queue.run('conversation-1', 'same.png', async () => {
    calls.push('new');
    return 'new';
  });
  const otherFile = queue.run('conversation-1', 'other.png', async () => {
    calls.push('other');
    return 'other';
  });

  await Promise.resolve();
  assert.deepEqual(calls, ['old:start', 'other']);
  first.resolve();
  assert.deepEqual(
    await Promise.all([oldUpload, newerUpload, otherFile]),
    ['old', 'new', 'other'],
  );
  assert.deepEqual(calls, ['old:start', 'other', 'old:end', 'new']);

  await assert.rejects(
    queue.run('conversation-1', 'same.png', async () => {
      throw new Error('failed mutation');
    }),
    /failed mutation/,
  );
  assert.equal(
    await queue.run('conversation-1', 'same.png', async () => 'after-failure'),
    'after-failure',
  );
}

async function main() {
  await testDeduplicatesByTokenValue();
  await testStaleCompletionCannotInstall();
  await testLocalFileReplacesPendingRemoteLoad();
  await testFailureCreatesNoUrlAndCanRetry();
  await testRemoveAndClearRevokeOwnedUrlsOnce();
  testDomPreviewSourcesAreLeaseFetchedBlobUrlsOnly();
  testMutationOwnerRejectsLateSameNameCompletion();
  await testMutationQueuePreservesActionOrderAndRecoversAfterFailure();
  console.log('PASS importAssetPreview');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
