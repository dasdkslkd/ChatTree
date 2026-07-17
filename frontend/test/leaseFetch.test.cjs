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

const frontendRoot = path.join(__dirname, '..');
const bootstrapModule = path.join(frontendRoot, 'src/runtime/frontendBootstrap.ts');
const epochModule = path.join(frontendRoot, 'src/runtime/connectionEpoch.ts');
const leaseFetchModule = path.join(frontendRoot, 'src/api/leaseFetch.ts');
const messageModule = path.join(frontendRoot, 'src/api/message.ts');
const runsModule = path.join(frontendRoot, 'src/api/runs.ts');

const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const CONTEXT_A = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: LEASE_A,
});

globalThis.window = {
  location: {
    href: 'http://127.0.0.1:5173/s/local',
    pathname: '/s/local',
  },
};
require(bootstrapModule).initializeFrontendBootstrap();

function response(options = {}) {
  let cancelled = 0;
  const status = options.status ?? 200;
  const jsonBody = options.jsonBody ?? { ok: true };
  const value = {
    status,
    ok: status >= 200 && status < 300,
    headers: options.headers ?? new Headers({
      'X-ChatTree-Connection-Lease-ID': LEASE_A,
    }),
    body: {
      async cancel() {
        cancelled += 1;
      },
    },
    clone() {
      return {
        async json() {
          return jsonBody;
        },
      };
    },
    async json() {
      return jsonBody;
    },
    get cancelled() {
      return cancelled;
    },
  };
  return value;
}

