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
  isOpenTask,
  sortTasksForDisplay,
  taskOwnerLabel,
  taskStatusLabel,
} = require(path.join(__dirname, '../src/utils/taskLedger.ts'));

function task(overrides) {
  return {
    task_id: 'task_1',
    conversation_id: 'conv_1',
    title: 'Inspect plan mode',
    detail: '',
    status: 'pending',
    owner_type: 'assistant',
    owner_run_id: null,
    created_by_run_id: null,
    evidence_run_id: null,
    evidence_summary: '',
    metadata: {},
    created_at: 1,
    updated_at: 1,
    finished_at: null,
    ...overrides,
  };
}

assert.equal(isOpenTask(task({ status: 'pending' })), true);
assert.equal(isOpenTask(task({ status: 'in_progress' })), true);
assert.equal(isOpenTask(task({ status: 'blocked' })), true);
assert.equal(isOpenTask(task({ status: 'completed' })), false);
assert.equal(isOpenTask(task({ status: 'cancelled' })), false);

assert.equal(taskStatusLabel('pending'), '待处理');
assert.equal(taskStatusLabel('in_progress'), '进行中');
assert.equal(taskStatusLabel('completed'), '已完成');
assert.equal(taskStatusLabel('blocked'), '已阻塞');
assert.equal(taskStatusLabel('cancelled'), '已取消');
assert.equal(taskStatusLabel('custom'), 'custom');

assert.equal(taskOwnerLabel('subagent'), '后台分支');
assert.equal(taskOwnerLabel('workflow'), 'Workflow');
assert.equal(taskOwnerLabel('command'), '后台命令');
assert.equal(taskOwnerLabel('assistant'), '主对话');

assert.equal(compactTaskTitle(task({ title: '  a   b   c  ' }), 20), 'a b c');
assert.equal(compactTaskTitle(task({ title: 'abcdefghijklmnopqrstuvwxyz' }), 10), 'abcdefg...');
assert.equal(compactTaskTitle(task({ title: '', detail: 'fallback detail' }), 50), 'fallback detail');

assert.deepEqual(
  sortTasksForDisplay([
    task({ task_id: 'done', status: 'completed', updated_at: 100 }),
    task({ task_id: 'progress_old', status: 'in_progress', updated_at: 10 }),
    task({ task_id: 'blocked', status: 'blocked', updated_at: 30 }),
    task({ task_id: 'pending', status: 'pending', updated_at: 20 }),
    task({ task_id: 'progress_new', status: 'in_progress', updated_at: 40 }),
  ]).map((item) => item.task_id),
  ['blocked', 'progress_new', 'progress_old', 'pending', 'done'],
);

console.log('taskLedger tests passed');
