const assert = require('node:assert/strict');
const fs = require('node:fs');
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

globalThis.window = {
  location: {
    href: 'http://127.0.0.1:18100/s/local',
    pathname: '/s/local',
  },
};
require('../src/runtime/profileContext.ts').initializeProfileContext();

const {
  CONNECTION_LEASE_HEADER,
} = require('../src/api/connectionLeaseHeader.ts');
const {
  leaseGuardedFetch,
} = require('../src/api/leaseFetch.ts');
const {
  StaleConnectionScopeError,
} = require('../src/runtime/connectionScope.ts');
const { ChatTreeApiError } = require('../src/api/errors.ts');

const LEASE_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

function createRuntime() {
  const controller = new AbortController();
  const scope = Object.freeze({
    profileId: 'local',
    serverInstanceId: '11111111-1111-4111-8111-111111111111',
    leaseId: LEASE_ID,
    signal: controller.signal,
  });
  let active = true;
  let invalidations = 0;
  return {
    scope,
    runtime: {
      current() {
        if (!active) throw new StaleConnectionScopeError();
        return scope;
      },
      isActive(candidate) {
        return active && candidate === scope;
      },
      invalidate(candidate) {
        if (!active || candidate !== scope) return false;
        active = false;
        invalidations += 1;
        controller.abort();
        return true;
      },
    },
    invalidate() {
      active = false;
      controller.abort();
    },
    get invalidations() {
      return invalidations;
    },
  };
}

function response(body = 'ok', options = {}) {
  return new Response(body, {
    status: options.status ?? 200,
    headers: {
      [CONNECTION_LEASE_HEADER]: options.leaseId ?? LEASE_ID,
      'Content-Type': options.contentType ?? 'text/plain',
    },
  });
}

async function withFetch(mock, run) {
  const original = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    return await run();
  } finally {
    globalThis.fetch = original;
  }
}

async function testRelativeRequestUsesProfileProxyAndScopeLease() {
  const state = createRuntime();
  const caller = new AbortController();
  let seen;

  const result = await withFetch(async (input, init) => {
    seen = { input, init };
    return response();
  }, () => leaseGuardedFetch(
    '/conversations/conv%2F1/messages/stream',
    {
      headers: { [CONNECTION_LEASE_HEADER]: 'spoofed' },
      signal: caller.signal,
    },
    state.runtime,
  ));

  assert.equal(
    seen.input,
    '/p/local/api/v1/conversations/conv%2F1/messages/stream',
  );
  assert.equal(new Headers(seen.init.headers).get(CONNECTION_LEASE_HEADER), LEASE_ID);
  assert.notEqual(seen.init.signal, caller.signal);
  assert.notEqual(seen.init.signal, state.scope.signal);
  assert.equal(await result.text(), 'ok');
}

async function testMismatchedLeaseInvalidatesScope() {
  const state = createRuntime();
  await withFetch(
    async () => response('stale', {
      leaseId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    }),
    async () => {
      await assert.rejects(
        leaseGuardedFetch('/runs/1/events', {}, state.runtime),
        StaleConnectionScopeError,
      );
    },
  );
  assert.equal(state.invalidations, 1);
  assert.equal(state.scope.signal.aborted, true);
}

async function testExplicitStaleLeaseEnvelopeInvalidatesScope() {
  const state = createRuntime();
  await withFetch(
    async () => response(JSON.stringify({
      error: {
        code: 'stale_connection_epoch',
        message: 'Lease is stale',
        retryable: true,
        request_id: 'stale-tree',
      },
    }), {
      status: 409,
      contentType: 'application/json',
    }),
    async () => {
      await assert.rejects(
        leaseGuardedFetch('/runs/1/events', {}, state.runtime),
        StaleConnectionScopeError,
      );
    },
  );
  assert.equal(state.invalidations, 1);
}

async function testTransportFailureAfterInvalidationBecomesStaleError() {
  const state = createRuntime();
  let rejectFetch;
  const blocked = new Promise((_, reject) => {
    rejectFetch = reject;
  });

  await withFetch(
    async () => blocked,
    async () => {
      const pending = leaseGuardedFetch('/runs/1/events', {}, state.runtime);
      await Promise.resolve();
      state.invalidate();
      rejectFetch(new TypeError('network dropped'));
      await assert.rejects(pending, StaleConnectionScopeError);
    },
  );
}

async function testModernFetchErrorPreservesProtocolFields() {
  const state = createRuntime();
  await withFetch(
    async () => response(JSON.stringify({
      error: {
        code: 'server_busy',
        message: 'Server has active runs',
        retryable: true,
        request_id: 'busy-tree-1',
        details: { active_run_ids: ['run-1'] },
      },
    }), {
      status: 409,
      contentType: 'application/json',
    }),
    async () => {
      await assert.rejects(
        leaseGuardedFetch('/server/shutdown', {}, state.runtime),
        (error) => (
          error instanceof ChatTreeApiError
          && error.status === 409
          && error.code === 'server_busy'
          && error.retryable === true
          && error.requestId === 'busy-tree-1'
          && error.details.active_run_ids[0] === 'run-1'
        ),
      );
    },
  );
  assert.equal(state.invalidations, 0);
}

async function testMalformedFetchErrorFailsClosed() {
  const state = createRuntime();
  await withFetch(
    async () => response('{"detail":"legacy"}', {
      status: 500,
      contentType: 'application/json',
    }),
    async () => {
      await assert.rejects(
        leaseGuardedFetch('/runs/1/attach', {}, state.runtime),
        (error) => (
          error instanceof ChatTreeApiError
          && error.status === 500
          && error.code === 'unexpected_response'
          && error.retryable === false
        ),
      );
    },
  );
}

async function testActiveTransportFailureBecomesRetryableNetworkError() {
  const state = createRuntime();
  await withFetch(
    async () => {
      throw new TypeError('network dropped');
    },
    async () => {
      await assert.rejects(
        leaseGuardedFetch('/runs/1/attach', {}, state.runtime),
        (error) => (
          error instanceof ChatTreeApiError
          && error.code === 'network_error'
          && error.retryable === true
          && error.cause instanceof TypeError
        ),
      );
    },
  );
}

(async () => {
  await testRelativeRequestUsesProfileProxyAndScopeLease();
  await testMismatchedLeaseInvalidatesScope();
  await testExplicitStaleLeaseEnvelopeInvalidatesScope();
  await testTransportFailureAfterInvalidationBecomesStaleError();
  await testModernFetchErrorPreservesProtocolFields();
  await testMalformedFetchErrorFailsClosed();
  await testActiveTransportFailureBecomesRetryableNetworkError();
  console.log('leaseFetch tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
