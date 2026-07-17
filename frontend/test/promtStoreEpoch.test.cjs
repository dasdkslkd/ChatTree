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

const promptApiModule = path.join(__dirname, '../src/api/prompt.ts');
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const storeModule = path.join(__dirname, '../src/store/promtStore.ts');

const CONTEXT_A = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

let handlers;
let calls;

require.cache[require.resolve(promptApiModule)] = {
  id: promptApiModule,
  filename: promptApiModule,
  loaded: true,
  exports: {
    promptApi: {
      list: (...args) => {
        calls.list += 1;
        return handlers.list(...args);
      },
      load: (...args) => {
        calls.load += 1;
        return handlers.load(...args);
      },
      save: (...args) => {
        calls.save += 1;
        return handlers.save(...args);
      },
      delete: (...args) => {
        calls.delete += 1;
        return handlers.delete(...args);
      },
    },
  },
};

function loadFreshStore() {
  delete require.cache[require.resolve(storeModule)];
  delete require.cache[require.resolve(epochModule)];
  const { connectionEpochRuntime } = require(epochModule);
  connectionEpochRuntime.install(CONTEXT_A);
  const { promptStore } = require(storeModule);
  return { promptStore, connectionEpochRuntime };
}

function resetHandlers() {
  calls = { list: 0, load: 0, save: 0, delete: 0 };
  handlers = {
    list: async () => ({ prompts: [] }),
    load: async () => null,
    save: async () => undefined,
    delete: async () => undefined,
  };
}

async function testAllDelayedActionsIgnoreStaleCompletion() {
  resetHandlers();
  const list = deferred();
  const load = deferred();
  const save = deferred();
  const remove = deferred();
  handlers.list = () => list.promise;
  handlers.load = () => load.promise;
  handlers.save = () => save.promise;
  handlers.delete = () => remove.promise;

  const { promptStore, connectionEpochRuntime } = loadFreshStore();
  const originalPrompt = { id: 'prompt-old', name: 'old', content: 'old' };
  promptStore.setState({
    prompts: [{ id: 'prompt-old', name: 'old' }],
    currentPrompt: originalPrompt,
    loading: false,
    error: 'keep-before-start',
  });

  const actions = promptStore.getState();
  const pending = [
    actions.loadPrompts(),
    actions.loadPrompt('prompt-new'),
    actions.savePrompt({ id: 'prompt-save', name: 'save', content: 'save' }),
    actions.deletePrompt('prompt-old'),
  ];
  const token = connectionEpochRuntime.capture();
  const atInvalidation = promptStore.getState();
  connectionEpochRuntime.invalidate(token);

  list.resolve({ prompts: [{ id: 'prompt-new', name: 'new' }] });
  load.resolve({ id: 'prompt-new', name: 'new', content: 'new' });
  save.resolve();
  remove.resolve();
  await Promise.all(pending);

  const after = promptStore.getState();
  assert.deepEqual(after.prompts, atInvalidation.prompts);
  assert.equal(after.currentPrompt, atInvalidation.currentPrompt);
  assert.equal(after.loading, atInvalidation.loading);
  assert.equal(after.error, atInvalidation.error);
  assert.deepEqual(calls, { list: 1, load: 1, save: 1, delete: 1 });
}

async function testCaptureFailureIsNeutralForAllActions() {
  resetHandlers();
  const { promptStore, connectionEpochRuntime } = loadFreshStore();
  promptStore.setState({
    prompts: [{ id: 'prompt-old', name: 'old' }],
    currentPrompt: { id: 'prompt-old', name: 'old', content: 'old' },
    loading: false,
    error: 'unchanged',
  });
  connectionEpochRuntime.invalidate(connectionEpochRuntime.capture());
  const before = promptStore.getState();
  const actions = promptStore.getState();

  await Promise.all([
    actions.loadPrompts(),
    actions.loadPrompt('prompt-new'),
    actions.savePrompt({ id: 'prompt-save', name: 'save', content: 'save' }),
    actions.deletePrompt('prompt-old'),
  ]);

  const after = promptStore.getState();
  assert.deepEqual(after.prompts, before.prompts);
  assert.equal(after.currentPrompt, before.currentPrompt);
  assert.equal(after.loading, before.loading);
  assert.equal(after.error, before.error);
  assert.deepEqual(calls, { list: 0, load: 0, save: 0, delete: 0 });
}

(async () => {
  await testAllDelayedActionsIgnoreStaleCompletion();
  await testCaptureFailureIsNeutralForAllActions();
  console.log('prompt store epoch tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
