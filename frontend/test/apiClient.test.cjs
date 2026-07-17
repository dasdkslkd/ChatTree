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

const clientModule = path.join(__dirname, '../src/api/client.ts');
const bootstrapModule = path.join(__dirname, '../src/runtime/frontendBootstrap.ts');
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const errorsModule = path.join(__dirname, '../src/api/errors.ts');
const serverModule = path.join(__dirname, '../src/api/server.ts');
const originalWindow = globalThis.window;
const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const LEASE_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const LEASE_HEADER = 'X-ChatTree-Connection-Lease-ID';
const CONTEXT_A = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 4,
  connectionLeaseId: LEASE_A,
});

function setWindow(value) {
  if (value === undefined) {
    delete globalThis.window;
  } else {
    globalThis.window = value;
  }
}

function loadClient(bootstrap, pathname = '/s/profile-7') {
  setWindow({
    __CHATTREE_BOOTSTRAP__: bootstrap,
    location: { href: `http://127.0.0.1:5173${pathname}`, pathname },
  });
  delete require.cache[require.resolve(bootstrapModule)];
  delete require.cache[require.resolve(epochModule)];
  delete require.cache[require.resolve(clientModule)];
  delete require.cache[require.resolve(serverModule)];
  require(bootstrapModule).initializeFrontendBootstrap();
  return require(clientModule);
}

function loadUninitializedClient(pathname = '/s/profile-7') {
  setWindow({
    location: {
      href: `http://127.0.0.1:5173${pathname}`,
      pathname,
    },
  });
  delete require.cache[require.resolve(bootstrapModule)];
  delete require.cache[require.resolve(epochModule)];
  delete require.cache[require.resolve(clientModule)];
  delete require.cache[require.resolve(serverModule)];
  return () => require(clientModule);
}

function testUninitializedClientFailsClosed() {
  assert.throws(loadUninitializedClient(), /Frontend bootstrap has not been initialized/);
}

function testInjectedRelativeBaseAndProfile() {
  const { apiClient, frontendBootstrap, serverApiUrl } = loadClient({
    profileId: 'profile-7',
    apiBase: '/p/profile-7/api/v1',
  });

  assert.equal(frontendBootstrap.apiBase, '/p/profile-7/api/v1');
  assert.equal(frontendBootstrap.profileId, 'profile-7');
  assert.equal(apiClient.defaults.baseURL, '/p/profile-7/api/v1');
  assert.equal(serverApiUrl('/runs'), '/p/profile-7/api/v1/runs');
  assert.equal(serverApiUrl('runs'), '/p/profile-7/api/v1/runs');
}

function testInjectedAbsoluteBase() {
  const { frontendBootstrap, serverApiUrl } = loadClient({
    profileId: 'profile-7',
    apiBase: 'https://launcher.example/p/profile-7/api/v1',
  });

  assert.equal(frontendBootstrap.apiBase, 'https://launcher.example/p/profile-7/api/v1');
  assert.equal(serverApiUrl('/handshake'), 'https://launcher.example/p/profile-7/api/v1/handshake');
}

function createRuntime() {
  const { ConnectionEpochRuntime } = require(epochModule);
  const runtime = new ConnectionEpochRuntime();
  runtime.install(CONTEXT_A);
  return runtime;
}

function successResponse(config, overrides = {}) {
  return {
    data: { ok: true },
    status: 200,
    statusText: 'OK',
    headers: { [LEASE_HEADER]: LEASE_A },
    config,
    ...overrides,
  };
}

function rejectedResponse(config, status, data, headers) {
  const error = new Error(`request failed: ${status}`);
  error.isAxiosError = true;
  error.config = config;
  error.response = {
    data,
    status,
    statusText: 'ERROR',
    headers,
    config,
  };
  return Promise.reject(error);
}

async function testApiClientNormalizesRejectedResponses() {
  const { createApiClient } = loadClient(undefined);
  const apiClient = createApiClient('/normalizer-test', null);
  const { ChatTreeApiError } = require(errorsModule);
  const source = new Error('raw axios error');
  source.isAxiosError = true;
  source.response = {
    status: 409,
    headers: {},
    data: {
      error: {
        code: 'active_runs_present',
        message: 'blocked',
        retryable: true,
        request_id: 'req_interceptor',
      },
    },
  };

  await assert.rejects(
    () => apiClient.get('/interceptor-test', {
      adapter: () => Promise.reject(source),
    }),
    (error) => {
      assert.ok(error instanceof ChatTreeApiError);
      assert.equal(error.code, 'active_runs_present');
      assert.equal(error.requestId, 'req_interceptor');
      return true;
    },
  );
}

