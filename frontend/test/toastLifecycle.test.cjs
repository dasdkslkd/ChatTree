const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');

require.extensions['.tsx'] = function loadTsx(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      jsx: ts.JsxEmit.ReactJSX,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

// sonner.tsx 用 event.relatedTarget instanceof Node 判断是否移入 toast 内部，
// Node.js 测试环境需要提供同名全局。
const MockNode = class MockNode {};
const originalNode = globalThis.Node;

const genericComponent = () => null;
const dismissCalls = [];
const sonnerMock = {
  Toaster: genericComponent,
  toast: { dismiss: (id) => { dismissCalls.push(id); } },
};

const originalLoad = Module._load;
Module._load = function loadStub(request, parent, isMain) {
  if (request === 'lucide-react') {
    return {
      CircleCheckIcon: genericComponent,
      InfoIcon: genericComponent,
      Loader2Icon: genericComponent,
      OctagonXIcon: genericComponent,
      TriangleAlertIcon: genericComponent,
    };
  }
  if (request === 'sonner') return sonnerMock;
  if (request === '@/store/themeStore') return { useThemeStore: () => ({ resolvedTheme: 'light' }) };
  if (request === 'react/jsx-runtime') {
    return {
      Fragment: Symbol('Fragment'),
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    };
  }
  if (request.endsWith('.css')) return {};
  return originalLoad.call(this, request, parent, isMain);
};

function makeToastEl({ type, testId, containsResult }) {
  let reflows = 0;
  const el = {
    dataset: type ? { type } : {},
    style: {},
    contains: () => (containsResult === undefined ? false : containsResult),
    get offsetWidth() { reflows += 1; return 0; },
    getAttribute(name) {
      if (name === 'data-testid') return testId ?? null;
      if (name === 'data-sonner-toast') return '';
      return null;
    },
    closest() { return el; },
  };
  return { el, reflows: () => reflows };
}

async function main() {
  globalThis.Node = MockNode;
  try {
    const { Toaster } = require('../src/components/ui/sonner.tsx');
    const rendered = Toaster({});
    assert.equal(rendered.type, 'div', '外层 div 承载 toast 生命周期事件');
    const { onMouseOut, onAnimationEnd } = rendered.props;
    assert.equal(typeof onMouseOut, 'function');
    assert.equal(typeof onAnimationEnd, 'function');

    // 移出：无 toast 元素（target 不在 toast 内）→ 不重启
    {
      const { el, reflows } = makeToastEl({ testId: 'app-toast-1' });
      el.closest = () => null;
      onMouseOut({ target: el, relatedTarget: null });
      assert.equal(reflows(), 0);
      assert.deepEqual(el.style, {}, '无 toast 元素时不应改动任何样式');
    }

    // 移出：loading toast 不重启（loading 无生命周期动画）
    {
      const { el, reflows } = makeToastEl({ type: 'loading', testId: 'app-toast-1' });
      onMouseOut({ target: el, relatedTarget: null });
      assert.equal(reflows(), 0);
    }

    // 移出到 toast 内部（如关闭按钮）→ 不重启，仅 hover 暂停
    {
      const { el, reflows } = makeToastEl({ testId: 'app-toast-1', containsResult: true });
      onMouseOut({ target: el, relatedTarget: new MockNode() });
      assert.equal(reflows(), 0);
    }

    // 真正移出 toast → 重启动画（触发 reflow），让停留 1s + 渐变 1s 从头再来
    {
      const { el, reflows } = makeToastEl({ testId: 'app-toast-1', containsResult: false });
      onMouseOut({ target: el, relatedTarget: new MockNode() });
      assert.equal(reflows(), 1, '移开应强制重排以重启 CSS 动画');
      assert.equal(el.style.animation, '', '重启后动画样式还原，由 CSS 继续驱动');
    }

    // 动画结束：非本 toast 动画名 → 不移除
    {
      dismissCalls.length = 0;
      const { el } = makeToastEl({ testId: 'app-toast-1' });
      onAnimationEnd({ animationName: 'sonner-toast-enter', target: el });
      assert.deepEqual(dismissCalls, [], '其它动画结束不应触发移除');
    }

    // 动画结束：自动渐变动画结束 → dismiss 对应 toast id
    {
      dismissCalls.length = 0;
      const { el } = makeToastEl({ testId: 'app-toast-1' });
      onAnimationEnd({ animationName: 'sonner-toast-auto-fade', target: el });
      assert.deepEqual(dismissCalls, ['app-toast-1'], '渐变结束后应真正移除该 toast');
    }

    // App.css 契约：停留 1s + 渐变 1s、hover 暂停、reduced-motion 回退
    const css = fs.readFileSync(path.join(__dirname, '../src/App.css'), 'utf8');
    assert.match(css, /@keyframes sonner-toast-auto-fade/);
    assert.match(css, /animation: sonner-toast-auto-fade 1s ease forwards/);
    assert.match(css, /animation-delay: 1s/, '前 1s 停留再开始渐变');
    assert.match(css, /\[data-sonner-toast\]:not\(\[data-type="loading"\]\):hover\s*\{\s*animation-play-state: paused;/);
    assert.match(css, /@media \(prefers-reduced-motion: reduce\)[\s\S]*?animation: none;/);

    console.log('toast lifecycle tests passed');
  } finally {
    Module._load = originalLoad;
    if (originalNode === undefined) delete globalThis.Node;
    else globalThis.Node = originalNode;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});