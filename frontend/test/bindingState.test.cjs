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
const bindingStateModule = path.join(frontendRoot, 'src/runtime/bindingState.ts');
const identityModule = path.join(frontendRoot, 'src/runtime/connectionIdentity.ts');

const SERVER_A = '11111111-1111-4111-8111-111111111111';
const SERVER_B = '22222222-2222-4222-8222-222222222222';
const LEASE_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const LEASE_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const CONTEXT_EPOCH_1 = Object.freeze({
  profileId: 'profile-a',
  apiBase: '/p/profile-a/api/v1',
  serverInstanceId: SERVER_A,
  connectionEpoch: 1,
  connectionLeaseId: LEASE_A,
});

function main() {
  const binding = require(bindingStateModule);
  const identity = require(identityModule);

  const initial = binding.createInitialBindingState();
  assert.deepEqual(initial, {
    status: 'connecting',
    context: null,
    error: null,
  });

  const ready = binding.reduceBindingState(initial, {
    type: 'probe_ready',
    context: CONTEXT_EPOCH_1,
  });
  assert.equal(ready.status, 'ready');
  assert.equal(ready.context, CONTEXT_EPOCH_1);
  assert.equal(ready.error, null);

  const offline = new Error('offline');
  const disconnected = binding.reduceBindingState(ready, {
    type: 'probe_failed',
    error: offline,
  });
  assert.equal(disconnected.status, 'disconnected');
  assert.equal(disconnected.context, CONTEXT_EPOCH_1);
  assert.equal(disconnected.error, offline);

  const reconnected = binding.reduceBindingState(disconnected, {
    type: 'probe_ready',
    context: { ...CONTEXT_EPOCH_1 },
  });
  assert.equal(reconnected.status, 'ready');
  assert.equal(reconnected.context, CONTEXT_EPOCH_1);
  assert.equal(reconnected.error, null);

  const initialFailure = binding.reduceBindingState(initial, {
    type: 'probe_failed',
    error: offline,
  });
  assert.equal(initialFailure.status, 'connecting');
  assert.equal(initialFailure.context, null);

  for (const context of [
    { ...CONTEXT_EPOCH_1, serverInstanceId: SERVER_B },
    { ...CONTEXT_EPOCH_1, connectionEpoch: 2 },
    { ...CONTEXT_EPOCH_1, connectionEpoch: 0 },
    { ...CONTEXT_EPOCH_1, connectionLeaseId: LEASE_B },
  ]) {
    assert.throws(
      () => binding.reduceBindingState(ready, {
        type: 'probe_ready',
        context,
      }),
      identity.BoundServerLeaseChangedError,
    );
  }

  for (const context of [
    { ...CONTEXT_EPOCH_1, profileId: 'profile-b' },
    { ...CONTEXT_EPOCH_1, apiBase: '/p/profile-b/api/v1' },
  ]) {
    assert.throws(
      () => binding.reduceBindingState(ready, {
        type: 'probe_ready',
        context,
      }),
      identity.BoundServerIdentityError,
    );
  }

  const fatal = new Error('fatal');
  const failedPermanently = binding.reduceBindingState(disconnected, {
    type: 'fatal_error',
    error: fatal,
  });
  assert.equal(failedPermanently.status, 'error');
  assert.equal(failedPermanently.context, CONTEXT_EPOCH_1);
  assert.equal(failedPermanently.error, fatal);

  const source = fs.readFileSync(bindingStateModule, 'utf8');
  assert.equal(source.includes('probe_started'), false);
  console.log('binding state tests passed');
}

main();
