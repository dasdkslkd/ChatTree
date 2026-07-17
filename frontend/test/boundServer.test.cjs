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

const frontendRoot = path.join(__dirname, '..');
const bootstrapModule = path.join(frontendRoot, 'src/runtime/frontendBootstrap.ts');
const identityModule = path.join(frontendRoot, 'src/runtime/connectionIdentity.ts');
const epochModule = path.join(frontendRoot, 'src/runtime/connectionEpoch.ts');
const boundServerModule = path.join(frontendRoot, 'src/runtime/boundServer.ts');
const bindingStateModule = path.join(frontendRoot, 'src/runtime/bindingState.ts');
const probeOwnerModule = path.join(frontendRoot, 'src/runtime/boundServerProbeOwner.ts');
const clientModule = path.join(frontendRoot, 'src/api/client.ts');
const connectionLeaseHeaderModule = path.join(
  frontendRoot,
  'src/api/connectionLeaseHeader.ts',
);
const serverModule = path.join(frontendRoot, 'src/api/server.ts');
const launcherModule = path.join(frontendRoot, 'src/api/launcher.ts');

const SERVER_A = '11111111-1111-4111-8111-111111111111';
const SERVER_B = '22222222-2222-4222-8222-222222222222';
const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const LEASE_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const BOOTSTRAP = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
});
const originalWindow = globalThis.window;

function clearModule(modulePath) {
  try {
    delete require.cache[require.resolve(modulePath)];
  } catch {
    // The module has not been loaded yet.
  }
}

function loadRuntimeModules() {
  globalThis.window = {
    __CHATTREE_BOOTSTRAP__: BOOTSTRAP,
    location: {
      href: 'http://127.0.0.1:5173/s/profile-a',
      pathname: '/s/profile-a',
    },
  };
  for (const modulePath of [
    bootstrapModule,
    identityModule,
    epochModule,
    boundServerModule,
    bindingStateModule,
    probeOwnerModule,
    clientModule,
    connectionLeaseHeaderModule,
    serverModule,
    launcherModule,
  ]) {
    clearModule(modulePath);
  }
  require(bootstrapModule).initializeFrontendBootstrap();
  return {
    identity: require(identityModule),
    boundServer: require(boundServerModule),
    binding: require(bindingStateModule),
    probeOwner: require(probeOwnerModule),
    server: require(serverModule),
    launcher: require(launcherModule),
  };
}

function readyStatus(overrides = {}) {
  return {
    profile_id: 'profile-a',
    status: 'ready',
    phase: null,
    connection_epoch: 4,
    connection_lease_id: LEASE_A,
    server_instance_id: SERVER_A,
    error: null,
    ...overrides,
  };
}

function health(overrides = {}) {
  return {
    status: 'ok',
    server_instance_id: SERVER_A,
    time: 1,
    ...overrides,
  };
}

function handshake(overrides = {}) {
  return {
    server_instance_id: SERVER_A,
    protocol_version: 1,
    server_version: '1.0.0',
    platform: 'test',
    features: ['error_envelope_v1'],
    provider_configured: true,
    ...overrides,
  };
}

function boundContext(overrides = {}) {
  return Object.freeze({
    profileId: 'profile-a',
    apiBase: '/p/profile-a/api/v1',
    serverInstanceId: SERVER_A,
    connectionEpoch: 4,
    connectionLeaseId: LEASE_A,
    ...overrides,
  });
}

