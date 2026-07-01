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
  const originalAttach = runsApi.attach;
  const originalStop = runsApi.stop;
  const manager = new StreamManager();
  try {
    await run(manager);
  } finally {
    messageApi.stream = originalStream;
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
  await testBlockingRunAliasesFollowServerRunId();
  await testGetStatePrefersActiveStreamingRunOverNewerError();
  await testStopUsesServerRunIdBeforeTargetNodeArrives();
  await testRunFinishedFailedMapsToErrorState();
  await testRunFinishedCancelledMapsToStoppedState();
  await testCoalescesContentNotificationsAndFlushesCompletionImmediately();
  await testDurationNotificationsUseCoarseInterval();
  testGenerationStatusUsesPersistedErrorMessage();
  console.log('streamManager tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
