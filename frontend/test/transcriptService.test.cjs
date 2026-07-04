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

const { createTranscriptService } = require(path.join(__dirname, '../src/services/transcript.ts'));

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

async function testFetchTranscriptCallsTranscriptRouteAndReturnsItems() {
  const expectedItems = [{ id: 'item-1', type: 'user_message', preview: 'hello' }];
  const { client, calls } = fakeClient({ items: expectedItems });
  const service = createTranscriptService(client);

  const items = await service.fetchTranscript('conv/1', 'node-1');

  assert.deepEqual(items, expectedItems);
  assert.deepEqual(calls[0], {
    method: 'get',
    url: '/conversations/conv%2F1/transcript',
    config: { params: { node_id: 'node-1' } },
  });
}

async function testFetchTranscriptDefaultsMissingItemsToEmptyArray() {
  const { client } = fakeClient({});
  const service = createTranscriptService(client);

  const items = await service.fetchTranscript('conv-1');

  assert.deepEqual(items, []);
}

async function main() {
  await testFetchTranscriptCallsTranscriptRouteAndReturnsItems();
  await testFetchTranscriptDefaultsMissingItemsToEmptyArray();
  console.log('transcriptService tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
