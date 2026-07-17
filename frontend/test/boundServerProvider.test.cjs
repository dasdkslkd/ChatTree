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
const providerModule = path.join(frontendRoot, 'src/runtime/BoundServerProvider.tsx');
const clientModule = path.join(frontendRoot, 'src/api/client.ts');
const launcherModule = path.join(frontendRoot, 'src/api/launcher.ts');
const serverModule = path.join(frontendRoot, 'src/api/server.ts');
const boundServerModule = path.join(frontendRoot, 'src/runtime/boundServer.ts');
const probeOwnerModule = path.join(frontendRoot, 'src/runtime/boundServerProbeOwner.ts');

const BOOTSTRAP = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
});
const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const PROBED_CONTEXT = Object.freeze({
  profileId: BOOTSTRAP.profileId,
  apiBase: BOOTSTRAP.apiBase,
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 4,
  connectionLeaseId: LEASE_A,
});
const originalWindow = globalThis.window;

function sameDependencies(before, after) {
  return before.length === after.length
    && before.every((value, index) => Object.is(value, after[index]));
}

function createHookRuntime() {
  const hookSlots = [];
  let hookCursor = 0;
  const pendingEffects = [];

  const react = {
    createContext(defaultValue) {
      const context = { current: defaultValue };
      context.Provider = { context };
      return context;
    },
    useContext(context) {
      return context.current;
    },
    useReducer(reducer, initialArg, initializer) {
      const index = hookCursor;
      hookCursor += 1;
      if (!hookSlots[index]) {
        const slot = {
          state: initializer ? initializer(initialArg) : initialArg,
          dispatch: null,
        };
        slot.dispatch = (event) => {
          slot.state = reducer(slot.state, event);
        };
        hookSlots[index] = slot;
      }
      const slot = hookSlots[index];
      return [slot.state, slot.dispatch];
    },
    useEffect(create, dependencies) {
      const index = hookCursor;
      hookCursor += 1;
      const previous = hookSlots[index];
      if (!previous || !sameDependencies(previous.dependencies, dependencies)) {
        pendingEffects.push({ index, create, dependencies, previous });
      }
    },
  };

  const jsxRuntime = {
    Fragment: Symbol('Fragment'),
    jsx(type, props) {
      if (type && type.context) type.context.current = props.value;
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
      hookCursor = 0;
      return Component(props);
    },
    flushEffects() {
      while (pendingEffects.length > 0) {
        const effect = pendingEffects.shift();
        effect.previous?.cleanup?.();
        hookSlots[effect.index] = {
          dependencies: effect.dependencies,
          cleanup: effect.create(),
        };
      }
    },
    unmount() {
      for (const slot of hookSlots) slot?.cleanup?.();
      hookSlots.length = 0;
      pendingEffects.length = 0;
    },
  };
}

function loadHarness() {
  delete require.cache[providerModule];
  const hookRuntime = createHookRuntime();
  const owners = [];
  const events = [];
  const calls = {
    apiClients: [],
    launcherFactories: [],
    serverFactories: [],
    statuses: [],
    health: [],
    handshakes: [],
    boundProbes: [],
  };

  class FakeBoundServerProbeOwner {
    constructor(options) {
      this.options = options;
      this.id = owners.length + 1;
      owners.push(this);
      events.push(`construct:${this.id}`);
    }

    start() {
      events.push(`start:${this.id}`);
    }

    dispose() {
      events.push(`dispose:${this.id}`);
    }
  }

  const stubs = new Map([
    [clientModule, {
      createApiClient(apiBase, runtime) {
        const client = { apiBase };
        calls.apiClients.push({ apiBase, runtime, client });
        return client;
      },
    }],
    [launcherModule, {
      createLauncherApi(bootstrap, pageHref) {
        calls.launcherFactories.push({ bootstrap, pageHref });
        return {
          async getProfileStatus(profileId, signal) {
            const status = { profile_id: profileId, status: 'ready' };
            calls.statuses.push({ profileId, signal, status });
            return status;
          },
        };
      },
    }],
    [serverModule, {
      createServerApi(client) {
        calls.serverFactories.push({ client });
        return {
          async health(expectedLeaseId, signal) {
            const result = { kind: 'health' };
            calls.health.push({ expectedLeaseId, signal, result });
            return result;
          },
          async handshake(expectedLeaseId, signal) {
            const result = { kind: 'handshake' };
            calls.handshakes.push({ expectedLeaseId, signal, result });
            return result;
          },
        };
      },
    }],
    [boundServerModule, {
      async probeBoundServerContext(dependencies, bootstrap, signal) {
        const status = await dependencies.getStatus(signal);
        const health = await dependencies.getHealth(LEASE_A, signal);
        const handshake = await dependencies.getHandshake(LEASE_A, signal);
        calls.boundProbes.push({
          dependencies,
          bootstrap,
          signal,
          status,
          health,
          handshake,
        });
        return PROBED_CONTEXT;
      },
    }],
    [probeOwnerModule, { BoundServerProbeOwner: FakeBoundServerProbeOwner }],
  ]);

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
    if (stubs.has(resolved)) return stubs.get(resolved);
    return originalLoad.call(this, request, parent, isMain);
  };

  let provider;
  try {
    provider = require(providerModule);
  } finally {
    Module._load = originalLoad;
  }
  return { provider, hookRuntime, owners, events, calls };
}

