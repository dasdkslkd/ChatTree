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

globalThis.window = {
  location: {
    href: 'http://127.0.0.1:18100/s/profile-7',
    pathname: '/s/profile-7',
  },
};

require('../src/runtime/profileContext.ts').initializeProfileContext();
const {
  createApiClient,
  profileContext,
  serverApiUrl,
} = require('../src/api/client.ts');
const {
  CONNECTION_LEASE_HEADER,
} = require('../src/api/connectionLeaseHeader.ts');
const {
  ChatTreeApiError,
} = require('../src/api/errors.ts');
const {
  StaleConnectionScopeError,
} = require('../src/runtime/connectionScope.ts');

const LEASE_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

function createRuntime() {
  const controller = new AbortController();
  const scope = Object.freeze({
    profileId: 'profile-7',
    serverInstanceId: '11111111-1111-4111-8111-111111111111',
    leaseId: LEASE_ID,
    signal: controller.signal,
  });
  let active = true;
  let invalidations = 0;
  return {
    scope,
    runtime: {
      current() {
        if (!active) throw new StaleConnectionScopeError();
        return scope;
      },
      isActive(candidate) {
        return active && candidate === scope;
      },
      invalidate(candidate) {
        if (!active || candidate !== scope) return false;
        active = false;
        invalidations += 1;
        controller.abort();
        return true;
      },
    },
    invalidate() {
      active = false;
      controller.abort();
    },
    get invalidations() {
      return invalidations;
    },
  };
}

function responseAdapter(handler) {
  return async (config) => {
    const result = await handler(config);
    return {
      data: result.data ?? {},
      status: result.status ?? 200,
      statusText: result.statusText ?? 'OK',
      headers: result.headers ?? { [CONNECTION_LEASE_HEADER]: LEASE_ID },
      config,
      request: {},
    };
  };
}

function testProfileRouteDefinesServerApiBase() {
  assert.deepEqual(profileContext, {
    profileId: 'profile-7',
    apiBase: '/p/profile-7/api/v1',
  });
  assert.equal(serverApiUrl('/runs/1'), '/p/profile-7/api/v1/runs/1');
  assert.equal(serverApiUrl('runs/1'), '/p/profile-7/api/v1/runs/1');
}

async function testRequestOwnsScopeAndInjectsLeaseAtTransportBoundary() {
  const state = createRuntime();
  const client = createApiClient('/p/profile-7/api/v1', state.runtime);
  const caller = new AbortController();
  let seen;
  client.defaults.adapter = responseAdapter(async (config) => {
    seen = config;
    return { data: { ok: true } };
  });

  const response = await client.get('/handshake', {
    headers: { [CONNECTION_LEASE_HEADER]: 'spoofed' },
    signal: caller.signal,
  });

  assert.deepEqual(response.data, { ok: true });
  assert.equal(seen.baseURL, '/p/profile-7/api/v1');
  assert.equal(seen.headers.get(CONNECTION_LEASE_HEADER), LEASE_ID);
  assert.notEqual(seen.signal, caller.signal);
  assert.notEqual(seen.signal, state.scope.signal);
  assert.equal(seen.signal.aborted, false);
}

async function testMismatchedResponseLeaseInvalidatesWholePageScope() {
  const state = createRuntime();
  const client = createApiClient('/p/profile-7/api/v1', state.runtime);
  client.defaults.adapter = responseAdapter(async () => ({
    headers: {
      [CONNECTION_LEASE_HEADER]: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    },
  }));

  await assert.rejects(client.get('/models'), StaleConnectionScopeError);
  assert.equal(state.invalidations, 1);
  assert.equal(state.scope.signal.aborted, true);
}

async function testResponseCannotCommitAfterScopeBecomesStale() {
  const state = createRuntime();
  const client = createApiClient('/p/profile-7/api/v1', state.runtime);
  let release;
  const blocked = new Promise((resolve) => {
    release = resolve;
  });
  client.defaults.adapter = responseAdapter(async () => {
    await blocked;
    return { data: { stale: true } };
  });

  const pending = client.get('/models');
  await Promise.resolve();
  state.invalidate();
  release();
  await assert.rejects(pending, StaleConnectionScopeError);
}

async function testOnlyModernErrorEnvelopeIsAccepted() {
  const modernState = createRuntime();
  const modern = createApiClient('/p/profile-7/api/v1', modernState.runtime);
  modern.defaults.adapter = responseAdapter(async () => ({
    status: 409,
    statusText: 'Conflict',
    data: {
      error: {
        code: 'idempotency_conflict',
        message: 'Request key was reused',
        retryable: false,
        request_id: 'request-tree',
        details: { run_id: 'run-1' },
      },
    },
  }));

  await assert.rejects(
    modern.post('/runs', {}),
    (error) => (
      error instanceof ChatTreeApiError
      && error.code === 'idempotency_conflict'
      && error.requestId === 'request-tree'
      && error.details.run_id === 'run-1'
    ),
  );

  const legacyState = createRuntime();
  const legacy = createApiClient('/p/profile-7/api/v1', legacyState.runtime);
  legacy.defaults.adapter = responseAdapter(async () => ({
    status: 400,
    statusText: 'Bad Request',
    data: { detail: 'legacy error' },
  }));
  await assert.rejects(
    legacy.get('/legacy'),
    (error) => error instanceof ChatTreeApiError
      && error.code === 'unexpected_response',
  );
}

async function testLauncherClientCanOptOutOfServerLease() {
  const client = createApiClient('/client/v1', null);
  let seen;
  client.defaults.adapter = responseAdapter(async (config) => {
    seen = config;
    return { data: { status: 'ready' }, headers: {} };
  });

  await client.get('/profiles/local/status');
  assert.equal(seen.headers.has(CONNECTION_LEASE_HEADER), false);
}

(async () => {
  testProfileRouteDefinesServerApiBase();
  await testRequestOwnsScopeAndInjectsLeaseAtTransportBoundary();
  await testMismatchedResponseLeaseInvalidatesWholePageScope();
  await testResponseCannotCommitAfterScopeBecomesStale();
  await testOnlyModernErrorEnvelopeIsAccepted();
  await testLauncherClientCanOptOutOfServerLease();
  console.log('apiClient tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
