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

const configModule = path.join(__dirname, '../src/api/config.ts');
const modelModule = path.join(__dirname, '../src/api/model.ts');
const storeModule = path.join(__dirname, '../src/store/modelStore.ts');

let getConfigCalls = 0;
let updateConfigCalls = 0;

require.cache[require.resolve(configModule)] = {
  id: configModule,
  filename: configModule,
  loaded: true,
  exports: {
    configApi: {
      get: async () => {
        getConfigCalls += 1;
        return {
          default_provider: null,
          provider: {},
        };
      },
      update: async () => {
        updateConfigCalls += 1;
      },
    },
  },
};

require.cache[require.resolve(modelModule)] = {
  id: modelModule,
  filename: modelModule,
  loaded: true,
  exports: {
    modelApi: {
      getProviders: async () => [],
      metadata: async () => ({}),
    },
  },
};

const { useModelStore } = require(storeModule);

async function testLoadConfigReusesRecentConfig() {
  getConfigCalls = 0;
  useModelStore.setState({
    config: null,
    currentProvider: 'existing-provider',
    currentModel: 'existing-model',
    loading: false,
    error: null,
  });

  await useModelStore.getState().loadConfig();
  await useModelStore.getState().loadConfig();

  assert.equal(getConfigCalls, 1);
}

async function testUpdateConfigForcesReload() {
  updateConfigCalls = 0;
  getConfigCalls = 0;

  await useModelStore.getState().updateConfig({ provider: {} });

  assert.equal(updateConfigCalls, 1);
  assert.equal(getConfigCalls, 1);
}

async function main() {
  await testLoadConfigReusesRecentConfig();
  await testUpdateConfigForcesReload();
  console.log('modelStoreConfig tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
