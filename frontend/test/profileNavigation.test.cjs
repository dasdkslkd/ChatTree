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
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  BoundRouteRestorer,
  bindBoundFrontendPopstate,
  createBoundFrontendNavigator,
  readFrontendRouteLocation,
  restoreBoundFrontendRoute,
  waitForRouteReadiness,
  waitForRouteRender,
} = require('../src/runtime/profileNavigation.ts');

const LEASE_A_TOKEN = Object.freeze({
  profileId: 'profile-a',
  serverInstanceId: 'server-a',
  connectionEpoch: 7,
  connectionLeaseId: 'lease-a',
  generation: 3,
});

const RUN_RECORD = Object.freeze({
  run_id: 'run/1',
  conversation_id: 'conv-1',
  status: 'completed',
  event_count: 2,
});

const RUN_EVENTS = Object.freeze([
  Object.freeze({ event_index: 0, event: 'run_started', data: {} }),
]);

function assertLeaseToken(token) {
  assert.strictEqual(token, LEASE_A_TOKEN);
}

function makeLocation(href, overrides = {}) {
  const url = new URL(href);
  return {
    href,
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    ...overrides,
  };
}

function createEpochGuard() {
  let current = true;
  let assertionCount = 0;
  return {
    guard: {
      assertCurrent(token) {
        assertLeaseToken(token);
        assertionCount += 1;
        if (!current) throw new Error('stale route token');
      },
    },
    invalidate() {
      current = false;
    },
    get assertionCount() {
      return assertionCount;
    },
  };
}

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createActions(buildOverrides) {
  const calls = [];
  const actions = {
    boundProfileId: 'profile-a',
    async selectConversation(id, token) {
      assertLeaseToken(token);
      calls.push(['selectConversation', id]);
      return true;
    },
    async switchNode(id, token) {
      assertLeaseToken(token);
      calls.push(['switchNode', id]);
      return true;
    },
    async getRun(id, token) {
      assertLeaseToken(token);
      calls.push(['getRun', id]);
      return RUN_RECORD;
    },
    async getEvents(id, fromEvent, token) {
      assertLeaseToken(token);
      calls.push(['getEvents', id, fromEvent]);
      return RUN_EVENTS;
    },
    restoreAndAttachRun(run, events, token) {
      assertLeaseToken(token);
      assert.strictEqual(run, RUN_RECORD);
      assert.strictEqual(events, RUN_EVENTS);
      calls.push(['restoreAndAttachRun', run.run_id]);
    },
    applyRestoredRoute(route, result, token) {
      assertLeaseToken(token);
      calls.push(['applyRestoredRoute', route.kind, result]);
    },
  };
  if (buildOverrides) Object.assign(actions, buildOverrides(calls));
  return { actions, calls };
}

function testCanonicalLocationReader() {
  const cases = [
    [
      'http://127.0.0.1:5173/s/profile-a',
      { kind: 'profile', profileId: 'profile-a' },
    ],
    [
      'https://launcher.example/s/profile%20a/c/conv%2F1',
      { kind: 'conversation', profileId: 'profile a', conversationId: 'conv/1' },
    ],
    [
      'http://127.0.0.1:5173/s/profile-a/c/conv-1/n/%E4%BC%9A%E8%AF%9D',
      {
        kind: 'node',
        profileId: 'profile-a',
        conversationId: 'conv-1',
        nodeId: '\u4f1a\u8bdd',
      },
    ],
    [
      'http://127.0.0.1:5173/s/profile-a/r/run%2F1',
      { kind: 'run', profileId: 'profile-a', runId: 'run/1' },
    ],
  ];

  for (const [href, expected] of cases) {
    assert.deepEqual(readFrontendRouteLocation(makeLocation(href)), expected);
  }

  const canonical = 'http://127.0.0.1:5173/s/profile-a';
  for (const suffix of ['?', '#', '?x=1', '#x']) {
    const href = `${canonical}${suffix}`;
    assert.throws(
      () => readFrontendRouteLocation(makeLocation(href)),
      /query or hash/,
      href,
    );
  }

  assert.throws(() => readFrontendRouteLocation(makeLocation(canonical, {
    pathname: '/s/profile-b',
  })), /canonical pathname/);
}

