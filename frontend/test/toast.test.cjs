const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
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

const calls = [];
const mockToast = (message, data) => {
  calls.push({ type: 'default', message, data });
  return 'default-id';
};
for (const type of ['error', 'success', 'info', 'warning', 'message', 'loading']) {
  mockToast[type] = (message, data) => {
    calls.push({ type, message, data });
    return `${type}-id`;
  };
}
mockToast.dismiss = () => {};
mockToast.promise = () => {};

const originalLoad = Module._load;
Module._load = function loadStub(request, parent, isMain) {
  if (request === 'sonner') return { toast: mockToast };
  return originalLoad.call(this, request, parent, isMain);
};

const originalWindow = globalThis.window;

async function main() {
  try {
    const errorHistory = require('../src/utils/errorHistory.ts');
    const { toast } = require('../src/utils/toast.ts');

    errorHistory.clearErrorHistory();
    calls.length = 0;

    // 基础调用：注入唯一 id/testId，并关闭 sonner 自带计时器（非减弱动效）
    toast('你好');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].type, 'default');
    assert.equal(calls[0].message, '你好');
    assert.equal(calls[0].data.id, 'app-toast-1');
    assert.equal(calls[0].data.testId, 'app-toast-1');
    assert.equal(calls[0].data.duration, Infinity, '默认应关闭 sonner 计时器，由 CSS 动画驱动');

    // 唯一 id 递增
    toast.success('成功');
    assert.equal(calls[1].data.id, 'app-toast-2');
    assert.equal(calls[1].data.testId, 'app-toast-2');

    // 字符串错误消息写入历史，其余类型不写入
    toast.error('请求失败');
    assert.equal(calls[2].type, 'error');
    let history = errorHistory.getErrorHistory();
    assert.equal(history.length, 1);
    assert.equal(history[0].message, '请求失败');

    toast.success('成功消息');
    toast.info('提示消息');
    toast.warning('警告消息');
    toast.message('普通消息');
    assert.equal(errorHistory.getErrorHistory().length, 1, '只有 error 类型写入历史');

    // 非字符串错误消息（ReactNode 对象）不写入历史
    toast.error({ message: '对象消息' });
    assert.equal(errorHistory.getErrorHistory().length, 1);

    // loading 不写入历史
    toast.loading('加载中');
    assert.equal(errorHistory.getErrorHistory().length, 1);

    // 减弱动效：回退到 sonner 自带计时器 2s，保证 toast 仍会消失
    globalThis.window = { matchMedia: () => ({ matches: true }) };
    toast('减弱动效');
    assert.equal(calls[calls.length - 1].data.duration, 2000, 'reduced-motion 时应回退为 2s 计时器');

    // 转发到 sonner 原始方法：返回值透传
    const returned = toast.error('返回值');
    assert.equal(returned, 'error-id');

    console.log('toast tests passed');
  } finally {
    Module._load = originalLoad;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});