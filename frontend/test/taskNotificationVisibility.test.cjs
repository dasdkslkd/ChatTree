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
  getTaskNotificationSummary,
  getTreeUserContent,
  createTaskNotificationTranscriptItem,
  hasTaskNotificationTranscriptItem,
  isRenderableTaskNotificationMessage,
  isTaskNotificationMessage,
  shouldExportMessage,
} = require(path.join(__dirname, '../src/utils/taskNotificationVisibility.ts'));

const notificationMessage = {
  role: 'user',
  subtype: 'task_notification',
  content: '<task-notification>{"content":"secret"}</task-notification>',
};

assert.equal(isTaskNotificationMessage(notificationMessage), true);
assert.equal(isRenderableTaskNotificationMessage(notificationMessage), true);
assert.equal(shouldExportMessage(notificationMessage), false);
assert.deepEqual(getTaskNotificationSummary({
  role: 'user',
  subtype: 'task_notification',
  content: `<task-notification>
{
  "kind": "task_notification",
  "source_run_kind": "command",
  "source_status": "completed",
  "original_slash_input": "/command npm run build",
  "content": "{\\"command\\":\\"npm run build -- --mode production\\",\\"stdout_tail\\":\\"build completed successfully\\\\n\\",\\"stderr_tail\\":\\"\\"}"
}
</task-notification>`,
}), {
  title: '后台命令 已完成',
  detail: 'npm run build -- --mode production',
  command: 'npm run build -- --mode production',
  output: 'build completed successfully',
  status: '已完成',
  kind: '后台命令',
});
assert.equal(shouldExportMessage({
  role: 'assistant',
  metadata: { message_kind: 'task_notification' },
  content: 'secret',
}), false);
assert.equal(isRenderableTaskNotificationMessage({
  role: 'user',
  metadata: { display: 'hidden' },
  content: 'secret',
}), false);
assert.equal(shouldExportMessage({
  role: 'user',
  metadata: { display: 'hidden' },
  content: 'secret',
}), false);
assert.equal(shouldExportMessage({
  role: 'user',
  content: '<task-notification>{"content":"secret"}</task-notification>',
}), false);
assert.equal(getTreeUserContent({
  user_content: '<task-notification>{"content":"secret"}</task-notification>',
  user_subtype: 'task_notification',
}), '');
assert.equal(getTreeUserContent({
  user_content: '<task-notification>{"content":"secret"}</task-notification>',
  user_subtype: null,
}), '');
assert.equal(getTreeUserContent({
  user_content: 'secret',
  user_subtype: null,
  metadata: { display: 'hidden' },
}), '');
assert.equal(getTreeUserContent({
  user_content: 'normal user task',
  user_subtype: null,
}), 'normal user task');

const notificationRecord = {
  id: 'notification-1',
  conversation_id: 'conversation-1',
  source_run_id: 'command-run-1',
  source_run_kind: 'command',
  status: 'delivering',
  delivery_node_id: 'anchor-node',
  delivered_run_id: 'delivery-run-1',
  delivered_node_id: 'delivery-node',
  summary: 'Command completed',
  content: 'done',
  payload: { source_status: 'completed', stdout_tail: 'done' },
};
const liveItem = createTaskNotificationTranscriptItem(notificationRecord, {
  runId: 'delivery-run-1',
  nodeId: 'delivery-node',
});
assert.equal(liveItem.type, 'task_notification');
assert.equal(liveItem.run_id, 'delivery-run-1');
assert.equal(liveItem.node_id, 'delivery-node');
assert.equal(liveItem.anchor_node_id, 'anchor-node');
assert.equal(liveItem.status, 'completed');
assert.equal(liveItem.summary, 'Command completed');
assert.equal(liveItem.props.source_run_id, 'command-run-1');
assert.equal(liveItem.props.content, 'done');
assert.equal(hasTaskNotificationTranscriptItem([liveItem], notificationRecord, 'delivery-node'), true);
assert.equal(hasTaskNotificationTranscriptItem([], notificationRecord, 'delivery-node'), false);

console.log('taskNotificationVisibility tests passed');