async function testOwnerOptionsWireEveryProbeDependency() {
  const harness = loadHarness();
  let installed = null;
  let reloads = 0;
  const onInitialContext = (context) => {
    installed = context;
  };
  const reloadCurrentPage = () => {
    reloads += 1;
  };
  const props = {
    bootstrap: BOOTSTRAP,
    onInitialContext,
    reloadCurrentPage,
    children: 'session',
  };
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();

  const owner = harness.owners[0];
  assert.equal(owner.options.onInitialContext, onInitialContext);
  assert.equal(owner.options.reloadCurrentPage, reloadCurrentPage);
  assert.equal(owner.options.scheduler, globalThis.window);
  assert.equal(typeof owner.options.dispatch, 'function');

  const controller = new AbortController();
  const result = await owner.options.probe(controller.signal);
  assert.equal(result, PROBED_CONTEXT);
  assert.deepEqual(harness.calls.apiClients.map((call) => call.apiBase), [
    BOOTSTRAP.apiBase,
  ]);
  assert.equal(harness.calls.apiClients[0].runtime, null);
  assert.deepEqual(harness.calls.launcherFactories, [{
    bootstrap: BOOTSTRAP,
    pageHref: globalThis.window.location.href,
  }]);
  assert.equal(
    harness.calls.serverFactories[0].client,
    harness.calls.apiClients[0].client,
  );
  assert.equal(harness.calls.boundProbes[0].bootstrap, BOOTSTRAP);
  assert.equal(harness.calls.boundProbes[0].signal, controller.signal);
  assert.equal(harness.calls.statuses[0].profileId, BOOTSTRAP.profileId);
  assert.equal(harness.calls.statuses[0].signal, controller.signal);
  assert.equal(harness.calls.health[0].expectedLeaseId, LEASE_A);
  assert.equal(harness.calls.health[0].signal, controller.signal);
  assert.equal(harness.calls.handshakes[0].expectedLeaseId, LEASE_A);
  assert.equal(harness.calls.handshakes[0].signal, controller.signal);
  assert.equal(
    harness.calls.boundProbes[0].status,
    harness.calls.statuses[0].status,
  );
  assert.equal(
    harness.calls.boundProbes[0].health,
    harness.calls.health[0].result,
  );
  assert.equal(
    harness.calls.boundProbes[0].handshake,
    harness.calls.handshakes[0].result,
  );

  owner.options.onInitialContext(PROBED_CONTEXT);
  owner.options.reloadCurrentPage();
  assert.equal(installed, PROBED_CONTEXT);
  assert.equal(reloads, 1);

  const probeError = new Error('offline');
  owner.options.dispatch({ type: 'probe_failed', error: probeError });
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  assert.equal(harness.provider.useBoundServer().error, probeError);
  assert.equal(harness.owners.length, 1);
  harness.hookRuntime.unmount();
}

function testStableExplicitCallbacksKeepOneOwner() {
  const harness = loadHarness();
  const onInitialContext = () => {};
  const reloadCurrentPage = () => {};
  const props = {
    bootstrap: BOOTSTRAP,
    onInitialContext,
    reloadCurrentPage,
    children: 'session',
  };

  assert.throws(() => harness.provider.useBoundServer(), /BoundServerProvider/);
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  assert.equal(harness.provider.useBoundServer().status, 'connecting');
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  assert.equal(harness.owners.length, 1);
  assert.deepEqual(harness.events, ['construct:1', 'start:1']);
  harness.hookRuntime.unmount();
  assert.deepEqual(harness.events, ['construct:1', 'start:1', 'dispose:1']);
}

function testStableModuleDefaultKeepsOneOwner() {
  const harness = loadHarness();
  const props = { bootstrap: BOOTSTRAP, children: 'session' };
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  assert.equal(harness.owners.length, 1);
  assert.deepEqual(harness.events, ['construct:1', 'start:1']);
  harness.hookRuntime.unmount();
}

function testDependencyChangeDisposesBeforeReplacementStarts() {
  const harness = loadHarness();
  const props = { bootstrap: BOOTSTRAP, children: 'session' };
  harness.hookRuntime.render(harness.provider.BoundServerProvider, props);
  harness.hookRuntime.flushEffects();
  harness.hookRuntime.render(harness.provider.BoundServerProvider, {
    ...props,
    bootstrap: Object.freeze({ ...BOOTSTRAP }),
  });
  harness.hookRuntime.flushEffects();
  assert.equal(harness.owners.length, 2);
  assert.equal(
    harness.owners[0].options.dispatch,
    harness.owners[1].options.dispatch,
  );
  assert.deepEqual(harness.events, [
    'construct:1',
    'start:1',
    'dispose:1',
    'construct:2',
    'start:2',
  ]);
  harness.hookRuntime.unmount();
}

async function main() {
  globalThis.window = {
    location: {
      href: 'http://127.0.0.1:4111/s/profile-a',
      reload() {},
    },
    setTimeout,
    clearTimeout,
  };
  try {
    await testOwnerOptionsWireEveryProbeDependency();
    testStableExplicitCallbacksKeepOneOwner();
    testStableModuleDefaultKeepsOneOwner();
    testDependencyChangeDisposesBeforeReplacementStarts();
    console.log('bound server provider tests passed');
  } finally {
    if (originalWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = originalWindow;
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