async function testApiClientFactoriesKeepIndependentFixedBasesAndNormalizers() {
  const { createApiClient } = loadClient(undefined);
  const { ChatTreeApiError } = require(errorsModule);
  const first = createApiClient('/p/profile-a/api/v1', null);
  const second = createApiClient('/p/profile-b/api/v1', null);

  assert.equal(first.defaults.baseURL, '/p/profile-a/api/v1');
  assert.equal(second.defaults.baseURL, '/p/profile-b/api/v1');
  assert.notEqual(first, second);

  for (const [client, requestId] of [
    [first, 'req_factory_a'],
    [second, 'req_factory_b'],
  ]) {
    const source = new Error('raw axios error');
    source.isAxiosError = true;
    source.response = {
      status: 503,
      headers: {},
      data: {
        error: {
          code: 'service_unavailable',
          message: 'temporarily unavailable',
          retryable: true,
          request_id: requestId,
        },
      },
    };
    await assert.rejects(
      () => client.get('/interceptor-test', {
        adapter: () => Promise.reject(source),
      }),
      (error) => {
        assert.ok(error instanceof ChatTreeApiError);
        assert.equal(error.requestId, requestId);
        return true;
      },
    );
  }
}

async function testBusinessRequestCapturesLeaseAndComposesAbortSignals() {
  const { createApiClient } = loadClient(undefined);
  const runtime = createRuntime();
  const client = createApiClient(CONTEXT_A.apiBase, runtime);
  const caller = new AbortController();
  let capturedConfig;
  let releaseAdapter;
  const adapterStarted = new Promise(resolve => {
    releaseAdapter = resolve;
  });
  const responseGate = {};
  responseGate.promise = new Promise(resolve => {
    responseGate.resolve = resolve;
  });

  const pending = client.get('/delayed', {
    signal: caller.signal,
    adapter(config) {
      capturedConfig = config;
      releaseAdapter();
      return responseGate.promise;
    },
  });
  await adapterStarted;
  assert.equal(capturedConfig.headers.get(LEASE_HEADER), LEASE_A);
  assert.notEqual(capturedConfig.signal, caller.signal);
  assert.equal(capturedConfig.signal.aborted, false);
  assert.equal(caller.signal.aborted, false);

  const token = runtime.capture();
  let notifications = 0;
  runtime.subscribeInvalidation(() => { notifications += 1; });
  assert.equal(runtime.invalidate(token), true);
  assert.equal(capturedConfig.signal.aborted, true);
  assert.equal(caller.signal.aborted, false);
  assert.equal(notifications, 1);
  responseGate.resolve(successResponse(capturedConfig));
  const { StaleConnectionEpochError } = require(epochModule);
  await assert.rejects(() => pending, StaleConnectionEpochError);
}

async function testCaptureFailureAndCallerCancellationPreserveIdentity() {
  const { createApiClient } = loadClient(undefined);
  const epoch = require(epochModule);
  const staleRuntime = new epoch.ConnectionEpochRuntime();
  let adapterCalls = 0;
  const staleClient = createApiClient('/stale', staleRuntime);
  await assert.rejects(
    () => staleClient.get('/never-sent', {
      adapter() {
        adapterCalls += 1;
        return Promise.reject(new Error('must not run'));
      },
    }),
    epoch.StaleConnectionEpochError,
  );
  assert.equal(adapterCalls, 0);

  const runtime = createRuntime();
  const client = createApiClient('/caller-cancel', runtime);
  const caller = new AbortController();
  const reason = new Error('caller cancelled');
  caller.abort(reason);
  await assert.rejects(
    () => client.get('/cancelled', {
      signal: caller.signal,
      adapter() {
        assert.fail('an already-aborted request must not reach the adapter');
      },
    }),
    error => {
      assert.equal(error instanceof epoch.StaleConnectionEpochError, false);
      assert.equal(error.code, 'ERR_CANCELED');
      assert.equal(runtime.isCurrent(runtime.capture()), true);
      return true;
    },
  );
}

