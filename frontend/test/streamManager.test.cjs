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

let nextTimerId = 1;
let timers = new Map();
let intervals = new Map();
let intervalDelays = new Map();

function resetTimers() {
  nextTimerId = 1;
  timers = new Map();
  intervals = new Map();
  intervalDelays = new Map();
}

function installWindowTimers() {
  global.window = {
    requestAnimationFrame(callback) {
      const id = nextTimerId++;
      timers.set(id, () => callback(Date.now()));
      return id;
    },
    cancelAnimationFrame(id) {
      timers.delete(id);
    },
    setTimeout(callback) {
      const id = nextTimerId++;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    setInterval(callback, delay) {
      const id = nextTimerId++;
      intervals.set(id, callback);
      intervalDelays.set(id, delay);
      return id;
    },
    clearInterval(id) {
      intervals.delete(id);
      intervalDelays.delete(id);
    },
  };
}

async function tick() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function flushTimersOnce() {
  const pending = [...timers.entries()];
  timers.clear();
  for (const [, callback] of pending) {
    callback();
  }
  await tick();
}

async function runTimersUntil(promise, maxSteps = 500) {
  let settled = false;
  promise.finally(() => {
    settled = true;
  });
  for (let step = 0; step < maxSteps && !settled; step += 1) {
    const pending = [...timers.entries()];
    timers.clear();
    for (const [, callback] of pending) {
      callback();
    }
    await tick();
  }
  if (!settled) {
    throw new Error('timed out waiting for stream to finish');
  }
  await promise;
}

function createControlledStream() {
  const queue = [];
  let wake = null;

  const stream = async function* stream() {
    while (true) {
      if (queue.length === 0) {
        await new Promise((resolve) => {
          wake = resolve;
        });
      }
      const item = queue.shift();
      if (!item) continue;
      if (item.done) return;
      yield item.chunk;
    }
  };

  async function push(chunk) {
    queue.push({ chunk });
    if (wake) {
      const resolve = wake;
      wake = null;
      resolve();
    }
    await tick();
    await tick();
  }

  async function close() {
    queue.push({ done: true });
    if (wake) {
      const resolve = wake;
      wake = null;
      resolve();
    }
    await tick();
  }

  return { stream, push, close };
}

function chunk(overrides) {
  return {
    status: 'content',
    content: null,
    node_id: 'node-1',
    conversation_id: 'conv-1',
    tokens_used: 0,
    ...overrides,
  };
}

const { StreamManager, STREAM_DURATION_UPDATE_MS } = require(path.join(__dirname, '../src/services/streamManager.ts'));
const { messageApi } = require(path.join(__dirname, '../src/api/message.ts'));
const { runsApi } = require(path.join(__dirname, '../src/api/runs.ts'));
const { getGenerationStatusText, getStreamStatusText } = require(path.join(__dirname, '../src/utils/generationStatus.ts'));

async function withManager(run) {
  resetTimers();
  installWindowTimers();
  const originalStream = messageApi.stream;
  const originalStreamPlanApproval = messageApi.streamPlanApproval;
  const originalStreamPlanAnswer = messageApi.streamPlanAnswer;
  const originalAttach = runsApi.attach;
  const originalStop = runsApi.stop;
  const manager = new StreamManager();
  try {
    await run(manager);
  } finally {
    messageApi.stream = originalStream;
    messageApi.streamPlanApproval = originalStreamPlanApproval;
    messageApi.streamPlanAnswer = originalStreamPlanAnswer;
    runsApi.attach = originalAttach;
    runsApi.stop = originalStop;
    manager.resetAll();
  }
}

async function testFlushesReasoningBeforeContentStarts() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const reasoning = '思考缓冲'.repeat(120);
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'reasoning', reasoning }));
    await controlled.push(chunk({ event_type: 'text', content: '主回复' }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.reasoning, reasoning);
      assert.equal(state.reasoningActive, false);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testFlushesBufferedTextIntoSingleToolCall() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const content = '工具前说明'.repeat(80);
    const toolCall = {
      id: 'call-1',
      type: 'function',
      function: { name: 'read_file', arguments: '{"path":"notes.txt"}' },
    };
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'text', content }));
    await controlled.push(chunk({ event_type: 'tool_call', tool_call: toolCall }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.content, '');
      assert.equal(state.toolInteractions.length, 1);
      assert.equal(state.toolInteractions[0].assistant.content, content);
      assert.deepEqual(state.toolInteractions[0].assistant.tool_calls, [toolCall]);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testMergesToolResultIntoExistingInteraction() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const toolCall = {
      id: 'call-1',
      type: 'function',
      function: { name: 'run_command', arguments: '{"command":"echo hello"}' },
    };
    const toolResult = {
      tool_call_id: 'call-1',
      name: 'run_command',
      content: JSON.stringify({
        stdout: 'hello\n',
        stderr: '',
        exit_code: 0,
      }),
    };
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'tool_call', tool_calls: [toolCall] }));
    await controlled.push(chunk({ event_type: 'tool_result', tool_call: toolResult }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.toolInteractions.length, 1);
      assert.equal(state.toolInteractions[0].tools.length, 1);
      assert.deepEqual(state.toolInteractions[0].tools[0], {
        role: 'tool',
        ...toolResult,
      });
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testToolCallStartFlushesBufferedTextBeforeToolCallCompletes() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const content = '工具调用前的说明'.repeat(80);
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'text', content }));
    await controlled.push(chunk({ event_type: 'tool_call_start' }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.content, '');
      assert.equal(state.toolInteractions.length, 1);
      assert.equal(state.toolInteractions[0].assistant.content, content);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testToolCallStartCreatesRunningPlaceholder() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const content = '准备调用文件工具。';
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'text', content }));
    await controlled.push(chunk({ event_type: 'tool_call_start' }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.content, '');
      assert.equal(state.toolInteractions.length, 1);
      assert.equal(state.toolInteractions[0].assistant.content, content);
      assert.equal(state.toolInteractions[0].assistant.tool_calls.length, 1);
      assert.equal(state.toolInteractions[0].assistant.tool_calls[0].pending, true);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testToolCallDeltaUpdatesRunningPlaceholder() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const partialToolCall = {
      id: 'call-1',
      type: 'function',
      function: { name: 'write_file', arguments: '{"path":"test_tools.py","content":"' },
    };
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'tool_call_start' }));
    await controlled.push(chunk({
      event_type: 'tool_call',
      tool_call: { tool_calls: [partialToolCall] },
      tool_calls: [partialToolCall],
    }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.toolInteractions.length, 1);
      assert.equal(state.toolInteractions[0].assistant.tool_calls.length, 1);
      assert.equal(state.toolInteractions[0].assistant.tool_calls[0].id, 'call-1');
      assert.equal(state.toolInteractions[0].assistant.tool_calls[0].function.name, 'write_file');
      assert.equal(state.toolInteractions[0].assistant.tool_calls[0].function.arguments, '{"path":"test_tools.py","content":"');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testStreamErrorStatePreservesRealMessage() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ status: 'error', error: 'upstream quota exceeded' }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.status, 'error');
      assert.equal(state.errorMessage, 'upstream quota exceeded');
      assert.equal(getStreamStatusText(state.status, state.errorMessage), 'upstream quota exceeded');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testRequestNodeAndUiAnchorAreIndependent() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    let streamArgs = null;
    messageApi.stream = (...args) => {
      streamArgs = args;
      return controlled.stream();
    };

    const running = manager.startStream(
      'conv-1',
      { content: '继续实现已批准的计划。' },
      '继续实现已批准的计划。',
      undefined,
      'node-current',
    );

    await tick();
    let state = manager.getConversationStates('conv-1')[0];
    assert.equal(streamArgs[2], undefined);
    assert.equal(state.anchorNodeId, 'node-current');
    assert.equal(state.nodeId, null);
    assert.equal(state.targetNodeId, null);

    await controlled.push(chunk({
      status: 'start',
      content: null,
      node_id: 'node-new',
      target_node_id: 'node-new',
    }));

    state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.anchorNodeId, 'node-current');
    assert.equal(state.nodeId, 'node-new');
    assert.equal(state.targetNodeId, 'node-new');

    await controlled.close();
    await runTimersUntil(running);
  });
}