function testBoundNavigator() {
  const calls = [];
  const history = {
    pushState(...args) {
      calls.push(['pushState', ...args]);
    },
    replaceState(...args) {
      calls.push(['replaceState', ...args]);
    },
  };
  const navigate = createBoundFrontendNavigator('profile a', history);

  navigate({
    kind: 'conversation', profileId: 'profile a', conversationId: 'conv/1',
  }, 'push');
  navigate({
    kind: 'run', profileId: 'profile a', runId: 'run 1',
  }, 'replace');

  assert.deepEqual(calls, [
    ['pushState', null, '', '/s/profile%20a/c/conv%2F1'],
    ['replaceState', null, '', '/s/profile%20a/r/run%201'],
  ]);

  assert.throws(() => navigate({
    kind: 'profile', profileId: 'profile-b',
  }, 'push'), /does not match bound Profile/);
  assert.equal(calls.length, 2, 'foreign Profile must fail before history mutation');
}

function createPopstateTarget(initialHref) {
  let location = makeLocation(initialHref);
  const listeners = new Set();
  const target = {
    get location() {
      return location;
    },
    addEventListener(type, listener) {
      assert.equal(type, 'popstate');
      listeners.add(listener);
    },
    removeEventListener(type, listener) {
      assert.equal(type, 'popstate');
      listeners.delete(listener);
    },
  };
  return {
    target,
    setHref(href) {
      location = makeLocation(href);
    },
    dispatch() {
      for (const listener of [...listeners]) listener();
    },
    get listenerCount() {
      return listeners.size;
    },
  };
}

async function testPopstateBindingRejectsForeignBeforeSubmitAndUnbinds() {
  const fixture = createPopstateTarget('http://127.0.0.1:5173/s/profile-a');
  const submissions = [];
  const committed = [];
  const errors = [];
  const restorer = {
    async submit(route, options) {
      assert.equal(route.profileId, 'profile-a', 'foreign route reached owner');
      submissions.push(route);
      const result = { conversationId: null, nodeId: null, runId: null };
      await options?.afterRestore?.(route, result, LEASE_A_TOKEN, submissions.length);
      return result;
    },
  };

  const unbind = bindBoundFrontendPopstate(
    fixture.target,
    'profile-a',
    restorer,
    (error) => errors.push(error),
    (route) => committed.push(route),
  );
  assert.equal(fixture.listenerCount, 1);

  fixture.dispatch();
  await Promise.resolve();
  assert.deepEqual(submissions, [{ kind: 'profile', profileId: 'profile-a' }]);
  assert.deepEqual(committed, submissions);
  assert.deepEqual(errors, []);

  fixture.setHref('http://127.0.0.1:5173/s/profile-b/c/conv-1');
  fixture.dispatch();
  await Promise.resolve();
  assert.equal(submissions.length, 1);
  assert.equal(errors.length, 1);
  assert.match(String(errors[0]), /does not match bound Profile/);

  fixture.setHref('http://127.0.0.1:5173/s/profile-a?');
  fixture.dispatch();
  await Promise.resolve();
  assert.equal(submissions.length, 1);
  assert.equal(errors.length, 2);
  assert.match(String(errors[1]), /query or hash/);

  unbind();
  assert.equal(fixture.listenerCount, 0);
  fixture.setHref('http://127.0.0.1:5173/s/profile-a/c/conv-2');
  fixture.dispatch();
  await Promise.resolve();
  assert.equal(submissions.length, 1, 'unmounted listener must not submit');
}

