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
  }).outputText;
  module._compile(output, filename);
};

const {
  REQUIRED_SERVER_FEATURES,
  probeBoundServerContext,
} = require('../src/runtime/boundServer.ts');
const {
  BoundServerIdentityError,
  BoundServerLeaseChangedError,
  BoundServerNotReadyError,
  BoundServerProtocolError,
  BoundServerStatusError,
  isFatalBoundServerError,
} = require('../src/runtime/connectionIdentity.ts');

const SERVER_ID = '11111111-1111-4111-8111-111111111111';
const LEASE_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const PROFILE = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
});

function readyStatus(overrides = {}) {
  return {
    profile_id: 'local',
    status: 'ready',
    phase: null,
    server_instance_id: SERVER_ID,
    connection_epoch: 3,
    connection_lease_id: LEASE_ID,
    error: null,
    ...overrides,
  };
}

function handshake(overrides = {}) {
  return {
    connectionLeaseId: LEASE_ID,
    data: {
      server_instance_id: SERVER_ID,
      protocol_version: 1,
      server_version: '0.1.0',
      platform: 'windows',
      features: [...REQUIRED_SERVER_FEATURES],
      provider_configured: true,
      ...overrides,
    },
  };
}

async function testProbeUsesExactlyOneStatusAndOneProxiedHandshake() {
  const calls = [];
  const controller = new AbortController();
  const context = await probeBoundServerContext({
    getStatus: async (signal) => {
      calls.push(['status', signal]);
      return readyStatus();
    },
    getHandshake: async (expectedLeaseId, signal) => {
      calls.push(['handshake', expectedLeaseId, signal]);
      return handshake();
    },
  }, PROFILE, controller.signal);

  assert.deepEqual(calls, [
    ['status', controller.signal],
    ['handshake', LEASE_ID, controller.signal],
  ]);
  assert.deepEqual(context, {
    profileId: 'local',
    apiBase: '/p/local/api/v1',
    serverInstanceId: SERVER_ID,
    connectionEpoch: 3,
    connectionLeaseId: LEASE_ID,
  });
  assert.equal(Object.isFrozen(context), true);
}

async function assertProbeRejects(errorType, status, result) {
  await assert.rejects(
    probeBoundServerContext({
      getStatus: async () => status,
      getHandshake: async () => result,
    }, PROFILE),
    errorType,
  );
}

async function testStatusMustBeReadyAndMatchProfile() {
  await assertProbeRejects(
    BoundServerNotReadyError,
    readyStatus({ status: 'connecting' }),
    handshake(),
  );
  await assertProbeRejects(
    BoundServerIdentityError,
    readyStatus({ profile_id: 'other' }),
    handshake(),
  );
  await assertProbeRejects(
    BoundServerNotReadyError,
    readyStatus({ connection_lease_id: 'not-a-uuid' }),
    handshake(),
  );
}

async function testHandshakeMustMatchLeaseIdentityAndProtocol() {
  await assertProbeRejects(
    BoundServerLeaseChangedError,
    readyStatus(),
    { ...handshake(), connectionLeaseId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' },
  );
  await assertProbeRejects(
    BoundServerIdentityError,
    readyStatus(),
    handshake({ server_instance_id: '22222222-2222-4222-8222-222222222222' }),
  );
  await assertProbeRejects(
    BoundServerProtocolError,
    readyStatus(),
    handshake({ protocol_version: 0 }),
  );

  const futureCompatible = await probeBoundServerContext({
    getStatus: async () => readyStatus(),
    getHandshake: async () => handshake({ protocol_version: 2 }),
  }, PROFILE);
  assert.equal(futureCompatible.serverInstanceId, SERVER_ID);
}

async function testLauncherTerminalProtocolErrorFailsWithoutRetryingHandshake() {
  let handshakeCalls = 0;
  const status = readyStatus({
    status: 'error',
    phase: 'handshake',
    server_instance_id: null,
    connection_epoch: 0,
    connection_lease_id: '',
    error: {
      code: 'local_server_protocol_mismatch',
      message: 'ChatTree Server protocol is too old',
      retryable: false,
      details: { minimum_protocol_version: 1, observed_protocol_version: 0 },
    },
  });

  await assert.rejects(
    probeBoundServerContext({
      getStatus: async () => status,
      getHandshake: async () => {
        handshakeCalls += 1;
        return handshake();
      },
    }, PROFILE),
    (error) => (
      error instanceof BoundServerStatusError
      && error.code === 'local_server_protocol_mismatch'
      && error.message === 'ChatTree Server protocol is too old'
      && error.retryable === false
      && error.details.observed_protocol_version === 0
      && isFatalBoundServerError(error)
    ),
  );
  assert.equal(handshakeCalls, 0);
}

async function testOldServerMissingRequiredFeatureFailsBeforeWorkspaceMounts() {
  const features = REQUIRED_SERVER_FEATURES.filter(
    (feature) => feature !== 'error_envelope_v1',
  );
  await assert.rejects(
    probeBoundServerContext({
      getStatus: async () => readyStatus(),
      getHandshake: async () => handshake({ features }),
    }, PROFILE),
    (error) => (
      error instanceof BoundServerProtocolError
      && /error_envelope_v1/.test(error.message)
      && /需要升级/.test(error.message)
    ),
  );
}

(async () => {
  await testProbeUsesExactlyOneStatusAndOneProxiedHandshake();
  await testStatusMustBeReadyAndMatchProfile();
  await testHandshakeMustMatchLeaseIdentityAndProtocol();
  await testLauncherTerminalProtocolErrorFailsWithoutRetryingHandshake();
  await testOldServerMissingRequiredFeatureFailsBeforeWorkspaceMounts();
  console.log('boundServer tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
