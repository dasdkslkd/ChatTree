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
const leaseFetchPath = path.join(__dirname, '../src/api/leaseFetch.ts');
const perfMarksPath = path.join(__dirname, '../src/perf/marks.ts');

let nextResponse;

require.cache[require.resolve(clientPath)] = {
  id: clientPath,
  filename: clientPath,
  loaded: true,
  exports: {
    apiClient: {
      async get() { throw new Error('unexpected apiClient.get'); },
      async post() { throw new Error('unexpected apiClient.post'); },
    },
  },
};

require.cache[require.resolve(leaseFetchPath)] = {
  id: leaseFetchPath,
  filename: leaseFetchPath,
  loaded: true,
  exports: {
    async leaseGuardedFetch() {
      if (!nextResponse) throw new Error('missing SSE response');
      const response = nextResponse;
      nextResponse = undefined;
      return response;
    },
  },
};

require.cache[require.resolve(perfMarksPath)] = {
  id: perfMarksPath,
  filename: perfMarksPath,
  loaded: true,
  exports: {
    perfNow: () => 0,
    recordMark() {},
    recordSpan() {},
  },
};

const { ChatTreeApiError } = require('../src/api/errors.ts');
const { runsApi } = require('../src/api/runs.ts');
const { messageApi } = require('../src/api/message.ts');

const encoder = new TextEncoder();

function sseResponse(chunks, failure) {
  let index = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (index < chunks.length) {
        controller.enqueue(encoder.encode(chunks[index]));
        index += 1;
        return;
      }
      if (failure) {
        controller.error(failure);
        return;
      }
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

async function collect(stream) {
  const events = [];
  for await (const event of stream) events.push(event);
  return events;
}

function isUnexpectedResponse(error) {
  return error instanceof ChatTreeApiError
    && error.code === 'unexpected_response'
    && error.retryable === false;
}

function isNetworkError(error) {
  return error instanceof ChatTreeApiError
    && error.code === 'network_error'
    && error.retryable === true;
}

async function testRunsOrderlyEofWithoutDoneFailsClosed() {
  nextResponse = sseResponse(['data: {"status":"completed"}\n\n']);
  await assert.rejects(
    collect(runsApi.attach('run-1', {})),
    isUnexpectedResponse,
  );
}

async function testMessageOrderlyEofWithoutDoneFailsClosed() {
  nextResponse = sseResponse(['data: {"status":"completed"}\n\n']);
  await assert.rejects(
    collect(messageApi.attachStream('conv-1', 'node-1', {})),
    isUnexpectedResponse,
  );
}

async function testRunsAcceptsFinalDoneWithoutTrailingDelimiter() {
  nextResponse = sseResponse([
    'data: {"status":"content","content":"ok"}\n\ndata:[DONE]',
  ]);
  const events = await collect(runsApi.attach('run-2', {}));
  assert.deepEqual(events, [{ status: 'content', content: 'ok' }]);
}

async function testMessageAcceptsFinalDoneWithoutTrailingDelimiter() {
  nextResponse = sseResponse([
    'data: {"status":"content","content":"ok"}\n\ndata:[DONE]',
  ]);
  const events = await collect(messageApi.attachStream('conv-2', 'node-2', {}));
  assert.deepEqual(events, [{ status: 'content', content: 'ok' }]);
}

async function testRunsMidReadFailureRemainsNetworkError() {
  nextResponse = sseResponse(
    ['data: {"status":"content","content":"partial"}\n\n'],
    new TypeError('network dropped'),
  );
  await assert.rejects(
    collect(runsApi.attach('run-3', {})),
    isNetworkError,
  );
}

async function testMessageMidReadFailureRemainsNetworkError() {
  nextResponse = sseResponse(
    ['data: {"status":"content","content":"partial"}\n\n'],
    new TypeError('network dropped'),
  );
  await assert.rejects(
    collect(messageApi.attachStream('conv-3', 'node-3', {})),
    isNetworkError,
  );
}

(async () => {
  await testRunsOrderlyEofWithoutDoneFailsClosed();
  await testMessageOrderlyEofWithoutDoneFailsClosed();
  await testRunsAcceptsFinalDoneWithoutTrailingDelimiter();
  await testMessageAcceptsFinalDoneWithoutTrailingDelimiter();
  await testRunsMidReadFailureRemainsNetworkError();
  await testMessageMidReadFailureRemainsNetworkError();
  console.log('SSE EOF contract tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
