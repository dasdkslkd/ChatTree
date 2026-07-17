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
const epochModule = path.join(frontendRoot, 'src/runtime/connectionEpoch.ts');
const identityModule = path.join(frontendRoot, 'src/runtime/connectionIdentity.ts');

const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const LEASE_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const CONTEXT_A = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
  serverInstanceId: '11111111-1111-4111-8111-111111111111',
  connectionEpoch: 4,
  connectionLeaseId: LEASE_A,
});

function testInstallCaptureAndPermanentInvalidation() {
  const epoch = require(epochModule);
  const identity = require(identityModule);
  const runtime = new epoch.ConnectionEpochRuntime();

  assert.throws(() => runtime.capture(), epoch.StaleConnectionEpochError);
  runtime.install(CONTEXT_A);
  const token = runtime.capture();
  assert.equal(Object.isFrozen(token), true);
  assert.deepEqual(token, {
    profileId: CONTEXT_A.profileId,
    serverInstanceId: CONTEXT_A.serverInstanceId,
    connectionEpoch: CONTEXT_A.connectionEpoch,
    connectionLeaseId: CONTEXT_A.connectionLeaseId,
    generation: 1,
  });
  assert.equal(runtime.isCurrent(token), true);
  assert.doesNotThrow(() => runtime.assertCurrent(token));
  assert.equal(runtime.signalFor(token).aborted, false);

  runtime.install({ ...CONTEXT_A });
  assert.equal(runtime.capture().generation, 1, 'equal valid installs are idempotent');
  assert.throws(
    () => runtime.install({ ...CONTEXT_A, connectionLeaseId: LEASE_B }),
    identity.BoundServerLeaseChangedError,
  );

  const signal = runtime.signalFor(token);
  assert.equal(runtime.invalidate(token), true);
  assert.equal(signal.aborted, true);
  assert.equal(runtime.isCurrent(token), false);
  assert.equal(runtime.signalFor(token).aborted, true);
  assert.throws(() => runtime.assertCurrent(token), epoch.StaleConnectionEpochError);
  assert.throws(() => runtime.capture(), epoch.StaleConnectionEpochError);
  assert.equal(runtime.invalidate(token), false, 'a stale CAS token is inert');
  assert.throws(
    () => runtime.install({ ...CONTEXT_A }),
    identity.BoundServerLeaseChangedError,
    'invalidated identities cannot be reinstalled in the same page realm',
  );
}

function testSynchronousSingleNotificationAndLateReplay() {
  const { ConnectionEpochRuntime } = require(epochModule);
  const runtime = new ConnectionEpochRuntime();
  runtime.install(CONTEXT_A);
  const token = runtime.capture();
  const events = [];
  const unsubscribeFirst = runtime.subscribeInvalidation(() => {
    events.push(`first:${runtime.signalFor(token).aborted}`);
  });
  const unsubscribeSecond = runtime.subscribeInvalidation(() => {
    events.push('second');
  });
  unsubscribeSecond();

  assert.equal(runtime.invalidate(token), true);
  assert.deepEqual(events, ['first:true']);
  assert.equal(runtime.invalidate(token), false);
  assert.deepEqual(events, ['first:true']);
  unsubscribeFirst();
  unsubscribeFirst();

  let lateCalls = 0;
  const unsubscribeLate = runtime.subscribeInvalidation(() => {
    lateCalls += 1;
  });
  assert.equal(lateCalls, 1, 'late subscribers synchronously replay invalidation');
  unsubscribeLate();
}

function testOldTokenCannotInvalidateAnotherRuntimeGeneration() {
  const { ConnectionEpochRuntime } = require(epochModule);
  const first = new ConnectionEpochRuntime();
  first.install(CONTEXT_A);
  const oldToken = first.capture();

  const second = new ConnectionEpochRuntime();
  second.install({ ...CONTEXT_A, connectionEpoch: 5 });
  const currentToken = second.capture();
  assert.equal(second.invalidate(oldToken), false);
  assert.equal(second.isCurrent(currentToken), true);
}

function testAbortSignalCompositionPreservesFirstReason() {
  const { composeConnectionAbortSignal } = require(epochModule);
  const runtime = new AbortController();
  assert.equal(composeConnectionAbortSignal(undefined, runtime.signal), runtime.signal);
  assert.equal(composeConnectionAbortSignal(null, runtime.signal), runtime.signal);

  const caller = new AbortController();
  const combined = composeConnectionAbortSignal(caller.signal, runtime.signal);
  const callerReason = new Error('caller cancelled');
  caller.abort(callerReason);
  assert.equal(combined.aborted, true);
  assert.equal(combined.reason, callerReason);

  const alreadyRuntime = new AbortController();
  const runtimeReason = new Error('runtime invalidated');
  alreadyRuntime.abort(runtimeReason);
  const freshCaller = new AbortController();
  const alreadyCombined = composeConnectionAbortSignal(
    freshCaller.signal,
    alreadyRuntime.signal,
  );
  assert.equal(alreadyCombined.aborted, true);
  assert.equal(alreadyCombined.reason, runtimeReason);
}

function testEpochLeafHasNoApiOrBootstrapImport() {
  const source = fs.readFileSync(epochModule, 'utf8');
  const sourceFile = ts.createSourceFile(
    epochModule,
    source,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  const imports = sourceFile.statements
    .filter(ts.isImportDeclaration)
    .map(statement => statement.moduleSpecifier.text);
  assert.deepEqual(imports, ['./connectionIdentity']);
  assert.match(
    source,
    /AbortSignal\.any\(\[caller, runtime\]\)/,
    'signal composition must not retain per-request listeners on the page runtime signal',
  );
}

function main() {
  testInstallCaptureAndPermanentInvalidation();
  testSynchronousSingleNotificationAndLateReplay();
  testOldTokenCannotInvalidateAnotherRuntimeGeneration();
  testAbortSignalCompositionPreservesFirstReason();
  testEpochLeafHasNoApiOrBootstrapImport();
  console.log('connection epoch tests passed');
}

main();
