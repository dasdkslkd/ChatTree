const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
      jsx: ts.JsxEmit.ReactJSX,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
}

require.extensions['.ts'] = loadTypeScript;
require.extensions['.tsx'] = loadTypeScript;

const frontendRoot = path.join(__dirname, '..');
const appModule = path.join(frontendRoot, 'src/App.tsx');
const providerModule = path.join(
  frontendRoot,
  'src/runtime/BoundServerProvider.tsx',
);
const epochModule = path.join(frontendRoot, 'src/runtime/connectionEpoch.ts');
const identityModule = path.join(
  frontendRoot,
  'src/runtime/connectionIdentity.ts',
);

const BOOTSTRAP = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
});
const CONTEXT_A = Object.freeze({
  ...BOOTSTRAP,
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 4,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

function sameDependencies(before, after) {
  return before !== undefined
    && after !== undefined
    && before.length === after.length
    && before.every((value, index) => Object.is(value, after[index]));
}

function createHookRuntime() {
  const slots = [];
  let cursor = 0;
  let pendingEffects = [];

  const react = {
    lazy() {
      return function LazyServerSessionApp() { return null; };
    },
    Suspense({ children }) {
      return children;
    },
    useRef(initialValue) {
      const index = cursor;
      cursor += 1;
      if (!slots[index]) slots[index] = { kind: 'ref', current: initialValue };
      return slots[index];
    },
    useCallback(callback, dependencies) {
      const index = cursor;
      cursor += 1;
      const previous = slots[index];
      if (!previous || !sameDependencies(previous.dependencies, dependencies)) {
        slots[index] = {
          kind: 'callback',
          dependencies,
          value: callback,
        };
      }
      return slots[index].value;
    },
    useEffect(setup, dependencies) {
      const index = cursor;
      cursor += 1;
      const previous = slots[index];
      if (!previous || !sameDependencies(previous.dependencies, dependencies)) {
        pendingEffects.push({ index, setup, dependencies, previous });
      }
    },
  };

  const jsxRuntime = {
    Fragment: Symbol('Fragment'),
    jsx(type, props) {
      return { type, props };
    },
    jsxs(type, props) {
      return this.jsx(type, props);
    },
  };

  return {
    react,
    jsxRuntime,
    render(Component, props) {
      cursor = 0;
      pendingEffects = [];
      return Component(props);
    },
    flushEffects() {
      const effects = pendingEffects;
      pendingEffects = [];
      for (const effect of effects) {
        effect.previous?.cleanup?.();
        slots[effect.index] = {
          kind: 'effect',
          dependencies: effect.dependencies,
          cleanup: effect.setup(),
        };
      }
    },
    unmount() {
      for (const slot of slots) {
        if (slot?.kind === 'effect') slot.cleanup?.();
      }
      slots.length = 0;
      pendingEffects = [];
    },
  };
}

function clearModule(modulePath) {
  try {
    delete require.cache[require.resolve(modulePath)];
  } catch {
    // The module has not been loaded yet.
  }
}

function loadHarness(onReload) {
  for (const modulePath of [appModule, epochModule, identityModule]) {
    clearModule(modulePath);
  }

  const hookRuntime = createHookRuntime();
  function FakeBoundServerProvider() { return null; }
  const providerStub = {
    BoundServerProvider: FakeBoundServerProvider,
    useBoundServer() {
      throw new Error('BoundApp is not rendered by this App hook harness');
    },
  };
  globalThis.window = {
    location: {
      href: 'http://127.0.0.1:4111/s/profile-a',
      reload: onReload,
    },
  };

  const originalLoad = Module._load;
  Module._load = function loadWithStubs(request, parent, isMain) {
    if (request === 'react') return hookRuntime.react;
    if (request === 'react/jsx-runtime') return hookRuntime.jsxRuntime;
    let resolved;
    try {
      resolved = Module._resolveFilename(request, parent, isMain);
    } catch {
      return originalLoad.call(this, request, parent, isMain);
    }
    if (resolved === providerModule) return providerStub;
    return originalLoad.call(this, request, parent, isMain);
  };

  let App;
  try {
    App = require(appModule).default;
  } finally {
    Module._load = originalLoad;
  }

  return {
    App,
    FakeBoundServerProvider,
    epoch: require(epochModule),
    hookRuntime,
  };
}

function instrumentRuntime(runtime) {
  const calls = {
    captures: 0,
    installs: [],
    invalidations: [],
    subscriptions: 0,
    unsubscriptions: 0,
  };
  const original = {
    capture: runtime.capture,
    install: runtime.install,
    invalidate: runtime.invalidate,
    subscribeInvalidation: runtime.subscribeInvalidation,
  };

  runtime.capture = function capture() {
    calls.captures += 1;
    return original.capture.call(this);
  };
  runtime.install = function install(context) {
    calls.installs.push(context);
    return original.install.call(this, context);
  };
  runtime.invalidate = function invalidate(token) {
    const entry = {
      token,
      signalBefore: this.signalFor(token).aborted,
      signalAfter: null,
      result: null,
    };
    calls.invalidations.push(entry);
    entry.result = original.invalidate.call(this, token);
    entry.signalAfter = this.signalFor(token).aborted;
    return entry.result;
  };
  runtime.subscribeInvalidation = function subscribeInvalidation(listener) {
    calls.subscriptions += 1;
    const unsubscribe = original.subscribeInvalidation.call(this, listener);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      calls.unsubscriptions += 1;
      unsubscribe();
    };
  };
  return calls;
}

function renderApp(harness) {
  const element = harness.hookRuntime.render(harness.App, {
    bootstrap: BOOTSTRAP,
  });
  assert.equal(element.type, harness.FakeBoundServerProvider);
  return element.props;
}

function testInitialInstallRerenderAndValidatedChangeReload() {
  let token;
  let reloads = 0;
  const harness = loadHarness(() => {
    reloads += 1;
    assert.equal(token.signal.aborted, true, 'A aborts before browser reload');
  });
  const runtime = harness.epoch.connectionEpochRuntime;
  const calls = instrumentRuntime(runtime);

  const first = renderApp(harness);
  harness.hookRuntime.flushEffects();
  assert.equal(calls.subscriptions, 1);
  assert.throws(() => runtime.capture(), harness.epoch.StaleConnectionEpochError);

  first.onInitialContext(CONTEXT_A);
  const installedToken = runtime.capture();
  token = {
    value: installedToken,
    signal: runtime.signalFor(installedToken),
  };
  assert.equal(runtime.isCurrent(installedToken), true);

  const second = renderApp(harness);
  harness.hookRuntime.flushEffects();
  assert.equal(second.onInitialContext, first.onInitialContext);
  assert.equal(second.reloadCurrentPage, first.reloadCurrentPage);
  assert.equal(calls.subscriptions, 1, 'ordinary rerender keeps one subscription');

  second.reloadCurrentPage();
  assert.equal(reloads, 1);
  assert.equal(runtime.isCurrent(installedToken), false);
  assert.equal(token.signal.aborted, true);
  assert.deepEqual(calls.installs, [CONTEXT_A], 'validated B is never installed');
  assert.equal(calls.invalidations.length, 1);
  assert.equal(calls.invalidations[0].signalBefore, false);
  assert.equal(calls.invalidations[0].signalAfter, true);
  assert.equal(calls.invalidations[0].result, true);

  second.reloadCurrentPage();
  assert.equal(reloads, 1, 'the App reload guard is one-shot');
  assert.equal(calls.invalidations.length, 1);
  harness.hookRuntime.unmount();
  assert.equal(calls.unsubscriptions, 1);
}

function testRuntimeInvalidationUsesSameGuardedReloadCallback() {
  let token;
  let reloads = 0;
  const harness = loadHarness(() => {
    reloads += 1;
    assert.equal(token.signal.aborted, true, 'subscriber observes aborted A');
  });
  const runtime = harness.epoch.connectionEpochRuntime;
  const calls = instrumentRuntime(runtime);
  const props = renderApp(harness);
  harness.hookRuntime.flushEffects();
  props.onInitialContext(CONTEXT_A);
  const installedToken = runtime.capture();
  token = {
    value: installedToken,
    signal: runtime.signalFor(installedToken),
  };

  assert.equal(runtime.invalidate(installedToken), true);
  assert.equal(reloads, 1);
  assert.equal(calls.invalidations.length, 2);
  assert.equal(calls.invalidations[0].result, true);
  assert.equal(calls.invalidations[1].result, false);
  assert.equal(
    calls.invalidations[1].signalBefore,
    true,
    'the synchronous subscriber cannot recurse because its guard is set first',
  );

  props.reloadCurrentPage();
  assert.equal(reloads, 1);
  harness.hookRuntime.unmount();
  assert.equal(calls.unsubscriptions, 1);
}

function testEffectOrderReplayAndUnmountCleanup() {
  let token;
  let reloads = 0;
  const harness = loadHarness(() => {
    reloads += 1;
    assert.equal(token.signal.aborted, true);
  });
  const runtime = harness.epoch.connectionEpochRuntime;
  const calls = instrumentRuntime(runtime);

  const props = renderApp(harness);
  props.onInitialContext(CONTEXT_A);
  const installedToken = runtime.capture();
  token = {
    value: installedToken,
    signal: runtime.signalFor(installedToken),
  };
  assert.equal(runtime.invalidate(installedToken), true);
  assert.equal(reloads, 0, 'the App effect has not subscribed yet');

  harness.hookRuntime.flushEffects();
  assert.equal(calls.subscriptions, 1);
  assert.equal(reloads, 1, 'late subscription immediately replays invalidation');
  assert.equal(calls.invalidations.at(-1).result, false);

  harness.hookRuntime.unmount();
  assert.equal(calls.unsubscriptions, 1);
}

function main() {
  const originalWindow = globalThis.window;
  try {
    testInitialInstallRerenderAndValidatedChangeReload();
    testRuntimeInvalidationUsesSameGuardedReloadCallback();
    testEffectOrderReplayAndUnmountCleanup();
    console.log('app connection runtime tests passed');
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}

main();
