const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const ts = require('typescript');

require.extensions['.tsx'] = function loadTsx(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      jsx: ts.JsxEmit.ReactJSX,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

function createHookRuntime() {
  let cursor = 0;
  const slots = [];
  const effects = [];
  let pendingEffects = [];

  const depsEqual = (left, right) => (
    left !== undefined
    && right !== undefined
    && left.length === right.length
    && left.every((value, index) => Object.is(value, right[index]))
  );

  return {
    react: {
      lazy() { return function LazyComponent() { return null; }; },
      Suspense({ children }) { return children; },
      useEffect(setup, deps) {
        const index = cursor++;
        const previous = effects[index];
        if (!previous || !depsEqual(previous.deps, deps)) {
          pendingEffects.push({ index, setup, deps });
        }
      },
      useMemo(factory, deps) {
        const index = cursor++;
        const previous = slots[index];
        if (!previous || !depsEqual(previous.deps, deps)) {
          slots[index] = { deps, value: factory() };
        }
        return slots[index].value;
      },
      useRef(initialValue) {
        const index = cursor++;
        if (!slots[index]) slots[index] = { current: initialValue };
        return slots[index];
      },
      useState(initialValue) {
        const index = cursor++;
        if (!slots[index]) slots[index] = { value: initialValue };
        return [slots[index].value, value => { slots[index].value = value; }];
      },
    },
    beginRender() {
      cursor = 0;
      pendingEffects = [];
    },
    flushEffects() {
      for (const pending of pendingEffects) {
        effects[pending.index]?.cleanup?.();
        const cleanup = pending.setup();
        effects[pending.index] = { deps: pending.deps, cleanup };
      }
      pendingEffects = [];
    },
    unmount() {
      for (const effect of effects) effect?.cleanup?.();
      effects.length = 0;
    },
  };
}

function containsLazyBusinessTree(value) {
  if (!value || typeof value !== 'object') return false;
  if (typeof value.type === 'function' && value.type.name === 'LazyComponent') return true;
  return Object.values(value).some(containsLazyBusinessTree);
}

const hookRuntime = createHookRuntime();
const ownerInstances = [];
class FakeInitializationOwner {
  constructor(options) {
    this.options = options;
    this.started = 0;
    this.connected = [];
    this.disposed = 0;
    ownerInstances.push(this);
  }
  start() { this.started += 1; }
  setConnected(value) { this.connected.push(value); }
  dispose() { this.disposed += 1; }
}

let perfLoads = 0;
const lifecycleRegistrations = [];
let lifecycleDisposals = 0;
const originalLoad = Module._load;
const originalWindow = globalThis.window;
const originalDocument = globalThis.document;

const genericComponent = () => null;
const modelStoreState = {
  config: {},
  error: null,
  async loadConfig() {},
};
function useModelStore() {
  return {
    currentProvider: null,
    currentModel: null,
    loadMetadata() {},
    getMetadata() { return undefined; },
  };
}
useModelStore.getState = () => modelStoreState;

Module._load = function loadStub(request, parent, isMain) {
  if (request === 'react') return hookRuntime.react;
  if (request === 'react/jsx-runtime') {
    return {
      Fragment: Symbol('Fragment'),
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    };
  }
  if (request === './serverSessionInitialization') {
    return {
      ServerSessionInitializationOwner: FakeInitializationOwner,
      initializeServerSessionStores: async () => {},
    };
  }
  if (request === './pageLifecycle') {
    return {
      installPageLifecycleFlush(pageTarget, visibilityTarget, flush) {
        lifecycleRegistrations.push({ pageTarget, visibilityTarget, flush });
        return () => { lifecycleDisposals += 1; };
      },
    };
  }
  if (request === '../perf/client') {
    return {
      flushPerfEventsSync() {},
      loadPerfConfig() { perfLoads += 1; return Promise.resolve({}); },
    };
  }
  if (request === '../store/navigationStore') {
    return { useNavigationStore: () => ({ activePage: 'chat', settingsSection: 'providers', openSettings() {} }) };
  }
  if (request === '../store/modelStore') return { useModelStore };
  if (request === '../store/conversationStore') return { useConversationStore: () => ({ messages: [] }) };
  if (request === '../components/SettingsDialog') return { SettingsPageView: genericComponent };
  if (request === 'lucide-react') return { Wifi: genericComponent, WifiOff: genericComponent };
  if (request.startsWith('@/components/')) {
    return new Proxy({}, { get: () => genericComponent });
  }
  if (request.endsWith('.css')) return {};
  return originalLoad.call(this, request, parent, isMain);
};

async function main() {
  try {
    const source = fs.readFileSync(
      require.resolve('../src/runtime/ServerSessionApp.tsx'),
      'utf8',
    );
    assert.doesNotMatch(
      source,
      /\{connected && \(/,
      'a recoverable disconnect must not gate the installed business subtree',
    );
    globalThis.window = { name: 'page-target' };
    globalThis.document = { name: 'visibility-target' };
    const { default: ServerSessionApp } = require('../src/runtime/ServerSessionApp.tsx');
    const binding = Object.freeze({
      profileId: 'profile-a',
      apiBase: '/p/profile-a/api/v1',
      serverInstanceId: '11111111-1111-4111-8111-111111111111',
      connectionEpoch: 1,
      connectionLeaseId: '22222222-2222-4222-8222-222222222222',
    });

    const businessTreePresent = [];
    for (const connected of [true, false, true]) {
      hookRuntime.beginRender();
      const rendered = ServerSessionApp({ binding, connected });
      businessTreePresent.push(containsLazyBusinessTree(rendered));
      hookRuntime.flushEffects();
    }

    assert.equal(ownerInstances.length, 1, 'one owner is constructed for the mounted realm');
    assert.equal(ownerInstances[0].started, 1);
    assert.deepEqual(ownerInstances[0].connected, [true, false, true]);
    assert.deepEqual(
      businessTreePresent,
      [true, true, true],
      'the bound business subtree remains installed through a recoverable disconnect',
    );
    assert.equal(perfLoads, 1);
    assert.equal(lifecycleRegistrations.length, 1);
    assert.equal(lifecycleRegistrations[0].pageTarget, globalThis.window);
    assert.equal(lifecycleRegistrations[0].visibilityTarget, globalThis.document);

    hookRuntime.unmount();
    assert.equal(ownerInstances[0].disposed, 1);
    assert.equal(lifecycleDisposals, 1);
    console.log('server session app tests passed');
  } finally {
    Module._load = originalLoad;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
