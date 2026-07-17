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

const {
  ACTIVE_STREAM_RECOVERY_HINTED_ATTEMPTS,
  ACTIVE_STREAM_RECOVERY_IDLE_ATTEMPTS,
  ActiveStreamRecoveryCoordinator,
  getActiveStreamRecoveryAttemptLimit,
} = require(path.join(__dirname, '../src/services/activeStreamRecoveryCoordinator.ts'));

function stream(overrides = {}) {
  return {
    conversation_id: 'conv-1',
    node_id: 'node-1',
    run_id: 'run-1',
    anchor_node_id: null,
    kind: 'chat',
    event_count: 1,
    done: false,
    created_at: 1,
    updated_at: 1,
    ...overrides,
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

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function testNegativeProbeDoesNotPrepareOrResume() {
  const calls = [];
  const coordinator = new ActiveStreamRecoveryCoordinator({
    getActiveStreams: async () => {
      calls.push('active');
      return [];
    },
    prepareAttach: async () => {
      calls.push('prepare');
    },
    resumeStream: () => {
      calls.push('resume');
    },
    delay: async () => {},
  }, createEpochSource().source);

  const result = await coordinator.probeConversation('conv-1', {
    reason: 'backend-followup',
    attempts: 3,
    intervalMs: 0,
  });

  assert.equal(result.status, 'none');
  assert.equal(result.attempts, 3);
  assert.deepEqual(calls, ['active', 'active', 'active']);
}

async function testFollowupAttachesAndSchedulesOnce() {
  const calls = [];
  let reads = 0;
  const coordinator = new ActiveStreamRecoveryCoordinator({
    getActiveStreams: async () => {
      reads += 1;
      calls.push('active');
      return reads === 2 ? [stream()] : [];
    },
    prepareAttach: async (_conversationId, active, reason) => {
      calls.push(`prepare:${active.node_id}:${reason}`);
    },
    resumeStream: (_conversationId, active, reason) => {
      calls.push(`resume:${active.run_id}:${reason}`);
    },
    delay: async () => {},
  }, createEpochSource().source);

  const result = await coordinator.probeConversation('conv-1', {
    reason: 'backend-followup',
    attempts: 3,
    intervalMs: 0,
  });

  assert.equal(result.status, 'attached');
  assert.equal(result.attempts, 2);
  assert.equal(result.attachable.length, 1);
  assert.deepEqual(calls, [
    'active',
    'active',
    'prepare:node-1:backend-followup',
    'resume:run-1:backend-followup',
  ]);
}

async function testDuplicateProbesCoalesceAndUpgradeAttempts() {
  let reads = 0;
  const coordinator = new ActiveStreamRecoveryCoordinator({
    getActiveStreams: async () => {
      reads += 1;
      await new Promise((resolve) => setTimeout(resolve, 0));
      return [];
    },
    delay: async () => {},
  }, createEpochSource().source);

  const first = coordinator.probeConversation('conv-1', {
    reason: 'active-stream-recovery',
    attempts: 1,
    intervalMs: 0,
  });
  const second = coordinator.probeConversation('conv-1', {
    reason: 'backend-followup',
    attempts: 3,
    intervalMs: 0,
  });

  const [firstResult, secondResult] = await Promise.all([first, second]);

  assert.equal(firstResult.status, 'none');
  assert.equal(secondResult.status, 'none');
  assert.equal(reads, 3);
}

async function testInvalidationStopsAttachAndSettlesCoalescedWaitersWithoutRecapture() {
  const calls = [];
  const blocked = deferred();
  const epoch = createEpochSource();
  const coordinator = new ActiveStreamRecoveryCoordinator({
    getActiveStreams: async () => {
      calls.push('active');
      await blocked.promise;
      return [stream()];
    },
    prepareAttach: async () => {
      calls.push('prepare');
    },
    resumeStream: () => {
      calls.push('resume');
    },
  }, epoch.source);

  const first = coordinator.probeConversation('conv-1', { attempts: 2 });
  await new Promise((resolve) => setImmediate(resolve));
  epoch.invalidate();
  const second = coordinator.probeConversation('conv-1', { attempts: 3 });
  blocked.resolve();

  const [firstResult, secondResult] = await Promise.all([first, second]);
  for (const result of [firstResult, secondResult]) {
    assert.equal(result.status, 'paused');
    assert.deepEqual(result.attachable, []);
  }
  assert.deepEqual(calls, ['active']);
  assert.equal(epoch.captures, 1);
}

async function testInvalidationAfterProbeDowngradesAttachedResult() {
  const epoch = createEpochSource();
  const coordinator = new ActiveStreamRecoveryCoordinator({
    getActiveStreams: async () => [stream()],
    resumeStream: () => {
      queueMicrotask(() => epoch.invalidate());
    },
  }, epoch.source);

  const result = await coordinator.probeConversation('conv-1');
  assert.equal(result.status, 'paused');
  assert.equal(result.attempts, 1);
  assert.deepEqual(result.attachable, []);
}

async function testThrownHandlersSettleWaitersAndReleaseProbeState() {
  for (const failurePoint of ['isAttachable', 'resumeStream', 'delay']) {
    const boom = new Error(`${failurePoint} failed`);
    let fail = true;
    const handlers = {
      getActiveStreams: async () => (
        failurePoint === 'delay' ? [] : [stream()]
      ),
      isAttachable: () => {
        if (fail && failurePoint === 'isAttachable') throw boom;
        return true;
      },
      resumeStream: () => {
        if (fail && failurePoint === 'resumeStream') throw boom;
      },
      delay: async () => {
        if (fail && failurePoint === 'delay') throw boom;
      },
    };
    const coordinator = new ActiveStreamRecoveryCoordinator(
      handlers,
      createEpochSource().source,
    );

    const first = coordinator.probeConversation('conv-1', { attempts: 2 });
    const coalesced = coordinator.probeConversation('conv-1', { attempts: 2 });
    const failed = await Promise.all([first, coalesced]);
    for (const result of failed) {
      assert.equal(result.status, 'error');
      assert.equal(result.error, boom);
    }

    fail = false;
    const retried = await coordinator.probeConversation('conv-1', { attempts: 1 });
    assert.equal(
      retried.status,
      failurePoint === 'delay' ? 'none' : 'attached',
      `${failurePoint} must not leave a stuck state`,
    );
  }
}

function testAttemptLimitUsesHintOnly() {
  assert.equal(ACTIVE_STREAM_RECOVERY_IDLE_ATTEMPTS, 3);
  assert.equal(ACTIVE_STREAM_RECOVERY_HINTED_ATTEMPTS, 10);
  assert.equal(
    getActiveStreamRecoveryAttemptLimit({ activeStreamHintCount: 0 }),
    ACTIVE_STREAM_RECOVERY_IDLE_ATTEMPTS,
  );
  assert.equal(
    getActiveStreamRecoveryAttemptLimit({ activeStreamHintCount: 2 }),
    ACTIVE_STREAM_RECOVERY_HINTED_ATTEMPTS,
  );
}

(async () => {
  testAttemptLimitUsesHintOnly();
  await testNegativeProbeDoesNotPrepareOrResume();
  await testFollowupAttachesAndSchedulesOnce();
  await testDuplicateProbesCoalesceAndUpgradeAttempts();
  await testInvalidationStopsAttachAndSettlesCoalescedWaitersWithoutRecapture();
  await testInvalidationAfterProbeDowngradesAttachedResult();
  await testThrownHandlersSettleWaitersAndReleaseProbeState();
  console.log('active stream recovery coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
