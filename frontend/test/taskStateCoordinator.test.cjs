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

const taskStateModule = path.join(__dirname, '../src/api/taskState.ts');
require.cache[require.resolve(taskStateModule)] = {
  id: taskStateModule,
  filename: taskStateModule,
  loaded: true,
  exports: {
    taskStateApi: {},
    storeTaskState: (_conversationId, state) => state,
  },
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


function mockTimers() {
  const timers = [];
  const originalSetTimeout = global.setTimeout;
  const originalClearTimeout = global.clearTimeout;
  global.setTimeout = (callback, delay) => {
    const timer = { callback, delay };
    timers.push(timer);
    return timer;
  };
  global.clearTimeout = (timer) => {
    const index = timers.indexOf(timer);
    if (index >= 0) timers.splice(index, 1);
  };
  return {
    timers,
    async fireNext() {
      const timer = timers.shift();
      if (!timer) return false;
      timer.callback();
      await new Promise((resolve) => setImmediate(resolve));
      return true;
    },
    restore() {
      global.setTimeout = originalSetTimeout;
      global.clearTimeout = originalClearTimeout;
    },
  };
}

function runningSnapshot(version) {
  return snapshot(version, { flags: { running: true, delivering: false, needsFollowup: false } });
}

async function testStopsSchedulingWhenVersionUnchanged() {
  const timers = mockTimers();
  try {
    const coordinator = new TaskStateCoordinator({
      fetch: async () => runningSnapshot('v1'),
    });
    coordinator.subscribe('conv-1', () => {});
    await coordinator.refresh('conv-1');
    assert.equal(timers.timers.length, 1, 'first changed version should schedule a refresh');
    assert.equal(timers.timers[0].delay, 1000);

    await timers.fireNext();
    assert.equal(timers.timers.length, 0, 'unchanged version must stop polling');
  } finally {
    timers.restore();
  }
}

async function testSchedulesBackoffThenStopsWhenVersionStabilizes() {
  const versions = [runningSnapshot('v1'), runningSnapshot('v2'), runningSnapshot('v2')];
  const timers = mockTimers();
  try {
    const coordinator = new TaskStateCoordinator({
      fetch: async () => versions.shift(),
    });
    coordinator.subscribe('conv-1', () => {});
    await coordinator.refresh('conv-1');
    assert.deepEqual(timers.timers.map((timer) => timer.delay), [1000]);

    await timers.fireNext();
    assert.deepEqual(timers.timers.map((timer) => timer.delay), [2000], 'changed version backs off');

    await timers.fireNext();
    assert.deepEqual(timers.timers, [], 'stabilized version stops backoff polling');
  } finally {
    timers.restore();
  }
}
(async () => {
  await testPublishesFetchedStateToSubscribers();
  await testCoalescesInvalidationDuringFetchIntoFreshRead();
  await testStopsSchedulingWhenVersionUnchanged();
  await testSchedulesBackoffThenStopsWhenVersionStabilizes();
  console.log('task state coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