async function testPlanApprovalUsesControlStreamWithoutPendingUserMessage() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    let streamArgs = null;
    messageApi.streamPlanApproval = (...args) => {
      streamArgs = args;
      return controlled.stream();
    };

    const running = manager.startPlanApprovalStream(
      'conv-1',
      'plan-1',
      { reasoning_effort: 'medium', thinking_enabled: true },
      'node-current',
    );

    await tick();
    let state = manager.getConversationStates('conv-1')[0];
    assert.equal(streamArgs[0], 'conv-1');
    assert.equal(streamArgs[1], 'plan-1');
    assert.equal(streamArgs[2].node_id, 'node-current');
    assert.equal(state.pendingUserMessage, null);
    assert.equal(state.anchorNodeId, 'node-current');
    assert.equal(state.metadata.origin, 'plan_approval');

    await controlled.push(chunk({
      status: 'start',
      content: null,
      node_id: 'node-new',
      target_node_id: 'node-new',
    }));

    state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.targetNodeId, 'node-new');
    await controlled.close();
    await runTimersUntil(running);
  });
}

async function testPlanQuestionAnswerUsesControlStreamWithoutPendingUserMessage() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    let streamArgs = null;
    messageApi.streamPlanAnswer = (...args) => {
      streamArgs = args;
      return controlled.stream();
    };

    const running = manager.startPlanAnswerStream(
      'conv-1',
      'plan-1',
      '默认显示',
      {},
      'node-current',
    );

    await tick();
    const state = manager.getConversationStates('conv-1')[0];
    assert.equal(streamArgs[2].answer, '默认显示');
    assert.equal(streamArgs[2].node_id, 'node-current');
    assert.equal(state.pendingUserMessage, null);
    assert.equal(state.metadata.origin, 'plan_question_answer');

    await controlled.close();
    await runTimersUntil(running);
  });
}