function createFakeScheduler() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeout(callback, delay) {
      const id = nextId;
      nextId += 1;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    delays() {
      return [...timers.values()].map((timer) => timer.delay);
    },
    runNext(expectedDelay) {
      const entry = timers.entries().next().value;
      assert.ok(entry, `expected a ${expectedDelay}ms timer`);
      const [id, timer] = entry;
      assert.equal(timer.delay, expectedDelay);
      timers.delete(id);
      timer.callback();
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function settleAsync() {
  return new Promise((resolve) => setImmediate(resolve));
}

function fakeProbeDependencies(options = {}) {
  const before = options.before ?? readyStatus();
  const after = options.after ?? before;
  const statuses = [before, after];
  let statusIndex = 0;
  return {
    getStatus: async (signal) => {
      options.calls?.push({ method: 'status', signal });
      const value = statuses[Math.min(statusIndex, statuses.length - 1)];
      statusIndex += 1;
      return value;
    },
    getHealth: async (expectedLeaseId, signal) => {
      options.calls?.push({ method: 'health', expectedLeaseId, signal });
      return {
        data: options.healthData ?? health(),
        connectionLeaseId: Object.prototype.hasOwnProperty.call(options, 'healthLeaseId')
          ? options.healthLeaseId
          : LEASE_A,
      };
    },
    getHandshake: async (expectedLeaseId, signal) => {
      options.calls?.push({ method: 'handshake', expectedLeaseId, signal });
      return {
        data: options.handshakeData ?? handshake(),
        connectionLeaseId: Object.prototype.hasOwnProperty.call(options, 'handshakeLeaseId')
          ? options.handshakeLeaseId
          : LEASE_A,
      };
    },
    get statusCalls() {
      return statusIndex;
    },
  };
}

function testConnectionIdentityIsApiIndependent() {
  const source = fs.readFileSync(identityModule, 'utf8');
  const sourceFile = ts.createSourceFile(
    identityModule,
    source,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const imports = sourceFile.statements
    .filter(ts.isImportDeclaration)
    .map((statement) => statement.moduleSpecifier.text);
  assert.equal(
    imports.some((specifier) => /(^|\/)api(?:\/|$)/.test(specifier)),
    false,
    `connectionIdentity.ts must not import api modules: ${imports.join(', ')}`,
  );
}

function testBoundServerCanLoadBeforeBootstrapInitialization() {
  delete globalThis.window;
  for (const modulePath of [
    bootstrapModule,
    identityModule,
    epochModule,
    boundServerModule,
    clientModule,
    connectionLeaseHeaderModule,
    serverModule,
  ]) {
    clearModule(modulePath);
  }
  assert.doesNotThrow(
    () => require(boundServerModule),
    'boundServer.ts must not initialize the bootstrap-bound API singleton',
  );
  assert.doesNotThrow(
    () => require(epochModule),
    'connectionEpoch.ts must remain an uninitialized runtime leaf',
  );
  const server = require(serverModule);
  assert.equal(typeof server.createServerApi, 'function');
  assert.equal('serverApi' in server, false, 'api/server.ts exports no business singleton');
  const serverSource = fs.readFileSync(serverModule, 'utf8');
  assert.doesNotMatch(serverSource, /from ['"]\.\/client['"]/);
}

async function testProbeBuildsFrozenContextInRequiredOrder() {
  const { boundServer } = loadRuntimeModules();
  const calls = [];
  const controller = new AbortController();
  const deps = fakeProbeDependencies({ calls });

  const context = await boundServer.probeBoundServerContext(
    deps,
    BOOTSTRAP,
    controller.signal,
  );

  assert.deepEqual(context, {
    profileId: 'profile-a',
    apiBase: '/p/profile-a/api/v1',
    serverInstanceId: SERVER_A,
    connectionEpoch: 4,
    connectionLeaseId: LEASE_A,
  });
  assert.equal(Object.isFrozen(context), true);
  assert.deepEqual(calls.map((call) => call.method), [
    'status',
    'health',
    'handshake',
    'status',
  ]);
  assert.equal(calls[1].expectedLeaseId, LEASE_A);
  assert.equal(calls[2].expectedLeaseId, LEASE_A);
  assert.ok(calls.every((call) => call.signal === controller.signal));
}

async function testProbeStartsHealthAndHandshakeInParallel() {
  const { boundServer } = loadRuntimeModules();
  const started = [];
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let statusCalls = 0;
  const deps = {
    getStatus: async () => {
      statusCalls += 1;
      return readyStatus();
    },
    getHealth: async () => {
      started.push('health');
      await gate;
      return { data: health(), connectionLeaseId: LEASE_A };
    },
    getHandshake: async () => {
      started.push('handshake');
      await gate;
      return { data: handshake(), connectionLeaseId: LEASE_A };
    },
  };

  const pending = boundServer.probeBoundServerContext(deps, BOOTSTRAP);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(started, ['health', 'handshake']);
  assert.equal(statusCalls, 1);
  release();
  await pending;
  assert.equal(statusCalls, 2);
}

async function testProbeClassifiesReadinessAndAllStatusRaces() {
  const { identity, boundServer } = loadRuntimeModules();
  const invalidBefore = [
    readyStatus({ status: 'connecting' }),
    readyStatus({ server_instance_id: null }),
    readyStatus({ connection_epoch: 0 }),
    readyStatus({ connection_epoch: 1.5 }),
    readyStatus({ connection_lease_id: LEASE_A.toUpperCase() }),
  ];
  for (const before of invalidBefore) {
    await assert.rejects(
      () => boundServer.probeBoundServerContext(
        fakeProbeDependencies({ before }),
        BOOTSTRAP,
      ),
      identity.BoundServerNotReadyError,
    );
  }

  const changedAfter = [
    readyStatus({ server_instance_id: SERVER_B }),
    readyStatus({ connection_epoch: 5 }),
    readyStatus({ connection_lease_id: LEASE_B }),
  ];
  for (const after of changedAfter) {
    await assert.rejects(
      () => boundServer.probeBoundServerContext(
        fakeProbeDependencies({ after }),
        BOOTSTRAP,
      ),
      identity.BoundServerLeaseChangedError,
    );
  }
}

async function testProbeClassifiesProfileContradictionsAsFatal() {
  const { identity, boundServer } = loadRuntimeModules();
  for (const options of [
    { before: readyStatus({ profile_id: 'profile-b' }) },
    { after: readyStatus({ profile_id: 'profile-b' }) },
  ]) {
    await assert.rejects(
      () => boundServer.probeBoundServerContext(
        fakeProbeDependencies(options),
        BOOTSTRAP,
      ),
      identity.BoundServerIdentityError,
    );
  }
}

async function testProbeChecksResponseLeaseBeforeSecondStatus() {
  const { identity, boundServer } = loadRuntimeModules();
  for (const override of [
    { healthLeaseId: undefined },
    { healthLeaseId: LEASE_B },
    { handshakeLeaseId: LEASE_B },
  ]) {
    const deps = fakeProbeDependencies(override);
    await assert.rejects(
      () => boundServer.probeBoundServerContext(deps, BOOTSTRAP),
      identity.BoundServerLeaseChangedError,
    );
    assert.equal(deps.statusCalls, 1);
  }
}

async function testLeaseRaceWinsBeforeProtocolOrIdentityFatal() {
  const { identity, boundServer } = loadRuntimeModules();
  const deps = fakeProbeDependencies({
    after: readyStatus({ connection_lease_id: LEASE_B }),
    healthData: health({ server_instance_id: SERVER_B }),
    handshakeData: handshake({
      server_instance_id: SERVER_B,
      protocol_version: 2,
    }),
  });
  await assert.rejects(
    () => boundServer.probeBoundServerContext(deps, BOOTSTRAP),
    identity.BoundServerLeaseChangedError,
  );
}

async function testStableProbeClassifiesProtocolAndIdentityAsFatal() {
  const { identity, boundServer } = loadRuntimeModules();
  await assert.rejects(
    () => boundServer.probeBoundServerContext(
      fakeProbeDependencies({
        handshakeData: handshake({ protocol_version: 2 }),
      }),
      BOOTSTRAP,
    ),
    identity.BoundServerProtocolError,
  );
  for (const overrides of [
    { healthData: health({ server_instance_id: SERVER_B }) },
    { handshakeData: handshake({ server_instance_id: SERVER_B }) },
  ]) {
    await assert.rejects(
      () => boundServer.probeBoundServerContext(
        fakeProbeDependencies(overrides),
        BOOTSTRAP,
      ),
      identity.BoundServerIdentityError,
    );
  }
  assert.equal(
    identity.isFatalBoundServerError(new identity.BoundServerIdentityError('x')),
    true,
  );
  assert.equal(
    identity.isFatalBoundServerError(new identity.BoundServerProtocolError('x')),
    true,
  );
  assert.equal(
    identity.isFatalBoundServerError(new identity.BoundServerLeaseChangedError('x')),
    false,
  );
}

function testSameBoundServerContextComparesTheCompleteBinding() {
  const { identity } = loadRuntimeModules();
  const context = boundContext();
  assert.equal(identity.sameBoundServerContext(context, { ...context }), true);
  for (const changed of [
    { ...context, profileId: 'profile-b' },
    { ...context, apiBase: '/p/profile-b/api/v1' },
    { ...context, serverInstanceId: SERVER_B },
    { ...context, connectionEpoch: 5 },
    { ...context, connectionLeaseId: LEASE_B },
  ]) {
    assert.equal(identity.sameBoundServerContext(context, changed), false);
  }
}

async function testProbeOwnerUsesCappedBackoffAndAbortsFailedAttempts() {
  const { probeOwner } = loadRuntimeModules();
  const scheduler = createFakeScheduler();
  const signals = [];
  const events = [];
  const owner = new probeOwner.BoundServerProbeOwner({
    probe(signal) {
      signals.push(signal);
      return Promise.reject(new Error(`offline-${signals.length}`));
    },
    dispatch(event) {
      events.push(event);
    },
    scheduler,
    reloadCurrentPage() {
      assert.fail('a recoverable failure must not reload the page');
    },
  });

  owner.start();
  assert.equal(signals.length, 1, 'the first probe must start immediately');
  const expectedDelays = [500, 1000, 2000, 5000, 5000];
  for (let index = 0; index < expectedDelays.length; index += 1) {
    await settleAsync();
    assert.equal(signals[index].aborted, true);
    assert.deepEqual(scheduler.delays(), [expectedDelays[index]]);
    if (index < expectedDelays.length - 1) {
      scheduler.runNext(expectedDelays[index]);
      assert.equal(signals.length, index + 2);
    }
  }
  assert.equal(events.length, expectedDelays.length);
  assert.ok(events.every((event) => event.type === 'probe_failed'));
  assert.equal(events.some((event) => event.type === 'probe_started'), false);

  owner.dispose();
  assert.deepEqual(scheduler.delays(), []);
}

async function testProbeOwnerSchedulesHealthAndResetsRetryBackoff() {
  const { probeOwner } = loadRuntimeModules();
  const scheduler = createFakeScheduler();
  const context = boundContext();
  const outcomes = [
    { error: new Error('initially offline') },
    { context },
    { error: new Error('later offline') },
  ];
  const signals = [];
  const order = [];
  const owner = new probeOwner.BoundServerProbeOwner({
    probe(signal) {
      signals.push(signal);
      const outcome = outcomes.shift();
      return outcome.error ? Promise.reject(outcome.error) : Promise.resolve(outcome.context);
    },
    dispatch(event) {
      order.push(event.type);
    },
    onInitialContext(installed) {
      assert.equal(installed, context);
      order.push('installed');
    },
    scheduler,
    reloadCurrentPage() {
      assert.fail('an unchanged binding must not reload the page');
    },
  });

  owner.start();
  await settleAsync();
  assert.deepEqual(scheduler.delays(), [500]);
  scheduler.runNext(500);
  await settleAsync();
  assert.deepEqual(order, ['probe_failed', 'installed', 'probe_ready']);
  assert.deepEqual(scheduler.delays(), [30000]);
  scheduler.runNext(30000);
  await settleAsync();
  assert.deepEqual(scheduler.delays(), [500]);
  assert.ok(signals.every((signal) => signal.aborted));
  owner.dispose();
}

async function testProbeOwnerRecoversEqualContextWithoutReplacingIt() {
  const { binding, probeOwner } = loadRuntimeModules();
  const scheduler = createFakeScheduler();
  const installed = boundContext();
  const outcomes = [installed, { ...installed }];
  let state = binding.createInitialBindingState();
  const installedContexts = [];
  const owner = new probeOwner.BoundServerProbeOwner({
    probe() {
      return Promise.resolve(outcomes.shift());
    },
    dispatch(event) {
      state = binding.reduceBindingState(state, event);
    },
    onInitialContext(context) {
      installedContexts.push(context);
    },
    scheduler,
    reloadCurrentPage() {
      assert.fail('an equal context must recover in place');
    },
  });

  owner.start();
  await settleAsync();
  assert.equal(state.context, installed);
  scheduler.runNext(30000);
  await settleAsync();
  assert.equal(state.status, 'ready');
  assert.equal(state.context, installed);
  assert.deepEqual(installedContexts, [installed]);
  assert.deepEqual(scheduler.delays(), [30000]);
  owner.dispose();
}

async function testProbeOwnerReloadsOnceForEveryNewServerGeneration() {
  const { probeOwner } = loadRuntimeModules();
  const original = boundContext();
  for (const changed of [
    { ...original, serverInstanceId: SERVER_B },
    { ...original, connectionLeaseId: LEASE_B },
    { ...original, connectionEpoch: 5 },
    { ...original, connectionEpoch: 1 },
  ]) {
    const scheduler = createFakeScheduler();
    const outcomes = [original, changed];
    const events = [];
    const installed = [];
    let reloads = 0;
    let probes = 0;
    const owner = new probeOwner.BoundServerProbeOwner({
      probe() {
        probes += 1;
        return Promise.resolve(outcomes.shift());
      },
      dispatch(event) {
        events.push(event);
      },
      onInitialContext(context) {
        installed.push(context);
      },
      scheduler,
      reloadCurrentPage() {
        reloads += 1;
      },
    });

    owner.start();
    await settleAsync();
    scheduler.runNext(30000);
    await settleAsync();
    assert.equal(reloads, 1);
    assert.deepEqual(events.map((event) => event.type), ['probe_ready']);
    assert.deepEqual(installed, [original]);
    assert.deepEqual(scheduler.delays(), []);
    owner.start();
    assert.equal(probes, 2, 'a stopped owner must not restart');
    owner.dispose();
  }
}

async function testProbeOwnerTreatsProfileAndApiBaseChangesAsFatal() {
  const { identity, probeOwner } = loadRuntimeModules();
  const original = boundContext();
  for (const changed of [
    { ...original, profileId: 'profile-b' },
    { ...original, apiBase: '/p/profile-b/api/v1' },
  ]) {
    const scheduler = createFakeScheduler();
    const outcomes = [original, changed];
    const events = [];
    let reloads = 0;
    const owner = new probeOwner.BoundServerProbeOwner({
      probe() {
        return Promise.resolve(outcomes.shift());
      },
      dispatch(event) {
        events.push(event);
      },
      scheduler,
      reloadCurrentPage() {
        reloads += 1;
      },
    });

    owner.start();
    await settleAsync();
    scheduler.runNext(30000);
    await settleAsync();
    assert.deepEqual(events.map((event) => event.type), ['probe_ready', 'fatal_error']);
    assert.ok(events[1].error instanceof identity.BoundServerIdentityError);
    assert.equal(reloads, 0);
    assert.deepEqual(scheduler.delays(), []);
    owner.dispose();
  }
}

async function testProbeOwnerStopsForFatalProbeAndInitialInstallErrors() {
  const { identity, probeOwner } = loadRuntimeModules();
  for (const error of [
    new identity.BoundServerIdentityError('contradictory profile'),
    new identity.BoundServerProtocolError('unsupported protocol'),
  ]) {
    const scheduler = createFakeScheduler();
    const events = [];
    const signals = [];
    let probes = 0;
    const owner = new probeOwner.BoundServerProbeOwner({
      probe(signal) {
        probes += 1;
        signals.push(signal);
        return Promise.reject(error);
      },
      dispatch(event) {
        events.push(event);
      },
      scheduler,
      reloadCurrentPage() {
        assert.fail('a fatal probe error must not reload');
      },
    });
    owner.start();
    await settleAsync();
    assert.deepEqual(events, [{ type: 'fatal_error', error }]);
    assert.equal(signals[0].aborted, true);
    assert.deepEqual(scheduler.delays(), []);
    owner.start();
    assert.equal(probes, 1);
    owner.dispose();
  }

  const installError = new Error('cannot install initial context');
  const scheduler = createFakeScheduler();
  const events = [];
  const owner = new probeOwner.BoundServerProbeOwner({
    probe() {
      return Promise.resolve(boundContext());
    },
    dispatch(event) {
      events.push(event);
    },
    onInitialContext() {
      throw installError;
    },
    scheduler,
    reloadCurrentPage() {
      assert.fail('an install error must not reload');
    },
  });
  owner.start();
  await settleAsync();
  assert.deepEqual(events, [{ type: 'fatal_error', error: installError }]);
  assert.deepEqual(scheduler.delays(), []);
  owner.dispose();
}

async function testProbeOwnerDisposeMakesLateCompletionInert() {
  const { probeOwner } = loadRuntimeModules();
  for (const completion of ['resolve', 'reject']) {
    const scheduler = createFakeScheduler();
    const gate = deferred();
    const events = [];
    const installed = [];
    const signals = [];
    let reloads = 0;
    const owner = new probeOwner.BoundServerProbeOwner({
      probe(signal) {
        signals.push(signal);
        return gate.promise;
      },
      dispatch(event) {
        events.push(event);
      },
      onInitialContext(context) {
        installed.push(context);
      },
      scheduler,
      reloadCurrentPage() {
        reloads += 1;
      },
    });

    owner.start();
    owner.dispose();
    assert.equal(signals[0].aborted, true);
    if (completion === 'resolve') {
      gate.resolve(boundContext());
    } else {
      gate.reject(new Error('late failure'));
    }
    await settleAsync();
    assert.deepEqual(events, []);
    assert.deepEqual(installed, []);
    assert.equal(reloads, 0);
    assert.deepEqual(scheduler.delays(), []);
  }
}

async function testServerApiGuardsLeaseHeaderAndKeepsSignalPosition() {
  const { identity, server } = loadRuntimeModules();
  const controller = new AbortController();
  let responseHeaders = {
    'x-chattree-connection-lease-id': LEASE_A,
  };
  let protocolVersion = 1;
  const calls = [];
  const fakeClient = {
    async get(url, config) {
      calls.push({ url, config });
      return {
        data: url === '/health'
          ? health()
          : handshake({ protocol_version: protocolVersion }),
        headers: responseHeaders,
      };
    },
  };
  const api = server.createServerApi(fakeClient);
  const guarded = await api.health(LEASE_A, controller.signal);
  assert.deepEqual(guarded, {
    data: health(),
    connectionLeaseId: LEASE_A,
  });
  assert.equal(calls[0].config.signal, controller.signal);
  assert.equal(calls[0].config.timeout, 5000);
  assert.deepEqual(calls[0].config.headers, {
    'X-ChatTree-Connection-Lease-ID': LEASE_A,
  });
  const compatible = await api.assertCompatible(LEASE_A, controller.signal);
  assert.equal(compatible.data.protocol_version, 1);
  assert.equal(compatible.connectionLeaseId, LEASE_A);
  protocolVersion = 2;
  await assert.rejects(
    () => api.assertCompatible(LEASE_A, controller.signal),
    /Unsupported ChatTree protocol version: 2/,
  );
  protocolVersion = 1;

  for (const invalidHeaders of [
    {},
    { 'x-chattree-connection-lease-id': [LEASE_A, LEASE_A] },
    { 'x-chattree-connection-lease-id': `${LEASE_A}, ${LEASE_A}` },
    { 'x-chattree-connection-lease-id': `${LEASE_A} ` },
    { 'x-chattree-connection-lease-id': LEASE_B },
    {
      'x-chattree-connection-lease-id': LEASE_A,
      'X-ChatTree-Connection-Lease-ID': LEASE_A,
    },
  ]) {
    responseHeaders = invalidHeaders;
    await assert.rejects(
      () => api.handshake(LEASE_A, controller.signal),
      identity.BoundServerLeaseChangedError,
    );
  }
}

async function testLauncherStatusUsesOriginDerivedFromBootstrapAndPageHref() {
  const { launcher } = loadRuntimeModules();
  const axios = require('axios');
  const originalAdapter = axios.defaults.adapter;
  const captured = [];
  axios.defaults.adapter = async (config) => {
    captured.push(config);
    return {
      data: readyStatus(),
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    };
  };
  const controller = new AbortController();
  try {
    const relative = launcher.createLauncherApi(
      BOOTSTRAP,
      'http://127.0.0.1:4111/s/profile-a',
    );
    await relative.getProfileStatus('profile/a', controller.signal);

    const absolute = launcher.createLauncherApi(
      {
        profileId: 'profile-a',
        apiBase: 'https://launcher.example/p/profile-a/api/v1',
      },
      'http://127.0.0.1:4111/s/profile-a',
    );
    await absolute.getProfileStatus('profile-a');
  } finally {
    axios.defaults.adapter = originalAdapter;
  }

  assert.equal(captured[0].baseURL, 'http://127.0.0.1:4111/client/v1');
  assert.equal(captured[0].url, '/profiles/profile%2Fa/status');
  assert.equal(captured[0].signal, controller.signal);
  assert.equal(captured[0].timeout, 5000);
  assert.equal(captured[1].baseURL, 'https://launcher.example/client/v1');
}

async function main() {
  try {
    testConnectionIdentityIsApiIndependent();
    testBoundServerCanLoadBeforeBootstrapInitialization();
    await testProbeBuildsFrozenContextInRequiredOrder();
    await testProbeStartsHealthAndHandshakeInParallel();
    await testProbeClassifiesReadinessAndAllStatusRaces();
    await testProbeClassifiesProfileContradictionsAsFatal();
    await testProbeChecksResponseLeaseBeforeSecondStatus();
    await testLeaseRaceWinsBeforeProtocolOrIdentityFatal();
    await testStableProbeClassifiesProtocolAndIdentityAsFatal();
    testSameBoundServerContextComparesTheCompleteBinding();
    await testProbeOwnerUsesCappedBackoffAndAbortsFailedAttempts();
    await testProbeOwnerSchedulesHealthAndResetsRetryBackoff();
    await testProbeOwnerRecoversEqualContextWithoutReplacingIt();
    await testProbeOwnerReloadsOnceForEveryNewServerGeneration();
    await testProbeOwnerTreatsProfileAndApiBaseChangesAsFatal();
    await testProbeOwnerStopsForFatalProbeAndInitialInstallErrors();
    await testProbeOwnerDisposeMakesLateCompletionInert();
    await testServerApiGuardsLeaseHeaderAndKeepsSignalPosition();
    await testLauncherStatusUsesOriginDerivedFromBootstrapAndPageHref();
    console.log('bound server tests passed');
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
