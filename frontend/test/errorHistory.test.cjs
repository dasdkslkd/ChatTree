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

const errorHistoryModule = require('../src/utils/errorHistory.ts');
const sonner = require('sonner');
const originalError = sonner.toast.error;
const { toast } = require('../src/utils/toast.ts');

async function main() {
  errorHistoryModule.clearErrorHistory();
  assert.equal(errorHistoryModule.getErrorHistory().length, 0);

  let notified = 0;
  const unsubscribe = errorHistoryModule.subscribeErrorHistory(() => { notified += 1; });

  errorHistoryModule.recordError('第一条错误');
  assert.equal(notified, 1);
  let history = errorHistoryModule.getErrorHistory();
  assert.equal(history.length, 1);
  assert.equal(history[0].message, '第一条错误');
  assert.equal(typeof history[0].id, 'string');
  assert.equal(typeof history[0].time, 'number');

  errorHistoryModule.recordError('第二条错误');
  assert.equal(notified, 2);
  history = errorHistoryModule.getErrorHistory();
  assert.equal(history.length, 2);
  assert.equal(history[1].message, '第二条错误');

  // 取消订阅后不再通知
  unsubscribe();
  errorHistoryModule.recordError('第三条错误');
  assert.equal(notified, 2);

  // 清空
  errorHistoryModule.clearErrorHistory();
  assert.equal(errorHistoryModule.getErrorHistory().length, 0);

  // monkey-patch 生效：toast.error 已被替换为带历史记录版本
  assert.notEqual(toast.error, originalError, 'toast.error 应被替换为错误历史版本');

  // 字符串错误消息写入历史
  toast.error('toast 错误消息');
  history = errorHistoryModule.getErrorHistory();
  assert.equal(history.length, 1);
  assert.equal(history[0].message, 'toast 错误消息');

  // 非字符串消息（ReactNode 对象）不写入历史
  toast.error({ message: '对象消息' });
  assert.equal(errorHistoryModule.getErrorHistory().length, 1);

  // 上限裁剪：超过 MAX_ENTRIES 后保留最新 200 条
  errorHistoryModule.clearErrorHistory();
  for (let i = 0; i < 205; i += 1) {
    errorHistoryModule.recordError('err-' + i);
  }
  history = errorHistoryModule.getErrorHistory();
  assert.equal(history.length, 200);
  assert.equal(history[0].message, 'err-5');
  assert.equal(history[199].message, 'err-204');

  errorHistoryModule.clearErrorHistory();
  console.log('errorHistory tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