async function testBlockingRunAliasesFollowServerRunId() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: 'hello' });
    const clientRunId = manager.getConversationStates('conv-1')[0].runId;

    await controlled.push(chunk({
      run_id: 'run-server-1',
      status: 'content',
      content: 'hello',
    }));

    try {
      assert.equal(manager.areRunsInactive([clientRunId]), false);
      await controlled.push(chunk({
        run_id: 'run-server-1',
        status: 'complete',
        content: null,
      }));
      assert.equal(manager.areRunsInactive([clientRunId]), true);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testWaitingApprovalRunStaysBlockingAndCanBeStopped() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    const stoppedRunIds = [];
    runsApi.attach = controlled.stream;
    runsApi.stop = async (runId) => {
      stoppedRunIds.push(runId);
    };
    const running = manager.resumeStream('conv-1', null, 'run_waiting', 0);

    await controlled.push({
      type: 'run_started',
      run_id: 'run_waiting',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'running',
    });
    await controlled.push({
      run_id: 'run_waiting',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'waiting_approval',
      event_type: 'tool_approval_request',
      approval: {
        id: 'approval-1',
        status: 'pending',
        tool_name: 'run_command',
      },
    });

    try {
      assert.equal(manager.getConversationStates('conv-1')[0].status, 'waiting_approval');
      assert.equal(manager.areRunsInactive(['run_waiting']), false);
      await manager.stopRun('run_waiting');
      assert.deepEqual(stoppedRunIds, ['run_waiting']);
      assert.equal(manager.getConversationStates('conv-1')[0].status, 'stopping');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testAttachedNotificationRunCanStayAnchoredUntilTargetLands() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream(
      'conv-1',
      null,
      'run_notification',
      0,
      'node-anchor',
      'chat',
      { anchorUntilTargetLands: true },
    );

    await controlled.push({
      type: 'run_target_bound',
      run_id: 'run_notification',
      conversation_id: 'conv-1',
      kind: 'chat',
      target_node_id: 'node-notify',
      status: 'content',
      event_index: 1,
    });
    await controlled.push({
      run_id: 'run_notification',
      conversation_id: 'conv-1',
      kind: 'chat',
      status: 'content',
      node_id: 'node-notify',
      target_node_id: 'node-notify',
      content: '通知回复',
      event_index: 2,
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.anchorNodeId, 'node-anchor');
      assert.equal(state.targetNodeId, 'node-notify');
      assert.equal(state.anchorUntilTargetLands, true);
      assert.equal(state.content, '通知回复');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testPermissionModeChangedEventUpdatesRunStateImmediately() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({
      event_type: 'permission_mode_changed',
      tool_permission_mode: 'plan',
    }));

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.toolPermissionMode, 'plan');
      assert.equal(state.metadata.tool_permission_mode, 'plan');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testStopRunAbortsLocalStreamWithoutWaitingForServerAck() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    let resolveStop;
    messageApi.stream = controlled.stream;
    runsApi.stop = async () => {
      await new Promise((resolve) => {
        resolveStop = resolve;
      });
    };
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push({
      type: 'run_started',
      run_id: 'run_server_slow_stop',
      conversation_id: 'conv-1',
      kind: 'chat',
      status: 'running',
    });

    const stopPromise = manager.stopRun('run_server_slow_stop');
    await tick();

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.abortController.signal.aborted, true);
    } finally {
      resolveStop();
      await stopPromise;
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testResumeStreamPreservesAttachedRunKindBeforeFirstEvent() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', null, 'run_subagent', 0, null, 'subagent');

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.runId, 'run_subagent');
      assert.equal(state.kind, 'subagent');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testRestoreCompletedSideRunFromBackendEvents() {
  await withManager(async (manager) => {
    manager.restoreRunFromEvents(
      {
        run_id: 'run_restored',
        conversation_id: 'conv-1',
        kind: 'subagent',
        status: 'completed',
        anchor_node_id: 'node-anchor',
        target_node_id: null,
        event_count: 3,
        created_at: 10,
        updated_at: 13,
        finished_at: 13,
      },
      [
        {
          type: 'run_started',
          run_id: 'run_restored',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'running',
          anchor_node_id: 'node-anchor',
          target_node_id: null,
          event_index: 0,
        },
        {
          run_id: 'run_restored',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'content',
          content: 'restored result',
          event_index: 1,
        },
        {
          type: 'run_finished',
          run_id: 'run_restored',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'completed',
          event_index: 2,
        },
      ],
    );

    const state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.runId, 'run_restored');
    assert.equal(state.kind, 'subagent');
    assert.equal(state.status, 'completed');
    assert.equal(state.content, 'restored result');
    assert.equal(state.anchorNodeId, 'node-anchor');
    assert.equal(state.abortController, null);
  });
}

