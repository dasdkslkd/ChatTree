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

const {
  ConnectionScopeRuntime,
  StaleConnectionScopeError,
  composeConnectionAbortSignal,
} = require('../src/runtime/connectionScope.ts');

const CONTEXT = Object.freeze({
  profileId: 'local',
  apiBase: '/p/local/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 1,
  connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
});

function testScopeIsImmutableAndInstalledOncePerPage() {
  const runtime = new ConnectionScopeRuntime();
  const scope = runtime.install(CONTEXT);

  assert.equal(Object.isFrozen(scope), true);
  assert.deepEqual(
    {
      profileId: scope.profileId,
      serverInstanceId: scope.serverInstanceId,
      leaseId: scope.leaseId,
    },
    {
      profileId: CONTEXT.profileId,
      serverInstanceId: CONTEXT.serverInstanceId,
      leaseId: CONTEXT.connectionLeaseId,
    },
  );
  assert.equal(runtime.current(), scope);
  assert.equal(runtime.install({ ...CONTEXT }), scope);
  assert.throws(
    () => runtime.install({
      ...CONTEXT,
      connectionLeaseId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    }),
    /page reload/i,
  );
}

function testInvalidationAbortsOnceAndMakesScopeStale() {
  const runtime = new ConnectionScopeRuntime();
  const scope = runtime.install(CONTEXT);
  let notifications = 0;
  runtime.subscribeInvalidation(() => {
    notifications += 1;
  });

  assert.equal(runtime.invalidate(scope), true);
  assert.equal(scope.signal.aborted, true);
  assert.equal(runtime.isActive(scope), false);
  assert.equal(runtime.invalidate(scope), false);
  assert.equal(notifications, 1);
  assert.throws(() => runtime.current(), StaleConnectionScopeError);

  let lateNotifications = 0;
  runtime.subscribeInvalidation(() => {
    lateNotifications += 1;
  });
  assert.equal(lateNotifications, 1);
}

function testComposedSignalHonorsCallerAndPageScope() {
  const caller = new AbortController();
  const page = new AbortController();
  const composed = composeConnectionAbortSignal(caller.signal, page.signal);

  assert.notEqual(composed, caller.signal);
  assert.notEqual(composed, page.signal);
  caller.abort();
  assert.equal(composed.aborted, true);
  assert.equal(composeConnectionAbortSignal(undefined, page.signal), page.signal);
}

testScopeIsImmutableAndInstalledOncePerPage();
testInvalidationAbortsOnceAndMakesScopeStale();
testComposedSignalHonorsCallerAndPageScope();
console.log('connectionScope tests passed');
