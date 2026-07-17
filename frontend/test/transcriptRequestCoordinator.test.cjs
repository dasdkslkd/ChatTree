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
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  createTranscriptRequestCoordinator,
} = require(path.join(__dirname, '../src/services/transcriptRequestCoordinator.ts'));

function collectAstNodes(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}

function testMainPageWiresCoordinatorRequestAndCleanup() {
  const filename = path.join(__dirname, '../src/pages/MainPage.tsx');
  const source = fs.readFileSync(filename, 'utf8');
  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );

  const imports = collectAstNodes(sourceFile, (node) => (
    ts.isImportDeclaration(node)
    && ts.isStringLiteral(node.moduleSpecifier)
    && node.moduleSpecifier.text === '../services/transcriptRequestCoordinator'
  ));
  assert.equal(imports.length, 1, 'MainPage must import the transcript coordinator once');

  const calls = collectAstNodes(sourceFile, ts.isCallExpression);
  const createCalls = calls.filter((call) => (
    ts.isIdentifier(call.expression)
    && call.expression.text === 'createTranscriptRequestCoordinator'
  ));
  assert.equal(createCalls.length, 1, 'MainPage must create one transcript coordinator');

  const options = createCalls[0].arguments[0];
  assert.ok(options && ts.isObjectLiteralExpression(options));
  const optionNames = new Set(options.properties.flatMap((property) => {
    if (!('name' in property) || !property.name) return [];
    return [property.name.getText(sourceFile)];
  }));
  assert.deepEqual(
    [...optionNames].sort(),
    ['fetchSnapshot', 'getVisibleTarget', 'onErrorChange', 'onLoadingChange', 'onSnapshot'],
  );

  for (const method of ['request', 'cancelActive']) {
    assert.ok(
      calls.some((call) => (
        ts.isPropertyAccessExpression(call.expression)
        && call.expression.name.text === method
      )),
      `MainPage must call coordinator.${method}()`,
    );
  }
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

function makeTarget(conversationId, tipNodeId) {
  return { conversationId, tipNodeId };
}

function makeSnapshot(target, itemId = 'item-1') {
  return {
    conversation_id: target.conversationId,
    tip_node_id: target.tipNodeId,
    revision: 1,
    items: [{ id: itemId, type: 'user_message' }],
  };
}

function createEpochSource() {
  const token = Object.freeze({
    profileId: 'profile-a',
    serverInstanceId: '11111111-1111-4111-8111-111111111111',
    connectionEpoch: 1,
    connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    generation: 1,
  });
  const controller = new AbortController();
  let current = true;
  let captures = 0;
  return {
    source: {
      capture() {
        captures += 1;
        return token;
      },
      isCurrent(candidate) {
        return current && candidate === token;
      },
      signalFor(candidate) {
        return current && candidate === token ? controller.signal : AbortSignal.abort();
      },
    },
    invalidate() {
      current = false;
      controller.abort();
    },
    get captures() {
      return captures;
    },
  };
}

function createHarness(
  initialTarget = makeTarget('conversation-a', 'node-a'),
  epochSource,
) {
  let visibleTarget = initialTarget;
  const requests = [];
  const loading = [];
  const snapshots = [];
  const errors = [];

  const coordinator = createTranscriptRequestCoordinator({
    fetchSnapshot(conversationId, tipNodeId, signal) {
      const result = deferred();
      requests.push({ conversationId, tipNodeId, signal, ...result });
      return result.promise;
    },
    getVisibleTarget: () => visibleTarget,
    onLoadingChange: (value) => loading.push(value),
    onSnapshot: (snapshot) => snapshots.push(snapshot),
    onErrorChange: (error) => errors.push(error),
    epochSource: epochSource ?? createEpochSource().source,
  });

  return {
    coordinator,
    requests,
    loading,
    snapshots,
    errors,
    setVisibleTarget(target) {
      visibleTarget = target;
    },
  };
}

async function testEpochInvalidationAbortsTransportAndSuppressesEveryLateCallback() {
  const epoch = createEpochSource();
  const harness = createHarness(makeTarget('conversation-a', 'node-a'), epoch.source);
  const target = makeTarget('conversation-a', 'node-a');

  const first = harness.coordinator.request(target);
  await flushFetch();
  assert.equal(epoch.captures, 1);
  assert.equal(harness.requests[0].signal.aborted, false);
  assert.deepEqual(harness.loading, [true]);

  epoch.invalidate();
  assert.equal(harness.requests[0].signal.aborted, true);
  const shared = harness.coordinator.request(target);
  assert.equal(shared, first);
  assert.deepEqual(harness.loading, [true]);

  harness.requests[0].resolve(makeSnapshot(target, 'late-epoch-a'));
  await first;
  assert.deepEqual(harness.snapshots, []);
  assert.deepEqual(harness.errors.filter(Boolean), []);
  assert.deepEqual(harness.loading, [true]);
  assert.equal(epoch.captures, 1);
}

async function flushFetch() {
  await Promise.resolve();
}

async function testNewTargetAbortsOldAndLateSettlementCannotCommitOrClearNewLoading() {
  const harness = createHarness();
  const targetA = makeTarget('conversation-a', 'node-a');
  const targetB = makeTarget('conversation-b', 'node-b');

  const first = harness.coordinator.request(targetA);
  await flushFetch();
  harness.setVisibleTarget(targetB);
  const second = harness.coordinator.request(targetB);
  await flushFetch();

  assert.equal(harness.requests[0].signal.aborted, true);
  harness.requests[0].resolve(makeSnapshot(targetA, 'late-a'));
  await first;
  assert.deepEqual(harness.snapshots, []);
  assert.equal(harness.loading.at(-1), true, 'old finally must not clear the new request loading state');

  harness.requests[1].resolve(makeSnapshot(targetB, 'current-b'));
  await second;
  assert.deepEqual(harness.snapshots.map((snapshot) => snapshot.items[0].id), ['current-b']);
  assert.equal(harness.loading.at(-1), false);
}