async function testRestoreRunDoesNotReviveHistoricalApprovalRequests() {
  await withManager(async (manager) => {
    manager.restoreRunFromEvents(
      {
        run_id: 'run_restored_approval',
        conversation_id: 'conv-1',
        kind: 'subagent',
        status: 'cancelled',
        anchor_node_id: 'node-anchor',
        target_node_id: null,
        event_count: 3,
        created_at: 10,
        updated_at: 13,
        finished_at: 13,
      },
      [
        {
          type: 'run_started',
          run_id: 'run_restored_approval',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'running',
          event_index: 0,
        },
        {
          run_id: 'run_restored_approval',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'waiting_approval',
          event_type: 'tool_approval_request',
          approval: {
            id: 'stale-approval',
            status: 'pending',
            tool_name: 'run_command',
          },
          event_index: 1,
        },
        {
          type: 'run_finished',
          run_id: 'run_restored_approval',
          conversation_id: 'conv-1',
          kind: 'subagent',
          status: 'cancelled',
          event_index: 2,
        },
      ],
    );

    const state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.status, 'stopped');
    assert.deepEqual(state.pendingApprovals, {});
  });
}

async function testRestoreRunKeepsParentSummaryAndMetadata() {
  await withManager(async (manager) => {
    manager.restoreRunFromEvents(
      {
        run_id: 'run_step',
        conversation_id: 'conv-1',
        kind: 'workflow_step',
        status: 'completed',
        anchor_node_id: 'node-anchor',
        target_node_id: null,
        created_by_run_id: 'run_workflow',
        cancellation_parent_run_id: 'run_workflow',
        summary: '检查实现',
        event_count: 1,
        metadata: {
          workflow_step_index: 1,
          workflow_step_name: '检查实现',
        },
        created_at: 10,
        updated_at: 11,
        finished_at: 11,
      },
      [
        {
          type: 'run_started',
          run_id: 'run_step',
          conversation_id: 'conv-1',
          kind: 'workflow_step',
          status: 'running',
          created_by_run_id: 'run_workflow',
          cancellation_parent_run_id: 'run_workflow',
          summary: '检查实现',
          metadata: {
            workflow_step_index: 1,
            workflow_step_name: '检查实现',
          },
          event_index: 0,
        },
      ],
    );

    const state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.createdByRunId, 'run_workflow');
    assert.equal(state.cancellationParentRunId, 'run_workflow');
    assert.equal(state.summary, '检查实现');
    assert.equal(state.metadata.workflow_step_index, 1);
    assert.equal(state.metadata.workflow_step_name, '检查实现');
  });
}

