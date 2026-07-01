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
  getTreeUserContent,
  isTaskNotificationMessage,
  shouldExportMessage,
} = require(path.join(__dirname, '../src/utils/taskNotificationVisibility.ts'));

const notificationMessage = {
  role: 'user',
  subtype: 'task_notification',
  content: '<task-notification>{"content":"secret"}</task-notification>',
};

assert.equal(isTaskNotificationMessage(notificationMessage), true);
assert.equal(shouldExportMessage(notificationMessage), false);
assert.equal(getTreeUserContent({
  user_content: '<task-notification>{"content":"secret"}</task-notification>',
  user_subtype: 'task_notification',
}), '');
assert.equal(getTreeUserContent({
  user_content: 'normal user task',
  user_subtype: null,
}), 'normal user task');

console.log('taskNotificationVisibility tests passed');
