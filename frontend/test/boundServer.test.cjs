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
const boundServerModule = path.join(frontendRoot, 'src/runtime/boundServer.ts');
const clientModule = path.join(frontendRoot, 'src/api/client.ts');
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
    boundServerModule,
    clientModule,
    serverModule,
    launcherModule,
  ]) {
    clearModule(modulePath);
  }
  require(bootstrapModule).initializeFrontendBootstrap();
  return {
    identity: require(identityModule),
    boundServer: require(boundServerModule),
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
    boundServerModule,
    clientModule,
    serverModule,
  ]) {
    clearModule(modulePath);
  }
  assert.doesNotThrow(
    () => require(boundServerModule),
    'boundServer.ts must not initialize the bootstrap-bound API singleton',
  );
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
    readyStatus({ profile_id: 'profile-b' }),
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
    readyStatus({ profile_id: 'profile-b' }),
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

async function testLegacyServerApiNeverTreatsSignalAsLeaseId() {
  const { server } = loadRuntimeModules();
  const controller = new AbortController();
  const singletonClient = require(clientModule).apiClient;
  const originalGet = singletonClient.get;
  const calls = [];
  singletonClient.get = async (url, config) => {
    calls.push({ url, config });
    return {
      data: url === '/health' ? health() : handshake(),
      headers: {},
    };
  };
  try {
    await server.serverApi.health(controller.signal);
    await server.serverApi.handshake(controller.signal);
    assert.equal(calls.length, 2);
    for (const call of calls) {
      assert.equal(call.config.signal, controller.signal);
      assert.equal(call.config.headers, undefined);
    }
  } finally {
    singletonClient.get = originalGet;
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
    await testProbeChecksResponseLeaseBeforeSecondStatus();
    await testLeaseRaceWinsBeforeProtocolOrIdentityFatal();
    await testStableProbeClassifiesProtocolAndIdentityAsFatal();
    await testServerApiGuardsLeaseHeaderAndKeepsSignalPosition();
    await testLegacyServerApiNeverTreatsSignalAsLeaseId();
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
