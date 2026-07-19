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
    },
  }).outputText;
  module._compile(output, filename);
};

const clientPath = path.join(__dirname, '../src/api/client.ts');
const launcherPath = path.join(__dirname, '../src/api/launcher.ts');
const routePath = path.join(__dirname, '../src/runtime/profileRoute.ts');
const requests = [];

require.cache[require.resolve(clientPath)] = {
  id: clientPath,
  filename: clientPath,
  loaded: true,
  exports: {
    createApiClient(baseUrl) {
      return {
        async get(url, options) {
          requests.push({ method: 'GET', baseUrl, url, options });
          if (url === '/ssh/config') {
            return {
              data: {
                path: 'C:/Users/me/.ssh/config',
                text: 'Host gpu-box\n',
                hosts: ['gpu-box'],
                warnings: [],
              },
            };
          }
          if (url === '/ssh/hosts') {
            return {
              data: {
                path: 'C:/Users/me/.ssh/config',
                hosts: ['gpu-box'],
                warnings: [],
              },
            };
          }
          return {
            data: {
              profile_id: 'ssh:Z3B1LWJveA',
              host_alias: 'gpu-box',
              session: { status: 'ready', profile_id: 'ssh:Z3B1LWJveA' },
            },
          };
        },
        async put(url, data) {
          requests.push({ method: 'PUT', baseUrl, url, data });
          return {
            data: {
              path: 'C:/Users/me/.ssh/config',
              text: data.text,
              hosts: ['gpu-box'],
              warnings: [],
            },
          };
        },
        async post(url, data) {
          requests.push({ method: 'POST', baseUrl, url, data });
          return {
            data: {
              profile_id: 'ssh:Z3B1LWJveA',
              host_alias: 'gpu-box',
              session: { status: 'ready', profile_id: 'ssh:Z3B1LWJveA' },
            },
          };
        },
      };
    },
  },
};

const { createLauncherApi } = require(launcherPath);
const { buildFrontendRoute } = require(routePath);

async function testSshConfigApiUsesLauncherOriginAndExpectedRoutes() {
  requests.length = 0;
  const api = createLauncherApi(
    { profileId: 'local', apiBase: '/p/local/api/v1' },
    'http://127.0.0.1:18100/s/local',
  );

  await api.getSshConfig();
  await api.saveSshConfig('Host gpu-box\n');
  await api.listSshHosts();
  await api.getSshHostStatus('gpu box');
  await api.connectSshHost('gpu box');
  await api.disconnectSshHost('gpu box');

  assert.deepEqual(requests.map((request) => [request.method, request.baseUrl, request.url]), [
    ['GET', 'http://127.0.0.1:18100/client/v1', '/ssh/config'],
    ['PUT', 'http://127.0.0.1:18100/client/v1', '/ssh/config'],
    ['GET', 'http://127.0.0.1:18100/client/v1', '/ssh/hosts'],
    ['GET', 'http://127.0.0.1:18100/client/v1', '/ssh/hosts/gpu%20box/status'],
    ['POST', 'http://127.0.0.1:18100/client/v1', '/ssh/hosts/gpu%20box/connect'],
    ['POST', 'http://127.0.0.1:18100/client/v1', '/ssh/hosts/gpu%20box/disconnect'],
  ]);
  assert.deepEqual(requests[1].data, { text: 'Host gpu-box\n' });
}

function testConnectedSshProfileBuildsProfileRoute() {
  assert.equal(
    buildFrontendRoute({ profileId: 'ssh:Z3B1LWJveA' }),
    '/s/ssh%3AZ3B1LWJveA',
  );
}

(async () => {
  await testSshConfigApiUsesLauncherOriginAndExpectedRoutes();
  testConnectedSshProfileBuildsProfileRoute();
  console.log('ssh config API tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