async function testSubagentResultDoesNotAppendAlreadyStreamedContent() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', null, 'run_subagent', 0, 'node-anchor', 'subagent');

    await controlled.push({
      run_id: 'run_subagent',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'content',
      content: 'final answer',
      event_index: 1,
    });
    await controlled.push({
      run_id: 'run_subagent',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'complete',
      event_type: 'subagent_result',
      content: 'final answer',
      event_index: 2,
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.content, 'final answer');
      assert.equal(state.status, 'completed');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testWorkflowResultDoesNotAppendAggregateContentToRunBody() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', null, 'run_workflow', 0, 'node-anchor', 'workflow');

    await controlled.push({
      run_id: 'run_workflow',
      conversation_id: 'conv-1',
      kind: 'workflow',
      status: 'complete',
      event_type: 'workflow_result',
      content: 'workflow summary',
      event_index: 1,
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.content, '');
      assert.equal(state.status, 'completed');
      assert.equal(state.workflowEvents.length, 1);
      assert.equal(state.workflowEvents[0].content, 'workflow summary');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testCommandOutputUsesDedicatedBufferInsteadOfRunContent() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', null, 'run_command', 0, 'node-anchor', 'command');

    await controlled.push({
      run_id: 'run_command',
      conversation_id: 'conv-1',
      kind: 'command',
      status: 'content',
      event_type: 'command_stdout',
      content: 'stdout line\n',
      event_index: 1,
    });
    await controlled.push({
      run_id: 'run_command',
      conversation_id: 'conv-1',
      kind: 'command',
      status: 'content',
      event_type: 'command_stderr',
      content: 'stderr line\n',
      event_index: 2,
    });
    await controlled.push({
      run_id: 'run_command',
      conversation_id: 'conv-1',
      kind: 'command',
      status: 'content',
      event_type: 'command_exited',
      exit_code: 0,
      event_index: 3,
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.kind, 'command');
      assert.equal(state.content, '');
      assert.equal(state.command.stdout, 'stdout line\n');
      assert.equal(state.command.stderr, 'stderr line\n');
      assert.equal(state.command.exitCode, 0);
      assert.equal(state.command.status, 'completed');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testChildRunStartedEventAddsSideRunNotification() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push({
      type: 'child_run_started',
      event_type: 'child_run_started',
      run_id: 'run-parent',
      conversation_id: 'conv-1',
      kind: 'chat',
      status: 'content',
      child_run_id: 'run-child',
      child_kind: 'subagent',
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.deepEqual(state.sideRunNotifications, [{ runId: 'run-child', kind: 'subagent' }]);
      assert.equal(state.createdByRunId, null);
      assert.equal(state.cancellationParentRunId, null);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testGetStatePrefersActiveStreamingRunOverNewerError() {
  await withManager(async (manager) => {
    const oldStream = createControlledStream();
    const newStream = createControlledStream();
    let streamCount = 0;
    messageApi.stream = () => {
      streamCount += 1;
      return streamCount === 1 ? oldStream.stream() : newStream.stream();
    };

    const oldRunning = manager.startStream('conv-1', { content: 'old' });
    await oldStream.push(chunk({
      run_id: 'run-old',
      node_id: 'node-old',
      status: 'content',
      content: 'still running',
    }));
    const newRunning = manager.startStream('conv-1', { content: 'new' });
    await newStream.push(chunk({
      run_id: 'run-new',
      node_id: 'node-new',
      status: 'error',
      error: 'failed quickly',
    }));

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.status, 'streaming');
      assert.equal(state.runId, 'run-old');
    } finally {
      await oldStream.close();
      await newStream.close();
      await runTimersUntil(oldRunning);
      await runTimersUntil(newRunning);
    }
  });
}

async function testStopUsesServerRunIdBeforeTargetNodeArrives() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    const stoppedRunIds = [];
    messageApi.stream = controlled.stream;
    runsApi.stop = async (runId) => {
      stoppedRunIds.push(runId);
    };
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push({
      type: 'run_started',
      run_id: 'run_server_early',
      conversation_id: 'conv-1',
      kind: 'chat',
      status: 'running',
    });

    try {
      await manager.stopRun('run_server_early');
      assert.deepEqual(stoppedRunIds, ['run_server_early']);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testStopUsesRunsApiForAnyServerRunId() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    const stoppedRunIds = [];
    runsApi.attach = controlled.stream;
    runsApi.stop = async (runId) => {
      stoppedRunIds.push(runId);
    };
    const running = manager.resumeStream('conv-1', null, 'srv-child-1', 0, 'node-1', 'subagent');

    await controlled.push({
      type: 'run_started',
      run_id: 'srv-child-1',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'running',
    });

    try {
      await manager.stopRun('srv-child-1');
      assert.deepEqual(stoppedRunIds, ['srv-child-1']);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testStoppingStatusIsTrackedExplicitly() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', null, 'run-stopping', 0, 'node-1', 'subagent');

    await controlled.push({
      type: 'run_stop_requested',
      run_id: 'run-stopping',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'stopping',
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.status, 'stopping');
      assert.equal(getStreamStatusText(state.status), '正在停止');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testStoppingStreamFinishesAsStopped() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    const finishes = [];
    runsApi.attach = controlled.stream;
    const unsubscribe = manager.onFinish((info) => finishes.push(info));
    const running = manager.resumeStream('conv-1', null, 'run-stopping-final', 0, 'node-1', 'subagent');

    await controlled.push({
      type: 'run_stop_requested',
      run_id: 'run-stopping-final',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'stopping',
    });
    await controlled.close();
    await runTimersUntil(running);
    unsubscribe();

    const state = manager.getConversationStates('conv-1')[0];
    assert.equal(state.status, 'stopped');
    assert.equal(finishes.length, 1);
    assert.equal(finishes[0].status, 'stopped');
  });
}

async function testRunFinishedFailedMapsToErrorState() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', '', 'run-failed', 0);

    await controlled.push({
      type: 'run_started',
      run_id: 'run-failed',
      conversation_id: 'conv-1',
      kind: 'workflow',
      status: 'running',
    });
    await controlled.push({
      type: 'run_finished',
      run_id: 'run-failed',
      conversation_id: 'conv-1',
      kind: 'workflow',
      status: 'failed',
      error: 'workflow failed',
    });

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.status, 'error');
      assert.equal(state.errorMessage, 'workflow failed');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testRunFinishedCancelledMapsToStoppedState() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    runsApi.attach = controlled.stream;
    const running = manager.resumeStream('conv-1', '', 'run-cancelled', 0);

    await controlled.push({
      type: 'run_started',
      run_id: 'run-cancelled',
      conversation_id: 'conv-1',
      kind: 'workflow',
      status: 'running',
    });
    await controlled.push({
      type: 'run_finished',
      run_id: 'run-cancelled',
      conversation_id: 'conv-1',
      kind: 'workflow',
      status: 'cancelled',
    });

    try {
      const state = manager.getState('conv-1');
      assert.equal(state.status, 'stopped');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testCompletedDirectResponseCanBeArchivedAndRemovedFromActiveRuns() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: '/status' }, '/status');

    await controlled.push({
      type: 'run_started',
      run_id: 'run-status',
      conversation_id: 'conv-1',
      kind: 'direct_response',
      status: 'running',
    });
    await controlled.push({
      event_type: 'text',
      run_id: 'run-status',
      conversation_id: 'conv-1',
      kind: 'direct_response',
      status: 'content',
      content: 'All systems nominal',
    });
    await controlled.push({
      type: 'run_finished',
      run_id: 'run-status',
      conversation_id: 'conv-1',
      kind: 'direct_response',
      status: 'completed',
    });

    try {
      const archived = manager.archiveRun('run-status');
      assert.equal(archived.runId, 'run-status');
      assert.equal(archived.kind, 'direct_response');
      assert.equal(archived.status, 'completed');
      assert.equal(archived.content, 'All systems nominal');
      assert.equal(archived.pendingUserMessage, '/status');
      assert.deepEqual(manager.getConversationStates('conv-1'), []);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testCoalescesContentNotificationsAndFlushesCompletionImmediately() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const changedIds = [];
    const unsubscribe = manager.subscribe((conversationId) => {
      changedIds.push(conversationId);
    });
    const running = manager.startStream('conv-1', { content: 'hello' });

    await controlled.push(chunk({ event_type: 'text', content: 'a' }));
    await controlled.push(chunk({ event_type: 'text', content: 'b' }));
    assert.deepEqual(changedIds, ['conv-1']);

    await flushTimersOnce();
    assert.deepEqual(changedIds, ['conv-1', 'conv-1']);

    await controlled.push(chunk({ status: 'complete', content: null }));
    assert.deepEqual(changedIds, ['conv-1', 'conv-1', 'conv-1']);

    try {
      assert.equal(manager.getConversationStates('conv-1')[0].content, 'ab');
    } finally {
      unsubscribe();
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testDurationNotificationsUseCoarseInterval() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: 'hello' });

    try {
      assert.deepEqual([...intervalDelays.values()], [STREAM_DURATION_UPDATE_MS]);
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

async function testWaitingApprovalStatusIsVisible() {
  await withManager(async (manager) => {
    const controlled = createControlledStream();
    messageApi.stream = controlled.stream;
    const running = manager.startStream('conv-1', { content: '/fork inspect' }, '/fork inspect');

    await controlled.push({
      run_id: 'run-fork',
      conversation_id: 'conv-1',
      kind: 'subagent',
      status: 'waiting_approval',
      event_type: 'tool_approval_request',
      approval: {
        id: 'approval-1',
        status: 'pending',
        tool_name: 'run_command',
      },
    });

    try {
      const state = manager.getConversationStates('conv-1')[0];
      assert.equal(state.status, 'waiting_approval');
      assert.equal(getStreamStatusText(state.status), '等待工具审批');
    } finally {
      await controlled.close();
      await runTimersUntil(running);
    }
  });
}

function testGenerationStatusUsesPersistedErrorMessage() {
  assert.equal(
    getGenerationStatusText({ status: 'error', error_message: 'provider authentication failed' }),
    'provider authentication failed',
  );
  assert.equal(getGenerationStatusText({ status: 'error', error_message: '' }), '生成出错');
  assert.equal(getGenerationStatusText({ status: 'stopped', error_message: null }), '已停止');
  assert.equal(getGenerationStatusText({ status: 'completed', error_message: 'ignored' }), null);
}

async function main() {
  await testFlushesReasoningBeforeContentStarts();
  await testFlushesBufferedTextIntoSingleToolCall();
  await testMergesToolResultIntoExistingInteraction();
  await testToolCallStartFlushesBufferedTextBeforeToolCallCompletes();
  await testToolCallStartCreatesRunningPlaceholder();
  await testToolCallDeltaUpdatesRunningPlaceholder();
  await testStreamErrorStatePreservesRealMessage();
  await testRequestNodeAndUiAnchorAreIndependent();
  await testPlanApprovalUsesControlStreamWithoutPendingUserMessage();
  await testPlanQuestionAnswerUsesControlStreamWithoutPendingUserMessage();
  await testBlockingRunAliasesFollowServerRunId();
  await testWaitingApprovalRunStaysBlockingAndCanBeStopped();
  await testAttachedNotificationRunCanStayAnchoredUntilTargetLands();
  await testPermissionModeChangedEventUpdatesRunStateImmediately();
  await testStopRunAbortsLocalStreamWithoutWaitingForServerAck();
  await testResumeStreamPreservesAttachedRunKindBeforeFirstEvent();
  await testRestoreCompletedSideRunFromBackendEvents();
  await testRestoreRunDoesNotReviveHistoricalApprovalRequests();
  await testRestoreRunKeepsParentSummaryAndMetadata();
  await testSubagentResultDoesNotAppendAlreadyStreamedContent();
  await testWorkflowResultDoesNotAppendAggregateContentToRunBody();
  await testCommandOutputUsesDedicatedBufferInsteadOfRunContent();
  await testChildRunStartedEventAddsSideRunNotification();
  await testGetStatePrefersActiveStreamingRunOverNewerError();
  await testStopUsesServerRunIdBeforeTargetNodeArrives();
  await testStopUsesRunsApiForAnyServerRunId();
  await testStoppingStatusIsTrackedExplicitly();
  await testStoppingStreamFinishesAsStopped();
  await testRunFinishedFailedMapsToErrorState();
  await testRunFinishedCancelledMapsToStoppedState();
  await testCompletedDirectResponseCanBeArchivedAndRemovedFromActiveRuns();
  await testCoalescesContentNotificationsAndFlushesCompletionImmediately();
  await testDurationNotificationsUseCoarseInterval();
  await testWaitingApprovalStatusIsVisible();
  testGenerationStatusUsesPersistedErrorMessage();
  console.log('streamManager tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