async function testNodeAndRunRestorationUseExactToken() {
  const nodeFixture = createActions();
  const nodeGuard = createEpochGuard();
  const nodeRoute = {
    kind: 'node', profileId: 'profile-a', conversationId: 'conv-1', nodeId: 'node/2',
  };
  const nodeResult = await restoreBoundFrontendRoute(
    nodeRoute,
    nodeFixture.actions,
    LEASE_A_TOKEN,
    nodeGuard.guard,
  );
  assert.deepEqual(nodeResult, {
    conversationId: 'conv-1', nodeId: 'node/2', runId: null,
  });
  assert.deepEqual(nodeFixture.calls, [
    ['selectConversation', 'conv-1'],
    ['switchNode', 'node/2'],
    ['applyRestoredRoute', 'node', nodeResult],
  ]);

  const runFixture = createActions();
  const runGuard = createEpochGuard();
  const runRoute = { kind: 'run', profileId: 'profile-a', runId: 'run/1' };
  const runResult = await restoreBoundFrontendRoute(
    runRoute,
    runFixture.actions,
    LEASE_A_TOKEN,
    runGuard.guard,
  );
  assert.deepEqual(runResult, {
    conversationId: 'conv-1', nodeId: null, runId: 'run/1',
  });
  assert.deepEqual(runFixture.calls, [
    ['getRun', 'run/1'],
    ['selectConversation', 'conv-1'],
    ['getEvents', 'run/1', 0],
    ['restoreAndAttachRun', 'run/1'],
    ['applyRestoredRoute', 'run', runResult],
  ]);
  assert.ok(nodeGuard.assertionCount > 0);
  assert.ok(runGuard.assertionCount > 0);
}

async function testBooleanPostconditionsStopRestoration() {
  const selectionFixture = createActions((calls) => ({
    async selectConversation(id, token) {
      assertLeaseToken(token);
      calls.push(['selectConversation', id]);
      return false;
    },
  }));
  await assert.rejects(() => restoreBoundFrontendRoute(
    { kind: 'node', profileId: 'profile-a', conversationId: 'missing', nodeId: 'node-1' },
    selectionFixture.actions,
    LEASE_A_TOKEN,
    createEpochGuard().guard,
  ), /conversation selection failed/);
  assert.deepEqual(selectionFixture.calls, [['selectConversation', 'missing']]);

  const nodeFixture = createActions((calls) => ({
    async switchNode(id, token) {
      assertLeaseToken(token);
      calls.push(['switchNode', id]);
      return false;
    },
  }));
  await assert.rejects(() => restoreBoundFrontendRoute(
    { kind: 'node', profileId: 'profile-a', conversationId: 'conv-1', nodeId: 'missing' },
    nodeFixture.actions,
    LEASE_A_TOKEN,
    createEpochGuard().guard,
  ), /node selection failed/);
  assert.deepEqual(nodeFixture.calls, [
    ['selectConversation', 'conv-1'],
    ['switchNode', 'missing'],
  ]);
}

function createInvalidatingRunActions(invalidateAfter, epoch) {
  const calls = [];
  const finish = (name, value) => {
    if (name === invalidateAfter) epoch.invalidate();
    return value;
  };
  return {
    calls,
    actions: {
      boundProfileId: 'profile-a',
      async getRun(id, token) {
        assertLeaseToken(token);
        calls.push('getRun');
        return finish('getRun', RUN_RECORD);
      },
      async selectConversation(id, token) {
        assertLeaseToken(token);
        assert.equal(id, 'conv-1');
        calls.push('selectConversation');
        return finish('selectConversation', true);
      },
      async getEvents(id, fromEvent, token) {
        assertLeaseToken(token);
        assert.equal(id, 'run/1');
        assert.equal(fromEvent, 0);
        calls.push('getEvents');
        return finish('getEvents', RUN_EVENTS);
      },
      restoreAndAttachRun(run, events, token) {
        assertLeaseToken(token);
        assert.strictEqual(run, RUN_RECORD);
        assert.strictEqual(events, RUN_EVENTS);
        calls.push('restoreAndAttachRun');
        finish('restoreAndAttachRun');
      },
      async switchNode() {
        throw new Error('run restoration must not switch nodes');
      },
      applyRestoredRoute() {
        calls.push('applyRestoredRoute');
      },
    },
  };
}

