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
const errorsModule = path.join(__dirname, '../src/api/errors.ts');
const serverModule = path.join(__dirname, '../src/api/server.ts');
const originalWindow = globalThis.window;

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

async function testServerProtocolContract() {
  const client = loadClient(undefined);

  const calls = [];
  let protocolVersion = 1;
  const originalGet = client.apiClient.get;
  client.apiClient.get = async (url, config) => {
    calls.push({ url, config });
    if (url === '/health') {
      return { data: { status: 'ok', server_instance_id: 'server-1', time: 123 } };
    }
    return {
      data: {
        server_instance_id: 'server-1',
        protocol_version: protocolVersion,
        server_version: '1.0.0',
        platform: 'test',
        features: [],
        provider_configured: true,
      },
    };
  };

  try {
    const { serverApi } = require(serverModule);
    const controller = new AbortController();

    const handshake = await serverApi.assertCompatible(controller.signal);
    assert.equal(handshake.protocol_version, 1);
    assert.equal(calls[0].url, '/handshake');
    assert.equal(calls[0].config.signal, controller.signal);
    assert.equal(calls[0].config.timeout, 5000);

    const health = await serverApi.health(controller.signal);
    assert.equal(health.status, 'ok');
    assert.equal(calls[1].url, '/health');
    assert.equal(calls[1].config.signal, controller.signal);
    assert.equal(calls[1].config.timeout, 5000);

    protocolVersion = 2;
    await assert.rejects(
      () => serverApi.assertCompatible(controller.signal),
      /Unsupported ChatTree protocol version: 2/,
    );
  } finally {
    client.apiClient.get = originalGet;
  }
}

async function testApiClientNormalizesRejectedResponses() {
  const { apiClient } = loadClient(undefined);
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

async function main() {
  try {
    testUninitializedClientFailsClosed();
    testInjectedRelativeBaseAndProfile();
    testInjectedAbsoluteBase();
    await testServerProtocolContract();
    await testApiClientNormalizesRejectedResponses();
    console.log('api client tests passed');
  } finally {
    setWindow(originalWindow);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
