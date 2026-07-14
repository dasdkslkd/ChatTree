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

const {
  TaskStateCoordinator,
} = require(path.join(__dirname, '../src/services/taskStateCoordinator.ts'));

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
  const coordinator = new TaskStateCoordinator({
    fetch: async () => snapshot('v1'),
  });
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));
  const state = await coordinator.refresh('conv-1');

  assert.equal(state.version, 'v1');
  assert.deepEqual(seen, ['v1']);
}

async function testCoalescesInvalidationDuringFetchIntoFreshRead() {
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
  });
  const seen = [];
  coordinator.subscribe('conv-1', (state) => seen.push(state.version));

  const first = coordinator.refresh('conv-1');
  const second = coordinator.invalidate('conv-1');
  releaseFirst();
  await Promise.all([first, second]);

  assert.deepEqual(seen, ['v1', 'v2']);
}

(async () => {
  await testPublishesFetchedStateToSubscribers();
  await testCoalescesInvalidationDuringFetchIntoFreshRead();
  console.log('task state coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