async function testStaleTokenStopsAtEveryAdjacentRunBoundary() {
  const actionOrder = [
    'getRun',
    'selectConversation',
    'getEvents',
    'restoreAndAttachRun',
  ];
  for (let index = 0; index < actionOrder.length; index += 1) {
    const epoch = createEpochGuard();
    const fixture = createInvalidatingRunActions(actionOrder[index], epoch);
    await assert.rejects(() => restoreBoundFrontendRoute(
      { kind: 'run', profileId: 'profile-a', runId: 'run/1' },
      fixture.actions,
      LEASE_A_TOKEN,
      epoch.guard,
    ), /stale route token/);
    assert.deepEqual(
      fixture.calls,
      actionOrder.slice(0, index + 1),
      `stale boundary after ${actionOrder[index]} ran a later action`,
    );
  }
}

async function testInitiallyStaleAndQueuedRoutesRunNoActions() {
  const initiallyStale = createEpochGuard();
  const initialFixture = createActions();
  initiallyStale.invalidate();
  await assert.rejects(() => restoreBoundFrontendRoute(
    { kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-1' },
    initialFixture.actions,
    LEASE_A_TOKEN,
    initiallyStale.guard,
  ), /stale route token/);
  assert.deepEqual(initialFixture.calls, []);

  const epoch = createEpochGuard();
  const releaseFirst = createDeferred();
  const calls = [];
  const actions = createActions(() => ({
    async selectConversation(id, token) {
      assertLeaseToken(token);
      calls.push(['select:start', id]);
      if (id === 'conv-a') await releaseFirst.promise;
      calls.push(['select:end', id]);
      return true;
    },
    applyRestoredRoute(route) {
      calls.push(['apply', route.conversationId]);
    },
  })).actions;
  const restorer = new BoundRouteRestorer(actions, LEASE_A_TOKEN, epoch.guard);
  const first = restorer.submit({
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-a',
  });
  const queued = restorer.submit({
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-b',
  });
  await Promise.resolve();
  await Promise.resolve();
  epoch.invalidate();
  releaseFirst.resolve();
  await assert.rejects(first, /stale route token/);
  await assert.rejects(queued, /stale route token/);
  assert.deepEqual(calls, [
    ['select:start', 'conv-a'],
    ['select:end', 'conv-a'],
  ]);
}

async function testBoundRouteRestorerSerializesCompleteRequests() {
  const releaseFirst = createDeferred();
  const calls = [];
  let finalConversationId = null;
  let finalLocation = null;
  const actions = createActions(() => ({
    async selectConversation(id, token) {
      assertLeaseToken(token);
      calls.push(['select:start', id]);
      if (id === 'conv-a') await releaseFirst.promise;
      calls.push(['select:end', id]);
      return true;
    },
    applyRestoredRoute(route, result, token) {
      assertLeaseToken(token);
      calls.push(['apply', route.conversationId]);
      finalConversationId = result.conversationId;
    },
  })).actions;
  const restorer = new BoundRouteRestorer(actions, LEASE_A_TOKEN, createEpochGuard().guard);

  const firstRoute = {
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-a',
  };
  const secondRoute = {
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-b',
  };
  const first = restorer.submit(firstRoute, {
    afterRestore: () => {
      finalLocation = firstRoute.conversationId;
      calls.push(['history', firstRoute.conversationId]);
    },
  });
  const second = restorer.submit(secondRoute, {
    afterRestore: () => {
      finalLocation = secondRoute.conversationId;
      calls.push(['history', secondRoute.conversationId]);
    },
  });

  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(calls, [['select:start', 'conv-a']]);
  releaseFirst.resolve();
  const [firstResult, secondResult] = await Promise.all([first, second]);

  assert.deepEqual(firstResult, {
    conversationId: 'conv-a', nodeId: null, runId: null,
  });
  assert.deepEqual(secondResult, {
    conversationId: 'conv-b', nodeId: null, runId: null,
  });
  assert.deepEqual(calls, [
    ['select:start', 'conv-a'],
    ['select:end', 'conv-a'],
    ['apply', 'conv-a'],
    ['history', 'conv-a'],
    ['select:start', 'conv-b'],
    ['select:end', 'conv-b'],
    ['apply', 'conv-b'],
    ['history', 'conv-b'],
  ]);
  assert.equal(finalConversationId, 'conv-b');
  assert.equal(finalLocation, 'conv-b');
}

async function testBoundRouteRestorerFailureDoesNotPoisonTail() {
  const calls = [];
  const actions = createActions(() => ({
    async selectConversation(id, token) {
      assertLeaseToken(token);
      calls.push(['selectConversation', id]);
      return id !== 'bad';
    },
    applyRestoredRoute(route, result, token) {
      assertLeaseToken(token);
      calls.push(['applyRestoredRoute', route.conversationId]);
      assert.equal(result.conversationId, route.conversationId);
    },
  })).actions;
  const restorer = new BoundRouteRestorer(actions, LEASE_A_TOKEN, createEpochGuard().guard);

  const failed = restorer.submit({
    kind: 'conversation', profileId: 'profile-a', conversationId: 'bad',
  });
  const succeeded = restorer.submit({
    kind: 'conversation', profileId: 'profile-a', conversationId: 'good',
  });

  await assert.rejects(failed, /conversation selection failed/);
  assert.deepEqual(await succeeded, {
    conversationId: 'good', nodeId: null, runId: null,
  });
  assert.deepEqual(calls, [
    ['selectConversation', 'bad'],
    ['selectConversation', 'good'],
    ['applyRestoredRoute', 'good'],
  ]);
}

async function testPreparedRouteAndUserIntentShareOneOwner() {
  const calls = [];
  let route = { kind: 'profile', profileId: 'profile-a' };
  const actions = createActions(() => ({
    async selectConversation(id) {
      calls.push(['select', id]);
      return true;
    },
    applyRestoredRoute(restoredRoute) {
      calls.push(['apply', restoredRoute.kind]);
    },
  })).actions;
  const restorer = new BoundRouteRestorer(actions, LEASE_A_TOKEN, createEpochGuard().guard);
  const initial = restorer.submit(
    () => route,
    {
      prepare(token, intent) {
        assertLeaseToken(token);
        calls.push(['prepare', intent]);
        route = {
          kind: 'conversation',
          profileId: 'profile-a',
          conversationId: 'conv-prepared',
        };
      },
      afterRestore(_restoredRoute, result, token, intent) {
        assertLeaseToken(token);
        calls.push(['history', result.conversationId, intent]);
      },
    },
  );
  const user = restorer.run((token, intent) => {
    assertLeaseToken(token);
    calls.push(['user', intent]);
  });

  await Promise.all([initial, user]);
  assert.deepEqual(calls, [
    ['prepare', 1],
    ['select', 'conv-prepared'],
    ['apply', 'conversation'],
    ['history', 'conv-prepared', 1],
    ['user', 2],
  ]);
}

async function testDisposedOwnerRejectsInflightAndQueuedUIWork() {
  const release = createDeferred();
  const calls = [];
  const actions = createActions(() => ({
    async selectConversation(id) {
      calls.push(['select:start', id]);
      await release.promise;
      calls.push(['select:end', id]);
      return true;
    },
    applyRestoredRoute(route) {
      calls.push(['apply', route.conversationId]);
    },
  })).actions;
  const restorer = new BoundRouteRestorer(actions, LEASE_A_TOKEN, createEpochGuard().guard);
  const inflight = restorer.submit({
    kind: 'conversation', profileId: 'profile-a', conversationId: 'conv-a',
  });
  const queued = restorer.run(() => calls.push(['queued-ui']));
  await Promise.resolve();
  await Promise.resolve();
  restorer.dispose();
  release.resolve();

  await assert.rejects(inflight, /route owner is disposed/i);
  await assert.rejects(queued, /route owner is disposed/i);
  assert.deepEqual(calls, [
    ['select:start', 'conv-a'],
    ['select:end', 'conv-a'],
  ]);
}

async function testRouteReadinessIsBoundedAndLeaseGuarded() {
  const readyEpoch = createEpochGuard();
  let cancelled = false;
  assert.equal(await waitForRouteReadiness(
    LEASE_A_TOKEN,
    async () => {},
    { timeoutMs: 50, cancel: () => { cancelled = true; } },
    readyEpoch.guard,
  ), true);
  assert.equal(cancelled, false);

  const never = createDeferred();
  assert.equal(await waitForRouteReadiness(
    LEASE_A_TOKEN,
    () => never.promise,
    { timeoutMs: 5, cancel: () => { cancelled = true; } },
    readyEpoch.guard,
  ), false);
  assert.equal(cancelled, true);

  const staleEpoch = createEpochGuard();
  const delayed = createDeferred();
  const stale = waitForRouteReadiness(
    LEASE_A_TOKEN,
    () => delayed.promise,
    { timeoutMs: 50, cancel: () => {} },
    staleEpoch.guard,
  );
  staleEpoch.invalidate();
  delayed.resolve();
  await assert.rejects(stale, /stale route token/);
}

function createFrozenFrameScheduler() {
  let nextFrame = 0;
  const cancelledFrames = [];
  return {
    scheduler: {
      requestFrame() {
        nextFrame += 1;
        return nextFrame;
      },
      cancelFrame(handle) {
        cancelledFrames.push(handle);
      },
      setTimer(callback, delayMs) {
        return setTimeout(callback, delayMs);
      },
      clearTimer(handle) {
        clearTimeout(handle);
      },
    },
    cancelledFrames,
  };
}

async function testRouteRenderWaitUsesWallClockAndCancellationSignals() {
  const timeoutScheduler = createFrozenFrameScheduler();
  assert.equal(await waitForRouteRender(
    LEASE_A_TOKEN,
    () => false,
    { timeoutMs: 5, scheduler: timeoutScheduler.scheduler },
    createEpochGuard().guard,
  ), false, 'a frozen requestAnimationFrame must not block the route owner');
  assert.deepEqual(timeoutScheduler.cancelledFrames, [1]);

  const staleEpoch = createEpochGuard();
  const leaseController = new AbortController();
  const staleScheduler = createFrozenFrameScheduler();
  const stale = waitForRouteRender(
    LEASE_A_TOKEN,
    () => false,
    {
      timeoutMs: 1_000,
      signals: [leaseController.signal],
      scheduler: staleScheduler.scheduler,
    },
    staleEpoch.guard,
  );
  staleEpoch.invalidate();
  leaseController.abort();
  await assert.rejects(stale, /stale route token/);

  const restorer = new BoundRouteRestorer(
    createActions().actions,
    LEASE_A_TOKEN,
    createEpochGuard().guard,
  );
  const disposeScheduler = createFrozenFrameScheduler();
  const disposed = waitForRouteRender(
    LEASE_A_TOKEN,
    () => false,
    {
      timeoutMs: 1_000,
      signals: [restorer.signal],
      scheduler: disposeScheduler.scheduler,
    },
    createEpochGuard().guard,
  );
  restorer.dispose();
  await assert.rejects(disposed, /route render wait cancelled/i);
}

async function main() {
  testCanonicalLocationReader();
  testBoundNavigator();
  await testPopstateBindingRejectsForeignBeforeSubmitAndUnbinds();
  await testNodeAndRunRestorationUseExactToken();
  await testBooleanPostconditionsStopRestoration();
  await testStaleTokenStopsAtEveryAdjacentRunBoundary();
  await testInitiallyStaleAndQueuedRoutesRunNoActions();
  await testBoundRouteRestorerSerializesCompleteRequests();
  await testBoundRouteRestorerFailureDoesNotPoisonTail();
  await testPreparedRouteAndUserIntentShareOneOwner();
  await testDisposedOwnerRejectsInflightAndQueuedUIWork();
  await testRouteReadinessIsBoundedAndLeaseGuarded();
  await testRouteRenderWaitUsesWallClockAndCancellationSignals();
  console.log('profile navigation tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
