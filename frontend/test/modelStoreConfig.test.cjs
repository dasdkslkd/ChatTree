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
const epochModule = path.join(__dirname, '../src/runtime/connectionEpoch.ts');
const storeModule = path.join(__dirname, '../src/store/modelStore.ts');

const CONTEXT_A = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

let getConfigCalls = 0;
let updateConfigCalls = 0;
let getConfigHandler = async () => ({ default_provider: null, provider: {} });
let updateConfigHandler = async () => undefined;
let getProvidersHandler = async () => [];
let metadataHandler = async () => ({});

require.cache[require.resolve(configModule)] = {
  id: configModule,
  filename: configModule,
  loaded: true,
  exports: {
    configApi: {
      get: async () => {
        getConfigCalls += 1;
        return getConfigHandler();
      },
      update: async () => {
        updateConfigCalls += 1;
        return updateConfigHandler();
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
      getProviders: async () => getProvidersHandler(),
      metadata: async () => metadataHandler(),
    },
  },
};

const { connectionEpochRuntime } = require(epochModule);
connectionEpochRuntime.install(CONTEXT_A);
const { useModelStore } = require(storeModule);

async function testLoadConfigReusesRecentConfig() {
  getConfigHandler = async () => ({ default_provider: null, provider: {} });
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
  getConfigHandler = async () => ({ default_provider: null, provider: {} });
  updateConfigHandler = async () => undefined;
  updateConfigCalls = 0;
  getConfigCalls = 0;

  await useModelStore.getState().updateConfig({ provider: {} });

  assert.equal(updateConfigCalls, 1);
  assert.equal(getConfigCalls, 1);
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function loadFreshStore() {
  delete require.cache[require.resolve(storeModule)];
  delete require.cache[require.resolve(epochModule)];
  const { connectionEpochRuntime: runtime } = require(epochModule);
  runtime.install(CONTEXT_A);
  return { useModelStore: require(storeModule).useModelStore, runtime };
}

async function testProviderAndMetadataCompletionsAreEpochOwned() {
  const providers = deferred();
  const metadata = deferred();
  getProvidersHandler = () => providers.promise;
  metadataHandler = () => metadata.promise;
  const { useModelStore: store, runtime } = loadFreshStore();
  store.setState({
    providers: ['old-provider'],
    modelMetadata: { old: {} },
    loading: false,
    error: 'keep',
  });

  const pending = [
    store.getState().loadProviders(),
    store.getState().loadMetadata('new-provider'),
  ];
  const atInvalidation = store.getState();
  runtime.invalidate(runtime.capture());
  providers.resolve(['new-provider']);
  metadata.resolve({ 'new-model': {} });
  await Promise.all(pending);

  const after = store.getState();
  assert.deepEqual(after.providers, atInvalidation.providers);
  assert.deepEqual(after.modelMetadata, atInvalidation.modelMetadata);
  assert.equal(after.loading, atInvalidation.loading);
  assert.equal(after.error, atInvalidation.error);
}

async function testLoadConfigKeepsTokenThroughResetToDefault() {
  const metadata = deferred();
  getConfigHandler = async () => ({
    default_provider: 'provider-a',
    default_model: 'model-a',
    provider: {
      'provider-a': { models: ['model-a'], hidden_models: [] },
    },
  });
  metadataHandler = () => metadata.promise;
  const { useModelStore: store, runtime } = loadFreshStore();
  store.setState({ currentProvider: null, currentModel: null, config: null });
  const load = store.getState().loadConfig({ force: true });
  await Promise.resolve();
  await Promise.resolve();
  const atInvalidation = store.getState();
  runtime.invalidate(runtime.capture());
  metadata.resolve({ 'model-a': {} });
  await load;

  const after = store.getState();
  assert.equal(after.currentProvider, atInvalidation.currentProvider);
  assert.equal(after.currentModel, atInvalidation.currentModel);
  assert.equal(after.loading, atInvalidation.loading);
}

async function testUpdateConfigKeepsTokenThroughForcedReload() {
  const config = deferred();
  updateConfigHandler = async () => undefined;
  getConfigHandler = () => config.promise;
  const { useModelStore: store, runtime } = loadFreshStore();
  const baseline = { default_provider: 'old-provider', provider: {} };
  store.setState({ config: baseline, loading: false, error: 'keep' });
  const update = store.getState().updateConfig({ provider: {} });
  await Promise.resolve();
  await Promise.resolve();
  const atInvalidation = store.getState();
  runtime.invalidate(runtime.capture());
  config.resolve({ default_provider: 'new-provider', provider: {} });
  await update;

  const after = store.getState();
  assert.equal(after.config, atInvalidation.config);
  assert.equal(after.loading, atInvalidation.loading);
  assert.equal(after.error, atInvalidation.error);
}

async function main() {
  await testLoadConfigReusesRecentConfig();
  await testUpdateConfigForcesReload();
  await testProviderAndMetadataCompletionsAreEpochOwned();
  await testLoadConfigKeepsTokenThroughResetToDefault();
  await testUpdateConfigKeepsTokenThroughForcedReload();
  console.log('modelStoreConfig tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
