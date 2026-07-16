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
const serverModule = path.join(__dirname, '../src/api/server.ts');
const originalWindow = globalThis.window;

function setWindow(value) {
  if (value === undefined) {
    delete globalThis.window;
  } else {
    globalThis.window = value;
  }
}

function loadClient(bootstrap) {
  setWindow(bootstrap === undefined ? undefined : { __CHATTREE_BOOTSTRAP__: bootstrap });
  delete require.cache[require.resolve(clientModule)];
  return require(clientModule);
}

function testDefaultApiBase() {
  const { apiClient, frontendBootstrap, serverApiUrl } = loadClient(undefined);

  assert.equal(frontendBootstrap.apiBase, '/api/v1');
  assert.equal(apiClient.defaults.baseURL, '/api/v1');
  assert.equal(serverApiUrl('/health'), '/api/v1/health');
  assert.equal(serverApiUrl('health'), '/api/v1/health');
}

function testInjectedRelativeBaseAndProfile() {
  const { apiClient, frontendBootstrap, serverApiUrl } = loadClient({
    apiBase: ' custom/api/v2/// ',
    profileId: 'profile-7',
  });

  assert.equal(frontendBootstrap.apiBase, '/custom/api/v2');
  assert.equal(frontendBootstrap.profileId, 'profile-7');
  assert.equal(apiClient.defaults.baseURL, '/custom/api/v2');
  assert.equal(serverApiUrl('/runs'), '/custom/api/v2/runs');
  assert.equal(serverApiUrl('runs'), '/custom/api/v2/runs');
}

function testInjectedAbsoluteBase() {
  const { frontendBootstrap, serverApiUrl } = loadClient({
    apiBase: 'https://server.example/root/api/v1///',
  });

  assert.equal(frontendBootstrap.apiBase, 'https://server.example/root/api/v1');
  assert.equal(serverApiUrl('/handshake'), 'https://server.example/root/api/v1/handshake');
}

async function testServerProtocolContract() {
  const client = loadClient(undefined);
  delete require.cache[require.resolve(serverModule)];

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

async function main() {
  try {
    testDefaultApiBase();
    testInjectedRelativeBaseAndProfile();
    testInjectedAbsoluteBase();
    await testServerProtocolContract();
    console.log('api client tests passed');
  } finally {
    setWindow(originalWindow);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
