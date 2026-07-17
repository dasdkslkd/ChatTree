const assert = require('node:assert/strict');
const fs = require('node:fs');
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

const { resolveProjectWorkspaceForEpoch } = require('../src/runtime/projectWorkspaceEpoch.ts');

const TOKEN = Object.freeze({
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

async function testCurrentResolutionCommitsAndFinishes() {
  const events = [];
  const workspace = { cwd: 'D:/project', label: 'project' };
  const result = await resolveProjectWorkspaceForEpoch(TOKEN, {
    resolve: async () => workspace,
    onSuccess: (value) => events.push(['success', value]),
    onError: (error) => events.push(['error', error]),
    onFinally: () => events.push(['finally']),
  }, { isCurrent: () => true });
  assert.equal(result, workspace);
  assert.deepEqual(events, [['success', workspace], ['finally']]);
}

async function testInvalidationSuppressesEveryCompletionCommit() {
  const work = deferred();
  const events = [];
  let current = true;
  const resultPromise = resolveProjectWorkspaceForEpoch(TOKEN, {
    resolve: () => work.promise,
    onSuccess: (value) => events.push(['success', value]),
    onError: (error) => events.push(['error', error]),
    onFinally: () => events.push(['finally']),
  }, { isCurrent: () => current });
  current = false;
  work.resolve({ cwd: 'D:/stale', label: 'stale' });
  assert.equal(await resultPromise, null);
  assert.deepEqual(events, []);
}

async function testCurrentFailureCommitsErrorAndFinally() {
  const expected = new Error('resolve failed');
  const events = [];
  const result = await resolveProjectWorkspaceForEpoch(TOKEN, {
    resolve: async () => { throw expected; },
    onSuccess: () => events.push(['success']),
    onError: (error) => events.push(['error', error]),
    onFinally: () => events.push(['finally']),
  }, { isCurrent: () => true });
  assert.equal(result, null);
  assert.deepEqual(events, [['error', expected], ['finally']]);
}

async function main() {
  await testCurrentResolutionCommitsAndFinishes();
  await testInvalidationSuppressesEveryCompletionCommit();
  await testCurrentFailureCommitsErrorAndFinally();
  console.log('PASS projectWorkspaceEpoch');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
