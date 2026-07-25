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
const transcriptModule = path.join(__dirname, '../src/services/transcript.ts');

require.cache[require.resolve(clientModule)] = {
  id: clientModule,
  filename: clientModule,
  loaded: true,
  exports: {
    apiClient: {
      get: async () => {
        throw new Error('unexpected default client call');
      },
    },
  },
};
delete require.cache[require.resolve(transcriptModule)];

const { createTranscriptService } = require(transcriptModule);

function fakeClient(responseData) {
  const calls = [];
  return {
    calls,
    client: {
      async get(url, config) {
        calls.push({ method: 'get', url, config });
        return { data: responseData };
      },
    },
  };
}

async function testFetchBranchSnapshotEncodesBothIdsAndForwardsSignal() {
  const expectedSnapshot = {
    conversation_id: 'conv/1',
    node_id: 'node/1',
    revision: 7,
    items: [{ id: 'item-1', type: 'user_message', preview: 'hello' }],
  };
  const { client, calls } = fakeClient(expectedSnapshot);
  const service = createTranscriptService(client);
  const controller = new AbortController();

  const snapshot = await service.fetchBranchSnapshot('conv/1', 'node/1', controller.signal);

  assert.deepEqual(snapshot, expectedSnapshot);
  assert.deepEqual(calls[0], {
    method: 'get',
    url: '/conversations/conv%2F1/transcript',
    config: { signal: controller.signal, params: { node_id: 'node/1' } },
  });
}

async function testFetchBranchSnapshotReturnsCompleteResponseWithoutSignal() {
  const expectedSnapshot = {
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 0,
    items: [],
  };
  const { client, calls } = fakeClient(expectedSnapshot);
  const service = createTranscriptService(client);

  const snapshot = await service.fetchBranchSnapshot('conv-1', 'node-1');

  assert.deepEqual(snapshot, expectedSnapshot);
  assert.deepEqual(calls[0], {
    method: 'get',
    url: '/conversations/conv-1/transcript',
    config: { params: { node_id: 'node-1' } },
  });
}

async function main() {
  await testFetchBranchSnapshotEncodesBothIdsAndForwardsSignal();
  await testFetchBranchSnapshotReturnsCompleteResponseWithoutSignal();
  console.log('transcriptService tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
