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
  compactTaskTitle,
  createTaskPanelItem,
  shouldPollTaskState,
  taskStatusLabel,
} = require(path.join(__dirname, '../src/utils/activeTask.ts'));

function task(overrides = {}) {
  return {
    title: 'Inspect task model',
    detail: '',
    status: 'pending',
    execution_state: 'idle',
    active_run_id: null,
    active_step: null,
    steps: [],
    ...overrides,
  };
}

assert.equal(taskStatusLabel('pending'), '待处理');
assert.equal(taskStatusLabel('running'), '运行中');
assert.equal(taskStatusLabel('stopping'), '停止中');
assert.equal(taskStatusLabel('completed'), '已完成');
assert.equal(taskStatusLabel('blocked'), '已阻塞');
assert.equal(taskStatusLabel('custom'), 'custom');

assert.equal(compactTaskTitle(task({ title: '  a   b   c  ' }), 20), 'a b c');
assert.equal(compactTaskTitle(task({ title: 'abcdefghijklmnopqrstuvwxyz' }), 10), 'abcdefg...');
assert.equal(compactTaskTitle(task({ title: '', detail: 'fallback detail' }), 50), 'fallback detail');
assert.equal(createTaskPanelItem(null), null);

const panelItem = createTaskPanelItem(task({
  execution_state: 'running',
  active_run_id: 'run-1',
  active_step: 2,
  steps: [
    { position: 1, status: 'completed', title: 'Prepare', detail: '', evidence_summary: 'ok' },
    { position: 2, status: 'pending', title: 'Execute', detail: '', evidence_summary: '' },
  ],
}));
assert.equal(panelItem.statusLabel, '运行中');
assert.equal(panelItem.progressText, '1/2 步');
assert.deepEqual(panelItem.steps.map((step) => [step.step.position, step.statusLabel, step.running]), [
  [1, '已完成', false],
  [2, '待处理', true],
]);

const blocked = createTaskPanelItem(task({
  status: 'blocked',
  steps: [{ position: 1, status: 'blocked', title: 'Verify', detail: '', evidence_summary: 'failed' }],
}));
assert.equal(blocked.statusLabel, '已阻塞');
assert.equal(blocked.steps[0].statusLabel, '已阻塞');

assert.equal(shouldPollTaskState({ conversationId: null, activeRunCount: 1 }), false);
assert.equal(shouldPollTaskState({ conversationId: 'conv-1' }), false);
assert.equal(shouldPollTaskState({ conversationId: 'conv-1', activeRunCount: 1 }), true);
assert.equal(shouldPollTaskState({ conversationId: 'conv-1', activeTask: task() }), true);
assert.equal(shouldPollTaskState({ conversationId: 'conv-1', visibleNotificationCount: 1 }), true);

console.log('activeTask tests passed');