async function testCancellationErrorsAreSilentAndCancelDoesNotDisposeCoordinator() {
  const harness = createHarness();
  const target = makeTarget('conversation-a', 'node-a');

  const abortErrorRequest = harness.coordinator.request(target);
  await flushFetch();
  harness.requests[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  await abortErrorRequest;

  const axiosCancelRequest = harness.coordinator.request(target);
  await flushFetch();
  harness.requests[1].reject(Object.assign(new Error('cancelled'), { code: 'ERR_CANCELED' }));
  await axiosCancelRequest;

  const cancelledRequest = harness.coordinator.request(target);
  await flushFetch();
  harness.coordinator.cancelActive();
  assert.equal(harness.requests[2].signal.aborted, true);
  harness.requests[2].reject(new Error('transport rejected after abort'));
  await cancelledRequest;

  const afterCancel = harness.coordinator.request(target);
  await flushFetch();
  harness.requests[3].resolve(makeSnapshot(target, 'after-cancel'));
  await afterCancel;

  assert.deepEqual(harness.errors.filter(Boolean), []);
  assert.deepEqual(harness.snapshots.map((snapshot) => snapshot.items[0].id), ['after-cancel']);
}

async function testErrorsAreReportedOnlyForTheCurrentlyVisibleBranch() {
  const harness = createHarness();
  const targetA = makeTarget('conversation-a', 'node-a');
  const targetB = makeTarget('conversation-b', 'node-b');

  const staleRequest = harness.coordinator.request(targetA);
  await flushFetch();
  harness.setVisibleTarget(targetB);
  harness.requests[0].reject(new Error('stale failure'));
  await staleRequest;
  assert.deepEqual(harness.errors.filter(Boolean), []);

  const currentRequest = harness.coordinator.request(targetB);
  await flushFetch();
  harness.requests[1].reject(new Error('current failure'));
  await currentRequest;
  assert.deepEqual(harness.errors.filter(Boolean).map((error) => error.message), ['current failure']);
}

async function testMismatchedSnapshotIdentityNeverCommits() {
  const harness = createHarness();
  const target = makeTarget('conversation-a', 'node-a');

  const request = harness.coordinator.request(target);
  await flushFetch();
  harness.requests[0].resolve({
    ...makeSnapshot(target, 'wrong-node'),
    tip_node_id: 'node-other',
  });
  await request;

  assert.deepEqual(harness.snapshots, []);
  assert.equal(harness.loading.at(-1), false);
}

async function testSameTargetSharesOneInFlightRequest() {
  const harness = createHarness();
  const target = makeTarget('conversation-a', 'node-a');

  const first = harness.coordinator.request(target);
  const second = harness.coordinator.request({ ...target });
  assert.equal(second, first);
  assert.deepEqual(harness.loading, [true, true]);
  await flushFetch();
  assert.equal(harness.requests.length, 1);

  harness.requests[0].resolve(makeSnapshot(target, 'single-flight'));
  await Promise.all([first, second]);
  assert.deepEqual(harness.snapshots.map((snapshot) => snapshot.items[0].id), ['single-flight']);
}

async function testEverySequentialCallbackRechecksEpochOwnership() {
  const target = makeTarget('conversation-a', 'node-a');

  {
    const epoch = createEpochSource();
    const calls = [];
    const coordinator = createTranscriptRequestCoordinator({
      fetchSnapshot: async () => makeSnapshot(target),
      getVisibleTarget: () => target,
      onLoadingChange: (loading) => {
        calls.push(`loading:${loading}`);
        if (loading) epoch.invalidate();
      },
      onSnapshot: () => calls.push('snapshot'),
      onErrorChange: () => calls.push('error'),
      epochSource: epoch.source,
    });

    await coordinator.request(target);
    assert.deepEqual(calls, ['loading:true']);
  }

  {
    const epoch = createEpochSource();
    const result = deferred();
    const calls = [];
    const coordinator = createTranscriptRequestCoordinator({
      fetchSnapshot: () => result.promise,
      getVisibleTarget: () => target,
      onLoadingChange: (loading) => calls.push(`loading:${loading}`),
      onSnapshot: () => {
        calls.push('snapshot');
        epoch.invalidate();
      },
      onErrorChange: () => calls.push('error'),
      epochSource: epoch.source,
    });

    const pending = coordinator.request(target);
    await flushFetch();
    result.resolve(makeSnapshot(target));
    await pending;
    assert.deepEqual(calls, ['loading:true', 'error', 'snapshot']);
  }
}

async function main() {
  testMainPageWiresCoordinatorRequestAndCleanup();
  await testNewTargetAbortsOldAndLateSettlementCannotCommitOrClearNewLoading();
  await testCancellationErrorsAreSilentAndCancelDoesNotDisposeCoordinator();
  await testErrorsAreReportedOnlyForTheCurrentlyVisibleBranch();
  await testMismatchedSnapshotIdentityNeverCommits();
  await testSameTargetSharesOneInFlightRequest();
  await testEverySequentialCallbackRechecksEpochOwnership();
  await testEpochInvalidationAbortsTransportAndSuppressesEveryLateCallback();
  console.log('transcriptRequestCoordinator tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
