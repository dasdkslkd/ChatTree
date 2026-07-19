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
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const { installPageLifecycleFlush } = require('../src/runtime/pageLifecycle.ts');

function createTarget(extra = {}) {
  const listeners = new Map();
  const added = [];
  const removed = [];

  return {
    ...extra,
    listeners,
    added,
    removed,
    addEventListener(type, listener) {
      added.push(type);
      const current = listeners.get(type) ?? new Set();
      current.add(listener);
      listeners.set(type, current);
    },
    removeEventListener(type, listener) {
      removed.push(type);
      listeners.get(type)?.delete(listener);
    },
    emit(type) {
      for (const listener of [...(listeners.get(type) ?? [])]) listener();
    },
    listenerCount(type) {
      return listeners.get(type)?.size ?? 0;
    },
  };
}

function testLifecycleTargetsAndFinalFlush() {
  const page = createTarget();
  const visibility = createTarget({ visibilityState: 'visible' });
  let flushes = 0;
  const dispose = installPageLifecycleFlush(page, visibility, () => { flushes += 1; });

  assert.deepEqual(page.added, ['pagehide', 'beforeunload']);
  assert.deepEqual(visibility.added, ['visibilitychange']);
  assert.equal(page.listenerCount('visibilitychange'), 0);
  assert.equal(visibility.listenerCount('pagehide'), 0);
  assert.equal(visibility.listenerCount('beforeunload'), 0);

  visibility.emit('visibilitychange');
  assert.equal(flushes, 0, 'visible changes must not flush');
  visibility.visibilityState = 'hidden';
  visibility.emit('visibilitychange');
  page.emit('pagehide');
  page.emit('beforeunload');
  assert.equal(flushes, 3);

  dispose();
  dispose();
  assert.equal(flushes, 4, 'dispose performs one final unmount flush');
  assert.deepEqual(page.removed, ['pagehide', 'beforeunload']);
  assert.deepEqual(visibility.removed, ['visibilitychange']);
  assert.equal(page.listenerCount('pagehide'), 0);
  assert.equal(page.listenerCount('beforeunload'), 0);
  assert.equal(visibility.listenerCount('visibilitychange'), 0);
}

function testReinstallAfterTeardownHasNoDuplicateListener() {
  const page = createTarget();
  const visibility = createTarget({ visibilityState: 'hidden' });
  let flushes = 0;

  const disposeFirst = installPageLifecycleFlush(page, visibility, () => { flushes += 1; });
  disposeFirst();
  const disposeSecond = installPageLifecycleFlush(page, visibility, () => { flushes += 1; });

  assert.equal(page.listenerCount('pagehide'), 1);
  assert.equal(page.listenerCount('beforeunload'), 1);
  assert.equal(visibility.listenerCount('visibilitychange'), 1);
  page.emit('pagehide');
  assert.equal(flushes, 2, 'one final flush plus one event flush');
  disposeSecond();
  assert.equal(flushes, 3);
}

testLifecycleTargetsAndFinalFlush();
testReinstallAfterTeardownHasNoDuplicateListener();
console.log('page lifecycle tests passed');