async function testStrictResponseLeaseValidation() {
  const { createApiClient } = loadClient(undefined);
  const { StaleConnectionEpochError } = require(epochModule);
  const invalidHeaders = [
    {},
    { [LEASE_HEADER]: LEASE_B },
    { [LEASE_HEADER]: `${LEASE_A}, ${LEASE_A}` },
    { [LEASE_HEADER]: [LEASE_A, LEASE_A] },
    {
      'x-chattree-connection-lease-id': LEASE_A,
      'X-ChatTree-Connection-Lease-ID': LEASE_B,
    },
  ];

  for (const headers of invalidHeaders) {
    for (const status of [200, 404, 500]) {
      const runtime = createRuntime();
      const token = runtime.capture();
      const signal = runtime.signalFor(token);
      let notifications = 0;
      runtime.subscribeInvalidation(() => { notifications += 1; });
      const client = createApiClient('/strict', runtime);
      await assert.rejects(
        () => client.get('/response', {
          adapter: config => status === 200
            ? Promise.resolve(successResponse(config, { headers }))
            : rejectedResponse(config, status, {
              error: {
                code: `ordinary_${status}`,
                message: `ordinary ${status}`,
                retryable: false,
                request_id: `req_ordinary_${status}`,
              },
            }, headers),
        }),
        StaleConnectionEpochError,
      );
      assert.equal(runtime.isCurrent(token), false);
      assert.equal(signal.aborted, true);
      assert.equal(notifications, 1);
    }
  }

  const runtime = createRuntime();
  const client = createApiClient('/strict', runtime);
  const response = await client.get('/valid', {
    headers: {
      'x-chattree-connection-lease-id': LEASE_B,
      'X-ChatTree-Connection-Lease-ID': LEASE_B,
    },
    adapter(config) {
      assert.equal(config.headers.get(LEASE_HEADER), LEASE_A);
      return Promise.resolve(successResponse(config));
    },
  });
  assert.deepEqual(response.data, { ok: true });
}

async function testStaleEnvelopePrecedesOrdinaryNormalization() {
  const { createApiClient } = loadClient(undefined);
  const epoch = require(epochModule);
  const { ChatTreeApiError } = require(errorsModule);
  const envelope = code => ({
    error: {
      code,
      message: code,
      retryable: code === 'stale_connection_epoch',
      request_id: `req_${code}`,
    },
  });

  const staleRuntime = createRuntime();
  const staleToken = staleRuntime.capture();
  const staleClient = createApiClient('/stale-envelope', staleRuntime);
  await assert.rejects(
    () => staleClient.get('/conflict', {
      adapter: config => rejectedResponse(
        config,
        409,
        envelope('stale_connection_epoch'),
        { [LEASE_HEADER]: LEASE_A },
      ),
    }),
    epoch.StaleConnectionEpochError,
  );
  assert.equal(staleRuntime.isCurrent(staleToken), false);

  for (const status of [404, 409, 500]) {
    const runtime = createRuntime();
    const client = createApiClient('/ordinary-error', runtime);
    const code = status === 409 ? 'active_runs_present' : `ordinary_${status}`;
    await assert.rejects(
      () => client.get('/ordinary', {
        adapter: config => rejectedResponse(
          config,
          status,
          envelope(code),
          { [LEASE_HEADER]: LEASE_A },
        ),
      }),
      error => {
        assert.ok(error instanceof ChatTreeApiError);
        assert.equal(error.code, code);
        assert.equal(runtime.isCurrent(runtime.capture()), true);
        return true;
      },
    );
  }

  const fulfilledRuntime = createRuntime();
  const fulfilledClient = createApiClient('/fulfilled-conflict', fulfilledRuntime);
  await assert.rejects(
    () => fulfilledClient.get('/ordinary', {
      adapter: config => Promise.resolve(successResponse(config, {
        status: 409,
        statusText: 'Conflict',
        data: envelope('active_runs_present'),
      })),
    }),
    error => error instanceof ChatTreeApiError
      && error.code === 'active_runs_present',
  );
}

async function main() {
  try {
    testUninitializedClientFailsClosed();
    testInjectedRelativeBaseAndProfile();
    testInjectedAbsoluteBase();
    await testApiClientNormalizesRejectedResponses();
    await testApiClientFactoriesKeepIndependentFixedBasesAndNormalizers();
    await testBusinessRequestCapturesLeaseAndComposesAbortSignals();
    await testCaptureFailureAndCallerCancellationPreserveIdentity();
    await testStrictResponseLeaseValidation();
    await testStaleEnvelopePrecedesOrdinaryNormalization();
    console.log('api client tests passed');
  } finally {
    setWindow(originalWindow);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