function createRuntime() {
  const { ConnectionEpochRuntime } = require(epochModule);
  const runtime = new ConnectionEpochRuntime();
  runtime.install(CONTEXT_A);
  return runtime;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function testMatchingLeaseUsesProfileUrlExplicitTokenAndCombinedSignal() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const caller = new AbortController();
  const returned = response();
  let request = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    request = { input, init };
    return returned;
  };
  try {
    const actual = await leaseGuardedFetch(
      '/health',
      { signal: caller.signal, headers: { 'x-chattree-connection-lease-id': 'spoofed' } },
      token,
      runtime,
    );
    assert.equal(actual, returned);
    assert.equal(request.input, '/p/local/api/v1/health');
    assert.equal(request.init.headers.get('X-ChatTree-Connection-Lease-ID'), LEASE_A);
    assert.notEqual(request.init.signal, caller.signal);
    assert.equal(request.init.signal.aborted, false);
    runtime.invalidate(token);
    assert.equal(request.init.signal.aborted, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testAbsoluteAndNonRootRelativeInputsRemainUnchanged() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const inputs = [
    'https://launcher.example/p/local/api/v1/health',
    'health',
    new URL('https://launcher.example/p/local/api/v1/runs'),
  ];
  const seen = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    seen.push(input);
    return response();
  };
  try {
    for (const input of inputs) {
      await leaseGuardedFetch(input, {}, token, runtime);
    }
    assert.equal(seen[0], inputs[0]);
    assert.equal(seen[1], inputs[1]);
    assert.equal(seen[2], inputs[2]);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testStringInputDoesNotRequireRequestOrUrlGlobals() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const originalFetch = globalThis.fetch;
  const originalRequest = globalThis.Request;
  const originalUrl = globalThis.URL;
  let seen = null;
  globalThis.Request = undefined;
  globalThis.URL = undefined;
  globalThis.fetch = async (input) => {
    seen = input;
    return response();
  };
  try {
    await leaseGuardedFetch('/health', {}, token, runtime);
    assert.equal(seen, '/p/local/api/v1/health');
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.Request = originalRequest;
    globalThis.URL = originalUrl;
  }
}

async function testCallerCancellationIsPreservedWhileOwnerIsCurrent() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const caller = new AbortController();
  const cancelled = Object.assign(new Error('cancelled by caller'), { name: 'AbortError' });
  caller.abort(cancelled);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_input, init) => {
    assert.equal(init.signal.aborted, true);
    throw cancelled;
  };
  try {
    await assert.rejects(
      () => leaseGuardedFetch('/health', { signal: caller.signal }, token, runtime),
      (error) => error === cancelled,
    );
    assert.equal(runtime.isCurrent(token), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testRequestInputSignalIsInheritedAndPreserved() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const caller = new AbortController();
  const request = new Request(
    'https://launcher.example/p/local/api/v1/health',
    { signal: caller.signal },
  );
  const cancelled = Object.assign(new Error('request input cancelled'), {
    name: 'AbortError',
  });
  const originalFetch = globalThis.fetch;
  let transportSignal = null;
  globalThis.fetch = async (_input, init) => {
    transportSignal = init.signal;
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(init.signal.reason), {
        once: true,
      });
    });
  };
  try {
    const pending = leaseGuardedFetch(request, {}, token, runtime);
    await new Promise((resolve) => setImmediate(resolve));
    const rejected = assert.rejects(pending, (error) => error === cancelled);
    caller.abort(cancelled);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(transportSignal.aborted, true);
    await rejected;
    assert.equal(request.signal.aborted, true);
    assert.equal(runtime.isCurrent(token), true);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testRuntimeInvalidationDuringTransportBecomesCanonicalStale() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const { StaleConnectionEpochError } = require(epochModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const originalFetch = globalThis.fetch;
  let started;
  const fetchStarted = new Promise((resolve) => {
    started = resolve;
  });
  globalThis.fetch = async (_input, init) => {
    started();
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => {
        reject(Object.assign(new Error('transport aborted'), { name: 'AbortError' }));
      }, { once: true });
    });
  };
  try {
    const pending = leaseGuardedFetch('/health', {}, token, runtime);
    await fetchStarted;
    runtime.invalidate(token);
    await assert.rejects(() => pending, StaleConnectionEpochError);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testInvalidationDuringConflictCloneParsingCancelsOriginalBody() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const { StaleConnectionEpochError } = require(epochModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const cloneBody = deferred();
  const returned = response({ status: 409 });
  returned.clone = () => ({ json: () => cloneBody.promise });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => returned;
  try {
    const pending = leaseGuardedFetch('/health', {}, token, runtime);
    await new Promise((resolve) => setImmediate(resolve));
    runtime.invalidate(token);
    cloneBody.resolve({ error: { code: 'anything' } });
    await assert.rejects(() => pending, StaleConnectionEpochError);
    assert.equal(returned.cancelled, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testCallerCancellationDuringConflictCloneParsingPropagates() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const caller = new AbortController();
  const cloneBody = deferred();
  const returned = response({ status: 409 });
  returned.clone = () => ({ json: () => cloneBody.promise });
  const cancelled = Object.assign(new Error('cancelled while parsing conflict'), {
    name: 'AbortError',
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => returned;
  try {
    const pending = leaseGuardedFetch(
      '/health',
      { signal: caller.signal },
      token,
      runtime,
    );
    await new Promise((resolve) => setImmediate(resolve));
    caller.abort(cancelled);
    cloneBody.reject(cancelled);
    await assert.rejects(() => pending, (error) => error === cancelled);
    assert.equal(runtime.isCurrent(token), true);
    assert.equal(returned.cancelled, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testRequestInputCancellationDuringConflictClonePropagates() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const caller = new AbortController();
  const request = new Request(
    'https://launcher.example/p/local/api/v1/health',
    { signal: caller.signal },
  );
  const cloneBody = deferred();
  const returned = response({ status: 409 });
  returned.clone = () => ({ json: () => cloneBody.promise });
  const cancelled = Object.assign(new Error('request cancelled during clone'), {
    name: 'AbortError',
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => returned;
  try {
    const pending = leaseGuardedFetch(request, {}, token, runtime);
    await new Promise((resolve) => setImmediate(resolve));
    caller.abort(cancelled);
    cloneBody.reject(cancelled);
    await assert.rejects(() => pending, (error) => error === cancelled);
    assert.equal(runtime.isCurrent(token), true);
    assert.equal(returned.cancelled, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testInvalidResponseLeaseInvalidatesAndCancelsBody() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const { StaleConnectionEpochError } = require(epochModule);
  for (const headers of [
    new Headers(),
    { 'X-ChatTree-Connection-Lease-ID': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' },
    { 'X-ChatTree-Connection-Lease-ID': [LEASE_A] },
    { 'X-ChatTree-Connection-Lease-ID': `${LEASE_A}, ${LEASE_A}` },
  ]) {
    const runtime = createRuntime();
    const token = runtime.capture();
    const returned = response({ headers });
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => returned;
    try {
      await assert.rejects(
        () => leaseGuardedFetch('/health', {}, token, runtime),
        StaleConnectionEpochError,
      );
      assert.equal(runtime.isCurrent(token), false);
      assert.equal(runtime.signalFor(token).aborted, true);
      assert.equal(returned.cancelled, 1);
    } finally {
      globalThis.fetch = originalFetch;
    }
  }
}

async function testMatchingOrdinary409RemainsReadable() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const body = {
    error: {
      code: 'active_runs_present',
      message: 'still running',
      retryable: true,
      request_id: 'req_ordinary_409',
    },
  };
  const returned = response({ status: 409, jsonBody: body });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => returned;
  try {
    const actual = await leaseGuardedFetch('/runs/stop', {}, token, runtime);
    assert.equal(actual, returned);
    assert.deepEqual(await actual.json(), body);
    assert.equal(runtime.isCurrent(token), true);
    assert.equal(returned.cancelled, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testMatchingStale409InvalidatesBeforeCallerBodyParsing() {
  const { leaseGuardedFetch } = require(leaseFetchModule);
  const { StaleConnectionEpochError } = require(epochModule);
  const runtime = createRuntime();
  const token = runtime.capture();
  const returned = response({
    status: 409,
    jsonBody: {
      error: {
        code: 'stale_connection_epoch',
        message: 'stale',
        retryable: false,
        request_id: 'req_stale_409',
      },
    },
  });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => returned;
  try {
    await assert.rejects(
      () => leaseGuardedFetch('/health', {}, token, runtime),
      StaleConnectionEpochError,
    );
    assert.equal(runtime.isCurrent(token), false);
    assert.equal(returned.cancelled, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function testRawStreamApisUseExplicitTokenOptionsWithoutLazyCapture() {
  const { connectionEpochRuntime } = require(epochModule);
  connectionEpochRuntime.install(CONTEXT_A);
  const token = connectionEpochRuntime.capture();
  const originalCapture = connectionEpochRuntime.capture;
  connectionEpochRuntime.capture = () => {
    throw new Error('raw stream API recaptured an epoch lazily');
  };
  const { messageApi } = require(messageModule);
  const { runsApi } = require(runsModule);
  const requests = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    requests.push({ input, init });
    return new Response(
      `data: ${JSON.stringify({ status: 'content', event_type: 'text', content: 'ok' })}\n\ndata: [DONE]\n\n`,
      {
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'X-ChatTree-Connection-Lease-ID': LEASE_A,
        },
      },
    );
  };
  const controller = new AbortController();
  try {
    const streams = [
      messageApi.stream('conv/1', { content: 'hello' }, {
        token,
        nodeId: 'node-1',
        signal: controller.signal,
      }),
      messageApi.attachStream('conv/1', 'node/1', {
        token,
        fromEvent: 7,
        signal: controller.signal,
      }),
      messageApi.streamPlanApproval('conv/1', 'plan/1', {}, { token, signal: controller.signal }),
      messageApi.streamPlanAnswer('conv/1', 'plan/1', { answer: 'yes' }, { token, signal: controller.signal }),
      messageApi.streamPlanReject('conv/1', 'plan/1', { feedback: 'no' }, { token, signal: controller.signal }),
      runsApi.attach('run/1', { token, fromEvent: 9, signal: controller.signal }),
    ];
    for (const stream of streams) {
      const chunks = [];
      for await (const item of stream) chunks.push(item);
      assert.equal(chunks.length, 1);
    }
    const streamRequests = requests.filter(
      (request) => !String(request.input).includes('/perf/'),
    );
    assert.deepEqual(streamRequests.map((request) => request.input), [
      '/p/local/api/v1/conversations/conv%2F1/messages/stream',
      '/p/local/api/v1/conversations/conv%2F1/messages/node%2F1/stream/attach?from_event=7',
      '/p/local/api/v1/conversations/conv%2F1/plans/plan%2F1/approve/stream',
      '/p/local/api/v1/conversations/conv%2F1/plans/plan%2F1/answer/stream',
      '/p/local/api/v1/conversations/conv%2F1/plans/plan%2F1/reject/stream',
      '/p/local/api/v1/runs/run%2F1/attach?from_event=9',
    ]);
    for (const request of streamRequests) {
      assert.equal(request.init.headers.get('X-ChatTree-Connection-Lease-ID'), LEASE_A);
      assert.notEqual(request.init.signal, controller.signal);
    }
  } finally {
    connectionEpochRuntime.capture = originalCapture;
    globalThis.fetch = originalFetch;
  }
}

async function testRawStreamEarlyFailuresCancelResponseBodies() {
  const { connectionEpochRuntime, StaleConnectionEpochError } = require(epochModule);
  const { messageApi } = require(messageModule);
  const { runsApi } = require(runsModule);
  const token = connectionEpochRuntime.capture();
  const originalFetch = globalThis.fetch;
  const originalIsCurrent = connectionEpochRuntime.isCurrent;

  const consume = async (stream) => {
    for await (const _item of stream) {
      // These cases fail before emitting any event.
    }
  };

  try {
    for (const streamFactory of [
      () => messageApi.stream('conv-1', { content: 'hello' }, { token }),
      () => runsApi.attach('run-1', { token }),
    ]) {
      const returned = response({ status: 500 });
      globalThis.fetch = async () => returned;
      await assert.rejects(() => consume(streamFactory()), /HTTP error! status: 500/);
      assert.equal(returned.cancelled, 1);
    }

    for (const streamFactory of [
      () => messageApi.stream('conv-1', { content: 'hello' }, { token }),
      () => runsApi.attach('run-1', { token }),
    ]) {
      const returned = response();
      let currentChecks = 0;
      globalThis.fetch = async () => returned;
      connectionEpochRuntime.isCurrent = function isCurrentUntilParsing(owner) {
        currentChecks += 1;
        if (currentChecks >= 3) return false;
        return originalIsCurrent.call(this, owner);
      };
      await assert.rejects(() => consume(streamFactory()), StaleConnectionEpochError);
      assert.equal(returned.cancelled, 1);
      connectionEpochRuntime.isCurrent = originalIsCurrent;
    }
  } finally {
    connectionEpochRuntime.isCurrent = originalIsCurrent;
    globalThis.fetch = originalFetch;
  }
}

async function main() {
  await testMatchingLeaseUsesProfileUrlExplicitTokenAndCombinedSignal();
  await testAbsoluteAndNonRootRelativeInputsRemainUnchanged();
  await testStringInputDoesNotRequireRequestOrUrlGlobals();
  await testCallerCancellationIsPreservedWhileOwnerIsCurrent();
  await testRequestInputSignalIsInheritedAndPreserved();
  await testRuntimeInvalidationDuringTransportBecomesCanonicalStale();
  await testInvalidationDuringConflictCloneParsingCancelsOriginalBody();
  await testCallerCancellationDuringConflictCloneParsingPropagates();
  await testRequestInputCancellationDuringConflictClonePropagates();
  await testInvalidResponseLeaseInvalidatesAndCancelsBody();
  await testMatchingOrdinary409RemainsReadable();
  await testMatchingStale409InvalidatesBeforeCallerBodyParsing();
  await testRawStreamApisUseExplicitTokenOptionsWithoutLazyCapture();
  await testRawStreamEarlyFailuresCancelResponseBodies();
  console.log('lease fetch tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
